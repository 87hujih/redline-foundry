"""生产 repository 与持久化 HTTP 边界装配。

本模块保持显式依赖：所有 SQL 兼容 repository 都连接到一个自有 pool，拒绝
构造不完整的生产 Graph。Runtime/Projection worker executor 由持久化 Runtime
装配提供，因为它们依赖配置好的 LangGraph/Tool 边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from docreview.agent_graph.assembly import (
    ProductionGraphAssembly,
    build_production_graph_executor,
)
from docreview.agent_graph.boundary import ProjectRuntimeBoundary
from docreview.agent_graph.production import (
    PostgresBudgetReader,
    PostgresGraphFactStore,
    ProductionGraphCommitter,
    ProductionGraphToolRuntime,
)
from docreview.agent_graph.runtime import RuntimeBoundary as GraphRuntimeBoundary
from docreview.api.dependencies import AppDependencies
from docreview.config.settings import Settings
from docreview.context.assembler import (
    ContextAssembler,
    ContextConfig,
    ContextLayer,
    ManagedContextAssembler,
)
from docreview.context.storage import RepositoryManifestStore
from docreview.document.upload import DocumentUploadService
from docreview.identity.policy import PolicyResolver
from docreview.identity.trusted_ingress import TrustedIngressAdapter
from docreview.knowledge.legacy_search import LegacySearchService
from docreview.providers.assembly import ProductionProviderDependencies
from docreview.providers.embedding import ChunkEmbeddingProjector
from docreview.providers.reranker import RerankResult
from docreview.runtime.assembly import build_production_durable_runtime
from docreview.runtime.embedding import ChunkEmbeddingWorker
from docreview.runtime.engine import RuntimeExecutor
from docreview.runtime.lifecycle import RuntimeLifecycle
from docreview.storage.postgres.agent_queries import AgentQueryRepository
from docreview.storage.postgres.assistant import AssistantRepository
from docreview.storage.postgres.builtin import (
    PostgresCanonicalDocumentRepository,
    PostgresCommitScopeResolver,
    PostgresEvidenceRepository,
    PostgresValidationRequestFactory,
)
from docreview.storage.postgres.chunk_embedding import PostgresPendingEmbeddingRepository
from docreview.storage.postgres.context import PostgresContextCandidateSource
from docreview.storage.postgres.document_commit import PostgresCommitStore
from docreview.storage.postgres.identity import IdentityRepository
from docreview.storage.postgres.pool import DatabasePool
from docreview.storage.postgres.resources import ResourceRepository
from docreview.storage.postgres.runtime_projection_repository import RuntimeProjectionRepository
from docreview.storage.postgres.runtime_repository import RuntimeRepository
from docreview.storage.postgres.tool_runtime import (
    LocalArtifactBlobStore,
    PostgresApprovalStore,
    PostgresArtifactRepository,
    PostgresToolAudit,
    PostgresToolPolicy,
    PostgresToolScopeStore,
)
from docreview.storage.postgres.turn import TurnRepository
from docreview.storage.postgres.upload_write import UploadMetadataRepository
from docreview.storage.postgres.uploaded_files import UploadedFileRepository
from docreview.tool_runtime.builtin.assembly import (
    ProductionFullToolRuntimeDependencies,
    ProductionReadOnlyToolRuntimeDependencies,
    build_production_full_tool_runtime,
    production_evidence_config,
)
from docreview.tool_runtime.builtin.patch import PatchValidationBackend
from docreview.tool_runtime.postgres import PooledPostgresRateLimitRepository
from docreview.tool_runtime.rate_limit import RateLimitRule, StaticRateLimitRules
from docreview.tool_runtime.token_counter import JSONTokenCounter
from docreview.turn.coordinator import TurnCoordinator
from docreview.turn.pipeline import DurableRunner


@dataclass(frozen=True, slots=True)
class ProductionRepositoryAssembly:
    dependencies: AppDependencies
    resources: ResourceRepository
    runs: AgentQueryRepository
    assistant: AssistantRepository
    identity: IdentityRepository
    turns: TurnRepository
    runtime: RuntimeRepository
    projection: RuntimeProjectionRepository
    uploads: UploadMetadataRepository
    uploaded_files: UploadedFileRepository
    graph: ProductionGraphAssembly | None


def build_production_project_runtime_boundary(
    settings: Settings,
    *,
    pool: DatabasePool,
    providers: ProductionProviderDependencies,
    runtime: RuntimeRepository,
    identity: IdentityRepository,
    canonical_committer: PostgresCommitStore,
) -> ProjectRuntimeBoundary:
    """构造完整且缺依赖即 fail-closed 的 Graph 依赖闭包。"""

    worker_id = (settings.runtime_worker_id or "").strip()
    if not worker_id:
        raise ValueError("生产环境 ProjectRuntimeBoundary 需要 RUNTIME_WORKER_ID")
    if (
        settings.embedding_model is None
        or settings.embedding_dim is None
        or settings.reranker_model is None
    ):
        raise ValueError("生产环境 ProjectRuntimeBoundary 提供方 配置档 不完整")

    scopes = PostgresToolScopeStore(pool)
    policy = PostgresToolPolicy(PolicyResolver(identity))
    approvals = PostgresApprovalStore(runtime)
    audit = PostgresToolAudit(
        runtime,
        worker_id=worker_id,
        lease_duration=timedelta(seconds=60),
    )
    artifacts = PostgresArtifactRepository(pool)
    artifact_blobs = LocalArtifactBlobStore(providers.file_store)
    commit_scopes = PostgresCommitScopeResolver(pool)
    patch_validation = PatchValidationBackend(
        PostgresValidationRequestFactory(canonical_committer, commit_scopes)
    )
    evidence_config = production_evidence_config(
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dim,
        reranker_model=settings.reranker_model,
    )
    tools = build_production_full_tool_runtime(
        ProductionFullToolRuntimeDependencies(
            read_only=ProductionReadOnlyToolRuntimeDependencies(
                document_repository=PostgresCanonicalDocumentRepository(pool),
                evidence_repository=PostgresEvidenceRepository(pool),
                artifact_repository=artifacts,
                artifact_blob_store=artifact_blobs,
                embedder=providers.embedder,
                reranker=providers.reranker,
                evidence_config=evidence_config,
                policy=policy,
                approvals=approvals,
                audit=audit,
                rate_limit_repository=PooledPostgresRateLimitRepository(pool),
                rate_limit_rules=StaticRateLimitRules(
                    default=RateLimitRule(120, timedelta(minutes=1)),
                    by_risk={},
                ),
                scopes=scopes,
                token_counter=JSONTokenCounter(),
            ),
            patch_validation=patch_validation,
            patch_commit_store=canonical_committer,
            patch_commit_scope_resolver=commit_scopes,
            web_search_provider=providers.web_search,
        )
    )
    manifest_store = RepositoryManifestStore(runtime)
    context_tokenizer = providers.chunk_tokenizer
    if context_tokenizer is None:
        raise ValueError("生产环境 ContextManifest 装配 需要 嵌入 分词器")
    contexts = ManagedContextAssembler(
        ContextAssembler(
            ContextConfig(
                tokenizer=context_tokenizer,
                token_budget=8192,
                reserved_output_tokens=2048,
                layer_budgets={
                    ContextLayer.CONTROL: 512,
                    ContextLayer.TASK: 1024,
                    ContextLayer.WORKING_MEMORY: 2048,
                    ContextLayer.EVIDENCE: 3072,
                    ContextLayer.CONVERSATION: 1024,
                    ContextLayer.ARTIFACT_REFERENCE: 512,
                },
            ),
            manifest_store,
        ),
        manifest_store,
        PostgresContextCandidateSource(pool),
    )
    facts = PostgresGraphFactStore(pool)
    graph_tools = ProductionGraphToolRuntime(
        executor=tools.runtime.executor,
        runtime=tools.runtime.runtime,
        scopes=scopes,
        facts=facts,
    )
    return ProjectRuntimeBoundary(
        models=providers.model_gateway,
        contexts=contexts,
        tools=graph_tools,
        committer=ProductionGraphCommitter(graph_tools),
        facts=facts,
        budgets=PostgresBudgetReader(pool),
    )


class _EmbeddingQueryAdapter:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    async def embed(self, query: str) -> str | None:
        values = await self._provider.embed(query)  # type: ignore[attr-defined]
        if not values:
            return None
        typed_values = cast(list[float], values)
        return "[" + ",".join(str(float(item)) for item in typed_values) + "]"


class _RerankerQueryAdapter:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        values = await self._provider.rerank(query, documents, top_n)  # type: ignore[attr-defined]
        typed_values = cast(list[RerankResult], values)
        return [item.index for item in typed_values]


def assemble_production_repositories(
    settings: Settings,
    *,
    pool: DatabasePool,
    providers: ProductionProviderDependencies,
    runtime_executor: RuntimeExecutor | None = None,
    checkpointer: object | None = None,
    runtime_boundary: GraphRuntimeBoundary | None = None,
) -> ProductionRepositoryAssembly:
    """创建完整的 SQL-backed HTTP 兼容 Graph。

    ``pool`` 与 ``providers`` 的打开和关闭由调用方负责。本函数不建立连接，
    因此可安全地在测试中检查。
    """

    if settings.app_env == "production" and not pool.opened:
        raise RuntimeError("生产环境仓库装配需要已打开的连接池")
    resources = ResourceRepository(pool)
    runs = AgentQueryRepository(pool)
    assistant = AssistantRepository(pool)
    identity = IdentityRepository(pool)
    turns = TurnRepository(pool)
    runtime = RuntimeRepository(pool)
    projection = RuntimeProjectionRepository(pool)
    uploaded_files = UploadedFileRepository(pool)
    tokenizer = providers.chunk_tokenizer
    if tokenizer is None:
        raise ValueError("生产环境 仓库 装配 需要 嵌入 分词器")
    uploads = UploadMetadataRepository(
        pool,
        tokenizer=tokenizer,
        require_exact_tokenizer=True,
    )
    canonical_committer = PostgresCommitStore(
        pool,
        tokenizer=tokenizer,
        require_exact_tokenizer=True,
    )
    ingress = settings.trusted_ingress
    if ingress is None:
        raise ValueError("生产环境 仓库 装配 需要 可信 入口")
    identity_adapter = TrustedIngressAdapter(
        secret=ingress.secret.get_secret_value(),
        trust_source=ingress.source,
        max_age=timedelta(milliseconds=ingress.max_age_ms),
    )
    search = LegacySearchService(
        resources,
        embedder=_EmbeddingQueryAdapter(providers.embedder),
        reranker=_RerankerQueryAdapter(providers.reranker),
    )
    uploader = DocumentUploadService(
        parser=providers.document_parser,
        store=providers.file_store,
        metadata=uploads,
    )
    coordinator = TurnCoordinator(turns)
    pipeline = DurableRunner(
        coordinator,
        turns,
        poll_interval=0.25,
        max_wait=300.0,
    )
    runtime_lifecycle = None
    runtime_engine = None
    projection_worker = None
    embedding_worker = None
    graph = None
    if settings.runtime_worker_enabled:
        if runtime_boundary is None and runtime_executor is None:
            runtime_boundary = build_production_project_runtime_boundary(
                settings,
                pool=pool,
                providers=providers,
                runtime=runtime,
                identity=identity,
                canonical_committer=canonical_committer,
            )
        if runtime_executor is None and runtime_boundary is not None:
            graph = build_production_graph_executor(pool=pool, boundary=runtime_boundary)
            runtime_executor = graph.executor
            checkpointer = graph.checkpointer
        if runtime_executor is None or checkpointer is None:
            raise ValueError(
                "生产环境运行时需要 ProjectRuntimeBoundary, 或配套的图执行器与持久化检查点保存器"
            )
        durable = build_production_durable_runtime(
            pool=pool,
            executor=runtime_executor,
            worker_id=settings.runtime_worker_id or "",
        )
        runtime_lifecycle = durable.lifecycle
        runtime_engine = durable.engine
        projection_worker = durable.projection_worker
        if (
            providers.chunk_tokenizer is None
            or settings.embedding_model is None
            or settings.embedding_dim is None
        ):
            raise ValueError("生产环境 嵌入 投影需要模型、维度和分词器")
        embedding_projector = ChunkEmbeddingProjector(
            repository=PostgresPendingEmbeddingRepository(pool),
            provider=providers.embedder,
            tokenizer=providers.chunk_tokenizer,
            embedding_profile="embedding-v1",
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dim,
            retrieval_index_version="hnsw-cosine-v1",
        )
        embedding_worker = ChunkEmbeddingWorker(
            embedding_projector,
            worker_id=(settings.runtime_worker_id or "runtime") + ":embedding",
        )
        runtime_lifecycle = RuntimeLifecycle(
            durable.runtime_worker,
            durable.projection_worker,
            timedelta(milliseconds=250),
            embedding_worker,
        )
    dependencies = AppDependencies(
        identity_adapter=identity_adapter,
        resources=resources,
        resource_search=search,
        run_queries=runs,
        approval_queries=runs,
        assistant=assistant,
        assistant_resource_selection=assistant,
        assistant_writer=assistant,
        uploaded_files=uploaded_files,
        file_store=providers.file_store,
        upload_policy_extensions=list(providers.document_parser.supported_extensions),
        upload_max_bytes=settings.upload_max_bytes,
        assistant_uploader=uploader,
        turn_pipeline=pipeline,
        approval_decider=runtime,
        runtime_lifecycle=runtime_lifecycle,
        providers=providers,
        database_pool=pool,
        runtime_engine=runtime_engine,
        runtime_executor=runtime_executor,
        runtime_boundary=runtime_boundary,
        projection_worker=projection_worker,
        checkpointer=checkpointer,
        canonical_committer=canonical_committer,
    )
    return ProductionRepositoryAssembly(
        dependencies=dependencies,
        resources=resources,
        runs=runs,
        assistant=assistant,
        identity=identity,
        turns=turns,
        runtime=runtime,
        projection=projection,
        uploads=uploads,
        uploaded_files=uploaded_files,
        graph=graph,
    )


__all__ = [
    "ProductionRepositoryAssembly",
    "assemble_production_repositories",
    "build_production_project_runtime_boundary",
]
