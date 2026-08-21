from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .baseline import compare_baseline
from .metrics import evaluate_case, summarize, summarize_by_tag
from .schema import EvalCase, Prediction
from .validation import validate_dataset, validate_predictions


def read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        rows.append(cast(dict[str, object], value))
    return rows


def _read_summary(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("baseline must be a JSON object")
    typed = cast(dict[str, Any], value)
    summary: object = typed.get("summary", typed)
    if not isinstance(summary, dict):
        raise ValueError("baseline summary must be a JSON object")
    return cast(dict[str, Any], summary)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(
    dataset: Path,
    predictions_path: Path,
    *,
    k: int = 5,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    cases = [EvalCase.from_dict(row) for row in read_jsonl(dataset)]
    predictions = [Prediction.from_dict(row) for row in read_jsonl(predictions_path)]
    validate_dataset(cases)
    validate_predictions(cases, predictions)
    predictions_by_id = {prediction.case_id: prediction for prediction in predictions}
    results = [
        evaluate_case(case, predictions_by_id[case.case_id], k=k) for case in cases
    ]
    summary = summarize(results)
    regressions = (
        compare_baseline(summary, _read_summary(baseline_path)) if baseline_path else []
    )
    return {
        "schema_version": "1.0",
        "run": {
            "created_at": datetime.now(UTC).isoformat(),
            "git_sha": os.environ.get("GITHUB_SHA", "local"),
            "dataset": str(dataset),
            "dataset_hash": _digest(dataset),
            "predictions_hash": _digest(predictions_path),
            "k": k,
        },
        "summary": summary,
        "by_tag": summarize_by_tag(results),
        "regressions": regressions,
        "results": results,
    }


def _gate_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    summary = report["summary"]
    failures = list(report["regressions"])
    minimums = {
        "pass_rate": args.min_pass_rate,
        "recall_at_k": args.min_recall_at_k,
        "citation_precision": args.min_citation_precision,
        "citation_recall": args.min_citation_recall,
        "claim_recall": args.min_claim_recall,
    }
    for metric, threshold in minimums.items():
        if float(summary[metric]) < threshold:
            failures.append(f"{metric} {summary[metric]:.4f} is below {threshold:.4f}")
    if int(summary["safety_failures"]) > args.max_safety_failures:
        failures.append("safety failure limit exceeded")
    if int(summary["critical_failures"]) > args.max_critical_failures:
        failures.append("critical case failure limit exceeded")
    if args.max_p95_latency_ms and summary["p95_latency_ms"] > args.max_p95_latency_ms:
        failures.append("p95 latency limit exceeded")
    if args.max_total_cost and summary["total_cost"] > args.max_total_cost:
        failures.append("total cost limit exceeded")
    for requirement in args.min_judge_score:
        name, separator, raw_threshold = requirement.partition("=")
        if not separator or not name:
            raise ValueError("judge threshold must use name=value syntax")
        threshold = float(raw_threshold)
        if not 0 <= threshold <= 1:
            raise ValueError("judge threshold must be between 0 and 1")
        score = summary["judge_scores"].get(name)
        if score is None or float(score) < threshold:
            failures.append(f"judge score {name} is missing or below {threshold:.4f}")
    return failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 DocReview 确定性评测")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--min-pass-rate", type=float, default=0.0)
    parser.add_argument("--min-recall-at-k", type=float, default=0.0)
    parser.add_argument("--min-citation-precision", type=float, default=0.0)
    parser.add_argument("--min-citation-recall", type=float, default=0.0)
    parser.add_argument("--min-claim-recall", type=float, default=0.0)
    parser.add_argument("--max-safety-failures", type=int, default=0)
    parser.add_argument("--max-critical-failures", type=int, default=0)
    parser.add_argument("--max-p95-latency-ms", type=float, default=0.0)
    parser.add_argument("--max-total-cost", type=float, default=0.0)
    parser.add_argument("--min-judge-score", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.k < 1:
        raise ValueError("k must be positive")
    thresholds = (
        args.min_pass_rate,
        args.min_recall_at_k,
        args.min_citation_precision,
        args.min_citation_recall,
        args.min_claim_recall,
    )
    if any(not 0 <= value <= 1 for value in thresholds):
        raise ValueError("minimum score thresholds must be between 0 and 1")
    report = build_report(
        args.dataset,
        args.predictions,
        k=args.k,
        baseline_path=args.baseline,
    )
    failures = _gate_failures(report, args)
    report["gate"] = {"passed": not failures, "failures": failures}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
