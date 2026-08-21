"""生产 Tool 执行使用的严格公开值对象。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from docreview.tool_runtime.schema import JSONObject

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_TOOL_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True, slots=True)
class ToolName:
    value: str

    def __post_init__(self) -> None:
        if self.value != self.value.strip() or not _TOOL_NAME.fullmatch(self.value):
            raise ValueError("tool name is invalid")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ToolVersion:
    value: str

    def __post_init__(self) -> None:
        if self.value != self.value.strip() or not _TOOL_VERSION.fullmatch(self.value):
            raise ValueError("tool version is invalid")

    def __str__(self) -> str:
        return self.value


class ToolRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolErrorCategory(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    APPROVAL_REQUIRED = "approval_required"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RETRYABLE_UPSTREAM = "retryable_upstream"
    INVALID_OUTPUT = "invalid_output"
    PERMANENT_FAILURE = "permanent_failure"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"

    @property
    def retryable(self) -> bool:
        return self in {
            ToolErrorCategory.RATE_LIMITED,
            ToolErrorCategory.TIMEOUT,
            ToolErrorCategory.RETRYABLE_UPSTREAM,
        }


class AuditStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Principal:
    type: str
    id: str

    def __post_init__(self) -> None:
        if self.type not in {"user", "service"}:
            raise ValueError("主体 类型 无效")
        if not self.id.strip() or self.id != self.id.strip():
            raise ValueError("主体 id 为必填项")


@dataclass(frozen=True, slots=True)
class ToolIntent:
    name: ToolName
    version: ToolVersion
    raw_input: str | bytes
    idempotency_key: str = ""
    approval_id: str | None = None
    patch_hash: str | None = None

    def __post_init__(self) -> None:
        if type(self.raw_input) not in {str, bytes}:
            raise TypeError("工具 输入 必须是 JSON 文本 或 字节")
        for value, label in (
            (self.idempotency_key, "idempotency key"),
            (self.approval_id, "approval id"),
            (self.patch_hash, "patch hash"),
        ):
            if value is not None and value != value.strip():
                raise ValueError(f"工具{label}必须是 规范")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    request_id: str
    run_id: str
    step_id: str
    workspace_id: str
    resource_id: str
    principal: Principal
    roles: tuple[str, ...]
    trace_id: str
    attempt: int
    deadline: datetime

    def __post_init__(self) -> None:
        values = (
            self.request_id,
            self.run_id,
            self.step_id,
            self.workspace_id,
            self.resource_id,
            self.trace_id,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("可信 工具 执行 上下文 不完整")
        if not self.roles or any(not role.strip() or role != role.strip() for role in self.roles):
            raise ValueError("可信 工具 角色 为 不完整")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("可信 工具 角色 必须是 唯一")
        if self.attempt < 1:
            raise ValueError("工具 尝试 必须为正数")
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("工具 截止时间 必须包含时区信息")


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    definition: ToolDefinition
    context: ToolExecutionContext
    tool_input: JSONObject
    input_hash: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason_code: str

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or self.reason_code != self.reason_code.strip():
            raise ValueError("策略 原因 代码 为必填项")


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    idempotency_key: str
    input_hash: str
    patch_hash: str

    def __post_init__(self) -> None:
        identifiers = (
            self.workspace_id,
            self.run_id,
            self.step_id,
            self.resource_id,
            self.idempotency_key,
        )
        if any(not value.strip() or value != value.strip() for value in identifiers):
            raise ValueError("审批 要求 不完整")
        for value in (self.input_hash, self.patch_hash):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise ValueError("审批 哈希 绑定 无效")


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    approval_id: str
    status: str
    requirement: ApprovalRequirement

    def __post_init__(self) -> None:
        if not self.approval_id.strip() or self.approval_id != self.approval_id.strip():
            raise ValueError("审批 id 为必填项")
        if self.status not in {"approved", "rejected", "cancelled", "pending"}:
            raise ValueError("审批 状态 无效")


@dataclass(frozen=True, slots=True)
class RateLimitRequest:
    definition: ToolDefinition
    context: ToolExecutionContext
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.retry_after < timedelta(0):
            raise ValueError("速率 限制 重试 延迟 不能为负数")


@dataclass(frozen=True, slots=True)
class ToolError:
    category: ToolErrorCategory
    message: str
    details: dict[str, str | int] | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("工具 错误 消息 为必填项")


@dataclass(frozen=True, slots=True)
class Provenance:
    source_type: str
    source_id: str
    trust_level: str
    resource_id: str | None = None
    version_id: str | None = None
    content_hash: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip() or value != value.strip()
            for value in (self.source_type, self.source_id)
        ):
            raise ValueError("工具 来源信息 来源 为必填项")
        if self.trust_level not in {"trusted", "untrusted"}:
            raise ValueError("工具 来源信息 信任级别 无效")
        if (
            self.content_hash is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash) is None
        ):
            raise ValueError("工具 来源信息 内容 哈希 无效")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    uri: str
    content_hash: str
    size_bytes: int
    workspace_id: str
    run_id: str
    step_id: str
    tool_name: ToolName
    tool_version: ToolVersion

    def __post_init__(self) -> None:
        values = (
            self.artifact_id,
            self.uri,
            self.workspace_id,
            self.run_id,
            self.step_id,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("制品 引用 不完整")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash) is None:
            raise ValueError("制品 内容 哈希 无效")
        if self.size_bytes < 0:
            raise ValueError("制品 大小 不能为负数")


@dataclass(frozen=True, slots=True)
class ToolResult:
    output: JSONObject
    provenance: tuple[Provenance, ...]
    artifact: ArtifactReference | None = None
    oversize_summary: JSONObject | None = None

    def __post_init__(self) -> None:
        if not self.provenance:
            raise ValueError("工具 结果 来源信息 为必填项")


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: str | None
    status: AuditStatus
    result: ToolResult | None = None
    error: ToolError | None = None
    approval_id: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: ToolName
    version: ToolVersion
    description: str
    input_schema: str
    output_schema: str
    risk_level: ToolRiskLevel
    timeout: timedelta
    requires_resource: bool
    requires_approval: bool
    max_inline_output_bytes: int
    backend: object
    resource_input_field: str = "resource_id"
    max_attempts: int = 1
    retry_backoff: timedelta = timedelta(0)
    side_effecting: bool = False
    max_summary_bytes: int = 4_096
    required_permissions: tuple[str, ...] = ()
    resource_type: str = ""
    resource_access: str = "read"
    max_result_tokens: int | None = None
    data_classification: str = "internal"

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("工具 描述 为必填项")
        if self.timeout <= timedelta(0) or self.timeout > timedelta(minutes=10):
            raise ValueError("工具 超时时间 无效")
        if self.max_inline_output_bytes <= 0:
            raise ValueError("工具 输出 限制 无效")
        if self.max_result_tokens is not None and self.max_result_tokens <= 0:
            raise ValueError("工具 令牌 输出 限制 无效")
        if (
            not self.resource_input_field.strip()
            or self.resource_input_field != self.resource_input_field.strip()
        ):
            raise ValueError("工具 资源 输入 字段 无效")
        if self.backend is None:
            raise ValueError("工具 后端 为必填项")
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError("工具 重试 数量 无效")
        if self.retry_backoff < timedelta(0) or (
            self.max_attempts > 1 and self.retry_backoff <= timedelta(0)
        ):
            raise ValueError("工具 重试 退避时间 无效")
        if self.max_summary_bytes < 128 or self.max_summary_bytes > 64 * 1_024:
            raise ValueError("工具 摘要 限制 无效")
        if any(
            not permission.strip() or permission != permission.strip()
            for permission in self.required_permissions
        ) or len(self.required_permissions) != len(set(self.required_permissions)):
            raise ValueError("工具 权限 无效")
        if self.resource_access not in {"read", "write"}:
            raise ValueError("工具 资源 访问 无效")
        if self.data_classification not in {
            "public",
            "internal",
            "confidential",
            "restricted",
        }:
            raise ValueError("工具 数据 分类 无效")
        if (
            self.risk_level in {ToolRiskLevel.HIGH, ToolRiskLevel.CRITICAL}
            and not self.requires_approval
        ):
            raise ValueError("高风险 工具 需要 审批")


@dataclass(frozen=True, slots=True)
class BackendRequest:
    definition: ToolDefinition
    context: ToolExecutionContext
    tool_input: JSONObject
    input_hash: str
    idempotency_key: str
    backend_attempt: int
    recovering: bool


@dataclass(frozen=True, slots=True)
class ArtifactWriteRequest:
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    idempotency_key: str
    content: bytes
    content_hash: str
    provenance: tuple[Provenance, ...]
    metadata: JSONObject | None = None


@dataclass(frozen=True, slots=True)
class AuditClaimRequest:
    run_id: str
    step_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    idempotency_key: str
    tool_input: JSONObject
    input_hash: str
    attempt: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class AuditClaim:
    call_id: str
    acquired: bool
    recovered: bool
    status: AuditStatus
    result: ToolResult | None = None
    error: ToolError | None = None
    attempts: int = 0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("审计 调用 id 为必填项")
        if self.acquired and self.status is not AuditStatus.RUNNING:
            raise ValueError("已获取 审计 领取记录 必须是 运行中")
        if self.recovered and not self.acquired:
            raise ValueError("只有已获取的审计领取记录才能恢复")
        if self.status is AuditStatus.SUCCEEDED and self.result is None:
            raise ValueError("成功 审计 领取记录 需要 结果")
        if self.status in {AuditStatus.FAILED, AuditStatus.CANCELLED} and self.error is None:
            raise ValueError("失败 审计 领取记录 需要 错误")


@dataclass(frozen=True, slots=True)
class AuditFinishRequest:
    call_id: str
    status: AuditStatus
    result: ToolResult | None
    error: ToolError | None
    attempt: int
    backend_attempts: int
    latency_ms: int
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.status is AuditStatus.SUCCEEDED and (self.result is None or self.error is not None):
            raise ValueError("成功 审计 结果 仅需要 结果")
        if self.status in {AuditStatus.FAILED, AuditStatus.CANCELLED} and self.error is None:
            raise ValueError("失败 审计 结果 需要 错误")
        if self.attempt < 1 or self.backend_attempts < 0 or self.latency_ms < 0:
            raise ValueError("审计 结果 遥测信息 无效")

    @property
    def artifact_reference(self) -> ArtifactReference | None:
        return self.result.artifact if self.result is not None else None


class IdempotencyConflictError(RuntimeError):
    pass


class ToolBackendFailure(RuntimeError):
    def __init__(self, category: ToolErrorCategory, safe_message: str) -> None:
        if category not in {
            ToolErrorCategory.INVALID_INPUT,
            ToolErrorCategory.UNAUTHORIZED,
            ToolErrorCategory.NOT_FOUND,
            ToolErrorCategory.RETRYABLE_UPSTREAM,
            ToolErrorCategory.PERMANENT_FAILURE,
            ToolErrorCategory.IDEMPOTENCY_CONFLICT,
            ToolErrorCategory.CANCELLED,
        }:
            raise ValueError("后端 失败 类别 无效")
        if not safe_message.strip():
            raise ValueError("后端 失败 安全 消息 为必填项")
        super().__init__(safe_message)
        self.category = category
        self.safe_message = safe_message


__all__ = [
    "ApprovalGrant",
    "ApprovalRequirement",
    "ArtifactReference",
    "ArtifactWriteRequest",
    "AuditClaim",
    "AuditClaimRequest",
    "AuditFinishRequest",
    "AuditStatus",
    "BackendRequest",
    "IdempotencyConflictError",
    "PolicyDecision",
    "PolicyRequest",
    "Principal",
    "Provenance",
    "RateLimitDecision",
    "RateLimitRequest",
    "ToolBackendFailure",
    "ToolDefinition",
    "ToolError",
    "ToolErrorCategory",
    "ToolExecutionContext",
    "ToolIntent",
    "ToolName",
    "ToolObservation",
    "ToolResult",
    "ToolRiskLevel",
    "ToolVersion",
]
