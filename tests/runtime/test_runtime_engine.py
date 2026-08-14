from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from docreview.runtime.contracts import AttemptFinish, EngineConfig, OutcomeCommit, RetryCommit
from docreview.runtime.engine import RuntimeEngine, backoff
from docreview.runtime.errors import ErrorCategory, ExecutionFailure, LeaseLostError
from docreview.runtime.models import (
    Attempt,
    ExecutionInput,
    ExecutionResult,
    Outcome,
    RunStatus,
    StepStatus,
    WorkItem,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def work(**changes: object) -> WorkItem:
    values: dict[str, object] = {
        "run_id": "run-1",
        "run_version": 1,
        "run_deadline_at": None,
        "cancel_requested_at": None,
        "step_id": "step-1",
        "step_key": "understand:1",
        "step_type": "UnderstandGoal",
        "input": {},
        "attempt_number": 1,
        "max_attempts": 3,
        "lease_generation": 1,
        "claimed_by": "worker-1",
        "step_started_at": NOW,
        "max_steps": 64,
        "step_count": 1,
        "max_tool_calls": 32,
        "tool_call_count": 0,
        "token_budget": None,
        "tokens_used": 0,
        "cost_budget": None,
        "cost_used": 0.0,
    }
    values.update(changes)
    return WorkItem(**values)  # type: ignore[arg-type]


class FakeClock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class FakeStore:
    def __init__(self, item: WorkItem | None) -> None:
        self.item = item
        self.heartbeats = 0
        self.attempts: list[Attempt] = []
        self.finished: list[AttemptFinish] = []
        self.outcomes: list[OutcomeCommit] = []
        self.retries: list[RetryCommit] = []
        self.recoveries = 0

    async def recover_expired_steps(self, now: datetime) -> tuple[int, int]:
        self.recoveries += 1
        return (1, 0)

    async def claim_step(
        self, worker_id: str, now: datetime, lease_duration: timedelta
    ) -> WorkItem | None:
        item, self.item = self.item, None
        return item

    async def heartbeat_step(
        self, work: WorkItem, now: datetime, lease_duration: timedelta
    ) -> None:
        self.heartbeats += 1

    async def start_attempt(
        self, step_id: str, number: int, trace_id: str, started_at: datetime
    ) -> Attempt:
        value = Attempt(
            id="attempt-1",
            step_id=step_id,
            attempt_number=number,
            provider=None,
            model=None,
            prompt_version=None,
            temperature=None,
            context_manifest_id=None,
            trace_id=trace_id,
            input_tokens=None,
            output_tokens=None,
            cost=None,
            latency_ms=None,
            retry_count=0,
            finish_reason=None,
            error_category=None,
            started_at=started_at,
            completed_at=None,
        )
        self.attempts.append(value)
        return value

    async def finish_attempt(self, command: AttemptFinish) -> None:
        self.finished.append(command)

    async def commit_outcome(self, command: OutcomeCommit) -> None:
        self.outcomes.append(command)

    async def schedule_retry(self, command: RetryCommit) -> None:
        self.retries.append(command)


class FakeExecutor:
    def __init__(self, result: ExecutionResult, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.inputs: list[ExecutionInput] = []

    async def execute(self, input: ExecutionInput) -> ExecutionResult:
        self.inputs.append(input)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def engine(
    store: FakeStore, executor: FakeExecutor, clock: FakeClock | None = None
) -> RuntimeEngine:
    return RuntimeEngine(
        EngineConfig(
            worker_id="worker-1",
            lease_duration=timedelta(seconds=10),
            heartbeat_interval=timedelta(milliseconds=5),
            attempt_timeout=timedelta(seconds=1),
            step_timeout=timedelta(seconds=20),
            retry_base=timedelta(seconds=2),
            retry_max=timedelta(seconds=20),
        ),
        store,
        executor,
        clock or FakeClock(),
    )


@pytest.mark.asyncio
async def test_success_uses_stable_key_and_lease_fenced_outcome() -> None:
    store = FakeStore(work())
    executor = FakeExecutor(ExecutionResult(outcome=Outcome.SUCCEED, output={"message": "ok"}))

    assert await engine(store, executor).process_one() is True
    assert executor.inputs[0].idempotency_key == "agent-step:step-1"
    assert store.outcomes[0].step_status is StepStatus.SUCCEEDED
    assert store.outcomes[0].run_status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_retry_uses_deterministic_backoff_and_does_not_commit_terminal() -> None:
    store = FakeStore(work())
    error = ExecutionFailure(ErrorCategory.TIMEOUT, "provider timeout")
    executor = FakeExecutor(ExecutionResult(error=error))

    await engine(store, executor).process_one()
    assert len(store.retries) == 1
    assert store.retries[0].next_retry_at == NOW + timedelta(seconds=2)
    assert not store.outcomes
    assert backoff(timedelta(seconds=2), timedelta(seconds=5), 4) == timedelta(seconds=5)


@pytest.mark.asyncio
async def test_cancel_and_run_timeout_skip_executor_and_attempt() -> None:
    store = FakeStore(work(cancel_requested_at=NOW))
    executor = FakeExecutor(ExecutionResult())
    await engine(store, executor).process_one()
    assert not executor.inputs
    assert not store.attempts
    assert store.outcomes[0].run_status is RunStatus.CANCELLED

    store = FakeStore(work(run_deadline_at=NOW))
    executor = FakeExecutor(ExecutionResult())
    await engine(store, executor).process_one()
    assert store.outcomes[0].error is not None
    assert store.outcomes[0].error.category is ErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_attempt_timeout_is_terminal_after_max_attempts() -> None:
    store = FakeStore(work(attempt_number=3, max_attempts=3))
    executor = FakeExecutor(ExecutionResult(), delay=0.05)
    config_clock = FakeClock()
    runtime = RuntimeEngine(
        EngineConfig(
            worker_id="worker-1",
            lease_duration=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=5),
            attempt_timeout=timedelta(milliseconds=1),
            step_timeout=timedelta(seconds=5),
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=2),
        ),
        store,
        executor,
        config_clock,
    )
    await runtime.process_one()
    assert not store.retries
    assert store.outcomes[0].error is not None


@pytest.mark.asyncio
async def test_stale_heartbeat_is_propagated_and_not_swallowed() -> None:
    store = FakeStore(work())

    async def lost_heartbeat(work: WorkItem, now: datetime, lease_duration: timedelta) -> None:
        raise LeaseLostError("stale worker")

    store.heartbeat_step = lost_heartbeat  # type: ignore[method-assign]
    executor = FakeExecutor(ExecutionResult(), delay=0.05)
    with pytest.raises(LeaseLostError):
        await engine(store, executor).process_one()


@pytest.mark.asyncio
async def test_invalid_executor_telemetry_is_persisted_as_invalid_input() -> None:
    store = FakeStore(work())
    executor = FakeExecutor(ExecutionResult(outcome=Outcome.SUCCEED, input_tokens=-1))

    await engine(store, executor).process_one()

    assert store.finished[0].input_tokens == 0
    assert store.finished[0].error_category == ErrorCategory.INVALID_INPUT.value
    assert store.outcomes[0].error is not None
    assert store.outcomes[0].error.category is ErrorCategory.INVALID_INPUT
