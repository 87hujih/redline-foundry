"""由 Runtime 持有的无副作用 LangGraph 命令协议驱动器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.types import Command

from docreview.agent_graph.checkpoint import AsyncProjectCheckpointer, ProjectCheckpointer
from docreview.agent_graph.graph import build_graph
from docreview.agent_graph.models import GraphResume, GraphState, RuntimeRequest, RuntimeResponse
from docreview.runtime.models import ExecutionInput, ExecutionResult, Outcome, StepSpec


class GraphLike(Protocol):
    async def ainvoke(self, input: object, config: RunnableConfig) -> object: ...


class RuntimeBoundary(Protocol):
    """Project Runtime 将请求分派到权威子系统。"""

    async def dispatch(self, request: RuntimeRequest) -> RuntimeResponse: ...


@dataclass(frozen=True, slots=True)
class GraphRun:
    state: GraphState | None
    interrupts: tuple[RuntimeRequest, ...]
    completed: bool


class LangGraphExecutor:
    """将一个持久化 Step 适配为有界 Graph 调用。

    Executor 只通过 ``RuntimeBoundary`` 分派 Graph 发出的命令，绝不导入
    provider、repository、parser、ToolRuntime 或 Committer。
    """

    def __init__(
        self,
        graph: GraphLike,
        checkpointer: ProjectCheckpointer | AsyncProjectCheckpointer,
        boundary: RuntimeBoundary,
    ) -> None:
        self.graph = graph
        self.checkpointer = checkpointer
        self.boundary = boundary

    @classmethod
    def create(
        cls,
        checkpointer: ProjectCheckpointer | AsyncProjectCheckpointer,
        boundary: RuntimeBoundary,
    ) -> LangGraphExecutor:
        graph = cast(GraphLike, build_graph(checkpointer=checkpointer))
        return cls(graph, checkpointer, boundary)

    @staticmethod
    def _config(run_id: str, namespace: str = "") -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": run_id,
                "run_id": run_id,
                "checkpoint_ns": namespace,
            }
        }

    @staticmethod
    def _interrupts(value: object) -> tuple[RuntimeRequest, ...]:
        if not isinstance(value, list):
            return ()
        requests: list[RuntimeRequest] = []
        for item in cast(list[object], value):
            raw = getattr(item, "value", item)
            requests.append(RuntimeRequest.model_validate_json(json.dumps(raw)))
        return tuple(requests)

    async def start(self, state: GraphState, namespace: str = "") -> GraphRun:
        config = self._config(state.run_id, namespace)
        existing = await self.checkpointer.aget_tuple(config)
        if existing is not None:
            return self._checkpoint_result(existing)
        raw = await self.graph.ainvoke(state.model_dump(mode="json"), config)
        return self._result(raw)

    async def resume(self, run_id: str, response: RuntimeResponse, namespace: str = "") -> GraphRun:
        raw = await self.graph.ainvoke(
            Command(resume=response.model_dump(mode="json")),
            self._config(run_id, namespace),
        )
        return self._result(raw)

    async def execute(self, input: ExecutionInput) -> ExecutionResult:
        replay = await self.checkpointer.aget_step_result(input.run_id, input.step_id)
        if replay is not None:
            return self._execution_result(replay)
        resume_value = input.input.get("graph_resume")
        if resume_value is not None:
            resume = GraphResume.model_validate_json(json.dumps(resume_value))
            namespace = f"step:{resume.checkpoint_step_id}"
            run = await self.resume(input.run_id, resume.response, namespace)
        else:
            state = GraphState.model_validate(input.input)
            namespace = f"step:{input.step_id}"
            run = await self.start(state, namespace)
        while run.interrupts:
            request = run.interrupts[0].model_copy(update={"step_id": input.step_id})
            if request.target.value == "runtime" and request.operation in {
                "await_approval",
                "await_user_input",
            }:
                waiting = (
                    Outcome.WAIT_APPROVAL
                    if request.operation == "await_approval"
                    else Outcome.WAIT_INPUT
                )
                result = ExecutionResult(
                    outcome=waiting,
                    output={
                        "graph_request": request.model_dump(mode="json"),
                        "checkpoint_thread_id": input.run_id,
                        "checkpoint_step_id": input.step_id,
                        "graph_state": run.state.model_dump(mode="json")
                        if run.state is not None
                        else {},
                    },
                )
                await self.checkpointer.aput_step_result(
                    input.run_id, input.step_id, self._execution_value(result)
                )
                return result
            response = await self.boundary.dispatch(request)
            run = await self.resume(input.run_id, response, namespace)
        if run.state is None:
            raise RuntimeError("图 完成时缺少 状态")
        if run.state.outcome_ref is not None:
            result = ExecutionResult(
                outcome=Outcome.SUCCEED,
                output={
                    "outcome_fact_id": run.state.outcome_ref.fact_id,
                    "outcome_artifact_id": run.state.outcome_ref.artifact_id,
                },
            )
            await self.checkpointer.aput_step_result(
                input.run_id, input.step_id, self._execution_value(result)
            )
            return result
        if run.state.approval_ref is not None and run.state.approval_ref.status == "pending":
            return ExecutionResult(
                outcome=Outcome.WAIT_APPROVAL,
                output={"approval_id": run.state.approval_ref.approval_id},
            )
        result = ExecutionResult(
            outcome=Outcome.CONTINUE,
            output=run.state.model_dump(mode="json"),
            next_steps=(
                StepSpec(
                    step_key=f"graph:{run.state.current_node.value}:{run.state.sequence}",
                    step_type=run.state.current_node.value,
                    input=run.state.model_dump(mode="json"),
                ),
            ),
        )
        await self.checkpointer.aput_step_result(
            input.run_id, input.step_id, self._execution_value(result)
        )
        return result

    @staticmethod
    def _result(raw: object) -> GraphRun:
        if not isinstance(raw, dict):
            raise RuntimeError("LangGraph 返回的结果不是对象")
        typed_raw = cast(dict[str, Any], raw)
        interrupts = LangGraphExecutor._interrupts(typed_raw.get("__interrupt__"))
        state_value = {key: value for key, value in typed_raw.items() if key != "__interrupt__"}
        state = GraphState.model_validate(state_value) if state_value else None
        return GraphRun(state=state, interrupts=interrupts, completed=not interrupts)

    @staticmethod
    def _checkpoint_result(checkpoint: CheckpointTuple) -> GraphRun:
        values = checkpoint.checkpoint.get("channel_values", {})
        state_fields = GraphState.model_fields
        state_value = {key: value for key, value in values.items() if key in state_fields}
        state = GraphState.model_validate(state_value) if state_value else None
        interrupts = tuple(
            RuntimeRequest.model_validate_json(json.dumps(value.value))
            for _, channel, pending in checkpoint.pending_writes or []
            if channel == "__interrupt__"
            for value in ([pending] if hasattr(pending, "value") else [])
        )
        return GraphRun(state=state, interrupts=interrupts, completed=not interrupts)

    @staticmethod
    def _execution_value(result: ExecutionResult) -> dict[str, object]:
        return {
            "outcome": result.outcome.value,
            "output": result.output,
            "next_steps": [
                {
                    "step_key": item.step_key,
                    "step_type": item.step_type,
                    "input": item.input,
                    "max_attempts": item.max_attempts,
                }
                for item in result.next_steps
            ],
        }

    @staticmethod
    def _execution_result(value: object) -> ExecutionResult:
        if not isinstance(value, dict):
            raise ValueError("已存储的 图 步骤 结果 必须是对象")
        typed = cast(dict[str, Any], value)
        next_steps_value = typed.get("next_steps")
        if not isinstance(next_steps_value, list):
            raise ValueError("已存储的 图 步骤 结果 next_steps 必须是数组")
        output = typed.get("output")
        if not isinstance(output, dict):
            raise ValueError("已存储的 图 步骤 结果 输出 必须是对象")
        return ExecutionResult(
            outcome=Outcome(str(typed.get("outcome", ""))),
            output=cast(dict[str, Any], output),
            next_steps=tuple(
                StepSpec(
                    step_key=str(item["step_key"]),
                    step_type=str(item["step_type"]),
                    input=cast(dict[str, Any], item["input"]),
                    max_attempts=int(item["max_attempts"]),
                )
                for raw in cast(list[object], next_steps_value)
                if isinstance(raw, dict)
                for item in [cast(dict[str, Any], raw)]
            ),
        )


__all__ = ["GraphRun", "LangGraphExecutor", "RuntimeBoundary"]
