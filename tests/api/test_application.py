import json
import logging
import re
from collections.abc import Sequence
from io import StringIO
from typing import Protocol, cast

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from docreview.api.errors import APIError
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.observability.logging import configure_json_logging


class IncludedRoute(Protocol):
    original_router: APIRouter


def make_app(*, origins: str = "https://app.example.com") -> FastAPI:
    settings = load_settings({"CORS_ALLOWED_ORIGINS": origins})
    return create_app(settings)


@pytest.mark.anyio
async def test_healthz_contract_request_id_and_lifespan() -> None:
    app = make_app()
    assert app.state.started is False

    async with app.router.lifespan_context(app):
        assert app.state.started is True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

    assert app.state.started is False
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "server"}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


@pytest.mark.anyio
async def test_preserves_trimmed_request_id() -> None:
    app = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "  stable-id  "})

    assert response.headers["X-Request-ID"] == "stable-id"


@pytest.mark.anyio
async def test_allowed_origin_receives_exact_cors_headers() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.options(
            "/api/not-implemented",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert response.headers["Access-Control-Allow-Methods"] == ("GET, POST, PATCH, DELETE, OPTIONS")
    assert response.headers["Access-Control-Allow-Headers"] == "Content-Type, X-Request-ID"
    assert response.headers["Access-Control-Expose-Headers"] == "X-Request-ID"
    assert "Origin" in response.headers["Vary"]


@pytest.mark.anyio
async def test_disallowed_preflight_fails_without_cors_permission() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.options(
            "/api/not-implemented",
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == 403
    assert "Access-Control-Allow-Origin" not in response.headers
    assert response.headers["Vary"] == "Origin"


@pytest.mark.anyio
async def test_originless_preflight_is_no_content() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.options("/api/not-implemented")

    assert response.status_code == 204
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.anyio
async def test_options_outside_api_is_not_intercepted() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.options("/healthz")

    assert response.status_code == 405


@pytest.mark.anyio
async def test_non_preflight_disallowed_origin_reaches_route_without_permission() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"Origin": "https://attacker.example"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers
    assert response.headers["Vary"] == "Origin"


@pytest.mark.anyio
async def test_api_error_uses_frozen_error_envelope() -> None:
    app = make_app()

    async def raise_api_error() -> None:
        raise APIError(status_code=409, message="审批状态冲突")

    app.add_api_route("/__test/error", raise_api_error, methods=["GET"])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/__test/error")

    assert response.status_code == 409
    assert response.json() == {"error": "审批状态冲突"}


def test_active_online_public_routes_are_registered() -> None:
    routes: set[tuple[str, str]] = set()

    def collect(items: Sequence[BaseRoute]) -> None:
        for route in items:
            if isinstance(route, APIRoute):
                routes.update((method, route.path) for method in route.methods or set())
            else:
                collect(cast(IncludedRoute, route).original_router.routes)

    collect(list(make_app().routes))

    assert routes == {
        ("GET", "/healthz"),
        ("GET", "/api/resources"),
        ("GET", "/api/resources/{resource_id}"),
        ("GET", "/api/resources/{resource_id}/export"),
        ("GET", "/api/resources/{resource_id}/search"),
        ("GET", "/api/agent/runs"),
        ("GET", "/api/agent/runs/{run_id}"),
        ("GET", "/api/agent/approvals"),
        ("GET", "/api/agent/approvals/{approval_id}"),
        ("GET", "/api/assistant/capabilities"),
        ("GET", "/api/assistant/sessions"),
        ("GET", "/api/assistant/sessions/{session_id}"),
        ("DELETE", "/api/assistant/sessions/{session_id}"),
        ("POST", "/api/assistant/conversations"),
        ("POST", "/api/assistant/conversations/stream"),
        ("POST", "/api/assistant/sessions/{session_id}/messages"),
        ("POST", "/api/assistant/sessions/{session_id}/messages/stream"),
        ("GET", "/api/files/{file_id}/download"),
        ("POST", "/api/assistant/conversations/files"),
        ("POST", "/api/assistant/sessions/{session_id}/files"),
        ("POST", "/api/agent/approvals/{approval_id}/approve"),
        ("POST", "/api/agent/approvals/{approval_id}/reject"),
    }


def test_json_logger_emits_machine_readable_record() -> None:
    stream = StringIO()
    logger = configure_json_logging(level="INFO", stream=stream)

    logger.info("service ready", extra={"event": "service.ready", "worker_count": 1})

    record = json.loads(stream.getvalue())
    assert record["level"] == "INFO"
    assert record["message"] == "service ready"
    assert record["service"] == "docreview-api"
    assert record["event"] == "service.ready"
    assert record["worker_count"] == 1
    logging.shutdown()


@pytest.mark.anyio
async def test_request_access_log_is_json_and_correlated() -> None:
    stream = StringIO()
    app = make_app()
    app.state.logger.handlers[0].stream = stream
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "correlated-id"})

    record = json.loads(stream.getvalue())
    assert response.status_code == 200
    assert record["event"] == "http.request.completed"
    assert record["request_id"] == "correlated-id"
    assert record["method"] == "GET"
    assert record["path"] == "/healthz"
    assert record["status"] == 200
    assert isinstance(record["latency_ms"], int)
