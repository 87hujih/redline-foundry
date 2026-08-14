"""Read-only identity and resource-scope queries."""

from __future__ import annotations

from typing import Any, Protocol, cast

from docreview.identity.policy import Membership, MembershipRole

ACTIVE_MEMBERSHIP_SQL = """
SELECT membership.workspace_id::text, membership.user_id::text,
       membership.role, membership.status
FROM memberships AS membership
JOIN users AS account ON account.id = membership.user_id
JOIN workspaces AS workspace ON workspace.id = membership.workspace_id
WHERE membership.workspace_id = %s AND membership.user_id = %s
  AND membership.status = 'active'
  AND account.status = 'active'
  AND workspace.status = 'active'
"""

RESOURCE_OWNERSHIP_SQL = {
    "document": "SELECT EXISTS (SELECT 1 FROM resources WHERE id = %s AND workspace_id = %s)",
    "artifact": (
        "SELECT EXISTS (SELECT 1 FROM agent_artifacts WHERE id = %s AND workspace_id = %s)"
    ),
    "task": "SELECT EXISTS (SELECT 1 FROM tasks WHERE id = %s AND workspace_id = %s)",
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


class IdentityRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def get_active_membership(self, workspace_id: str, user_id: str) -> Membership | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(ACTIVE_MEMBERSHIP_SQL, (workspace_id, user_id))
            row = await cursor.fetchone()
        if row is None:
            return None
        return Membership(
            workspace_id=str(row[0]),
            user_id=str(row[1]),
            role=MembershipRole(str(row[2])),
            status=str(row[3]),
        )

    async def resource_belongs_to_workspace(
        self, resource_type: str, resource_id: str, workspace_id: str
    ) -> bool:
        query = RESOURCE_OWNERSHIP_SQL.get(resource_type)
        if query is None:
            return False
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(query, (resource_id, workspace_id))
            row = await cursor.fetchone()
        return row is not None and cast(bool, row[0])


__all__ = ["ACTIVE_MEMBERSHIP_SQL", "RESOURCE_OWNERSHIP_SQL", "IdentityRepository"]
