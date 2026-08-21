"""生产边界自有 PostgreSQL connection-pool 生命周期。

Pool 只从已校验的 ``Settings`` 对象创建。任何 repository 都不得自行创建
连接，测试路径也不得回退到 ``DATABASE_URL``；数据库测试辅助函数仍位于
``docreview.testsupport``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from docreview.config.settings import Settings


@dataclass(frozen=True, slots=True)
class DatabasePoolConfig:
    min_size: int
    max_size: int
    timeout: float

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabasePoolConfig:
        if settings.database_url is None:
            raise ValueError("DATABASE_URL 为必填项 之前 打开 该 数据库 连接池")
        return cls(
            min_size=settings.database_min_size,
            max_size=settings.database_max_size,
            timeout=settings.database_timeout_seconds,
        )


class DatabasePool:
    """轻量自有封装，显式管理 open/close 与健康状态。"""

    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self.pool = pool
        self.opened = False

    async def open(self) -> None:
        if self.opened:
            return
        await self.pool.open(wait=True)
        self.opened = True

    async def close(self) -> None:
        if not self.opened:
            return
        await self.pool.close()
        self.opened = False

    def connection(self) -> Any:
        if not self.opened:
            raise RuntimeError("database connection pool is not open")
        return self.pool.connection()


async def create_database_pool(settings: Settings) -> DatabasePool:
    """打开一个有界 pool，并在出错时于构造 repository 前失败。"""

    if settings.database_url is None:
        raise ValueError("DATABASE_URL 为必填项 之前 打开 该 数据库 连接池")
    config = DatabasePoolConfig.from_settings(settings)
    pool = AsyncConnectionPool(
        conninfo=settings.database_url.get_secret_value(),
        min_size=config.min_size,
        max_size=config.max_size,
        timeout=config.timeout,
        open=False,
    )
    owned = DatabasePool(pool)
    try:
        await owned.open()
    except BaseException:
        # ``open(wait=True)`` 可能在抛错前启动后台 pool worker。
        # 此时 ``owned.opened`` 刻意仍为 false，因此直接关闭底层 pool，
        # 不走通常的 no-op guard。
        await pool.close()
        raise
    return owned


__all__ = ["DatabasePool", "DatabasePoolConfig", "create_database_pool"]
