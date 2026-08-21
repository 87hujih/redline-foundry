"""当前只读 builtin Tool 的 fail-closed 生产装配。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from docreview.knowledge.evidence_service import (
    EvidenceConfig,
    EvidenceEmbedder,
    EvidenceRepository,
    EvidenceReranker,
    EvidenceService,
)
from docreview.providers.web_search import WebSearchProvider
from docreview.tool_runtime.assembly import (
    ProductionToolRuntimeAssembly,
    ProductionToolRuntimeDependencies,
    build_production_tool_runtime,
)
from docreview.tool_runtime.builtin.artifact import (
    ArtifactBackend,
    ArtifactBlobStore,
    ArtifactRepository,
)
from docreview.tool_runtime.builtin.document import (
    CanonicalDocumentRepository,
    DocumentReadBackend,
)
from docreview.tool_runtime.builtin.evidence import EvidenceRetrievalBackend
from docreview.tool_runtime.builtin.patch import (
    CommitScopeResolver,
    PatchCommitBackend,
    PatchValidationBackend,
)
from docreview.tool_runtime.builtin.registration import (
    register_patch_commit_tool,
    register_patch_validation_tool,
    register_read_only_builtins,
    register_web_search_tool,
)
from docreview.tool_runtime.builtin.web import WebSearchBackend
from docreview.tool_runtime.executor import ScopeStore
from docreview.tool_runtime.rate_limit import Clock, RateLimitRepository, StaticRateLimitRules
from docreview.tool_runtime.registry import ToolRegistry
from docreview.tool_runtime.runtime import (
    ApprovalBoundary,
    AuditBoundary,
    PolicyBoundary,
    TokenCounter,
)


@dataclass(frozen=True, slots=True)
class ProductionReadOnlyToolRuntimeDependencies:
    document_repository: object | None
    evidence_repository: object | None
    artifact_repository: object | None
    artifact_blob_store: object | None
    embedder: object | None
    reranker: object | None
    evidence_config: EvidenceConfig | None
    policy: object | None
    approvals: object | None
    audit: object | None
    rate_limit_repository: object | None
    rate_limit_rules: StaticRateLimitRules | None
    scopes: object | None
    token_counter: object | None
    clock: Clock | None = None


@dataclass(frozen=True, slots=True)
class ReadOnlyBuiltinBackends:
    documents: DocumentReadBackend
    retrieval: EvidenceRetrievalBackend
    artifacts: ArtifactBackend


@dataclass(frozen=True, slots=True)
class ProductionReadOnlyToolRuntimeAssembly:
    runtime: ProductionToolRuntimeAssembly
    backends: ReadOnlyBuiltinBackends


@dataclass(frozen=True, slots=True)
class ProductionFullToolRuntimeDependencies:
    read_only: ProductionReadOnlyToolRuntimeDependencies
    patch_validation: PatchValidationBackend | None
    patch_commit_store: object | None
    patch_commit_scope_resolver: CommitScopeResolver | None
    web_search_provider: object | None


@dataclass(frozen=True, slots=True)
class FullBuiltinBackends:
    read_only: ReadOnlyBuiltinBackends
    patch_validation: PatchValidationBackend
    patch_commit: PatchCommitBackend
    web_search: WebSearchBackend | None


@dataclass(frozen=True, slots=True)
class ProductionFullToolRuntimeAssembly:
    runtime: ProductionToolRuntimeAssembly
    backends: FullBuiltinBackends


def build_production_read_only_tool_runtime(
    dependencies: ProductionReadOnlyToolRuntimeDependencies,
) -> ProductionReadOnlyToolRuntimeAssembly:
    required = {
        "document_repository": dependencies.document_repository,
        "evidence_repository": dependencies.evidence_repository,
        "artifact_repository": dependencies.artifact_repository,
        "artifact_blob_store": dependencies.artifact_blob_store,
        "embedder": dependencies.embedder,
        "reranker": dependencies.reranker,
        "evidence_config": dependencies.evidence_config,
        "policy": dependencies.policy,
        "approvals": dependencies.approvals,
        "audit": dependencies.audit,
        "rate_limit_repository": dependencies.rate_limit_repository,
        "rate_limit_rules": dependencies.rate_limit_rules,
        "scopes": dependencies.scopes,
        "token_counter": dependencies.token_counter,
    }
    missing = next((name for name, value in required.items() if value is None), None)
    if missing is not None:
        raise ValueError(f"生产环境 只读 工具运行时 依赖{missing}为必填项")

    evidence_service = EvidenceService(
        config=cast(EvidenceConfig, dependencies.evidence_config),
        repository=cast(EvidenceRepository, dependencies.evidence_repository),
        embedder=cast(EvidenceEmbedder, dependencies.embedder),
        reranker=cast(EvidenceReranker, dependencies.reranker),
    )
    backends = ReadOnlyBuiltinBackends(
        documents=DocumentReadBackend(
            cast(CanonicalDocumentRepository, dependencies.document_repository)
        ),
        retrieval=EvidenceRetrievalBackend(evidence_service),
        artifacts=ArtifactBackend(
            repository=cast(ArtifactRepository, dependencies.artifact_repository),
            blob_store=cast(ArtifactBlobStore, dependencies.artifact_blob_store),
        ),
    )
    registration_registry = ToolRegistry()
    definitions = register_read_only_builtins(
        registration_registry,
        documents=backends.documents,
        retrieval=backends.retrieval,
        artifacts=backends.artifacts,
    )
    runtime = build_production_tool_runtime(
        ProductionToolRuntimeDependencies(
            active_definitions=definitions,
            policy=cast(PolicyBoundary, dependencies.policy),
            approvals=cast(ApprovalBoundary, dependencies.approvals),
            audit=cast(AuditBoundary, dependencies.audit),
            rate_limit_repository=cast(RateLimitRepository, dependencies.rate_limit_repository),
            rate_limit_rules=cast(StaticRateLimitRules, dependencies.rate_limit_rules),
            artifacts=backends.artifacts,
            scopes=cast(ScopeStore, dependencies.scopes),
            token_counter=cast(TokenCounter, dependencies.token_counter),
            clock=dependencies.clock,
        )
    )
    return ProductionReadOnlyToolRuntimeAssembly(runtime=runtime, backends=backends)


def build_production_full_tool_runtime(
    dependencies: ProductionFullToolRuntimeDependencies,
) -> ProductionFullToolRuntimeAssembly:
    """构建 read、validation、可选 web 与已批准 Commit Tool 集合。"""

    if dependencies.patch_validation is None:
        raise ValueError("生产环境补丁校验后端为必填项")
    if dependencies.patch_commit_store is None:
        raise ValueError("生产环境 patch 提交 存储 为必填项")
    if dependencies.patch_commit_scope_resolver is None:
        raise ValueError("生产环境补丁提交范围解析器为必填项")
    read_only = build_production_read_only_tool_runtime(dependencies.read_only)
    backends = FullBuiltinBackends(
        read_only=read_only.backends,
        patch_validation=dependencies.patch_validation,
        patch_commit=PatchCommitBackend(
            dependencies.patch_commit_store,
            dependencies.patch_commit_scope_resolver,
        ),
        web_search=(
            None
            if dependencies.web_search_provider is None
            else WebSearchBackend(cast(WebSearchProvider, dependencies.web_search_provider))
        ),
    )
    registry = ToolRegistry()
    definitions = list(
        register_read_only_builtins(
            registry,
            documents=backends.read_only.documents,
            retrieval=backends.read_only.retrieval,
            artifacts=backends.read_only.artifacts,
        )
    )
    definitions.append(register_patch_validation_tool(registry, backend=backends.patch_validation))
    definitions.append(register_patch_commit_tool(registry, backend=backends.patch_commit))
    if backends.web_search is not None:
        definitions.append(register_web_search_tool(registry, backend=backends.web_search))
    base = dependencies.read_only
    runtime = build_production_tool_runtime(
        ProductionToolRuntimeDependencies(
            active_definitions=tuple(definitions),
            policy=cast(PolicyBoundary, base.policy),
            approvals=cast(ApprovalBoundary, base.approvals),
            audit=cast(AuditBoundary, base.audit),
            rate_limit_repository=cast(RateLimitRepository, base.rate_limit_repository),
            rate_limit_rules=cast(StaticRateLimitRules, base.rate_limit_rules),
            artifacts=backends.read_only.artifacts,
            scopes=cast(ScopeStore, base.scopes),
            token_counter=cast(TokenCounter, base.token_counter),
            clock=base.clock,
        )
    )
    return ProductionFullToolRuntimeAssembly(runtime=runtime, backends=backends)


def production_evidence_config(
    *,
    embedding_model: str,
    embedding_dimensions: int,
    reranker_model: str,
) -> EvidenceConfig:
    if not embedding_model.strip() or not reranker_model.strip() or embedding_dimensions <= 0:
        raise ValueError("生产环境 证据 提供方 配置 不完整")
    from docreview.knowledge.evidence_service import EmbeddingProfile, FusionAlgorithm

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
            version="embedding-v1",
            model=embedding_model,
            dimensions=embedding_dimensions,
            vector_type=f"vector({embedding_dimensions})",
            index_version="hnsw-cosine-v1",
        ),
        rerank_enabled=True,
        rerank_profile_version="rerank-v1",
        rerank_model=reranker_model,
        now=lambda: datetime.now(UTC),
    )


__all__ = [
    "FullBuiltinBackends",
    "ProductionFullToolRuntimeAssembly",
    "ProductionFullToolRuntimeDependencies",
    "ProductionReadOnlyToolRuntimeAssembly",
    "ProductionReadOnlyToolRuntimeDependencies",
    "ReadOnlyBuiltinBackends",
    "build_production_full_tool_runtime",
    "build_production_read_only_tool_runtime",
    "production_evidence_config",
]
