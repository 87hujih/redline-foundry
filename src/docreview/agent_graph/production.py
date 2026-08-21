"""构建在持久化 ToolRuntime 与不可变 SQL 事实之上的生产 Graph 适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel

from docreview.agent_graph.models import (
    ApprovalRef,
    ApprovalRequestResult,
    BudgetSnapshot,
    CommitRef,
    CommitResult,
    FindingRef,
    FindingReferencesResult,
    FindingsOutput,
    GeneratedPatchResult,
    Observation,
    OutcomeRef,
    Patch,
    PatchOutput,
    PatchRef,
    PatchValidationResult,
    RenderedOutcome,
    RenderResult,
    RuntimeRequest,
)
from docreview.agent_graph.models import (
    ToolResult as GraphToolResult,
)
from docreview.approval.models import ApprovalBinding
from docreview.document.patch import parse_strict as parse_document_patch
from docreview.document.patch import patch_hash as document_patch_hash
from docreview.tool_runtime.executor import RuntimeToolExecutor, ScopeStore
from docreview.tool_runtime.models import (
    AuditStatus,
    ToolError,
    ToolIntent,
    ToolName,
    ToolObservation,
    ToolVersion,
)
from docreview.tool_runtime.runtime import ToolRuntime
from docreview.tool_runtime.schema import (
    JSONObject,
    JSONValue,
    canonical_json_bytes,
    canonical_json_hash,
)

INSERT_GRAPH_FACT_SQL = """
INSERT INTO agent_artifacts (
    workspace_id, run_id, step_id, idempotency_key, data_classification,
    content_json, content_hash, token_count, provenance_json
)
SELECT run.workspace_id, run.id, %s, %s, 'internal', %s::jsonb, %s, 0, %s::jsonb
FROM agent_runs AS run
WHERE run.id = %s AND run.workspace_id IS NOT NULL
ON CONFLICT (workspace_id, idempotency_key) DO NOTHING
RETURNING id::text, run_id::text, step_id::text, content_json, content_hash,
          provenance_json, created_at
"""

GET_GRAPH_FACT_BY_KEY_SQL = """
SELECT artifact.id::text, artifact.run_id::text, artifact.step_id::text,
       artifact.content_json, artifact.content_hash, artifact.provenance_json,
       artifact.created_at
FROM agent_artifacts AS artifact
WHERE artifact.run_id = %s AND artifact.idempotency_key = %s
"""

GET_GRAPH_FACT_BY_ID_SQL = """
SELECT artifact.id::text, artifact.run_id::text, artifact.step_id::text,
       artifact.content_json, artifact.content_hash, artifact.provenance_json,
       artifact.created_at
FROM agent_artifacts AS artifact
WHERE artifact.run_id = %s AND artifact.id = %s
"""

INSERT_OBSERVATION_SQL = """
INSERT INTO agent_observations (
    run_id, step_id, observation_key, kind, action, tool_call_id,
    payload_json, content_hash, novel, created_at
)
SELECT %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
       NOT EXISTS (
           SELECT 1 FROM agent_observations
           WHERE run_id = %s AND content_hash = %s
       ), %s
ON CONFLICT (run_id, observation_key) DO NOTHING
RETURNING id::text, payload_json, content_hash, novel
"""

GET_OBSERVATION_SQL = """
SELECT id::text, payload_json, content_hash, novel, kind, action,
       COALESCE(tool_call_id::text, '')
FROM agent_observations
WHERE run_id = %s AND observation_key = %s
"""

LOAD_BUDGET_SQL = """
SELECT run.version, run.max_steps, run.max_tool_calls, run.token_budget,
       run.cost_budget, run.deadline_at,
       (SELECT COUNT(*) FROM agent_steps WHERE run_id = run.id)::integer,
       (SELECT COUNT(*) FROM tool_calls WHERE run_id = run.id)::integer,
       COALESCE((
           SELECT SUM(COALESCE(attempt.input_tokens, 0) + COALESCE(attempt.output_tokens, 0))
           FROM agent_attempts AS attempt
           JOIN agent_steps AS step ON step.id = attempt.step_id
           WHERE step.run_id = run.id
       ), 0)::bigint,
       COALESCE((
           SELECT SUM(COALESCE(attempt.cost, 0))
           FROM agent_attempts AS attempt
           JOIN agent_steps AS step ON step.id = attempt.step_id
           WHERE step.run_id = run.id
       ), 0)::double precision
FROM agent_runs AS run
WHERE run.id = %s
"""

_FACT_SOURCE = "python_graph_fact"


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    async def commit(self) -> None: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


@dataclass(frozen=True, slots=True)
class GraphFact:
    id: str
    run_id: str
    step_id: str
    kind: str
    content: JSONObject
    content_hash: str
    created_at: datetime


def _context_evidence_content(value: object) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            source_id = item.get("source_id")
            content = item.get("content")
            if (
                isinstance(source_id, str)
                and source_id.startswith("ev_")
                and isinstance(content, str)
            ):
                found[source_id] = content
            for child in item.values():
                visit(child)
        elif isinstance(item, list | tuple):
            for child in item:
                visit(child)

    visit(value)
    return found


class PostgresGraphFactStore:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def record_findings(
        self, request: RuntimeRequest, output: FindingsOutput
    ) -> FindingReferencesResult:
        evidence_content = _context_evidence_content(request.payload.get("context_items"))
        available_evidence = set(evidence_content)
        if available_evidence:
            for finding in output.findings:
                missing = set(finding.evidence_ids) - available_evidence
                if missing:
                    raise ValueError(
                        "finding 引用了当前上下文不存在的证据: " + ", ".join(sorted(missing))
                    )
                if not finding.evidence_quotes:
                    raise ValueError("finding 必须包含可核验的证据原文引句")
                for binding in finding.evidence_quotes:
                    source = evidence_content.get(binding.evidence_id)
                    if source is None or binding.quote not in source:
                        raise ValueError("finding 证据引句不在对应证据正文中")
        references: list[FindingRef] = []
        for item in output.findings:
            content = cast(JSONObject, item.model_dump(mode="json"))
            fact = await self._create_fact(
                request,
                key=f"graph-finding:{request.run_id}:{item.finding_id}",
                kind="finding",
                content=content,
            )
            references.append(
                FindingRef(
                    finding_id=item.finding_id,
                    fact_id=fact.id,
                    content_hash=fact.content_hash,
                )
            )
        observation = await self.record_observation(
            request,
            kind="findings",
            action=request.operation,
            payload={"output": cast(JSONValue, output.model_dump(mode="json"))},
            key=f"graph-observation:{request.idempotency_hint}",
        )
        return FindingReferencesResult(references=tuple(references), observation=observation)
    async def record_patch(
        self, request: RuntimeRequest, output: PatchOutput
    ) -> GeneratedPatchResult:
        content = cast(JSONObject, output.patch.model_dump(mode="json", exclude_none=True))
        fact = await self._create_fact(
            request,
            key=f"graph-patch:{request.idempotency_hint}",
            kind="patch",
            content=content,
        )
        reference = PatchRef(
            artifact_id=fact.id,
            fact_id=fact.id,
            content_hash=fact.content_hash,
            resource_id=output.patch.resource_id,
            base_version_id=output.patch.base_version_id,
            generated=True,
            valid=False,
            target_idempotency_key=None,
        )
        observation = await self.record_observation(
            request,
            kind="generated_patch",
            action=request.operation,
            payload={"output": cast(JSONValue, {"patch": content})},
            key=f"graph-observation:{request.idempotency_hint}",
            artifact_id=fact.id,
        )
        return GeneratedPatchResult(reference=reference, observation=observation)

    async def record_outcome(
        self, request: RuntimeRequest, output: RenderedOutcome
    ) -> RenderResult:
        content = cast(JSONObject, output.model_dump(mode="json"))
        fact = await self._create_fact(
            request,
            key=f"graph-outcome:{request.idempotency_hint}",
            kind="outcome",
            content=content,
        )
        return RenderResult(
            outcome=OutcomeRef(
                fact_id=fact.id,
                artifact_id=fact.id,
                content_hash=fact.content_hash,
            )
        )

    async def load_patch(self, run_id: str, fact_id: str) -> tuple[GraphFact, Patch]:
        fact = await self.load_fact(run_id, fact_id, "patch")
        return fact, Patch.model_validate_json(canonical_json_bytes(fact.content))

    async def load_fact(self, run_id: str, fact_id: str, kind: str) -> GraphFact:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_GRAPH_FACT_BY_ID_SQL, (run_id, fact_id))
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("图 事实 未找到")
        fact = _graph_fact(row)
        if fact.run_id != run_id or fact.kind != kind:
            raise RuntimeError("图 事实 绑定 不匹配")
        return fact

    async def record_tool_observation(
        self,
        request: RuntimeRequest,
        value: ToolObservation,
        *,
        key_suffix: str = "",
    ) -> Observation:
        payload: JSONObject = {"status": value.status.value}
        artifact_id = None
        if value.result is not None:
            payload["output"] = value.result.output
            if value.result.artifact is not None:
                artifact_id = value.result.artifact.artifact_id
                payload["artifact"] = cast(
                    JSONValue,
                    {
                        "artifact_id": value.result.artifact.artifact_id,
                        "content_hash": value.result.artifact.content_hash,
                    },
                )
        if value.error is not None:
            payload["error"] = cast(JSONValue, _error_payload(value.error))
        return await self.record_observation(
            request,
            kind=request.operation,
            action=request.tool_name or request.operation,
            payload=payload,
            hash_payload=_stable_tool_observation_payload(request.operation, payload),
            key=f"graph-observation:{request.idempotency_hint}:{key_suffix}",
            artifact_id=artifact_id,
            tool_call_id=value.call_id,
        )

    async def record_observation(
        self,
        request: RuntimeRequest,
        *,
        kind: str,
        action: str,
        payload: JSONObject,
        key: str,
        hash_payload: JSONObject | None = None,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Observation:
        step_id = _step_id(request)
        content_hash = canonical_json_hash(hash_payload or payload)
        now = datetime.now(UTC)
        params: tuple[object, ...] = (
            request.run_id,
            step_id,
            key,
            kind,
            action,
            tool_call_id,
            canonical_json_bytes(payload).decode(),
            content_hash,
            request.run_id,
            content_hash,
            now,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(INSERT_OBSERVATION_SQL, params)
            row = await cursor.fetchone()
            if row is None:
                await cursor.execute(GET_OBSERVATION_SQL, (request.run_id, key))
                replay = await cursor.fetchone()
                if replay is None:
                    raise RuntimeError("观察结果 幂等 查询 未返回数据行")
                if (
                    not _same_json(replay[1], payload)
                    or str(replay[2]) != content_hash
                    or str(replay[4]) != kind
                    or str(replay[5]) != action
                    or str(replay[6]) != (tool_call_id or "")
                ):
                    raise RuntimeError("观察结果 幂等 冲突")
                row = replay[:4]
            else:
                await connection.commit()
        return Observation(
            observation_id=str(row[0]),
            fact_id=str(row[0]),
            kind=kind,
            content_hash=str(row[2]),
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
            novel=bool(row[3]),
        )

    async def _create_fact(
        self, request: RuntimeRequest, *, key: str, kind: str, content: JSONObject
    ) -> GraphFact:
        step_id = _step_id(request)
        digest = canonical_json_hash(content)
        provenance: list[JSONObject] = [
            {
                "source_type": _FACT_SOURCE,
                "source_id": request.request_id,
                "trust_level": "trusted",
                "kind": kind,
            }
        ]
        params: tuple[object, ...] = (
            step_id,
            key,
            canonical_json_bytes(content).decode(),
            digest,
            json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
            request.run_id,
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(INSERT_GRAPH_FACT_SQL, params)
            row = await cursor.fetchone()
            if row is None:
                await cursor.execute(GET_GRAPH_FACT_BY_KEY_SQL, (request.run_id, key))
                row = await cursor.fetchone()
            else:
                await connection.commit()
        if row is None:
            raise RuntimeError("图 事实 幂等 查询 未返回数据行")
        fact = _graph_fact(row)
        if (
            fact.run_id != request.run_id
            or fact.step_id != step_id
            or fact.kind != kind
            or fact.content_hash != digest
            or canonical_json_bytes(fact.content) != canonical_json_bytes(content)
        ):
            raise RuntimeError("图 事实 幂等 冲突")
        return fact


class ProductionGraphToolRuntime:
    def __init__(
        self,
        *,
        executor: RuntimeToolExecutor,
        runtime: ToolRuntime,
        scopes: ScopeStore,
        facts: PostgresGraphFactStore,
    ) -> None:
        self._executor = executor
        self._runtime = runtime
        self._scopes = scopes
        self._facts = facts

    async def execute(self, request: RuntimeRequest, output_type: type[BaseModel]) -> BaseModel:
        if request.operation in {"retrieval.search", "document.read_nodes"}:
            observation = await self._execute_tool(request, request.payload)
            fact = await self._facts.record_tool_observation(request, observation)
            return output_type.model_validate(GraphToolResult(observation=fact))
        if request.operation == "patch.validate":
            return output_type.model_validate(await self._validate_patch(request))
        if request.operation == "workflow.request_approval":
            return output_type.model_validate(await self._request_approval(request))
        raise ValueError(f"不支持的 图 工具 操作{request.operation}")

    async def execute_commit(self, request: RuntimeRequest) -> CommitResult:
        patch_id = _payload_string(request, "patch_fact_id")
        approval_id = _payload_string(request, "approval_id")
        key = _payload_string(request, "target_idempotency_key")
        _fact, patch = await self._facts.load_patch(request.run_id, patch_id)
        tool_input: JSONObject = {
            "resource_id": patch.resource_id,
            "patch": cast(JSONValue, patch.model_dump(mode="json", exclude_none=True)),
        }
        observation = await self._execute_tool(
            request,
            tool_input,
            tool_name="document.commit_patch",
            tool_version="1.0.0",
            idempotency_key=key,
            approval_id=approval_id,
            patch_hash=_canonical_patch_hash(patch),
        )
        recorded = await self._facts.record_tool_observation(
            request, observation, key_suffix="commit"
        )
        if observation.status is not AuditStatus.SUCCEEDED or observation.result is None:
            raise RuntimeError("已批准 规范 提交 未成功")
        output = observation.result.output
        return CommitResult(
            commit=CommitRef(
                fact_id=observation.call_id or recorded.fact_id,
                resource_id=_object_string(output, "resource_id"),
                version_id=_object_string(output, "version_id"),
                outbox_id=_object_string(output, "outbox_id"),
            ),
            observation=recorded,
        )

    async def _validate_patch(self, request: RuntimeRequest) -> PatchValidationResult:
        patch_id = _payload_string(request, "patch_fact_id")
        fact, patch = await self._facts.load_patch(request.run_id, patch_id)
        observation = await self._execute_tool(
            request,
            {
                "resource_id": patch.resource_id,
                "patch": cast(JSONValue, patch.model_dump(mode="json", exclude_none=True)),
            },
            tool_name="patch.validate",
            tool_version="1.0.0",
        )
        recorded = await self._facts.record_tool_observation(request, observation)
        valid = False
        errors: tuple[str, ...] = ()
        if observation.status is AuditStatus.SUCCEEDED and observation.result is not None:
            raw_valid = observation.result.output.get("valid")
            valid = raw_valid is True
            raw_errors = observation.result.output.get("errors", [])
            if isinstance(raw_errors, list):
                errors = tuple(_validation_error(item) for item in raw_errors)[:100]
        elif observation.error is not None:
            errors = (observation.error.message,)
        reference = PatchRef(
            artifact_id=fact.id,
            fact_id=fact.id,
            content_hash=fact.content_hash,
            resource_id=patch.resource_id,
            base_version_id=patch.base_version_id,
            generated=True,
            valid=valid,
            target_idempotency_key=(
                f"document-commit:{request.run_id}:{fact.id}" if valid else None
            ),
        )
        return PatchValidationResult(
            valid=valid,
            errors=errors,
            reference=reference,
            observation=recorded,
        )

    async def _request_approval(self, request: RuntimeRequest) -> ApprovalRequestResult:
        patch_id = _payload_string(request, "patch_fact_id")
        key = _payload_string(request, "target_idempotency_key")
        fact, patch = await self._facts.load_patch(request.run_id, patch_id)
        scope = await self._scopes.load_tool_scope(request.run_id, _step_id(request))
        tool_input: JSONObject = {
            "resource_id": patch.resource_id,
            "patch": cast(JSONValue, patch.model_dump(mode="json", exclude_none=True)),
        }
        observation = await self._runtime.request_pending_approval(
            scope.context,
            ApprovalBinding(
                workspace_id=scope.context.workspace_id,
                run_id=request.run_id,
                step_id=_step_id(request),
                resource_id=patch.resource_id,
                patch_id=fact.id,
                patch_hash=_canonical_patch_hash(patch),
                tool_name="document.commit_patch",
                tool_version="1.0.0",
                input_hash=canonical_json_hash(tool_input),
                idempotency_key=key,
                target_version_id=patch.base_version_id,
            ),
            reason="规范文档变更需要外部审批",
            payload={"patch_fact_id": fact.id},
        )
        recorded = await self._facts.record_tool_observation(request, observation)
        if observation.status is not AuditStatus.PENDING or observation.approval_id is None:
            raise RuntimeError("审批 请求 未创建 待处理 事实")
        return ApprovalRequestResult(
            approval=ApprovalRef(
                approval_id=observation.approval_id,
                fact_id=observation.approval_id,
                status="pending",
            ),
            observation=recorded,
        )

    async def _execute_tool(
        self,
        request: RuntimeRequest,
        payload: JSONObject,
        *,
        tool_name: str | None = None,
        tool_version: str | None = None,
        idempotency_key: str | None = None,
        approval_id: str | None = None,
        patch_hash: str | None = None,
    ) -> ToolObservation:
        return await self._executor.execute(
            ToolIntent(
                name=ToolName(tool_name or request.tool_name),
                version=ToolVersion(tool_version or request.tool_version),
                raw_input=canonical_json_bytes(payload),
                idempotency_key=idempotency_key or request.idempotency_hint,
                approval_id=approval_id,
                patch_hash=patch_hash,
            ),
            run_id=request.run_id,
            step_id=_step_id(request),
        )


class ProductionGraphCommitter:
    def __init__(self, tools: ProductionGraphToolRuntime) -> None:
        self._tools = tools

    async def commit(self, request: RuntimeRequest) -> CommitResult:
        return await self._tools.execute_commit(request)


class PostgresBudgetReader:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def load(self, run_id: str) -> BudgetSnapshot:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LOAD_BUDGET_SQL, (run_id,))
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("运行时 预算 未找到")
        steps_remaining = max(0, int(cast(int, row[1])) - int(cast(int, row[6])))
        tools_remaining = max(0, int(cast(int, row[2])) - int(cast(int, row[7])))
        token_budget = cast(int | None, row[3])
        cost_budget = cast(float | None, row[4])
        tokens_used = int(cast(int, row[8]))
        cost_used = float(cast(float, row[9]))
        deadline = cast(datetime | None, row[5])
        deadline_exceeded = deadline is not None and deadline <= datetime.now(UTC)
        reason = None
        if deadline_exceeded:
            reason = "deadline_exceeded"
        elif steps_remaining == 0:
            reason = "step_budget_exhausted"
        elif tools_remaining == 0:
            reason = "tool_budget_exhausted"
        return BudgetSnapshot(
            fact_id=f"budget:{run_id}:{int(cast(int, row[0]))}",
            steps_remaining=steps_remaining,
            tool_calls_remaining=tools_remaining,
            tokens_remaining=(None if token_budget is None else max(0, token_budget - tokens_used)),
            cost_remaining=(None if cost_budget is None else max(0.0, cost_budget - cost_used)),
            deadline_exceeded=deadline_exceeded,
            exhausted_reason=reason,
        )


def _graph_fact(row: tuple[object, ...]) -> GraphFact:
    content = _object(row[3], "graph fact content")
    provenance = row[5]
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    if not isinstance(provenance, list):
        raise RuntimeError("图 事实 来源信息 无效")
    marker: dict[str, object] | None = None
    for raw_item in cast(list[object], provenance):
        if not isinstance(raw_item, dict):
            continue
        item = {str(key): value for key, value in cast(dict[object, object], raw_item).items()}
        if item.get("source_type") == _FACT_SOURCE:
            marker = item
            break
    if marker is None or not isinstance(marker.get("kind"), str):
        raise RuntimeError("图 事实 标记 无效")
    digest = canonical_json_hash(content)
    if digest != str(row[4]):
        raise RuntimeError("图 事实 内容 哈希 无效")
    return GraphFact(
        id=str(row[0]),
        run_id=str(row[1]),
        step_id=str(row[2]),
        kind=cast(str, marker["kind"]),
        content=content,
        content_hash=digest,
        created_at=cast(datetime, row[6]),
    )


def _object(value: object, field: str) -> JSONObject:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(f"{field}不是对象")
    return cast(JSONObject, value)


def _same_json(raw: object, expected: JSONObject) -> bool:
    try:
        return canonical_json_bytes(_object(raw, "persisted JSON")) == canonical_json_bytes(
            expected
        )
    except (TypeError, ValueError, RuntimeError):
        return False


def _stable_tool_observation_payload(operation: str, payload: JSONObject) -> JSONObject:
    if operation != "retrieval.search":
        return payload
    copied = json.loads(canonical_json_bytes(payload))
    if not isinstance(copied, dict):
        return payload
    output = copied.get("output")
    if isinstance(output, dict):
        evidence_set = output.get("evidence_set")
        if isinstance(evidence_set, dict):
            evidence_set.pop("created_at", None)
    return cast(JSONObject, copied)


def _step_id(request: RuntimeRequest) -> str:
    if request.step_id is None:
        raise ValueError("持久化 图 请求 需要 step_id")
    return request.step_id


def _payload_string(request: RuntimeRequest, field: str) -> str:
    value = request.payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"图 请求{field}为必填项")
    return value


def _object_string(value: JSONObject, field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise RuntimeError(f"工具 结果{field}缺失")
    return item


def _validation_error(value: JSONValue) -> str:
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message[:2000]
    return str(value)[:2000] or "补丁校验失败"


def _canonical_patch_hash(patch: Patch) -> str:
    parsed = parse_document_patch(
        canonical_json_bytes(patch.model_dump(mode="json", exclude_none=True))
    )
    return document_patch_hash(parsed)


def _error_payload(value: ToolError) -> JSONObject:
    result: JSONObject = {"category": value.category.value, "message": value.message}
    if value.details is not None:
        result["details"] = cast(JSONValue, value.details)
    return result


__all__ = [
    "GET_GRAPH_FACT_BY_ID_SQL",
    "GET_GRAPH_FACT_BY_KEY_SQL",
    "GET_OBSERVATION_SQL",
    "INSERT_GRAPH_FACT_SQL",
    "INSERT_OBSERVATION_SQL",
    "LOAD_BUDGET_SQL",
    "GraphFact",
    "PostgresBudgetReader",
    "PostgresGraphFactStore",
    "ProductionGraphCommitter",
    "ProductionGraphToolRuntime",
]
