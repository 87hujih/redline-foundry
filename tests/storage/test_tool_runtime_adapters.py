from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from docreview.approval.models import (
    ApprovalBinding,
    ApprovalCreateCommand,
)
from docreview.approval.models import (
    Principal as ApprovalPrincipal,
)
from docreview.runtime.models import Approval, Tool, ToolStatus
from docreview.storage.filestore import LocalFileStore
from docreview.storage.postgres.runtime_repository import RuntimeRepository
from docreview.storage.postgres.tool_runtime import (
    INSERT_ARTIFACT_SQL,
    TOOL_SCOPE_SQL,
    LocalArtifactBlobStore,
    PostgresApprovalStore,
    PostgresArtifactRepository,
    PostgresToolAudit,
    PostgresToolPolicy,
    PostgresToolScopeStore,
)
from docreview.tool_runtime.builtin.artifact import ArtifactCreate
from docreview.tool_runtime.models import (
    AuditClaimRequest,
    AuditFinishRequest,
    AuditStatus,
    IdempotencyConflictError,
    PolicyRequest,
    Principal,
    Provenance,
    ToolBackendFailure,
    ToolDefinition,
    ToolError,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolName,
    ToolResult,
    ToolRiskLevel,
    ToolVersion,
)
from docreview.tool_runtime.schema import JSONObject, canonical_json_bytes, canonical_json_hash


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
        self.commits = 0

    def cursor(self) -> Cursor:
        return self._cursor

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Pool:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def connection(self) -> Connection:
        return self._connection


@pytest.mark.asyncio
async def test_tool_scope_is_loaded_from_one_exact_running_step_query() -> None:
    deadline = datetime(2026, 8, 15, 12, 5, tzinfo=UTC)
    cursor = Cursor(
        (
            "request-1",
            "run-1",
            "step-1",
            "workspace-1",
            "resource-1",
            "user",
            "user-1",
            "editor",
            "trace-1",
            2,
            deadline,
            "workspace-1",
        )
    )
    store = PostgresToolScopeStore(cast(Any, Pool(Connection(cursor))))

    value = await store.load_tool_scope("run-1", "step-1")

    assert cursor.query == TOOL_SCOPE_SQL
    assert cursor.params == ("run-1", "step-1")
    assert value.context.workspace_id == "workspace-1"
    assert value.context.resource_id == "resource-1"
    assert value.context.roles == ("editor",)
    assert value.context.deadline == deadline
    assert "step.status = 'running'" in TOOL_SCOPE_SQL
    assert "resource.workspace_id = run.workspace_id" in TOOL_SCOPE_SQL


class Resolver:
    def __init__(self, *, permission: bool = True, resource: bool = True) -> None:
        self.permission = permission
        self.resource = resource
        self.permissions: list[str] = []

    async def has_permission(self, scope: object, permission: str) -> bool:
        self.permissions.append(permission)
        return self.permission

    async def authorize_resource(
        self, scope: object, resource: object, *, bound_resource_id: str | None = None
    ) -> bool:
        return self.resource and bound_resource_id == "resource-1"


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name=ToolName("document.read_nodes"),
        version=ToolVersion("1.0.0"),
        description="read",
        input_schema=(
            '{"type":"object","properties":{"resource_id":{"type":"string"}},'
            '"required":["resource_id"],"additionalProperties":false}'
        ),
        output_schema=(
            '{"type":"object","properties":{"ok":{"type":"boolean"}},'
            '"required":["ok"],"additionalProperties":false}'
        ),
        risk_level=ToolRiskLevel.LOW,
        timeout=timedelta(seconds=5),
        requires_resource=True,
        requires_approval=False,
        max_inline_output_bytes=1024,
        backend=object(),
        resource_input_field="resource_id",
        required_permissions=("document.read",),
        resource_type="document",
        resource_access="read",
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id="run-1",
        step_id="step-1",
        workspace_id="workspace-1",
        resource_id="resource-1",
        principal=Principal("user", "user-1"),
        roles=("editor",),
        trace_id="trace-1",
        attempt=1,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_tool_policy_requires_permission_and_resource_ownership() -> None:
    resolver = Resolver()
    policy = PostgresToolPolicy(cast(Any, resolver))
    decision = await policy.authorize(
        PolicyRequest(
            definition=_definition(),
            context=_context(),
            tool_input={"resource_id": "resource-1"},
            input_hash=canonical_json_hash({"resource_id": "resource-1"}),
        )
    )
    assert decision.allowed
    assert resolver.permissions == ["document.read"]

    denied = PostgresToolPolicy(cast(Any, Resolver(resource=False)))
    assert not (
        await denied.authorize(
            PolicyRequest(
                definition=_definition(),
                context=_context(),
                tool_input={"resource_id": "resource-1"},
                input_hash=canonical_json_hash({"resource_id": "resource-1"}),
            )
        )
    ).allowed


def _tool(*, status: ToolStatus = ToolStatus.RUNNING, output: dict[str, Any] | None = None) -> Tool:
    now = datetime.now(UTC)
    return Tool(
        id="call-1",
        run_id="run-1",
        step_id="step-1",
        tool_name="document.read_nodes",
        tool_version="1.0.0",
        input={"resource_id": "resource-1"},
        output=output,
        status=status,
        idempotency_key="agent-step:step-1",
        error=None,
        error_category=None,
        claimed_by="worker-1" if status is ToolStatus.RUNNING else None,
        lease_expires_at=now + timedelta(minutes=1) if status is ToolStatus.RUNNING else None,
        lease_generation=1,
        attempt_count=1,
        started_at=now,
        completed_at=None,
        created_at=now,
    )


class AuditRepository:
    def __init__(self) -> None:
        self.value = _tool()
        self.begin_input: dict[str, Any] | None = None
        self.finished: tuple[object, ...] | None = None

    async def begin_tool(self, *args: object) -> tuple[Tool, bool]:
        self.begin_input = cast(dict[str, Any], args[4])
        return self.value, True

    async def get_tool_by_id(self, tool_id: str) -> Tool | None:
        return self.value if tool_id == self.value.id else None

    async def finish_tool(self, *args: object) -> None:
        self.finished = args


@pytest.mark.asyncio
async def test_tool_audit_persists_input_and_stable_result_envelope() -> None:
    repository = AuditRepository()
    audit = PostgresToolAudit(
        cast(RuntimeRepository, repository),
        worker_id="worker-1",
        lease_duration=timedelta(minutes=1),
    )
    tool_input: JSONObject = {"resource_id": "resource-1"}
    claim = await audit.claim(
        AuditClaimRequest(
            run_id="run-1",
            step_id="step-1",
            tool_name=ToolName("document.read_nodes"),
            tool_version=ToolVersion("1.0.0"),
            idempotency_key="agent-step:step-1",
            tool_input=tool_input,
            input_hash=canonical_json_hash(tool_input),
            attempt=1,
            started_at=datetime.now(UTC),
        )
    )
    assert claim.acquired
    assert repository.begin_input == tool_input

    result = ToolResult(
        output={"ok": True},
        provenance=(Provenance("document", "node-1", "untrusted"),),
    )
    await audit.finish(
        AuditFinishRequest(
            call_id="call-1",
            status=AuditStatus.SUCCEEDED,
            result=result,
            error=None,
            attempt=1,
            backend_attempts=1,
            latency_ms=3,
            completed_at=datetime.now(UTC),
        )
    )
    assert repository.finished is not None
    persisted = cast(dict[str, Any], repository.finished[2])
    assert persisted["output"] == {"ok": True}
    assert persisted["provenance"][0]["source_id"] == "node-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_category", "database_category"),
    [
        (ToolErrorCategory.UNAUTHORIZED, "permission_denied"),
        (ToolErrorCategory.APPROVAL_REQUIRED, "policy_blocked"),
        (ToolErrorCategory.INVALID_OUTPUT, "terminal_upstream"),
        (ToolErrorCategory.PERMANENT_FAILURE, "terminal_upstream"),
        (ToolErrorCategory.IDEMPOTENCY_CONFLICT, "conflict"),
    ],
)
async def test_tool_audit_maps_runtime_errors_to_frozen_database_categories(
    tool_category: ToolErrorCategory, database_category: str
) -> None:
    repository = AuditRepository()
    audit = PostgresToolAudit(
        cast(RuntimeRepository, repository),
        worker_id="worker-1",
        lease_duration=timedelta(minutes=1),
    )

    await audit.finish(
        AuditFinishRequest(
            call_id="call-1",
            status=AuditStatus.FAILED,
            result=None,
            error=ToolError(tool_category, "bounded failure"),
            attempt=1,
            backend_attempts=1,
            latency_ms=3,
            completed_at=datetime.now(UTC),
        )
    )

    assert repository.finished is not None
    assert repository.finished[4] == database_category


@pytest.mark.asyncio
async def test_tool_audit_rejects_changed_input_before_repository_claim() -> None:
    repository = AuditRepository()
    audit = PostgresToolAudit(
        cast(RuntimeRepository, repository),
        worker_id="worker-1",
        lease_duration=timedelta(minutes=1),
    )
    with pytest.raises(IdempotencyConflictError, match="input hash mismatch"):
        await audit.claim(
            AuditClaimRequest(
                run_id="run-1",
                step_id="step-1",
                tool_name=ToolName("document.read_nodes"),
                tool_version=ToolVersion("1.0.0"),
                idempotency_key="agent-step:step-1",
                tool_input={"resource_id": "changed"},
                input_hash=canonical_json_hash({"resource_id": "resource-1"}),
                attempt=1,
                started_at=datetime.now(UTC),
            )
        )
    assert repository.begin_input is None


class ApprovalRepository:
    def __init__(self) -> None:
        self.command: object | None = None

    async def request_approval(self, command: object) -> Approval:
        self.command = command
        value = cast(Any, command)
        return Approval(
            id="approval-1",
            workspace_id=value.workspace_id,
            run_id=value.run_id,
            step_id=value.step_id,
            tool_name=value.tool_name,
            tool_version=value.tool_version,
            idempotency_key=value.idempotency_key,
            resources=list(value.resources),
            resources_hash=value.resources_hash,
            payload=value.payload,
            reason=value.reason,
            status="pending",
            requested_by_type=value.requested_by_type,
            requested_by_id=value.requested_by_id,
            created_at=datetime.now(UTC),
        )

    async def get_approval_by_id(self, approval_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_approval_store_creates_a_bound_pending_database_fact() -> None:
    repository = ApprovalRepository()
    store = PostgresApprovalStore(cast(RuntimeRepository, repository))
    binding = ApprovalBinding(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-1",
        resource_id="resource-1",
        patch_id="patch-1",
        patch_hash="sha256:" + "a" * 64,
        tool_name="document.commit_patch",
        tool_version="1.0.0",
        input_hash="sha256:" + "b" * 64,
        idempotency_key="commit-1",
        target_version_id="version-1",
    )
    value = await store.create(
        ApprovalCreateCommand(
            binding=binding,
            reason="external approval required",
            payload={},
            requested_by=ApprovalPrincipal("user", "user-1"),
            source="tool_runtime",
        )
    )
    assert value.status == "pending"
    command = cast(Any, repository.command)
    assert command.payload["patch_hash"] == binding.patch_hash
    assert command.resources == ({"type": "document", "id": "resource-1", "access": "write"},)
    assert command.resources_hash.startswith("sha256:")


class ArtifactCursor(Cursor):
    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        await super().execute(query, params)
        if query == INSERT_ARTIFACT_SQL:
            self.row = (
                "artifact-1",
                params[0],
                params[1],
                params[2],
                params[3],
                params[4],
                params[5],
                params[6],
                params[7],
                params[8],
                datetime.now(UTC),
            )


@pytest.mark.asyncio
async def test_artifact_repository_writes_stable_json_and_binding() -> None:
    content: JSONObject = {"answer": "stored"}
    raw = canonical_json_bytes(content)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    cursor = ArtifactCursor(None)
    connection = Connection(cursor)
    repository = PostgresArtifactRepository(cast(Any, Pool(connection)))
    request = ArtifactCreate(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-1",
        resource_id="resource-1",
        tool_name=ToolName("retrieval.search"),
        tool_version=ToolVersion("2.0.0"),
        idempotency_key="tool-result:step-1",
        data_classification="internal",
        mime_type="application/json",
        artifact_type="tool_result",
        content_hash=digest,
        size_bytes=len(raw),
        blob_key=digest,
        content=content,
        metadata={},
        provenance=(Provenance("retrieval", "set-1", "untrusted", "resource-1"),),
        created_at=datetime.now(UTC),
    )

    record, created = await repository.create_or_get(request)

    assert created
    assert connection.commits == 1
    assert record.resource_id == "resource-1"
    assert record.provenance == request.provenance
    assert cursor.params[5] == raw.decode()
    assert "python_tool_artifact_binding" in str(cursor.params[8])


@pytest.mark.asyncio
async def test_artifact_repository_rejects_a_false_content_hash() -> None:
    repository = PostgresArtifactRepository(cast(Any, Pool(Connection(Cursor(None)))))
    request = ArtifactCreate(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-1",
        resource_id="resource-1",
        tool_name=ToolName("retrieval.search"),
        tool_version=ToolVersion("2.0.0"),
        idempotency_key="tool-result:step-1",
        data_classification="internal",
        mime_type="application/json",
        artifact_type="tool_result",
        content_hash="sha256:" + "0" * 64,
        size_bytes=2,
        blob_key="sha256:" + "0" * 64,
        content={"answer": "changed"},
        metadata={},
        provenance=(Provenance("retrieval", "set-1", "untrusted", "resource-1"),),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ToolBackendFailure) as raised:
        await repository.create_or_get(request)
    assert raised.value.category is ToolErrorCategory.INVALID_INPUT


@pytest.mark.asyncio
async def test_local_artifact_blob_store_round_trip(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "artifacts")
    blobs = LocalArtifactBlobStore(store)
    content = b'{"answer":"stored"}'
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    await blobs.put(digest, content, digest)
    assert await blobs.get(digest) == content
    await store.aclose()
