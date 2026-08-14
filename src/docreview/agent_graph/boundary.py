"""Project-runtime adapters for graph commands and strict model output handling."""

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
                raise ValueError(f"unsupported ToolRuntime operation {request.operation}")
            result = await self.tools.execute(request, result_type)
            data = result_type.model_validate(result)
        elif request.target is RuntimeTarget.COMMITTER:
            if request.operation != "commit_patch":
                raise ValueError(f"unsupported Committer operation {request.operation}")
            data = await self.committer.commit(request)
        else:
            raise ValueError(f"unsupported runtime target {request.target}")
        budget = await self.budgets.load(request.run_id)
        return RuntimeResponse(
            request_id=request.request_id,
            budget=budget,
            data=data.model_dump(mode="json"),
        )

    async def _model(self, request: RuntimeRequest) -> BaseModel:
        if request.operation == "understand_goal":
            context = await self.contexts.assemble(request)
            model_request = request.model_copy(
                update={
                    "payload": {
                        **request.payload,
                        "context_manifest_id": context.context_manifest_id,
                    }
                }
            )
            raw = await self.models.invoke(model_request)
            return GoalResult(
                goal=decode_model(raw, Goal),
                context_manifest_id=context.context_manifest_id,
            )
        raw = await self.models.invoke(request)
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
        raise ValueError(f"unsupported model operation {request.operation}")


__all__ = [
    "BudgetReader",
    "Committer",
    "ContextAssembler",
    "FactRecorder",
    "ModelGateway",
    "ProjectRuntimeBoundary",
    "ToolRuntime",
]
