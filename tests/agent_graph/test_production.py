from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from docreview.agent_graph.models import (
    NodeName,
    Observation,
    Patch,
    RuntimeRequest,
    RuntimeTarget,
)
from docreview.agent_graph.production import (
    GET_GRAPH_FACT_BY_ID_SQL,
    GET_GRAPH_FACT_BY_KEY_SQL,
    GET_OBSERVATION_SQL,
    INSERT_GRAPH_FACT_SQL,
    INSERT_OBSERVATION_SQL,
    LOAD_BUDGET_SQL,
    GraphFact,
    PostgresBudgetReader,
    ProductionGraphToolRuntime,
    _stable_tool_observation_payload,
)
from docreview.document.patch import parse_strict, patch_hash
from docreview.tool_runtime.executor import RuntimeToolExecutor
from docreview.tool_runtime.models import (
    AuditStatus,
    Provenance,
    ToolIntent,
    ToolObservation,
    ToolResult,
)
from docreview.tool_runtime.runtime import ToolRuntime


class Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.query = ""
        self.params: tuple[object, ...] = ()

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.query = query
        self.params = params

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    async def __aenter__(self) -> Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> Cursor:
        return self._cursor

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Pool:
    def __init__(self, cursor: Cursor) -> None:
        self._connection = Connection(cursor)

    def connection(self) -> Connection:
        return self._connection


@pytest.mark.asyncio
async def test_budget_reader_uses_durable_counts_usage_and_deadline() -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    cursor = Cursor((7, 10, 6, 1000, 5.0, deadline, 3, 2, 250, 1.25))
    reader = PostgresBudgetReader(cast(Any, Pool(cursor)))

    value = await reader.load("run-1")

    assert cursor.query == LOAD_BUDGET_SQL
    assert cursor.params == ("run-1",)
    assert value.fact_id == "budget:run-1:7"
    assert value.steps_remaining == 7
    assert value.tool_calls_remaining == 4
    assert value.tokens_remaining == 750
    assert value.cost_remaining == 3.75
    assert not value.deadline_exceeded


class Executor:
    def __init__(self) -> None:
        self.intent: ToolIntent | None = None
        self.scope: tuple[str, str] | None = None

    async def execute(self, intent: ToolIntent, *, run_id: str, step_id: str) -> ToolObservation:
        self.intent = intent
        self.scope = (run_id, step_id)
        return ToolObservation(
            call_id="call-1",
            status=AuditStatus.SUCCEEDED,
            result=ToolResult(
                output={
                    "resource_id": "resource-1",
                    "version_id": "version-2",
                    "outbox_id": "outbox-1",
                    "created": True,
                },
                provenance=(Provenance("canonical_commit", "version-2", "trusted"),),
            ),
        )


class Facts:
    def __init__(self) -> None:
        self.fact = GraphFact(
            id="patch-1",
            run_id="run-1",
            step_id="step-1",
            kind="patch",
            content={},
            content_hash="sha256:" + "a" * 64,
            created_at=datetime.now(UTC),
        )
        self.patch = Patch.model_validate_json(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "resource_id": "resource-1",
                    "base_version_id": "version-1",
                    "operations": [
                        {
                            "op": "replace_node",
                            "node_id": "node-1",
                            "expected_hash": "sha256:" + "b" * 64,
                            "content": "updated",
                        }
                    ],
                    "evidence_refs": ["evidence-1"],
                    "reason": "correct the finding",
                }
            )
        )

    async def load_patch(self, run_id: str, fact_id: str) -> tuple[GraphFact, Patch]:
        assert (run_id, fact_id) == ("run-1", "patch-1")
        return self.fact, self.patch

    async def record_tool_observation(
        self, request: RuntimeRequest, value: ToolObservation, *, key_suffix: str = ""
    ) -> Observation:
        assert key_suffix == "commit"
        return Observation(
            observation_id="observation-1",
            fact_id="observation-1",
            kind="commit_patch",
            content_hash="sha256:" + "c" * 64,
            tool_call_id=value.call_id,
            novel=True,
        )


class Scopes:
    async def load_tool_scope(self, run_id: str, step_id: str) -> object:
        return object()


@pytest.mark.asyncio
async def test_graph_commit_reenters_approved_high_risk_tool_runtime() -> None:
    executor = Executor()
    facts = Facts()
    tools = ProductionGraphToolRuntime(
        executor=cast(RuntimeToolExecutor, executor),
        runtime=cast(ToolRuntime, object()),
        scopes=cast(Any, Scopes()),
        facts=cast(Any, facts),
    )
    request = RuntimeRequest(
        request_id="request-1",
        run_id="run-1",
        step_id="step-1",
        node=NodeName.COMMIT_PATCH,
        target=RuntimeTarget.COMMITTER,
        operation="commit_patch",
        payload={
            "patch_fact_id": "patch-1",
            "approval_id": "approval-1",
            "target_idempotency_key": "commit-1",
        },
        idempotency_hint="graph-commit-1",
    )

    value = await tools.execute_commit(request)

    assert value.commit.version_id == "version-2"
    assert value.commit.outbox_id == "outbox-1"
    assert executor.scope == ("run-1", "step-1")
    assert executor.intent is not None
    assert str(executor.intent.name) == "document.commit_patch"
    assert executor.intent.approval_id == "approval-1"
    assert executor.intent.idempotency_key == "commit-1"
    parsed = parse_strict(
        json.dumps(facts.patch.model_dump(mode="json", exclude_none=True)).encode()
    )
    assert executor.intent.patch_hash == patch_hash(parsed)
    assert executor.intent.patch_hash != facts.fact.content_hash


def test_graph_fact_sql_is_scoped_idempotent_and_observation_backed() -> None:
    assert "run.workspace_id" in INSERT_GRAPH_FACT_SQL
    assert "ON CONFLICT" in INSERT_GRAPH_FACT_SQL
    assert "artifact.run_id = %s" in GET_GRAPH_FACT_BY_KEY_SQL
    assert "artifact.run_id = %s AND artifact.id = %s" in GET_GRAPH_FACT_BY_ID_SQL
    assert "NOT EXISTS" in INSERT_OBSERVATION_SQL
    assert "ON CONFLICT (run_id, observation_key)" in INSERT_OBSERVATION_SQL
    assert "WHERE run_id = %s AND observation_key = %s" in GET_OBSERVATION_SQL


def test_retrieval_observation_hash_ignores_volatile_set_timestamp() -> None:
    first = {
        "status": "succeeded",
        "output": {"evidence_set": {"created_at": "one", "evidence": []}},
    }
    second = {
        "status": "succeeded",
        "output": {"evidence_set": {"created_at": "two", "evidence": []}},
    }

    assert _stable_tool_observation_payload("retrieval.search", first) == (
        _stable_tool_observation_payload("retrieval.search", second)
    )
