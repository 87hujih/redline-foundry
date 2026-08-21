from __future__ import annotations

import pytest

from docreview.agent_graph.assembly import build_production_graph_executor
from docreview.agent_graph.checkpoint import AsyncProjectCheckpointer
from docreview.agent_graph.models import RuntimeRequest, RuntimeResponse
from docreview.agent_graph.runtime import LangGraphExecutor
from docreview.storage.postgres.checkpoint import PostgresCheckpointRepository


class Boundary:
    async def dispatch(self, request: RuntimeRequest) -> RuntimeResponse:
        raise AssertionError(f"unexpected dispatch: {request.operation}")


def test_production_graph_assembly_owns_postgres_checkpointer_and_executor() -> None:
    assembly = build_production_graph_executor(pool=object(), boundary=Boundary())  # type: ignore[arg-type]

    assert isinstance(assembly.repository, PostgresCheckpointRepository)
    assert isinstance(assembly.checkpointer, AsyncProjectCheckpointer)
    assert isinstance(assembly.executor, LangGraphExecutor)
    assert assembly.executor.checkpointer is assembly.checkpointer


def test_production_graph_assembly_requires_runtime_boundary() -> None:
    with pytest.raises(ValueError, match="ProjectRuntimeBoundary"):
        build_production_graph_executor(
            pool=object(),  # type: ignore[arg-type]
            boundary=None,  # type: ignore[arg-type]
        )
