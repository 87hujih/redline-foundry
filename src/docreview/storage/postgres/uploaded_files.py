"""Workspace-scoped 上传文件元数据读取适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from docreview.storage.models import UploadedFile

GET_UPLOADED_FILE_SQL = """
SELECT uploaded.id::text, uploaded.resource_id::text, uploaded.session_id::text,
       uploaded.original_filename, uploaded.content_type, uploaded.size_bytes,
       uploaded.sha256, uploaded.storage_key, uploaded.created_at
FROM uploaded_files AS uploaded
LEFT JOIN resources AS resource ON resource.id = uploaded.resource_id
LEFT JOIN assistant_sessions AS session ON session.id = uploaded.session_id
WHERE uploaded.id = %s AND uploaded.workspace_id = %s
  AND (uploaded.resource_id IS NULL OR resource.workspace_id = %s)
  AND (uploaded.session_id IS NULL OR session.workspace_id = %s)
"""


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


class UploadedFileRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def get_by_id(self, workspace_id: str, file_id: str) -> UploadedFile | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                GET_UPLOADED_FILE_SQL,
                (file_id, workspace_id, workspace_id, workspace_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return UploadedFile(
            id=str(row[0]),
            resource_id=None if row[1] is None else str(row[1]),
            session_id=None if row[2] is None else str(row[2]),
            original_filename=str(row[3]),
            content_type=str(row[4]),
            size_bytes=cast(int, row[5]),
            sha256=str(row[6]),
            storage_key=str(row[7]),
            created_at=cast(datetime, row[8]),
        )


__all__ = ["GET_UPLOADED_FILE_SQL", "UploadedFileRepository"]
