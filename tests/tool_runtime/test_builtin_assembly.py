from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docreview.document.validation import ValidationRequest
from docreview.knowledge.evidence_service import EmbeddingProfile, EvidenceConfig, FusionAlgorithm
from docreview.tool_runtime import (
    JSONTokenCounter,
    RateLimitRule,
    StaticRateLimitRules,
    ToolName,
    ToolVersion,
)
from docreview.tool_runtime.builtin.assembly import (
    ProductionFullToolRuntimeDependencies,
    ProductionReadOnlyToolRuntimeDependencies,
    build_production_full_tool_runtime,
    build_production_read_only_tool_runtime,
    production_evidence_config,
)
from docreview.tool_runtime.builtin.patch import (
    CommitScope,
    PatchValidationBackend,
)
from docreview.tool_runtime.models import BackendRequest


class Dependencies:
    async def authorize(self, request: object) -> object:
        raise AssertionError("not executed")

    async def load_approval(self, approval_id: str) -> object:
        raise AssertionError("not executed")

    async def claim(self, request: object) -> object:
        raise AssertionError("not executed")

    async def finish(self, request: object) -> None:
        raise AssertionError("not executed")

    async def increment(self, request: object) -> int:
        raise AssertionError("not executed")

    async def load_tool_scope(self, run_id: str, step_id: str) -> object:
        raise AssertionError("not executed")


class ValidationFactory:
    def __call__(self, request: BackendRequest, patch: object) -> ValidationRequest:
        raise AssertionError("not executed")


class CommitScopes:
    async def resolve(self, request: BackendRequest, patch: object) -> CommitScope:
        raise AssertionError("not executed")


def evidence_config() -> EvidenceConfig:
    return EvidenceConfig(
        profile_version="retrieval-v1",
        lexical_enabled=True,
        semantic_enabled=True,
        lexical_index_version="pg-trgm-v1",
        semantic_index_version="hnsw-cosine-v1",
        candidate_limit=50,
        fusion_algorithm=FusionAlgorithm.WEIGHTED_SUM,
        lexical_weight=0.45,
        vector_weight=0.55,
        rrf_constant=60,
        minimum_fused_score=0.05,
        embedding=EmbeddingProfile(
            "embedding-v1", "embedding-model", 3, "vector(3)", "hnsw-cosine-v1"
        ),
        rerank_enabled=True,
        rerank_profile_version="rerank-v1",
        rerank_model="reranker-model",
        now=lambda: datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
    )


def dependencies() -> ProductionReadOnlyToolRuntimeDependencies:
    core = Dependencies()
    return ProductionReadOnlyToolRuntimeDependencies(
        document_repository=core,
        evidence_repository=core,
        artifact_repository=core,
        artifact_blob_store=core,
        embedder=core,
        reranker=core,
        evidence_config=evidence_config(),
        policy=core,
        approvals=core,
        audit=core,
        rate_limit_repository=core,
        rate_limit_rules=StaticRateLimitRules(
            default=RateLimitRule(limit=60, window=timedelta(minutes=1))
        ),
        scopes=core,
        token_counter=JSONTokenCounter(),
    )


def test_production_read_only_factory_constructs_backends_registers_then_freezes() -> None:
    assembly = build_production_read_only_tool_runtime(dependencies())

    assert assembly.runtime.registry.frozen is True
    assert (
        assembly.runtime.registry.resolve(
            ToolName("retrieval.search"), ToolVersion("2.0.0")
        ).backend
        is assembly.backends.retrieval
    )
    assert (
        assembly.runtime.registry.resolve(
            ToolName("document.read_nodes"), ToolVersion("1.0.0")
        ).backend
        is assembly.backends.documents
    )
    assert (
        assembly.runtime.registry.resolve(ToolName("artifact.read"), ToolVersion("1.0.0")).backend
        is assembly.backends.artifacts
    )


def test_production_full_factory_omits_web_search_when_provider_is_disabled() -> None:
    assembly = build_production_full_tool_runtime(
        ProductionFullToolRuntimeDependencies(
            read_only=dependencies(),
            patch_validation=PatchValidationBackend(ValidationFactory()),
            patch_commit_store=object(),
            patch_commit_scope_resolver=CommitScopes(),
            web_search_provider=None,
        )
    )

    assert assembly.backends.web_search is None
    with pytest.raises(LookupError):
        assembly.runtime.registry.resolve(ToolName("web.search"), ToolVersion("1.0.0"))


def test_production_evidence_config_matches_current_assembly() -> None:
    config = production_evidence_config(
        embedding_model="embedding-model",
        embedding_dimensions=1024,
        reranker_model="reranker-model",
    )

    assert (
        config.profile_version,
        config.lexical_index_version,
        config.semantic_index_version,
        config.candidate_limit,
        config.fusion_algorithm,
        config.lexical_weight,
        config.vector_weight,
        config.minimum_fused_score,
    ) == (
        "retrieval-v1",
        "pg-trgm-v1",
        "hnsw-cosine-v1",
        50,
        FusionAlgorithm.WEIGHTED_SUM,
        0.45,
        0.55,
        0.05,
    )
    assert config.embedding == EmbeddingProfile(
        "embedding-v1", "embedding-model", 1024, "vector(1024)", "hnsw-cosine-v1"
    )
    assert config.rerank_enabled is True
    assert config.rerank_profile_version == "rerank-v1"
    assert config.rerank_model == "reranker-model"


@pytest.mark.parametrize(
    "missing",
    [
        "document_repository",
        "evidence_repository",
        "artifact_repository",
        "artifact_blob_store",
        "embedder",
        "reranker",
        "token_counter",
    ],
)
def test_production_read_only_factory_fails_closed_on_missing_dependency(missing: str) -> None:
    configured = dependencies()
    object.__setattr__(configured, missing, None)

    with pytest.raises(ValueError, match=missing):
        build_production_read_only_tool_runtime(configured)
