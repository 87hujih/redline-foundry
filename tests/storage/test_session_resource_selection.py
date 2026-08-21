from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from docreview.storage.postgres.assistant import AssistantRepository
from docreview.storage.postgres.errors import RecordNotFoundError, SessionNotFoundError

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
RESOURCE_ID = "33333333-3333-4333-8333-333333333333"


def compact(value: str) -> str:
    return " ".join(value.lower().split())


@dataclass
class SelectionFixture:
    session_exists: bool = True
    selected_resource_id: str | None = None
    resource_exists: bool = True


class Cursor:
    def __init__(self, fixture: SelectionFixture) -> None:
        self.fixture = fixture
        self.current_query = ""
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self) -> Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...]) -> Any:
        self.current_query = query
        self.executions.append((query, params))

    async def fetchone(self) -> tuple[object, ...] | None:
        query = compact(self.current_query)
        if "from assistant_sessions" in query:
            if not self.fixture.session_exists:
                return None
            return (self.fixture.selected_resource_id,)
        if "from resources" in query:
            return (RESOURCE_ID,) if self.fixture.resource_exists else None
        if "update assistant_sessions" in query:
            return (RESOURCE_ID,)
        raise AssertionError(f"unexpected selection query: {query}")

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []

    @property
    def rowcount(self) -> int:
        return 1


class Transaction:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> Transaction:
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if exc_type is None:
            self.connection.committed = True
        else:
            self.connection.rolled_back = True


class Connection:
    def __init__(self, fixture: SelectionFixture) -> None:
        self.cursor_value = Cursor(fixture)
        self.transaction_entries = 0
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def cursor(self) -> Cursor:
        return self.cursor_value

    def transaction(self) -> Transaction:
        return Transaction(self)

    async def commit(self) -> None:
        self.committed = True


class Pool:
    def __init__(self, fixture: SelectionFixture) -> None:
        self.connection_value = Connection(fixture)

    def connection(self) -> Connection:
        return self.connection_value


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [None, RESOURCE_ID])
async def test_selection_read_distinguishes_no_selection_from_missing_session(
    selected: str | None,
) -> None:
    pool = Pool(SelectionFixture(selected_resource_id=selected))
    repository = AssistantRepository(pool)

    result = await repository.get_resource_selection(WORKSPACE_ID, SESSION_ID)  # type: ignore[attr-defined]

    assert result == selected
    [(query, params)] = pool.connection_value.cursor_value.executions
    normalized = compact(query)
    assert "from assistant_sessions" in normalized
    assert "workspace_id = %s" in normalized
    assert "selected_resource_id" in normalized
    assert params == (SESSION_ID, WORKSPACE_ID)


@pytest.mark.asyncio
async def test_selection_read_hides_missing_or_cross_workspace_session() -> None:
    pool = Pool(SelectionFixture(session_exists=False))
    repository = AssistantRepository(pool)

    with pytest.raises(SessionNotFoundError):
        await repository.get_resource_selection(WORKSPACE_ID, SESSION_ID)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_selection_write_locks_and_validates_one_workspace_fact_graph() -> None:
    pool = Pool(SelectionFixture())
    repository = AssistantRepository(pool)

    result = await repository.set_resource_selection(  # type: ignore[attr-defined]
        WORKSPACE_ID, SESSION_ID, RESOURCE_ID
    )

    assert result == RESOURCE_ID
    assert pool.connection_value.transaction_entries == 1
    assert pool.connection_value.committed is True
    assert pool.connection_value.rolled_back is False
    executions = pool.connection_value.cursor_value.executions
    normalized = [(compact(query), params) for query, params in executions]
    session_query, session_params = normalized[0]
    resource_query, resource_params = normalized[1]
    assert "from assistant_sessions" in session_query
    assert "workspace_id = %s" in session_query
    assert "for update" in session_query
    assert session_params == (SESSION_ID, WORKSPACE_ID)
    assert "from resources" in resource_query
    assert "workspace_id = %s" in resource_query
    assert "source_type = 'upload'" in resource_query
    assert "for key share" in resource_query
    assert resource_params == (RESOURCE_ID, WORKSPACE_ID)
    update_queries = [
        query for query, _params in normalized if "update assistant_sessions" in query
    ]
    assert len(update_queries) == 1
    assert "selected_resource_id" in update_queries[0]
    assert "resource_selected_at" in update_queries[0]


@pytest.mark.asyncio
async def test_repeated_selection_does_not_mutate_session_timestamps() -> None:
    pool = Pool(SelectionFixture(selected_resource_id=RESOURCE_ID))
    repository = AssistantRepository(pool)

    result = await repository.set_resource_selection(  # type: ignore[attr-defined]
        WORKSPACE_ID, SESSION_ID, RESOURCE_ID
    )

    assert result == RESOURCE_ID
    normalized = [
        compact(query) for query, _params in pool.connection_value.cursor_value.executions
    ]
    assert any(
        "from resources" in query and "source_type = 'upload'" in query for query in normalized
    )
    updates = [query for query in normalized if "update assistant_sessions" in query]
    assert not updates or all("is distinct from" in query for query in updates)


@pytest.mark.asyncio
async def test_selection_write_rolls_back_for_missing_or_cross_workspace_resource() -> None:
    pool = Pool(SelectionFixture(resource_exists=False))
    repository = AssistantRepository(pool)

    with pytest.raises(RecordNotFoundError):
        await repository.set_resource_selection(  # type: ignore[attr-defined]
            WORKSPACE_ID, SESSION_ID, RESOURCE_ID
        )

    assert pool.connection_value.rolled_back is True
    assert pool.connection_value.committed is False
    assert all(
        "update assistant_sessions" not in compact(query)
        for query, _params in pool.connection_value.cursor_value.executions
    )


@pytest.mark.asyncio
async def test_selection_write_stops_before_resource_lookup_for_missing_session() -> None:
    pool = Pool(SelectionFixture(session_exists=False))
    repository = AssistantRepository(pool)

    with pytest.raises(SessionNotFoundError):
        await repository.set_resource_selection(  # type: ignore[attr-defined]
            WORKSPACE_ID, SESSION_ID, RESOURCE_ID
        )

    assert pool.connection_value.rolled_back is True
    assert len(pool.connection_value.cursor_value.executions) == 1
