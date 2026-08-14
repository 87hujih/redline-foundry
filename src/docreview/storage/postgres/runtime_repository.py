"""Active PostgreSQL repository for the Python durable runtime.

The repository intentionally uses small parameterized statements instead of an
ORM. PostgreSQL rows, constraints and locks remain the business fact source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from docreview.runtime.codec import canonical_json, require_object, same_json
from docreview.runtime.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    AttemptFinish,
    CreateRun,
    CreateStep,
    OutcomeCommit,
    RetryCommit,
)
from docreview.runtime.errors import (
    ApprovalConflictError,
    IdempotencyConflictError,
    LeaseLostError,
    RunConflictError,
)
from docreview.runtime.models import (
    Approval,
    Attempt,
    ContextManifest,
    JSONObject,
    Outbox,
    OutboxStatus,
    Run,
    RunStatus,
    Step,
    StepStatus,
    Tool,
    ToolStatus,
    WorkItem,
)
from docreview.storage.postgres.runtime_sql import (
    AUTHORIZE_APPROVAL_DECISION_SQL,
    AUTHORIZE_APPROVAL_RESOURCE_SQL,
    BEGIN_TOOL_SQL,
    CLAIM_OUTBOX_SQL,
    CLAIM_RUN_SQL,
    CLAIM_STEP_SQL,
    COMMIT_RUN_OUTCOME_SQL,
    COMMIT_STEP_OUTCOME_SQL,
    CREATE_APPROVAL_SQL,
    CREATE_APPROVED_STEP_SQL,
    CREATE_ATTEMPT_SQL,
    CREATE_CONTEXT_MANIFEST_SQL,
    CREATE_RUN_SQL,
    CREATE_STEP_SQL,
    DECIDE_APPROVAL_SQL,
    ENQUEUE_OUTBOX_SQL,
    FAIL_REJECTED_RUN_SQL,
    FAIL_REJECTED_STEP_SQL,
    FINISH_ATTEMPT_SQL,
    FINISH_TOOL_SQL,
    GET_APPROVAL_SQL,
    GET_ATTEMPT_SQL,
    GET_CONTEXT_MANIFEST_SQL,
    GET_OUTBOX_BY_KEY_SQL,
    GET_RUN_BY_REQUEST_SQL,
    GET_STEP_BY_KEY_SQL,
    GET_STEP_INPUT_SQL,
    HEARTBEAT_STEP_SQL,
    LOAD_WORK_SQL,
    LOCK_APPROVAL_SQL,
    LOCK_TOOL_BY_KEY_SQL,
    LOCK_WAITING_TARGET_SQL,
    MARK_OUTBOX_PUBLISHED_SQL,
    QUEUE_APPROVAL_RUN_SQL,
    RECLAIM_TOOL_SQL,
    RECOVER_EXPIRED_STEPS_SQL,
    RECOVER_OUTBOX_SQL,
    REQUEST_CANCEL_SQL,
    RETRY_OUTBOX_SQL,
    RETRY_STEP_SQL,
    SUCCEED_APPROVAL_STEP_SQL,
)


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def fetchall(self) -> list[tuple[object, ...]]: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self) -> Any: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


def _json(value: object) -> str:
    return canonical_json(value)


def _go_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _outcome_hash(command: OutcomeCommit) -> str:
    envelope: JSONObject = {
        "step_status": command.step_status.value,
        "run_status": command.run_status.value,
        "output_json": command.output,
    }
    if command.error is not None:
        envelope["error"] = command.error.as_json()
    envelope["next_steps"] = [
        {
            "step_key": item.step_key.strip(),
            "step_type": item.step_type.strip(),
            "input_json": item.input,
            "max_attempts": item.max_attempts or 5,
        }
        for item in command.next_steps
    ]
    envelope["observations"] = []
    return f"sha256:{hashlib.sha256(_go_json(envelope).encode()).hexdigest()}"


def _timestamp(value: datetime) -> str:
    text = value.astimezone(UTC).isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _resource_hash(resources: Sequence[JSONObject]) -> str:
    canonical = sorted(
        (
            {
                "type": str(item.get("type", "")).strip(),
                "id": str(item.get("id", "")).strip(),
                "access": str(item.get("access", "")).strip(),
            }
            for item in resources
        ),
        key=lambda item: (item["type"], item["id"], item["access"]),
    )
    return f"sha256:{hashlib.sha256(_go_json(canonical).encode()).hexdigest()}"


def _optional(value: object) -> str | None:
    text = str(value) if value is not None else ""
    return text or None


def _obj(value: object, field: str = "json") -> JSONObject:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return require_object(value, field)


def _list(value: object) -> list[JSONObject]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("items_json must be a JSON array")
    items = cast(list[object], value)
    return [require_object(item, "items_json item") for item in items]


def prepare_approval_continuation(
    value: object, approval_id: str, target_idempotency_key: str, status: str
) -> JSONObject:
    """Validate and bind a LangGraph checkpoint resume to one Approval/Patch fact."""
    continuation = _obj(value, "approval continuation")
    if continuation.get("approval_id") != approval_id or continuation.get("status") != "pending":
        raise ValueError("approval continuation identity mismatch")
    request = _obj(continuation.get("graph_request"), "approval graph request")
    if request.get("operation") != "await_approval":
        raise ValueError("approval continuation operation mismatch")
    request_payload = _obj(request.get("payload"), "approval graph request payload")
    if (
        request_payload.get("approval_id") != approval_id
        or request_payload.get("target_idempotency_key") != target_idempotency_key
    ):
        raise ValueError("approval continuation request binding mismatch")
    request_id = str(request.get("request_id", "")).strip()
    thread_id = str(continuation.get("checkpoint_thread_id", "")).strip()
    step_id = str(continuation.get("checkpoint_step_id", "")).strip()
    if not request_id or not thread_id or not step_id:
        raise ValueError("approval continuation checkpoint binding is missing")
    state = _obj(continuation.get("graph_state"), "approval continuation state")
    approval_ref = _obj(state.get("approval_ref"), "approval continuation approval")
    patch = _obj(state.get("patch_ref"), "approval continuation patch")
    if (
        approval_ref.get("approval_id") != approval_id
        or approval_ref.get("status") != "pending"
        or patch.get("valid") is not True
        or patch.get("target_idempotency_key") != target_idempotency_key
    ):
        raise ValueError("approval continuation patch binding mismatch")
    budget = _obj(state.get("budget"), "approval continuation budget")
    fact_id = str(approval_ref.get("fact_id", "")).strip()
    if not fact_id:
        raise ValueError("approval continuation approval fact is missing")
    return {
        "approval_id": approval_id,
        "status": "pending",
        "patch": patch,
        "graph_resume": {
            "checkpoint_thread_id": thread_id,
            "checkpoint_step_id": step_id,
            "response": {
                "request_id": request_id,
                "budget": budget,
                "data": {
                    "approval": {
                        "approval_id": approval_id,
                        "fact_id": fact_id,
                        "status": status,
                    }
                },
            },
        },
    }


def _run(row: tuple[object, ...]) -> Run:
    return Run(
        id=str(row[0]),
        organization_id=_optional(row[1]),
        workspace_id=_optional(row[2]),
        session_id=_optional(row[3]),
        request_id=_optional(row[4]),
        trace_id=_optional(row[5]),
        status=RunStatus(str(row[6])),
        objective=str(row[7]),
        current_step=_optional(row[8]),
        max_steps=int(cast(int, row[9])),
        max_tool_calls=int(cast(int, row[10])),
        token_budget=cast(int | None, row[11]),
        cost_budget=cast(float | None, row[12]),
        deadline_at=cast(datetime | None, row[13]),
        cancel_requested_at=cast(datetime | None, row[14]),
        state=_obj(row[15], "state_json"),
        version=int(cast(int, row[16])),
        created_at=cast(datetime, row[17]),
        updated_at=cast(datetime, row[18]),
        resource_id=_optional(row[19]),
        principal_type=_optional(row[20]),
        principal_id=_optional(row[21]),
        trust_source=_optional(row[22]),
        runtime_mode=_optional(row[23]),
    )


def _step(row: tuple[object, ...]) -> Step:
    return Step(
        id=str(row[0]),
        run_id=str(row[1]),
        step_key=str(row[2]),
        step_type=str(row[3]),
        status=StepStatus(str(row[4])),
        input=_obj(row[5], "input_json"),
        output=None if row[6] is None else _obj(row[6], "output_json"),
        error=None if row[7] is None else _obj(row[7], "error_json"),
        claimed_by=_optional(row[8]),
        lease_expires_at=cast(datetime | None, row[9]),
        heartbeat_at=cast(datetime | None, row[10]),
        lease_generation=int(cast(int, row[11])),
        attempt_count=int(cast(int, row[12])),
        max_attempts=int(cast(int, row[13])),
        next_retry_at=cast(datetime | None, row[14]),
        started_at=cast(datetime | None, row[15]),
        completed_at=cast(datetime | None, row[16]),
        created_at=cast(datetime, row[17]),
        updated_at=cast(datetime, row[18]),
    )


def _attempt(row: tuple[object, ...]) -> Attempt:
    return Attempt(
        id=str(row[0]),
        step_id=str(row[1]),
        attempt_number=int(cast(int, row[2])),
        provider=_optional(row[3]),
        model=_optional(row[4]),
        prompt_version=_optional(row[5]),
        temperature=cast(float | None, row[6]),
        context_manifest_id=_optional(row[7]),
        trace_id=_optional(row[8]),
        input_tokens=cast(int | None, row[9]),
        output_tokens=cast(int | None, row[10]),
        cost=cast(float | None, row[11]),
        latency_ms=cast(int | None, row[12]),
        retry_count=int(cast(int, row[13])),
        finish_reason=_optional(row[14]),
        error_category=_optional(row[15]),
        started_at=cast(datetime, row[16]),
        completed_at=cast(datetime | None, row[17]),
    )


def _manifest(row: tuple[object, ...]) -> ContextManifest:
    return ContextManifest(
        id=str(row[0]),
        run_id=str(row[1]),
        step_id=str(row[2]),
        token_budget=int(cast(int, row[3])),
        reserved_output_tokens=int(cast(int, row[4])),
        tokenizer=str(row[5]),
        items=_list(row[6]),
        total_tokens=int(cast(int, row[7])),
        content_hash=str(row[8]),
        created_at=cast(datetime, row[9]),
    )


def _tool(row: tuple[object, ...]) -> Tool:
    return Tool(
        id=str(row[0]),
        run_id=str(row[1]),
        step_id=str(row[2]),
        tool_name=str(row[3]),
        tool_version=str(row[4]),
        input=_obj(row[5], "input_json"),
        output=None if row[6] is None else _obj(row[6], "output_json"),
        status=ToolStatus(str(row[7])),
        idempotency_key=_optional(row[8]),
        error=None if row[9] is None else _obj(row[9], "error_json"),
        error_category=_optional(row[10]),
        claimed_by=_optional(row[11]),
        lease_expires_at=cast(datetime | None, row[12]),
        lease_generation=int(cast(int, row[13])),
        attempt_count=int(cast(int, row[14])),
        started_at=cast(datetime | None, row[15]),
        completed_at=cast(datetime | None, row[16]),
        created_at=cast(datetime, row[17]),
    )


def _outbox(row: tuple[object, ...]) -> Outbox:
    return Outbox(
        id=str(row[0]),
        aggregate_type=str(row[1]),
        aggregate_id=str(row[2]),
        event_type=str(row[3]),
        idempotency_key=str(row[4]),
        payload=_obj(row[5], "payload_json"),
        status=OutboxStatus(str(row[6])),
        attempt_count=int(cast(int, row[7])),
        next_attempt_at=cast(datetime | None, row[8]),
        claimed_by=_optional(row[9]),
        lease_expires_at=cast(datetime | None, row[10]),
        lease_generation=int(cast(int, row[11])),
        error=None if row[12] is None else _obj(row[12], "error_json"),
        created_at=cast(datetime, row[13]),
        published_at=cast(datetime | None, row[14]),
    )


def _approval(row: tuple[object, ...]) -> Approval:
    return Approval(
        id=str(row[0]),
        workspace_id=str(row[1]),
        run_id=str(row[2]),
        step_id=str(row[3]),
        tool_name=str(row[4]),
        tool_version=str(row[5]),
        idempotency_key=str(row[6]),
        resources=_list(row[7]),
        resources_hash=str(row[8]),
        payload=_obj(row[9], "payload_json"),
        reason=str(row[10]),
        status=str(row[11]),
        created_at=cast(datetime, row[12]),
    )


class RuntimeRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def create_run_with_initial_step(
        self, command: CreateRun, step: CreateStep
    ) -> tuple[Run, Step, bool]:
        self._validate_create_run(command)
        async with self._pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                run, created = await self._create_or_get_run(cursor, command)
                step_value, step_created = await self._create_or_get_step(cursor, run.id, step)
                event_key = f"agent-run-created:{run.id}"
                payload = {
                    "run_id": run.id,
                    "initial_step_id": step_value.id,
                    "initial_step_type": step_value.step_type,
                }
                await self.enqueue_outbox(
                    cursor, "agent_run", run.id, "agent.run.created", event_key, payload
                )
            return run, step_value, created or step_created

    async def _create_or_get_run(self, cursor: AsyncCursor, command: CreateRun) -> tuple[Run, bool]:
        params = (
            command.organization_id,
            command.workspace_id,
            command.session_id,
            command.request_id,
            command.trace_id,
            command.objective.strip(),
            command.max_steps,
            command.max_tool_calls,
            command.token_budget,
            command.cost_budget,
            command.deadline_at,
            _json(command.state),
            command.resource_id,
            command.principal_type,
            command.principal_id,
            command.trust_source,
        )
        await cursor.execute(CREATE_RUN_SQL, params)
        row = await cursor.fetchone()
        if row is not None:
            return _run(row), True
        await cursor.execute(GET_RUN_BY_REQUEST_SQL, (command.workspace_id, command.request_id))
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("run idempotency lookup returned no row")
        value = _run(row)
        if (
            value.objective != command.objective.strip()
            or value.organization_id != command.organization_id
            or value.workspace_id != command.workspace_id
            or value.session_id != command.session_id
            or value.trace_id != command.trace_id
            or value.resource_id != command.resource_id
            or value.principal_type != command.principal_type
            or value.principal_id != command.principal_id
            or value.trust_source != command.trust_source
            or value.max_steps != command.max_steps
            or value.max_tool_calls != command.max_tool_calls
            or value.token_budget != command.token_budget
            or value.cost_budget != command.cost_budget
            or value.deadline_at != command.deadline_at
            or not same_json(value.state, command.state)
        ):
            raise IdempotencyConflictError("run request idempotency conflict")
        return value, False

    async def _create_or_get_step(
        self, cursor: AsyncCursor, run_id: str, command: CreateStep
    ) -> tuple[Step, bool]:
        if not command.step_key.strip() or not command.step_type.strip():
            raise ValueError("step key and type are required")
        attempts = command.max_attempts or 5
        if attempts < 1:
            raise ValueError("max_attempts must be positive")
        await cursor.execute(
            CREATE_STEP_SQL,
            (
                run_id,
                command.step_key.strip(),
                command.step_type.strip(),
                _json(command.input),
                attempts,
            ),
        )
        row = await cursor.fetchone()
        if row is not None:
            return _step(row), True
        await cursor.execute(GET_STEP_BY_KEY_SQL, (run_id, command.step_key.strip()))
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("step idempotency lookup returned no row")
        value = _step(row)
        if (
            value.step_type != command.step_type.strip()
            or value.max_attempts != attempts
            or not same_json(value.input, command.input)
        ):
            raise IdempotencyConflictError("step idempotency conflict")
        return value, False

    async def claim_step(
        self, worker_id: str, now: datetime, lease_duration: timedelta
    ) -> WorkItem | None:
        if not worker_id.strip() or lease_duration <= timedelta(0):
            raise ValueError("worker_id and lease duration are required")
        async with self._pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                await cursor.execute(
                    CLAIM_STEP_SQL,
                    (now, now, worker_id.strip(), now + lease_duration, now, now, now),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                step = _step(row)
                await cursor.execute(CLAIM_RUN_SQL, (step.step_key, now, step.run_id))
                await cursor.fetchone()
                await cursor.execute(LOAD_WORK_SQL, (step.run_id,))
                work_row = await cursor.fetchone()
            if work_row is None:
                raise RuntimeError("claimed step run disappeared")
        return WorkItem(
            run_id=step.run_id,
            run_version=int(cast(int, work_row[0])),
            run_deadline_at=cast(datetime | None, work_row[1]),
            cancel_requested_at=cast(datetime | None, work_row[2]),
            step_id=step.id,
            step_key=step.step_key,
            step_type=step.step_type,
            input=step.input,
            attempt_number=step.attempt_count,
            max_attempts=step.max_attempts,
            lease_generation=step.lease_generation,
            claimed_by=worker_id.strip(),
            step_started_at=step.started_at,
            max_steps=int(cast(int, work_row[3])),
            step_count=int(cast(int, work_row[7])),
            max_tool_calls=int(cast(int, work_row[4])),
            tool_call_count=int(cast(int, work_row[8])),
            token_budget=cast(int | None, work_row[5]),
            tokens_used=int(cast(int, work_row[9])),
            cost_budget=cast(float | None, work_row[6]),
            cost_used=float(cast(float, work_row[10])),
        )

    async def heartbeat_step(
        self, work: WorkItem, now: datetime, lease_duration: timedelta
    ) -> None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                HEARTBEAT_STEP_SQL,
                (
                    now,
                    now + lease_duration,
                    now,
                    work.step_id,
                    work.claimed_by,
                    work.lease_generation,
                    now,
                ),
            )
            if await _rowcount(cursor) != 1:
                raise LeaseLostError("step lease lost")
            await connection.commit()

    async def start_attempt(
        self, step_id: str, number: int, trace_id: str, started_at: datetime
    ) -> Attempt:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(CREATE_ATTEMPT_SQL, (step_id, number, trace_id, started_at))
            row = await cursor.fetchone()
            if row is None:
                await cursor.execute(GET_ATTEMPT_SQL, (step_id, number))
                row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("attempt insert returned no row")
            await connection.commit()
        return _attempt(row)

    async def finish_attempt(self, command: AttemptFinish) -> None:
        await self._execute_lease_free(
            FINISH_ATTEMPT_SQL,
            (
                command.provider,
                command.model,
                command.prompt_version,
                command.temperature,
                command.context_manifest_id,
                command.input_tokens,
                command.output_tokens,
                command.cost,
                command.latency_ms,
                command.retry_count,
                command.finish_reason,
                command.error_category,
                command.completed_at,
                command.attempt_id,
            ),
            "attempt",
        )

    async def commit_outcome(self, command: OutcomeCommit) -> None:
        output = _json(command.output)
        error = None if command.error is None else _json(command.error.as_json())
        completed = (
            command.committed_at
            if command.step_status
            in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
            else None
        )
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                COMMIT_STEP_OUTCOME_SQL,
                (
                    command.step_status.value,
                    output,
                    error,
                    completed,
                    command.committed_at,
                    command.work.step_id,
                    command.work.claimed_by,
                    command.work.lease_generation,
                    command.committed_at,
                ),
            )
            if await _rowcount(cursor) != 1:
                if await self._outcome_already_exists(cursor, command):
                    return
                raise LeaseLostError("step outcome lease lost")
            for next_step in command.next_steps:
                await cursor.execute(
                    CREATE_STEP_SQL,
                    (
                        command.work.run_id,
                        next_step.step_key,
                        next_step.step_type,
                        _json(next_step.input),
                        next_step.max_attempts or 5,
                    ),
                )
            current = (
                command.next_steps[0].step_key
                if command.next_steps
                else (
                    command.work.step_key
                    if command.run_status in {RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL}
                    else None
                )
            )
            await cursor.execute(
                COMMIT_RUN_OUTCOME_SQL,
                (
                    command.run_status.value,
                    current,
                    command.committed_at,
                    command.work.run_id,
                    command.work.run_version,
                ),
            )
            if await _rowcount(cursor) != 1:
                raise RunConflictError("run outcome version conflict")
            event_key = f"step-outcome:{command.work.step_id}:{command.work.lease_generation}"
            commit_hash = _outcome_hash(command)
            await self.enqueue_outbox(
                cursor,
                "agent_run",
                command.work.run_id,
                "agent.step.outcome_committed",
                event_key,
                {
                    "run_id": command.work.run_id,
                    "step_id": command.work.step_id,
                    "run_status": command.run_status.value,
                    "step_status": command.step_status.value,
                    "commit_hash": commit_hash,
                },
            )

    async def schedule_retry(self, command: RetryCommit) -> None:
        error = _json(command.error.as_json())
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                RETRY_STEP_SQL,
                (
                    error,
                    command.next_retry_at,
                    command.committed_at,
                    command.work.step_id,
                    command.work.claimed_by,
                    command.work.lease_generation,
                    command.committed_at,
                ),
            )
            if await _rowcount(cursor) != 1:
                if await self._retry_already_exists(cursor, command):
                    return
                raise LeaseLostError("step retry lease lost")
            await cursor.execute(
                COMMIT_RUN_OUTCOME_SQL,
                (
                    RunStatus.QUEUED.value,
                    command.work.step_key,
                    command.committed_at,
                    command.work.run_id,
                    command.work.run_version,
                ),
            )
            if await _rowcount(cursor) != 1:
                raise RunConflictError("run retry version conflict")
            key = f"step-retry:{command.work.step_id}:{command.work.lease_generation}"
            await self.enqueue_outbox(
                cursor,
                "agent_run",
                command.work.run_id,
                "agent.step.retry_scheduled",
                key,
                {
                    "run_id": command.work.run_id,
                    "step_id": command.work.step_id,
                    "next_retry_at": _timestamp(command.next_retry_at),
                    "error": command.error.as_json(),
                },
            )

    async def request_cancel(self, run_id: str, requested_at: datetime) -> bool:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                REQUEST_CANCEL_SQL,
                (
                    requested_at,
                    requested_at,
                    run_id,
                    run_id,
                    requested_at,
                    requested_at,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return bool(row and row[0])

    async def recover_expired_steps(self, now: datetime) -> tuple[int, int]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(RECOVER_EXPIRED_STEPS_SQL, (now, now, now, now, now, now))
            row = await cursor.fetchone()
            await connection.commit()
        return (int(cast(int, row[0])), int(cast(int, row[1]))) if row else (0, 0)

    async def request_approval(self, command: ApprovalRequest) -> Approval:
        if (
            not command.workspace_id.strip()
            or not command.run_id.strip()
            or not command.step_id.strip()
        ):
            raise ValueError("approval scope is required")
        resources_hash = _resource_hash(command.resources)
        if resources_hash != command.resources_hash:
            raise ValueError("approval resources hash does not match canonical resources")
        async with self._pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                for resource in command.resources:
                    kind = str(resource.get("type", "")).strip()
                    resource_id = str(resource.get("id", "")).strip()
                    access = str(resource.get("access", "")).strip()
                    if (
                        kind not in {"document", "artifact", "task"}
                        or not resource_id
                        or access not in {"read", "write"}
                    ):
                        raise ValueError("approval resource is invalid")
                    await cursor.execute(
                        AUTHORIZE_APPROVAL_RESOURCE_SQL,
                        (
                            kind,
                            resource_id,
                            command.workspace_id,
                            resource_id,
                            command.workspace_id,
                            resource_id,
                            command.workspace_id,
                        ),
                    )
                    resource_row = await cursor.fetchone()
                    if not resource_row or not resource_row[0]:
                        raise PermissionError("approval resource is outside workspace")
                await cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM agent_runs AS run
                        JOIN agent_steps AS step ON step.run_id = run.id
                        WHERE run.id = %s AND step.id = %s AND run.workspace_id = %s
                    )
                    """,
                    (command.run_id, command.step_id, command.workspace_id),
                )
                scope_row = await cursor.fetchone()
                if not scope_row or not scope_row[0]:
                    raise PermissionError("approval target is outside workspace")
                await cursor.execute(
                    CREATE_APPROVAL_SQL,
                    (
                        command.workspace_id,
                        command.run_id,
                        command.step_id,
                        command.tool_name,
                        command.tool_version,
                        command.idempotency_key,
                        _json(list(command.resources)),
                        resources_hash,
                        _json(command.payload),
                        command.reason,
                        command.requested_by_type,
                        command.requested_by_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await cursor.execute(
                        GET_APPROVAL_SQL,
                        (
                            command.workspace_id,
                            command.run_id,
                            command.idempotency_key,
                        ),
                    )
                    row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("approval idempotency lookup returned no row")
                value = _approval(row)
                if (
                    value.step_id != command.step_id
                    or value.tool_name != command.tool_name
                    or value.tool_version != command.tool_version
                    or value.resources_hash != command.resources_hash
                    or value.reason != command.reason
                    or not same_json(value.resources, list(command.resources))
                    or not same_json(value.payload, command.payload)
                ):
                    raise IdempotencyConflictError("approval idempotency conflict")
                event_key = f"tool-approval-requested:{value.id}"
                await self.enqueue_outbox(
                    cursor,
                    "agent_tool_approval",
                    value.id,
                    "agent.tool_approval.requested",
                    event_key,
                    {"approval_id": value.id, "run_id": value.run_id, "tool_name": value.tool_name},
                )
            return value

    async def decide_approval(self, command: ApprovalDecision) -> Approval:
        if command.status not in {"approved", "rejected"}:
            raise ValueError("approval status must be approved or rejected")
        async with self._pool.connection() as connection:
            async with connection.transaction(), connection.cursor() as cursor:
                if command.decided_by_type != "user":
                    raise PermissionError("approval decisions require a trusted user")
                await cursor.execute(
                    AUTHORIZE_APPROVAL_DECISION_SQL,
                    (command.workspace_id, command.decided_by_id),
                )
                allowed = await cursor.fetchone()
                if not allowed or not allowed[0]:
                    raise PermissionError("approval decision requires active owner or admin")
                await cursor.execute(LOCK_APPROVAL_SQL, (command.approval_id, command.workspace_id))
                locked = await cursor.fetchone()
                if locked is None:
                    raise LookupError("approval not found")
                current_status = str(locked[0])
                if current_status != "pending":
                    if (
                        current_status != command.status
                        or str(locked[7]) != command.decided_by_type
                        or str(locked[8]) != command.decided_by_id
                        or str(locked[9]) != command.reason
                    ):
                        raise ApprovalConflictError("approval already has a different decision")
                    return await self._load_approval(
                        cursor, command.workspace_id, str(locked[1]), str(locked[5])
                    )
                await cursor.execute(
                    DECIDE_APPROVAL_SQL,
                    (
                        command.status,
                        command.reason,
                        command.decided_by_type,
                        command.decided_by_id,
                        command.decided_at,
                        command.approval_id,
                        command.workspace_id,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ApprovalConflictError("approval transition conflict")
                approval = _approval(row)
                await cursor.execute(LOCK_WAITING_TARGET_SQL, (str(locked[2]), str(locked[1])))
                target = await cursor.fetchone()
                if target is None:
                    raise LookupError("approval waiting target not found")
                step_status, run_status = str(target[0]), str(target[1])
                if step_status != "waiting_approval" or run_status != "waiting_approval":
                    # Legacy/non-waiting approvals retain their decision fact only.
                    return approval
                if command.status == "rejected":
                    error = _json(
                        {
                            "category": "policy_blocked",
                            "message": "external approval was rejected",
                            "approval_id": approval.id,
                        }
                    )
                    await cursor.execute(
                        FAIL_REJECTED_STEP_SQL,
                        (error, command.decided_at, command.decided_at, approval.step_id),
                    )
                    if await _rowcount(cursor) != 1:
                        raise ApprovalConflictError("approval waiting step transition conflict")
                    await cursor.execute(
                        FAIL_REJECTED_RUN_SQL, (command.decided_at, approval.run_id)
                    )
                    if await _rowcount(cursor) != 1:
                        raise ApprovalConflictError("approval waiting run transition conflict")
                    event_type = "agent.tool_approval.rejected"
                else:
                    try:
                        continuation_json = prepare_approval_continuation(
                            target[2], approval.id, approval.idempotency_key, command.status
                        )
                    except ValueError as error:
                        raise ApprovalConflictError(str(error)) from error
                    step_key = f"commit_patch:approval:{approval.id}"
                    await cursor.execute(
                        CREATE_APPROVED_STEP_SQL,
                        (
                            approval.run_id,
                            step_key,
                            _json(continuation_json),
                        ),
                    )
                    await cursor.execute(GET_STEP_INPUT_SQL, (approval.run_id, step_key))
                    continuation_row = await cursor.fetchone()
                    if (
                        continuation_row is None
                        or str(continuation_row[0]) != "CommitPatch"
                        or not same_json(continuation_row[1], continuation_json)
                    ):
                        raise ApprovalConflictError("approved continuation idempotency conflict")
                    await cursor.execute(
                        SUCCEED_APPROVAL_STEP_SQL,
                        (command.decided_at, command.decided_at, approval.step_id),
                    )
                    if await _rowcount(cursor) != 1:
                        raise ApprovalConflictError("approval waiting step transition conflict")
                    await cursor.execute(
                        QUEUE_APPROVAL_RUN_SQL,
                        (step_key, command.decided_at, approval.run_id),
                    )
                    if await _rowcount(cursor) != 1:
                        raise ApprovalConflictError("approval waiting run transition conflict")
                    event_type = "agent.tool_approval.approved"
                event_key = f"tool-approval-decided:{approval.id}"
                await self.enqueue_outbox(
                    cursor,
                    "agent_tool_approval",
                    approval.id,
                    event_type,
                    event_key,
                    {
                        "approval_id": approval.id,
                        "run_id": approval.run_id,
                        "tool_name": approval.tool_name,
                        "status": command.status,
                    },
                )
            return approval

    @staticmethod
    async def _load_approval(
        cursor: AsyncCursor, workspace_id: str, run_id: str, key: str
    ) -> Approval:
        await cursor.execute(GET_APPROVAL_SQL, (workspace_id, run_id, key))
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("approval replay lookup returned no row")
        return _approval(row)

    async def create_context_manifest(
        self,
        run_id: str,
        step_id: str,
        token_budget: int,
        reserved_output_tokens: int,
        tokenizer: str,
        items: list[JSONObject],
        total_tokens: int,
        content_hash: str,
    ) -> ContextManifest:
        if (
            not run_id.strip()
            or not step_id.strip()
            or not tokenizer.strip()
            or not content_hash.strip()
            or not content_hash.startswith("sha256:")
            or len(content_hash) != 71
            or token_budget <= 0
            or reserved_output_tokens < 0
            or total_tokens < 0
            or total_tokens + reserved_output_tokens > token_budget
        ):
            raise ValueError("invalid context token budget")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                CREATE_CONTEXT_MANIFEST_SQL,
                (
                    run_id,
                    step_id,
                    token_budget,
                    reserved_output_tokens,
                    tokenizer,
                    _json(items),
                    total_tokens,
                    content_hash,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("context manifest insert returned no row")
        return _manifest(row)

    async def get_context_manifest(self, manifest_id: str) -> ContextManifest | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_CONTEXT_MANIFEST_SQL, (manifest_id,))
            row = await cursor.fetchone()
        return None if row is None else _manifest(row)

    async def enqueue_outbox(
        self,
        cursor: AsyncCursor,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        idempotency_key: str,
        payload: JSONObject,
        next_attempt_at: datetime | None = None,
    ) -> Outbox:
        await cursor.execute(
            ENQUEUE_OUTBOX_SQL,
            (
                aggregate_type,
                aggregate_id,
                event_type,
                idempotency_key,
                _json(payload),
                next_attempt_at,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            await cursor.execute(
                GET_OUTBOX_BY_KEY_SQL, (aggregate_type, aggregate_id, idempotency_key)
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("outbox idempotency lookup returned no row")
        value = _outbox(row)
        if value.event_type != event_type or not same_json(value.payload, payload):
            raise IdempotencyConflictError("outbox idempotency conflict")
        return value

    async def claim_outbox(
        self,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int,
        event_types: Sequence[str] = (),
    ) -> list[Outbox]:
        if not 0 < limit <= 1000:
            raise ValueError("outbox limit must be 1..1000")
        types = list(dict.fromkeys(item.strip() for item in event_types if item.strip()))
        type_arg: list[str] | None = types or None
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                CLAIM_OUTBOX_SQL,
                (
                    now,
                    type_arg,
                    type_arg,
                    limit,
                    worker_id,
                    now + lease_duration,
                ),
            )
            rows = await cursor.fetchall()
            await connection.commit()
        return [_outbox(row) for row in rows]

    async def mark_outbox_published(self, event: Outbox, published_at: datetime) -> None:
        await self._execute_lease_free(
            MARK_OUTBOX_PUBLISHED_SQL,
            (
                published_at,
                event.id,
                event.claimed_by,
                event.lease_generation,
                published_at,
            ),
            "outbox",
        )

    async def schedule_outbox_retry(
        self,
        event: Outbox,
        error: JSONObject,
        next_attempt_at: datetime,
        now: datetime,
        dead_letter: bool,
    ) -> None:
        status = OutboxStatus.DEAD_LETTER.value if dead_letter else OutboxStatus.PENDING.value
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                RETRY_OUTBOX_SQL,
                (
                    status,
                    _json(error),
                    None if dead_letter else next_attempt_at,
                    event.id,
                    event.claimed_by,
                    event.lease_generation,
                    now,
                ),
            )
            if await _rowcount(cursor) != 1:
                raise LeaseLostError("outbox lease lost")
            await connection.commit()

    async def recover_expired_outbox(self, now: datetime) -> int:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(RECOVER_OUTBOX_SQL, (now, now))
            count = await _rowcount(cursor)
            await connection.commit()
        return count

    async def begin_tool(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        input: JSONObject,
        idempotency_key: str | None,
        worker_id: str,
        started_at: datetime,
        lease_duration: timedelta,
    ) -> tuple[Tool, bool]:
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                BEGIN_TOOL_SQL,
                (
                    run_id,
                    step_id,
                    tool_name,
                    tool_version,
                    _json(input),
                    idempotency_key,
                    started_at,
                    worker_id,
                    started_at + lease_duration,
                ),
            )
            row = await cursor.fetchone()
            if row is not None:
                return _tool(row), True
            if not idempotency_key:
                raise IdempotencyConflictError("tool insert conflicted without idempotency key")
            await cursor.execute(LOCK_TOOL_BY_KEY_SQL, (run_id, idempotency_key))
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("tool idempotency lookup returned no row")
            value = _tool(row)
            if (
                value.step_id != step_id
                or value.tool_name != tool_name
                or value.tool_version != tool_version
                or not same_json(value.input, input)
            ):
                raise IdempotencyConflictError("tool idempotency conflict")
            if value.status in {ToolStatus.PENDING, ToolStatus.RUNNING} and (
                value.status == ToolStatus.PENDING
                or value.lease_expires_at is None
                or value.lease_expires_at <= started_at
            ):
                await cursor.execute(
                    RECLAIM_TOOL_SQL,
                    (
                        worker_id,
                        started_at + lease_duration,
                        started_at,
                        value.id,
                        started_at,
                    ),
                )
                reclaimed = await cursor.fetchone()
                if reclaimed is None:
                    raise LeaseLostError("tool reclaim lost")
                return _tool(reclaimed), True
            return value, False

    async def finish_tool(
        self,
        tool: Tool,
        status: ToolStatus,
        output: JSONObject | None,
        error: JSONObject | None,
        error_category: str | None,
        latency_ms: int,
        completed_at: datetime,
        attempts: int = 1,
    ) -> None:
        if latency_ms < 0 or attempts < 0:
            raise ValueError("tool attempts and latency must be nonnegative")
        if status is ToolStatus.SUCCEEDED:
            if output is None or error is not None or error_category is not None:
                raise ValueError("successful tool outcome requires output only")
        elif status in {ToolStatus.FAILED, ToolStatus.CANCELLED}:
            if output is not None or error is None or not error_category:
                raise ValueError("failed tool outcome requires classified error only")
        else:
            raise ValueError("tool outcome must be terminal")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(
                FINISH_TOOL_SQL,
                (
                    status.value,
                    None if output is None else _json(output),
                    None if error is None else _json(error),
                    error_category,
                    latency_ms,
                    completed_at,
                    attempts,
                    tool.id,
                    tool.claimed_by,
                    tool.lease_generation,
                    completed_at,
                ),
            )
            if await _rowcount(cursor) != 1:
                raise LeaseLostError("tool lease lost")
            await connection.commit()

    async def _execute_lease_free(self, query: str, params: Sequence[object], label: str) -> None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(query, params)
            if await _rowcount(cursor) != 1:
                raise LeaseLostError(f"{label} update did not match one fact")
            await connection.commit()

    async def _outcome_already_exists(self, cursor: AsyncCursor, command: OutcomeCommit) -> bool:
        key = f"step-outcome:{command.work.step_id}:{command.work.lease_generation}"
        await cursor.execute(GET_OUTBOX_BY_KEY_SQL, ("agent_run", command.work.run_id, key))
        row = await cursor.fetchone()
        if row is None:
            return False
        event = _outbox(row)
        expected = _outcome_hash(command)
        return (
            event.event_type == "agent.step.outcome_committed"
            and event.payload.get("commit_hash") == expected
        )

    async def _retry_already_exists(self, cursor: AsyncCursor, command: RetryCommit) -> bool:
        key = f"step-retry:{command.work.step_id}:{command.work.lease_generation}"
        await cursor.execute(GET_OUTBOX_BY_KEY_SQL, ("agent_run", command.work.run_id, key))
        row = await cursor.fetchone()
        if row is None:
            return False
        event = _outbox(row)
        expected = {
            "run_id": command.work.run_id,
            "step_id": command.work.step_id,
            "next_retry_at": _timestamp(command.next_retry_at),
            "error": command.error.as_json(),
        }
        return event.event_type == "agent.step.retry_scheduled" and same_json(
            event.payload, expected
        )

    @staticmethod
    def _validate_create_run(command: CreateRun) -> None:
        if (
            not command.workspace_id.strip()
            or not command.request_id.strip()
            or not command.resource_id.strip()
        ):
            raise ValueError("trusted run scope and request id are required")
        if not command.objective.strip() or command.max_steps < 1 or command.max_tool_calls < 0:
            raise ValueError("run objective and limits are invalid")
        if command.token_budget is not None and command.token_budget <= 0:
            raise ValueError("token_budget must be positive")
        if command.cost_budget is not None and command.cost_budget < 0:
            raise ValueError("cost_budget must be nonnegative")


async def _rowcount(cursor: AsyncCursor) -> int:
    # Psycopg exposes rowcount; the injected test cursor may expose it as a
    # synchronous integer. No fallback state is used as an authority.
    value = getattr(cursor, "rowcount", -1)
    return int(value)


__all__ = ["RuntimeRepository", "prepare_approval_continuation"]
