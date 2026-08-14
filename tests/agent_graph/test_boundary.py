from __future__ import annotations

import pytest
from pydantic import BaseModel

from docreview.agent_graph.boundary import ProjectRuntimeBoundary
from docreview.agent_graph.models import (
    BudgetSnapshot,
    CommitResult,
    ContextResult,
    FindingReferencesResult,
    FindingsOutput,
    GeneratedPatchResult,
    NodeName,
    PatchOutput,
    RenderedOutcome,
    RenderResult,
    RuntimeRequest,
    RuntimeTarget,
)


class Models:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    async def invoke(self, request: RuntimeRequest) -> str:
        return self.raw


class Contexts:
    async def assemble(self, request: RuntimeRequest) -> ContextResult:
        return ContextResult(context_manifest_id="manifest-1")


class Tools:
    async def execute(self, request: RuntimeRequest, output_type: type[BaseModel]) -> BaseModel:
        raise AssertionError("tool execution was not expected")


class Commits:
    async def commit(self, request: RuntimeRequest) -> CommitResult:
        raise AssertionError("commit was not expected")


class Facts:
    async def record_findings(
        self, request: RuntimeRequest, output: FindingsOutput
    ) -> FindingReferencesResult:
        raise AssertionError("finding recording was not expected")

    async def record_patch(
        self, request: RuntimeRequest, output: PatchOutput
    ) -> GeneratedPatchResult:
        raise AssertionError("patch recording was not expected")

    async def record_outcome(
        self, request: RuntimeRequest, output: RenderedOutcome
    ) -> RenderResult:
        raise AssertionError("outcome recording was not expected")


class Budgets:
    async def load(self, run_id: str) -> BudgetSnapshot:
        return BudgetSnapshot(fact_id="budget-1", steps_remaining=4, tool_calls_remaining=3)


def request(operation: str = "decide_next_action") -> RuntimeRequest:
    return RuntimeRequest(
        request_id="request-1",
        run_id="run-1",
        node=NodeName.DECIDE_NEXT_ACTION,
        target=RuntimeTarget.MODEL_GATEWAY,
        operation=operation,
        payload={},
        idempotency_hint="key-1",
    )


@pytest.mark.asyncio
async def test_model_gateway_output_is_decoded_strictly_before_resume() -> None:
    raw = (
        '{"action":"finish","reason":"done","tool_input":{},'
        '"expected_observation":"outcome","confidence":1,"confidence":0}'
    )
    boundary = ProjectRuntimeBoundary(
        Models(raw), Contexts(), Tools(), Commits(), Facts(), Budgets()
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        await boundary.dispatch(request())


@pytest.mark.asyncio
async def test_runtime_wait_commands_cannot_be_dispatched_as_side_effects() -> None:
    boundary = ProjectRuntimeBoundary(
        Models("{}"), Contexts(), Tools(), Commits(), Facts(), Budgets()
    )
    wait = RuntimeRequest(
        request_id="request-1",
        run_id="run-1",
        node=NodeName.REQUEST_APPROVAL,
        target=RuntimeTarget.RUNTIME,
        operation="await_approval",
        payload={},
        idempotency_hint="key-1",
    )
    with pytest.raises(RuntimeError, match="durable Runtime"):
        await boundary.dispatch(wait)
