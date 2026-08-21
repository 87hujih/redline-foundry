"""生产 EvidenceService 的可信 scope 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from docreview.knowledge.evidence_service import (
    EmbeddingProfileMismatch,
    Evidence,
    EvidenceSet,
    InvalidSearchRequest,
    ProcessRecord,
    ProcessStage,
    ProcessStatus,
    RetrievalUnavailable,
    ScopeNotFound,
)
from docreview.tool_runtime.models import (
    BackendRequest,
    Provenance,
    ToolBackendFailure,
    ToolErrorCategory,
    ToolResult,
)
from docreview.tool_runtime.schema import JSONObject, JSONValue


class EvidenceSearchService(Protocol):
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
    ) -> EvidenceSet: ...


class EvidenceRetrievalBackend:
    def __init__(self, service: EvidenceSearchService) -> None:
        self._service = service

    async def execute(self, request: BackendRequest) -> ToolResult:
        resource_id = _required_string(request.tool_input.get("resource_id"), "resource_id")
        if resource_id != request.context.resource_id:
            raise ToolBackendFailure(
                ToolErrorCategory.UNAUTHORIZED,
                "检索资源与可信执行范围不匹配",
            )
        version_value = request.tool_input.get("version_id")
        version_id = (
            None
            if version_value is None or version_value == ""
            else _required_string(version_value, "version_id")
        )
        history_value = request.tool_input.get("include_history", False)
        if not isinstance(history_value, bool):
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "检索历史范围无效")
        query = _required_string(request.tool_input.get("query"), "query")
        limit = request.tool_input.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "检索 限制 无效")
        try:
            evidence_set = await self._service.search(
                workspace_id=request.context.workspace_id,
                resource_id=resource_id,
                version_id=version_id,
                include_history=history_value,
                query=query,
                limit=limit,
                request_id=request.context.request_id,
                trace_id=request.context.trace_id,
            )
        except InvalidSearchRequest as error:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "证据搜索请求无效") from error
        except ScopeNotFound as error:
            raise ToolBackendFailure(ToolErrorCategory.NOT_FOUND, "证据 范围 未找到") from error
        except EmbeddingProfileMismatch as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "嵌入配置档不匹配"
            ) from error
        except RetrievalUnavailable as error:
            raise ToolBackendFailure(
                ToolErrorCategory.RETRYABLE_UPSTREAM, "证据 检索 不可用"
            ) from error
        except ToolBackendFailure:
            raise
        except Exception as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "证据 检索 失败"
            ) from error
        try:
            evidence_set.validate()
        except ValueError as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "invalid evidence set"
            ) from error
        if (
            evidence_set.workspace_id != request.context.workspace_id
            or evidence_set.resource_id != resource_id
            or (history_value and evidence_set.version_id != version_id)
        ):
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE,
                "证据集与可信执行范围不匹配",
            )
        provenance = tuple(
            Provenance(
                source_type=item.source_type,
                source_id=item.evidence_id,
                resource_id=item.resource_id,
                version_id=item.version_id,
                content_hash=item.content_hash,
                trust_level="untrusted",
            )
            for item in evidence_set.evidence
        )
        if not provenance:
            provenance = (
                Provenance(
                    source_type="retrieval",
                    source_id=evidence_set.set_id,
                    resource_id=evidence_set.resource_id,
                    version_id=evidence_set.version_id,
                    trust_level="untrusted",
                ),
            )
        return ToolResult(
            output={"evidence_set": evidence_set_json(evidence_set)},
            provenance=provenance,
            oversize_summary=_oversize_summary(evidence_set),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        return None


def evidence_set_json(evidence_set: EvidenceSet) -> JSONObject:
    result: JSONObject = {
        "schema_version": evidence_set.schema_version,
        "set_id": evidence_set.set_id,
        "workspace_id": evidence_set.workspace_id,
        "resource_id": evidence_set.resource_id,
        "version_id": evidence_set.version_id,
        "query": evidence_set.query,
        "query_hash": evidence_set.query_hash,
        "profile_version": evidence_set.profile_version,
        "created_at": _timestamp(evidence_set.created_at),
        "evidence": cast(list[JSONValue], [_evidence_json(item) for item in evidence_set.evidence]),
        "process": cast(list[JSONValue], [_process_json(item) for item in evidence_set.process]),
    }
    return result


def _evidence_json(item: Evidence) -> JSONObject:
    rerank: JSONObject = {
        "enabled": item.provenance.rerank.enabled,
        "applied": item.provenance.rerank.applied,
        "profile_version": item.provenance.rerank.profile_version,
        "before_rank": item.provenance.rerank.before_rank,
        "after_rank": item.provenance.rerank.after_rank,
        "score": item.provenance.rerank.score,
    }
    if item.provenance.rerank.model:
        rerank["model"] = item.provenance.rerank.model
    if item.provenance.rerank.degraded_reason:
        rerank["degraded_reason"] = item.provenance.rerank.degraded_reason
    result: JSONObject = {
        "evidence_id": item.evidence_id,
        "resource_id": item.resource_id,
        "version_id": item.version_id,
        "node_id": item.node_id,
        "source_type": item.source_type,
        "content": item.content,
        "content_hash": item.content_hash,
        "lexical_score": item.lexical_score,
        "vector_score": item.vector_score,
        "fused_score": item.fused_score,
        "trust_level": item.trust_level,
        "created_at": _timestamp(item.created_at),
        "provenance": {
            "retrieval": cast(
                list[JSONValue],
                [
                    {
                        "channel": record.channel.value,
                        "rank": record.rank,
                        "score": record.score,
                        "index_version": record.index_version,
                    }
                    for record in item.provenance.retrieval
                ],
            ),
            "filtering": cast(
                list[JSONValue],
                [
                    {
                        "stage": record.stage,
                        "decision": record.decision,
                        "reason": record.reason,
                    }
                    for record in item.provenance.filtering
                ],
            ),
            "fusion": {
                "algorithm": item.provenance.fusion.algorithm.value,
                "profile_version": item.provenance.fusion.profile_version,
                "pre_rerank_rank": item.provenance.fusion.pre_rerank_rank,
                "threshold": item.provenance.fusion.threshold,
            },
            "rerank": rerank,
        },
    }
    if item.chunk_id:
        result["chunk_id"] = item.chunk_id
    if item.chunk_profile:
        result["chunk_profile"] = item.chunk_profile
    if item.window_group_id:
        result["window_group_id"] = item.window_group_id
    if item.order_in_section > 0:
        result["order_in_section"] = item.order_in_section
    return result


def _process_json(record: ProcessRecord) -> JSONObject:
    value: JSONObject = {
        "stage": record.stage.value,
        "status": record.status.value,
        "input_count": record.input_count,
        "output_count": record.output_count,
    }
    if record.channel is not None:
        value["channel"] = record.channel.value
    if record.reason:
        value["reason"] = record.reason
    return value


def _oversize_summary(evidence_set: EvidenceSet) -> JSONObject:
    citations: list[JSONValue] = []
    for item in evidence_set.evidence[:12]:
        citations.append(
            {
                "evidence_id": item.evidence_id,
                "resource_id": item.resource_id,
                "version_id": item.version_id,
                "node_id": item.node_id,
                "content_hash": item.content_hash,
                "fused_score": item.fused_score,
                "trust_level": item.trust_level,
            }
        )
    degradations: list[JSONValue] = []
    seen: set[str] = set()
    for record in evidence_set.process:
        if (
            record.stage is ProcessStage.DEGRADATION
            and record.status is ProcessStatus.DEGRADED
            and record.channel is not None
            and record.channel.value not in seen
        ):
            seen.add(record.channel.value)
            degradations.append(record.channel.value)
    return {
        "kind": "evidence_set",
        "schema_version": evidence_set.schema_version,
        "set_id": evidence_set.set_id,
        "resource_id": evidence_set.resource_id,
        "version_id": evidence_set.version_id,
        "profile_version": evidence_set.profile_version,
        "evidence_count": len(evidence_set.evidence),
        "citations": citations,
        "degradations": degradations,
    }


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, f"{label} 无效")
    return value


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["EvidenceRetrievalBackend", "EvidenceSearchService", "evidence_set_json"]
