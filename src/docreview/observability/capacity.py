"""持久化 Runtime Canary 的数值化 staging 容量与告警门禁。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapacityThresholds:
    max_queue_age_seconds: float
    min_lease_heartbeat_margin_seconds: float
    max_database_pool_utilization: float
    max_sse_connections: int
    max_outbox_lag_seconds: float
    max_projection_lag_seconds: float
    max_dead_letters: int = 0

    def __post_init__(self) -> None:
        if (
            self.max_queue_age_seconds <= 0
            or self.min_lease_heartbeat_margin_seconds <= 0
            or not 0 < self.max_database_pool_utilization < 1
            or self.max_sse_connections < 1
            or self.max_outbox_lag_seconds <= 0
            or self.max_projection_lag_seconds <= 0
            or self.max_dead_letters < 0
        ):
            raise ValueError("容量 阈值 无效")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    queue_age_seconds: float
    lease_heartbeat_margin_seconds: float
    database_pool_in_use: int
    database_pool_size: int
    sse_connections: int
    outbox_lag_seconds: float
    projection_lag_seconds: float
    dead_letters: int


@dataclass(frozen=True, slots=True)
class CapacityEvaluation:
    healthy: bool
    alerts: tuple[str, ...]


def evaluate_capacity(
    snapshot: CapacitySnapshot, thresholds: CapacityThresholds
) -> CapacityEvaluation:
    if (
        min(
            snapshot.queue_age_seconds,
            snapshot.lease_heartbeat_margin_seconds,
            snapshot.database_pool_in_use,
            snapshot.database_pool_size,
            snapshot.sse_connections,
            snapshot.outbox_lag_seconds,
            snapshot.projection_lag_seconds,
            snapshot.dead_letters,
        )
        < 0
        or snapshot.database_pool_size < 1
        or snapshot.database_pool_in_use > snapshot.database_pool_size
    ):
        raise ValueError("capacity snapshot is invalid")
    pool_utilization = snapshot.database_pool_in_use / snapshot.database_pool_size
    checks = (
        (snapshot.queue_age_seconds <= thresholds.max_queue_age_seconds, "runtime_queue_age"),
        (
            snapshot.lease_heartbeat_margin_seconds
            >= thresholds.min_lease_heartbeat_margin_seconds,
            "lease_heartbeat_margin",
        ),
        (
            pool_utilization <= thresholds.max_database_pool_utilization,
            "database_pool_headroom",
        ),
        (snapshot.sse_connections <= thresholds.max_sse_connections, "sse_connection_budget"),
        (snapshot.outbox_lag_seconds <= thresholds.max_outbox_lag_seconds, "outbox_lag"),
        (
            snapshot.projection_lag_seconds <= thresholds.max_projection_lag_seconds,
            "projection_lag",
        ),
        (snapshot.dead_letters <= thresholds.max_dead_letters, "projection_dead_letters"),
    )
    alerts = tuple(name for healthy, name in checks if not healthy)
    return CapacityEvaluation(not alerts, alerts)


__all__ = [
    "CapacityEvaluation",
    "CapacitySnapshot",
    "CapacityThresholds",
    "evaluate_capacity",
]
