"""SQL adapter for out-of-transaction structured child embedding projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol, cast

from docreview.providers.embedding import PendingEmbeddingChunk

LIST_PENDING_EMBEDDINGS_SQL = """
SELECT chunk.id::text, resource.workspace_id::text, chunk.resource_id::text,
       chunk.version_id::text, chunk.content, chunk.content_hash, chunk.chunk_profile,
       chunk.embedding_profile, chunk.metadata_json
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE chunk.embedding_status = 'pending'
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
  AND canonical.embedding_profile = %s AND chunk.embedding_profile = %s
ORDER BY chunk.created_at, chunk.id
LIMIT %s
"""

WRITE_EMBEDDING_IF_CURRENT_SQL = """
UPDATE resource_chunks AS chunk
SET embedding = %s::vector, embedding_status = 'ready', embedding_model = %s,
    embedding_dimensions = %s, retrieval_index_version = %s
FROM resource_versions AS version
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE chunk.id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND resource.workspace_id = %s AND version.id = chunk.version_id
  AND chunk.content_hash = %s AND chunk.chunk_profile = %s AND chunk.embedding_profile = %s
  AND canonical.chunk_profile = chunk.chunk_profile
  AND canonical.embedding_profile = chunk.embedding_profile
  AND chunk.embedding_status = 'pending'
  AND chunk.metadata_json->>'fragment_hash' = %s
  AND chunk.metadata_json->>'tokenizer_profile' = %s
RETURNING chunk.id::text
"""

MARK_EMBEDDING_FAILED_IF_CURRENT_SQL = """
UPDATE resource_chunks AS chunk
SET embedding_status = 'failed'
FROM resource_versions AS version
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE chunk.id = %s AND chunk.resource_id = %s AND chunk.version_id = %s
  AND resource.workspace_id = %s AND version.id = chunk.version_id
  AND chunk.content_hash = %s AND chunk.chunk_profile = %s AND chunk.embedding_profile = %s
  AND canonical.chunk_profile = chunk.chunk_profile
  AND canonical.embedding_profile = chunk.embedding_profile
  AND chunk.embedding_status = 'pending'
  AND chunk.metadata_json->>'fragment_hash' = %s
RETURNING chunk.id::text
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...
    async def fetchall(self) -> list[tuple[object, ...]]: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self) -> Any: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class PostgresPendingEmbeddingRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def list_pending_embeddings(
        self, *, chunk_profile: str, embedding_profile: str, limit: int
    ) -> list[PendingEmbeddingChunk]:
        if not chunk_profile.strip() or not embedding_profile.strip() or not 0 < limit <= 100:
            raise ValueError("无效的 待处理 嵌入 投影 范围")
        params = (chunk_profile, chunk_profile, embedding_profile, embedding_profile, limit)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LIST_PENDING_EMBEDDINGS_SQL, params)
            rows = await cursor.fetchall()
        return [_pending(row) for row in rows]

    async def write_embedding_if_current(
        self,
        chunk: PendingEmbeddingChunk,
        *,
        vector: list[float],
        embedding_model: str,
        embedding_dimensions: int,
        retrieval_index_version: str,
        tokenizer_profile: str,
    ) -> bool:
        vector_text = "[" + ",".join(str(float(value)) for value in vector) + "]"
        params = (
            vector_text,
            embedding_model,
            embedding_dimensions,
            retrieval_index_version,
            chunk.id,
            chunk.resource_id,
            chunk.version_id,
            chunk.workspace_id,
            chunk.content_hash,
            chunk.chunk_profile,
            chunk.embedding_profile,
            _fragment_hash(chunk),
            tokenizer_profile,
        )
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(WRITE_EMBEDDING_IF_CURRENT_SQL, params)
            return await cursor.fetchone() is not None

    async def mark_embedding_failed_if_current(
        self, chunk: PendingEmbeddingChunk, *, reason: str
    ) -> bool:
        del reason  # 冻结的数据库结构没有安全的自由格式提供方错误字段。
        params = (
            chunk.id,
            chunk.resource_id,
            chunk.version_id,
            chunk.workspace_id,
            chunk.content_hash,
            chunk.chunk_profile,
            chunk.embedding_profile,
            _fragment_hash(chunk),
        )
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(MARK_EMBEDDING_FAILED_IF_CURRENT_SQL, params)
            return await cursor.fetchone() is not None


def _pending(row: tuple[object, ...]) -> PendingEmbeddingChunk:
    if len(row) != 9:
        raise RuntimeError("待处理 嵌入 数据行 无效")
    metadata = _metadata(row[8])
    return PendingEmbeddingChunk(
        id=_required(row[0]),
        workspace_id=_required(row[1]),
        resource_id=_required(row[2]),
        version_id=_required(row[3]),
        content=_required(row[4]),
        content_hash=_required(row[5]),
        chunk_profile=_required(row[6]),
        embedding_profile=_required(row[7]),
        metadata=metadata,
    )


def _metadata(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("待处理 嵌入 元数据 无效")
    return cast(dict[str, object], value)


def _fragment_hash(chunk: PendingEmbeddingChunk) -> str:
    value = chunk.metadata.get("fragment_hash")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("待处理 嵌入 片段 哈希 无效")
    return value


def _required(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("待处理 嵌入 数据行 包含无效的 标识符")
    return value


__all__ = [
    "LIST_PENDING_EMBEDDINGS_SQL",
    "MARK_EMBEDDING_FAILED_IF_CURRENT_SQL",
    "WRITE_EMBEDDING_IF_CURRENT_SQL",
    "PostgresPendingEmbeddingRepository",
]
