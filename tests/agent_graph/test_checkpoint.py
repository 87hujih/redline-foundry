from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint
from langgraph.types import Interrupt

from docreview.agent_graph.checkpoint import (
    InMemoryCheckpointRepository,
    ProjectCheckpointer,
)


def config(run_id: str = "run-1") -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "run_id": run_id,
            "checkpoint_ns": "",
        }
    }


def checkpoint(checkpoint_id: str = "checkpoint-1") -> Checkpoint:
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-08-13T00:00:00+00:00",
        "channel_values": {"state": {"sequence": 1}},
        "channel_versions": {"state": 1},
        "versions_seen": {},
        "updated_channels": None,
    }


def test_project_checkpointer_round_trip_and_interrupt_marker() -> None:
    repository = InMemoryCheckpointRepository()
    saver = ProjectCheckpointer(repository)
    saved = saver.put(config(), checkpoint(), {"source": "input", "step": -1}, {})
    saver.put_writes(
        saved,
        [("__interrupt__", Interrupt(value={"request_id": "r"}, id="i"))],
        "task-1",
    )
    loaded = saver.get_tuple(saved)
    assert loaded is not None
    loaded_value = loaded
    assert loaded_value.checkpoint["id"] == "checkpoint-1"
    assert loaded_value.pending_writes is not None
    assert loaded_value.pending_writes[0][1] == "__interrupt__"
    assert isinstance(loaded_value.pending_writes[0][2], Interrupt)


def test_project_checkpointer_requires_durable_thread_binding_and_json_values() -> None:
    saver = ProjectCheckpointer(InMemoryCheckpointRepository())
    with pytest.raises(ValueError, match="thread_id"):
        saver.put(
            {"configurable": {"thread_id": "wrong", "run_id": "run-1"}},
            checkpoint(),
            {"source": "input", "step": -1},
            {},
        )
    with pytest.raises(ValueError, match="bounded JSON"):
        saver.put(
            config(),
            {**checkpoint(), "channel_values": {"bad": object()}},  # type: ignore[reportArgumentType]
            {"source": "input", "step": -1},
            {},
        )
