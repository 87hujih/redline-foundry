"""Workspace-scoped assistant session/message reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from docreview.storage.models import AssistantMessage, AssistantSession

LIST_SESSIONS_SQL = """
SELECT id::text, title, web_search_enabled, last_message_at, created_at, updated_at
FROM assistant_sessions
WHERE workspace_id = %s
ORDER BY last_message_at DESC, id DESC
"""

GET_CONVERSATION_SESSION_SQL = """
SELECT id::text, title, web_search_enabled, last_message_at, created_at, updated_at
FROM assistant_sessions
WHERE id = %s AND workspace_id = %s
"""

LIST_SESSION_MESSAGES_SQL = """
SELECT message.id::text, message.role, message.kind, message.payload,
       message.sequence_no, message.created_at
FROM assistant_messages AS message
JOIN assistant_sessions AS session ON session.id = message.session_id
WHERE message.session_id = %s AND session.workspace_id = %s
ORDER BY message.sequence_no ASC, message.id ASC
"""

DELETE_SESSION_SQL = """
DELETE FROM assistant_sessions
WHERE id = %s AND workspace_id = %s
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...

    async def __aenter__(self) -> AsyncCursor: ...

    async def __aexit__(self, *args: object) -> None: ...

    @property
    def rowcount(self) -> int: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> AsyncConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


def _session(row: tuple[object, ...]) -> AssistantSession:
    return AssistantSession(
        id=str(row[0]),
        title=str(row[1]),
        web_search_enabled=cast(bool, row[2]),
        last_message_at=cast(datetime, row[3]),
        created_at=cast(datetime, row[4]),
        updated_at=cast(datetime, row[5]),
    )


def _message(row: tuple[object, ...]) -> AssistantMessage:
    return AssistantMessage(
        id=str(row[0]),
        role=str(row[1]),
        kind=str(row[2]),
        payload=row[3],
        sequence_no=cast(int, row[4]),
        created_at=cast(datetime, row[5]),
    )


class AssistantRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def list_sessions(self, workspace_id: str) -> list[AssistantSession]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LIST_SESSIONS_SQL, (workspace_id,))
            rows = await cursor.fetchall()
        return [_session(row) for row in rows]

    async def get_conversation(
        self, workspace_id: str, session_id: str
    ) -> tuple[AssistantSession, list[AssistantMessage]] | None:
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(GET_CONVERSATION_SESSION_SQL, (session_id, workspace_id))
                session_row = await cursor.fetchone()
            if session_row is None:
                return None
            async with connection.cursor() as cursor:
                await cursor.execute(LIST_SESSION_MESSAGES_SQL, (session_id, workspace_id))
                message_rows = await cursor.fetchall()
        return _session(session_row), [_message(row) for row in message_rows]

    async def delete_session(self, workspace_id: str, session_id: str) -> bool:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(DELETE_SESSION_SQL, (session_id, workspace_id))
            deleted = int(getattr(cursor, "rowcount", 0)) > 0
            await connection.commit()
        return deleted


__all__ = [
    "DELETE_SESSION_SQL",
    "GET_CONVERSATION_SESSION_SQL",
    "LIST_SESSIONS_SQL",
    "LIST_SESSION_MESSAGES_SQL",
    "AssistantRepository",
]
