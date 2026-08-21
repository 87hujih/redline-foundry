from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint
from langgraph.types import Interrupt

from docreview.agent_graph.checkpoint import (
    AsyncCheckpointRepository,
    AsyncProjectCheckpointer,
    StoredCheckpoint,
    StoredStepResult,
    StoredWrite,
)


def config(run_id: str = "run-1") -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "run_id": run_id,
            "checkpoint_ns": "step:step-1",
        }
    }


def checkpoint(checkpoint_id: str = "checkpoint-1") -> Checkpoint:
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-08-15T00:00:00+00:00",
        "channel_values": {"state": {"sequence": 1}},
        "channel_versions": {"state": 1},
        "versions_seen": {},
        "updated_channels": None,
    }


class AsyncMemoryRepository(AsyncCheckpointRepository):
    def __init__(self) -> None:
        self.checkpoints: dict[tuple[str, str, str], StoredCheckpoint] = {}
        self.writes: dict[tuple[str, str, str, str, int], StoredWrite] = {}
        self.results: dict[tuple[str, str], StoredStepResult] = {}

    async def save_checkpoint(self, value: StoredCheckpoint) -> None:
        key = (value.run_id, value.namespace, value.checkpoint_id)
        existing = self.checkpoints.get(key)
        if existing is not None and existing != value:
            raise RuntimeError("checkpoint idempotency conflict")
        self.checkpoints[key] = value

    async def load_checkpoint(
        self, run_id: str, namespace: str, checkpoint_id: str | None
    ) -> StoredCheckpoint | None:
        if checkpoint_id is not None:
            return self.checkpoints.get((run_id, namespace, checkpoint_id))
        values = [
            value
            for value in self.checkpoints.values()
            if value.run_id == run_id and value.namespace == namespace
        ]
        return max(values, key=lambda item: item.checkpoint_id) if values else None

    async def list_checkpoints(
        self,
        run_id: str | None,
        namespace: str | None,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> Sequence[StoredCheckpoint]:
        values = [
            value
            for value in self.checkpoints.values()
            if (run_id is None or value.run_id == run_id)
            and (namespace is None or value.namespace == namespace)
            and (before_checkpoint_id is None or value.checkpoint_id < before_checkpoint_id)
        ]
        values.sort(key=lambda item: item.checkpoint_id, reverse=True)
        return values if limit is None else values[:limit]

    async def save_writes(self, values: Sequence[StoredWrite]) -> None:
        for value in values:
            key = (
                value.run_id,
                value.namespace,
                value.checkpoint_id,
                value.task_id,
                value.index,
            )
            self.writes.setdefault(key, value)

    async def load_writes(
        self, run_id: str, namespace: str, checkpoint_id: str
    ) -> Sequence[StoredWrite]:
        return sorted(
            (
                value
                for value in self.writes.values()
                if value.run_id == run_id
                and value.namespace == namespace
                and value.checkpoint_id == checkpoint_id
            ),
            key=lambda item: (item.task_id, item.index),
        )

    async def delete_thread(self, run_id: str) -> None:
        self.checkpoints = {
            key: value for key, value in self.checkpoints.items() if value.run_id != run_id
        }
        self.writes = {key: value for key, value in self.writes.items() if value.run_id != run_id}
        self.results = {key: value for key, value in self.results.items() if value.run_id != run_id}

    async def save_step_result(self, value: StoredStepResult) -> None:
        key = (value.run_id, value.step_id)
        existing = self.results.get(key)
        if existing is not None and existing != value:
            raise RuntimeError("graph Step result idempotency conflict")
        self.results[key] = value

    async def load_step_result(self, run_id: str, step_id: str) -> StoredStepResult | None:
        return self.results.get((run_id, step_id))


async def test_async_project_checkpointer_round_trip_and_step_replay() -> None:
    repository = AsyncMemoryRepository()
    saver = AsyncProjectCheckpointer(repository)

    saved = await saver.aput(config(), checkpoint(), {"source": "input", "step": -1}, {})
    await saver.aput_writes(
        saved,
        [("__interrupt__", Interrupt(value={"request_id": "request-1"}, id="i-1"))],
        "task-1",
    )
    await saver.aput_step_result("run-1", "step-1", {"outcome": "continue"})

    loaded = await saver.aget_tuple(saved)
    assert loaded is not None
    assert loaded.checkpoint["id"] == "checkpoint-1"
    assert loaded.pending_writes is not None
    assert isinstance(loaded.pending_writes[0][2], Interrupt)
    assert await saver.aget_step_result("run-1", "step-1") == {"outcome": "continue"}


def test_async_project_checkpointer_rejects_sync_entrypoints() -> None:
    saver = AsyncProjectCheckpointer(AsyncMemoryRepository())

    with pytest.raises(RuntimeError, match="async"):
        saver.put(config(), checkpoint(), {"source": "input", "step": -1}, {})
