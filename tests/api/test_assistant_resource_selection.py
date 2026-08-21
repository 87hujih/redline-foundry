from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest

from docreview.api.dependencies import AppDependencies, CompatibilityScope
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.identity.trusted_ingress import (
    IdentityRequest,
    Principal,
    TrustedIngressAdapter,
    WorkspaceScope,
)
from docreview.storage.postgres.errors import RecordNotFoundError, SessionNotFoundError
from docreview.turn.models import Turn, TurnStatus
from docreview.turn.pipeline import PipelineRequest, PipelineResult

SECRET = "s" * 32
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
OTHER_SESSION_ID = "44444444-4444-4444-8444-444444444444"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"
OTHER_RESOURCE_ID = "66666666-6666-4666-8666-666666666666"
ALTERNATE_RESOURCE_ID = "77777777-7777-4777-8777-777777777777"
MISSING_RESOURCE_ID = "88888888-8888-4888-8888-888888888888"
NON_UPLOAD_RESOURCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PRINCIPAL_ID = "99999999-9999-4999-8999-999999999999"
ORGANIZATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def signed_headers(method: str, path: str, *, request_id: str) -> dict[str, str]:
    issued_at = "2026-08-19T12:00:00Z"
    roles = "owner"
    canonical = "\n".join(
        (
            "v1",
            request_id,
            method,
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
class SelectionStore:
    selections: dict[tuple[str, str], str | None] = field(
        default_factory=lambda: dict[tuple[str, str], str | None]()
    )
    upload_resources: set[tuple[str, str]] = field(default_factory=lambda: set[tuple[str, str]]())
    read_error: Exception | None = None
    write_error: Exception | None = None
    changed_writes: int = 0

    async def get_resource_selection(self, workspace_id: str, session_id: str) -> str | None:
        if self.read_error is not None:
            raise self.read_error
        key = (workspace_id, session_id)
        if key not in self.selections:
            raise SessionNotFoundError
        return self.selections[key]

    async def set_resource_selection(
        self, workspace_id: str, session_id: str, resource_id: str
    ) -> str:
        if self.write_error is not None:
            raise self.write_error
        key = (workspace_id, session_id)
        if key not in self.selections:
            raise SessionNotFoundError
        if (workspace_id, resource_id) not in self.upload_resources:
            raise RecordNotFoundError
        if self.selections[key] != resource_id:
            self.selections[key] = resource_id
            self.changed_writes += 1
        return resource_id


@dataclass
class CapturingPipeline:
    requests: list[PipelineRequest] = field(default_factory=lambda: list[PipelineRequest]())

    async def execute(self, request: PipelineRequest, observer: object) -> PipelineResult:
        del observer
        self.requests.append(request)
        if not request.resource_id:
            raise ValueError("resource scope is required")
        return PipelineResult(
            mode="durable",
            dto={"session": {"id": SESSION_ID}, "messages": []},
            events=(),
            turn=Turn(
                "turn-1",
                SESSION_ID,
                "run-1",
                request.request_id,
                TurnStatus.SUCCEEDED,
            ),
        )


@dataclass(frozen=True)
class FixedIdentityAdapter:
    scope: WorkspaceScope

    def authenticate(self, request: IdentityRequest, requested_workspace_id: str) -> WorkspaceScope:
        del request, requested_workspace_id
        return self.scope


def app(
    store: SelectionStore | None,
    *,
    pipeline: CapturingPipeline | None = None,
    assistant: object | None = None,
    identity_adapter: object | None = None,
) -> Any:
    adapter = identity_adapter or TrustedIngressAdapter(
        secret=SECRET,
        trust_source="edge-hmac-v1",
        max_age=timedelta(minutes=5),
        now=lambda: NOW,
    )
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            # Deliberately different: selection authorization must use signed identity scope.
            compatibility_scope=CompatibilityScope(OTHER_WORKSPACE_ID),
            identity_adapter=cast(Any, adapter),
            assistant=cast(Any, assistant),
            assistant_resource_selection=store,
            turn_pipeline=pipeline,
        ),
    )


def selection_path(session_id: str = SESSION_ID) -> str:
    return f"/api/assistant/sessions/{session_id}/resource-selection"


@pytest.mark.anyio
async def test_selection_put_is_exposed_by_allowed_origin_preflight() -> None:
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        response = await client.options(
            path,
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "content-type,x-request-id,x-docreview-principal-type,"
                    "x-docreview-principal-id,x-docreview-organization-id,"
                    "x-docreview-workspace-id,x-docreview-identity-issued-at,"
                    "x-docreview-roles,x-docreview-identity-signature"
                ),
            },
        )

    assert response.status_code == 204
    assert "PUT" in response.headers["Access-Control-Allow-Methods"]
    allow_headers = response.headers["Access-Control-Allow-Headers"].lower()
    assert "x-docreview-workspace-id" in allow_headers
    assert "x-docreview-identity-signature" in allow_headers


@pytest.mark.anyio
async def test_signed_session_selection_can_be_read_set_and_repeated_idempotently() -> None:
    store = SelectionStore(
        selections={(WORKSPACE_ID, SESSION_ID): None},
        upload_resources={(WORKSPACE_ID, RESOURCE_ID)},
    )
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        empty = await client.get(
            path, headers=signed_headers("GET", path, request_id="selection-read-empty")
        )
        selected = await client.put(
            path,
            headers=signed_headers("PUT", path, request_id="selection-write-1"),
            json={"resource_id": RESOURCE_ID},
        )
        replayed = await client.put(
            path,
            headers=signed_headers("PUT", path, request_id="selection-write-2"),
            json={"resource_id": RESOURCE_ID},
        )
        restored = await client.get(
            path, headers=signed_headers("GET", path, request_id="selection-read-selected")
        )

    assert empty.status_code == 200
    assert empty.json() == {"resource_id": None}
    assert selected.status_code == replayed.status_code == restored.status_code == 200
    assert selected.json() == replayed.json() == restored.json() == {"resource_id": RESOURCE_ID}
    assert selected.headers["X-Request-ID"] == "selection-write-1"
    assert store.changed_writes == 1


@pytest.mark.anyio
async def test_selection_normalizes_equivalent_uuid_text_before_idempotency_check() -> None:
    resource_id = NON_UPLOAD_RESOURCE_ID
    store = SelectionStore(
        selections={(WORKSPACE_ID, SESSION_ID): resource_id},
        upload_resources={(WORKSPACE_ID, resource_id)},
    )
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        response = await client.put(
            path,
            headers=signed_headers("PUT", path, request_id="selection-equivalent-uuid"),
            json={"resource_id": resource_id.upper()},
        )

    assert response.status_code == 200
    assert response.json() == {"resource_id": resource_id}
    assert store.changed_writes == 0


@pytest.mark.anyio
async def test_selection_rejects_missing_or_untrusted_identity_before_repository_access() -> None:
    store = SelectionStore(selections={(WORKSPACE_ID, SESSION_ID): None})
    path = selection_path()
    invalid_headers = signed_headers("GET", path, request_id="selection-invalid-signature")
    invalid_headers["X-DocReview-Identity-Signature"] = "0" * 64

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        missing = await client.get(path)
        invalid = await client.get(path, headers=invalid_headers)

    assert missing.status_code == 401
    assert missing.json() == {"error": "持久化 身份 为必填项"}
    assert invalid.status_code == 401
    assert invalid.json() == {"error": "持久化 身份 不可信"}
    assert store.selections == {(WORKSPACE_ID, SESSION_ID): None}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scope",
    [
        WorkspaceScope(
            principal=Principal("user", PRINCIPAL_ID, ORGANIZATION_ID, ("owner",)),
            workspace_id=WORKSPACE_ID,
            trust_source="edge-hmac-v1",
            trusted=False,
            issued_at=NOW,
        ),
        WorkspaceScope(
            principal=Principal("user", PRINCIPAL_ID, ORGANIZATION_ID, ("owner",)),
            workspace_id=OTHER_WORKSPACE_ID,
            trust_source="edge-hmac-v1",
            trusted=True,
            issued_at=NOW,
        ),
    ],
)
async def test_selection_rejects_untrusted_or_mismatched_workspace_scope(
    scope: WorkspaceScope,
) -> None:
    store = SelectionStore(selections={(WORKSPACE_ID, SESSION_ID): None})
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store, identity_adapter=FixedIdentityAdapter(scope))),
        base_url="http://test",
    ) as client:
        response = await client.get(
            path,
            headers=signed_headers("GET", path, request_id="selection-scope-mismatch"),
        )

    assert response.status_code == 403
    assert response.json() == {"error": "持久化 工作区 范围 不可信"}
    assert store.selections == {(WORKSPACE_ID, SESSION_ID): None}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"resource_id": None}, {"resource_id": 1}, {"resource_id": "bad"}],
)
async def test_selection_rejects_invalid_resource_body(body: object) -> None:
    store = SelectionStore(selections={(WORKSPACE_ID, SESSION_ID): None})
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        response = await client.put(
            path,
            headers=signed_headers("PUT", path, request_id=f"invalid-{type(body).__name__}"),
            json=body,
        )

    assert response.status_code == 400
    assert response.json() == {"error": "资源 ID 非法"}


@pytest.mark.anyio
async def test_selection_rejects_invalid_session_uuid() -> None:
    store = SelectionStore()
    path = selection_path("not-a-uuid")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        response = await client.get(
            path, headers=signed_headers("GET", path, request_id="invalid-session")
        )

    assert response.status_code == 400
    assert response.json() == {"error": "会话 ID 非法"}


@pytest.mark.anyio
async def test_selection_hides_cross_workspace_and_missing_resource_existence() -> None:
    store = SelectionStore(
        selections={
            (WORKSPACE_ID, SESSION_ID): None,
            (OTHER_WORKSPACE_ID, OTHER_SESSION_ID): None,
        },
        upload_resources={
            (WORKSPACE_ID, RESOURCE_ID),
            (OTHER_WORKSPACE_ID, OTHER_RESOURCE_ID),
        },
    )
    local_path = selection_path()
    other_session_path = selection_path(OTHER_SESSION_ID)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store)), base_url="http://test"
    ) as client:
        missing_resource = await client.put(
            local_path,
            headers=signed_headers("PUT", local_path, request_id="missing-resource"),
            json={"resource_id": MISSING_RESOURCE_ID},
        )
        cross_resource = await client.put(
            local_path,
            headers=signed_headers("PUT", local_path, request_id="cross-resource"),
            json={"resource_id": OTHER_RESOURCE_ID},
        )
        non_upload_resource = await client.put(
            local_path,
            headers=signed_headers("PUT", local_path, request_id="non-upload-resource"),
            json={"resource_id": NON_UPLOAD_RESOURCE_ID},
        )
        cross_session = await client.put(
            other_session_path,
            headers=signed_headers("PUT", other_session_path, request_id="cross-session"),
            json={"resource_id": RESOURCE_ID},
        )

    assert missing_resource.status_code == cross_resource.status_code == 404
    assert non_upload_resource.status_code == 404
    assert (
        missing_resource.json()
        == cross_resource.json()
        == non_upload_resource.json()
        == {"error": "资源不存在"}
    )
    assert cross_session.status_code == 404
    assert cross_session.json() == {"error": "会话不存在"}


@pytest.mark.anyio
async def test_selection_maps_unavailable_and_unexpected_repository_failures() -> None:
    path = selection_path()
    failed_read = SelectionStore(
        selections={(WORKSPACE_ID, SESSION_ID): None}, read_error=RuntimeError("database down")
    )
    failed_write = SelectionStore(
        selections={(WORKSPACE_ID, SESSION_ID): None}, write_error=RuntimeError("database down")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        unavailable = await client.get(
            path, headers=signed_headers("GET", path, request_id="unavailable")
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(failed_read)), base_url="http://test"
    ) as client:
        read = await client.get(path, headers=signed_headers("GET", path, request_id="failed-read"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(failed_write)), base_url="http://test"
    ) as client:
        write = await client.put(
            path,
            headers=signed_headers("PUT", path, request_id="failed-write"),
            json={"resource_id": RESOURCE_ID},
        )

    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "会话资源选择不可用"}
    assert read.status_code == 500
    assert read.json() == {"error": "查询会话资源选择失败"}
    assert write.status_code == 500
    assert write.json() == {"error": "更新会话资源选择失败"}


@pytest.mark.anyio
async def test_selection_does_not_treat_plain_assistant_reader_as_selection_repository() -> None:
    path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None, assistant=object())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            path,
            headers=signed_headers("GET", path, request_id="selection-dependency-unavailable"),
        )

    assert response.status_code == 503
    assert response.json() == {"error": "会话资源选择不可用"}


@pytest.mark.anyio
async def test_message_resource_remains_explicit_and_does_not_follow_or_rewrite_selection() -> None:
    store = SelectionStore(
        selections={(WORKSPACE_ID, SESSION_ID): RESOURCE_ID},
        upload_resources={(WORKSPACE_ID, RESOURCE_ID)},
    )
    pipeline = CapturingPipeline()
    message_path = f"/api/assistant/sessions/{SESSION_ID}/messages"
    resource_path = selection_path()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(store, pipeline=pipeline)), base_url="http://test"
    ) as client:
        explicit = await client.post(
            message_path,
            headers=signed_headers("POST", message_path, request_id="message-explicit"),
            json={"message": "review", "resource_id": ALTERNATE_RESOURCE_ID},
        )
        restored = await client.get(
            resource_path,
            headers=signed_headers("GET", resource_path, request_id="selection-after-message"),
        )
        missing = await client.post(
            message_path,
            headers=signed_headers("POST", message_path, request_id="message-missing"),
            json={"message": "review"},
        )

    assert explicit.status_code == 200
    assert pipeline.requests[0].resource_id == ALTERNATE_RESOURCE_ID
    assert restored.status_code == 200
    assert restored.json() == {"resource_id": RESOURCE_ID}
    assert missing.status_code == 500
    assert missing.json() == {"error": "处理助手请求失败"}
    assert pipeline.requests[-1].resource_id == ""
