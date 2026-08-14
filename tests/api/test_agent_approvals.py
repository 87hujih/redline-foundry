from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from docreview.api.dependencies import AppDependencies
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.identity.trusted_ingress import TrustedIngressAdapter
from docreview.storage.models import ApprovalSummary
from docreview.storage.postgres.errors import RecordNotFoundError

SECRET = "s" * 32
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
APPROVAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_ID = "77777777-7777-4777-8777-777777777777"
STEP_ID = "88888888-8888-4888-8888-888888888888"


def signed_headers(path: str) -> dict[str, str]:
    principal_id = "11111111-1111-4111-8111-111111111111"
    organization_id = "22222222-2222-4222-8222-222222222222"
    issued_at = "2026-08-12T12:00:00Z"
    roles = "viewer"
    request_id = "stable-request"
    canonical = "\n".join(
        (
            "v1",
            request_id,
            "GET",
            path,
            "user",
            principal_id,
            organization_id,
            WORKSPACE_ID,
            issued_at,
            roles,
        )
    )
    return {
        "X-Request-ID": request_id,
        "X-DocReview-Principal-Type": "user",
        "X-DocReview-Principal-ID": principal_id,
        "X-DocReview-Organization-ID": organization_id,
        "X-DocReview-Workspace-ID": WORKSPACE_ID,
        "X-DocReview-Identity-Issued-At": issued_at,
        "X-DocReview-Roles": roles,
        "X-DocReview-Identity-Signature": hmac.new(
            SECRET.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }


@dataclass
class FakeApprovals:
    approvals: list[ApprovalSummary] = field(default_factory=lambda: list[ApprovalSummary]())
    detail: ApprovalSummary | None = None
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def list_approvals(
        self, workspace_id: str, status: str, limit: int
    ) -> list[ApprovalSummary]:
        self.calls.append(("list", workspace_id, status, limit))
        return self.approvals

    async def get_approval(self, workspace_id: str, approval_id: str) -> ApprovalSummary:
        self.calls.append(("get", workspace_id, approval_id))
        if self.detail is None:
            raise RecordNotFoundError
        return self.detail


def adapter() -> TrustedIngressAdapter:
    return TrustedIngressAdapter(
        secret=SECRET,
        trust_source="edge",
        max_age=timedelta(minutes=5),
        now=lambda: NOW,
    )


def app(repository: FakeApprovals | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            approval_queries=repository,
            identity_adapter=adapter(),
        ),
    )


def approval() -> ApprovalSummary:
    return ApprovalSummary(
        id=APPROVAL_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_id=STEP_ID,
        objective="Review policy",
        tool_name="CommitPatch",
        tool_version="v1",
        reason="Needs approval",
        status="pending",
        resources=[{"type": "document", "id": "resource-1", "access": "write"}],
        payload={"patch_id": "patch-1"},
        created_at=NOW,
        resource_id="55555555-5555-4555-8555-555555555555",
        session_id="66666666-6666-4666-8666-666666666666",
    )


@pytest.mark.anyio
async def test_approval_list_filters_and_exact_workspace_scope() -> None:
    repository = FakeApprovals(approvals=[approval()])
    path = "/api/agent/approvals"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(f"{path}?status=pending&limit=7", headers=signed_headers(path))

    assert response.status_code == 200
    assert response.json()["approvals"][0] == {
        "id": APPROVAL_ID,
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "step_id": STEP_ID,
        "resource_id": "55555555-5555-4555-8555-555555555555",
        "session_id": "66666666-6666-4666-8666-666666666666",
        "objective": "Review policy",
        "tool_name": "CommitPatch",
        "tool_version": "v1",
        "reason": "Needs approval",
        "status": "pending",
        "resources": [{"type": "document", "id": "resource-1", "access": "write"}],
        "payload": {"patch_id": "patch-1"},
        "created_at": "2026-08-12T12:00:00Z",
    }
    assert repository.calls == [("list", WORKSPACE_ID, "pending", 7)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("limit=0", "limit 必须介于 1 和 100 之间"),
        ("status=unknown", "审批状态无效"),
    ],
)
async def test_approval_list_rejects_invalid_filters(query: str, message: str) -> None:
    repository = FakeApprovals()
    path = "/api/agent/approvals"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(f"{path}?{query}", headers=signed_headers(path))

    assert response.status_code == 400
    assert response.json() == {"error": message}
    assert repository.calls == []


@pytest.mark.anyio
async def test_approval_detail_invalid_missing_and_untrusted() -> None:
    repository = FakeApprovals()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        invalid_path = "/api/agent/approvals/not-a-uuid"
        invalid = await client.get(invalid_path, headers=signed_headers(invalid_path))
        missing_path = f"/api/agent/approvals/{APPROVAL_ID}"
        missing = await client.get(missing_path, headers=signed_headers(missing_path))
        untrusted = await client.get(missing_path)

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "审批 ID 非法"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "记录不存在"}
    assert untrusted.status_code == 401
    assert untrusted.json() == {"error": "Agent 查询身份不可信"}


@pytest.mark.anyio
async def test_unconfigured_approval_query_service_precedes_auth_and_validation() -> None:
    path = "/api/agent/approvals"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        response = await client.get(f"{path}?limit=0")

    assert response.status_code == 503
    assert response.json() == {"error": "Agent 运行查询服务未配置"}
