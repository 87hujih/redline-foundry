"""FastAPI 应用工厂与只读 Phase 2 入口。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from docreview.api.assembly import assemble_production_repositories
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
from docreview.providers.assembly import (
    ProductionProviderDependencies,
    create_production_provider_dependencies,
)
from docreview.storage.postgres.pool import DatabasePool, create_database_pool

type ProviderDependencyFactory = Callable[
    [Settings], Awaitable[ProductionProviderDependencies | None]
]


def windows_selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


def _request_id(request: Request) -> str:
    request_id = request.headers.get("X-Request-ID", "").strip()
    return request_id or secrets.token_hex(16)


def _providers_available(providers: ProductionProviderDependencies | None) -> bool:
    if providers is None:
        return False
    client = getattr(providers, "http_client", None)
    file_store = getattr(providers, "file_store", None)
    return (
        getattr(providers, "model_gateway", None) is not None
        and getattr(providers, "embedder", None) is not None
        and getattr(providers, "reranker", None) is not None
        and getattr(providers, "document_parser", None) is not None
        and getattr(providers, "chunk_tokenizer", None) is not None
        and file_store is not None
        and getattr(file_store, "is_closed", True) is False
        and client is not None
        and getattr(client, "is_closed", True) is False
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    dependencies: AppDependencies = app.state.dependencies
    providers = dependencies.providers
    lifecycle = dependencies.runtime_lifecycle
    lifecycle_started = False
    pool_owned = False
    owned_pool: DatabasePool | None = None
    provider_owned = False
    try:
        if app.state.settings.app_env == "production" and providers is None:
            providers = await app.state.provider_dependency_factory(app.state.settings)
            provider_owned = providers is not None
            if providers is not None:
                dependencies = replace(dependencies, providers=providers)
        if (
            app.state.settings.app_env == "production"
            and providers is not None
            and _providers_available(providers)
        ):
            dependencies = replace(
                dependencies,
                providers=providers,
                file_store=providers.file_store,
                upload_policy_extensions=list(providers.document_parser.supported_extensions),
                upload_max_bytes=app.state.settings.upload_max_bytes,
            )
            app.state.dependencies = dependencies
            lifecycle = dependencies.runtime_lifecycle
        if app.state.settings.app_env == "production" and not _providers_available(providers):
            raise RuntimeError("production AI providers are unavailable")
        if (
            app.state.settings.app_env == "production"
            and providers is not None
            and dependencies.database_pool is None
            and app.state.settings.database_url is not None
        ):
            pool = await create_database_pool(app.state.settings)
            pool_owned = True
            owned_pool = pool
            assembled = assemble_production_repositories(
                app.state.settings,
                pool=pool,
                providers=providers,
                runtime_executor=dependencies.runtime_executor,  # type: ignore[arg-type]
                checkpointer=dependencies.checkpointer,
                runtime_boundary=dependencies.runtime_boundary,  # type: ignore[arg-type]
            )
            dependencies = replace(
                assembled.dependencies,
                runtime_lifecycle=(
                    dependencies.runtime_lifecycle or assembled.dependencies.runtime_lifecycle
                ),
                runtime_engine=(
                    dependencies.runtime_engine or assembled.dependencies.runtime_engine
                ),
                runtime_executor=(
                    dependencies.runtime_executor or assembled.dependencies.runtime_executor
                ),
                runtime_boundary=(
                    dependencies.runtime_boundary or assembled.dependencies.runtime_boundary
                ),
                projection_worker=(
                    dependencies.projection_worker or assembled.dependencies.projection_worker
                ),
                checkpointer=dependencies.checkpointer or assembled.dependencies.checkpointer,
            )
            app.state.dependencies = dependencies
            lifecycle = dependencies.runtime_lifecycle
        if lifecycle is not None:
            await lifecycle.start()
            lifecycle_started = True
        app.state.runtime_worker_started = lifecycle_started
        app.state.projection_worker_started = lifecycle_started
        app.state.started = True
        yield
    finally:
        try:
            if lifecycle_started and lifecycle is not None:
                await lifecycle.stop()
        finally:
            try:
                if provider_owned and providers is not None:
                    await providers.aclose()
            finally:
                if pool_owned and owned_pool is not None:
                    await owned_pool.close()
                app.state.runtime_worker_started = False
                app.state.projection_worker_started = False
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
            allow_methods = "GET, POST, PATCH, DELETE, OPTIONS"
            allow_headers = "Content-Type, X-Request-ID"
            if request.url.path.startswith(
                "/api/assistant/sessions/"
            ) and request.url.path.endswith("/resource-selection"):
                allow_methods = "GET, PUT, OPTIONS"
                allow_headers = ", ".join(
                    (
                        "Content-Type",
                        "X-Request-ID",
                        "X-DocReview-Principal-Type",
                        "X-DocReview-Principal-ID",
                        "X-DocReview-Organization-ID",
                        "X-DocReview-Workspace-ID",
                        "X-DocReview-Identity-Issued-At",
                        "X-DocReview-Roles",
                        "X-DocReview-Identity-Signature",
                    )
                )
            response.headers["Access-Control-Allow-Methods"] = allow_methods
            response.headers["Access-Control-Allow-Headers"] = allow_headers
            response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
        return response

    app.middleware("http")(exact_cors_middleware)


def create_app(
    settings: Settings | None = None,
    *,
    dependencies: AppDependencies | None = None,
    provider_dependency_factory: ProviderDependencyFactory = (
        create_production_provider_dependencies
    ),
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
    app.state.provider_dependency_factory = provider_dependency_factory
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
        loop=(
            "docreview.api.main:windows_selector_loop_factory"
            if sys.platform == "win32"
            else "auto"
        ),
        lifespan="on",
        log_config=None,
    )


__all__ = ["create_app", "lifespan", "main", "windows_selector_loop_factory"]
