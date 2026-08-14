"""Lease-fenced transactional-outbox projection worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from docreview.runtime.contracts import ProjectionWorkerConfig
from docreview.runtime.models import Outbox, RunStatus


class ProjectionStore(Protocol):
    async def recover_expired_outbox(self, now: datetime) -> int: ...
    async def claim_outbox(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        event_types: tuple[str, ...],
    ) -> list[Outbox]: ...
    async def mark_outbox_published(self, event: Outbox, published_at: datetime) -> None: ...
    async def schedule_outbox_retry(
        self,
        event: Outbox,
        error: dict[str, object],
        next_attempt_at: datetime,
        now: datetime,
        dead_letter: bool,
    ) -> None: ...


class RuntimeSnapshot:
    def __init__(
        self,
        turn_id: str,
        run_id: str,
        run_status: str,
        step_type: str,
        output: dict[str, object],
        error: dict[str, object] | None,
    ) -> None:
        self.turn_id = turn_id
        self.run_id = run_id
        self.run_status = run_status
        self.step_type = step_type
        self.output = output
        self.error = error


class SnapshotReader(Protocol):
    async def load(self, event: Outbox) -> RuntimeSnapshot: ...


class OutcomeCommitter(Protocol):
    async def commit_projection_outcome(
        self,
        turn_id: str,
        idempotency_key: str,
        status: str,
        output: dict[str, object],
        error: dict[str, object] | None,
        message: dict[str, str] | None,
    ) -> None: ...


class ReceiptStore(Protocol):
    async def exists(self, event_id: str, projection_name: str) -> bool: ...
    async def record(self, event_id: str, projection_name: str, payload_hash: str) -> None: ...


PROJECTION_NAME = "agent-turn-runtime-v1"


def event_hash(event: Outbox) -> str:
    payload = {
        "id": event.id,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "event_type": event.event_type,
        "payload": event.payload,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


class RuntimeProjector:
    def __init__(
        self, reader: SnapshotReader, committer: OutcomeCommitter, receipts: ReceiptStore
    ) -> None:
        self.reader = reader
        self.committer = committer
        self.receipts = receipts

    async def project(self, event: Outbox) -> None:
        if event.event_type not in {"agent.step.outcome_committed", "agent.tool_approval.rejected"}:
            raise ValueError(f"unsupported runtime projection event {event.event_type!r}")
        if await self.receipts.exists(event.id, PROJECTION_NAME):
            return
        snapshot = await self.reader.load(event)
        status = self._turn_status(snapshot.run_status)
        message = None
        if status == RunStatus.SUCCEEDED.value and snapshot.step_type == "RenderOutcome":
            content = str(snapshot.output.get("message", "")).strip()
            if not content:
                raise ValueError("RenderOutcome projection requires a typed message")
            message = {"role": "assistant", "kind": "text", "content": content}
        await self.committer.commit_projection_outcome(
            snapshot.turn_id,
            f"outbox-projection:{event.id}",
            status,
            snapshot.output,
            snapshot.error,
            message,
        )
        await self.receipts.record(event.id, PROJECTION_NAME, event_hash(event))

    @staticmethod
    def _turn_status(status: str) -> str:
        mapping = {
            "queued": RunStatus.RUNNING.value,
            "running": RunStatus.RUNNING.value,
            "waiting_input": RunStatus.WAITING_INPUT.value,
            "waiting_approval": RunStatus.WAITING_APPROVAL.value,
            "succeeded": RunStatus.SUCCEEDED.value,
            "failed": RunStatus.FAILED.value,
            "cancelled": RunStatus.CANCELLED.value,
        }
        if status not in mapping:
            raise ValueError(f"invalid runtime run status {status!r}")
        return mapping[status]


class ProjectionWorker:
    def __init__(
        self,
        config: ProjectionWorkerConfig,
        store: ProjectionStore,
        projector: RuntimeProjector,
        clock: object | None = None,
    ) -> None:
        if (
            not config.worker_id.strip()
            or config.lease_duration <= timedelta(0)
            or config.batch_size < 1
            or config.batch_size > 1000
            or config.max_attempts < 1
            or config.retry_base <= timedelta(0)
            or config.retry_max < config.retry_base
        ):
            raise ValueError("invalid projection worker configuration")
        self.config = config
        self.store = store
        self.projector = projector
        self.clock = clock

    def now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            return self.clock.now()  # type: ignore[no-any-return]
        return datetime.now(UTC)

    async def process_one(self) -> bool:
        now = self.now()
        events = await self.store.claim_outbox(
            self.config.worker_id,
            now,
            self.config.lease_duration,
            self.config.batch_size,
            self.config.event_types,
        )
        for event in events:
            try:
                await self.projector.project(event)
            except Exception as exc:
                dead_letter = event.attempt_count >= self.config.max_attempts
                delay = now + self._backoff(event.attempt_count) if not dead_letter else now
                await self.store.schedule_outbox_retry(
                    event,
                    {
                        "category": "projection_failed",
                        "message": str(exc),
                        "retryable": not dead_letter,
                    },
                    delay,
                    now,
                    dead_letter,
                )
                continue
            await self.store.mark_outbox_published(event, self.now())
        return bool(events)

    async def recover(self) -> int:
        return await self.store.recover_expired_outbox(self.now())

    def _backoff(self, attempt: int) -> timedelta:
        value = self.config.retry_base
        for _ in range(1, max(attempt, 1)):
            value = min(value * 2, self.config.retry_max)
        return min(value, self.config.retry_max)

    async def run(self, stop: asyncio.Event, poll_interval: timedelta) -> None:
        await self.recover()
        while not stop.is_set():
            if not await self.process_one():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), poll_interval.total_seconds())


__all__ = [
    "PROJECTION_NAME",
    "ProjectionWorker",
    "RuntimeProjector",
    "RuntimeSnapshot",
    "event_hash",
]
