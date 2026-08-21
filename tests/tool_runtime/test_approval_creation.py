from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from docreview.approval import (
    ApprovalBackend,
    ApprovalBinding,
    InMemoryApprovalRepository,
    PatchFact,
    ResourceFact,
    RunFact,
    StepFact,
)
from docreview.tool_runtime import (
    AuditStatus,
    PolicyDecision,
    Principal,
    ToolDefinition,
    ToolExecutionContext,
    ToolName,
    ToolRegistry,
    ToolRiskLevel,
    ToolRuntime,
    ToolVersion,
)


class Policy:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def authorize(self, request: object) -> PolicyDecision:
        del request
        self.calls += 1
        return PolicyDecision(self.allowed, "authorized" if self.allowed else "denied")


class Backend:
    async def execute(self, request: object) -> object:
        raise AssertionError("approval creation must not execute the target tool")

    async def recover(self, request: object) -> object:
        raise AssertionError("approval creation must not recover the target tool")


def runtime(policy: Policy | None = None) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=ToolName("document.commit_patch"),
            version=ToolVersion("1.0.0"),
            description="Commit an approved canonical Patch",
            input_schema=(
                '{"type":"object","properties":{"resource_id":{"type":"string"}},'
                '"required":["resource_id"],"additionalProperties":false}'
            ),
            output_schema='{"type":"object","additionalProperties":false}',
            risk_level=ToolRiskLevel.HIGH,
            timeout=timedelta(seconds=10),
            max_inline_output_bytes=1024,
            requires_resource=True,
            resource_input_field="resource_id",
            resource_access="write",
            required_permissions=("document.write",),
            requires_approval=True,
            side_effecting=True,
            backend=Backend(),
        )
    )
    registry.freeze()
    repository = InMemoryApprovalRepository()
    binding = ApprovalBinding(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-1",
        resource_id="resource-1",
        patch_id="patch-1",
        patch_hash="sha256:" + "a" * 64,
        tool_name="document.commit_patch",
        tool_version="1.0.0",
        input_hash="sha256:" + "b" * 64,
        idempotency_key="patch-commit-1",
        target_version_id="version-1",
    )
    repository.add_run(RunFact("run-1", "workspace-1", "resource-1", "waiting_approval"))
    repository.add_step(StepFact("step-1", "run-1", "waiting_approval", binding.as_dict(), {}))
    repository.add_resource(ResourceFact("resource-1", "workspace-1"))
    repository.add_patch(PatchFact("patch-1", "resource-1", binding.patch_hash, "version-1"))
    approvals = ApprovalBackend(repository, id_factory=lambda: "approval-1")
    return ToolRuntime(
        registry=registry,
        policy=policy or Policy(),
        approvals=approvals,
        limiter=cast(Any, object()),
        audit=cast(Any, object()),
        artifacts=cast(Any, object()),
    )


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id="run-1",
        step_id="step-1",
        workspace_id="workspace-1",
        resource_id="resource-1",
        principal=Principal("service", "runtime-1"),
        roles=("editor",),
        trace_id="trace-1",
        attempt=1,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_tool_runtime_can_only_create_a_pending_bound_approval_observation() -> None:
    value = await runtime().request_pending_approval(
        context(),
        ApprovalBinding(
            workspace_id="workspace-1",
            run_id="run-1",
            step_id="step-1",
            resource_id="resource-1",
            patch_id="patch-1",
            patch_hash="sha256:" + "a" * 64,
            tool_name="document.commit_patch",
            tool_version="1.0.0",
            input_hash="sha256:" + "b" * 64,
            idempotency_key="patch-commit-1",
            target_version_id="version-1",
        ),
        reason="publish",
        payload={"patch_id": "patch-1"},
    )

    assert value.status is AuditStatus.PENDING
    assert value.approval_id == "approval-1"
    assert value.error is None


@pytest.mark.asyncio
async def test_tool_runtime_denies_approval_creation_before_persisting() -> None:
    policy = Policy(allowed=False)
    value = await runtime(policy).request_pending_approval(
        context(),
        ApprovalBinding(
            workspace_id="workspace-1",
            run_id="run-1",
            step_id="step-1",
            resource_id="resource-1",
            patch_id="patch-1",
            patch_hash="sha256:" + "a" * 64,
            tool_name="document.commit_patch",
            tool_version="1.0.0",
            input_hash="sha256:" + "b" * 64,
            idempotency_key="patch-commit-1",
            target_version_id="version-1",
        ),
        reason="publish",
        payload={"patch_id": "patch-1"},
    )

    assert policy.calls == 1
    assert value.status is AuditStatus.FAILED
    assert value.approval_id is None
    assert value.error is not None
