"""Bounded LangGraph whose nodes emit commands instead of performing side effects.

LangGraph 1.2 lacks complete Pyright stubs. This module isolates that dynamic
construction surface; domain models and runtime payloads remain strictly typed.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from docreview.agent_graph.models import (
    ActionKind,
    ApprovalDecisionResult,
    ApprovalRequestResult,
    BudgetSnapshot,
    CommitResult,
    ContextResult,
    DecisionResult,
    FindingReferencesResult,
    GeneratedPatchResult,
    GoalResult,
    GraphState,
    NodeName,
    PatchValidationResult,
    RenderResult,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeTarget,
    StrictModel,
    ToolResult,
    UserInputResult,
)
from docreview.agent_graph.policy import validate_action


@dataclass(frozen=True, slots=True)
class GraphLimits:
    max_cycles: int = 32
    max_no_progress: int = 3
    max_observations: int = 32

    def __post_init__(self) -> None:
        if self.max_cycles <= 0 or self.max_no_progress <= 0 or self.max_observations <= 0:
            raise ValueError("graph limits must be positive")


class AgentGraphNodes:
    def __init__(self, limits: GraphLimits) -> None:
        self.limits = limits

    @staticmethod
    def _request(
        state: GraphState,
        *,
        target: RuntimeTarget,
        operation: str,
        payload: dict[str, Any],
        tool_name: str = "",
        tool_version: str = "",
        suffix: str = "execute",
    ) -> RuntimeRequest:
        node = state.current_node
        return RuntimeRequest(
            request_id=f"{state.run_id}:{node.value}:{state.sequence}:{suffix}",
            run_id=state.run_id,
            node=node,
            target=target,
            operation=operation,
            payload=payload,
            tool_name=tool_name,
            tool_version=tool_version,
            idempotency_hint=f"agent-graph:{state.run_id}:{node.value}:{state.sequence}:{suffix}",
        )

    @staticmethod
    def _resume(
        request: RuntimeRequest, result_type: type[StrictModel]
    ) -> tuple[Any, BudgetSnapshot]:
        raw = interrupt(request.model_dump(mode="json"))
        response = RuntimeResponse.model_validate_json(json.dumps(raw))
        if response.request_id != request.request_id:
            raise ValueError("runtime response request_id does not match interrupted request")
        return result_type.model_validate_json(json.dumps(response.data)), response.budget

    @staticmethod
    def _base_payload(state: GraphState) -> dict[str, Any]:
        return {
            "request_fact_id": state.request_fact_id,
            "context_manifest_id": state.context_manifest_id,
            "observation_fact_ids": [item.fact_id for item in state.observations],
            "finding_fact_ids": [item.fact_id for item in state.finding_refs],
            "patch_fact_id": state.patch_ref.fact_id if state.patch_ref else None,
            "approval_fact_id": state.approval_ref.fact_id if state.approval_ref else None,
            "commit_fact_id": state.commit_ref.fact_id if state.commit_ref else None,
            "budget_fact_id": state.budget.fact_id,
        }

    def _record_observation(
        self, state: GraphState, observation: Any
    ) -> tuple[tuple[Any, ...], int]:
        prior_hashes = {item.content_hash for item in state.observations}
        expected_novel = observation.content_hash not in prior_hashes
        if observation.novel is not expected_novel:
            raise ValueError("observation novelty does not match bounded graph history")
        observations = (*state.observations, observation)[-self.limits.max_observations :]
        no_progress = 0 if expected_novel else state.consecutive_no_progress + 1
        return observations, no_progress

    def understand_goal(self, state: GraphState) -> dict[str, Any]:
        request = self._request(
            state,
            target=RuntimeTarget.MODEL_GATEWAY,
            operation="understand_goal",
            payload={
                "request_fact_id": state.request_fact_id,
                "schema": "goal_understanding.v1",
            },
        )
        result, budget = self._resume(request, GoalResult)
        assert isinstance(result, GoalResult)
        return {
            "goal": result.goal,
            "context_manifest_id": result.context_manifest_id,
            "budget": budget,
            "current_node": NodeName.ASSEMBLE_CONTEXT,
            "sequence": state.sequence + 1,
        }

    def assemble_context(self, state: GraphState) -> dict[str, Any]:
        request = self._request(
            state,
            target=RuntimeTarget.CONTEXT_ASSEMBLER,
            operation="assemble_context",
            payload=self._base_payload(state),
        )
        result, budget = self._resume(request, ContextResult)
        assert isinstance(result, ContextResult)
        return {
            "context_manifest_id": result.context_manifest_id,
            "budget": budget,
            "current_node": NodeName.DECIDE_NEXT_ACTION,
            "sequence": state.sequence + 1,
        }

    def decide_next_action(self, state: GraphState) -> dict[str, Any]:
        stop_reason = state.stop_reason
        if stop_reason is None and state.consecutive_no_progress >= self.limits.max_no_progress:
            stop_reason = "no_new_information"
        if stop_reason is None and state.cycle_count >= self.limits.max_cycles:
            stop_reason = "graph_cycle_budget_exhausted"
        if stop_reason is None and state.budget.exhausted:
            stop_reason = state.budget.exhausted_reason or "runtime_budget_exhausted"
        if stop_reason is not None:
            return {
                "stop_reason": stop_reason,
                "current_node": NodeName.RENDER_OUTCOME,
                "sequence": state.sequence + 1,
            }
        if state.context_manifest_id is None:
            raise ValueError("DecideNextAction requires a persisted context manifest")
        request = self._request(
            state,
            target=RuntimeTarget.MODEL_GATEWAY,
            operation="decide_next_action",
            payload={**self._base_payload(state), "schema": "decision.v1"},
        )
        result, budget = self._resume(request, DecisionResult)
        assert isinstance(result, DecisionResult)
        action = validate_action(state, result.decision)
        return {
            "last_decision": result.decision,
            "last_action": action,
            "budget": budget,
            "current_node": action.next_node,
            "cycle_count": state.cycle_count + 1,
            "sequence": state.sequence + 1,
        }

    def await_user_input(self, state: GraphState) -> dict[str, Any]:
        decision = state.last_decision
        action = state.last_action
        if (
            decision is None
            or action is None
            or decision.action is not ActionKind.REQUEST_USER_INPUT
            or not action.waits_for_input
        ):
            raise ValueError("AwaitUserInput requires a persisted user-input decision")
        input_request = self._request(
            state,
            target=RuntimeTarget.RUNTIME,
            operation="await_user_input",
            payload={
                "reason": decision.reason,
                "expected_observation": decision.expected_observation,
            },
            suffix="input",
        )
        supplied, budget = self._resume(input_request, UserInputResult)
        assert isinstance(supplied, UserInputResult)
        observations, no_progress = self._record_observation(state, supplied.observation)
        return {
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.ASSEMBLE_CONTEXT,
            "sequence": state.sequence + 1,
        }

    def _tool_observation(
        self, state: GraphState, *, operation: str, expected: ActionKind
    ) -> dict[str, Any]:
        action = state.last_action
        if (
            action is None
            or action.kind is not expected
            or action.next_node is not state.current_node
        ):
            raise ValueError("persisted action does not authorize this tool node")
        request = self._request(
            state,
            target=RuntimeTarget.TOOL_RUNTIME,
            operation=operation,
            payload=cast(dict[str, Any], action.tool_input),
            tool_name=action.tool_name,
            tool_version=action.tool_version,
        )
        result, budget = self._resume(request, ToolResult)
        assert isinstance(result, ToolResult)
        observations, no_progress = self._record_observation(state, result.observation)
        return {
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.ASSEMBLE_CONTEXT,
            "sequence": state.sequence + 1,
        }

    def retrieve_evidence(self, state: GraphState) -> dict[str, Any]:
        return self._tool_observation(
            state, operation="retrieval.search", expected=ActionKind.RETRIEVE_EVIDENCE
        )

    def read_document_nodes(self, state: GraphState) -> dict[str, Any]:
        return self._tool_observation(
            state, operation="document.read_nodes", expected=ActionKind.READ_NODES
        )

    def analyze_evidence(self, state: GraphState) -> dict[str, Any]:
        if not state.observations:
            raise ValueError("AnalyzeEvidence requires durable observations")
        request = self._request(
            state,
            target=RuntimeTarget.MODEL_GATEWAY,
            operation="analyze_evidence",
            payload={**self._base_payload(state), "schema": "findings.v1"},
        )
        result, budget = self._resume(request, FindingReferencesResult)
        assert isinstance(result, FindingReferencesResult)
        observations, no_progress = self._record_observation(state, result.observation)
        return {
            "finding_refs": result.references,
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.ASSEMBLE_CONTEXT,
            "sequence": state.sequence + 1,
        }

    def generate_patch(self, state: GraphState) -> dict[str, Any]:
        if not state.finding_refs:
            raise ValueError("GeneratePatch requires typed finding references")
        request = self._request(
            state,
            target=RuntimeTarget.MODEL_GATEWAY,
            operation="generate_patch",
            payload={**self._base_payload(state), "schema": "patch_input.v1"},
        )
        result, budget = self._resume(request, GeneratedPatchResult)
        assert isinstance(result, GeneratedPatchResult)
        if not result.reference.generated or result.reference.valid:
            raise ValueError("generated Patch reference has an invalid state")
        observations, no_progress = self._record_observation(state, result.observation)
        return {
            "patch_ref": result.reference,
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.VALIDATE_PATCH,
            "sequence": state.sequence + 1,
        }

    def validate_patch(self, state: GraphState) -> dict[str, Any]:
        patch = state.patch_ref
        if patch is None or not patch.generated or patch.valid:
            raise ValueError("ValidatePatch requires an unvalidated generated Patch reference")
        request = self._request(
            state,
            target=RuntimeTarget.TOOL_RUNTIME,
            operation="patch.validate",
            payload={"patch_artifact_id": patch.artifact_id, "patch_fact_id": patch.fact_id},
            tool_name="patch.validate",
            tool_version="1.0.0",
        )
        result, budget = self._resume(request, PatchValidationResult)
        assert isinstance(result, PatchValidationResult)
        if (
            result.reference.artifact_id != patch.artifact_id
            or result.reference.fact_id != patch.fact_id
            or result.reference.content_hash != patch.content_hash
            or result.reference.valid is not result.valid
        ):
            raise ValueError("Patch validation response is not bound to the generated Patch")
        observations, no_progress = self._record_observation(state, result.observation)
        return {
            "patch_ref": result.reference,
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.REQUEST_APPROVAL
            if result.valid
            else NodeName.ASSEMBLE_CONTEXT,
            "sequence": state.sequence + 1,
        }

    def request_approval(self, state: GraphState) -> dict[str, Any]:
        patch = state.patch_ref
        if patch is None or not patch.valid or patch.target_idempotency_key is None:
            raise ValueError("RequestApproval requires a validated Patch and commit key")
        create_request = self._request(
            state,
            target=RuntimeTarget.TOOL_RUNTIME,
            operation="workflow.request_approval",
            payload={
                "patch_artifact_id": patch.artifact_id,
                "patch_fact_id": patch.fact_id,
                "target_idempotency_key": patch.target_idempotency_key,
            },
            tool_name="workflow.request_approval",
            tool_version="1.0.0",
            suffix="create",
        )
        created, budget = self._resume(create_request, ApprovalRequestResult)
        assert isinstance(created, ApprovalRequestResult)
        if created.approval.status != "pending":
            raise ValueError("approval ToolRuntime must return a pending approval fact")
        observations, no_progress = self._record_observation(state, created.observation)
        return {
            "approval_ref": created.approval,
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "current_node": NodeName.AWAIT_APPROVAL,
            "sequence": state.sequence + 1,
        }

    def await_approval(self, state: GraphState) -> dict[str, Any]:
        patch = state.patch_ref
        approval = state.approval_ref
        if patch is None or not patch.valid or patch.target_idempotency_key is None:
            raise ValueError("AwaitApproval requires a validated Patch and commit key")
        if approval is None or approval.status != "pending":
            raise ValueError("AwaitApproval requires a pending approval fact")
        wait_request = self._request(
            state,
            target=RuntimeTarget.RUNTIME,
            operation="await_approval",
            payload={
                "approval_id": approval.approval_id,
                "approval_fact_id": approval.fact_id,
                "patch_fact_id": patch.fact_id,
                "target_idempotency_key": patch.target_idempotency_key,
            },
            suffix="decision",
        )
        decided, budget = self._resume(wait_request, ApprovalDecisionResult)
        assert isinstance(decided, ApprovalDecisionResult)
        if (
            decided.approval.approval_id != approval.approval_id
            or decided.approval.fact_id != approval.fact_id
            or decided.approval.status not in {"approved", "rejected"}
        ):
            raise ValueError("approval decision does not match the interrupted approval fact")
        if decided.approval.status == "approved":
            return {
                "approval_ref": decided.approval,
                "budget": budget,
                "current_node": NodeName.COMMIT_PATCH,
                "sequence": state.sequence + 1,
            }
        return {
            "approval_ref": decided.approval,
            "budget": budget,
            "stop_reason": "approval_rejected",
            "current_node": NodeName.RENDER_OUTCOME,
            "sequence": state.sequence + 1,
        }

    def commit_patch(self, state: GraphState) -> dict[str, Any]:
        patch = state.patch_ref
        approval = state.approval_ref
        if (
            patch is None
            or not patch.valid
            or patch.target_idempotency_key is None
            or approval is None
            or approval.status != "approved"
        ):
            raise ValueError("CommitPatch requires the bound validated Patch and approval fact")
        request = self._request(
            state,
            target=RuntimeTarget.COMMITTER,
            operation="commit_patch",
            payload={
                "patch_artifact_id": patch.artifact_id,
                "patch_fact_id": patch.fact_id,
                "approval_id": approval.approval_id,
                "approval_fact_id": approval.fact_id,
                "target_idempotency_key": patch.target_idempotency_key,
            },
        )
        result, budget = self._resume(request, CommitResult)
        assert isinstance(result, CommitResult)
        if result.commit.resource_id != patch.resource_id:
            raise ValueError("commit result resource does not match the approved Patch")
        observations, no_progress = self._record_observation(state, result.observation)
        return {
            "commit_ref": result.commit,
            "observations": observations,
            "consecutive_no_progress": no_progress,
            "budget": budget,
            "stop_reason": "goal_achieved",
            "current_node": NodeName.RENDER_OUTCOME,
            "sequence": state.sequence + 1,
        }

    def render_outcome(self, state: GraphState) -> dict[str, Any]:
        request = self._request(
            state,
            target=RuntimeTarget.MODEL_GATEWAY,
            operation="render_outcome",
            payload={**self._base_payload(state), "schema": "outcome.v1"},
        )
        result, budget = self._resume(request, RenderResult)
        assert isinstance(result, RenderResult)
        return {
            "outcome_ref": result.outcome,
            "budget": budget,
            "current_node": NodeName.END,
            "sequence": state.sequence + 1,
        }


def _route(state: GraphState) -> str:
    return state.current_node.value


def build_graph(
    *,
    checkpointer: Any,
    limits: GraphLimits | None = None,
) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
    nodes = AgentGraphNodes(limits or GraphLimits())
    builder = StateGraph(GraphState, input_schema=GraphState, output_schema=GraphState)
    handlers = {
        NodeName.UNDERSTAND_GOAL: nodes.understand_goal,
        NodeName.ASSEMBLE_CONTEXT: nodes.assemble_context,
        NodeName.DECIDE_NEXT_ACTION: nodes.decide_next_action,
        NodeName.AWAIT_USER_INPUT: nodes.await_user_input,
        NodeName.RETRIEVE_EVIDENCE: nodes.retrieve_evidence,
        NodeName.READ_DOCUMENT_NODES: nodes.read_document_nodes,
        NodeName.ANALYZE_EVIDENCE: nodes.analyze_evidence,
        NodeName.GENERATE_PATCH: nodes.generate_patch,
        NodeName.VALIDATE_PATCH: nodes.validate_patch,
        NodeName.REQUEST_APPROVAL: nodes.request_approval,
        NodeName.AWAIT_APPROVAL: nodes.await_approval,
        NodeName.COMMIT_PATCH: nodes.commit_patch,
        NodeName.RENDER_OUTCOME: nodes.render_outcome,
    }
    for name, handler in handlers.items():
        builder.add_node(name.value, handler)
        destinations = [item.value for item in handlers]
        destinations.append(END)
        route_map: dict[Any, str] = {item: item for item in destinations}
        route_map[NodeName.END.value] = END
        builder.add_conditional_edges(name.value, _route, route_map)
    start_map: dict[Any, str] = {item.value: item.value for item in handlers}
    builder.add_conditional_edges(START, _route, start_map)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=[item.value for item in handlers],
        name="docreview-agent-graph",
    )


__all__ = ["AgentGraphNodes", "GraphLimits", "build_graph"]
