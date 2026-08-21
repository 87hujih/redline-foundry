from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from docreview.knowledge.evidence_service import (
    Candidate,
    EmbeddingProfile,
    EmbeddingProfileMismatch,
    EvidenceConfig,
    EvidenceScope,
    EvidenceService,
    FusionAlgorithm,
    InvalidSearchRequest,
    RerankResult,
    RetrievalUnavailable,
    ScopeNotFound,
    ScoredCandidate,
)


class LexicalRepository:
    async def resolve_scope(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str | None,
        include_history: bool,
    ) -> EvidenceScope:
        assert (workspace_id, resource_id, version_id, include_history) == (
            "workspace-1",
            "resource-1",
            None,
            False,
        )
        return EvidenceScope(
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id="version-1",
            source_type="document_node",
            embedding_profile="embedding-v1",
        )

    async def embedding_vector_type(self) -> str:
        raise AssertionError("semantic retrieval is disabled")

    async def search_lexical(
        self, scope: EvidenceScope, query: str, limit: int
    ) -> list[ScoredCandidate]:
        assert query == "policy"
        assert limit == 50
        return [
            ScoredCandidate(
                Candidate(
                    source_id="source-1",
                    resource_id="resource-1",
                    version_id="version-1",
                    node_id="node-1",
                    source_type="document_node",
                    content="Policy text",
                    created_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
                ),
                0.8,
            )
        ]

    async def search_semantic(
        self,
        scope: EvidenceScope,
        vector: list[float],
        profile: EmbeddingProfile,
        limit: int,
    ) -> list[ScoredCandidate]:
        raise AssertionError("semantic retrieval is disabled")


def lexical_config() -> EvidenceConfig:
    return EvidenceConfig(
        profile_version="retrieval-v1",
        lexical_enabled=True,
        semantic_enabled=False,
        lexical_index_version="pg-trgm-v1",
        semantic_index_version="hnsw-cosine-v1",
        candidate_limit=50,
        fusion_algorithm=FusionAlgorithm.WEIGHTED_SUM,
        lexical_weight=0.45,
        vector_weight=0.55,
        rrf_constant=60,
        minimum_fused_score=0.05,
        embedding=EmbeddingProfile(
            version="embedding-v1",
            model="embedding-model",
            dimensions=3,
            vector_type="vector(3)",
            index_version="hnsw-cosine-v1",
        ),
        rerank_enabled=False,
        rerank_profile_version="rerank-v1",
        rerank_model="reranker-model",
        now=lambda: datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_lexical_only_builds_stable_evidence() -> None:
    service = EvidenceService(
        config=lexical_config(),
        repository=LexicalRepository(),
        embedder=None,
        reranker=None,
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query=" policy ",
        limit=5,
    )

    assert result.query == "policy"
    assert (
        result.query_hash
        == "sha256:823412d1eacb67956220e532959f0104603057c88704863ca38e7cd188fda812"
    )
    assert result.set_id == "evset_976d0442578d107362bc8071b8f6302d"
    assert [item.evidence_id for item in result.evidence] == ["ev_74c550ca13c61a41f90e835b826af3e0"]
    item = result.evidence[0]
    assert (
        item.content_hash
        == "sha256:c10162a93f18c13e9ec89da5b6378e93f4e980a7cfda4c56919ab000ca9c0b29"
    )
    assert (item.lexical_score, item.vector_score, item.fused_score) == (0.8, 0.0, 0.8)
    assert item.trust_level == "untrusted"
    assert [
        (record.channel, record.rank, record.index_version) for record in item.provenance.retrieval
    ] == [("lexical", 1, "pg-trgm-v1")]
    assert item.provenance.filtering[0].reason == "current_version"
    assert item.provenance.fusion.pre_rerank_rank == 1
    assert item.provenance.rerank.applied is False
    assert [(record.stage, record.status) for record in result.process] == [
        ("recall", "succeeded"),
        ("filter", "succeeded"),
        ("fusion", "succeeded"),
        ("rerank", "skipped"),
    ]


def candidate(source_id: str, node_id: str, content: str) -> Candidate:
    return Candidate(
        source_id=source_id,
        resource_id="resource-1",
        version_id="version-1",
        node_id=node_id,
        source_type="document_node",
        content=content,
        created_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
    )


class HybridRepository(LexicalRepository):
    def __init__(self) -> None:
        self.scope = EvidenceScope(
            "workspace-1", "resource-1", "version-1", "document_node", "embedding-v1"
        )
        self.vector_type = "vector(3)"
        self.lexical_result: list[ScoredCandidate] = []
        self.semantic_result: list[ScoredCandidate] = []
        self.lexical_error = False
        self.semantic_error = False
        self.semantic_profile: EmbeddingProfile | None = None

    async def resolve_scope(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str | None,
        include_history: bool,
    ) -> EvidenceScope:
        return self.scope

    async def embedding_vector_type(self) -> str:
        return self.vector_type

    async def search_lexical(
        self, scope: EvidenceScope, query: str, limit: int
    ) -> list[ScoredCandidate]:
        if self.lexical_error:
            raise RuntimeError("lexical unavailable")
        return self.lexical_result

    async def search_semantic(
        self,
        scope: EvidenceScope,
        vector: list[float],
        profile: EmbeddingProfile,
        limit: int,
    ) -> list[ScoredCandidate]:
        self.semantic_profile = profile
        if self.semantic_error:
            raise RuntimeError("semantic unavailable")
        return self.semantic_result

    async def list_leading_chunks(
        self, scope: EvidenceScope, limit: int
    ) -> list[ScoredCandidate]:
        return []


class Embedder:
    def __init__(self, vectors: list[list[float]] | None = None, *, fail: bool = False) -> None:
        self.vectors = vectors if vectors is not None else [[0.1, 0.2, 0.3]]
        self.fail = fail

    async def embed_many(
        self,
        texts: list[str],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return self.vectors


@pytest.mark.asyncio
async def test_summary_query_falls_back_to_leading_chunks() -> None:
    repository = HybridRepository()
    repository.list_leading_chunks = lambda scope, limit: _leading_chunks(scope, limit)  # type: ignore[method-assign]
    service = EvidenceService(
        config=lexical_config(), repository=repository, embedder=None, reranker=None
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="这个文档主要讲什么",
        limit=5,
    )

    assert [item.content for item in result.evidence] == ["Document introduction"]
    assert any(
        item.reason == "summary_leading_chunks_fallback" for item in result.process
    )


async def _leading_chunks(scope: EvidenceScope, limit: int) -> list[ScoredCandidate]:
    assert (scope.resource_id, limit) == ("resource-1", 5)
    item = candidate("source-leading", "node-leading", "Document introduction")
    return [ScoredCandidate(item, 0.25)]


class Reranker:
    def __init__(self, results: list[RerankResult] | None = None, *, fail: bool = False) -> None:
        self.results = results if results is not None else [RerankResult(0, 0.95)]
        self.fail = fail

    async def rerank(
        self,
        query: str,
        documents: list[str],
        limit: int,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[RerankResult]:
        if self.fail:
            raise RuntimeError("reranker unavailable")
        return self.results


def semantic_config(**changes: object) -> EvidenceConfig:
    config = replace(
        lexical_config(),
        lexical_enabled=False,
        semantic_enabled=True,
    )
    return replace(config, **changes)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_semantic_only_preserves_exact_embedding_profile_and_index_version() -> None:
    repository = HybridRepository()
    repository.semantic_result = [ScoredCandidate(candidate("source-s", "node-s", "Semantic"), 0.9)]
    service = EvidenceService(
        config=semantic_config(),
        repository=repository,
        embedder=Embedder(),
        reranker=None,
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="policy",
        limit=5,
    )

    assert (result.evidence[0].lexical_score, result.evidence[0].vector_score) == (0.0, 0.9)
    assert result.evidence[0].provenance.retrieval[0].channel == "semantic"
    assert repository.semantic_profile == replace(
        semantic_config().embedding, index_version="hnsw-cosine-v1"
    )


@pytest.mark.asyncio
async def test_fusion_threshold_then_rerank_preserves_pre_and_post_rank() -> None:
    repository = HybridRepository()
    first = candidate("source-b", "node-b", "High")
    filtered = candidate("source-c", "node-c", "Low")
    repository.lexical_result = [ScoredCandidate(first, 0.8), ScoredCandidate(filtered, 0.2)]
    repository.semantic_result = [ScoredCandidate(first, 0.6), ScoredCandidate(filtered, 0.2)]
    config = replace(
        lexical_config(), semantic_enabled=True, rerank_enabled=True, minimum_fused_score=0.5
    )
    service = EvidenceService(
        config=config,
        repository=repository,
        embedder=Embedder(),
        reranker=Reranker(),
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="policy",
        limit=5,
    )

    assert len(result.evidence) == 1
    assert result.evidence[0].fused_score == pytest.approx(0.69)
    assert result.evidence[0].provenance.fusion.pre_rerank_rank == 1
    assert result.evidence[0].provenance.rerank.applied is True
    assert result.evidence[0].provenance.rerank.score == 0.95
    assert result.process[-1].stage == "rerank"
    assert result.process[-1].status == "succeeded"


@pytest.mark.asyncio
async def test_equal_scores_use_stable_source_order_and_fixture_ids() -> None:
    repository = HybridRepository()
    repository.lexical_result = [
        ScoredCandidate(candidate("source-z", "node-z", "Z"), 0.8),
        ScoredCandidate(candidate("source-a", "node-a", "A"), 0.8),
    ]
    service = EvidenceService(
        config=lexical_config(), repository=repository, embedder=None, reranker=None
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="policy",
        limit=5,
    )

    assert [(item.node_id, item.evidence_id) for item in result.evidence] == [
        ("node-a", "ev_1978987e2c8ffacb4aa214f1312ad28d"),
        ("node-z", "ev_133a8e1d1c49658c67112d5329827312"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_channel", "expected_reason"),
    [("lexical", "lexical_recall_failed"), ("semantic", "semantic_recall_failed")],
)
async def test_one_failed_channel_degrades_only_when_the_other_channel_succeeds(
    failed_channel: str, expected_reason: str
) -> None:
    repository = HybridRepository()
    repository.lexical_result = [ScoredCandidate(candidate("source-l", "node-l", "Lexical"), 0.7)]
    repository.semantic_result = [ScoredCandidate(candidate("source-s", "node-s", "Semantic"), 0.8)]
    repository.lexical_error = failed_channel == "lexical"
    repository.semantic_error = failed_channel == "semantic"
    service = EvidenceService(
        config=replace(lexical_config(), semantic_enabled=True),
        repository=repository,
        embedder=Embedder(),
        reranker=None,
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="policy",
        limit=5,
    )

    assert any(
        record.status == "degraded" and record.reason == expected_reason
        for record in result.process
    )
    assert result.evidence


@pytest.mark.asyncio
async def test_all_configured_channels_unavailable_fails_closed() -> None:
    repository = HybridRepository()
    repository.lexical_error = True
    repository.semantic_error = True
    service = EvidenceService(
        config=replace(lexical_config(), semantic_enabled=True),
        repository=repository,
        embedder=Embedder(),
        reranker=None,
    )

    with pytest.raises(RetrievalUnavailable):
        await service.search(
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id=None,
            include_history=False,
            query="policy",
            limit=5,
        )


@pytest.mark.asyncio
async def test_reranker_failure_retains_fusion_order_with_explicit_degradation() -> None:
    repository = HybridRepository()
    repository.lexical_result = [ScoredCandidate(candidate("source-a", "node-a", "A"), 0.9)]
    service = EvidenceService(
        config=replace(lexical_config(), rerank_enabled=True),
        repository=repository,
        embedder=None,
        reranker=Reranker(fail=True),
    )

    result = await service.search(
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id=None,
        include_history=False,
        query="policy",
        limit=5,
    )

    assert result.evidence[0].provenance.rerank.degraded_reason == "reranker_failed"
    assert [(record.stage, record.reason) for record in result.process[-2:]] == [
        ("rerank", "reranker_failed"),
        ("degradation", "fusion_order_retained"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["profile", "vector_type", "dimension"])
async def test_embedding_profile_vector_type_and_dimension_mismatch_fail_closed(
    mismatch: str,
) -> None:
    repository = HybridRepository()
    vectors = [[0.1, 0.2, 0.3]]
    if mismatch == "profile":
        repository.scope = replace(repository.scope, embedding_profile="other-profile")
    elif mismatch == "vector_type":
        repository.vector_type = "vector(4)"
    else:
        vectors = [[0.1, 0.2]]
    service = EvidenceService(
        config=semantic_config(),
        repository=repository,
        embedder=Embedder(vectors),
        reranker=None,
    )

    with pytest.raises(EmbeddingProfileMismatch):
        await service.search(
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id=None,
            include_history=False,
            query="policy",
            limit=5,
        )


@pytest.mark.asyncio
async def test_repository_scope_cannot_cross_workspace() -> None:
    repository = HybridRepository()
    repository.scope = replace(repository.scope, workspace_id="workspace-2")
    service = EvidenceService(
        config=lexical_config(), repository=repository, embedder=None, reranker=None
    )

    with pytest.raises(ScopeNotFound):
        await service.search(
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id=None,
            include_history=False,
            query="policy",
            limit=5,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_id", "include_history"),
    [("version-1", False), (None, True)],
)
async def test_current_and_history_scope_are_mutually_exclusive(
    version_id: str | None, include_history: bool
) -> None:
    service = EvidenceService(
        config=lexical_config(),
        repository=HybridRepository(),
        embedder=None,
        reranker=None,
    )

    with pytest.raises(InvalidSearchRequest):
        await service.search(
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id=version_id,
            include_history=include_history,
            query="policy",
            limit=5,
        )
