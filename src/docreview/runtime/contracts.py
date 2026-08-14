"""Commands crossing the durable runtime/repository boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from docreview.runtime.errors import ExecutionFailure
from docreview.runtime.models import (
    JSONObject,
    RunStatus,
    StepSpec,
    StepStatus,
    WorkItem,
    empty_object,
)


@dataclass(frozen=True, slots=True)
class CreateRun:
    workspace_id: str
    request_id: str
    objective: str
    resource_id: str
    principal_type: str
    principal_id: str
    trust_source: str
    organization_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    max_steps: int = 64
    max_tool_calls: int = 32
    token_budget: int | None = None
    cost_budget: float | None = None
    deadline_at: datetime | None = None
    state: JSONObject = field(default_factory=empty_object)


@dataclass(frozen=True, slots=True)
class CreateStep:
    step_key: str
    step_type: str
    input: JSONObject = field(default_factory=empty_object)
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class AttemptFinish:
    attempt_id: str
    provider: str
    model: str
    prompt_version: str
    temperature: float | None
    context_manifest_id: str
    retry_count: int
    input_tokens: int
    output_tokens: int
    cost: float
    latency_ms: int
    finish_reason: str
    error_category: str | None
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class OutcomeCommit:
    work: WorkItem
    step_status: StepStatus
    run_status: RunStatus
    output: JSONObject
    error: ExecutionFailure | None
    next_steps: tuple[StepSpec, ...]
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class RetryCommit:
    work: WorkItem
    error: ExecutionFailure
    next_retry_at: datetime
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class EngineConfig:
    worker_id: str
    lease_duration: timedelta
    heartbeat_interval: timedelta
    attempt_timeout: timedelta
    step_timeout: timedelta
    retry_base: timedelta
    retry_max: timedelta


@dataclass(frozen=True, slots=True)
class ProjectionWorkerConfig:
    worker_id: str
    lease_duration: timedelta
    batch_size: int
    max_attempts: int
    retry_base: timedelta
    retry_max: timedelta
    event_types: tuple[str, ...] = (
        "agent.step.outcome_committed",
        "agent.tool_approval.rejected",
    )


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    workspace_id: str
    run_id: str
    step_id: str
    tool_name: str
    tool_version: str
    idempotency_key: str
    resources: tuple[JSONObject, ...]
    resources_hash: str
    payload: JSONObject
    reason: str
    requested_by_type: str
    requested_by_id: str


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approval_id: str
    workspace_id: str
    status: str
    reason: str
    decided_by_type: str
    decided_by_id: str
    decided_at: datetime


__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "AttemptFinish",
    "CreateRun",
    "CreateStep",
    "EngineConfig",
    "OutcomeCommit",
    "ProjectionWorkerConfig",
    "RetryCommit",
]
