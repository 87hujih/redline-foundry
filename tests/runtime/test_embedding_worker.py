from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from docreview.runtime.embedding import ChunkEmbeddingWorker


@pytest.mark.asyncio
async def test_embedding_worker_drains_projector_until_stopped() -> None:
    stop = asyncio.Event()

    class Projector:
        calls = 0

        async def project_once(
            self, *, request_id: str | None = None, trace_id: str | None = None
        ) -> int:
            assert request_id == "test:request"
            assert trace_id == "test:trace"
            self.calls += 1
            stop.set()
            return 1

    projector = Projector()
    worker = ChunkEmbeddingWorker(projector, worker_id="test")
    await worker.run(stop, timedelta(milliseconds=1))
    assert projector.calls == 1


@pytest.mark.asyncio
async def test_embedding_worker_keeps_running_after_provider_failure() -> None:
    stop = asyncio.Event()

    class Projector:
        calls = 0

        async def project_once(
            self, *, request_id: str | None = None, trace_id: str | None = None
        ) -> int:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            stop.set()
            return 0

    projector = Projector()
    worker = ChunkEmbeddingWorker(projector, worker_id="test")
    await worker.run(stop, timedelta(milliseconds=1))
    assert projector.calls == 2
