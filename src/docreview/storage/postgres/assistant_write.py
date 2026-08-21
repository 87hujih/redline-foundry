"""上传流程使用的 Workspace-scoped Assistant 会话/消息写入适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from docreview.storage.models import AssistantMessage, AssistantSession

CREATE_SESSION_SQL = """
INSERT INTO assistant_sessions (workspace_id, title)
VALUES (%s, %s)
RETURNING id::text, title, web_search_enabled, last_message_at, created_at, updated_at
"""

APPEND_MESSAGE_SQL = """
INSERT INTO assistant_messages (session_id, role, kind, sequence_no, payload)
SELECT %s, %s, %s, COALESCE(MAX(sequence_no), 0) + 1, %s::jsonb
FROM assistant_messages
WHERE session_id = %s
RETURNING id::text, role, kind, payload, sequence_no, created_at
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def __aenter__(self) -> AsyncCursor: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def __aenter__(self) -> AsyncConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class AssistantWriteRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def create_session(self, workspace_id: str, title: str) -> AssistantSession:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(CREATE_SESSION_SQL, (workspace_id, title))
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("助手 会话 写入 未返回数据行")
            await connection.commit()
        return AssistantSession(
            str(row[0]),
            str(row[1]),
            bool(row[2]),
            cast(datetime, row[3]),
            cast(datetime, row[4]),
            cast(datetime, row[5]),
        )

    async def append_message(
        self, session_id: str, role: str, kind: str, payload: Any
    ) -> AssistantMessage:
        import json

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(APPEND_MESSAGE_SQL, (session_id, role, kind, encoded, session_id))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise LookupError("助手 会话 未找到")
            await connection.commit()
        return AssistantMessage(
            str(row[0]), role, kind, row[3], int(cast(int, row[4])), cast(datetime, row[5])
        )


__all__ = ["APPEND_MESSAGE_SQL", "CREATE_SESSION_SQL", "AssistantWriteRepository"]
