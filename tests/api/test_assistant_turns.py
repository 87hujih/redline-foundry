from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from docreview.api.dependencies import AppDependencies, CompatibilityScope
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.identity.trusted_ingress import TrustedIngressAdapter
from docreview.turn.models import Turn, TurnEvent, TurnStatus
from docreview.turn.pipeline import PipelineRequest, PipelineResult, TurnNotReadyError
from docreview.turn.sse import event_frames

SECRET = "s" * 32
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PRINCIPAL_ID = "44444444-4444-4444-8444-444444444444"
ORGANIZATION_ID = "55555555-5555-4555-8555-555555555555"


def headers(path: str, *, last_event_id: str | None = None) -> dict[str, str]:
    request_id = "request-1"
    issued_at = "2026-08-13T12:00:00Z"
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
    values = {
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
    if last_event_id is not None:
        values["Last-Event-ID"] = last_event_id
    return values


def projection() -> dict[str, object]:
    return {
        "session": {
            "id": SESSION_ID,
            "title": "review",
            "web_search_enabled": False,
            "last_message_at": "2026-08-13T12:00:00Z",
            "created_at": "2026-08-13T12:00:00Z",
            "updated_at": "2026-08-13T12:00:00Z",
        },
        "messages": [],
    }


@dataclass
class Pipeline:
    error: Exception | None = None
    requests: list[PipelineRequest] = field(default_factory=lambda: list[PipelineRequest]())
    observer_kinds: list[bool] = field(default_factory=lambda: list[bool]())

    async def execute(self, request: PipelineRequest, observer: object) -> PipelineResult:
        self.requests.append(request)
        self.observer_kinds.append(observer is not None)
        if self.error is not None:
            raise self.error
        events = (
            TurnEvent("e1", "turn-1", 1, "turn.accepted", {"status": "running"}),
            TurnEvent(
                "e2",
                "turn-1",
                2,
                "assistant.message",
                {
                    "id": "message-1",
                    "role": "assistant",
                    "kind": "text",
                    "payload": {"content": "done"},
                    "sequence_no": 2,
                    "created_at": "2026-08-13T12:00:00Z",
                },
            ),
            TurnEvent("e3", "turn-1", 3, "turn.succeeded", {"status": "succeeded"}),
        )
        if observer is not None:
            for event in events:
                if event.sequence > request.after_sequence:
                    await observer(event)  # type: ignore[operator]
        return PipelineResult(
            "durable",
            projection(),
            events,
            Turn("turn-1", SESSION_ID, "run-1", request.request_id, TurnStatus.SUCCEEDED),
        )


def app(pipeline: Pipeline | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            compatibility_scope=CompatibilityScope(WORKSPACE_ID),
            identity_adapter=TrustedIngressAdapter(
                secret=SECRET,
                trust_source="edge-hmac-v1",
                max_age=timedelta(minutes=5),
                now=lambda: NOW,
            ),
            turn_pipeline=pipeline,
        ),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "status", "session_id"),
    [
        ("/api/assistant/conversations", 201, None),
        (f"/api/assistant/sessions/{SESSION_ID}/messages", 200, SESSION_ID),
    ],
)
async def test_non_stream_write_routes_use_one_durable_pipeline(
    path: str, status: int, session_id: str | None
) -> None:
    pipeline = Pipeline()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(pipeline)), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            headers=headers(path),
            json={"message": "review", "resource_id": RESOURCE_ID},
        )

    assert response.status_code == status
    assert response.json() == projection()
    assert response.headers["X-Request-ID"] == "request-1"
    assert pipeline.observer_kinds == [False]
    request = pipeline.requests[0]
    assert request.session_id == session_id
    assert request.workspace_id == WORKSPACE_ID
    assert request.resource_id == RESOURCE_ID
    assert request.request_id == request.trace_id == "request-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/api/assistant/conversations/stream",
        f"/api/assistant/sessions/{SESSION_ID}/messages/stream",
    ],
)
async def test_stream_routes_replay_after_last_event_id_with_compatible_sse(path: str) -> None:
    pipeline = Pipeline()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(pipeline)), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            headers=headers(path, last_event_id="1"),
            json={"message": "review", "resource_id": RESOURCE_ID},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert "id: 1\n" not in response.text
    assert "id: 2\nevent: message_completed\n" in response.text
    assert "id: 3\nevent: done\ndata: {}\n\n" in response.text
    assert pipeline.requests[0].after_sequence == 1
    assert pipeline.observer_kinds == [True]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("headers_override", "body", "status", "message"),
    [
        (
            {},
            {"message": "review", "resource_id": RESOURCE_ID},
            401,
            "durable identity is required",
        ),
        (None, {"message": " ", "resource_id": RESOURCE_ID}, 400, "消息内容不能为空"),
        (None, {"message": "review", "resource_id": "bad"}, 400, "资源 ID 非法"),
    ],
)
async def test_turn_validation_and_identity_fail_before_pipeline(
    headers_override: dict[str, str] | None,
    body: dict[str, object],
    status: int,
    message: str,
) -> None:
    path = "/api/assistant/conversations"
    pipeline = Pipeline()
    request_headers = headers(path) if headers_override is None else headers_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(pipeline)), base_url="http://test"
    ) as client:
        response = await client.post(path, headers=request_headers, json=body)

    assert response.status_code == status
    assert response.json() == {"error": message}
    assert pipeline.requests == []


@pytest.mark.anyio
async def test_last_event_id_and_pipeline_errors_keep_frozen_mapping() -> None:
    path = "/api/assistant/conversations/stream"
    pipeline = Pipeline(error=TurnNotReadyError())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(pipeline)), base_url="http://test"
    ) as client:
        invalid = await client.post(
            path,
            headers=headers(path, last_event_id="-1"),
            json={"message": "review", "resource_id": RESOURCE_ID},
        )
        streamed = await client.post(
            path,
            headers=headers(path),
            json={"message": "review", "resource_id": RESOURCE_ID},
        )

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "Last-Event-ID 非法"}
    assert streamed.status_code == 200
    assert "event: error" in streamed.text
    assert "assistant_internal_error" in streamed.text


def test_frontend_fixture_is_generated_from_public_sse_frames() -> None:
    value = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "phase6_frontend.json").read_text()
    )
    assert value["request_id"] == "request-1"
    persisted = [
        TurnEvent("e1", "turn-1", 1, "turn.running", {"status": "running"}),
        TurnEvent(
            "e2",
            "turn-1",
            2,
            "assistant.message",
            {
                "id": "message-1",
                "role": "assistant",
                "kind": "text",
                "payload": {"content": "done"},
                "sequence_no": 2,
                "created_at": "2026-08-13T12:00:00Z",
            },
        ),
        TurnEvent("e3", "turn-1", 3, "turn.succeeded", {"status": "succeeded"}),
    ]
    generated = [
        {"id": frame.id, "event": frame.event, "data": frame.data}
        for event in persisted
        for frame in event_frames(event)
    ]
    assert value["frames"] == generated
