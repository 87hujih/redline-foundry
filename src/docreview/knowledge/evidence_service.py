"""带版本的混合 evidence 检索服务。"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import TypeAdapter

from docreview.knowledge.chunking import REVIEW_STRUCTURE_PROFILE


class RetrievalChannel(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class FusionAlgorithm(StrEnum):
    WEIGHTED_SUM = "weighted_sum"
    RRF = "reciprocal_rank_fusion"


class ProcessStage(StrEnum):
    RECALL = "recall"
    FILTER = "filter"
    FUSION = "fusion"
    RERANK = "rerank"
    DEGRADATION = "degradation"


class ProcessStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Candidate:
    source_id: str
    resource_id: str
    version_id: str
    node_id: str
    source_type: str
    content: str
    created_at: datetime
    rerank_text: str = ""
    window_group_id: str = ""
    order_in_section: int = 0


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    workspace_id: str
    resource_id: str
    version_id: str
    source_type: str
    embedding_profile: str
    chunk_profile: str = REVIEW_STRUCTURE_PROFILE.profile_id
    canonical_embedding_profile: str = ""


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    version: str
    model: str
    dimensions: int
    vector_type: str
    index_version: str


@dataclass(frozen=True, slots=True)
class RetrievalRecord:
    channel: RetrievalChannel
    rank: int
    score: float
    index_version: str


@dataclass(frozen=True, slots=True)
class FilterRecord:
    stage: str
    decision: str
    reason: str


@dataclass(frozen=True, slots=True)
class FusionRecord:
    algorithm: FusionAlgorithm
    profile_version: str
    pre_rerank_rank: int
    threshold: float


@dataclass(frozen=True, slots=True)
class RerankRecord:
    enabled: bool
    applied: bool
    profile_version: str
    model: str
    before_rank: int
    after_rank: int
    score: float = 0.0
    degraded_reason: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    retrieval: tuple[RetrievalRecord, ...]
    filtering: tuple[FilterRecord, ...]
    fusion: FusionRecord
    rerank: RerankRecord


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    resource_id: str
    version_id: str
    node_id: str
    source_type: str
    content: str
    content_hash: str
    lexical_score: float
    vector_score: float
    fused_score: float
    trust_level: str
    created_at: datetime
    provenance: EvidenceProvenance
    rerank_text: str = ""
    chunk_id: str = ""
    chunk_profile: str = ""
    window_group_id: str = ""
    order_in_section: int = 0


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    stage: ProcessStage
    status: ProcessStatus
    input_count: int = 0
    output_count: int = 0
    channel: RetrievalChannel | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    schema_version: str
    set_id: str
    workspace_id: str
    resource_id: str
    version_id: str
    query: str
    query_hash: str
    profile_version: str
    created_at: datetime
    evidence: tuple[Evidence, ...]
    process: tuple[ProcessRecord, ...]

    def validate(self) -> None:
        values = (
            self.set_id,
            self.workspace_id,
            self.resource_id,
            self.version_id,
            self.query,
            self.profile_version,
        )
        if self.schema_version != "1.0" or any(not value.strip() for value in values):
            raise ValueError("无效的 证据 集合 身份")
        if (
            not _valid_hash(self.query_hash)
            or not _valid_datetime(self.created_at)
            or not self.process
        ):
            raise ValueError("证据集合的哈希、时间或处理记录无效")
        for record in self.process:
            if (
                not _is_process_stage(record.stage)
                or not _is_process_status(record.status)
                or (record.channel is not None and not _is_retrieval_channel(record.channel))
                or record.input_count < 0
                or record.output_count < 0
            ):
                raise ValueError("无效的 证据 处理 记录")
        seen: set[str] = set()
        for item in self.evidence:
            if item.evidence_id in seen:
                raise ValueError("重复的 证据 ID")
            seen.add(item.evidence_id)
            if (
                item.resource_id != self.resource_id
                or item.version_id != self.version_id
                or item.trust_level != "untrusted"
                or not item.node_id.strip()
                or not item.source_type.strip()
                or not item.content.strip()
                or not _valid_hash(item.content_hash)
                or not _valid_datetime(item.created_at)
                or not all(
                    _valid_score(score)
                    for score in (item.lexical_score, item.vector_score, item.fused_score)
                )
                or not item.provenance.retrieval
                or not item.provenance.filtering
            ):
                raise ValueError("无效的 证据 项")
            for record in item.provenance.retrieval:
                if (
                    not _is_retrieval_channel(record.channel)
                    or record.rank < 1
                    or not _valid_score(record.score)
                    or not record.index_version.strip()
                ):
                    raise ValueError("无效的 证据 检索 来源信息")
            for record in item.provenance.filtering:
                if (
                    not record.stage.strip()
                    or record.decision not in {"included", "excluded"}
                    or not record.reason.strip()
                ):
                    raise ValueError("无效的 证据 过滤 来源信息")
            fusion = item.provenance.fusion
            if (
                not _is_fusion_algorithm(fusion.algorithm)
                or not fusion.profile_version.strip()
                or fusion.pre_rerank_rank < 1
                or not _valid_score(fusion.threshold)
            ):
                raise ValueError("无效的 证据 融合 来源信息")
            rerank = item.provenance.rerank
            if (
                not rerank.profile_version.strip()
                or rerank.before_rank < 1
                or rerank.after_rank < 1
                or not _valid_score(rerank.score)
                or (rerank.applied and (not rerank.enabled or not rerank.model.strip()))
            ):
                raise ValueError("无效的 证据 重排序 来源信息")


_EVIDENCE_SET_ADAPTER = TypeAdapter(EvidenceSet)


def decode_evidence_set(value: object) -> EvidenceSet:
    result = _EVIDENCE_SET_ADAPTER.validate_python(value)
    result.validate()
    return result


@dataclass(frozen=True, slots=True)
class EvidenceConfig:
    profile_version: str
    lexical_enabled: bool
    semantic_enabled: bool
    lexical_index_version: str
    semantic_index_version: str
    candidate_limit: int
    fusion_algorithm: FusionAlgorithm
    lexical_weight: float
    vector_weight: float
    rrf_constant: float
    minimum_fused_score: float
    embedding: EmbeddingProfile
    rerank_enabled: bool
    rerank_profile_version: str
    rerank_model: str
    chunk_profile: str = REVIEW_STRUCTURE_PROFILE.profile_id
    now: DateTimeClock = lambda: datetime.now(UTC)


class DateTimeClock(Protocol):
    def __call__(self) -> datetime: ...


class EvidenceRepository(Protocol):
    async def resolve_scope(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str | None,
        include_history: bool,
    ) -> EvidenceScope: ...

    async def embedding_vector_type(self) -> str: ...

    async def search_lexical(
        self, scope: EvidenceScope, query: str, limit: int
    ) -> list[ScoredCandidate]: ...

    async def search_semantic(
        self,
        scope: EvidenceScope,
        vector: list[float],
        profile: EmbeddingProfile,
        limit: int,
    ) -> list[ScoredCandidate]: ...

    async def list_leading_chunks(
        self, scope: EvidenceScope, limit: int
    ) -> list[ScoredCandidate]: ...


class EvidenceEmbedder(Protocol):
    async def embed_many(
        self,
        texts: list[str],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class EvidenceReranker(Protocol):
    async def rerank(
        self,
        query: str,
        documents: list[str],
        limit: int,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[RerankResult]: ...


class InvalidSearchRequest(ValueError):
    pass


class ScopeNotFound(LookupError):
    pass


class EmbeddingProfileMismatch(RuntimeError):
    pass


class RetrievalUnavailable(RuntimeError):
    pass


def _is_summary_query(query: str) -> bool:
    normalized = query.casefold()
    return any(
        marker in normalized
        for marker in (
            "主要",
            "讲什么",
            "内容",
            "概括",
            "总结",
            "summary",
            "summarize",
            "overview",
            "about",
        )
    )


@dataclass(slots=True)
class _FusedCandidate:
    candidate: Candidate
    lexical_score: float = 0.0
    lexical_rank: int = 0
    vector_score: float = 0.0
    vector_rank: int = 0
    fused_score: float = 0.0


class EvidenceService:
    def __init__(
        self,
        *,
        config: EvidenceConfig,
        repository: EvidenceRepository,
        embedder: EvidenceEmbedder | None,
        reranker: EvidenceReranker | None,
    ) -> None:
        if (
            not config.profile_version.strip()
            or not 0 < config.candidate_limit <= 100
            or not (config.lexical_enabled or config.semantic_enabled)
            or not _valid_score(config.minimum_fused_score)
            or not config.rerank_profile_version.strip()
            or config.chunk_profile != REVIEW_STRUCTURE_PROFILE.profile_id
        ):
            raise ValueError("无效的 证据 服务 配置")
        if config.lexical_enabled and (
            not config.lexical_index_version.strip() or config.lexical_weight <= 0
        ):
            raise ValueError("无效的 词法 检索 配置")
        embedding = config.embedding
        if config.semantic_enabled and (
            embedder is None
            or not config.semantic_index_version.strip()
            or config.vector_weight <= 0
            or not embedding.version.strip()
            or not embedding.model.strip()
            or embedding.dimensions <= 0
            or not embedding.vector_type.strip()
        ):
            raise ValueError("无效的 语义 检索 配置")
        if config.fusion_algorithm is FusionAlgorithm.RRF and config.rrf_constant <= 0:
            raise ValueError("无效的 RRF 常量")
        if config.rerank_enabled and (reranker is None or not config.rerank_model.strip()):
            raise ValueError("无效的 重排序器 配置")
        self._config = config
        self._repository = repository
        self._embedder = embedder
        self._reranker = reranker

    async def search(
        self,
        *,
        workspace_id: str,
        resource_id: str,
        version_id: str | None,
        include_history: bool,
        query: str,
        limit: int,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> EvidenceSet:
        workspace_id = workspace_id.strip()
        resource_id = resource_id.strip()
        version_id = None if version_id is None else version_id.strip()
        query = query.strip()
        if (
            not workspace_id
            or not resource_id
            or not query
            or len(query) > 500
            or not 0 < limit <= 50
            or (include_history and not version_id)
            or (not include_history and version_id is not None)
        ):
            raise InvalidSearchRequest("无效的 证据 搜索 请求")
        scope = await self._repository.resolve_scope(
            workspace_id, resource_id, version_id, include_history
        )
        if (
            not scope.workspace_id.strip()
            or not scope.resource_id.strip()
            or not scope.version_id.strip()
            or scope.workspace_id != workspace_id
            or scope.resource_id != resource_id
            or (include_history and scope.version_id != version_id)
            or scope.chunk_profile != self._config.chunk_profile
            or (
                scope.canonical_embedding_profile
                and scope.canonical_embedding_profile != self._config.embedding.version
            )
        ):
            raise ScopeNotFound("证据 范围 未找到")

        query_hash = _digest(query)
        process: list[ProcessRecord] = []
        lexical: list[ScoredCandidate] = []
        lexical_available = False
        if self._config.lexical_enabled:
            try:
                lexical = await self._repository.search_lexical(
                    scope, query, self._config.candidate_limit
                )
            except Exception:
                process.append(
                    ProcessRecord(
                        ProcessStage.RECALL,
                        ProcessStatus.DEGRADED,
                        channel=RetrievalChannel.LEXICAL,
                        reason="lexical_recall_failed",
                    )
                )
            else:
                lexical_available = True
                process.append(
                    ProcessRecord(
                        ProcessStage.RECALL,
                        ProcessStatus.SUCCEEDED,
                        output_count=len(lexical),
                        channel=RetrievalChannel.LEXICAL,
                    )
                )

        semantic: list[ScoredCandidate] = []
        semantic_available = False
        if self._config.semantic_enabled:
            try:
                vector_type = await self._repository.embedding_vector_type()
            except Exception:
                process.append(
                    ProcessRecord(
                        ProcessStage.RECALL,
                        ProcessStatus.DEGRADED,
                        channel=RetrievalChannel.SEMANTIC,
                        reason="vector_type_lookup_failed",
                    )
                )
            else:
                if (
                    vector_type.strip() != self._config.embedding.vector_type
                    or scope.embedding_profile.strip() != self._config.embedding.version
                ):
                    raise EmbeddingProfileMismatch("嵌入 配置档 与预期不匹配 索引")
                assert self._embedder is not None
                try:
                    async with asyncio.timeout(20):
                        vectors = await self._embedder.embed_many(
                            [query], request_id=request_id, trace_id=trace_id
                        )
                except Exception:
                    process.append(
                        ProcessRecord(
                            ProcessStage.RECALL,
                            ProcessStatus.DEGRADED,
                            channel=RetrievalChannel.SEMANTIC,
                            reason="embedding_provider_failed",
                        )
                    )
                else:
                    if len(vectors) != 1 or len(vectors[0]) != self._config.embedding.dimensions:
                        raise EmbeddingProfileMismatch("嵌入 响应 维度 不匹配")
                    profile = replace(
                        self._config.embedding,
                        index_version=self._config.semantic_index_version,
                    )
                    try:
                        async with asyncio.timeout(20):
                            semantic = await self._repository.search_semantic(
                                scope, vectors[0], profile, self._config.candidate_limit
                            )
                    except Exception:
                        process.append(
                            ProcessRecord(
                                ProcessStage.RECALL,
                                ProcessStatus.DEGRADED,
                                channel=RetrievalChannel.SEMANTIC,
                                reason="semantic_recall_failed",
                            )
                        )
                    else:
                        semantic_available = True
                        process.append(
                            ProcessRecord(
                                ProcessStage.RECALL,
                                ProcessStatus.SUCCEEDED,
                                output_count=len(semantic),
                                channel=RetrievalChannel.SEMANTIC,
                            )
                        )
        if not lexical and not semantic and _is_summary_query(query):
            fallback = getattr(self._repository, "list_leading_chunks", None)
            if fallback is not None:
                try:
                    lexical = await fallback(scope, min(limit, 8))
                except Exception:
                    pass
                else:
                    if lexical:
                        lexical_available = True
                        process.append(
                            ProcessRecord(
                                ProcessStage.DEGRADATION,
                                ProcessStatus.DEGRADED,
                                input_count=0,
                                output_count=len(lexical),
                                channel=RetrievalChannel.LEXICAL,
                                reason="summary_leading_chunks_fallback",
                            )
                        )
        if not lexical_available and not semantic_available:
            raise RetrievalUnavailable("全部 已配置的 检索 通道 为 不可用")

        evidence = self._fuse(scope, lexical, semantic, include_history, process)
        evidence = await self._rerank(
            query,
            evidence,
            limit,
            process,
            request_id=request_id,
            trace_id=trace_id,
        )
        result = EvidenceSet(
            schema_version="1.0",
            set_id=_set_id(scope, query_hash, self._config.profile_version),
            workspace_id=scope.workspace_id,
            resource_id=scope.resource_id,
            version_id=scope.version_id,
            query=query,
            query_hash=query_hash,
            profile_version=self._config.profile_version,
            created_at=self._config.now().astimezone(UTC),
            evidence=tuple(evidence),
            process=tuple(process),
        )
        result.validate()
        return result

    def _fuse(
        self,
        scope: EvidenceScope,
        lexical: list[ScoredCandidate],
        semantic: list[ScoredCandidate],
        historical: bool,
        process: list[ProcessRecord],
    ) -> list[Evidence]:
        candidates: dict[str, _FusedCandidate] = {}
        for channel, values in (
            (RetrievalChannel.LEXICAL, lexical),
            (RetrievalChannel.SEMANTIC, semantic),
        ):
            for rank, scored in enumerate(values, 1):
                item = scored.candidate
                if (
                    not _valid_score(scored.score)
                    or not item.source_id.strip()
                    or not item.node_id.strip()
                    or not item.content.strip()
                    or item.created_at.tzinfo is None
                    or item.resource_id != scope.resource_id
                    or item.version_id != scope.version_id
                ):
                    continue
                candidate = candidates.setdefault(item.source_id, _FusedCandidate(item))
                if channel is RetrievalChannel.LEXICAL:
                    candidate.lexical_score = scored.score
                    candidate.lexical_rank = rank
                else:
                    candidate.vector_score = scored.score
                    candidate.vector_rank = rank
        active_lexical = bool(lexical)
        active_semantic = bool(semantic)
        for candidate in candidates.values():
            candidate.fused_score = self._fused_score(candidate, active_lexical, active_semantic)
        ordered = sorted(
            (
                candidate
                for candidate in candidates.values()
                if candidate.fused_score >= self._config.minimum_fused_score
            ),
            key=lambda candidate: (-candidate.fused_score, candidate.candidate.source_id),
        )
        process.extend(
            (
                ProcessRecord(
                    ProcessStage.FILTER,
                    ProcessStatus.SUCCEEDED,
                    input_count=len(candidates),
                    output_count=len(ordered),
                    reason="minimum_fused_score",
                ),
                ProcessRecord(
                    ProcessStage.FUSION,
                    ProcessStatus.SUCCEEDED,
                    input_count=len(candidates),
                    output_count=len(ordered),
                    reason=self._config.fusion_algorithm.value,
                ),
            )
        )
        scope_reason = "explicit_historical_version" if historical else "current_version"
        evidence: list[Evidence] = []
        for rank, candidate in enumerate(ordered, 1):
            retrieval: list[RetrievalRecord] = []
            if candidate.lexical_rank:
                retrieval.append(
                    RetrievalRecord(
                        RetrievalChannel.LEXICAL,
                        candidate.lexical_rank,
                        candidate.lexical_score,
                        self._config.lexical_index_version,
                    )
                )
            if candidate.vector_rank:
                retrieval.append(
                    RetrievalRecord(
                        RetrievalChannel.SEMANTIC,
                        candidate.vector_rank,
                        candidate.vector_score,
                        self._config.semantic_index_version,
                    )
                )
            item = candidate.candidate
            evidence.append(
                Evidence(
                    evidence_id=_evidence_id(item),
                    resource_id=item.resource_id,
                    version_id=item.version_id,
                    node_id=item.node_id,
                    source_type=item.source_type,
                    content=item.content,
                    content_hash=_digest(item.content),
                    lexical_score=candidate.lexical_score,
                    vector_score=candidate.vector_score,
                    fused_score=candidate.fused_score,
                    trust_level="untrusted",
                    created_at=item.created_at.astimezone(UTC),
                    provenance=EvidenceProvenance(
                        retrieval=tuple(retrieval),
                        filtering=(
                            FilterRecord(
                                "workspace_resource_version_scope", "included", scope_reason
                            ),
                            FilterRecord(
                                "minimum_fused_score",
                                "included",
                                "score_at_or_above_threshold",
                            ),
                        ),
                        fusion=FusionRecord(
                            self._config.fusion_algorithm,
                            self._config.profile_version,
                            rank,
                            self._config.minimum_fused_score,
                        ),
                        rerank=RerankRecord(
                            enabled=self._config.rerank_enabled,
                            applied=False,
                            profile_version=self._config.rerank_profile_version,
                            model=self._config.rerank_model,
                            before_rank=rank,
                            after_rank=rank,
                        ),
                    ),
                    rerank_text=item.rerank_text or item.content,
                    chunk_id=item.source_id,
                    chunk_profile=scope.chunk_profile,
                    window_group_id=item.window_group_id,
                    order_in_section=item.order_in_section,
                )
            )
        return evidence

    async def _rerank(
        self,
        query: str,
        items: list[Evidence],
        limit: int,
        process: list[ProcessRecord],
        *,
        request_id: str | None,
        trace_id: str | None,
    ) -> list[Evidence]:
        if not self._config.rerank_enabled or not items:
            bounded = items[:limit]
            process.append(
                ProcessRecord(
                    ProcessStage.RERANK,
                    ProcessStatus.SKIPPED,
                    input_count=len(items),
                    output_count=len(bounded),
                    reason="rerank_disabled_or_empty",
                )
            )
            return bounded
        assert self._reranker is not None
        try:
            async with asyncio.timeout(20):
                results = await self._reranker.rerank(
                    query,
                    [item.rerank_text or item.content for item in items],
                    min(limit, len(items)),
                    request_id=request_id,
                    trace_id=trace_id,
                )
        except Exception:
            bounded = [
                replace(
                    item,
                    provenance=replace(
                        item.provenance,
                        rerank=replace(item.provenance.rerank, degraded_reason="reranker_failed"),
                    ),
                )
                for item in items[:limit]
            ]
            process.extend(
                (
                    ProcessRecord(
                        ProcessStage.RERANK,
                        ProcessStatus.DEGRADED,
                        input_count=len(items),
                        output_count=len(bounded),
                        reason="reranker_failed",
                    ),
                    ProcessRecord(
                        ProcessStage.DEGRADATION,
                        ProcessStatus.DEGRADED,
                        input_count=len(items),
                        output_count=len(bounded),
                        reason="fusion_order_retained",
                    ),
                )
            )
            return bounded
        ranked: list[Evidence] = []
        seen: set[int] = set()
        for result in results:
            if (
                result.index in seen
                or not 0 <= result.index < len(items)
                or not _valid_score(result.score)
            ):
                continue
            seen.add(result.index)
            item = items[result.index]
            ranked.append(
                replace(
                    item,
                    provenance=replace(
                        item.provenance,
                        rerank=replace(
                            item.provenance.rerank,
                            applied=True,
                            after_rank=len(ranked) + 1,
                            score=result.score,
                        ),
                    ),
                )
            )
            if len(ranked) == limit:
                break
        if not ranked:
            bounded = [
                replace(
                    item,
                    provenance=replace(
                        item.provenance,
                        rerank=replace(
                            item.provenance.rerank,
                            degraded_reason="invalid_reranker_response",
                        ),
                    ),
                )
                for item in items[:limit]
            ]
            process.append(
                ProcessRecord(
                    ProcessStage.RERANK,
                    ProcessStatus.DEGRADED,
                    input_count=len(items),
                    output_count=len(bounded),
                    reason="invalid_reranker_response",
                )
            )
            return bounded
        process.append(
            ProcessRecord(
                ProcessStage.RERANK,
                ProcessStatus.SUCCEEDED,
                input_count=len(items),
                output_count=len(ranked),
                reason=self._config.rerank_profile_version,
            )
        )
        return ranked

    def _fused_score(self, candidate: _FusedCandidate, lexical: bool, semantic: bool) -> float:
        lexical_weight = self._config.lexical_weight if lexical else 0.0
        vector_weight = self._config.vector_weight if semantic else 0.0
        weight = lexical_weight + vector_weight
        if weight == 0:
            return 0.0
        if self._config.fusion_algorithm is FusionAlgorithm.RRF:
            score = 0.0
            if candidate.lexical_rank:
                score += lexical_weight / (self._config.rrf_constant + candidate.lexical_rank)
            if candidate.vector_rank:
                score += vector_weight / (self._config.rrf_constant + candidate.vector_rank)
            maximum = weight / (self._config.rrf_constant + 1)
            return _clamp(score / maximum)
        return _clamp(
            (candidate.lexical_score * lexical_weight + candidate.vector_score * vector_weight)
            / weight
        )


def _valid_score(value: float) -> bool:
    return math.isfinite(value) and 0 <= value <= 1


def _valid_datetime(value: datetime) -> bool:
    return value.year > 1 and value.tzinfo is not None


def _is_process_stage(value: object) -> bool:
    return isinstance(value, ProcessStage)


def _is_process_status(value: object) -> bool:
    return isinstance(value, ProcessStatus)


def _is_retrieval_channel(value: object) -> bool:
    return isinstance(value, RetrievalChannel)


def _is_fusion_algorithm(value: object) -> bool:
    return isinstance(value, FusionAlgorithm)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _valid_hash(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _evidence_id(candidate: Candidate) -> str:
    value = "\0".join(
        (candidate.resource_id, candidate.version_id, candidate.node_id, candidate.source_id)
    )
    return "ev_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _set_id(scope: EvidenceScope, query_hash: str, profile: str) -> str:
    value = "\0".join(
        (scope.workspace_id, scope.resource_id, scope.version_id, query_hash, profile)
    )
    return "evset_" + hashlib.sha256(value.encode()).hexdigest()[:32]


__all__ = [
    "Candidate",
    "EmbeddingProfile",
    "EmbeddingProfileMismatch",
    "Evidence",
    "EvidenceConfig",
    "EvidenceProvenance",
    "EvidenceRepository",
    "EvidenceScope",
    "EvidenceService",
    "EvidenceSet",
    "FilterRecord",
    "FusionAlgorithm",
    "FusionRecord",
    "InvalidSearchRequest",
    "ProcessRecord",
    "ProcessStage",
    "ProcessStatus",
    "RerankRecord",
    "RerankResult",
    "RetrievalChannel",
    "RetrievalRecord",
    "RetrievalUnavailable",
    "ScopeNotFound",
    "ScoredCandidate",
    "decode_evidence_set",
]
