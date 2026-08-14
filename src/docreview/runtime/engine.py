"""Durable runtime worker orchestration without owning business state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from docreview.runtime.contracts import AttemptFinish, EngineConfig, OutcomeCommit, RetryCommit
from docreview.runtime.errors import ErrorCategory, ExecutionFailure, TimeoutScope
from docreview.runtime.models import (
    Attempt,
    ExecutionInput,
    ExecutionResult,
    Outcome,
    RunStatus,
    StepStatus,
    WorkItem,
)


class RuntimeStore(Protocol):
    async def recover_expired_steps(self, now: datetime) -> tuple[int, int]: ...
    async def claim_step(
        self, worker_id: str, now: datetime, lease_duration: timedelta
    ) -> WorkItem | None: ...
    async def heartbeat_step(
        self, work: WorkItem, now: datetime, lease_duration: timedelta
    ) -> None: ...
    async def start_attempt(
        self, step_id: str, number: int, trace_id: str, started_at: datetime
    ) -> Attempt: ...
    async def finish_attempt(self, command: AttemptFinish) -> None: ...
    async def commit_outcome(self, command: OutcomeCommit) -> None: ...
    async def schedule_retry(self, command: RetryCommit) -> None: ...


class RuntimeExecutor(Protocol):
    async def execute(self, input: ExecutionInput) -> ExecutionResult: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def backoff(base: timedelta, maximum: timedelta, attempt: int) -> timedelta:
    value = base
    for _ in range(1, max(attempt, 1)):
        value = min(value * 2, maximum)
    return min(value, maximum)


def timeout_failure(scope: str) -> ExecutionFailure:
    from docreview.runtime.errors import TimeoutScope

    return ExecutionFailure(ErrorCategory.TIMEOUT, f"{scope} timeout exceeded", TimeoutScope(scope))


class RuntimeEngine:
    def __init__(
        self,
        config: EngineConfig,
        store: RuntimeStore,
        executor: RuntimeExecutor,
        clock: Clock | None = None,
    ) -> None:
        if (
            not config.worker_id.strip()
            or config.lease_duration <= timedelta(0)
            or config.heartbeat_interval <= timedelta(0)
            or config.heartbeat_interval >= config.lease_duration
            or config.attempt_timeout <= timedelta(0)
            or config.step_timeout <= timedelta(0)
            or config.retry_base <= timedelta(0)
            or config.retry_max < config.retry_base
        ):
            raise ValueError("invalid durable runtime engine configuration")
        self.config = config
        self.store = store
        self.executor = executor
        self.clock = clock or SystemClock()

    async def recover(self) -> tuple[int, int]:
        return await self.store.recover_expired_steps(self.clock.now())

    async def process_one(self) -> bool:
        now = self.clock.now()
        work = await self.store.claim_step(self.config.worker_id, now, self.config.lease_duration)
        if work is None:
            return False
        if work.cancel_requested_at is not None:
            await self._commit_terminal(
                work,
                StepStatus.CANCELLED,
                RunStatus.CANCELLED,
                ExecutionFailure(ErrorCategory.CANCELLED, "run cancellation requested"),
            )
            return True
        if work.run_deadline_at is not None and work.run_deadline_at <= now:
            await self._commit_terminal(
                work, StepStatus.FAILED, RunStatus.FAILED, timeout_failure("run")
            )
            return True
        if (
            work.step_started_at is not None
            and work.step_started_at + self.config.step_timeout <= now
        ):
            await self._commit_terminal(
                work, StepStatus.FAILED, RunStatus.FAILED, timeout_failure("step")
            )
            return True
        limit_error = self._limit_error(work)
        if limit_error is not None:
            await self._commit_terminal(work, StepStatus.FAILED, RunStatus.FAILED, limit_error)
            return True

        started = self.clock.now()
        trace_id = f"{work.run_id}:{work.step_id}:{work.attempt_number}"
        attempt = await self.store.start_attempt(
            work.step_id, work.attempt_number, trace_id, started
        )
        timeout, timeout_scope = self._attempt_budget(work, started)
        try:
            result = await self._execute_with_heartbeat(work, trace_id, timeout)
        except TimeoutError:
            result = ExecutionResult(error=timeout_failure(timeout_scope))
        validation_error = self._validate_result(result)
        if validation_error is not None:
            result = ExecutionResult(
                error=ExecutionFailure(ErrorCategory.INVALID_INPUT, validation_error),
                provider=result.provider,
                model=result.model,
                prompt_version=result.prompt_version,
                temperature=result.temperature,
                context_manifest_id=result.context_manifest_id,
            )
        completed = self.clock.now()
        if result.error is not None:
            error_category: str | None = result.error.category.value
        else:
            error_category = None
        await self.store.finish_attempt(
            AttemptFinish(
                attempt_id=attempt.id,
                provider=result.provider,
                model=result.model,
                prompt_version=result.prompt_version,
                temperature=result.temperature,
                context_manifest_id=result.context_manifest_id,
                retry_count=result.retry_count,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost=result.cost,
                latency_ms=max(0, int((completed - started).total_seconds() * 1000)),
                finish_reason=result.finish_reason,
                error_category=error_category,
                completed_at=completed,
            )
        )
        if result.error is not None:
            if (
                result.error.category.retryable
                and work.attempt_number < work.max_attempts
                and result.error.timeout_scope not in {TimeoutScope.RUN, TimeoutScope.STEP}
                and work.run_deadline_at is not None
                and completed
                + backoff(self.config.retry_base, self.config.retry_max, work.attempt_number)
                < work.run_deadline_at
            ) or (
                result.error.category.retryable
                and work.attempt_number < work.max_attempts
                and result.error.timeout_scope not in {TimeoutScope.RUN, TimeoutScope.STEP}
                and work.run_deadline_at is None
            ):
                next_retry = completed + backoff(
                    self.config.retry_base, self.config.retry_max, work.attempt_number
                )
                await self.store.schedule_retry(
                    RetryCommit(work, result.error, next_retry, completed)
                )
            else:
                await self._commit_terminal(work, StepStatus.FAILED, RunStatus.FAILED, result.error)
            return True

        if result.outcome is Outcome.CONTINUE:
            projected = WorkItem(
                run_id=work.run_id,
                run_version=work.run_version,
                run_deadline_at=work.run_deadline_at,
                cancel_requested_at=work.cancel_requested_at,
                step_id=work.step_id,
                step_key=work.step_key,
                step_type=work.step_type,
                input=work.input,
                attempt_number=work.attempt_number,
                max_attempts=work.max_attempts,
                lease_generation=work.lease_generation,
                claimed_by=work.claimed_by,
                step_started_at=work.step_started_at,
                max_steps=work.max_steps,
                step_count=work.step_count + len(result.next_steps),
                max_tool_calls=work.max_tool_calls,
                tool_call_count=work.tool_call_count,
                token_budget=work.token_budget,
                tokens_used=work.tokens_used + result.input_tokens + result.output_tokens,
                cost_budget=work.cost_budget,
                cost_used=work.cost_used + result.cost,
            )
            limit_error = self._limit_error(projected)
            if limit_error is not None:
                await self._commit_terminal(work, StepStatus.FAILED, RunStatus.FAILED, limit_error)
                return True
        step_status, run_status = {
            Outcome.CONTINUE: (StepStatus.SUCCEEDED, RunStatus.QUEUED),
            Outcome.WAIT_INPUT: (StepStatus.WAITING_INPUT, RunStatus.WAITING_INPUT),
            Outcome.WAIT_APPROVAL: (StepStatus.WAITING_APPROVAL, RunStatus.WAITING_APPROVAL),
            Outcome.SUCCEED: (StepStatus.SUCCEEDED, RunStatus.SUCCEEDED),
        }[result.outcome]
        await self.store.commit_outcome(
            OutcomeCommit(
                work=work,
                step_status=step_status,
                run_status=run_status,
                output=result.output,
                error=None,
                next_steps=result.next_steps,
                committed_at=completed,
            )
        )
        return True

    async def _execute_with_heartbeat(
        self, work: WorkItem, trace_id: str, timeout: timedelta
    ) -> ExecutionResult:
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval.total_seconds())
                await self.store.heartbeat_step(work, self.clock.now(), self.config.lease_duration)

        executor_task = asyncio.create_task(
            self.executor.execute(
                ExecutionInput(
                    run_id=work.run_id,
                    step_id=work.step_id,
                    trace_id=trace_id,
                    step_key=work.step_key,
                    step_type=work.step_type,
                    input=work.input,
                    attempt_number=work.attempt_number,
                    idempotency_key=work.stable_idempotency_key,
                )
            )
        )
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait(
                {executor_task, heartbeat_task},
                timeout=timeout.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                # Calling result() is deliberate: stale-worker fencing errors
                # must reach the worker boundary and cannot be downgraded.
                heartbeat_task.result()
                raise RuntimeError("heartbeat task exited unexpectedly")
            if executor_task not in done:
                raise TimeoutError
            return executor_task.result()
        finally:
            executor_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(executor_task, heartbeat_task, return_exceptions=True)

    def _attempt_budget(self, work: WorkItem, now: datetime) -> tuple[timedelta, str]:
        duration = self.config.attempt_timeout
        scope = "attempt"
        if work.step_started_at is not None:
            remaining = work.step_started_at + self.config.step_timeout - now
            if remaining < duration:
                duration = remaining
                scope = "step"
        if work.run_deadline_at is not None:
            remaining = work.run_deadline_at - now
            if remaining < duration:
                duration = remaining
                scope = "run"
        return max(duration, timedelta(microseconds=1)), scope

    async def _commit_terminal(
        self,
        work: WorkItem,
        step_status: StepStatus,
        run_status: RunStatus,
        failure: ExecutionFailure,
    ) -> None:
        await self.store.commit_outcome(
            OutcomeCommit(
                work=work,
                step_status=step_status,
                run_status=run_status,
                output={},
                error=failure,
                next_steps=(),
                committed_at=self.clock.now(),
            )
        )

    @staticmethod
    def _limit_error(work: WorkItem) -> ExecutionFailure | None:
        if work.max_steps > 0 and work.step_count > work.max_steps:
            return ExecutionFailure(ErrorCategory.POLICY_BLOCKED, "maximum steps exceeded")
        if work.max_tool_calls > 0 and work.tool_call_count >= work.max_tool_calls:
            return ExecutionFailure(ErrorCategory.POLICY_BLOCKED, "maximum tool calls reached")
        if work.token_budget is not None and work.tokens_used >= work.token_budget:
            return ExecutionFailure(ErrorCategory.POLICY_BLOCKED, "token budget exhausted")
        if work.cost_budget is not None and work.cost_used >= work.cost_budget:
            return ExecutionFailure(ErrorCategory.POLICY_BLOCKED, "cost budget exhausted")
        return None

    @staticmethod
    def _validate_result(result: ExecutionResult) -> str | None:
        if (
            result.input_tokens < 0
            or result.output_tokens < 0
            or result.cost < 0
            or result.retry_count < 0
            or (result.temperature is not None and result.temperature < 0)
        ):
            return "execution usage values must be nonnegative"
        if result.error is not None:
            return None
        if result.outcome is Outcome.CONTINUE and not result.next_steps:
            return "continue outcome requires at least one next step"
        if result.outcome is not Outcome.CONTINUE and result.next_steps:
            return "terminal or waiting outcome cannot add next steps"
        seen: set[str] = set()
        for item in result.next_steps:
            if not item.step_key.strip() or not item.step_type.strip() or item.max_attempts < 0:
                return "next step identity and retry policy are invalid"
            if item.step_key.strip() in seen:
                return "next steps contain a duplicate step key"
            seen.add(item.step_key.strip())
        return None


__all__ = ["RuntimeEngine", "RuntimeExecutor", "RuntimeStore", "backoff"]
