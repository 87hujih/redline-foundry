"""Runtime 与 HTTP 适配器共享的 Approval 事实和命令。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Principal:
    type: str
    id: str

    def __post_init__(self) -> None:
        if (
            self.type not in {"user", "service"}
            or not self.id.strip()
            or self.id != self.id.strip()
        ):
            raise ValueError("approval principal is invalid")


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    patch_id: str
    patch_hash: str
    tool_name: str
    tool_version: str
    input_hash: str
    idempotency_key: str
    target_version_id: str

    def __post_init__(self) -> None:
        values = (
            self.workspace_id,
            self.run_id,
            self.step_id,
            self.resource_id,
            self.patch_id,
            self.tool_name,
            self.tool_version,
            self.idempotency_key,
            self.target_version_id,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("审批 绑定 不完整")
        if _HASH.fullmatch(self.patch_hash) is None or _HASH.fullmatch(self.input_hash) is None:
            raise ValueError("审批 绑定 哈希 无效")

    def as_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "resource_id": self.resource_id,
            "patch_id": self.patch_id,
            "patch_hash": self.patch_hash,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "input_hash": self.input_hash,
            "idempotency_key": self.idempotency_key,
            "target_version_id": self.target_version_id,
        }


@dataclass(frozen=True, slots=True)
class ApprovalCreateCommand:
    binding: ApprovalBinding | Mapping[str, str]
    reason: str
    payload: dict[str, Any]
    requested_by: Principal
    source: str
    required_role: str = "owner_or_admin"

    def __post_init__(self) -> None:
        if isinstance(self.binding, Mapping):
            object.__setattr__(self, "binding", ApprovalBinding(**dict(self.binding)))
        if not isinstance(self.binding, ApprovalBinding):
            raise ValueError("审批 绑定 为必填项")
        if not self.reason.strip() or self.reason != self.reason.strip():
            raise ValueError("approval reason is required")
        if self.source != "tool_runtime":
            raise ValueError("only ToolRuntime may create approvals")
        if self.required_role != "owner_or_admin":
            raise ValueError("审批所需角色无效")


@dataclass(frozen=True, slots=True)
class ApprovalDecisionCommand:
    approval_id: str
    workspace_id: str
    decided_by: Principal
    status: str
    reason: str
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.approval_id.strip() or not self.workspace_id.strip():
            raise ValueError("审批 决定 范围 为必填项")
        if self.status not in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}:
            raise ValueError("审批 决定 状态 无效")
        if not self.reason.strip() or self.reason != self.reason.strip():
            raise ValueError("approval decision reason is required")


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    patch_id: str
    patch_hash: str
    tool_name: str
    tool_version: str
    input_hash: str
    requested_by: Principal
    required_role: str
    status: str
    decision: str | None
    decision_reason: str | None
    decided_by: Principal | None
    created_at: datetime
    decided_at: datetime | None
    idempotency_key: str
    continuation_step_id: str | None
    payload: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    reason: str = ""

    @property
    def id(self) -> str:
        return self.approval_id

    @property
    def binding(self) -> ApprovalBinding:
        return ApprovalBinding(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            step_id=self.step_id,
            resource_id=self.resource_id,
            patch_id=self.patch_id,
            patch_hash=self.patch_hash,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            input_hash=self.input_hash,
            idempotency_key=self.idempotency_key,
            target_version_id=cast(str, self.payload["target_version_id"]),
        )


@dataclass(frozen=True, slots=True)
class RunFact:
    id: str
    workspace_id: str
    resource_id: str
    status: str
    current_step: str | None = None


@dataclass(frozen=True, slots=True)
class StepFact:
    id: str
    run_id: str
    status: str
    binding: Mapping[str, str]
    continuation: dict[str, Any]
    step_type: str = "RequestApproval"
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ResourceFact:
    id: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class PatchFact:
    id: str
    resource_id: str
    patch_hash: str
    target_version_id: str


@dataclass(frozen=True, slots=True)
class MembershipFact:
    workspace_id: str
    user_id: str
    role: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class OutboxFact:
    aggregate_type: str
    aggregate_id: str
    event_type: str
    idempotency_key: str
    payload: dict[str, Any]


__all__ = [
    "Approval",
    "ApprovalBinding",
    "ApprovalCreateCommand",
    "ApprovalDecisionCommand",
    "ApprovalStatus",
    "MembershipFact",
    "OutboxFact",
    "PatchFact",
    "Principal",
    "ResourceFact",
    "RunFact",
    "StepFact",
]
