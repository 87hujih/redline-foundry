from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from docreview.agent_graph.models import JSONObject
from docreview.knowledge.evidence_service import (
    Evidence,
    EvidenceProvenance,
    EvidenceSet,
    FilterRecord,
    FusionAlgorithm,
    FusionRecord,
    ProcessRecord,
    ProcessStage,
    ProcessStatus,
    RerankRecord,
    RetrievalChannel,
    RetrievalRecord,
)
from docreview.storage.postgres.context import PostgresContextCandidateSource
from docreview.tool_runtime.builtin.evidence import evidence_set_json


class Source(PostgresContextCandidateSource):
    def __init__(self) -> None:
        super().__init__(cast(Any, object()))
        self.calls: list[tuple[str, str, str, str, str]] = []

    async def _window_children(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str,
        profile: str,
        group: str,
    ) -> list[tuple[str, str, str, str, str, str, int, tuple[JSONObject, ...]]]:
        self.calls.append((workspace_id, resource_id, version_id, profile, group))
        return [
            (
                "chunk-sibling",
                "resource-1",
                "version-1",
                "node-sibling",
                "Sibling source text",
                "sha256:" + "2" * 64,
                3,
                (
                    cast(
                        JSONObject,
                        {
                            "node_id": "node-sibling",
                            "start_offset": 20,
                            "end_offset": 39,
                            "page_start": 1,
                            "page_end": 1,
                        },
                    ),
                ),
            )
        ]


class ArtifactCursor:
    def __init__(self, content: object) -> None:
        self.content = content
        self.params: tuple[object, ...] = ()

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        assert "FROM agent_artifacts" in query
        self.params = params

    async def fetchone(self) -> tuple[object, ...]:
        return (self.content,)


def evidence_set() -> EvidenceSet:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    provenance = EvidenceProvenance(
        retrieval=(RetrievalRecord(RetrievalChannel.LEXICAL, 1, 0.9, "lex-v1"),),
        filtering=(FilterRecord("scope", "included", "current_version"),),
        fusion=FusionRecord(FusionAlgorithm.WEIGHTED_SUM, "retrieval-v1", 1, 0.0),
        rerank=RerankRecord(True, True, "rerank-v1", "model", 1, 1, 0.95),
    )
    evidence = Evidence(
        evidence_id="ev_" + "1" * 32,
        resource_id="resource-1",
        version_id="version-1",
        node_id="node-hit",
        source_type="canonical_chunk",
        content="Matched source text",
        content_hash="sha256:" + "1" * 64,
        lexical_score=0.9,
        vector_score=0.0,
        fused_score=0.9,
        trust_level="untrusted",
        created_at=now,
        provenance=provenance,
        chunk_id="chunk-hit",
        chunk_profile="docreview-review-structure-2026-08-17",
        window_group_id="win_" + "a" * 32,
        order_in_section=2,
    )
    return EvidenceSet(
        "1.0",
        "evset_" + "b" * 32,
        "workspace-1",
        "resource-1",
        "version-1",
        "query",
        "sha256:" + "3" * 64,
        "retrieval-v1",
        now,
        (evidence,),
        (ProcessRecord(ProcessStage.RERANK, ProcessStatus.SUCCEEDED, output_count=1),),
    )


@pytest.mark.asyncio
async def test_context_window_expansion_preserves_precise_retrieval_evidence() -> None:
    source = Source()
    payload = {"output": {"evidence_set": evidence_set_json(evidence_set())}}

    items = await source.evidence_items("workspace-1", "resource-1", payload)

    assert items is not None
    assert [item.source_id for item in items] == ["ev_" + "1" * 32, "chunk-sibling"]
    assert [item.node_id for item in items] == ["node-hit", "node-sibling"]
    assert items[1].source_spans[0]["page_start"] == 1
    assert items[1].selected_reason == "parent window sibling expanded after child rerank"
    assert source.calls == [
        (
            "workspace-1",
            "resource-1",
            "version-1",
            "docreview-review-structure-2026-08-17",
            "win_" + "a" * 32,
        )
    ]


@pytest.mark.asyncio
async def test_artifactized_retrieval_is_rehydrated_into_evidence_context() -> None:
    source = Source()
    cursor = ArtifactCursor({"evidence_set": evidence_set_json(evidence_set())})
    payload = {
        "output": {
            "truncated": True,
            "artifact_id": "artifact-1",
            "summary": {"kind": "evidence_set"},
        }
    }

    items = await source.evidence_items(
        "workspace-1",
        "resource-1",
        payload,
        run_id="run-1",
        step_id="step-1",
        cursor=cast(Any, cursor),
    )

    assert items is not None
    assert items[0].content == "Matched source text"
    assert cursor.params == ("artifact-1", "workspace-1", "run-1", "step-1")
