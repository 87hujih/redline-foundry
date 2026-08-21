from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from docreview.agent_graph.boundary import ProjectRuntimeBoundary
from docreview.agent_graph.production import (
    PostgresBudgetReader,
    PostgresGraphFactStore,
    ProductionGraphCommitter,
    ProductionGraphToolRuntime,
)
from docreview.api.assembly import (
    assemble_production_repositories,
    build_production_project_runtime_boundary,
)
from docreview.config.settings import load_settings
from docreview.knowledge.chunking import DeterministicTokenizer
from docreview.providers.assembly import ProductionProviderDependencies
from docreview.storage.postgres.assistant import AssistantRepository
from docreview.storage.postgres.document_commit import PostgresCommitStore
from docreview.storage.postgres.identity import IdentityRepository
from docreview.storage.postgres.pool import DatabasePool
from docreview.storage.postgres.runtime_repository import RuntimeRepository

PRODUCTION_RUNTIME_ENV = {
    "APP_ENV": "production",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "s" * 32,
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "300000",
    "DATABASE_URL": "postgresql://database.example/docreview",
    "DOCUMENT_PARSER": "structured",
    "TIKA_URL": "http://tika.internal:9998",
    "TIKA_TIMEOUT_MS": "45000",
    "EMBEDDING_TOKENIZER_PROFILE": "docreview-production-tokenizer",
    "RUNTIME_WORKER_ENABLED": "true",
    "PROJECTION_WORKER_ENABLED": "true",
    "RUNTIME_WORKER_ID": "worker-1",
    "SILICONFLOW_API_KEY": "test-key",
    "SILICONFLOW_BASE_URL": "https://provider.example/v1",
    "LLM_MODEL": "chat-model",
    "EMBEDDING_MODEL": "embedding-model",
    "EMBEDDING_DIM": "1024",
    "RERANKER_MODEL": "reranker-model",
    "LLM_TIMEOUT_MS": "30000",
    "LLM_RETRY_MAX": "1",
    "LLM_RETRY_BACKOFF_MS": "100",
    "WEB_SEARCH_URL": "https://search.example/v1",
}


def _providers(*, web_search: object | None = object()) -> ProductionProviderDependencies:
    return cast(
        ProductionProviderDependencies,
        SimpleNamespace(
            model_gateway=object(),
            embedder=object(),
            reranker=object(),
            file_store=object(),
            web_search=web_search,
            chunk_tokenizer=DeterministicTokenizer.for_testing(),
        ),
    )


def test_complete_project_runtime_boundary_is_constructed_without_database_io() -> None:
    settings = load_settings(PRODUCTION_RUNTIME_ENV)
    pool = cast(DatabasePool, object())
    boundary = build_production_project_runtime_boundary(
        settings,
        pool=pool,
        providers=_providers(),
        runtime=RuntimeRepository(cast(Any, pool)),
        identity=IdentityRepository(cast(Any, pool)),
        canonical_committer=PostgresCommitStore(cast(Any, pool)),
    )

    assert isinstance(boundary, ProjectRuntimeBoundary)
    assert isinstance(boundary.tools, ProductionGraphToolRuntime)
    assert isinstance(boundary.committer, ProductionGraphCommitter)
    assert isinstance(boundary.facts, PostgresGraphFactStore)
    assert isinstance(boundary.budgets, PostgresBudgetReader)


def test_project_runtime_boundary_allows_web_search_to_be_disabled() -> None:
    environment = PRODUCTION_RUNTIME_ENV.copy()
    del environment["WEB_SEARCH_URL"]
    settings = load_settings(environment)
    pool = cast(DatabasePool, object())
    boundary = build_production_project_runtime_boundary(
        settings,
        pool=pool,
        providers=_providers(web_search=None),
        runtime=RuntimeRepository(cast(Any, pool)),
        identity=IdentityRepository(cast(Any, pool)),
        canonical_committer=PostgresCommitStore(cast(Any, pool)),
    )

    assert isinstance(boundary, ProjectRuntimeBoundary)


def test_production_repository_assembly_explicitly_binds_session_resource_selection() -> None:
    settings = load_settings(
        {
            "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "s" * 32,
            "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy",
            "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "300000",
        }
    )
    providers = cast(
        ProductionProviderDependencies,
        SimpleNamespace(
            embedder=object(),
            reranker=object(),
            document_parser=SimpleNamespace(supported_extensions=(".pdf",)),
            file_store=object(),
            chunk_tokenizer=DeterministicTokenizer.for_testing(),
        ),
    )

    assembly = assemble_production_repositories(
        settings,
        pool=cast(DatabasePool, SimpleNamespace(opened=False)),
        providers=providers,
    )

    assert isinstance(assembly.assistant, AssistantRepository)
    assert assembly.dependencies.assistant is assembly.assistant
    assert assembly.dependencies.assistant_resource_selection is assembly.assistant
