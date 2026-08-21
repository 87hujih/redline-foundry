from __future__ import annotations

from typing import Any

import pytest

from docreview.config.settings import load_settings
from docreview.storage.postgres.pool import DatabasePool, create_database_pool


class FakeAsyncPool:
    created: FakeAsyncPool | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        FakeAsyncPool.created = self

    async def open(self, *, wait: bool) -> None:
        assert wait is True
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    def connection(self) -> object:
        return object()


class FailingAsyncPool(FakeAsyncPool):
    async def open(self, *, wait: bool) -> None:
        assert wait is True
        raise RuntimeError("pool startup failed")


@pytest.mark.anyio
async def test_database_pool_uses_bounded_settings_and_owned_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("docreview.storage.postgres.pool.AsyncConnectionPool", FakeAsyncPool)
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://user:secret@database.internal/agent_project",
            "DATABASE_MIN_SIZE": "3",
            "DATABASE_MAX_SIZE": "12",
            "DATABASE_POOL_TIMEOUT_SECONDS": "15",
        }
    )

    owned = await create_database_pool(settings)
    created = FakeAsyncPool.created
    assert created is not None and created.opened is True
    assert created.kwargs["min_size"] == 3
    assert created.kwargs["max_size"] == 12
    assert created.kwargs["timeout"] == 15
    assert created.kwargs["open"] is False
    assert "secret" not in repr(settings)
    await owned.close()
    assert created.closed is True and owned.opened is False


@pytest.mark.anyio
async def test_database_pool_fails_before_factory_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def denied(**kwargs: Any) -> FakeAsyncPool:
        nonlocal called
        called = True
        return FakeAsyncPool(**kwargs)

    monkeypatch.setattr("docreview.storage.postgres.pool.AsyncConnectionPool", denied)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        await create_database_pool(load_settings({}))
    assert called is False


@pytest.mark.anyio
async def test_database_pool_closes_underlying_pool_when_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("docreview.storage.postgres.pool.AsyncConnectionPool", FailingAsyncPool)
    settings = load_settings({"DATABASE_URL": "postgresql://database.internal/agent_project"})

    with pytest.raises(RuntimeError, match="startup failed"):
        await create_database_pool(settings)

    created = FailingAsyncPool.created
    assert created is not None and created.closed is True


def test_owned_pool_rejects_connection_before_open() -> None:
    pool = DatabasePool(FakeAsyncPool())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="not open"):
        pool.connection()
