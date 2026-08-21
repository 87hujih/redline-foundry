"""针对一个 Workspace cohort 的只读历史事实对账。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

RECONCILIATION_SQL: dict[str, str] = {
    "resource_scope": """
        SELECT count(*) FROM resources AS resource
        WHERE resource.workspace_id = %s AND NOT EXISTS (
            SELECT 1 FROM resource_versions AS version
            WHERE version.resource_id = resource.id
        )
    """,
    "canonical_version": """
        SELECT count(*)
        FROM resources AS resource
        JOIN LATERAL (
            SELECT id FROM resource_versions
            WHERE resource_id = resource.id
            ORDER BY version_number DESC LIMIT 1
        ) AS version ON true
        LEFT JOIN canonical_documents AS document ON document.version_id = version.id
        WHERE resource.workspace_id = %s
          AND (document.version_id IS NULL OR document.workspace_id <> resource.workspace_id
               OR document.resource_id <> resource.id)
    """,
    "canonical_nodes": """
        SELECT count(*)
        FROM canonical_documents AS document
        LEFT JOIN document_nodes AS node
          ON node.version_id = document.version_id AND node.node_id = document.root_node_id
        WHERE document.workspace_id = %s
          AND (node.node_id IS NULL OR node.content_hash !~ '^sha256:[0-9a-f]{64}$')
    """,
    "retrieval_profile": """
        SELECT count(*)
        FROM resource_chunks AS chunk
        JOIN resources AS resource ON resource.id = chunk.resource_id
        WHERE resource.workspace_id = %s
          AND (chunk.chunk_profile IS NULL OR chunk.embedding_profile IS NULL
               OR chunk.embedding_status NOT IN ('pending', 'ready', 'failed'))
    """,
    "approval_scope": """
        SELECT count(*)
        FROM agent_tool_approvals AS approval
        JOIN agent_runs AS run ON run.id = approval.run_id
        WHERE run.workspace_id = %s
          AND approval.workspace_id IS DISTINCT FROM run.workspace_id
    """,
    "commit_bundle": """
        SELECT count(*)
        FROM document_patch_commits AS commit
        LEFT JOIN canonical_documents AS document ON document.version_id = commit.new_version_id
        LEFT JOIN outbox_events AS event ON event.id = commit.outbox_event_id
        WHERE commit.workspace_id = %s
          AND (document.version_id IS NULL OR document.resource_id <> commit.resource_id
               OR event.id IS NULL OR event.event_type <> 'document.version.committed')
    """,
    "outbox": """
        SELECT count(*)
        FROM outbox_events AS event
        JOIN agent_runs AS run ON run.id::text = event.aggregate_id
        WHERE run.workspace_id = %s
          AND (event.status = 'dead_letter'
               OR (event.status = 'publishing' AND event.lease_expires_at < now()))
    """,
    "turn_projection": """
        SELECT count(*)
        FROM agent_turns AS turn
        LEFT JOIN agent_turn_public_projections AS projection ON projection.turn_id = turn.id
        WHERE turn.workspace_id = %s AND turn.status IN
          ('waiting_input', 'waiting_approval', 'succeeded', 'failed', 'cancelled')
          AND (projection.turn_id IS NULL OR projection.status <> turn.status)
    """,
}

HISTORICAL_RECONCILIATION_SQL: dict[str, str] = {
    "unscoped_resources": "SELECT count(*) FROM resources WHERE workspace_id IS NULL",
    "unscoped_tasks": "SELECT count(*) FROM tasks WHERE workspace_id IS NULL",
    "unscoped_legacy_approvals": "SELECT count(*) FROM approvals WHERE workspace_id IS NULL",
    "unscoped_execution_jobs": "SELECT count(*) FROM execution_jobs WHERE workspace_id IS NULL",
    "unscoped_assistant_sessions": (
        "SELECT count(*) FROM assistant_sessions WHERE workspace_id IS NULL"
    ),
    "unscoped_uploaded_files": "SELECT count(*) FROM uploaded_files WHERE workspace_id IS NULL",
    "runtime_scope_gaps": """
        SELECT count(*) FROM agent_runs
        WHERE workspace_id IS NULL OR resource_id IS NULL
           OR principal_type IS NULL OR principal_id IS NULL OR trust_source IS NULL
    """,
    "canonical_scope_mismatch": """
        SELECT count(*)
        FROM canonical_documents AS document
        JOIN resources AS resource ON resource.id = document.resource_id
        WHERE resource.workspace_id IS NULL
           OR document.workspace_id IS DISTINCT FROM resource.workspace_id
    """,
    "turn_scope_mismatch": """
        SELECT count(*)
        FROM agent_turns AS turn
        LEFT JOIN assistant_sessions AS session ON session.id = turn.session_id
        WHERE turn.workspace_id IS NULL
           OR (session.id IS NOT NULL AND session.workspace_id IS DISTINCT FROM turn.workspace_id)
    """,
}


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    workspace_id: str
    mismatch_counts: dict[str, int]

    @property
    def eligible(self) -> bool:
        return bool(self.mismatch_counts) and all(
            value == 0 for value in self.mismatch_counts.values()
        )


class ReconciliationRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def reconcile(self, workspace_id: str) -> ReconciliationReport:
        try:
            UUID(workspace_id)
        except (TypeError, ValueError) as error:
            raise ValueError("workspace_id 必须是 UUID") from error
        results: dict[str, int] = {}
        async with self._pool.connection() as connection:
            for name, query in RECONCILIATION_SQL.items():
                async with connection.cursor() as cursor:
                    await cursor.execute(query, (workspace_id,))
                    row = await cursor.fetchone()
                if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
                    raise RuntimeError(f"对账查询 {name} 返回的数量无效")
                results[name] = row[0]
        return ReconciliationReport(workspace_id, results)


@dataclass(frozen=True, slots=True)
class HistoricalReconciliationReport:
    mismatch_counts: dict[str, int]

    @property
    def eligible(self) -> bool:
        return bool(self.mismatch_counts) and all(
            value == 0 for value in self.mismatch_counts.values()
        )


class HistoricalReconciliationRepository:
    """只读盘点租户化之前及跨 scope 的历史缺口。"""

    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def reconcile(self) -> HistoricalReconciliationReport:
        results: dict[str, int] = {}
        async with self._pool.connection() as connection:
            for name, query in HISTORICAL_RECONCILIATION_SQL.items():
                async with connection.cursor() as cursor:
                    await cursor.execute(query, ())
                    row = await cursor.fetchone()
                if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
                    raise RuntimeError(f"历史对账查询 {name} 返回的数量无效")
                results[name] = row[0]
        return HistoricalReconciliationReport(results)


__all__ = [
    "HISTORICAL_RECONCILIATION_SQL",
    "RECONCILIATION_SQL",
    "HistoricalReconciliationReport",
    "HistoricalReconciliationRepository",
    "ReconciliationReport",
    "ReconciliationRepository",
]
