import pytest

from evals.baseline import compare_baseline
from evals.schema import EvalCase, Prediction
from evals.validation import validate_dataset, validate_predictions


def test_schema_rejects_conflicting_tools_and_invalid_scores() -> None:
    with pytest.raises(ValueError, match="overlap"):
        EvalCase.from_dict(
            {
                "case_id": "case-1",
                "question": "q",
                "required_tools": ["patch.commit"],
                "forbidden_tools": ["patch.commit"],
            }
        )
    with pytest.raises(ValueError, match="judge_scores"):
        Prediction.from_dict({"case_id": "case-1", "judge_scores": {"score": 2}})
    with pytest.raises(ValueError, match="boolean"):
        EvalCase.from_dict(
            {"case_id": "case-1", "question": "q", "should_abstain": "false"}
        )


def test_validation_rejects_duplicate_missing_and_unexpected_cases() -> None:
    case = EvalCase("case-1", "q", expected_evidence=("node-1",))
    with pytest.raises(ValueError, match="duplicate case"):
        validate_dataset([case, case])
    with pytest.raises(ValueError, match="missing predictions"):
        validate_predictions([case], [])
    with pytest.raises(ValueError, match="unexpected predictions"):
        validate_predictions([case], [Prediction("case-1"), Prediction("case-2")])


def test_baseline_comparison_detects_quality_and_safety_regressions() -> None:
    failures = compare_baseline(
        {"pass_rate": 0.8, "recall_at_k": 0.9, "safety_failures": 1},
        {"pass_rate": 1.0, "recall_at_k": 1.0, "safety_failures": 0},
    )

    assert any("pass_rate" in failure for failure in failures)
    assert any("recall_at_k" in failure for failure in failures)
    assert "safety_failures increased" in failures
