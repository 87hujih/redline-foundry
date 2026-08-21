"""稳定的持久化 Runtime 失败分类与 repository 错误。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    RETRYABLE_UPSTREAM = "retryable_upstream"
    TERMINAL_UPSTREAM = "terminal_upstream"
    POLICY_BLOCKED = "policy_blocked"
    CANCELLED = "cancelled"
    LEASE_EXPIRED = "lease_expired"

    @property
    def retryable(self) -> bool:
        return self in {
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.TIMEOUT,
            ErrorCategory.RETRYABLE_UPSTREAM,
            ErrorCategory.LEASE_EXPIRED,
        }


class TimeoutScope(StrEnum):
    ATTEMPT = "attempt"
    STEP = "step"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    category: ErrorCategory
    message: str
    timeout_scope: TimeoutScope | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("执行 失败 消息 为必填项")

    def as_json(self) -> dict[str, str]:
        value = {"category": self.category.value, "message": self.message}
        if self.timeout_scope is not None:
            value["timeout_scope"] = self.timeout_scope.value
        return value


class DurableRuntimeError(RuntimeError):
    pass


class LeaseLostError(DurableRuntimeError):
    pass


class IdempotencyConflictError(DurableRuntimeError):
    pass


class RunConflictError(DurableRuntimeError):
    pass


class ApprovalConflictError(DurableRuntimeError):
    pass


__all__ = [
    "ApprovalConflictError",
    "DurableRuntimeError",
    "ErrorCategory",
    "ExecutionFailure",
    "IdempotencyConflictError",
    "LeaseLostError",
    "RunConflictError",
    "TimeoutScope",
]
