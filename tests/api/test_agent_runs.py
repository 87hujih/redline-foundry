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
from docreview.storage.models import (
    ApprovalView,
    Finding,
    PublicRun,
    PublicRunDetail,
    PublicStep,
    PublicToolCall,
    RunSummary,
)
from docreview.storage.postgres.errors import RecordNotFoundError

SECRET = "s" * 32
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"
RUN_ID = "77777777-7777-4777-8777-777777777777"
STEP_ID = "88888888-8888-4888-8888-888888888888"


def signed_headers(
    path: str, *, principal_type: str = "user", request_id: str = "stable-request"
) -> dict[str, str]:
    values = {
        "principal_type": principal_type,
        "principal_id": "11111111-1111-4111-8111-111111111111",
        "organization_id": "22222222-2222-4222-8222-222222222222",
        "workspace_id": WORKSPACE_ID,
        "issued_at": "2026-08-12T12:00:00Z",
        "roles": "viewer",
    }
    canonical = "\n".join(
        (
            "v1",
            request_id,
            "GET",
            path,
            values["principal_type"],
            values["principal_id"],
            values["organization_id"],
            values["workspace_id"],
            values["issued_at"],
            values["roles"],
        )
    )
    return {
        "X-Request-ID": request_id,
        "X-DocReview-Principal-Type": values["principal_type"],
        "X-DocReview-Principal-ID": values["principal_id"],
        "X-DocReview-Organization-ID": values["organization_id"],
        "X-DocReview-Workspace-ID": values["workspace_id"],
        "X-DocReview-Identity-Issued-At": values["issued_at"],
        "X-DocReview-Roles": values["roles"],
        "X-DocReview-Identity-Signature": hmac.new(
            SECRET.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }


@dataclass
class FakeAgentQueries:
    runs: list[RunSummary] = field(default_factory=lambda: list[RunSummary]())
    detail: PublicRunDetail | None = None
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def list_runs(
        self, workspace_id: str, status: str, resource_id: str, limit: int
    ) -> list[RunSummary]:
        self.calls.append(("list_runs", workspace_id, status, resource_id, limit))
        return self.runs

    async def get_run(self, workspace_id: str, run_id: str) -> PublicRunDetail:
        self.calls.append(("get_run", workspace_id, run_id))
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


def app(repository: FakeAgentQueries | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(run_queries=repository, identity_adapter=adapter()),
    )


@pytest.mark.anyio
async def test_run_list_requires_signed_user_before_query_validation() -> None:
    repository = FakeAgentQueries()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        missing = await client.get("/api/agent/runs?limit=0")
        service = await client.get(
            "/api/agent/runs",
            headers=signed_headers("/api/agent/runs", principal_type="service"),
        )

    assert missing.status_code == 401
    assert missing.json() == {"error": "Agent 查询身份不可信"}
    assert service.status_code == 401
    assert service.json() == {"error": "Agent 查询身份不可信"}
    assert repository.calls == []


@pytest.mark.anyio
async def test_run_list_filters_defaults_and_exact_workspace_scope() -> None:
    repository = FakeAgentQueries(
        runs=[
            RunSummary(
                id=RUN_ID,
                workspace_id=WORKSPACE_ID,
                status="running",
                objective="Review policy",
                step_count=3,
                completed_step_count=1,
                failed_step_count=0,
                created_at=NOW,
                updated_at=NOW,
                resource_id=RESOURCE_ID,
                current_step="DecideNextAction",
            )
        ]
    )
    path = "/api/agent/runs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(
            f"{path}?status=running&resource_id={RESOURCE_ID}", headers=signed_headers(path)
        )

    assert response.status_code == 200
    assert response.json() == {
        "runs": [
            {
                "id": RUN_ID,
                "workspace_id": WORKSPACE_ID,
                "resource_id": RESOURCE_ID,
                "status": "running",
                "objective": "Review policy",
                "current_step": "DecideNextAction",
                "step_count": 3,
                "completed_step_count": 1,
                "failed_step_count": 0,
                "created_at": "2026-08-12T12:00:00Z",
                "updated_at": "2026-08-12T12:00:00Z",
            }
        ]
    }
    assert repository.calls == [("list_runs", WORKSPACE_ID, "running", RESOURCE_ID, 50)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("limit=0", "limit 必须介于 1 和 100 之间"),
        ("limit=101", "limit 必须介于 1 和 100 之间"),
        ("status=unknown", "运行状态无效"),
        ("resource_id=invalid", "资源 ID 非法"),
    ],
)
async def test_run_list_rejects_invalid_filters(query: str, message: str) -> None:
    repository = FakeAgentQueries()
    path = "/api/agent/runs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(f"{path}?{query}", headers=signed_headers(path))

    assert response.status_code == 400
    assert response.json() == {"error": message}
    assert repository.calls == []


@pytest.mark.anyio
async def test_run_detail_uses_public_allowlist_and_never_leaks_internals() -> None:
    detail = PublicRunDetail(
        run=PublicRun(
            id=RUN_ID,
            status="failed",
            objective="Review policy",
            created_at=NOW,
            updated_at=NOW,
            resource_id=RESOURCE_ID,
            request_id="stable-request",
        ),
        steps=[
            PublicStep(
                id=STEP_ID,
                step_key="review:1",
                step_type="Review",
                status="failed",
                attempt_count=2,
                max_attempts=2,
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        tool_calls=[
            PublicToolCall(
                id="99999999-9999-4999-8999-999999999999",
                step_id=STEP_ID,
                tool_name="SearchEvidence",
                tool_version="v1",
                status="failed",
                error_category="timeout",
            )
        ],
        approvals=[
            ApprovalView(
                id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                run_id=RUN_ID,
                step_id=STEP_ID,
                tool_name="CommitPatch",
                status="rejected",
                created_at=NOW,
            )
        ],
        findings=[Finding(severity="error", code="failed_step", message="步骤失败")],
    )
    repository = FakeAgentQueries(detail=detail)
    path = f"/api/agent/runs/{RUN_ID}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=signed_headers(path))

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"] == {
        "id": RUN_ID,
        "resource_id": RESOURCE_ID,
        "request_id": "stable-request",
        "status": "failed",
        "objective": "Review policy",
        "created_at": "2026-08-12T12:00:00Z",
        "updated_at": "2026-08-12T12:00:00Z",
    }
    assert payload["steps"][0]["attempt_count"] == 2
    assert payload["tool_calls"][0]["error_category"] == "timeout"
    assert payload["approvals"][0]["status"] == "rejected"
    assert payload["findings"] == [
        {"severity": "error", "code": "failed_step", "message": "步骤失败"}
    ]
    forbidden = {
        "state_json",
        "input_json",
        "output_json",
        "context_manifests",
        "attempts",
        "trace_index",
        "trace_id",
        "runtime_mode",
        "outbox_events",
    }
    assert forbidden.isdisjoint(str(payload).lower())


@pytest.mark.anyio
async def test_run_detail_invalid_and_missing_error_mapping() -> None:
    repository = FakeAgentQueries()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        invalid_path = "/api/agent/runs/invalid"
        invalid = await client.get(invalid_path, headers=signed_headers(invalid_path))
        missing_path = f"/api/agent/runs/{RUN_ID}"
        missing = await client.get(missing_path, headers=signed_headers(missing_path))

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "运行 ID 非法"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "记录不存在"}


@pytest.mark.anyio
async def test_unconfigured_run_query_service_is_503() -> None:
    path = "/api/agent/runs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=signed_headers(path))

    assert response.status_code == 503
    assert response.json() == {"error": "Agent 运行查询服务未配置"}


@pytest.mark.anyio
async def test_unconfigured_run_query_service_precedes_auth_and_validation() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        response = await client.get("/api/agent/runs?limit=0")

    assert response.status_code == 503
    assert response.json() == {"error": "Agent 运行查询服务未配置"}
