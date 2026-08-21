"""LangGraph 与项目 Checkpoint 的生产装配, 缺依赖时保持 fail-closed。"""

from __future__ import annotations

from dataclasses import dataclass

from docreview.agent_graph.checkpoint import AsyncProjectCheckpointer
from docreview.agent_graph.runtime import LangGraphExecutor, RuntimeBoundary
from docreview.storage.postgres.checkpoint import (
    AsyncPool,
    PostgresCheckpointRepository,
)


@dataclass(frozen=True, slots=True)
class ProductionGraphAssembly:
    repository: PostgresCheckpointRepository
    checkpointer: AsyncProjectCheckpointer
    executor: LangGraphExecutor


def build_production_graph_executor(
    *, pool: AsyncPool, boundary: RuntimeBoundary | None
) -> ProductionGraphAssembly:
    if boundary is None:
        raise ValueError("生产环境 ProjectRuntimeBoundary 为必填项")
    repository = PostgresCheckpointRepository(pool)
    checkpointer = AsyncProjectCheckpointer(repository)
    executor = LangGraphExecutor.create(checkpointer, boundary)
    return ProductionGraphAssembly(repository, checkpointer, executor)


__all__ = ["ProductionGraphAssembly", "build_production_graph_executor"]
