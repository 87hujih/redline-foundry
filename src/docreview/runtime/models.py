"""Typed representations of durable runtime business facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from docreview.runtime.errors import ExecutionFailure

JSONObject = dict[str, Any]


def empty_object() -> JSONObject:
    return {}


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class Outcome(StrEnum):
    CONTINUE = "continue"
    SUCCEED = "succeed"
    WAIT_INPUT = "wait_input"
    WAIT_APPROVAL = "wait_approval"


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    organization_id: str | None
    workspace_id: str | None
    session_id: str | None
    request_id: str | None
    trace_id: str | None
    status: RunStatus
    objective: str
    current_step: str | None
    max_steps: int
    max_tool_calls: int
    token_budget: int | None
    cost_budget: float | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    state: JSONObject
    version: int
    created_at: datetime
    updated_at: datetime
    resource_id: str | None
    principal_type: str | None
    principal_id: str | None
    trust_source: str | None
    runtime_mode: str | None


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    run_id: str
    step_key: str
    step_type: str
    status: StepStatus
    input: JSONObject
    output: JSONObject | None
    error: JSONObject | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    lease_generation: int
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Attempt:
    id: str
    step_id: str
    attempt_number: int
    provider: str | None
    model: str | None
    prompt_version: str | None
    temperature: float | None
    context_manifest_id: str | None
    trace_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None
    latency_ms: int | None
    retry_count: int
    finish_reason: str | None
    error_category: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ContextManifest:
    id: str
    run_id: str
    step_id: str
    token_budget: int
    reserved_output_tokens: int
    tokenizer: str
    items: list[JSONObject]
    total_tokens: int
    content_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Tool:
    id: str
    run_id: str
    step_id: str
    tool_name: str
    tool_version: str
    input: JSONObject
    output: JSONObject | None
    status: ToolStatus
    idempotency_key: str | None
    error: JSONObject | None
    error_category: str | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Outbox:
    id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    idempotency_key: str
    payload: JSONObject
    status: OutboxStatus
    attempt_count: int
    next_attempt_at: datetime | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    error: JSONObject | None
    created_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    workspace_id: str
    run_id: str
    step_id: str
    tool_name: str
    tool_version: str
    idempotency_key: str
    resources: list[JSONObject]
    resources_hash: str
    payload: JSONObject
    reason: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StepSpec:
    step_key: str
    step_type: str
    input: JSONObject = field(default_factory=empty_object)
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class WorkItem:
    run_id: str
    run_version: int
    run_deadline_at: datetime | None
    cancel_requested_at: datetime | None
    step_id: str
    step_key: str
    step_type: str
    input: JSONObject
    attempt_number: int
    max_attempts: int
    lease_generation: int
    claimed_by: str
    step_started_at: datetime | None
    max_steps: int
    step_count: int
    max_tool_calls: int
    tool_call_count: int
    token_budget: int | None
    tokens_used: int
    cost_budget: float | None
    cost_used: float

    @property
    def stable_idempotency_key(self) -> str:
        return f"agent-step:{self.step_id}"


@dataclass(frozen=True, slots=True)
class ExecutionInput:
    run_id: str
    step_id: str
    trace_id: str
    step_key: str
    step_type: str
    input: JSONObject
    attempt_number: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: Outcome = Outcome.SUCCEED
    output: JSONObject = field(default_factory=empty_object)
    next_steps: tuple[StepSpec, ...] = ()
    error: ExecutionFailure | None = None
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    temperature: float | None = None
    context_manifest_id: str = ""
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    finish_reason: str = ""


__all__ = [
    "Approval",
    "Attempt",
    "ContextManifest",
    "ExecutionInput",
    "ExecutionResult",
    "JSONObject",
    "Outbox",
    "OutboxStatus",
    "Outcome",
    "Run",
    "RunStatus",
    "Step",
    "StepSpec",
    "StepStatus",
    "Tool",
    "ToolStatus",
    "WorkItem",
]
