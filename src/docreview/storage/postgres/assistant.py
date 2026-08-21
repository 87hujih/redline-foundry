"""Workspace-scoped Assistant 会话/消息读取。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from docreview.storage.models import AssistantMessage, AssistantSession
from docreview.storage.postgres.errors import RecordNotFoundError, SessionNotFoundError

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

GET_RESOURCE_SELECTION_SQL = """
SELECT session.selected_resource_id::text
FROM assistant_sessions AS session
LEFT JOIN resources AS resource
  ON resource.id = session.selected_resource_id
 AND resource.workspace_id = session.workspace_id
WHERE session.id = %s AND session.workspace_id = %s
  AND (session.selected_resource_id IS NULL OR resource.id IS NOT NULL)
"""

LOCK_RESOURCE_SELECTION_SESSION_SQL = """
SELECT selected_resource_id::text
FROM assistant_sessions
WHERE id = %s AND workspace_id = %s
FOR UPDATE
"""

LOCK_SELECTABLE_RESOURCE_SQL = """
SELECT id::text
FROM resources
WHERE id = %s AND workspace_id = %s AND source_type = 'upload'
FOR KEY SHARE
"""

UPDATE_RESOURCE_SELECTION_SQL = """
UPDATE assistant_sessions
SET selected_resource_id = %s,
    resource_selected_at = now(),
    updated_at = now()
WHERE id = %s AND workspace_id = %s
  AND selected_resource_id IS DISTINCT FROM %s
RETURNING selected_resource_id::text
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

    def transaction(self) -> Any: ...

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

    async def get_resource_selection(self, workspace_id: str, session_id: str) -> str | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_RESOURCE_SELECTION_SQL, (session_id, workspace_id))
            row = await cursor.fetchone()
        if row is None:
            raise SessionNotFoundError
        return str(row[0]) if row[0] is not None else None

    async def set_resource_selection(
        self, workspace_id: str, session_id: str, resource_id: str
    ) -> str:
        async with self._pool.connection() as connection, connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute(
                    LOCK_RESOURCE_SELECTION_SESSION_SQL,
                    (session_id, workspace_id),
                )
                session = await cursor.fetchone()
            if session is None:
                raise SessionNotFoundError

            async with connection.cursor() as cursor:
                await cursor.execute(
                    LOCK_SELECTABLE_RESOURCE_SQL,
                    (resource_id, workspace_id),
                )
                resource = await cursor.fetchone()
            if resource is None:
                raise RecordNotFoundError

            selected = str(session[0]) if session[0] is not None else None
            if selected == resource_id:
                return resource_id

            async with connection.cursor() as cursor:
                await cursor.execute(
                    UPDATE_RESOURCE_SELECTION_SQL,
                    (resource_id, session_id, workspace_id, resource_id),
                )
                updated = await cursor.fetchone()
            if updated is None:
                raise RuntimeError("会话资源选择更新未返回数据行")
            return str(updated[0])


__all__ = [
    "DELETE_SESSION_SQL",
    "GET_CONVERSATION_SESSION_SQL",
    "GET_RESOURCE_SELECTION_SQL",
    "LIST_SESSIONS_SQL",
    "LIST_SESSION_MESSAGES_SQL",
    "LOCK_RESOURCE_SELECTION_SESSION_SQL",
    "LOCK_SELECTABLE_RESOURCE_SQL",
    "UPDATE_RESOURCE_SELECTION_SQL",
    "AssistantRepository",
]
