from __future__ import annotations

from pathlib import Path

RULES = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "observability"
    / "docreview-capacity-alerts.yml.template"
)


def test_capacity_alert_template_covers_every_runtime_gate() -> None:
    rules = RULES.read_text(encoding="utf-8")
    for metric in (
        "docreview_runtime_queue_age_seconds",
        "docreview_runtime_lease_heartbeat_margin_seconds",
        "docreview_database_pool_in_use",
        "docreview_database_pool_size",
        "docreview_sse_connections",
        "docreview_outbox_lag_seconds",
        "docreview_projection_lag_seconds",
        "docreview_outbox_dead_letters",
        "docreview_reconciliation_mismatches",
    ):
        assert metric in rules
    assert 'scope="historical"' in rules
    assert "DocReviewCapacityMetricsMissing" in rules
    assert rules.count("absent(") == 9
    assert rules.count("severity: critical") == 9
    assert "${MAX_POOL_UTILIZATION}" in rules
