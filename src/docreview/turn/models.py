"""Transport-neutral durable Turn facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TurnStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def deterministic(self) -> bool:
        return self in {
            TurnStatus.WAITING_INPUT,
            TurnStatus.WAITING_APPROVAL,
            TurnStatus.SUCCEEDED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class TurnRequest:
    request_id: str
    trace_id: str
    organization_id: str
    workspace_id: str
    resource_id: str
    session_id: str | None
    message: str
    principal_type: str
    principal_id: str
    trust_source: str
    runtime_mode: str = "durable"


@dataclass(frozen=True, slots=True)
class AcceptTurn:
    request: TurnRequest
    idempotency_scope: str
    input: dict[str, Any]
    input_json: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    session_id: str
    run_id: str
    request_id: str
    status: TurnStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnEvent:
    id: str
    turn_id: str
    sequence: int
    type: str
    payload: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn: Turn
    created: bool
    events: tuple[TurnEvent, ...]


__all__ = [
    "AcceptTurn",
    "Turn",
    "TurnEvent",
    "TurnRequest",
    "TurnResult",
    "TurnStatus",
]
