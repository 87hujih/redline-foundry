"""Workspace-scoped 公开 Run 与 Approval 查询 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from docreview.storage.models import (
    ApprovalSummary,
    ApprovalView,
    Finding,
    PublicRun,
    PublicRunDetail,
    PublicStep,
    PublicToolCall,
    RunSummary,
)
from docreview.storage.postgres.errors import RecordNotFoundError

PUBLIC_RUN_LIST_SQL = """
SELECT run.id::text, run.workspace_id::text, COALESCE(run.resource_id::text, ''),
       COALESCE(run.session_id::text, ''), COALESCE(run.request_id, ''), run.status,
       run.objective, COALESCE(run.current_step, ''),
       (SELECT COUNT(*) FROM agent_steps AS step WHERE step.run_id = run.id)::integer,
       (SELECT COUNT(*) FROM agent_steps AS step
        WHERE step.run_id = run.id AND step.status = 'succeeded')::integer,
       (SELECT COUNT(*) FROM agent_steps AS step
        WHERE step.run_id = run.id AND step.status = 'failed')::integer,
       COALESCE((
           SELECT approval.id::text FROM agent_tool_approvals AS approval
           WHERE approval.workspace_id = %s AND approval.run_id = run.id
             AND approval.status = 'pending'
           ORDER BY approval.created_at, approval.id LIMIT 1
       ), ''),
       run.created_at, run.updated_at
FROM agent_runs AS run
WHERE run.workspace_id = %s
  AND (%s = '' OR run.status = %s)
  AND (NULLIF(%s, '') IS NULL OR run.resource_id = NULLIF(%s, '')::uuid)
ORDER BY run.updated_at DESC, run.id DESC
LIMIT %s
"""

PUBLIC_RUN_DETAIL_SQL = """
SELECT run.id::text, COALESCE(run.resource_id::text, ''),
       COALESCE(run.session_id::text, ''), COALESCE(run.request_id, ''),
       run.status, run.objective, COALESCE(run.current_step, ''),
       run.deadline_at, run.cancel_requested_at, run.created_at, run.updated_at
FROM agent_runs AS run
WHERE run.workspace_id = %s AND run.id = %s
"""

PUBLIC_STEPS_SQL = """
SELECT step.id::text, step.step_key, step.step_type, step.status,
       step.attempt_count, step.max_attempts, step.next_retry_at,
       step.created_at, step.updated_at
FROM agent_steps AS step
JOIN agent_runs AS run ON run.id = step.run_id
WHERE run.workspace_id = %s AND run.id = %s
ORDER BY step.created_at, step.id
"""

PUBLIC_TOOL_CALLS_SQL = """
SELECT call.id::text, call.step_id::text, call.tool_name, call.tool_version,
       call.status, COALESCE(call.error_category, ''), call.started_at, call.completed_at
FROM tool_calls AS call
JOIN agent_runs AS run ON run.id = call.run_id
WHERE run.workspace_id = %s AND run.id = %s
ORDER BY call.created_at, call.id
"""

PUBLIC_APPROVAL_VIEWS_SQL = """
SELECT approval.id::text, approval.run_id::text, approval.step_id::text,
       approval.tool_name, approval.status, approval.created_at, approval.decided_at
FROM agent_tool_approvals AS approval
JOIN agent_runs AS run ON run.id = approval.run_id
WHERE approval.workspace_id = %s AND run.workspace_id = approval.workspace_id
  AND run.id = %s
ORDER BY approval.created_at, approval.id
"""

_APPROVAL_COLUMNS = """
approval.id::text, approval.workspace_id::text, approval.run_id::text, approval.step_id::text,
COALESCE(run.resource_id::text, ''), COALESCE(run.session_id::text, ''), run.objective,
approval.tool_name, approval.tool_version, approval.reason, approval.status,
approval.resources_json, approval.payload_json, COALESCE(approval.decision_reason, ''),
approval.created_at, approval.decided_at
"""

PUBLIC_APPROVAL_LIST_SQL = f"""
SELECT {_APPROVAL_COLUMNS}
FROM agent_tool_approvals AS approval
JOIN agent_runs AS run ON run.id = approval.run_id
 AND run.workspace_id = approval.workspace_id
WHERE approval.workspace_id = %s
  AND (%s = '' OR approval.status = %s)
ORDER BY approval.created_at DESC, approval.id DESC
LIMIT %s
"""

PUBLIC_APPROVAL_DETAIL_SQL = f"""
SELECT {_APPROVAL_COLUMNS}
FROM agent_tool_approvals AS approval
JOIN agent_runs AS run ON run.id = approval.run_id
 AND run.workspace_id = approval.workspace_id
WHERE approval.workspace_id = %s AND approval.id = %s
"""

PUBLIC_RUN_FINDINGS_SQL = """
WITH scoped AS (
    SELECT run.id, run.workspace_id, run.status, run.created_at
    FROM agent_runs AS run
    WHERE run.workspace_id = %s AND run.id = %s
), scoped_outbox AS (
    SELECT event.id::text, event.status, event.created_at
    FROM outbox_events AS event
    WHERE EXISTS (
        SELECT 1 FROM scoped
        WHERE (event.aggregate_type = 'agent_run' AND event.aggregate_id = scoped.id::text)
           OR event.payload_json ->> 'run_id' = scoped.id::text
           OR event.id::text IN (
               SELECT COALESCE(call.output_json #>> '{output,commit,outbox_id}', '')
               FROM tool_calls AS call WHERE call.run_id = scoped.id
           )
    )
), findings AS (
SELECT 1 AS finding_group, step.created_at AS fact_created_at,
       step.id::text AS fact_id, 1 AS finding_order,
       'critical' AS severity, 'expired_step_lease' AS code,
       '运行中步骤的租约已过期\uff1a' || step.id::text AS message
FROM agent_steps AS step
JOIN scoped ON scoped.id = step.run_id
WHERE step.status = 'running' AND step.lease_expires_at <= CURRENT_TIMESTAMP
UNION ALL
SELECT 1, step.created_at, step.id::text, 2,
       'error', 'failed_step', '步骤出现终态错误\uff1a' || step.id::text
FROM agent_steps AS step
JOIN scoped ON scoped.id = step.run_id
WHERE step.status = 'failed'
  AND COALESCE(step.error_json, '{}'::jsonb) NOT IN ('{}'::jsonb, 'null'::jsonb)
UNION ALL
SELECT 2, scoped_outbox.created_at, scoped_outbox.id, 1,
       'critical', 'outbox_dead_letter',
       '发件箱事件需要审核后重放\uff1a' || scoped_outbox.id
FROM scoped_outbox WHERE scoped_outbox.status = 'dead_letter'
UNION ALL
SELECT 3, scoped.created_at, scoped.id::text, 1,
       'critical', 'missing_approval_fact', '运行正在等待审批\uff0c但缺少审批事实'
FROM scoped
WHERE scoped.status = 'waiting_approval' AND NOT EXISTS (
    SELECT 1 FROM agent_tool_approvals AS approval
    WHERE approval.workspace_id = scoped.workspace_id AND approval.run_id = scoped.id
)
)
SELECT severity, code, message
FROM findings
ORDER BY finding_group, fact_created_at, fact_id, finding_order
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...

    async def __aenter__(self) -> AsyncCursor: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...

    async def __aenter__(self) -> AsyncConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


def _optional(value: object) -> str | None:
    text = str(value)
    return text or None


class AgentQueryRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def list_runs(
        self, workspace_id: str, status: str, resource_id: str, limit: int
    ) -> list[RunSummary]:
        params = (
            workspace_id,
            workspace_id,
            status,
            status,
            resource_id,
            resource_id,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(PUBLIC_RUN_LIST_SQL, params)
            rows = await cursor.fetchall()
        return [
            RunSummary(
                id=str(row[0]),
                workspace_id=str(row[1]),
                resource_id=_optional(row[2]),
                session_id=_optional(row[3]),
                request_id=_optional(row[4]),
                status=str(row[5]),
                objective=str(row[6]),
                current_step=_optional(row[7]),
                step_count=cast(int, row[8]),
                completed_step_count=cast(int, row[9]),
                failed_step_count=cast(int, row[10]),
                pending_approval_id=_optional(row[11]),
                created_at=cast(datetime, row[12]),
                updated_at=cast(datetime, row[13]),
            )
            for row in rows
        ]

    async def get_run(self, workspace_id: str, run_id: str) -> PublicRunDetail:
        async with self._pool.connection() as connection:
            run_row = await self._fetchone(
                connection, PUBLIC_RUN_DETAIL_SQL, (workspace_id, run_id)
            )
            if run_row is None:
                raise RecordNotFoundError
            step_rows = await self._fetchall(connection, PUBLIC_STEPS_SQL, (workspace_id, run_id))
            tool_rows = await self._fetchall(
                connection, PUBLIC_TOOL_CALLS_SQL, (workspace_id, run_id)
            )
            approval_rows = await self._fetchall(
                connection, PUBLIC_APPROVAL_VIEWS_SQL, (workspace_id, run_id)
            )
            finding_rows = await self._fetchall(
                connection, PUBLIC_RUN_FINDINGS_SQL, (workspace_id, run_id)
            )

        run = PublicRun(
            id=str(run_row[0]),
            resource_id=_optional(run_row[1]),
            session_id=_optional(run_row[2]),
            request_id=_optional(run_row[3]),
            status=str(run_row[4]),
            objective=str(run_row[5]),
            current_step=_optional(run_row[6]),
            deadline_at=cast(datetime | None, run_row[7]),
            cancel_requested_at=cast(datetime | None, run_row[8]),
            created_at=cast(datetime, run_row[9]),
            updated_at=cast(datetime, run_row[10]),
        )
        steps = [
            PublicStep(
                id=str(row[0]),
                step_key=str(row[1]),
                step_type=str(row[2]),
                status=str(row[3]),
                attempt_count=cast(int, row[4]),
                max_attempts=cast(int, row[5]),
                next_retry_at=cast(datetime | None, row[6]),
                created_at=cast(datetime, row[7]),
                updated_at=cast(datetime, row[8]),
            )
            for row in step_rows
        ]
        tools = [
            PublicToolCall(
                id=str(row[0]),
                step_id=str(row[1]),
                tool_name=str(row[2]),
                tool_version=str(row[3]),
                status=str(row[4]),
                error_category=_optional(row[5]),
                started_at=cast(datetime | None, row[6]),
                completed_at=cast(datetime | None, row[7]),
            )
            for row in tool_rows
        ]
        approvals = [
            ApprovalView(
                id=str(row[0]),
                run_id=str(row[1]),
                step_id=str(row[2]),
                tool_name=str(row[3]),
                status=str(row[4]),
                created_at=cast(datetime, row[5]),
                decided_at=cast(datetime | None, row[6]),
            )
            for row in approval_rows
        ]
        findings = [
            Finding(severity=str(row[0]), code=str(row[1]), message=str(row[2]))
            for row in finding_rows
        ]
        return PublicRunDetail(
            run=run, steps=steps, tool_calls=tools, approvals=approvals, findings=findings
        )

    async def list_approvals(
        self, workspace_id: str, status: str, limit: int
    ) -> list[ApprovalSummary]:
        async with self._pool.connection() as connection:
            rows = await self._fetchall(
                connection, PUBLIC_APPROVAL_LIST_SQL, (workspace_id, status, status, limit)
            )
        return [_approval(row) for row in rows]

    async def get_approval(self, workspace_id: str, approval_id: str) -> ApprovalSummary:
        async with self._pool.connection() as connection:
            row = await self._fetchone(
                connection, PUBLIC_APPROVAL_DETAIL_SQL, (workspace_id, approval_id)
            )
        if row is None:
            raise RecordNotFoundError
        return _approval(row)

    @staticmethod
    async def _fetchone(
        connection: AsyncConnection, query: str, params: tuple[object, ...]
    ) -> tuple[object, ...] | None:
        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchone()

    @staticmethod
    async def _fetchall(
        connection: AsyncConnection, query: str, params: tuple[object, ...]
    ) -> list[tuple[object, ...]]:
        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()


__all__ = [
    "PUBLIC_APPROVAL_DETAIL_SQL",
    "PUBLIC_APPROVAL_LIST_SQL",
    "PUBLIC_APPROVAL_VIEWS_SQL",
    "PUBLIC_RUN_DETAIL_SQL",
    "PUBLIC_RUN_FINDINGS_SQL",
    "PUBLIC_RUN_LIST_SQL",
    "PUBLIC_STEPS_SQL",
    "PUBLIC_TOOL_CALLS_SQL",
    "AgentQueryRepository",
]


def _approval(row: tuple[object, ...]) -> ApprovalSummary:
    return ApprovalSummary(
        id=str(row[0]),
        workspace_id=str(row[1]),
        run_id=str(row[2]),
        step_id=str(row[3]),
        resource_id=_optional(row[4]),
        session_id=_optional(row[5]),
        objective=str(row[6]),
        tool_name=str(row[7]),
        tool_version=str(row[8]),
        reason=str(row[9]),
        status=str(row[10]),
        resources=row[11],
        payload=row[12],
        decision_reason=_optional(row[13]),
        created_at=cast(datetime, row[14]),
        decided_at=cast(datetime | None, row[15]),
    )
