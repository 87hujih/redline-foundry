"""无需打开连接即可验证行为的 PostgreSQL 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from docreview.tool_runtime.rate_limit import RateLimitKey

FIXED_WINDOW_INCREMENT_SQL = """\
WITH rate_input (
    workspace_id, principal_type, principal_id, tool_name, tool_version,
    bucket_start, observed_at, limit_value
) AS (
    VALUES (
        %s::uuid, %s::text, %s::uuid, %s::text, %s::text,
        %s::timestamptz, %s::timestamptz, %s::integer
    )
)
INSERT INTO agent_tool_rate_limit_buckets (
    workspace_id, principal_type, principal_id, tool_name, tool_version,
    bucket_start, call_count, updated_at
)
SELECT
    workspace_id, principal_type, principal_id, tool_name, tool_version,
    bucket_start, 1, observed_at
FROM rate_input
ON CONFLICT (workspace_id, principal_type, principal_id, tool_name, tool_version, bucket_start)
DO UPDATE SET
    call_count = agent_tool_rate_limit_buckets.call_count + 1,
    updated_at = EXCLUDED.updated_at
WHERE agent_tool_rate_limit_buckets.call_count < (SELECT limit_value FROM rate_input)
RETURNING call_count
"""


class AsyncCursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...


class AsyncConnection(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> AsyncCursor: ...


class PooledCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def __aenter__(self) -> PooledCursor: ...

    async def __aexit__(self, *args: object) -> None: ...


class PooledConnection(Protocol):
    def cursor(self) -> PooledCursor: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> PooledConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> PooledConnection: ...


class PostgresRateLimitRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def increment(self, key: RateLimitKey, limit: int, now: datetime) -> int | None:
        cursor = await self._connection.execute(
            FIXED_WINDOW_INCREMENT_SQL,
            (
                key.workspace_id,
                key.principal_type,
                key.principal_id,
                str(key.tool_name),
                str(key.tool_version),
                key.bucket_start,
                now,
                limit,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if len(row) != 1 or type(row[0]) is not int:
            raise RuntimeError("PostgreSQL 速率 限制 数据行 无效")
        return row[0]


class PooledPostgresRateLimitRepository:
    """生产 pool 适配器；一次原子语句和一次显式 commit。"""

    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def increment(self, key: RateLimitKey, limit: int, now: datetime) -> int | None:
        params: tuple[object, ...] = (
            key.workspace_id,
            key.principal_type,
            key.principal_id,
            str(key.tool_name),
            str(key.tool_version),
            key.bucket_start,
            now,
            limit,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(FIXED_WINDOW_INCREMENT_SQL, params)
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            return None
        if len(row) != 1 or type(row[0]) is not int:
            raise RuntimeError("PostgreSQL 速率 限制 数据行 无效")
        return row[0]


__all__ = [
    "FIXED_WINDOW_INCREMENT_SQL",
    "PooledPostgresRateLimitRepository",
    "PostgresRateLimitRepository",
]
