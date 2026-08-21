import pytest

from docreview.observability.capacity import (
    CapacitySnapshot,
    CapacityThresholds,
    evaluate_capacity,
)


def thresholds() -> CapacityThresholds:
    return CapacityThresholds(5, 10, 0.8, 100, 10, 10)


def test_capacity_gate_reports_specific_saturation_alerts() -> None:
    evaluation = evaluate_capacity(
        CapacitySnapshot(6, 9, 9, 10, 101, 11, 12, 1),
        thresholds(),
    )

    assert evaluation.healthy is False
    assert evaluation.alerts == (
        "runtime_queue_age",
        "lease_heartbeat_margin",
        "database_pool_headroom",
        "sse_connection_budget",
        "outbox_lag",
        "projection_lag",
        "projection_dead_letters",
    )


def test_capacity_gate_passes_only_with_headroom_and_rejects_bad_samples() -> None:
    assert evaluate_capacity(CapacitySnapshot(1, 20, 5, 10, 20, 2, 2, 0), thresholds()).healthy
    with pytest.raises(ValueError, match="snapshot"):
        evaluate_capacity(CapacitySnapshot(1, 20, 11, 10, 20, 2, 2, 0), thresholds())
