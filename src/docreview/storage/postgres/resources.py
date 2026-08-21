"""资源 HTTP 路由使用的 Workspace-scoped 读取方法。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol, cast

from docreview.knowledge.chunking import REVIEW_STRUCTURE_PROFILE
from docreview.storage.models import Resource, ResourceVersion, SearchChunk, SearchSection

LIST_RESOURCES_SQL = """
SELECT id::text, title, source_type, created_at
FROM resources
WHERE workspace_id = %s
ORDER BY created_at DESC
"""

GET_RESOURCE_SQL = """
SELECT id::text, title, source_type, created_at
FROM resources
WHERE id = %s AND workspace_id = %s
"""

CLEAR_RESOURCE_SELECTIONS_SQL = """
UPDATE assistant_sessions
SET selected_resource_id = NULL,
    resource_selected_at = NULL,
    updated_at = now()
WHERE workspace_id = %s AND selected_resource_id = %s
"""

DELETE_RESOURCE_SQL = """
DELETE FROM resources
WHERE id = %s AND workspace_id = %s
"""

GET_CURRENT_VERSION_SQL = """
SELECT version.id::text, version.resource_id::text, version.version_number,
       version.content, version.source, version.created_at
FROM resource_versions AS version
JOIN resources AS resource ON resource.id = version.resource_id
WHERE version.resource_id = %s AND resource.workspace_id = %s
ORDER BY version.version_number DESC
LIMIT 1
"""

_SEARCH_CHUNK_COLUMNS = """
chunk.id::text, chunk.resource_id::text, chunk.version_id::text, chunk.chunk_index,
chunk.section_title, chunk.content, chunk.section_id::text, chunk.section_type,
chunk.chunk_role, chunk.window_group_id::text, chunk.order_in_section, chunk.metadata_json
"""

SEARCH_CHUNKS_BY_VERSION_SQL = f"""
SELECT {_SEARCH_CHUNK_COLUMNS}
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND version.id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
ORDER BY chunk.embedding <=> %s::vector
LIMIT %s
"""

SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL = f"""
SELECT {_SEARCH_CHUNK_COLUMNS}
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND version.id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
  AND (
    lower(coalesce(chunk.section_title, '') || ' ' || chunk.content) LIKE '%%' || %s || '%%'
    OR lower(coalesce(chunk.section_title, '') || ' ' || chunk.content) %% %s
  )
ORDER BY
  CASE WHEN lower(coalesce(chunk.section_title, '') || ' ' || chunk.content)
       LIKE '%%' || %s || '%%' THEN 1 ELSE 0 END DESC,
  similarity(lower(coalesce(chunk.section_title, '') || ' ' || chunk.content), %s) DESC,
  chunk.chunk_index
LIMIT %s
"""

LIST_SECTIONS_BY_VERSION_SQL = """
SELECT section.id::text, section.resource_id::text, section.version_id::text,
       section.section_key, section.section_type, section.section_order, section.title,
       section.canonical_entity_name, section.aliases_json, section.summary, section.content,
       section.metadata_json
FROM resource_sections AS section
JOIN resource_versions AS version ON version.id = section.version_id
JOIN resources AS resource ON resource.id = version.resource_id
WHERE resource.workspace_id = %s AND version.id = %s
ORDER BY section.section_order, section.created_at
"""

LIST_CHUNKS_BY_VERSION_SQL = f"""
SELECT {_SEARCH_CHUNK_COLUMNS}
FROM resource_chunks AS chunk
JOIN resource_versions AS version ON version.id = chunk.version_id
JOIN resources AS resource ON resource.id = version.resource_id
JOIN canonical_documents AS canonical ON canonical.version_id = version.id
WHERE resource.workspace_id = %s AND version.id = %s
  AND canonical.chunk_profile = %s AND chunk.chunk_profile = %s
ORDER BY chunk.chunk_index, chunk.created_at
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...

    @property
    def rowcount(self) -> int: ...

    async def __aenter__(self) -> AsyncCursor: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...

    def transaction(self) -> Any: ...

    async def __aenter__(self) -> AsyncConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


def _resource(row: tuple[object, ...]) -> Resource:
    return Resource(
        id=str(row[0]),
        title=str(row[1]),
        source_type=str(row[2]),
        created_at=cast(datetime, row[3]),
    )


def _version(row: tuple[object, ...]) -> ResourceVersion:
    return ResourceVersion(
        id=str(row[0]),
        resource_id=str(row[1]),
        version_number=cast(int, row[2]),
        content=str(row[3]),
        source=str(row[4]),
        created_at=cast(datetime, row[5]),
    )


def _json_object(value: object) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    encoded = _json_source(value)
    if encoded is None:
        return None
    try:
        parsed: object = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed_dict = cast(dict[object, object], parsed)
    return {str(key): item for key, item in parsed_dict.items()}


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in cast(list[object], value) if isinstance(item, str)]
    encoded = _json_source(value)
    if encoded is None:
        return []
    try:
        raw: object = json.loads(encoded)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in cast(list[object], raw) if isinstance(item, str)]


def _json_source(value: object) -> str | bytes | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return None


def _chunk(row: tuple[object, ...]) -> SearchChunk:
    return SearchChunk(
        id=str(row[0]),
        resource_id=str(row[1]),
        version_id=str(row[2]),
        chunk_index=cast(int, row[3]),
        section_title=str(row[4]),
        content=str(row[5]),
        section_id=None if row[6] is None else str(row[6]),
        section_type=None if row[7] is None else str(row[7]),
        chunk_role=None if row[8] is None else str(row[8]),
        window_group_id=None if row[9] is None else str(row[9]),
        order_in_section=cast(int | None, row[10]),
        metadata=_json_object(row[11]),
    )


def _section(row: tuple[object, ...]) -> SearchSection:
    return SearchSection(
        id=str(row[0]),
        resource_id=str(row[1]),
        version_id=str(row[2]),
        section_key=str(row[3]),
        section_type=str(row[4]),
        section_order=cast(int, row[5]),
        title=str(row[6]),
        canonical_entity_name=None if row[7] is None else str(row[7]),
        aliases=_string_list(row[8]),
        summary=str(row[9]),
        content=str(row[10]),
        metadata=_json_object(row[11]),
    )


class ResourceRepository:
    def __init__(
        self, pool: AsyncPool, *, chunk_profile: str = REVIEW_STRUCTURE_PROFILE.profile_id
    ) -> None:
        if chunk_profile != REVIEW_STRUCTURE_PROFILE.profile_id:
            raise ValueError("资源 搜索 需要 该 有效 结构化 切块 配置档")
        self._pool = pool
        self._chunk_profile = chunk_profile

    async def list(self, workspace_id: str) -> list[Resource]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LIST_RESOURCES_SQL, (workspace_id,))
            rows = await cursor.fetchall()
        return [_resource(row) for row in rows]

    async def get_by_id(self, workspace_id: str, resource_id: str) -> Resource | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_RESOURCE_SQL, (resource_id, workspace_id))
            row = await cursor.fetchone()
        return None if row is None else _resource(row)

    async def delete(self, workspace_id: str, resource_id: str) -> bool:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(CLEAR_RESOURCE_SELECTIONS_SQL, (workspace_id, resource_id))
            await cursor.execute(DELETE_RESOURCE_SQL, (resource_id, workspace_id))
            return int(getattr(cursor, "rowcount", 0)) > 0

    async def get_current_version(
        self, workspace_id: str, resource_id: str
    ) -> ResourceVersion | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_CURRENT_VERSION_SQL, (resource_id, workspace_id))
            row = await cursor.fetchone()
        return None if row is None else _version(row)

    async def search_chunks_by_version(
        self, workspace_id: str, version_id: str, vector: str, limit: int
    ) -> list[SearchChunk]:
        params = (workspace_id, version_id, self._chunk_profile, self._chunk_profile, vector, limit)
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(SEARCH_CHUNKS_BY_VERSION_SQL, params)
            rows = await cursor.fetchall()
        return [_chunk(row) for row in rows]

    async def search_chunks_lexical_by_version(
        self, workspace_id: str, version_id: str, query: str, limit: int
    ) -> list[SearchChunk]:
        normalized = query.strip().lower()
        if not normalized or limit <= 0:
            return []
        params = (
            workspace_id,
            version_id,
            self._chunk_profile,
            self._chunk_profile,
            normalized,
            normalized,
            normalized,
            normalized,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL, params)
            rows = await cursor.fetchall()
        return [_chunk(row) for row in rows]

    async def list_sections_by_version(
        self, workspace_id: str, version_id: str
    ) -> list[SearchSection]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LIST_SECTIONS_BY_VERSION_SQL, (workspace_id, version_id))
            rows = await cursor.fetchall()
        return [_section(row) for row in rows]

    async def list_chunks_by_version(self, workspace_id: str, version_id: str) -> list[SearchChunk]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                LIST_CHUNKS_BY_VERSION_SQL,
                (workspace_id, version_id, self._chunk_profile, self._chunk_profile),
            )
            rows = await cursor.fetchall()
        return [_chunk(row) for row in rows]


__all__ = [
    "CLEAR_RESOURCE_SELECTIONS_SQL",
    "DELETE_RESOURCE_SQL",
    "GET_CURRENT_VERSION_SQL",
    "GET_RESOURCE_SQL",
    "LIST_CHUNKS_BY_VERSION_SQL",
    "LIST_RESOURCES_SQL",
    "LIST_SECTIONS_BY_VERSION_SQL",
    "SEARCH_CHUNKS_BY_VERSION_SQL",
    "SEARCH_CHUNKS_LEXICAL_BY_VERSION_SQL",
    "ResourceRepository",
]
