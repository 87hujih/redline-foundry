from __future__ import annotations

from typing import Any

DEFAULT_REGRESSION_LIMITS = {
    "pass_rate": 0.0,
    "recall_at_k": 0.02,
    "citation_recall": 0.02,
    "claim_recall": 0.03,
}


def compare_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    limits: dict[str, float] | None = None,
) -> list[str]:
    failures: list[str] = []
    for metric, allowed_drop in (limits or DEFAULT_REGRESSION_LIMITS).items():
        if metric not in current or metric not in baseline:
            continue
        drop = float(baseline[metric]) - float(current[metric])
        if drop > allowed_drop:
            failures.append(
                f"{metric} regressed by {drop:.4f} (allowed {allowed_drop:.4f})"
            )
    if int(current.get("safety_failures", 0)) > int(baseline.get("safety_failures", 0)):
        failures.append("safety_failures increased")
    return failures
