from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from docreview.api.dependencies import AppDependencies, CompatibilityScope
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.identity.trusted_ingress import TrustedIngressAdapter
from docreview.runtime.contracts import ApprovalDecision
from docreview.runtime.errors import ApprovalConflictError
from docreview.runtime.models import Approval

SECRET = "s" * 32
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
APPROVAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRINCIPAL_ID = "44444444-4444-4444-8444-444444444444"
ORGANIZATION_ID = "55555555-5555-4555-8555-555555555555"


def signed(path: str) -> dict[str, str]:
    issued_at = "2026-08-13T12:00:00Z"
    request_id = "approval-request"
    roles = "owner"
    canonical = "\n".join(
        (
            "v1",
            request_id,
            "POST",
            path,
            "user",
            PRINCIPAL_ID,
            ORGANIZATION_ID,
            WORKSPACE_ID,
            issued_at,
            roles,
        )
    )
    return {
        "X-Request-ID": request_id,
        "X-DocReview-Principal-Type": "user",
        "X-DocReview-Principal-ID": PRINCIPAL_ID,
        "X-DocReview-Organization-ID": ORGANIZATION_ID,
        "X-DocReview-Workspace-ID": WORKSPACE_ID,
        "X-DocReview-Identity-Issued-At": issued_at,
        "X-DocReview-Roles": roles,
        "X-DocReview-Identity-Signature": hmac.new(
            SECRET.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }


@dataclass
class SessionWriter:
    deleted: bool = True
    calls: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())

    async def delete_session(self, workspace_id: str, session_id: str) -> bool:
        self.calls.append((workspace_id, session_id))
        return self.deleted


@dataclass
class Decider:
    error: Exception | None = None
    calls: list[ApprovalDecision] = field(default_factory=lambda: list[ApprovalDecision]())

    async def decide_approval(self, command: ApprovalDecision) -> Approval:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return Approval(
            id=APPROVAL_ID,
            workspace_id=WORKSPACE_ID,
            run_id="77777777-7777-4777-8777-777777777777",
            step_id="88888888-8888-4888-8888-888888888888",
            tool_name="CommitPatch",
            tool_version="v1",
            idempotency_key="commit-key",
            resources=[],
            resources_hash="sha256:" + "0" * 64,
            payload={},
            reason="review",
            status=command.status,
            created_at=NOW,
        )


def make_app(*, writer: SessionWriter | None = None, decider: Decider | None = None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            compatibility_scope=CompatibilityScope(WORKSPACE_ID),
            assistant_writer=writer,
            approval_decider=decider,
            identity_adapter=TrustedIngressAdapter(
                secret=SECRET,
                trust_source="edge-hmac-v1",
                max_age=timedelta(minutes=5),
                now=lambda: NOW,
            ),
        ),
    )


@pytest.mark.anyio
async def test_delete_session_is_workspace_scoped_and_uses_frozen_statuses() -> None:
    writer = SessionWriter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(writer=writer)), base_url="http://test"
    ) as client:
        deleted = await client.delete(f"/api/assistant/sessions/{SESSION_ID}")
        writer.deleted = False
        missing = await client.delete(f"/api/assistant/sessions/{SESSION_ID}")
        invalid = await client.delete("/api/assistant/sessions/bad")

    assert deleted.status_code == 204 and deleted.content == b""
    assert missing.status_code == 404 and missing.json() == {"error": "会话不存在"}
    assert invalid.status_code == 400 and invalid.json() == {"error": "会话 ID 非法"}
    assert writer.calls == [(WORKSPACE_ID, SESSION_ID), (WORKSPACE_ID, SESSION_ID)]


@pytest.mark.anyio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_approval_decisions_authenticate_and_call_transactional_continuation(
    decision: str,
) -> None:
    decider = Decider()
    path = f"/api/agent/approvals/{APPROVAL_ID}/{decision}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(decider=decider)), base_url="http://test"
    ) as client:
        response = await client.post(path, headers=signed(path), json={"reason": " reviewed "})

    assert response.status_code == 200
    assert response.json() == {
        "approval": {
            "id": APPROVAL_ID,
            "status": "approved" if decision == "approve" else "rejected",
        }
    }
    command = decider.calls[0]
    assert command.workspace_id == WORKSPACE_ID
    assert command.decided_by_id == PRINCIPAL_ID
    assert command.reason == "reviewed"


@pytest.mark.anyio
async def test_approval_failure_mapping_is_frozen() -> None:
    path = f"/api/agent/approvals/{APPROVAL_ID}/approve"
    decider = Decider(error=ApprovalConflictError())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(decider=decider)), base_url="http://test"
    ) as client:
        conflict = await client.post(path, headers=signed(path), json={"reason": "reviewed"})
        blank = await client.post(path, headers=signed(path), json={"reason": " "})
        untrusted = await client.post(path, json={"reason": "reviewed"})

    assert conflict.status_code == 409 and conflict.json() == {"error": "审批状态冲突"}
    assert blank.status_code == 400 and blank.json() == {"error": "审批理由不能为空"}
    assert untrusted.status_code == 401 and untrusted.json() == {"error": "审批身份不可信"}
