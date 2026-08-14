"""Explicit lifecycle ownership for durable Runtime and Projection workers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import Protocol


class RuntimeEngine(Protocol):
    async def recover(self) -> object: ...
    async def process_one(self) -> bool: ...


class RuntimeWorker:
    def __init__(self, engine: RuntimeEngine) -> None:
        self._engine = engine

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        await self._engine.recover()
        while not stop.is_set():
            if not await self._engine.process_one():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), poll_interval.total_seconds())


class Worker(Protocol):
    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None: ...


class RuntimeLifecycle:
    def __init__(
        self,
        runtime: Worker | None,
        projection: Worker | None,
        poll_interval: timedelta,
    ) -> None:
        if runtime is None or projection is None:
            raise ValueError("both runtime and projection workers are required")
        if poll_interval <= timedelta(0):
            raise ValueError("worker poll interval must be positive")
        self.runtime = runtime
        self.projection = projection
        self.poll_interval = poll_interval
        self.started = False
        self._stop = asyncio.Event()
        self._tasks: tuple[asyncio.Task[None], asyncio.Task[None]] | None = None

    async def start(self) -> None:
        if self.started:
            return
        self._stop = asyncio.Event()
        tasks = (
            asyncio.create_task(self.runtime.run(self._stop, self.poll_interval)),
            asyncio.create_task(self.projection.run(self._stop, self.poll_interval)),
        )
        self._tasks = tasks
        self.started = True
        # Worker recovery runs inside each task. Give fail-fast startup errors one
        # scheduling turn so the sibling task can be stopped and joined.
        await asyncio.sleep(0)
        failures: list[BaseException] = []
        for task in tasks:
            if task.done() and not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    failures.append(failure)
        if failures:
            self._stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = None
            self.started = False
            raise failures[0]

    async def stop(self) -> None:
        if not self.started or self._tasks is None:
            return
        self._stop.set()
        tasks = self._tasks
        self._tasks = None
        self.started = False
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result


__all__ = ["RuntimeLifecycle", "RuntimeWorker", "Worker"]
