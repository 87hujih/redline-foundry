"""Deterministic validation of model decisions into graph actions."""

from __future__ import annotations

from docreview.agent_graph.models import (
    Action,
    ActionKind,
    Decision,
    GraphState,
    NodeName,
)

_TOOL_ACTIONS: dict[ActionKind, tuple[str, str, NodeName]] = {
    ActionKind.RETRIEVE_EVIDENCE: (
        "retrieval.search",
        "2.0.0",
        NodeName.RETRIEVE_EVIDENCE,
    ),
    ActionKind.READ_NODES: (
        "document.read_nodes",
        "1.0.0",
        NodeName.READ_DOCUMENT_NODES,
    ),
    ActionKind.REQUEST_APPROVAL: (
        "workflow.request_approval",
        "1.0.0",
        NodeName.REQUEST_APPROVAL,
    ),
}


def validate_action(state: GraphState, decision: Decision) -> Action:
    if state.goal is None:
        raise ValueError("goal must be understood before deciding an action")
    if decision.action in _TOOL_ACTIONS:
        tool_name, version, node = _TOOL_ACTIONS[decision.action]
        if decision.tool_name != tool_name:
            raise ValueError(f"{decision.action} may only use {tool_name}")
        if decision.action is ActionKind.REQUEST_APPROVAL and (
            state.patch_ref is None or not state.patch_ref.generated or not state.patch_ref.valid
        ):
            raise ValueError("request_approval requires a deterministically validated patch")
        return Action(
            kind=decision.action,
            next_node=node,
            tool_name=tool_name,
            tool_version=version,
            tool_input=decision.tool_input,
        )
    if decision.tool_name or decision.tool_input:
        raise ValueError(f"semantic action {decision.action} cannot call a tool")
    if decision.action is ActionKind.ANALYZE:
        if not state.observations:
            raise ValueError("analysis requires durable observations")
        return Action(kind=decision.action, next_node=NodeName.ANALYZE_EVIDENCE)
    if decision.action is ActionKind.GENERATE_PATCH:
        if not state.finding_refs:
            raise ValueError("patch generation requires typed finding references")
        return Action(kind=decision.action, next_node=NodeName.GENERATE_PATCH)
    if decision.action is ActionKind.REQUEST_USER_INPUT:
        return Action(
            kind=decision.action,
            next_node=NodeName.AWAIT_USER_INPUT,
            waits_for_input=True,
        )
    if decision.action is ActionKind.FINISH:
        return Action(kind=decision.action, next_node=NodeName.RENDER_OUTCOME)
    raise ValueError(f"unsupported action {decision.action}")


__all__ = ["validate_action"]
