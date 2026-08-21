"""ToolRuntime 使用的 SQL-backed 文档、检索与 Patch scope 适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast

from docreview.document.commit import CommitSnapshot
from docreview.document.patch import PatchSet
from docreview.document.validation import ValidationRequest, ValidationSnapshot
from docreview.knowledge.evidence_service import (
    Candidate,
    EmbeddingProfile,
    EvidenceScope,
    ScoredCandidate,
)
from docreview.tool_runtime.builtin.document import (
    CanonicalDocumentNode,
    CanonicalDocumentVersion,
)
from docreview.tool_runtime.builtin.patch import CommitScope
from docreview.tool_runtime.models import BackendRequest
from docreview.tool_runtime.schema import JSONObject

DOCUMENT_VERSION_SQL = """
SELECT version.id::text, version.resource_id::text, version.version_number,
       version.source, version.created_at
FROM resource_versions AS version
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND resource.id = %s
  AND (%s::text IS NULL OR version.id::text = %s::text)
ORDER BY version.version_number DESC, version.id DESC
LIMIT 1
"""

DOCUMENT_NODES_SQL = """
WITH selected_version AS (
    SELECT version.id
    FROM resource_versions AS version
    JOIN resources AS resource ON resource.id = version.resource_id
    JOIN canonical_documents AS canonical ON canonical.version_id = version.id
    WHERE resource.workspace_id = %s AND version.resource_id = %s
      AND (%s = '' OR version.id::text = %s)
    ORDER BY version.version_number DESC, version.id DESC
    LIMIT 1
)
SELECT node.node_id, node.workspace_id::text, node.resource_id::text, node.version_id::text,
       node.node_type, node.content, node.content_hash, node.sibling_order,
       node.page_mapping_json, node.attributes_json
FROM document_nodes AS node
JOIN selected_version ON selected_version.id = node.version_id
WHERE node.workspace_id = %s AND node.resource_id = %s
  AND node.node_id = ANY(%s::text[])
ORDER BY node.sibling_order, node.node_id
"""

DOCUMENT_SEARCH_SQL = """
WITH selected_version AS (
    SELECT version.id
    FROM resource_versions AS version
    JOIN resources AS resource ON resource.id = version.resource_id
    JOIN canonical_documents AS canonical ON canonical.version_id = version.id
    WHERE resource.workspace_id = %s AND version.resource_id = %s
      AND (%s = '' OR version.id::text = %s)
    ORDER BY version.version_number DESC, version.id DESC
    LIMIT 1
)
SELECT node.node_id, node.workspace_id::text, node.resource_id::text, node.version_id::text,
       node.node_type, node.content, node.content_hash, node.sibling_order,
       node.page_mapping_json, node.attributes_json
FROM document_nodes AS node
JOIN selected_version ON selected_version.id = node.version_id
WHERE node.workspace_id = %s AND node.resource_id = %s
  AND length(node.content) > 0
  AND node.content ILIKE '%%' || %s || '%%'
ORDER BY strpos(lower(node.content), lower(%s)), node.sibling_order, node.node_id
LIMIT %s
"""

EVIDENCE_SCOPE_SQL = """
SELECT version.id::text, COALESCE(version.embedding_profile, ''),
       COALESCE(canonical.chunk_profile, ''), COALESCE(canonical.embedding_profile, '')
FROM resource_versions AS version
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND resource.id = %s
  AND (%s::text IS NULL OR version.id::text = %s::text)
ORDER BY version.version_number DESC, version.id DESC
LIMIT 1
"""

EVIDENCE_LEXICAL_SQL = """
SELECT chunk.id::text, chunk.resource_id::text, chunk.version_id::text,
       COALESCE(chunk.canonical_node_id, chunk.id::text),
       COALESCE(chunk.section_type, 'chunk'), chunk.content, chunk.created_at,
       GREATEST(
           similarity(normalized.haystack, normalized.query),
           CASE WHEN fallback.term_match THEN 0.25 ELSE 0 END
       ),
       chunk.metadata_json, COALESCE(chunk.section_title, ''),
       COALESCE(chunk.window_group_id::text, ''), COALESCE(chunk.order_in_section, 0)
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
CROSS JOIN LATERAL (
    SELECT lower(COALESCE(chunk.section_title, '') || ' ' || chunk.content) AS haystack,
           lower(%s) AS query
) AS normalized
CROSS JOIN LATERAL (
    SELECT EXISTS (
        SELECT 1
        FROM regexp_split_to_table(normalized.query, '[[:space:]]+') AS term(value)
        WHERE length(term.value) >= 2
          AND normalized.haystack LIKE '%%' || term.value || '%%'
    ) AS term_match
) AS fallback
WHERE resource.workspace_id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
  AND (normalized.haystack LIKE '%%' || normalized.query || '%%'
       OR normalized.haystack %% normalized.query
       OR fallback.term_match)
ORDER BY GREATEST(
             similarity(normalized.haystack, normalized.query),
             CASE WHEN fallback.term_match THEN 0.25 ELSE 0 END
         ) DESC,
         chunk.chunk_index
LIMIT %s
"""

EVIDENCE_SEMANTIC_SQL = """
SELECT chunk.id::text, chunk.resource_id::text, chunk.version_id::text,
       COALESCE(chunk.canonical_node_id, chunk.id::text),
       COALESCE(chunk.section_type, 'chunk'), chunk.content, chunk.created_at,
       1 - (chunk.embedding <=> %s::vector), chunk.metadata_json,
       COALESCE(chunk.section_title, ''), COALESCE(chunk.window_group_id::text, ''),
       COALESCE(chunk.order_in_section, 0)
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND chunk.embedding IS NOT NULL AND chunk.embedding_status = 'ready'
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
  AND canonical.embedding_profile = %s
  AND chunk.embedding_profile = %s AND chunk.embedding_model = %s
  AND chunk.embedding_dimensions = %s AND chunk.retrieval_index_version = %s
ORDER BY chunk.embedding <=> %s::vector, chunk.chunk_index
LIMIT %s
"""

LEADING_CHUNKS_SQL = """
SELECT chunk.id::text, chunk.resource_id::text, chunk.version_id::text,
       COALESCE(chunk.canonical_node_id, chunk.id::text),
       COALESCE(chunk.section_type, 'chunk'), chunk.content, chunk.created_at,
       0.25, chunk.metadata_json, COALESCE(chunk.section_title, ''),
       COALESCE(chunk.window_group_id::text, ''), COALESCE(chunk.order_in_section, 0)
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
ORDER BY chunk.chunk_index, chunk.id
LIMIT %s
"""

WINDOW_SIBLINGS_SQL = """
SELECT chunk.id::text, chunk.resource_id::text, chunk.version_id::text,
       COALESCE(chunk.canonical_node_id, chunk.id::text),
       COALESCE(chunk.section_type, 'chunk'), chunk.content, chunk.created_at,
       1.0, chunk.metadata_json, COALESCE(chunk.section_title, ''),
       COALESCE(chunk.window_group_id::text, ''), COALESCE(chunk.order_in_section, 0)
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
  AND chunk.window_group_id::text = %s
ORDER BY chunk.order_in_section, chunk.chunk_index, chunk.id
"""

COMMIT_SCOPE_SQL = """
SELECT run.workspace_id::text, run.resource_id::text, run.principal_type,
       run.principal_id::text, version.id::text
FROM agent_runs AS run
JOIN agent_steps AS step ON step.run_id = run.id
JOIN resources AS resource ON resource.id = run.resource_id
JOIN resource_versions AS version ON version.resource_id = resource.id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE run.id = %s AND step.id = %s AND run.workspace_id = %s
  AND run.resource_id = %s AND run.principal_type = 'user'
ORDER BY version.version_number DESC, version.id DESC
LIMIT 1
"""

AUTHORIZED_NODES_SQL = """
SELECT node.node_id
FROM document_nodes AS node
WHERE node.workspace_id = %s AND node.resource_id = %s AND node.version_id = %s
  AND node.node_id = ANY(%s::text[])
"""

AUTHORIZED_EVIDENCE_SQL = """
SELECT DISTINCT evidence.value->>'evidence_id'
FROM agent_observations AS observation
JOIN agent_runs AS run ON run.id = observation.run_id
CROSS JOIN LATERAL jsonb_array_elements(
    COALESCE(observation.payload_json #> '{output,evidence_set,evidence}', '[]'::jsonb)
) AS evidence(value)
WHERE observation.run_id = %s AND run.workspace_id = %s AND run.resource_id = %s
  AND evidence.value->>'evidence_id' = ANY(%s::text[])
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...] = ()) -> Any: ...
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


class SnapshotStore(Protocol):
    async def load_snapshot(self, workspace_id: str, resource_id: str) -> CommitSnapshot: ...


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, object], value))
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return dict(cast(Mapping[str, object], parsed))
    return {}


class PostgresCanonicalDocumentRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def resolve_version(
        self, workspace_id: str, resource_id: str, version_id: str | None
    ) -> CanonicalDocumentVersion | None:
        requested = None if version_id is None else version_id.strip()
        params = (workspace_id, resource_id, requested, requested)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(DOCUMENT_VERSION_SQL, params)
            row = await cursor.fetchone()
        if row is None:
            return None
        return CanonicalDocumentVersion(
            id=str(row[0]),
            workspace_id=workspace_id,
            resource_id=str(row[1]),
            version_number=int(cast(int, row[2])),
            source=str(row[3]),
            created_at=cast(datetime, row[4]),
        )

    async def read_nodes(
        self, workspace_id: str, resource_id: str, version_id: str, node_ids: tuple[str, ...]
    ) -> list[CanonicalDocumentNode]:
        if not node_ids or len(node_ids) > 50:
            return []
        params = (
            workspace_id,
            resource_id,
            version_id,
            version_id,
            workspace_id,
            resource_id,
            list(node_ids),
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(DOCUMENT_NODES_SQL, params)
            rows = await cursor.fetchall()
        return [_node(row) for row in rows]

    async def search_nodes(
        self, workspace_id: str, resource_id: str, version_id: str, query: str, limit: int
    ) -> list[CanonicalDocumentNode]:
        if not query.strip() or not 0 < limit <= 50:
            return []
        params = (
            workspace_id,
            resource_id,
            version_id,
            version_id,
            workspace_id,
            resource_id,
            query.strip(),
            query.strip(),
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(DOCUMENT_SEARCH_SQL, params)
            rows = await cursor.fetchall()
        return [_node(row) for row in rows]


class PostgresEvidenceRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def resolve_scope(
        self, workspace_id: str, resource_id: str, version_id: str | None, include_history: bool
    ) -> EvidenceScope:
        requested = version_id if include_history else None
        params = (workspace_id, resource_id, requested, requested)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(EVIDENCE_SCOPE_SQL, params)
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("证据 范围 未找到")
        return EvidenceScope(
            workspace_id=workspace_id,
            resource_id=resource_id,
            version_id=str(row[0]),
            source_type="canonical_chunk",
            embedding_profile=str(row[1]),
            chunk_profile=str(row[2]),
            canonical_embedding_profile=str(row[3]),
        )

    async def embedding_vector_type(self) -> str:
        query = """
        SELECT format_type(attribute.atttypid, attribute.atttypmod)
        FROM pg_attribute AS attribute
        JOIN pg_class AS relation ON relation.oid = attribute.attrelid
        WHERE relation.relname = 'resource_chunks' AND attribute.attname = 'embedding'
        """
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(query)
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("嵌入 向量 类型 未找到")
        return str(row[0])

    async def search_lexical(
        self, scope: EvidenceScope, query: str, limit: int
    ) -> list[ScoredCandidate]:
        params = (
            query,
            scope.workspace_id,
            scope.resource_id,
            scope.version_id,
            scope.chunk_profile,
            scope.chunk_profile,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(EVIDENCE_LEXICAL_SQL, params)
            rows = await cursor.fetchall()
        return [_candidate(row, float(cast(float, row[7]))) for row in rows]

    async def search_semantic(
        self, scope: EvidenceScope, vector: list[float], profile: EmbeddingProfile, limit: int
    ) -> list[ScoredCandidate]:
        vector_text = "[" + ",".join(str(float(item)) for item in vector) + "]"
        params = (
            vector_text,
            scope.workspace_id,
            scope.resource_id,
            scope.version_id,
            scope.chunk_profile,
            scope.chunk_profile,
            profile.version,
            profile.version,
            profile.model,
            profile.dimensions,
            profile.index_version,
            vector_text,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(EVIDENCE_SEMANTIC_SQL, params)
            rows = await cursor.fetchall()
        return [_candidate(row, max(0.0, min(1.0, float(cast(float, row[7]))))) for row in rows]

    async def list_leading_chunks(
        self, scope: EvidenceScope, limit: int
    ) -> list[ScoredCandidate]:
        if not 0 < limit <= 8:
            return []
        params = (
            scope.workspace_id,
            scope.resource_id,
            scope.version_id,
            scope.chunk_profile,
            scope.chunk_profile,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LEADING_CHUNKS_SQL, params)
            rows = await cursor.fetchall()
        return [_candidate(row, 0.25) for row in rows]

    async def list_window_siblings(
        self, scope: EvidenceScope, window_group_id: str
    ) -> list[Candidate]:
        group = window_group_id.strip()
        if not group:
            return []
        params = (
            scope.workspace_id,
            scope.resource_id,
            scope.version_id,
            scope.chunk_profile,
            scope.chunk_profile,
            group,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(WINDOW_SIBLINGS_SQL, params)
            rows = await cursor.fetchall()
        return [_candidate(row, 1.0).candidate for row in rows]


class PostgresCommitScopeResolver:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def resolve(self, request: BackendRequest, patch: object) -> CommitScope:
        if not isinstance(patch, PatchSet):
            raise TypeError("提交 范围 需要 PatchSet")
        node_ids = tuple(
            sorted(
                {operation.node_id for operation in patch.operations}
                | {
                    operation.expected_parent_id
                    for operation in patch.operations
                    if operation.expected_parent_id
                }
            )
        )
        evidence_refs = tuple(patch.evidence_refs)
        params = (
            request.context.run_id,
            request.context.step_id,
            request.context.workspace_id,
            request.context.resource_id,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(COMMIT_SCOPE_SQL, params)
            scope = await cursor.fetchone()
            if scope is None:
                raise PermissionError("提交 范围 超出 该 持久化 运行")
            await cursor.execute(
                AUTHORIZED_NODES_SQL,
                (
                    request.context.workspace_id,
                    request.context.resource_id,
                    scope[4],
                    list(node_ids),
                ),
            )
            nodes = await cursor.fetchall()
            await cursor.execute(
                AUTHORIZED_EVIDENCE_SQL,
                (
                    request.context.run_id,
                    request.context.workspace_id,
                    request.context.resource_id,
                    list(evidence_refs),
                ),
            )
            evidence = await cursor.fetchall()
        authorized_node_ids = frozenset(str(row[0]) for row in nodes)
        authorized_evidence = frozenset(str(row[0]) for row in evidence)
        if authorized_node_ids != frozenset(node_ids) or authorized_evidence != frozenset(
            evidence_refs
        ):
            raise PermissionError("提交 引用 为 外部 该 持久化 范围")
        return CommitScope(
            authorized_node_ids=authorized_node_ids,
            evidence_refs=authorized_evidence,
        )


class PostgresValidationRequestFactory:
    def __init__(self, store: SnapshotStore, scopes: PostgresCommitScopeResolver) -> None:
        self._store = store
        self._scopes = scopes

    async def __call__(self, request: BackendRequest, patch: object) -> ValidationRequest:
        if not isinstance(patch, PatchSet):
            raise TypeError("补丁校验需要 PatchSet")
        snapshot = await self._store.load_snapshot(
            request.context.workspace_id, request.context.resource_id
        )
        scope = await self._scopes.resolve(request, patch)
        return ValidationRequest(
            workspace_id=request.context.workspace_id,
            resource_id=request.context.resource_id,
            principal_type=request.context.principal.type,
            principal_id=request.context.principal.id,
            idempotency_key=request.idempotency_key,
            patch=patch,
            snapshot=ValidationSnapshot(
                workspace_id=request.context.workspace_id,
                resource_id=request.context.resource_id,
                current_version_id=snapshot.current_version_id,
                document=snapshot.document,
                authorized_node_ids=scope.authorized_node_ids,
                evidence=tuple(scope.evidence_refs),
                citations=tuple(scope.evidence_refs),
                required_approval=False,
                base_document_hash=snapshot.document.content_hash,
            ),
            base_document_hash=snapshot.document.content_hash,
        )


def _node(row: tuple[object, ...]) -> CanonicalDocumentNode:
    page_start, page_end = _page_range(row[8])
    return CanonicalDocumentNode(
        node_id=str(row[0]),
        workspace_id=str(row[1]),
        resource_id=str(row[2]),
        version_id=str(row[3]),
        node_type=str(row[4]),
        content=str(row[5]),
        content_hash=str(row[6]),
        sibling_order=int(cast(int, row[7])),
        page_start=page_start,
        page_end=page_end,
        attributes=cast(JSONObject, _json_object(row[9])),
    )


def _page_range(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        return None, None
    pages = [
        int(page)
        for item in cast(list[object], value)
        if isinstance(item, dict)
        and isinstance((page := cast(dict[str, object], item).get("page_number")), int)
        and page > 0
    ]
    return (min(pages), max(pages)) if pages else (None, None)


def _candidate(row: tuple[object, ...], score: float) -> ScoredCandidate:
    values = _candidate_values(row)
    metadata = _json_object(values[8])
    section_title = str(values[9])
    content = str(values[5])
    return ScoredCandidate(
        candidate=Candidate(
            source_id=str(values[0]),
            resource_id=str(values[1]),
            version_id=str(values[2]),
            node_id=str(values[3]),
            source_type=str(values[4]),
            content=content,
            created_at=cast(datetime, values[6]),
            rerank_text=_rerank_text(section_title, content, metadata),
            window_group_id=str(values[10]),
            order_in_section=int(cast(int, values[11])),
        ),
        score=score,
    )


def _candidate_values(row: tuple[object, ...]) -> tuple[object, ...]:
    if len(row) != 12:
        raise RuntimeError("证据 候选 数据行 无效")
    return row


def _rerank_text(section_title: str, content: str, metadata: dict[str, object]) -> str:
    headings = metadata.get("heading_path")
    values: list[str] = [section_title.strip()] if section_title.strip() else []
    if isinstance(headings, list):
        values.extend(
            str(cast(Mapping[str, object], item).get("text", "")).strip()
            for item in cast(list[object], headings)
            if isinstance(item, Mapping)
            and str(cast(Mapping[str, object], item).get("text", "")).strip()
        )
    values.append(content.strip())
    return "\n\n".join(dict.fromkeys(value for value in values if value))


__all__ = [
    "AUTHORIZED_EVIDENCE_SQL",
    "AUTHORIZED_NODES_SQL",
    "COMMIT_SCOPE_SQL",
    "DOCUMENT_NODES_SQL",
    "DOCUMENT_SEARCH_SQL",
    "DOCUMENT_VERSION_SQL",
    "EVIDENCE_LEXICAL_SQL",
    "EVIDENCE_SCOPE_SQL",
    "EVIDENCE_SEMANTIC_SQL",
    "LEADING_CHUNKS_SQL",
    "WINDOW_SIBLINGS_SQL",
    "PostgresCanonicalDocumentRepository",
    "PostgresCommitScopeResolver",
    "PostgresEvidenceRepository",
    "PostgresValidationRequestFactory",
]
