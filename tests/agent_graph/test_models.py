from __future__ import annotations

import pytest
from pydantic import ValidationError

from docreview.agent_graph.codec import decode_model, decode_unique_object
from docreview.agent_graph.models import (
    ActionKind,
    Decision,
    Finding,
    Patch,
    PatchOperation,
    PatchOperationKind,
)


def test_model_boundary_rejects_unknown_duplicate_and_trailing_json() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        decode_unique_object('{"action":"finish","action":"analyze"}')
    with pytest.raises(ValueError, match="Extra inputs"):
        decode_model(
            '{"action":"finish","reason":"done","tool_input":{},'
            '"expected_observation":"outcome","confidence":0.9,"extra":true}',
            Decision,
        )
    with pytest.raises(ValueError, match="exactly one"):
        decode_unique_object('{"value":{}} {"trailing":true}')


def test_decision_rejects_illegal_action_and_non_object_tool_input() -> None:
    with pytest.raises(ValidationError):
        Decision.model_validate(
            {
                "action": "not_an_action",
                "reason": "bad",
                "tool_input": {},
                "expected_observation": "none",
                "confidence": 0.2,
            }
        )
    with pytest.raises(ValidationError):
        decode_model(
            '{"action":"finish","reason":"done","tool_input":{},'
            '"expected_observation":"outcome","confidence":"1"}',
            Decision,
        )
    with pytest.raises(ValidationError):
        Decision.model_validate(
            {
                "action": ActionKind.RETRIEVE_EVIDENCE,
                "reason": "need evidence",
                "tool_name": "retrieval.search",
                "tool_input": [],
                "expected_observation": "evidence",
                "confidence": 0.9,
            }
        )


def test_patch_and_finding_models_are_closed_and_bounded() -> None:
    operation = PatchOperation(
        op=PatchOperationKind.REPLACE_NODE,
        node_id="node-1",
        expected_hash="sha256:" + "a" * 64,
        content="updated",
    )
    patch = Patch(
        schema_version="1.0",
        resource_id="resource-1",
        base_version_id="version-1",
        operations=(operation,),
        evidence_refs=("evidence-1",),
        reason="fix wording",
    )
    assert patch.operations[0].op is PatchOperationKind.REPLACE_NODE
    with pytest.raises(ValidationError):
        Finding(
            finding_id="finding-1",
            summary="bad duplicate evidence",
            evidence_ids=("evidence-1", "evidence-1"),
            confidence=0.5,
        )
    with pytest.raises(ValidationError):
        Patch.model_validate({**patch.model_dump(), "unknown": True})
