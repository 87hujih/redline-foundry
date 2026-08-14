from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from docreview.runtime.lifecycle import RuntimeLifecycle, RuntimeWorker


@dataclass
class Engine:
    values: list[bool]
    recovered: int = 0
    processed: int = 0

    async def recover(self) -> tuple[int, int]:
        self.recovered += 1
        return (0, 0)

    async def process_one(self) -> bool:
        self.processed += 1
        return self.values.pop(0) if self.values else False


@dataclass
class Worker:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        self.started.set()
        await stop.wait()
        self.stopped.set()


@dataclass
class FailingWorker:
    error: Exception

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        raise self.error


@dataclass
class DelayedFailingWorker:
    error: Exception

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        await asyncio.sleep(0.01)
        raise self.error


@pytest.mark.anyio
async def test_runtime_worker_recovers_then_polls_until_stopped() -> None:
    engine = Engine([True, False])
    worker = RuntimeWorker(engine)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop, timedelta(milliseconds=1)))
    await asyncio.sleep(0.01)
    stop.set()
    await task

    assert engine.recovered == 1
    assert engine.processed >= 2


@pytest.mark.anyio
async def test_lifecycle_starts_and_joins_runtime_and_projection_workers() -> None:
    runtime = Worker()
    projection = Worker()
    lifecycle = RuntimeLifecycle(runtime, projection, timedelta(milliseconds=1))

    await lifecycle.start()
    await asyncio.gather(runtime.started.wait(), projection.started.wait())
    assert lifecycle.started is True
    await lifecycle.stop()

    assert lifecycle.started is False
    assert runtime.stopped.is_set() and projection.stopped.is_set()


@pytest.mark.anyio
async def test_lifecycle_fails_closed_when_only_one_worker_is_configured() -> None:
    with pytest.raises(ValueError, match="both runtime and projection"):
        RuntimeLifecycle(Worker(), None, timedelta(milliseconds=1))


@pytest.mark.anyio
async def test_lifecycle_start_reclaims_sibling_when_worker_fails() -> None:
    projection = Worker()
    lifecycle = RuntimeLifecycle(
        FailingWorker(RuntimeError("runtime recovery failed")),
        projection,
        timedelta(milliseconds=1),
    )

    with pytest.raises(RuntimeError, match="runtime recovery failed"):
        await lifecycle.start()

    assert lifecycle.started is False
    assert projection.stopped.is_set()


@pytest.mark.anyio
async def test_lifecycle_stop_surfaces_worker_failure() -> None:
    runtime = Worker()
    projection = DelayedFailingWorker(RuntimeError("projection loop failed"))
    lifecycle = RuntimeLifecycle(runtime, projection, timedelta(milliseconds=1))

    await lifecycle.start()
    await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError, match="projection loop failed"):
        await lifecycle.stop()
    assert lifecycle.started is False
