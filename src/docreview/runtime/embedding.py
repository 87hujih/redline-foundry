"""Background worker for projecting pending document chunk embeddings."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import Protocol


class EmbeddingProjector(Protocol):
    async def project_once(
        self, *, request_id: str | None = None, trace_id: str | None = None
    ) -> int: ...


class ChunkEmbeddingWorker:
    """Continuously drains pending chunks without taking down the API on provider errors."""

    def __init__(
        self,
        projector: EmbeddingProjector,
        *,
        logger: logging.Logger | None = None,
        worker_id: str = "embedding",
    ) -> None:
        if not worker_id.strip():
            raise ValueError("嵌入 工作进程 ID 不能为空")
        self._projector = projector
        self._logger = logger or logging.getLogger("docreview.embedding")
        self._worker_id = worker_id.strip()

    async def process_one(self) -> int:
        return await self._projector.project_once(
            request_id=f"{self._worker_id}:request",
            trace_id=f"{self._worker_id}:trace",
        )

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        if poll_interval <= timedelta(0):
            raise ValueError("嵌入 工作进程 轮询间隔必须为正数")
        while not stop.is_set():
            try:
                written = await self.process_one()
            except Exception:
                self._logger.exception("embedding projection failed")
                written = 0
            if written == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), poll_interval.total_seconds())


__all__ = ["ChunkEmbeddingWorker", "EmbeddingProjector"]
