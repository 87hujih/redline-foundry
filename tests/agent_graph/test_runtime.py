from __future__ import annotations

from docreview.agent_graph.checkpoint import InMemoryCheckpointRepository, ProjectCheckpointer
from docreview.agent_graph.models import (
    ApprovalRef,
    BudgetSnapshot,
    Goal,
    GraphState,
    JSONObject,
    NodeName,
    PatchRef,
    RuntimeRequest,
    RuntimeResponse,
)
from docreview.agent_graph.runtime import LangGraphExecutor
from docreview.runtime.models import ExecutionInput, Outcome


def budget() -> BudgetSnapshot:
    return BudgetSnapshot(fact_id="budget-1", steps_remaining=30, tool_calls_remaining=15)


class Boundary:
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def dispatch(self, request: RuntimeRequest) -> RuntimeResponse:
        self.operations.append(request.operation)
        responses: dict[str, JSONObject] = {
            "understand_goal": {
                "goal": {
                    "objective": "review",
                    "constraints": [],
                    "expected_output": "outcome",
                },
                "context_manifest_id": "manifest-1",
            },
            "assemble_context": {"context_manifest_id": "manifest-2"},
            "decide_next_action": {
                "decision": {
                    "action": "finish",
                    "reason": "done",
                    "tool_input": {},
                    "expected_observation": "outcome",
                    "confidence": 1.0,
                }
            },
            "render_outcome": {
                "outcome": {
                    "fact_id": "outcome-fact",
                    "artifact_id": "outcome-artifact",
                    "content_hash": "sha256:" + "c" * 64,
                }
            },
        }
        data = responses[request.operation]
        return RuntimeResponse(request_id=request.request_id, budget=budget(), data=data)


def execution(state: GraphState) -> ExecutionInput:
    return ExecutionInput(
        run_id=state.run_id,
        step_id=f"step-{state.sequence}",
        trace_id=f"trace-{state.sequence}",
        step_key=f"graph:{state.current_node.value}:{state.sequence}",
        step_type=state.current_node.value,
        input=state.model_dump(mode="json"),
        attempt_number=1,
        idempotency_key=f"agent-step:step-{state.sequence}",
    )


async def test_executor_keeps_each_graph_node_inside_one_durable_step() -> None:
    boundary = Boundary()
    executor = LangGraphExecutor.create(
        ProjectCheckpointer(InMemoryCheckpointRepository()), boundary
    )
    state = GraphState(run_id="run-1", request_fact_id="request-1", budget=budget())
    expected = ["AssembleContext", "DecideNextAction", "RenderOutcome"]
    for next_type in expected:
        step_input = execution(state)
        result = await executor.execute(step_input)
        assert result.outcome is Outcome.CONTINUE
        assert result.next_steps[0].step_type == next_type
        replay = await executor.execute(step_input)
        assert replay == result
        state = GraphState.model_validate(result.next_steps[0].input)
    result = await executor.execute(execution(state))
    assert result.outcome is Outcome.SUCCEED
    assert result.output["outcome_artifact_id"] == "outcome-artifact"
    assert boundary.operations == [
        "understand_goal",
        "assemble_context",
        "decide_next_action",
        "render_outcome",
    ]


async def test_executor_returns_approval_wait_without_dispatching_runtime_command() -> None:
    boundary = Boundary()
    executor = LangGraphExecutor.create(
        ProjectCheckpointer(InMemoryCheckpointRepository()), boundary
    )
    state = GraphState(
        run_id="run-approval",
        request_fact_id="request-approval",
        current_node=NodeName.AWAIT_APPROVAL,
        goal=Goal(objective="edit", constraints=(), expected_output="patch"),
        patch_ref=PatchRef(
            artifact_id="patch-artifact",
            fact_id="patch-fact",
            content_hash="sha256:" + "d" * 64,
            resource_id="resource-1",
            base_version_id="version-1",
            valid=True,
            target_idempotency_key="commit-key",
        ),
        approval_ref=ApprovalRef(
            approval_id="approval-1", fact_id="approval-fact", status="pending"
        ),
        budget=budget(),
    )
    result = await executor.execute(execution(state))
    assert result.outcome is Outcome.WAIT_APPROVAL
    assert result.output["graph_request"]["operation"] == "await_approval"
    assert result.output["checkpoint_thread_id"] == "run-approval"
    assert boundary.operations == []


async def test_executor_resumes_approval_checkpoint_into_commit_step() -> None:
    boundary = Boundary()
    checkpointer = ProjectCheckpointer(InMemoryCheckpointRepository())
    executor = LangGraphExecutor.create(checkpointer, boundary)
    state = GraphState(
        run_id="run-resume",
        request_fact_id="request-resume",
        current_node=NodeName.AWAIT_APPROVAL,
        goal=Goal(objective="edit", constraints=(), expected_output="patch"),
        patch_ref=PatchRef(
            artifact_id="patch-artifact",
            fact_id="patch-fact",
            content_hash="sha256:" + "e" * 64,
            resource_id="resource-1",
            base_version_id="version-1",
            valid=True,
            target_idempotency_key="commit-key",
        ),
        approval_ref=ApprovalRef(
            approval_id="approval-1", fact_id="approval-fact", status="pending"
        ),
        budget=budget(),
    )
    waiting_input = execution(state)
    waiting = await executor.execute(waiting_input)
    assert waiting.outcome is Outcome.WAIT_APPROVAL
    resume_input = ExecutionInput(
        run_id="run-resume",
        step_id="step-commit-continuation",
        trace_id="trace-resume",
        step_key="graph-resume:approval-1",
        step_type="AwaitApprovalResume",
        input={
            "graph_resume": {
                "checkpoint_step_id": waiting_input.step_id,
                "response": {
                    "request_id": waiting.output["graph_request"]["request_id"],
                    "budget": budget().model_dump(mode="json"),
                    "data": {
                        "approval": {
                            "approval_id": "approval-1",
                            "fact_id": "approval-fact",
                            "status": "approved",
                        }
                    },
                },
            }
        },
        attempt_number=1,
        idempotency_key="agent-step:step-commit-continuation",
    )
    resumed = await executor.execute(resume_input)
    assert resumed.outcome is Outcome.CONTINUE
    assert resumed.next_steps[0].step_type == "CommitPatch"
    assert boundary.operations == []
