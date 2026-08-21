from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from docreview.api.dependencies import AppDependencies
from docreview.api.main import create_app
from docreview.config.settings import Settings, load_settings
from docreview.document.parser import DocumentParser
from docreview.providers.assembly import (
    ProductionProviderDependencies,
    create_production_provider_dependencies,
)
from docreview.providers.embedding import SiliconFlowEmbeddingProvider
from docreview.providers.llm import ProductionModelGateway
from docreview.providers.reranker import SiliconFlowReranker
from docreview.storage.filestore import (
    FileStoreIOError,
    FileStorePermissionError,
    LocalFileStore,
)

PRODUCTION_ENV = {
    "APP_ENV": "production",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "s" * 32,
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "300000",
    "SILICONFLOW_API_KEY": "test-provider-key",
    "SILICONFLOW_BASE_URL": "https://provider.example/v1",
    "LLM_MODEL": "test-chat-model",
    "EMBEDDING_MODEL": "test-embedding-model",
    "EMBEDDING_DIM": "1024",
    "RERANKER_MODEL": "test-reranker-model",
    "LLM_TIMEOUT_MS": "90000",
    "LLM_RETRY_MAX": "2",
    "LLM_RETRY_BACKOFF_MS": "1000",
    "DATABASE_URL": "postgresql://database.example/docreview",
    "DOCUMENT_PARSER": "structured",
    "TIKA_URL": "http://tika.internal:9998",
    "TIKA_TIMEOUT_MS": "45000",
    "EMBEDDING_TOKENIZER_PROFILE": "docreview-production-tokenizer",
}


type MockHandler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
)


def default_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


class ClientFactory:
    def __init__(self, handler: MockHandler = default_handler) -> None:
        self.client: httpx.AsyncClient | None = None
        self.timeout: httpx.Timeout | None = None
        self.handler = handler

    def __call__(self, timeout: httpx.Timeout, limits: httpx.Limits) -> httpx.AsyncClient:
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            timeout=timeout,
            limits=limits,
        )
        return self.client


@pytest.mark.asyncio
async def test_production_assembly_constructs_parser_store_and_all_providers(
    tmp_path: Path,
) -> None:
    factory = ClientFactory()
    settings = load_settings(PRODUCTION_ENV).model_copy(
        update={"upload_storage_dir": (tmp_path / "objects").resolve()}
    )

    dependencies = await create_production_provider_dependencies(settings, client_factory=factory)

    assert isinstance(dependencies.model_gateway, ProductionModelGateway)
    assert isinstance(dependencies.embedder, SiliconFlowEmbeddingProvider)
    assert isinstance(dependencies.reranker, SiliconFlowReranker)
    assert isinstance(dependencies.document_parser, DocumentParser)
    assert dependencies.document_parser.supported_extensions == (
        ".md",
        ".txt",
        ".doc",
        ".docx",
        ".pdf",
        ".rtf",
        ".odt",
    )
    assert isinstance(dependencies.file_store, LocalFileStore)
    assert dependencies.file_store.root == (tmp_path / "objects").resolve()
    assert dependencies.http_client is factory.client
    assert factory.timeout is not None
    assert factory.timeout.connect == 90
    assert factory.timeout.read == 90
    assert factory.timeout.write == 90
    assert factory.timeout.pool == 90
    assert factory.client is not None
    assert factory.client.is_closed is False

    await dependencies.aclose()

    assert factory.client.is_closed is True
    with pytest.raises(FileStoreIOError, match="已关闭"):
        await dependencies.file_store.stat("aa/" + "a" * 64)


@pytest.mark.asyncio
async def test_production_tika_parser_uses_the_shared_http_client(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"<html><body><h1>Project</h1><p>Body</p></body></html>",
        )

    factory = ClientFactory(handler)
    settings = load_settings(
        PRODUCTION_ENV
        | {
            "DOCUMENT_PARSER": "structured",
            "TIKA_URL": "http://tika.internal:9998",
            "TIKA_TIMEOUT_MS": "45000",
            "UPLOAD_STORAGE_DIR": str(tmp_path / "objects"),
            "UPLOAD_MAX_BYTES": "1024",
        }
    )

    dependencies = await create_production_provider_dependencies(settings, client_factory=factory)
    parsed = await dependencies.document_parser.parse("review.pdf", b"%PDF fixture")

    assert parsed.parser_name == "tika_xhtml"
    assert parsed.quality_flags == ["page_mapping_unavailable"]
    assert len(requests) == 1
    assert factory.client is dependencies.http_client
    await dependencies.aclose()


@pytest.mark.asyncio
async def test_production_assembly_closes_client_when_partial_construction_fails(
    tmp_path: Path,
) -> None:
    factory = ClientFactory()
    invalid = load_settings(PRODUCTION_ENV).model_copy(
        update={"reranker_model": "", "upload_storage_dir": (tmp_path / "objects").resolve()}
    )

    with pytest.raises(ValueError, match="reranker"):
        await create_production_provider_dependencies(invalid, client_factory=factory)

    assert factory.client is not None
    assert factory.client.is_closed is True


@pytest.mark.asyncio
async def test_production_assembly_closes_client_when_storage_cannot_start(
    tmp_path: Path,
) -> None:
    factory = ClientFactory()
    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("content", encoding="utf-8")
    settings = load_settings(PRODUCTION_ENV).model_copy(
        update={"upload_storage_dir": invalid_root.resolve()}
    )

    with pytest.raises(FileStoreIOError):
        await create_production_provider_dependencies(settings, client_factory=factory)

    assert factory.client is not None
    assert factory.client.is_closed is True
    assert invalid_root.read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_production_assembly_closes_client_when_storage_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = ClientFactory()
    settings = load_settings(PRODUCTION_ENV).model_copy(
        update={"upload_storage_dir": (tmp_path / "objects").resolve()}
    )

    def denied(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise PermissionError("write denied")

    monkeypatch.setattr("docreview.storage.filestore.tempfile.mkstemp", denied)

    with pytest.raises(FileStorePermissionError, match="write denied"):
        await create_production_provider_dependencies(settings, client_factory=factory)

    assert factory.client is not None
    assert factory.client.is_closed is True


@pytest.mark.asyncio
async def test_production_lifespan_closes_provider_client_and_store(tmp_path: Path) -> None:
    settings = load_settings(PRODUCTION_ENV).model_copy(
        update={"upload_storage_dir": (tmp_path / "objects").resolve()}
    )
    factory = ClientFactory()
    created: list[ProductionProviderDependencies] = []

    async def provider_factory(resolved: Settings) -> ProductionProviderDependencies:
        dependencies = await create_production_provider_dependencies(
            resolved, client_factory=factory
        )
        created.append(dependencies)
        return dependencies

    app = create_app(
        settings,
        dependencies=AppDependencies(database_pool=cast(Any, object())),
        provider_dependency_factory=provider_factory,
    )
    async with app.router.lifespan_context(app):
        assert len(created) == 1
        assert created[0].http_client.is_closed is False
        assert app.state.dependencies.providers is created[0]
        assert app.state.dependencies.file_store is created[0].file_store
        assert app.state.dependencies.upload_policy_extensions == [
            ".md",
            ".txt",
            ".doc",
            ".docx",
            ".pdf",
            ".rtf",
            ".odt",
        ]
        assert app.state.dependencies.upload_max_bytes == 20 * 1024 * 1024

    assert created[0].http_client.is_closed is True


@pytest.mark.asyncio
async def test_production_lifespan_fails_closed_when_factory_omits_providers(
    tmp_path: Path,
) -> None:
    async def missing_factory(_settings: Settings) -> ProductionProviderDependencies | None:
        return None

    app = create_app(
        load_settings(PRODUCTION_ENV).model_copy(
            update={"upload_storage_dir": (tmp_path / "objects").resolve()}
        ),
        provider_dependency_factory=missing_factory,
    )

    with pytest.raises(RuntimeError, match="production AI providers"):
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan must not start")

    assert app.state.started is False


@pytest.mark.asyncio
async def test_production_lifespan_closes_incomplete_provider_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class IncompleteProviders:
        model_gateway = None
        embedder = None
        reranker = None
        document_parser = None
        file_store = None
        http_client = None
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    incomplete = IncompleteProviders()

    async def incomplete_factory(_settings: Settings) -> ProductionProviderDependencies:
        return cast(ProductionProviderDependencies, incomplete)

    async def forbidden_pool_factory(_settings: Settings) -> object:
        raise AssertionError("incomplete providers must fail before database pool creation")

    monkeypatch.setattr("docreview.api.main.create_database_pool", forbidden_pool_factory)

    app = create_app(
        load_settings(PRODUCTION_ENV).model_copy(
            update={"upload_storage_dir": (tmp_path / "objects").resolve()}
        ),
        provider_dependency_factory=incomplete_factory,
    )

    with pytest.raises(RuntimeError, match="production AI providers"):
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan must not start")

    assert incomplete.closed is True
    assert app.state.started is False
