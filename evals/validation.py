from __future__ import annotations

from collections import Counter

from .schema import EvalCase, Prediction


def validate_dataset(cases: list[EvalCase]) -> None:
    if not cases:
        raise ValueError("dataset must contain at least one case")
    duplicates = sorted(
        case_id
        for case_id, count in Counter(case.case_id for case in cases).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
    for case in cases:
        if not case.should_abstain and not (case.expected_claims or case.expected_evidence):
            raise ValueError(f"{case.case_id}: answerable case requires claims or evidence")


def validate_predictions(cases: list[EvalCase], predictions: list[Prediction]) -> None:
    duplicates = sorted(
        case_id
        for case_id, count in Counter(prediction.case_id for prediction in predictions).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate prediction case ids: {', '.join(duplicates)}")
    expected = {case.case_id for case in cases}
    actual = {prediction.case_id for prediction in predictions}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise ValueError(f"missing predictions for cases: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"unexpected predictions for cases: {', '.join(unexpected)}")
