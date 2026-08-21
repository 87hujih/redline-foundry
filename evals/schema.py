from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

RISK_LEVELS = frozenset({"normal", "high", "critical"})


def _empty_metadata() -> dict[str, object]:
    return {}


def _empty_scores() -> dict[str, float]:
    return {}


def _boolean(value: object, field_name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _strings(
    value: object, field_name: str, *, require_unique: bool = True
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array of strings")
    items = cast(list[object], value)
    if any(not isinstance(item, str) for item in items):
        raise ValueError(f"{field_name} must be an array of strings")
    result = tuple(cast(str, item).strip() for item in items)
    if any(not item for item in result):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if require_unique and len(set(result)) != len(result):
        raise ValueError(f"{field_name} must contain unique strings")
    return result


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    expected_evidence: tuple[str, ...] = ()
    expected_claims: tuple[str, ...] = ()
    expected_steps: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    expected_workspace_id: str = ""
    expected_version_id: str = ""
    should_abstain: bool = False
    risk_level: str = "normal"
    tags: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=_empty_metadata)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> EvalCase:
        case_id = str(value.get("case_id", "")).strip()
        question = str(value.get("question", "")).strip()
        risk_level = str(value.get("risk_level", "normal")).strip()
        if not case_id or not question:
            raise ValueError("case_id and question are required")
        if risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {risk_level}")
        required_tools = _strings(value.get("required_tools"), "required_tools")
        forbidden_tools = _strings(value.get("forbidden_tools"), "forbidden_tools")
        if set(required_tools) & set(forbidden_tools):
            raise ValueError("required_tools and forbidden_tools must not overlap")
        metadata: object = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        return cls(
            case_id=case_id,
            question=question,
            expected_evidence=_strings(value.get("expected_evidence"), "expected_evidence"),
            expected_claims=_strings(value.get("expected_claims"), "expected_claims"),
            expected_steps=_strings(value.get("expected_steps"), "expected_steps"),
            required_tools=required_tools,
            forbidden_tools=forbidden_tools,
            expected_workspace_id=str(value.get("expected_workspace_id", "")).strip(),
            expected_version_id=str(value.get("expected_version_id", "")).strip(),
            should_abstain=_boolean(value.get("should_abstain"), "should_abstain"),
            risk_level=risk_level,
            tags=_strings(value.get("tags"), "tags"),
            metadata=dict(cast(dict[str, object], metadata)),
        )


@dataclass(frozen=True)
class Prediction:
    case_id: str
    retrieved_node_ids: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    answer: str = ""
    claims: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()
    workspace_id: str = ""
    version_id: str = ""
    abstained: bool = False
    safety_violations: tuple[str, ...] = ()
    latency_ms: int = 0
    cost: float = 0.0
    judge_scores: dict[str, float] = field(default_factory=_empty_scores)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Prediction:
        case_id = str(value.get("case_id", "")).strip()
        latency_ms = value.get("latency_ms", 0)
        cost = value.get("cost", 0.0)
        judge_scores: object = value.get("judge_scores", {})
        if not case_id:
            raise ValueError("prediction case_id is required")
        if not isinstance(latency_ms, int) or isinstance(latency_ms, bool) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            raise ValueError("cost must be a non-negative number")
        if not isinstance(judge_scores, dict) or any(
            not isinstance(key, str)
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= score <= 1
            for key, score in cast(dict[object, object], judge_scores).items()
        ):
            raise ValueError("judge_scores must map names to values between 0 and 1")
        return cls(
            case_id=case_id,
            retrieved_node_ids=_strings(
                value.get("retrieved_node_ids"), "retrieved_node_ids"
            ),
            citations=_strings(value.get("citations"), "citations"),
            answer=str(value.get("answer", "")),
            claims=_strings(value.get("claims"), "claims"),
            steps=_strings(value.get("steps"), "steps", require_unique=False),
            tool_calls=_strings(
                value.get("tool_calls"), "tool_calls", require_unique=False
            ),
            workspace_id=str(value.get("workspace_id", "")).strip(),
            version_id=str(value.get("version_id", "")).strip(),
            abstained=_boolean(value.get("abstained"), "abstained"),
            safety_violations=_strings(
                value.get("safety_violations"), "safety_violations"
            ),
            latency_ms=latency_ms,
            cost=float(cost),
            judge_scores={
                cast(str, key): float(cast(int | float, score))
                for key, score in cast(dict[object, object], judge_scores).items()
            },
        )
