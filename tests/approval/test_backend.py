from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from docreview.approval import (
    ApprovalBackend,
    ApprovalConflictError,
    ApprovalCreateCommand,
    ApprovalDecisionCommand,
    ApprovalValidationError,
    InMemoryApprovalRepository,
    MembershipFact,
    PatchFact,
    Principal,
    ResourceFact,
    RunFact,
    StepFact,
)
from docreview.runtime.contracts import ApprovalDecision
from docreview.tool_runtime import ApprovalGrant

NOW = datetime(2026, 8, 15, tzinfo=UTC)


def binding(**overrides: str) -> dict[str, str]:
    value = {
        "workspace_id": "workspace-1",
        "run_id": "run-1",
        "step_id": "step-1",
        "resource_id": "resource-1",
        "patch_id": "patch-1",
        "patch_hash": "sha256:" + "a" * 64,
        "tool_name": "patch.commit",
        "tool_version": "1.0.0",
        "input_hash": "sha256:" + "b" * 64,
        "idempotency_key": "patch-commit-1",
        "target_version_id": "version-1",
    }
    value.update(overrides)
    return value


def backend(*, outbox_failure: bool = False) -> ApprovalBackend:
    repository = InMemoryApprovalRepository(fail_outbox=outbox_failure)
    repository.add_run(RunFact("run-1", "workspace-1", "resource-1", "waiting_approval"))
    repository.add_step(
        StepFact(
            "step-1",
            "run-1",
            "waiting_approval",
            binding=binding(),
            continuation={
                "approval_id": "approval-1",
                "status": "pending",
                "graph_request": {
                    "request_id": "request-1",
                    "operation": "await_approval",
                    "payload": {
                        "approval_id": "approval-1",
                        "target_idempotency_key": "patch-commit-1",
                    },
                },
                "checkpoint_thread_id": "run-1",
                "checkpoint_step_id": "step-1",
                "graph_state": {
                    "budget": {"steps_remaining": 1},
                    "approval_ref": {
                        "approval_id": "approval-1",
                        "fact_id": "fact-approval-1",
                        "status": "pending",
                    },
                    "patch_ref": {
                        "valid": True,
                        "target_idempotency_key": "patch-commit-1",
                    },
                },
            },
        )
    )
    repository.add_resource(ResourceFact("resource-1", "workspace-1"))
    repository.add_patch(PatchFact("patch-1", "resource-1", "sha256:" + "a" * 64, "version-1"))
    repository.add_membership(MembershipFact("workspace-1", "owner-1", "owner"))
    return ApprovalBackend(repository, now=lambda: NOW, id_factory=lambda: "approval-1")


def create_command(**overrides: object) -> ApprovalCreateCommand:
    values: dict[str, object] = {
        "binding": binding(),
        "reason": "publish the validated patch",
        "payload": {"patch_id": "patch-1"},
        "requested_by": Principal("service", "runtime-1"),
        "source": "tool_runtime",
    }
    values.update(overrides)
    return ApprovalCreateCommand(**cast(Any, values))


@pytest.mark.asyncio
async def test_create_is_pending_and_repeated_same_binding_is_idempotent() -> None:
    service = backend()

    first = await service.create(create_command())
    second = await service.create(create_command())

    assert first.approval_id == "approval-1"
    assert first.status == "pending"
    assert second == first
    assert len(service.repository.outbox) == 1


@pytest.mark.asyncio
async def test_created_approval_can_be_loaded_by_tool_runtime_only_as_a_bound_grant() -> None:
    service = backend()
    await service.create(create_command())

    grant = await service.load_approval("approval-1")

    assert isinstance(grant, ApprovalGrant)
    assert grant.status == "pending"
    assert grant.requirement.run_id == "run-1"
    assert grant.requirement.resource_id == "resource-1"
    assert grant.requirement.patch_hash == "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_runtime_decider_adapter_keeps_http_decision_contract_at_fake_boundary() -> None:
    service = backend()
    await service.create(create_command())

    value = await service.decide_approval(
        ApprovalDecision(
            approval_id="approval-1",
            workspace_id="workspace-1",
            status="approved",
            reason="manual approval",
            decided_by_type="user",
            decided_by_id="owner-1",
            decided_at=NOW,
        )
    )

    assert value.id == "approval-1"
    assert value.status == "approved"


@pytest.mark.asyncio
async def test_create_rejects_changed_binding_and_invalid_scope_facts() -> None:
    service = backend()
    await service.create(create_command())

    with pytest.raises(ApprovalConflictError):
        await service.create(create_command(binding=binding(tool_name="other.tool")))
    service = backend()
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(workspace_id="workspace-2")))
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(resource_id="resource-2")))
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(input_hash="sha256:" + "c" * 64)))
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(run_id="run-2")))
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(step_id="step-2")))
    with pytest.raises(ApprovalValidationError):
        await service.create(create_command(binding=binding(patch_hash="sha256:" + "d" * 64)))
    with pytest.raises(ValueError, match="principal"):
        Principal("model", "model-1")
    with pytest.raises(ValueError, match="ToolRuntime"):
        create_command(source="model_output")


@pytest.mark.asyncio
async def test_approve_creates_one_commit_patch_continuation_and_replays() -> None:
    service = backend()
    await service.create(create_command())
    command = ApprovalDecisionCommand(
        "approval-1", "workspace-1", Principal("user", "owner-1"), "approved", "manual approval"
    )

    first = await service.approve(command)
    second = await service.approve(command)

    assert first == second
    assert first.status == "approved"
    assert first.continuation_step_id == "commit_patch:approval:approval-1"
    assert service.repository.steps[first.continuation_step_id].step_type == "CommitPatch"
    assert service.repository.runs["run-1"].status == "queued"
    assert service.repository.steps["step-1"].status == "succeeded"
    assert (
        len([event for event in service.repository.outbox if event.event_type.endswith("approved")])
        == 1
    )


@pytest.mark.asyncio
async def test_reject_is_terminal_and_never_creates_continuation() -> None:
    service = backend()
    await service.create(create_command())
    result = await service.reject(
        ApprovalDecisionCommand(
            "approval-1", "workspace-1", Principal("user", "owner-1"), "rejected", "unsafe"
        )
    )

    assert result.status == "rejected"
    assert result.continuation_step_id is None
    assert service.repository.runs["run-1"].status == "failed"
    assert service.repository.steps["step-1"].status == "failed"
    assert not any(step.step_type == "CommitPatch" for step in service.repository.steps.values())


@pytest.mark.asyncio
async def test_decision_replay_and_opposite_decision_conflict() -> None:
    service = backend()
    await service.create(create_command())
    approved = ApprovalDecisionCommand(
        "approval-1", "workspace-1", Principal("user", "owner-1"), "approved", "manual approval"
    )
    await service.approve(approved)

    with pytest.raises(ApprovalConflictError):
        await service.reject(
            ApprovalDecisionCommand(
                "approval-1", "workspace-1", Principal("user", "owner-1"), "rejected", "stop"
            )
        )
    with pytest.raises(ValueError, match="reason"):
        ApprovalDecisionCommand(
            "approval-1", "workspace-1", Principal("user", "owner-1"), "rejected", ""
        )
    with pytest.raises(PermissionError):
        await service.approve(
            ApprovalDecisionCommand(
                "approval-1",
                "workspace-2",
                Principal("user", "owner-1"),
                "approved",
                "manual approval",
            )
        )


@pytest.mark.asyncio
async def test_only_active_owner_or_admin_can_decide() -> None:
    service = backend()
    await service.create(create_command())
    service.repository.add_membership(MembershipFact("workspace-1", "editor-1", "editor"))

    with pytest.raises(PermissionError):
        await service.approve(
            ApprovalDecisionCommand(
                "approval-1",
                "workspace-1",
                Principal("user", "editor-1"),
                "approved",
                "manual approval",
            )
        )


@pytest.mark.asyncio
async def test_decision_and_outbox_rollback_together() -> None:
    service = backend()
    await service.create(create_command())
    service.repository.fail_outbox = True

    with pytest.raises(RuntimeError, match="outbox"):
        await service.approve(
            ApprovalDecisionCommand(
                "approval-1",
                "workspace-1",
                Principal("user", "owner-1"),
                "approved",
                "manual approval",
            )
        )

    assert service.repository.approvals["approval-1"].status == "pending"
    assert service.repository.runs["run-1"].status == "waiting_approval"
    assert service.repository.steps["step-1"].status == "waiting_approval"
