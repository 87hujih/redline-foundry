from __future__ import annotations

from typing import Any, cast

import pytest

from docreview.agent_graph.checkpoint import StoredWrite
from docreview.storage.postgres.checkpoint import (
    DELETE_THREAD_SQL,
    INSERT_ARTIFACT_SQL,
    INSERT_WRITE_SQL,
    LIST_ARTIFACTS_SQL,
    LOAD_ARTIFACT_SQL,
    PostgresCheckpointRepository,
)


def test_checkpoint_sql_uses_existing_scoped_artifact_projection() -> None:
    for query in (INSERT_ARTIFACT_SQL, LOAD_ARTIFACT_SQL, LIST_ARTIFACTS_SQL):
        assert "agent_artifacts" in query
        assert "agent_runs" in query
        assert "workspace_id" in query
        assert "run_id" in query
    assert "ON CONFLICT" in INSERT_ARTIFACT_SQL
    assert "content_json" in INSERT_ARTIFACT_SQL
    assert "langgraph_" not in INSERT_ARTIFACT_SQL
    assert "DELETE FROM agent_artifacts" in DELETE_THREAD_SQL
    assert "content_json->>'kind'" in DELETE_THREAD_SQL


def test_checkpoint_write_sql_rejects_a_conflicting_replay() -> None:
    assert "ON CONFLICT (workspace_id, idempotency_key) DO UPDATE" in INSERT_WRITE_SQL
    assert "agent_artifacts.content_hash = EXCLUDED.content_hash" in INSERT_WRITE_SQL
    assert "agent_artifacts.content_json = EXCLUDED.content_json" in INSERT_WRITE_SQL


class _Cursor:
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...] | None] = [(True,), None]

    async def execute(self, query: str, params: object = ()) -> None:
        del query, params

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0)

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.value = _Cursor()

    def cursor(self) -> _Cursor:
        return self.value

    def transaction(self) -> _Connection:
        return self

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.value = _Connection()

    def connection(self) -> _Connection:
        return self.value


@pytest.mark.asyncio
async def test_checkpoint_write_conflict_fails_the_transaction() -> None:
    repository = PostgresCheckpointRepository(cast(Any, _Pool()))

    with pytest.raises(RuntimeError, match="idempotency conflict"):
        await repository.save_writes(
            [StoredWrite("run-1", "", "checkpoint-1", "task-1", "", 0, "value", b"{}")]
        )
