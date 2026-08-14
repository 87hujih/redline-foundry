"""Strict schemas for the bounded LangGraph orchestration boundary."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
Hash = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$", strip_whitespace=True),
]
JSONObject = dict[str, JsonValue]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NodeName(StrEnum):
    UNDERSTAND_GOAL = "UnderstandGoal"
    ASSEMBLE_CONTEXT = "AssembleContext"
    DECIDE_NEXT_ACTION = "DecideNextAction"
    RETRIEVE_EVIDENCE = "RetrieveEvidence"
    READ_DOCUMENT_NODES = "ReadDocumentNodes"
    ANALYZE_EVIDENCE = "AnalyzeEvidence"
    GENERATE_PATCH = "GeneratePatch"
    VALIDATE_PATCH = "ValidatePatch"
    REQUEST_APPROVAL = "RequestApproval"
    AWAIT_APPROVAL = "AwaitApproval"
    COMMIT_PATCH = "CommitPatch"
    RENDER_OUTCOME = "RenderOutcome"
    AWAIT_USER_INPUT = "AwaitUserInput"
    END = "End"


class ActionKind(StrEnum):
    RETRIEVE_EVIDENCE = "retrieve_evidence"
    READ_NODES = "read_nodes"
    ANALYZE = "analyze"
    GENERATE_PATCH = "generate_patch"
    REQUEST_USER_INPUT = "request_user_input"
    REQUEST_APPROVAL = "request_approval"
    FINISH = "finish"


class RuntimeTarget(StrEnum):
    MODEL_GATEWAY = "model_gateway"
    CONTEXT_ASSEMBLER = "context_assembler"
    TOOL_RUNTIME = "tool_runtime"
    COMMITTER = "committer"
    RUNTIME = "runtime"


class Goal(StrictModel):
    objective: ShortText
    constraints: tuple[Annotated[str, StringConstraints(max_length=500)], ...] = Field(
        default=(), max_length=32
    )
    expected_output: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ]


class Decision(StrictModel):
    action: ActionKind
    reason: ShortText
    tool_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""
    tool_input: JSONObject
    expected_observation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ]
    confidence: float = Field(ge=0, le=1)


class Action(StrictModel):
    kind: ActionKind
    next_node: NodeName
    tool_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""
    tool_version: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] = ""
    tool_input: JSONObject = Field(default_factory=dict)
    waits_for_input: bool = False


class Observation(StrictModel):
    observation_id: Identifier
    fact_id: Identifier
    kind: Identifier
    content_hash: Hash
    artifact_id: Identifier | None = None
    tool_call_id: Identifier | None = None
    novel: bool


class Finding(StrictModel):
    finding_id: Identifier
    summary: ShortText
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_ids must be unique")
        return value


class PatchOperationKind(StrEnum):
    REPLACE_NODE = "replace_node"
    INSERT_BEFORE = "insert_before"
    INSERT_AFTER = "insert_after"
    DELETE_NODE = "delete_node"
    UPDATE_ATTRIBUTES = "update_attributes"


class PatchOperation(StrictModel):
    op: PatchOperationKind
    node_id: Identifier
    expected_hash: Hash
    content: Annotated[str, StringConstraints(max_length=50_000)] | None = None
    attributes: JSONObject | None = None
    expected_parent_id: Identifier | None = None
    expected_parent_hash: Hash | None = None
    node: JSONObject | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.op is PatchOperationKind.REPLACE_NODE:
            if self.content is None or any(
                value is not None
                for value in (
                    self.attributes,
                    self.expected_parent_id,
                    self.expected_parent_hash,
                    self.node,
                )
            ):
                raise ValueError("replace_node accepts only content")
        elif self.op in {PatchOperationKind.INSERT_BEFORE, PatchOperationKind.INSERT_AFTER}:
            if (
                self.node is None
                or self.expected_parent_id is None
                or self.expected_parent_hash is None
                or self.content is not None
                or self.attributes is not None
            ):
                raise ValueError("insert operation requires only node and parent binding")
        elif self.op is PatchOperationKind.UPDATE_ATTRIBUTES:
            if self.attributes is None or any(
                value is not None
                for value in (
                    self.content,
                    self.expected_parent_id,
                    self.expected_parent_hash,
                    self.node,
                )
            ):
                raise ValueError("update_attributes accepts only attributes")
        elif any(
            value is not None
            for value in (
                self.content,
                self.attributes,
                self.expected_parent_id,
                self.expected_parent_hash,
                self.node,
            )
        ):
            raise ValueError("delete_node accepts no payload")
        return self


class Patch(StrictModel):
    schema_version: Annotated[str, StringConstraints(pattern=r"^1\.0$")]
    resource_id: Identifier
    base_version_id: Identifier
    operations: tuple[PatchOperation, ...] = Field(min_length=1, max_length=100)
    evidence_refs: tuple[Identifier, ...] = Field(default=(), max_length=100)
    reason: ShortText

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must be unique")
        return value


class FindingRef(StrictModel):
    finding_id: Identifier
    fact_id: Identifier
    content_hash: Hash


class PatchRef(StrictModel):
    artifact_id: Identifier
    fact_id: Identifier
    content_hash: Hash
    resource_id: Identifier
    base_version_id: Identifier
    generated: bool = True
    valid: bool = False
    target_idempotency_key: Identifier | None = None


class ApprovalRef(StrictModel):
    approval_id: Identifier
    fact_id: Identifier
    status: Annotated[str, StringConstraints(pattern=r"^(pending|approved|rejected)$")]


class CommitRef(StrictModel):
    fact_id: Identifier
    resource_id: Identifier
    version_id: Identifier
    outbox_id: Identifier


class OutcomeRef(StrictModel):
    fact_id: Identifier
    artifact_id: Identifier
    content_hash: Hash


class BudgetSnapshot(StrictModel):
    fact_id: Identifier
    steps_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)
    cost_remaining: float | None = Field(default=None, ge=0)
    deadline_exceeded: bool = False
    exhausted_reason: Annotated[str, StringConstraints(max_length=120)] | None = None

    @property
    def exhausted(self) -> bool:
        return (
            self.deadline_exceeded
            or self.steps_remaining == 0
            or self.tool_calls_remaining == 0
            or self.tokens_remaining == 0
            or self.cost_remaining == 0
            or self.exhausted_reason is not None
        )


class GraphState(StrictModel):
    run_id: Identifier
    request_fact_id: Identifier
    current_node: NodeName = NodeName.UNDERSTAND_GOAL
    goal: Goal | None = None
    context_manifest_id: Identifier | None = None
    observations: tuple[Observation, ...] = Field(default=(), max_length=32)
    finding_refs: tuple[FindingRef, ...] = Field(default=(), max_length=100)
    patch_ref: PatchRef | None = None
    last_decision: Decision | None = None
    last_action: Action | None = None
    approval_ref: ApprovalRef | None = None
    commit_ref: CommitRef | None = None
    outcome_ref: OutcomeRef | None = None
    budget: BudgetSnapshot
    consecutive_no_progress: int = Field(default=0, ge=0, le=100)
    cycle_count: int = Field(default=0, ge=0, le=1_000)
    stop_reason: Annotated[str, StringConstraints(max_length=120)] | None = None
    sequence: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="before")
    @classmethod
    def restore_checkpoint_json(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        restored = dict(cast(dict[str, Any], value))

        def model(field: str, target: type[StrictModel]) -> None:
            item = restored.get(field)
            if isinstance(item, dict):
                restored[field] = target.model_validate_json(json.dumps(item))

        def models(field: str, target: type[StrictModel]) -> None:
            items = restored.get(field)
            if isinstance(items, list):
                typed_items = cast(list[object], items)
                restored[field] = tuple(
                    target.model_validate_json(json.dumps(item)) if isinstance(item, dict) else item
                    for item in typed_items
                )

        current = restored.get("current_node")
        if isinstance(current, str):
            restored["current_node"] = NodeName(current)
        model("goal", Goal)
        models("observations", Observation)
        models("finding_refs", FindingRef)
        model("patch_ref", PatchRef)
        model("last_decision", Decision)
        model("last_action", Action)
        model("approval_ref", ApprovalRef)
        model("commit_ref", CommitRef)
        model("outcome_ref", OutcomeRef)
        model("budget", BudgetSnapshot)
        return restored


class RuntimeRequest(StrictModel):
    request_id: Identifier
    run_id: Identifier
    node: NodeName
    target: RuntimeTarget
    operation: Identifier
    payload: JSONObject
    tool_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""
    tool_version: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] = ""
    idempotency_hint: Identifier


class RuntimeResponse(StrictModel):
    request_id: Identifier
    budget: BudgetSnapshot
    data: JSONObject


class GraphResume(StrictModel):
    checkpoint_step_id: Identifier
    response: RuntimeResponse


class GoalResult(StrictModel):
    goal: Goal
    context_manifest_id: Identifier


class ContextResult(StrictModel):
    context_manifest_id: Identifier


class DecisionResult(StrictModel):
    decision: Decision


class ToolResult(StrictModel):
    observation: Observation


class FindingsOutput(StrictModel):
    findings: tuple[Finding, ...] = Field(min_length=1, max_length=100)


class FindingReferencesResult(StrictModel):
    references: tuple[FindingRef, ...] = Field(min_length=1, max_length=100)
    observation: Observation

    @field_validator("references")
    @classmethod
    def unique_findings(cls, value: tuple[FindingRef, ...]) -> tuple[FindingRef, ...]:
        if len({item.finding_id for item in value}) != len(value):
            raise ValueError("finding references must be unique")
        return value


class PatchOutput(StrictModel):
    patch: Patch


class GeneratedPatchResult(StrictModel):
    reference: PatchRef
    observation: Observation


class PatchValidationResult(StrictModel):
    valid: bool
    errors: tuple[ShortText, ...] = Field(default=(), max_length=100)
    reference: PatchRef
    observation: Observation


class ApprovalRequestResult(StrictModel):
    approval: ApprovalRef
    observation: Observation


class ApprovalDecisionResult(StrictModel):
    approval: ApprovalRef


class UserInputResult(StrictModel):
    fact_id: Identifier
    observation: Observation


class CommitResult(StrictModel):
    commit: CommitRef
    observation: Observation


class RenderResult(StrictModel):
    outcome: OutcomeRef


class RenderedOutcome(StrictModel):
    message: ShortText


__all__ = [
    "Action",
    "ActionKind",
    "ApprovalDecisionResult",
    "ApprovalRef",
    "ApprovalRequestResult",
    "BudgetSnapshot",
    "CommitRef",
    "CommitResult",
    "ContextResult",
    "Decision",
    "DecisionResult",
    "Finding",
    "FindingRef",
    "FindingReferencesResult",
    "FindingsOutput",
    "GeneratedPatchResult",
    "Goal",
    "GoalResult",
    "GraphResume",
    "GraphState",
    "Hash",
    "JSONObject",
    "NodeName",
    "Observation",
    "OutcomeRef",
    "Patch",
    "PatchOperation",
    "PatchOperationKind",
    "PatchOutput",
    "PatchRef",
    "PatchValidationResult",
    "RenderResult",
    "RuntimeRequest",
    "RuntimeResponse",
    "RuntimeTarget",
    "StrictModel",
    "ToolResult",
    "UserInputResult",
]
