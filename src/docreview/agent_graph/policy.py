"""将模型决策确定性校验为 Graph 动作。"""

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
        raise ValueError("决定操作前必须先理解目标")
    if decision.action in _TOOL_ACTIONS:
        tool_name, version, node = _TOOL_ACTIONS[decision.action]
        if decision.tool_name != tool_name:
            raise ValueError(f"{decision.action}只能使用{tool_name}")
        if decision.action is ActionKind.REQUEST_APPROVAL and (
            state.patch_ref is None or not state.patch_ref.generated or not state.patch_ref.valid
        ):
            raise ValueError("request_approval 需要经过确定性校验的补丁")
        return Action(
            kind=decision.action,
            next_node=node,
            tool_name=tool_name,
            tool_version=version,
            tool_input=decision.tool_input,
        )
    if decision.tool_name or decision.tool_input:
        raise ValueError(f"语义 操作{decision.action}不能调用 工具")
    if decision.action is ActionKind.ANALYZE:
        if not state.observations:
            raise ValueError("分析操作需要持久化的观察结果")
        return Action(kind=decision.action, next_node=NodeName.ANALYZE_EVIDENCE)
    if decision.action is ActionKind.GENERATE_PATCH:
        if not state.finding_refs:
            raise ValueError("生成补丁需要类型化的发现项引用")
        return Action(kind=decision.action, next_node=NodeName.GENERATE_PATCH)
    if decision.action is ActionKind.REQUEST_USER_INPUT:
        return Action(
            kind=decision.action,
            next_node=NodeName.AWAIT_USER_INPUT,
            waits_for_input=True,
        )
    if decision.action is ActionKind.FINISH:
        return Action(kind=decision.action, next_node=NodeName.RENDER_OUTCOME)
    raise ValueError(f"不支持的 操作{decision.action}")


__all__ = ["validate_action"]
