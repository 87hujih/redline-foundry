"""项目 Runtime 的 Graph 命令适配器与严格模型输出处理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from docreview.agent_graph.codec import decode_model
from docreview.agent_graph.models import (
    ApprovalRequestResult,
    BudgetSnapshot,
    CommitResult,
    ContextResult,
    ContextSnapshot,
    Decision,
    DecisionResult,
    FindingReferencesResult,
    FindingsOutput,
    GeneratedPatchResult,
    Goal,
    GoalResult,
    PatchOutput,
    PatchValidationResult,
    RenderedOutcome,
    RenderResult,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeTarget,
    ToolResult,
)


class ModelGateway(Protocol):
    async def invoke(self, request: RuntimeRequest) -> str | bytes: ...


class ContextAssembler(Protocol):
    async def assemble(self, request: RuntimeRequest) -> ContextResult: ...
    async def load(self, manifest_id: str) -> ContextSnapshot: ...


class ToolRuntime(Protocol):
    async def execute(self, request: RuntimeRequest, output_type: type[BaseModel]) -> BaseModel: ...


class Committer(Protocol):
    async def commit(self, request: RuntimeRequest) -> CommitResult: ...


class FactRecorder(Protocol):
    async def record_findings(
        self, request: RuntimeRequest, output: FindingsOutput
    ) -> FindingReferencesResult: ...

    async def record_patch(
        self, request: RuntimeRequest, output: PatchOutput
    ) -> GeneratedPatchResult: ...

    async def record_outcome(
        self, request: RuntimeRequest, output: RenderedOutcome
    ) -> RenderResult: ...


class BudgetReader(Protocol):
    async def load(self, run_id: str) -> BudgetSnapshot: ...


@dataclass(frozen=True, slots=True)
class ProjectRuntimeBoundary:
    models: ModelGateway
    contexts: ContextAssembler
    tools: ToolRuntime
    committer: Committer
    facts: FactRecorder
    budgets: BudgetReader

    async def dispatch(self, request: RuntimeRequest) -> RuntimeResponse:
        if request.target is RuntimeTarget.RUNTIME:
            raise RuntimeError("waiting commands must be resumed by the durable Runtime")
        if request.target is RuntimeTarget.CONTEXT_ASSEMBLER:
            data = await self.contexts.assemble(request)
        elif request.target is RuntimeTarget.MODEL_GATEWAY:
            data = await self._model(request)
        elif request.target is RuntimeTarget.TOOL_RUNTIME:
            result_type = {
                "retrieval.search": ToolResult,
                "document.read_nodes": ToolResult,
                "patch.validate": PatchValidationResult,
                "workflow.request_approval": ApprovalRequestResult,
            }.get(request.operation)
            if result_type is None:
                raise ValueError(f"不支持的 工具运行时 操作{request.operation}")
            result = await self.tools.execute(request, result_type)
            data = result_type.model_validate(result)
        elif request.target is RuntimeTarget.COMMITTER:
            if request.operation != "commit_patch":
                raise ValueError(f"不支持的 Committer 操作{request.operation}")
            data = await self.committer.commit(request)
        else:
            raise ValueError(f"不支持的 运行时 目标{request.target}")
        budget = await self.budgets.load(request.run_id)
        return RuntimeResponse(
            request_id=request.request_id,
            budget=budget,
            data=data.model_dump(mode="json"),
        )

    async def _model(self, request: RuntimeRequest) -> BaseModel:
        if request.operation == "understand_goal":
            context = await self.contexts.assemble(request)
            model_request = await self._with_context(request, context.context_manifest_id)
            raw = await self.models.invoke(model_request)
            return GoalResult(
                goal=decode_model(raw, Goal),
                context_manifest_id=context.context_manifest_id,
            )
        manifest_id = request.payload.get("context_manifest_id")
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise ValueError("模型 操作 需要 已持久化的 上下文 清单")
        raw = await self.models.invoke(await self._with_context(request, manifest_id))
        if request.operation == "decide_next_action":
            return DecisionResult(decision=decode_model(raw, Decision))
        if request.operation == "analyze_evidence":
            output = decode_model(raw, FindingsOutput)
            return await self.facts.record_findings(request, output)
        if request.operation == "generate_patch":
            output = decode_model(raw, PatchOutput)
            return await self.facts.record_patch(request, output)
        if request.operation == "render_outcome":
            output = decode_model(raw, RenderedOutcome)
            return await self.facts.record_outcome(request, output)
        raise ValueError(f"不支持的 模型 操作{request.operation}")

    async def _with_context(self, request: RuntimeRequest, manifest_id: str) -> RuntimeRequest:
        snapshot = await self.contexts.load(manifest_id)
        if snapshot.context_manifest_id != manifest_id or snapshot.run_id != request.run_id:
            raise RuntimeError("上下文 清单 与预期不匹配 该 持久化 模型 请求")
        return request.model_copy(
            update={
                "payload": {
                    **request.payload,
                    "context_manifest_id": manifest_id,
                    "context_items": [dict(item) for item in snapshot.items],
                    "context_content_hash": snapshot.content_hash,
                }
            }
        )


__all__ = [
    "BudgetReader",
    "Committer",
    "ContextAssembler",
    "FactRecorder",
    "ModelGateway",
    "ProjectRuntimeBoundary",
    "ToolRuntime",
]
