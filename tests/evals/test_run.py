from pathlib import Path

from evals.run import build_report

ROOT = Path(__file__).parents[2]


def test_regression_dataset_builds_complete_passing_report() -> None:
    report = build_report(
        ROOT / "evals/datasets/regression_v1.jsonl",
        ROOT / "evals/datasets/regression_v1.predictions.jsonl",
        baseline_path=ROOT / "evals/baselines/regression_v1.json",
    )

    assert report["summary"]["cases"] == 7
    assert report["summary"]["pass_rate"] == 1.0
    assert report["summary"]["safety_failures"] == 0
    assert report["regressions"] == []
    assert report["by_tag"]["安全"]["pass_rate"] == 1.0
