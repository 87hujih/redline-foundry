"""ToolRuntime 的 PostgreSQL-backed 生产边界。

适配器只使用冻结的持久化表，并保留 Tool audit 与 artifact 的稳定 JSON
envelope，使服务重启或滚动发布可以重放任一 Runtime 写入的事实。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from docreview.approval.models import (
    Approval as DomainApproval,
)
from docreview.approval.models import (
    ApprovalBinding,
    ApprovalCreateCommand,
)
from docreview.approval.models import (
    Principal as ApprovalPrincipal,
)
from docreview.identity.policy import Access, PolicyResolver, ResourceRef
from docreview.identity.trusted_ingress import Principal as IngressPrincipal
from docreview.identity.trusted_ingress import WorkspaceScope
from docreview.runtime.contracts import ApprovalRequest
from docreview.runtime.models import Approval, Tool, ToolStatus
from docreview.storage.filestore import LocalFileStore
from docreview.storage.postgres.runtime_repository import RuntimeRepository
from docreview.tool_runtime.builtin.artifact import ArtifactCreate, ArtifactRecord
from docreview.tool_runtime.executor import TrustedToolScope
from docreview.tool_runtime.models import (
    ApprovalGrant,
    ApprovalRequirement,
    ArtifactReference,
    AuditClaim,
    AuditClaimRequest,
    AuditFinishRequest,
    AuditStatus,
    IdempotencyConflictError,
    PolicyDecision,
    PolicyRequest,
    Principal,
    Provenance,
    ToolBackendFailure,
    ToolError,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolName,
    ToolResult,
    ToolVersion,
)
from docreview.tool_runtime.schema import (
    JSONObject,
    JSONValue,
    canonical_json_bytes,
    canonical_json_hash,
)

_DATABASE_ERROR_CATEGORIES = {
    ToolErrorCategory.UNAUTHORIZED: "permission_denied",
    ToolErrorCategory.APPROVAL_REQUIRED: "policy_blocked",
    ToolErrorCategory.INVALID_OUTPUT: "terminal_upstream",
    ToolErrorCategory.PERMANENT_FAILURE: "terminal_upstream",
    ToolErrorCategory.IDEMPOTENCY_CONFLICT: "conflict",
}

TOOL_SCOPE_SQL = """
SELECT run.request_id, run.id::text, step.id::text, run.workspace_id::text,
       run.resource_id::text, run.principal_type, run.principal_id::text,
       membership.role, COALESCE(run.trace_id, run.request_id), step.attempt_count,
       COALESCE(run.deadline_at, step.lease_expires_at), resource.workspace_id::text
FROM agent_runs AS run
JOIN agent_steps AS step ON step.run_id = run.id
JOIN resources AS resource ON resource.id = run.resource_id
JOIN memberships AS membership
  ON membership.workspace_id = run.workspace_id
 AND membership.user_id = run.principal_id
JOIN users AS account ON account.id = membership.user_id
JOIN workspaces AS workspace ON workspace.id = membership.workspace_id
WHERE run.id = %s AND step.id = %s
  AND step.status = 'running'
  AND run.workspace_id IS NOT NULL AND run.resource_id IS NOT NULL
  AND run.principal_type = 'user' AND run.principal_id IS NOT NULL
  AND membership.status = 'active' AND account.status = 'active'
  AND workspace.status = 'active'
  AND resource.workspace_id = run.workspace_id
"""

ARTIFACT_COLUMNS = """
id::text, workspace_id::text, run_id::text, step_id::text, idempotency_key,
data_classification, content_json, content_hash, token_count, provenance_json, created_at
"""

INSERT_ARTIFACT_SQL = f"""
INSERT INTO agent_artifacts (
    workspace_id, run_id, step_id, idempotency_key, data_classification,
    content_json, content_hash, token_count, provenance_json
)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
ON CONFLICT (workspace_id, idempotency_key) DO NOTHING
RETURNING {ARTIFACT_COLUMNS}
"""

GET_ARTIFACT_BY_KEY_SQL = f"""
SELECT {ARTIFACT_COLUMNS}
FROM agent_artifacts
WHERE workspace_id = %s AND idempotency_key = %s
"""

GET_ARTIFACT_BY_ID_SQL = f"""
SELECT {ARTIFACT_COLUMNS}
FROM agent_artifacts
WHERE workspace_id = %s AND id = %s
"""

_ARTIFACT_BINDING_SOURCE = "python_tool_artifact_binding"


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


class PostgresToolScopeStore:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def load_tool_scope(self, run_id: str, step_id: str) -> TrustedToolScope:
        if not run_id.strip() or not step_id.strip():
            raise ValueError("工具 范围 身份 为必填项")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(TOOL_SCOPE_SQL, (run_id, step_id))
            row = await cursor.fetchone()
        if row is None or row[10] is None:
            raise PermissionError("持久化 工具 范围 不可用")
        deadline = cast(datetime, row[10])
        context = ToolExecutionContext(
            request_id=str(row[0]),
            run_id=str(row[1]),
            step_id=str(row[2]),
            workspace_id=str(row[3]),
            resource_id=str(row[4]),
            principal=Principal(type=str(row[5]), id=str(row[6])),
            roles=(str(row[7]),),
            trace_id=str(row[8]),
            attempt=int(cast(int, row[9])),
            deadline=deadline,
        )
        return TrustedToolScope(context=context, resource_workspace_id=str(row[11]))


class PostgresToolPolicy:
    """将权威 membership/resource resolver 适配到 ToolRuntime。"""

    def __init__(self, resolver: PolicyResolver) -> None:
        self._resolver = resolver

    async def authorize(self, request: PolicyRequest) -> PolicyDecision:
        context = request.context
        scope = WorkspaceScope(
            principal=IngressPrincipal(
                type=context.principal.type,
                id=context.principal.id,
                organization_id="",
                roles=context.roles,
            ),
            workspace_id=context.workspace_id,
            trust_source="durable_runtime",
            trusted=True,
            issued_at=datetime.now(UTC),
        )
        for permission in request.definition.required_permissions:
            if not await self._resolver.has_permission(scope, permission):
                return PolicyDecision(False, "permission_denied")
        if request.definition.requires_resource:
            raw_resource_id = request.tool_input.get(request.definition.resource_input_field)
            if not isinstance(raw_resource_id, str):
                return PolicyDecision(False, "resource_missing")
            allowed = await self._resolver.authorize_resource(
                scope,
                ResourceRef(
                    type=request.definition.resource_type,
                    id=raw_resource_id,
                    access=Access(request.definition.resource_access),
                ),
                bound_resource_id=context.resource_id,
            )
            if not allowed:
                return PolicyDecision(False, "resource_denied")
        return PolicyDecision(True, "authorized")


class PostgresApprovalStore:
    def __init__(self, repository: RuntimeRepository) -> None:
        self._repository = repository

    async def create(self, command: ApprovalCreateCommand) -> DomainApproval:
        binding = cast(ApprovalBinding, command.binding)
        resources: tuple[JSONObject, ...] = (
            {"type": "document", "id": binding.resource_id, "access": "write"},
        )
        payload: JSONObject = {
            **cast(JSONObject, command.payload),
            "resource_id": binding.resource_id,
            "patch_id": binding.patch_id,
            "patch_hash": binding.patch_hash,
            "input_hash": binding.input_hash,
            "target_version_id": binding.target_version_id,
        }
        value = await self._repository.request_approval(
            ApprovalRequest(
                workspace_id=binding.workspace_id,
                run_id=binding.run_id,
                step_id=binding.step_id,
                tool_name=binding.tool_name,
                tool_version=binding.tool_version,
                idempotency_key=binding.idempotency_key,
                resources=resources,
                resources_hash=_resources_hash(resources),
                payload=payload,
                reason=command.reason,
                requested_by_type=command.requested_by.type,
                requested_by_id=command.requested_by.id,
            )
        )
        return _domain_approval(value)

    async def load_approval(self, approval_id: str) -> ApprovalGrant | None:
        value = await self._repository.get_approval_by_id(approval_id)
        if value is None:
            return None
        return _approval_grant(value)


class PostgresToolAudit:
    def __init__(
        self,
        repository: RuntimeRepository,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> None:
        if not worker_id.strip() or lease_duration <= timedelta(0):
            raise ValueError("工具 审计 工作进程 和 租约 为必填项")
        self._repository = repository
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration

    async def claim(self, request: AuditClaimRequest) -> AuditClaim:
        if canonical_json_hash(request.tool_input) != request.input_hash:
            raise IdempotencyConflictError("tool audit input hash mismatch")
        try:
            value, acquired = await self._repository.begin_tool(
                request.run_id,
                request.step_id,
                str(request.tool_name),
                str(request.tool_version),
                request.tool_input,
                request.idempotency_key,
                self._worker_id,
                request.started_at,
                self._lease_duration,
            )
        except Exception as error:
            if error.__class__.__name__ == "IdempotencyConflictError":
                raise IdempotencyConflictError("工具 审计 幂等 冲突") from error
            raise
        return _audit_claim(value, acquired)

    async def finish(self, request: AuditFinishRequest) -> None:
        value = await self._repository.get_tool_by_id(request.call_id)
        if value is None:
            raise LookupError("工具 审计 调用 未找到")
        if value.claimed_by != self._worker_id:
            raise PermissionError("工具 审计 租约 所有者 不匹配")
        status = ToolStatus(request.status.value)
        output = _result_json(request.result) if request.result is not None else None
        error = _error_json(request.error) if request.error is not None else None
        await self._repository.finish_tool(
            value,
            status,
            output,
            error,
            None
            if request.error is None
            else _DATABASE_ERROR_CATEGORIES.get(
                request.error.category, request.error.category.value
            ),
            request.latency_ms,
            request.completed_at,
            request.backend_attempts,
        )


class PostgresArtifactRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def create_or_get(self, request: ArtifactCreate) -> tuple[ArtifactRecord, bool]:
        content = request.content
        if canonical_json_hash(content) != request.content_hash:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "制品 内容 哈希 无效")
        provenance = _provenance_json(request)
        params: tuple[object, ...] = (
            request.workspace_id,
            request.run_id,
            request.step_id,
            request.idempotency_key,
            request.data_classification,
            canonical_json_bytes(content).decode(),
            request.content_hash,
            0,
            json.dumps(provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(INSERT_ARTIFACT_SQL, params)
            row = await cursor.fetchone()
            created = row is not None
            if row is None:
                await cursor.execute(
                    GET_ARTIFACT_BY_KEY_SQL,
                    (request.workspace_id, request.idempotency_key),
                )
                row = await cursor.fetchone()
            if created:
                await connection.commit()
        if row is None:
            raise RuntimeError("制品 幂等 查询 未返回数据行")
        record = _artifact_record(row)
        persisted_content = row[6]
        if isinstance(persisted_content, str):
            persisted_content = json.loads(persisted_content)
        if (
            not isinstance(persisted_content, dict)
            or canonical_json_bytes(cast(JSONObject, persisted_content))
            != canonical_json_bytes(content)
            or not _artifact_binding_matches(request, record)
        ):
            raise ToolBackendFailure(ToolErrorCategory.IDEMPOTENCY_CONFLICT, "制品 幂等 冲突")
        return record, created

    async def get(self, workspace_id: str, artifact_id: str) -> ArtifactRecord | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_ARTIFACT_BY_ID_SQL, (workspace_id, artifact_id))
            row = await cursor.fetchone()
        return None if row is None else _artifact_record(row)


class LocalArtifactBlobStore:
    def __init__(self, store: LocalFileStore) -> None:
        self._store = store

    async def put(self, key: str, content: bytes, content_hash: str) -> None:
        if key != content_hash or not content_hash.startswith("sha256:"):
            raise ValueError("制品 二进制对象 键 无效")
        stored = await self._store.save(content)
        if "sha256:" + stored.sha256 != content_hash or stored.size_bytes != len(content):
            raise RuntimeError("制品 二进制对象 持久化 不匹配")

    async def get(self, key: str) -> bytes | None:
        if not key.startswith("sha256:"):
            raise ValueError("制品 二进制对象 键 无效")
        digest = key.removeprefix("sha256:")
        storage_key = f"{digest[:2]}/{digest}"
        try:
            stream = await self._store.open(storage_key)
        except LookupError:
            return None
        try:
            return await asyncio.to_thread(stream.read)
        finally:
            await asyncio.to_thread(stream.close)


def _resources_hash(resources: tuple[JSONObject, ...]) -> str:
    canonical = sorted(
        (
            {
                "type": str(item.get("type", "")).strip(),
                "id": str(item.get("id", "")).strip(),
                "access": str(item.get("access", "")).strip(),
            }
            for item in resources
        ),
        key=lambda item: (item["type"], item["id"], item["access"]),
    )
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _domain_approval(value: Approval) -> DomainApproval:
    payload = value.payload
    return DomainApproval(
        approval_id=value.id,
        workspace_id=value.workspace_id,
        run_id=value.run_id,
        step_id=value.step_id,
        resource_id=_required_string(payload, "resource_id"),
        patch_id=_required_string(payload, "patch_id"),
        patch_hash=_required_string(payload, "patch_hash"),
        tool_name=value.tool_name,
        tool_version=value.tool_version,
        input_hash=_required_string(payload, "input_hash"),
        requested_by=ApprovalPrincipal(value.requested_by_type or "", value.requested_by_id or ""),
        required_role="owner_or_admin",
        status=value.status,
        decision=None if value.status == "pending" else value.status,
        decision_reason=value.decision_reason,
        decided_by=(
            None
            if value.decided_by_type is None or value.decided_by_id is None
            else ApprovalPrincipal(value.decided_by_type, value.decided_by_id)
        ),
        created_at=value.created_at,
        decided_at=value.decided_at,
        idempotency_key=value.idempotency_key,
        continuation_step_id=value.continuation_step_id,
        payload=payload,
        reason=value.reason,
    )


def _approval_grant(value: Approval) -> ApprovalGrant:
    payload = value.payload
    return ApprovalGrant(
        approval_id=value.id,
        status=value.status,
        requirement=ApprovalRequirement(
            workspace_id=value.workspace_id,
            run_id=value.run_id,
            step_id=value.step_id,
            resource_id=_required_string(payload, "resource_id"),
            tool_name=ToolName(value.tool_name),
            tool_version=ToolVersion(value.tool_version),
            idempotency_key=value.idempotency_key,
            input_hash=_required_string(payload, "input_hash"),
            patch_hash=_required_string(payload, "patch_hash"),
        ),
    )


def _required_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise RuntimeError(f"审批{field}绑定 缺失")
    return item


def _audit_claim(value: Tool, acquired: bool) -> AuditClaim:
    status = AuditStatus(value.status.value)
    result = _result_from_json(value.output) if status is AuditStatus.SUCCEEDED else None
    error = (
        _error_from_json(value.error)
        if status in {AuditStatus.FAILED, AuditStatus.CANCELLED}
        else None
    )
    return AuditClaim(
        call_id=value.id,
        acquired=acquired,
        recovered=acquired and value.attempt_count > 1,
        status=status,
        result=result,
        error=error,
        attempts=value.attempt_count,
        latency_ms=0,
    )


def _result_json(value: ToolResult) -> JSONObject:
    result: JSONObject = {
        "output": value.output,
        "provenance": cast(JSONValue, [_provenance_item(item) for item in value.provenance]),
    }
    if value.artifact is not None:
        result["artifact"] = cast(
            JSONValue,
            {
                "id": value.artifact.artifact_id,
                "artifact_id": value.artifact.artifact_id,
                "uri": value.artifact.uri,
                "content_hash": value.artifact.content_hash,
                "size_bytes": value.artifact.size_bytes,
                "token_count": 0,
                "workspace_id": value.artifact.workspace_id,
                "run_id": value.artifact.run_id,
                "step_id": value.artifact.step_id,
                "tool_name": str(value.artifact.tool_name),
                "tool_version": str(value.artifact.tool_version),
            },
        )
    return result


def _result_from_json(raw: JSONObject | None) -> ToolResult:
    if raw is None or not isinstance(raw.get("output"), dict):
        raise RuntimeError("已持久化的 工具 结果 无效")
    provenance_raw = raw.get("provenance")
    if not isinstance(provenance_raw, list):
        raise RuntimeError("已持久化的 工具 来源信息 无效")
    provenance = tuple(_provenance_from_item(item) for item in provenance_raw)
    artifact_raw = raw.get("artifact")
    artifact = None
    if artifact_raw is not None:
        if not isinstance(artifact_raw, dict):
            raise RuntimeError("已持久化的 工具 制品 无效")
        artifact = ArtifactReference(
            artifact_id=str(artifact_raw.get("artifact_id") or artifact_raw.get("id") or ""),
            uri=str(artifact_raw.get("uri") or ""),
            content_hash=str(artifact_raw.get("content_hash") or ""),
            size_bytes=int(cast(int, artifact_raw.get("size_bytes", 0))),
            workspace_id=str(artifact_raw.get("workspace_id") or ""),
            run_id=str(artifact_raw.get("run_id") or ""),
            step_id=str(artifact_raw.get("step_id") or ""),
            tool_name=ToolName(str(artifact_raw.get("tool_name") or "")),
            tool_version=ToolVersion(str(artifact_raw.get("tool_version") or "")),
        )
    return ToolResult(
        output=cast(JSONObject, raw["output"]),
        provenance=provenance,
        artifact=artifact,
    )


def _provenance_item(value: Provenance) -> JSONObject:
    item: JSONObject = {
        "source_type": value.source_type,
        "source_id": value.source_id,
        "trust_level": value.trust_level,
    }
    for key, raw in (
        ("resource_id", value.resource_id),
        ("version_id", value.version_id),
        ("content_hash", value.content_hash),
        ("provider", value.provider),
    ):
        if raw is not None:
            item[key] = raw
    return item


def _provenance_from_item(raw: JSONValue) -> Provenance:
    if not isinstance(raw, dict):
        raise RuntimeError("已持久化的 工具 来源信息 项 无效")
    return Provenance(
        source_type=str(raw.get("source_type") or ""),
        source_id=str(raw.get("source_id") or ""),
        trust_level=str(raw.get("trust_level") or ""),
        resource_id=_optional_string(raw.get("resource_id")),
        version_id=_optional_string(raw.get("version_id")),
        content_hash=_optional_string(raw.get("content_hash")),
        provider=_optional_string(raw.get("provider")),
    )


def _error_json(value: ToolError) -> JSONObject:
    result: JSONObject = {"category": value.category.value, "message": value.message}
    if value.details is not None:
        result["details"] = cast(JSONValue, value.details)
    return result


def _error_from_json(raw: JSONObject | None) -> ToolError:
    if raw is None:
        raise RuntimeError("已持久化的 工具 错误 无效")
    details = raw.get("details")
    return ToolError(
        category=ToolErrorCategory(str(raw.get("category") or "internal")),
        message=str(raw.get("message") or "工具执行失败"),
        details=cast(dict[str, str | int] | None, details if isinstance(details, dict) else None),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _provenance_json(request: ArtifactCreate) -> list[JSONObject]:
    values = [_provenance_item(item) for item in request.provenance]
    values.append(
        {
            "source_type": _ARTIFACT_BINDING_SOURCE,
            "source_id": request.blob_key,
            "trust_level": "trusted",
            "resource_id": request.resource_id,
            "tool_name": str(request.tool_name),
            "tool_version": str(request.tool_version),
            "mime_type": request.mime_type,
            "artifact_type": request.artifact_type,
            "size_bytes": request.size_bytes,
            "blob_key": request.blob_key,
            "metadata": request.metadata,
        }
    )
    return values


def _artifact_record(row: tuple[object, ...]) -> ArtifactRecord:
    raw_provenance = row[9]
    if isinstance(raw_provenance, str):
        raw_provenance = json.loads(raw_provenance)
    if not isinstance(raw_provenance, list):
        raise RuntimeError("已持久化的 制品 来源信息 无效")
    items = cast(list[JSONValue], raw_provenance)
    binding = next(
        (
            item
            for item in items
            if isinstance(item, dict) and item.get("source_type") == _ARTIFACT_BINDING_SOURCE
        ),
        None,
    )
    if not isinstance(binding, dict):
        raise RuntimeError("已持久化的 制品 绑定 缺失")
    provenance = tuple(
        _provenance_from_item(item)
        for item in items
        if not (isinstance(item, dict) and item.get("source_type") == _ARTIFACT_BINDING_SOURCE)
    )
    metadata = binding.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return ArtifactRecord(
        artifact_id=str(row[0]),
        workspace_id=str(row[1]),
        run_id=str(row[2]),
        step_id=str(row[3]),
        resource_id=str(binding.get("resource_id") or ""),
        tool_name=ToolName(str(binding.get("tool_name") or "")),
        tool_version=ToolVersion(str(binding.get("tool_version") or "")),
        idempotency_key=str(row[4]),
        data_classification=str(row[5]),
        mime_type=str(binding.get("mime_type") or ""),
        artifact_type=str(binding.get("artifact_type") or ""),
        content_hash=str(row[7]),
        size_bytes=int(cast(int, binding.get("size_bytes", 0))),
        blob_key=str(binding.get("blob_key") or ""),
        metadata=cast(JSONObject, metadata),
        provenance=provenance,
        created_at=cast(datetime, row[10]),
    )


def _artifact_binding_matches(request: ArtifactCreate, record: ArtifactRecord) -> bool:
    return (
        record.workspace_id == request.workspace_id
        and record.run_id == request.run_id
        and record.step_id == request.step_id
        and record.resource_id == request.resource_id
        and record.tool_name == request.tool_name
        and record.tool_version == request.tool_version
        and record.idempotency_key == request.idempotency_key
        and record.data_classification == request.data_classification
        and record.mime_type == request.mime_type
        and record.artifact_type == request.artifact_type
        and record.content_hash == request.content_hash
        and record.size_bytes == request.size_bytes
        and record.blob_key == request.blob_key
        and canonical_json_bytes(record.metadata) == canonical_json_bytes(request.metadata)
        and record.provenance == request.provenance
    )


__all__ = [
    "ARTIFACT_COLUMNS",
    "GET_ARTIFACT_BY_ID_SQL",
    "GET_ARTIFACT_BY_KEY_SQL",
    "INSERT_ARTIFACT_SQL",
    "TOOL_SCOPE_SQL",
    "LocalArtifactBlobStore",
    "PostgresApprovalStore",
    "PostgresArtifactRepository",
    "PostgresToolAudit",
    "PostgresToolPolicy",
    "PostgresToolScopeStore",
]
