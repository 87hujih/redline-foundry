"""Versioned hybrid retrieval, degradation and citation/provenance contracts."""

# Candidate metadata is intentionally an extensible provenance map.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast


class RetrievalChannel(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class FusionAlgorithm(StrEnum):
    WEIGHTED_SUM = "weighted_sum"
    RRF = "reciprocal_rank_fusion"


@dataclass(frozen=True, slots=True)
class Candidate:
    source_id: str
    resource_id: str
    version_id: str
    node_id: str
    source_type: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class RetrievalRepository(Protocol):
    async def lexical(
        self, workspace_id: str, resource_id: str, version_id: str, query: str, limit: int
    ) -> list[ScoredCandidate]: ...

    async def semantic(
        self, workspace_id: str, resource_id: str, version_id: str, vector: list[float], limit: int
    ) -> list[ScoredCandidate]: ...


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str], limit: int) -> list[RerankResult]: ...


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    profile_version: str = "retrieval-v1"
    lexical_weight: float = 0.5
    semantic_weight: float = 0.5
    threshold: float = 0.0
    algorithm: FusionAlgorithm = FusionAlgorithm.WEIGHTED_SUM
    rerank_enabled: bool = False
    rerank_profile_version: str = "rerank-v1"
    rerank_model: str = ""


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    resource_id: str
    version_id: str
    node_id: str
    snippet: str
    provenance_id: str


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    candidate: Candidate
    lexical_score: float
    semantic_score: float
    fused_score: float
    provenance_id: str
    rerank_score: float = 0.0
    degraded_reason: str = ""


@dataclass(slots=True)
class EvidenceSet:
    workspace_id: str
    resource_id: str
    version_id: str
    query: str
    profile_version: str
    evidence: list[Evidence] = field(default_factory=lambda: list[Evidence]())
    process: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())

    @property
    def query_hash(self) -> str:
        return _digest(self.query)

    @property
    def set_id(self) -> str:
        value = "\0".join(
            (
                self.workspace_id,
                self.resource_id,
                self.version_id,
                self.query_hash,
                self.profile_version,
            )
        )
        return "evset_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _evidence_id(candidate: Candidate) -> str:
    value = "\0".join(
        (candidate.resource_id, candidate.version_id, candidate.node_id, candidate.source_id)
    )
    return "ev_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _fuse(
    config: RetrievalConfig, lexical: list[ScoredCandidate], semantic: list[ScoredCandidate]
) -> list[Evidence]:
    merged: dict[str, dict[str, object]] = {}
    for channel, values in (
        (RetrievalChannel.LEXICAL, lexical),
        (RetrievalChannel.SEMANTIC, semantic),
    ):
        for rank, item in enumerate(values, 1):
            if not (0 <= item.score <= 1) or not item.candidate.content.strip():
                continue
            slot = merged.setdefault(
                item.candidate.source_id,
                {"candidate": item.candidate, "lexical": 0.0, "semantic": 0.0, "lr": 0, "sr": 0},
            )
            slot[channel.value] = item.score
            slot["lr" if channel is RetrievalChannel.LEXICAL else "sr"] = rank
    output: list[Evidence] = []
    active_l = bool(lexical)
    active_s = bool(semantic)
    weight = (config.lexical_weight if active_l else 0.0) + (
        config.semantic_weight if active_s else 0.0
    )
    if weight <= 0:
        return output
    for slot in merged.values():
        if config.algorithm is FusionAlgorithm.RRF:
            value = 0.0
            lexical_rank = cast(int, slot["lr"])
            semantic_rank = cast(int, slot["sr"])
            if lexical_rank:
                value += config.lexical_weight / (60 + lexical_rank)
            if semantic_rank:
                value += config.semantic_weight / (60 + semantic_rank)
            score = value / (weight / 61)
        else:
            score = (
                cast(float, slot["lexical"]) * (config.lexical_weight if active_l else 0.0)
                + cast(float, slot["semantic"]) * (config.semantic_weight if active_s else 0.0)
            ) / weight
        score = max(0.0, min(1.0, score))
        if score < config.threshold:
            continue
        candidate = slot["candidate"]
        assert isinstance(candidate, Candidate)
        provenance_id = _digest(candidate.source_id + "\0" + config.profile_version)
        output.append(
            Evidence(
                _evidence_id(candidate),
                candidate,
                cast(float, slot["lexical"]),
                cast(float, slot["semantic"]),
                score,
                provenance_id,
            )
        )
    output.sort(key=lambda item: (-item.fused_score, item.evidence_id))
    return output


async def retrieve(
    *,
    repository: RetrievalRepository,
    embedder: Embedder | None,
    reranker: Reranker | None,
    workspace_id: str,
    resource_id: str,
    version_id: str,
    query: str,
    limit: int = 5,
    config: RetrievalConfig | None = None,
) -> EvidenceSet:
    config = config or RetrievalConfig()
    if (
        not workspace_id.strip()
        or not resource_id.strip()
        or not version_id.strip()
        or not query.strip()
        or not 0 < limit <= 50
    ):
        raise ValueError("invalid retrieval request")
    result = EvidenceSet(
        workspace_id, resource_id, version_id, query.strip(), config.profile_version
    )
    lexical: list[ScoredCandidate] = []
    semantic: list[ScoredCandidate] = []
    try:
        lexical = await repository.lexical(workspace_id, resource_id, version_id, result.query, 8)
        result.process.append(
            {
                "stage": "recall",
                "channel": "lexical",
                "status": "succeeded",
                "output_count": len(lexical),
            }
        )
    except Exception:
        result.process.append(
            {
                "stage": "recall",
                "channel": "lexical",
                "status": "degraded",
                "reason": "lexical_recall_failed",
            }
        )
    if embedder is not None:
        try:
            vector = await embedder.embed(result.query)
            semantic = await repository.semantic(workspace_id, resource_id, version_id, vector, 8)
            result.process.append(
                {
                    "stage": "recall",
                    "channel": "semantic",
                    "status": "succeeded",
                    "output_count": len(semantic),
                }
            )
        except Exception:
            result.process.append(
                {
                    "stage": "recall",
                    "channel": "semantic",
                    "status": "degraded",
                    "reason": "semantic_recall_failed",
                }
            )
    if not lexical and not semantic:
        raise RuntimeError("all configured retrieval channels unavailable")
    result.evidence = _fuse(config, lexical, semantic)
    if config.rerank_enabled and reranker is not None and result.evidence:
        try:
            ranked = await reranker.rerank(
                result.query,
                [item.candidate.content for item in result.evidence],
                min(limit, len(result.evidence)),
            )
            selected: list[Evidence] = []
            for rank in ranked:
                if (
                    0 <= rank.index < len(result.evidence)
                    and math.isfinite(rank.score)
                    and 0 <= rank.score <= 1
                ):
                    item = result.evidence[rank.index]
                    selected.append(
                        Evidence(
                            item.evidence_id,
                            item.candidate,
                            item.lexical_score,
                            item.semantic_score,
                            item.fused_score,
                            item.provenance_id,
                            rank.score,
                        )
                    )
            if selected:
                result.evidence = selected
                result.process.append(
                    {"stage": "rerank", "status": "succeeded", "output_count": len(selected)}
                )
            else:
                raise ValueError("invalid reranker response")
        except Exception:
            result.process.append(
                {"stage": "rerank", "status": "degraded", "reason": "reranker_failed"}
            )
            result.evidence = [
                Evidence(
                    item.evidence_id,
                    item.candidate,
                    item.lexical_score,
                    item.semantic_score,
                    item.fused_score,
                    item.provenance_id,
                    degraded_reason="reranker_failed",
                )
                for item in result.evidence
            ]
    result.process.append(
        {"stage": "fusion", "status": "succeeded", "output_count": len(result.evidence)}
    )
    result.evidence = result.evidence[:limit]
    return result


def citations(evidence_set: EvidenceSet) -> list[Citation]:
    return [
        Citation(
            f"cite_{index}",
            item.candidate.resource_id,
            item.candidate.version_id,
            item.candidate.node_id,
            item.candidate.content[:200] + ("..." if len(item.candidate.content) > 200 else ""),
            item.provenance_id,
        )
        for index, item in enumerate(evidence_set.evidence, 1)
    ]


__all__ = [
    "Candidate",
    "Citation",
    "Evidence",
    "EvidenceSet",
    "FusionAlgorithm",
    "RerankResult",
    "RetrievalConfig",
    "ScoredCandidate",
    "citations",
    "retrieve",
]
