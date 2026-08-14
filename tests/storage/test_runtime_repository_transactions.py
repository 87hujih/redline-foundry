from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from docreview.runtime.contracts import CreateRun, CreateStep
from docreview.storage.postgres.runtime_repository import RuntimeRepository


class Transaction:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> Transaction:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.committed = True
        else:
            self.connection.rolled_back = True


class Cursor:
    rowcount = 1

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.responses: list[tuple[object, ...] | None] = [None]

    async def __aenter__(self) -> Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, params: Sequence[object] = ()) -> Any:
        self.connection.queries.append(query)
        if "select" in query.lower() and "agent_runs" in query.lower():
            raise RuntimeError("crash injection")

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.responses.pop(0) if self.responses else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []


class Connection:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.queries: list[str] = []
        self.cursor_value = Cursor(self)

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    def cursor(self) -> Cursor:
        return self.cursor_value

    def transaction(self) -> Transaction:
        return Transaction(self)


class Pool:
    def __init__(self) -> None:
        self.connection_value = Connection()

    def connection(self) -> Connection:
        return self.connection_value


@pytest.mark.asyncio
async def test_crash_between_run_insert_and_step_creation_rolls_back_transaction() -> None:
    pool = Pool()
    repository = RuntimeRepository(pool)

    with pytest.raises(RuntimeError, match="crash injection"):
        await repository.create_run_with_initial_step(
            CreateRun(
                workspace_id="workspace-1",
                request_id="request-1",
                objective="review",
                resource_id="resource-1",
                principal_type="user",
                principal_id="user-1",
                trust_source="trusted-ingress",
                state={},
                deadline_at=datetime(2026, 8, 14, tzinfo=UTC),
            ),
            CreateStep("understand_goal:1", "UnderstandGoal", {"message": "review"}),
        )

    assert pool.connection_value.rolled_back is True
    assert pool.connection_value.committed is False
