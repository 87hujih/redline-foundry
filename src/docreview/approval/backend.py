"""持久化 Approval 事务的无数据库后端。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from docreview.approval.models import (
    Approval,
    ApprovalBinding,
    ApprovalCreateCommand,
    ApprovalDecisionCommand,
    ApprovalStatus,
    MembershipFact,
    OutboxFact,
    PatchFact,
    Principal,
    ResourceFact,
    RunFact,
    StepFact,
)
from docreview.runtime.contracts import ApprovalDecision

if TYPE_CHECKING:
    from docreview.tool_runtime.models import ApprovalGrant


class ApprovalConflictError(RuntimeError):
    pass


class ApprovalValidationError(ValueError):
    pass


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    def __init__(self, repository: InMemoryApprovalRepository) -> None:
        self._repository = repository
        self._snapshot: (
            tuple[
                dict[str, Approval],
                dict[tuple[str, str, str], str],
                dict[str, RunFact],
                dict[str, StepFact],
                list[OutboxFact],
            ]
            | None
        ) = None

    async def __aenter__(self) -> _Transaction:
        await self._repository.lock.acquire()
        self._snapshot = (
            deepcopy(self._repository.approvals),
            deepcopy(self._repository.approval_keys),
            deepcopy(self._repository.runs),
            deepcopy(self._repository.steps),
            deepcopy(self._repository.outbox),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None and self._snapshot is not None:
            (
                self._repository.approvals,
                self._repository.approval_keys,
                self._repository.runs,
                self._repository.steps,
                self._repository.outbox,
            ) = self._snapshot
        self._repository.lock.release()


class InMemoryApprovalRepository:
    """离线测试用 fake repository, 使用单事务锁和回滚快照。"""

    def __init__(self, *, fail_outbox: bool = False) -> None:
        self.lock = asyncio.Lock()
        self.approvals: dict[str, Approval] = {}
        self.approval_keys: dict[tuple[str, str, str], str] = {}
        self.runs: dict[str, RunFact] = {}
        self.steps: dict[str, StepFact] = {}
        self.resources: dict[str, ResourceFact] = {}
        self.patches: dict[str, PatchFact] = {}
        self.memberships: dict[tuple[str, str], MembershipFact] = {}
        self.outbox: list[OutboxFact] = []
        self.fail_outbox = fail_outbox

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def add_run(self, value: RunFact) -> None:
        self.runs[value.id] = value

    def add_step(self, value: StepFact) -> None:
        self.steps[value.id] = value

    def add_resource(self, value: ResourceFact) -> None:
        self.resources[value.id] = value

    def add_patch(self, value: PatchFact) -> None:
        self.patches[value.id] = value

    def add_membership(self, value: MembershipFact) -> None:
        self.memberships[(value.workspace_id, value.user_id)] = value

    def enqueue_outbox(self, event: OutboxFact) -> None:
        if self.fail_outbox:
            raise RuntimeError("outbox write failed")
        duplicate = next(
            (
                value
                for value in self.outbox
                if (
                    value.aggregate_type,
                    value.aggregate_id,
                    value.idempotency_key,
                )
                == (event.aggregate_type, event.aggregate_id, event.idempotency_key)
            ),
            None,
        )
        if duplicate is not None:
            if duplicate != event:
                raise ApprovalConflictError("审批 发件箱 幂等 冲突")
            return
        self.outbox.append(event)


class ApprovalBackend:
    """待处理 Approval 的唯一创建边界; 此处不能提交 Patch。"""

    def __init__(
        self,
        repository: InMemoryApprovalRepository,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or self._default_id
        self._next_id = 0

    def _default_id(self) -> str:
        self._next_id += 1
        return f"approval-{self._next_id}"

    async def create(
        self, command: ApprovalCreateCommand, *, transaction: _Transaction | None = None
    ) -> Approval:
        if transaction is None:
            async with self.repository.transaction() as current:
                return await self.create(command, transaction=current)
        binding = command.binding
        assert isinstance(binding, ApprovalBinding)
        self._validate_requested_by(command.requested_by)
        key = (binding.workspace_id, binding.run_id, binding.idempotency_key)
        existing_id = self.repository.approval_keys.get(key)
        if existing_id is not None:
            existing = self.repository.approvals[existing_id]
            if not self._same_create(existing, command):
                raise ApprovalConflictError("审批 幂等 冲突")
            return existing
        self._validate_scope_facts(binding)
        approval_id = self._id_factory().strip()
        if not approval_id or approval_id in self.repository.approvals:
            raise ApprovalConflictError("审批 标识符 冲突")
        value = Approval(
            approval_id=approval_id,
            workspace_id=binding.workspace_id,
            run_id=binding.run_id,
            step_id=binding.step_id,
            resource_id=binding.resource_id,
            patch_id=binding.patch_id,
            patch_hash=binding.patch_hash,
            tool_name=binding.tool_name,
            tool_version=binding.tool_version,
            input_hash=binding.input_hash,
            requested_by=command.requested_by,
            required_role=command.required_role,
            status=ApprovalStatus.PENDING.value,
            decision=None,
            decision_reason=None,
            decided_by=None,
            created_at=self._now(),
            decided_at=None,
            idempotency_key=binding.idempotency_key,
            continuation_step_id=None,
            payload={**command.payload, "target_version_id": binding.target_version_id},
            reason=command.reason,
        )
        self.repository.approvals[value.approval_id] = value
        self.repository.approval_keys[key] = value.approval_id
        self.repository.enqueue_outbox(
            OutboxFact(
                "agent_tool_approval",
                value.approval_id,
                "agent.tool_approval.requested",
                f"tool-approval-requested:{value.approval_id}",
                {
                    "approval_id": value.approval_id,
                    "run_id": value.run_id,
                    "tool_name": value.tool_name,
                },
            )
        )
        return value

    async def get(self, approval_id: str, workspace_id: str) -> Approval:
        value = self.repository.approvals.get(approval_id)
        if value is None or value.workspace_id != workspace_id:
            raise LookupError("审批 未找到")
        return value

    async def load_approval(self, approval_id: str) -> ApprovalGrant | None:
        value = self.repository.approvals.get(approval_id)
        if value is None:
            return None
        from docreview.tool_runtime.models import (
            ApprovalGrant,
            ApprovalRequirement,
            ToolName,
            ToolVersion,
        )

        return ApprovalGrant(
            approval_id=value.approval_id,
            status=value.status,
            requirement=ApprovalRequirement(
                workspace_id=value.workspace_id,
                run_id=value.run_id,
                step_id=value.step_id,
                resource_id=value.resource_id,
                tool_name=ToolName(value.tool_name),
                tool_version=ToolVersion(value.tool_version),
                idempotency_key=value.idempotency_key,
                input_hash=value.input_hash,
                patch_hash=value.patch_hash,
            ),
        )

    async def approve(
        self, command: ApprovalDecisionCommand, *, transaction: _Transaction | None = None
    ) -> Approval:
        if command.status != ApprovalStatus.APPROVED.value:
            raise ApprovalValidationError("批准操作需要状态为已批准的决定")
        return await self._decide(command, transaction=transaction)

    async def reject(
        self, command: ApprovalDecisionCommand, *, transaction: _Transaction | None = None
    ) -> Approval:
        if command.status != ApprovalStatus.REJECTED.value:
            raise ApprovalValidationError("拒绝操作需要状态为已拒绝的决定")
        return await self._decide(command, transaction=transaction)

    async def decide_approval(self, command: ApprovalDecision) -> Approval:
        """适配冻结的 HTTP decision DTO, 不暴露 ToolRuntime 决策方法。"""
        try:
            decision = ApprovalDecisionCommand(
                approval_id=command.approval_id,
                workspace_id=command.workspace_id,
                decided_by=Principal(
                    command.decided_by_type,
                    command.decided_by_id,
                ),
                status=command.status,
                reason=command.reason,
                decided_at=command.decided_at,
            )
        except (TypeError, ValueError) as error:
            raise ApprovalValidationError("审批 决定 无效") from error
        if decision.status == ApprovalStatus.APPROVED.value:
            return await self.approve(decision)
        return await self.reject(decision)

    async def _decide(
        self, command: ApprovalDecisionCommand, *, transaction: _Transaction | None
    ) -> Approval:
        if transaction is None:
            async with self.repository.transaction() as current:
                return await self._decide(command, transaction=current)
        self._authorize_decider(command)
        value = await self.get(command.approval_id, command.workspace_id)
        if value.status != ApprovalStatus.PENDING.value:
            if (
                value.status == command.status
                and value.decided_by == command.decided_by
                and value.decision_reason == command.reason
            ):
                return value
            raise ApprovalConflictError("审批 已经存在不同的 决定")
        self._validate_scope_facts(value.binding)
        decided_at = command.decided_at or self._now()
        decided = replace(
            value,
            status=command.status,
            decision=command.status,
            decision_reason=command.reason,
            decided_by=command.decided_by,
            decided_at=decided_at,
        )
        step = self.repository.steps[value.step_id]
        run = self.repository.runs[value.run_id]
        if step.status != "waiting_approval" or run.status != "waiting_approval":
            raise ApprovalConflictError("审批 等待中 目标 转换 冲突")
        if command.status == ApprovalStatus.REJECTED.value:
            self.repository.steps[step.id] = replace(
                step,
                status="failed",
                error={
                    "category": "policy_blocked",
                    "message": "外部审批已被拒绝",
                    "approval_id": value.approval_id,
                },
            )
            self.repository.runs[run.id] = replace(run, status="failed", current_step=None)
            event_type = "agent.tool_approval.rejected"
        else:
            continuation_id = continuation_step_id(value.approval_id)
            continuation = build_continuation(step.continuation, value, command.status)
            existing = self.repository.steps.get(continuation_id)
            expected = StepFact(
                continuation_id,
                value.run_id,
                "queued",
                binding=value.binding.as_dict(),
                continuation=continuation,
                step_type="CommitPatch",
            )
            if existing is not None and existing != expected:
                raise ApprovalConflictError("已批准 后续步骤 幂等 冲突")
            self.repository.steps[continuation_id] = expected
            self.repository.steps[step.id] = replace(step, status="succeeded")
            self.repository.runs[run.id] = replace(
                run,
                status="queued",
                current_step=continuation_id,
            )
            decided = replace(decided, continuation_step_id=continuation_id)
            event_type = "agent.tool_approval.approved"
        self.repository.approvals[value.approval_id] = decided
        self.repository.enqueue_outbox(
            OutboxFact(
                "agent_tool_approval",
                value.approval_id,
                event_type,
                f"tool-approval-decided:{value.approval_id}",
                {
                    "approval_id": value.approval_id,
                    "run_id": value.run_id,
                    "tool_name": value.tool_name,
                    "status": command.status,
                },
            )
        )
        return decided

    def _validate_requested_by(self, principal: Principal) -> None:
        if principal.type not in {"user", "service"} or not principal.id:
            raise ApprovalValidationError("审批 requested_by 主体 无效")

    def _authorize_decider(self, command: ApprovalDecisionCommand) -> None:
        if command.decided_by.type != "user":
            raise PermissionError("审批决定需要可信用户")
        membership = self.repository.memberships.get((command.workspace_id, command.decided_by.id))
        if (
            membership is None
            or membership.status != "active"
            or membership.role not in {"owner", "admin"}
        ):
            raise PermissionError("审批 决定 需要ctive 所有者 或 管理员")

    def _validate_scope_facts(self, binding: ApprovalBinding) -> None:
        run = self.repository.runs.get(binding.run_id)
        if run is None or run.workspace_id != binding.workspace_id:
            raise ApprovalValidationError("审批 运行 超出 工作区")
        step = self.repository.steps.get(binding.step_id)
        if step is None or step.run_id != binding.run_id:
            raise ApprovalValidationError("审批 步骤 超出 运行")
        resource = self.repository.resources.get(binding.resource_id)
        if (
            resource is None
            or resource.workspace_id != binding.workspace_id
            or run.resource_id != binding.resource_id
        ):
            raise ApprovalValidationError("审批 资源 与预期不匹配 不可变 运行 资源")
        patch = self.repository.patches.get(binding.patch_id)
        if (
            patch is None
            or patch.resource_id != binding.resource_id
            or patch.patch_hash != binding.patch_hash
            or patch.target_version_id != binding.target_version_id
        ):
            raise ApprovalValidationError("审批补丁绑定无效")
        if dict(step.binding) != binding.as_dict():
            raise ApprovalValidationError("审批 工具 输入 绑定 无效")

    @staticmethod
    def _same_create(value: Approval, command: ApprovalCreateCommand) -> bool:
        binding = command.binding
        assert isinstance(binding, ApprovalBinding)
        return (
            value.binding == binding
            and value.reason == command.reason
            and value.payload == {**command.payload, "target_version_id": binding.target_version_id}
            and value.requested_by == command.requested_by
            and value.required_role == command.required_role
        )


def continuation_step_id(approval_id: str) -> str:
    return f"commit_patch:approval:{approval_id}"


def build_continuation(raw: dict[str, Any], approval: Approval, status: str) -> dict[str, Any]:
    if raw.get("approval_id") != approval.approval_id or raw.get("status") != "pending":
        raise ApprovalConflictError("审批 后续步骤 身份 不匹配")
    request = cast(object, raw.get("graph_request"))
    state = cast(object, raw.get("graph_state"))
    if not isinstance(request, dict) or not isinstance(state, dict):
        raise ApprovalConflictError("审批 后续步骤 格式错误")
    request_value = cast(dict[str, Any], request)
    state_value = cast(dict[str, Any], state)
    payload = cast(object, request_value.get("payload"))
    approval_ref = cast(object, state_value.get("approval_ref"))
    patch_ref = cast(object, state_value.get("patch_ref"))
    if (
        request_value.get("operation") != "await_approval"
        or not isinstance(payload, dict)
        or not isinstance(approval_ref, dict)
        or not isinstance(patch_ref, dict)
    ):
        raise ApprovalConflictError("审批 后续步骤 绑定 不匹配")
    payload_value = cast(dict[str, Any], payload)
    approval_value = cast(dict[str, Any], approval_ref)
    patch_value = cast(dict[str, Any], patch_ref)
    if (
        payload_value.get("approval_id") != approval.approval_id
        or payload_value.get("target_idempotency_key") != approval.idempotency_key
        or approval_value.get("approval_id") != approval.approval_id
        or approval_value.get("status") != "pending"
        or patch_value.get("valid") is not True
        or patch_value.get("target_idempotency_key") != approval.idempotency_key
    ):
        raise ApprovalConflictError("审批 后续步骤 绑定 不匹配")
    value = deepcopy(raw)
    value["approval_decision"] = {
        "approval_id": approval.approval_id,
        "status": status,
        "patch_hash": approval.patch_hash,
        "input_hash": approval.input_hash,
        "target_version_id": approval.binding.target_version_id,
    }
    return value


__all__ = [
    "ApprovalBackend",
    "ApprovalConflictError",
    "ApprovalValidationError",
    "InMemoryApprovalRepository",
    "build_continuation",
    "continuation_step_id",
]
