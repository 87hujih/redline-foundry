from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docreview.runtime.contracts import ProjectionWorkerConfig
from docreview.runtime.models import Outbox, OutboxStatus
from docreview.runtime.projection import (
    PROJECTION_NAME,
    ProjectionWorker,
    RuntimeProjector,
    RuntimeSnapshot,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def event(**changes: object) -> Outbox:
    values: dict[str, object] = {
        "id": "event-1",
        "aggregate_type": "agent_run",
        "aggregate_id": "run-1",
        "event_type": "agent.step.outcome_committed",
        "idempotency_key": "key-1",
        "payload": {"run_id": "run-1", "step_id": "step-1", "run_status": "succeeded"},
        "status": OutboxStatus.PUBLISHING,
        "attempt_count": 1,
        "next_attempt_at": None,
        "claimed_by": "projection-1",
        "lease_expires_at": NOW + timedelta(seconds=10),
        "lease_generation": 1,
        "error": None,
        "created_at": NOW,
        "published_at": None,
    }
    values.update(changes)
    return Outbox(**values)  # type: ignore[arg-type]


class Store:
    def __init__(self, item: Outbox) -> None:
        self.item = item
        self.published: list[str] = []
        self.retries: list[tuple[str, bool]] = []

    async def recover_expired_outbox(self, now: datetime) -> int:
        return 0

    async def claim_outbox(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        event_types: tuple[str, ...],
    ) -> list[Outbox]:
        if self.item is None:  # type: ignore[comparison-overlap]
            return []
        item, self.item = self.item, None  # type: ignore[assignment]
        return [item]

    async def mark_outbox_published(self, event: Outbox, published_at: datetime) -> None:
        self.published.append(event.id)

    async def schedule_outbox_retry(
        self,
        event: Outbox,
        error: dict[str, object],
        next_attempt_at: datetime,
        now: datetime,
        dead_letter: bool,
    ) -> None:
        self.retries.append((event.id, dead_letter))


class Reader:
    async def load(self, event: Outbox) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            "turn-1", "run-1", "succeeded", "RenderOutcome", {"message": "done"}, None
        )


class Committer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def commit_projection_outcome(
        self,
        turn_id: str,
        idempotency_key: str,
        status: str,
        output: dict[str, object],
        error: dict[str, object] | None,
        message: dict[str, str] | None,
    ) -> None:
        if (turn_id, idempotency_key) not in self.calls:
            self.calls.append((turn_id, idempotency_key))


class Receipts:
    def __init__(self) -> None:
        self.values: set[tuple[str, str]] = set()

    async def exists(self, event_id: str, projection_name: str) -> bool:
        return (event_id, projection_name) in self.values

    async def record(self, event_id: str, projection_name: str, payload_hash: str) -> None:
        self.values.add((event_id, projection_name))


@pytest.mark.asyncio
async def test_projection_replays_after_receipt_gap_and_publishes_once() -> None:
    item = event()
    store = Store(item)
    committer = Committer()
    receipts = Receipts()
    projector = RuntimeProjector(Reader(), committer, receipts)
    worker = ProjectionWorker(
        ProjectionWorkerConfig(
            worker_id="projection-1",
            lease_duration=timedelta(seconds=10),
            batch_size=10,
            max_attempts=3,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=4),
        ),
        store,
        projector,
    )
    assert await worker.process_one() is True
    assert committer.calls == [("turn-1", "outbox-projection:event-1")]
    assert store.published == ["event-1"]

    # A second delivery is an idempotent receipt replay, not a second public write.
    await projector.project(item)
    assert len(committer.calls) == 1
    assert PROJECTION_NAME == "agent-turn-runtime-v1"


@pytest.mark.asyncio
async def test_projection_failure_is_retryable_then_dead_lettered() -> None:
    class BrokenReader:
        async def load(self, event: Outbox) -> RuntimeSnapshot:
            raise RuntimeError("snapshot unavailable")

    store = Store(event(attempt_count=3))
    worker = ProjectionWorker(
        ProjectionWorkerConfig(
            worker_id="projection-1",
            lease_duration=timedelta(seconds=10),
            batch_size=10,
            max_attempts=3,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=4),
        ),
        store,
        RuntimeProjector(BrokenReader(), Committer(), Receipts()),
    )
    await worker.process_one()
    assert store.retries == [("event-1", True)]


@pytest.mark.asyncio
async def test_projection_receipt_gap_replays_same_outcome_key_without_duplicate_commit() -> None:
    item = event()
    committer = Committer()
    receipts = Receipts()
    projector = RuntimeProjector(Reader(), committer, receipts)
    await projector.project(item)
    assert committer.calls == [("turn-1", "outbox-projection:event-1")]
    # Simulate a crash after the public outcome commit but before receipt write.
    receipts.values.clear()
    await projector.project(item)
    assert committer.calls == [("turn-1", "outbox-projection:event-1")]
