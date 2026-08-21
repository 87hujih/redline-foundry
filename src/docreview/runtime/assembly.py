"""持久化 Runtime 与 Projection worker 的生产构造。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from docreview.runtime.contracts import EngineConfig, ProjectionWorkerConfig
from docreview.runtime.engine import RuntimeEngine, RuntimeExecutor
from docreview.runtime.lifecycle import RuntimeLifecycle, RuntimeWorker
from docreview.runtime.projection import ProjectionWorker, RuntimeProjector
from docreview.storage.postgres.runtime_projection_repository import RuntimeProjectionRepository
from docreview.storage.postgres.runtime_repository import AsyncPool, RuntimeRepository


@dataclass(frozen=True, slots=True)
class ProductionDurableRuntimeAssembly:
    repository: RuntimeRepository
    projection_repository: RuntimeProjectionRepository
    engine: RuntimeEngine
    runtime_worker: RuntimeWorker
    projector: RuntimeProjector
    projection_worker: ProjectionWorker
    lifecycle: RuntimeLifecycle


def build_production_durable_runtime(
    *,
    pool: AsyncPool,
    executor: RuntimeExecutor,
    worker_id: str,
    poll_interval: timedelta = timedelta(milliseconds=250),
    lease_duration: timedelta = timedelta(seconds=30),
    heartbeat_interval: timedelta = timedelta(seconds=10),
) -> ProductionDurableRuntimeAssembly:
    worker_id = worker_id.strip()
    if not worker_id:
        raise ValueError("必须提供持久化运行时工作进程 ID")
    if heartbeat_interval >= lease_duration:
        raise ValueError("持久化运行时心跳间隔必须短于租约时长")
    repository = RuntimeRepository(pool)
    projection_repository = RuntimeProjectionRepository(pool)
    engine = RuntimeEngine(
        EngineConfig(
            worker_id=worker_id,
            lease_duration=lease_duration,
            heartbeat_interval=heartbeat_interval,
            attempt_timeout=timedelta(minutes=2),
            step_timeout=timedelta(minutes=5),
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=30),
        ),
        repository,
        executor,
    )
    runtime_worker = RuntimeWorker(engine)
    projector = RuntimeProjector(
        projection_repository,
        projection_repository,
        projection_repository,
    )
    projection_worker = ProjectionWorker(
        ProjectionWorkerConfig(
            worker_id=worker_id + ":projection",
            lease_duration=lease_duration,
            batch_size=50,
            max_attempts=10,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(minutes=1),
        ),
        repository,
        projector,
    )
    lifecycle = RuntimeLifecycle(runtime_worker, projection_worker, poll_interval)
    return ProductionDurableRuntimeAssembly(
        repository=repository,
        projection_repository=projection_repository,
        engine=engine,
        runtime_worker=runtime_worker,
        projector=projector,
        projection_worker=projection_worker,
        lifecycle=lifecycle,
    )


__all__ = ["ProductionDurableRuntimeAssembly", "build_production_durable_runtime"]
