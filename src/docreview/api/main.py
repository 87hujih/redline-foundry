"""FastAPI application factory and read-only Phase 2 entrypoint."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.api.routes.agent_approvals import router as agent_approvals_router
from docreview.api.routes.agent_runs import router as agent_runs_router
from docreview.api.routes.assistant_capabilities import router as capabilities_router
from docreview.api.routes.assistant_sessions import router as assistant_sessions_router
from docreview.api.routes.assistant_turns import router as assistant_turns_router
from docreview.api.routes.assistant_uploads import router as assistant_uploads_router
from docreview.api.routes.files import router as files_router
from docreview.api.routes.resources import router as resources_router
from docreview.config.settings import Settings, load_settings
from docreview.observability.logging import configure_json_logging


def _request_id(request: Request) -> str:
    request_id = request.headers.get("X-Request-ID", "").strip()
    return request_id or secrets.token_hex(16)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.started = True
    lifecycle = app.state.dependencies.runtime_lifecycle
    if lifecycle is not None:
        await lifecycle.start()
    app.state.runtime_worker_started = lifecycle is not None
    app.state.projection_worker_started = lifecycle is not None
    try:
        yield
    finally:
        if lifecycle is not None:
            await lifecycle.stop()
        app.state.started = False


def _add_error_handlers(app: FastAPI) -> None:
    async def _api_error_handler(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, APIError):
            raise error
        return JSONResponse(status_code=error.status_code, content={"error": error.message})

    async def _validation_error_handler(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        return JSONResponse(status_code=400, content={"error": "请求无效"})

    app.add_exception_handler(APIError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)


def _install_request_id_middleware(app: FastAPI, logger: logging.Logger) -> None:
    async def request_id_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            logger.info(
                "request completed",
                extra={
                    "component": "http",
                    "event": "http.request.completed",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                },
            )

    app.middleware("http")(request_id_middleware)


def _install_cors_middleware(app: FastAPI, settings: Settings) -> None:
    allowed = frozenset(settings.cors_allowed_origins)

    async def exact_cors_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        origin = request.headers.get("Origin", "").strip()
        allowed_origin = origin if origin and origin in allowed else None
        vary = "Origin" if origin else ""

        is_api_preflight = request.method == "OPTIONS" and request.url.path.startswith("/api/")
        if is_api_preflight:
            if origin and allowed_origin is None:
                response = Response(status_code=403)
            else:
                response = Response(status_code=204)
        else:
            response = await call_next(request)

        if vary:
            response.headers["Vary"] = vary
        if allowed_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
            response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
        return response

    app.middleware("http")(exact_cors_middleware)


def create_app(
    settings: Settings | None = None, *, dependencies: AppDependencies | None = None
) -> FastAPI:
    resolved = settings or load_settings()
    logger = configure_json_logging(level=resolved.log_level)
    app = FastAPI(
        title="DocReview Agent API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.dependencies = dependencies or AppDependencies()
    app.state.started = False
    app.state.logger = logger
    _add_error_handlers(app)
    _install_cors_middleware(app, resolved)
    _install_request_id_middleware(app, logger)

    async def _healthz() -> dict[str, str]:
        return {"status": "ok", "service": "server"}

    app.add_api_route("/healthz", _healthz, methods=["GET"], response_model=None)
    app.include_router(resources_router)
    app.include_router(agent_runs_router)
    app.include_router(agent_approvals_router)
    app.include_router(capabilities_router)
    app.include_router(assistant_sessions_router)
    app.include_router(assistant_turns_router)
    app.include_router(assistant_uploads_router)
    app.include_router(files_router)

    return app


def main() -> None:
    settings = load_settings()
    logger = configure_json_logging(level=settings.log_level)
    logger.info("starting service", extra={"event": "service.starting", "worker_count": 1})
    uvicorn.run(
        "docreview.api.main:create_app",
        factory=True,
        host=settings.server_host,
        port=settings.server_port,
        workers=1,
        lifespan="on",
        log_config=None,
    )


__all__ = ["create_app", "lifespan", "main"]
