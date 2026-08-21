from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false
from typing import Any

import pytest
from langgraph.types import Command

from docreview.agent_graph.checkpoint import InMemoryCheckpointRepository, ProjectCheckpointer
from docreview.agent_graph.graph import build_graph
from docreview.agent_graph.models import (
    Action,
    ActionKind,
    BudgetSnapshot,
    Decision,
    GraphState,
    NodeName,
)


def initial_state(*, findings: bool = False) -> GraphState:
    from docreview.agent_graph.models import FindingRef

    return GraphState(
        run_id="run-1",
        request_fact_id="request-1",
        budget=BudgetSnapshot(fact_id="budget-1", steps_remaining=64, tool_calls_remaining=32),
        finding_refs=(
            FindingRef(
                finding_id="finding-1",
                fact_id="finding-fact-1",
                content_hash="sha256:" + "1" * 64,
            ),
        )
        if findings
        else (),
    )


def resume_until(
    graph: Any, state: GraphState, responses: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    config = {
        "configurable": {"thread_id": state.run_id, "run_id": state.run_id, "checkpoint_ns": ""}
    }
    result = graph.invoke(state.model_dump(mode="json"), config)
    while result.get("__interrupt__"):
        request = result["__interrupt__"][0].value
        response = responses[request["operation"]]
        result = graph.invoke(
            Command(resume={"request_id": request["request_id"], **response}), config
        )
    while result["current_node"] != NodeName.END:
        result = graph.invoke(None, config)
        while result.get("__interrupt__"):
            request = result["__interrupt__"][0].value
            response = responses[request["operation"]]
            result = graph.invoke(
                Command(resume={"request_id": request["request_id"], **response}), config
            )
    return result


def budget() -> dict[str, Any]:
    return {
        "fact_id": "budget-1",
        "steps_remaining": 63,
        "tool_calls_remaining": 31,
        "tokens_remaining": None,
        "cost_remaining": None,
        "deadline_exceeded": False,
        "exhausted_reason": None,
    }


def test_graph_success_path_is_checkpointed_and_side_effect_free() -> None:
    graph = build_graph(checkpointer=ProjectCheckpointer(InMemoryCheckpointRepository()))
    result = resume_until(
        graph,
        initial_state(),
        {
            "understand_goal": {
                "budget": budget(),
                "data": {
                    "goal": {
                        "objective": "review",
                        "constraints": [],
                        "expected_output": "outcome",
                    },
                    "context_manifest_id": "manifest-1",
                },
            },
            "assemble_context": {
                "budget": budget(),
                "data": {"context_manifest_id": "manifest-1"},
            },
            "decide_next_action": {
                "budget": budget(),
                "data": {
                    "decision": {
                        "action": "finish",
                        "reason": "done",
                        "tool_input": {},
                        "expected_observation": "outcome",
                        "confidence": 1.0,
                    }
                },
            },
            "render_outcome": {
                "budget": budget(),
                "data": {
                    "outcome": {
                        "fact_id": "outcome-fact",
                        "artifact_id": "outcome-artifact",
                        "content_hash": "sha256:" + "2" * 64,
                    }
                },
            },
        },
    )
    assert result["current_node"] == "End"
    assert result["outcome_ref"].fact_id == "outcome-fact"


def test_graph_stops_before_decision_model_on_no_progress() -> None:
    state = GraphState.model_validate(
        initial_state().model_dump(mode="json")
        | {
            "current_node": "DecideNextAction",
            "goal": {"objective": "review", "constraints": [], "expected_output": "outcome"},
            "context_manifest_id": "manifest-1",
            "consecutive_no_progress": 3,
        }
    )
    graph = build_graph(checkpointer=ProjectCheckpointer(InMemoryCheckpointRepository()))
    result = resume_until(
        graph,
        state,
        {
            "render_outcome": {
                "budget": budget(),
                "data": {
                    "outcome": {
                        "fact_id": "outcome-fact",
                        "artifact_id": "outcome-artifact",
                        "content_hash": "sha256:" + "3" * 64,
                    }
                },
            },
        },
    )
    assert result["stop_reason"] == "no_new_information"


def test_graph_finishes_when_model_repeats_analyze_after_findings() -> None:
    state = GraphState.model_validate(
        initial_state(findings=True).model_dump(mode="json")
        | {
            "current_node": "DecideNextAction",
            "goal": {"objective": "summarize", "constraints": [], "expected_output": "answer"},
            "context_manifest_id": "manifest-1",
        }
    )
    graph = build_graph(checkpointer=ProjectCheckpointer(InMemoryCheckpointRepository()))
    result = resume_until(
        graph,
        state,
        {
            "decide_next_action": {
                "budget": budget(),
                "data": {
                    "decision": {
                        "action": "analyze",
                        "reason": "analyze again",
                        "tool_input": {},
                        "expected_observation": "more analysis",
                        "confidence": 0.8,
                    }
                },
            },
            "render_outcome": {
                "budget": budget(),
                "data": {
                    "outcome": {
                        "fact_id": "outcome-fact",
                        "artifact_id": "outcome-artifact",
                        "content_hash": "sha256:" + "4" * 64,
                    }
                },
            },
        },
    )

    assert result["current_node"] == "End"
    assert result["last_decision"]["action"] == ActionKind.FINISH
    assert result["outcome_ref"].artifact_id == "outcome-artifact"


def observation(index: int, *, novel: bool = True) -> dict[str, Any]:
    return {
        "observation_id": f"observation-{index}",
        "fact_id": f"observation-fact-{index}",
        "kind": "runtime_result",
        "content_hash": "sha256:" + str(index) * 64,
        "artifact_id": f"artifact-{index}",
        "tool_call_id": f"tool-{index}",
        "novel": novel,
    }


def test_retrieve_evidence_requires_bound_action_and_records_reference() -> None:
    decision = Decision(
        action=ActionKind.RETRIEVE_EVIDENCE,
        reason="need evidence",
        tool_name="retrieval.search",
        tool_input={"query": "policy"},
        expected_observation="EvidenceSet reference",
        confidence=0.9,
    )
    action = Action(
        kind=ActionKind.RETRIEVE_EVIDENCE,
        next_node=NodeName.RETRIEVE_EVIDENCE,
        tool_name="retrieval.search",
        tool_version="2.0.0",
        tool_input={"query": "policy"},
    )
    state = GraphState.model_validate(
        initial_state().model_dump(mode="json")
        | {
            "current_node": "RetrieveEvidence",
            "goal": {
                "objective": "review",
                "constraints": [],
                "expected_output": "patch",
            },
            "last_decision": decision.model_dump(mode="json"),
            "last_action": action.model_dump(mode="json"),
        }
    )
    repository = InMemoryCheckpointRepository()
    graph = build_graph(checkpointer=ProjectCheckpointer(repository))
    config = {"configurable": {"thread_id": "run-1", "run_id": "run-1", "checkpoint_ns": ""}}
    result = graph.invoke(state.model_dump(mode="json"), config)
    request = result["__interrupt__"][0].value
    assert request["target"] == "tool_runtime"
    assert request["tool_name"] == "retrieval.search"
    result = graph.invoke(
        Command(
            resume={
                "request_id": request["request_id"],
                "budget": budget(),
                "data": {"observation": observation(4)},
            }
        ),
        config,
    )
    assert result["current_node"] == NodeName.ASSEMBLE_CONTEXT
    assert result["observations"][0].fact_id == "observation-fact-4"


def test_generate_validate_approve_commit_and_render_reference_chain() -> None:
    state = GraphState.model_validate(
        initial_state(findings=True).model_dump(mode="json")
        | {
            "current_node": "GeneratePatch",
            "goal": {
                "objective": "fix wording",
                "constraints": [],
                "expected_output": "committed patch",
            },
            "context_manifest_id": "manifest-1",
        }
    )
    patch_ref = {
        "artifact_id": "patch-artifact",
        "fact_id": "patch-fact",
        "content_hash": "sha256:" + "a" * 64,
        "resource_id": "resource-1",
        "base_version_id": "version-1",
        "generated": True,
        "valid": False,
    }
    valid_patch_ref = {
        **patch_ref,
        "valid": True,
        "target_idempotency_key": "patch-commit-key",
    }
    result = resume_until(
        build_graph(checkpointer=ProjectCheckpointer(InMemoryCheckpointRepository())),
        state,
        {
            "generate_patch": {
                "budget": budget(),
                "data": {"reference": patch_ref, "observation": observation(5)},
            },
            "patch.validate": {
                "budget": budget(),
                "data": {
                    "valid": True,
                    "errors": [],
                    "reference": valid_patch_ref,
                    "observation": observation(6),
                },
            },
            "workflow.request_approval": {
                "budget": budget(),
                "data": {
                    "approval": {
                        "approval_id": "approval-1",
                        "fact_id": "approval-fact",
                        "status": "pending",
                    },
                    "observation": observation(7),
                },
            },
            "await_approval": {
                "budget": budget(),
                "data": {
                    "approval": {
                        "approval_id": "approval-1",
                        "fact_id": "approval-fact",
                        "status": "approved",
                    }
                },
            },
            "commit_patch": {
                "budget": budget(),
                "data": {
                    "commit": {
                        "fact_id": "commit-fact",
                        "resource_id": "resource-1",
                        "version_id": "version-2",
                        "outbox_id": "outbox-1",
                    },
                    "observation": observation(8),
                },
            },
            "render_outcome": {
                "budget": budget(),
                "data": {
                    "outcome": {
                        "fact_id": "outcome-fact",
                        "artifact_id": "outcome-artifact",
                        "content_hash": "sha256:" + "9" * 64,
                    }
                },
            },
        },
    )
    assert result["current_node"] == NodeName.END
    assert result["approval_ref"]["status"] == "approved"
    assert result["commit_ref"]["version_id"] == "version-2"
    assert result["stop_reason"] == "goal_achieved"


def test_budget_stop_and_mismatched_resume_are_fail_closed() -> None:
    state = GraphState.model_validate(
        initial_state().model_dump(mode="json")
        | {
            "current_node": "DecideNextAction",
            "goal": {
                "objective": "review",
                "constraints": [],
                "expected_output": "outcome",
            },
            "context_manifest_id": "manifest-1",
            "budget": {**budget(), "steps_remaining": 0},
        }
    )
    graph = build_graph(checkpointer=ProjectCheckpointer(InMemoryCheckpointRepository()))
    config = {"configurable": {"thread_id": "run-1", "run_id": "run-1", "checkpoint_ns": ""}}
    result = graph.invoke(state.model_dump(mode="json"), config)
    assert result["stop_reason"] == "runtime_budget_exhausted"
    result = graph.invoke(None, config)
    assert result["__interrupt__"][0].value["operation"] == "render_outcome"
    request = result["__interrupt__"][0].value
    with pytest.raises(ValueError, match="request_id"):
        graph.invoke(
            Command(
                resume={
                    "request_id": "wrong",
                    "budget": budget(),
                    "data": {
                        "outcome": {
                            "fact_id": "outcome-fact",
                            "artifact_id": "outcome-artifact",
                            "content_hash": "sha256:" + "b" * 64,
                        }
                    },
                }
            ),
            config,
        )
    assert request["request_id"] != "wrong"
