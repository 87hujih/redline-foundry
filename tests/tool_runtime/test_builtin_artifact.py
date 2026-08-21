from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from docreview.tool_runtime import (
    ArtifactWriteRequest,
    BackendRequest,
    Principal,
    Provenance,
    ToolBackendFailure,
    ToolDefinition,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolName,
    ToolRiskLevel,
    ToolVersion,
)
from docreview.tool_runtime.builtin.artifact import (
    ArtifactBackend,
    ArtifactCreate,
    ArtifactRecord,
)
from docreview.tool_runtime.schema import JSONObject


class MemoryArtifactRepository:
    def __init__(self) -> None:
        self.by_id: dict[str, ArtifactRecord] = {}
        self.by_key: dict[tuple[str, str], ArtifactRecord] = {}

    async def create_or_get(self, request: ArtifactCreate) -> tuple[ArtifactRecord, bool]:
        key = (request.workspace_id, request.idempotency_key)
        if key in self.by_key:
            return self.by_key[key], False
        record = ArtifactRecord(
            artifact_id="artifact-1",
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            step_id=request.step_id,
            resource_id=request.resource_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            idempotency_key=request.idempotency_key,
            data_classification=request.data_classification,
            mime_type=request.mime_type,
            artifact_type=request.artifact_type,
            content_hash=request.content_hash,
            size_bytes=request.size_bytes,
            blob_key=request.blob_key,
            metadata=request.metadata,
            provenance=request.provenance,
            created_at=request.created_at,
        )
        self.by_key[key] = record
        self.by_id[record.artifact_id] = record
        return record, True

    async def get(self, workspace_id: str, artifact_id: str) -> ArtifactRecord | None:
        record = self.by_id.get(artifact_id)
        if record is None or record.workspace_id != workspace_id:
            return None
        return record


class MemoryBlobStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes, content_hash: str) -> None:
        existing = self.values.get(key)
        if existing is not None and existing != content:
            raise RuntimeError("blob conflict")
        self.values[key] = bytes(content)

    async def get(self, key: str) -> bytes | None:
        value = self.values.get(key)
        return None if value is None else bytes(value)


def context(
    *,
    workspace_id: str = "workspace-1",
    run_id: str = "run-1",
    resource_id: str = "resource-1",
) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id=run_id,
        step_id="step-read",
        workspace_id=workspace_id,
        resource_id=resource_id,
        principal=Principal(type="user", id="user-1"),
        roles=("owner",),
        trace_id="trace-1",
        attempt=1,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def read_request(
    backend: ArtifactBackend,
    artifact_id: str = "artifact-1",
    *,
    execution_context: ToolExecutionContext | None = None,
) -> BackendRequest:
    return BackendRequest(
        definition=ToolDefinition(
            name=ToolName("artifact.read"),
            version=ToolVersion("1.0.0"),
            description="Read a bounded artifact by immutable ID",
            input_schema='{"type":"object","additionalProperties":false}',
            output_schema='{"type":"object","additionalProperties":false}',
            risk_level=ToolRiskLevel.LOW,
            timeout=timedelta(seconds=10),
            requires_resource=True,
            resource_input_field="artifact_id",
            requires_approval=False,
            max_inline_output_bytes=64_000,
            backend=backend,
        ),
        context=execution_context or context(),
        tool_input={"artifact_id": artifact_id},
        input_hash="sha256:" + "a" * 64,
        idempotency_key="agent-step:step-read",
        backend_attempt=1,
        recovering=False,
    )


@pytest.mark.asyncio
async def test_artifact_create_and_authorized_read_preserve_binding_and_hash() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(
        repository=repository,
        blob_store=blobs,
        now=lambda: datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
    )
    content = b'{"answer":"bounded"}'

    reference = await backend.persist(
        ArtifactWriteRequest(
            workspace_id="workspace-1",
            run_id="run-1",
            step_id="step-write",
            resource_id="resource-1",
            tool_name=ToolName("retrieval.search"),
            tool_version=ToolVersion("2.0.0"),
            idempotency_key="tool-result:agent-step:step-write",
            content=content,
            content_hash="sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756",
            provenance=(
                Provenance(
                    source_type="document_node",
                    source_id="node-1",
                    resource_id="resource-1",
                    version_id="version-1",
                    content_hash="sha256:" + "b" * 64,
                    trust_level="untrusted",
                ),
            ),
        )
    )
    result = await backend.execute(read_request(backend))

    assert reference.artifact_id == "artifact-1"
    assert reference.workspace_id == "workspace-1"
    assert reference.run_id == "run-1"
    assert reference.step_id == "step-write"
    assert reference.tool_name == ToolName("retrieval.search")
    assert reference.tool_version == ToolVersion("2.0.0")
    assert (
        reference.content_hash
        == "sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756"
    )
    artifact = result.output["artifact"]
    assert artifact == {
        "id": "artifact-1",
        "uri": "artifact://artifact-1",
        "workspace_id": "workspace-1",
        "data_classification": "internal",
        "mime_type": "application/json",
        "type": "tool_result",
        "size_bytes": 20,
        "content_hash": "sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756",
        "summary": "20-byte application/json tool_result",
        "truncated": False,
        "content": {"answer": "bounded"},
        "reference": {
            "artifact_id": "artifact-1",
            "uri": "artifact://artifact-1",
            "content_hash": (
                "sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756"
            ),
            "size_bytes": 20,
        },
        "created_at": "2026-08-15T10:00:00Z",
    }
    assert result.provenance[0].source_type == "artifact"
    assert result.provenance[0].trust_level == "untrusted"


@pytest.mark.asyncio
async def test_artifact_metadata_is_bounded_before_store_access() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)
    content = b'{"answer":"bounded"}'
    write = ArtifactWriteRequest(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-write",
        resource_id="resource-1",
        tool_name=ToolName("retrieval.search"),
        tool_version=ToolVersion("2.0.0"),
        idempotency_key="tool-result:agent-step:step-write",
        content=content,
        content_hash="sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756",
        provenance=(Provenance(source_type="test", source_id="source-1", trust_level="untrusted"),),
        metadata={f"key-{index}": index for index in range(33)},
    )

    with pytest.raises(Exception, match="metadata"):
        await backend.persist(write)

    assert repository.by_id == {}
    assert blobs.values == {}


def write_request() -> ArtifactWriteRequest:
    return ArtifactWriteRequest(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id="step-write",
        resource_id="resource-1",
        tool_name=ToolName("retrieval.search"),
        tool_version=ToolVersion("2.0.0"),
        idempotency_key="tool-result:agent-step:step-write",
        content=b'{"answer":"bounded"}',
        content_hash="sha256:0de71bebde5f099d1c593fe29c5f5d484c662cd0754a865e9bde28c99f7f6756",
        provenance=(Provenance(source_type="test", source_id="source-1", trust_level="untrusted"),),
    )


@pytest.mark.asyncio
async def test_artifact_identical_replay_returns_one_record() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)

    first = await backend.persist(write_request())
    replay = await backend.persist(write_request())

    assert replay == first
    assert len(repository.by_id) == 1
    assert len(blobs.values) == 1


@pytest.mark.asyncio
async def test_artifact_same_idempotency_key_with_different_content_conflicts() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)
    await backend.persist(write_request())

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.persist(
            replace(
                write_request(),
                content=b'{"answer":"changed"}',
                content_hash="sha256:a40d90a3d5776c32a52f9c0c650a1dbb8957e36deb4fd573b423588b9d2bff2d",
            )
        )

    assert raised.value.category is ToolErrorCategory.IDEMPOTENCY_CONFLICT
    assert len(repository.by_id) == 1


@pytest.mark.asyncio
async def test_artifact_rejects_incorrect_content_hash_before_store_access() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.persist(replace(write_request(), content_hash="sha256:" + "0" * 64))

    assert raised.value.category is ToolErrorCategory.INVALID_INPUT
    assert repository.by_id == {}
    assert blobs.values == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_context", "expected"),
    [
        (context(workspace_id="workspace-2"), ToolErrorCategory.NOT_FOUND),
        (context(run_id="run-2"), ToolErrorCategory.UNAUTHORIZED),
        (context(resource_id="resource-2"), ToolErrorCategory.UNAUTHORIZED),
    ],
)
async def test_artifact_read_rechecks_workspace_run_and_resource_authorization(
    execution_context: ToolExecutionContext, expected: ToolErrorCategory
) -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)
    await backend.persist(write_request())

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.execute(read_request(backend, execution_context=execution_context))

    assert raised.value.category is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_id", ["../secret", "folder\\secret", "https://example.test/a"])
async def test_artifact_read_rejects_paths_and_urls(artifact_id: str) -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.execute(read_request(backend, artifact_id))

    assert raised.value.category is ToolErrorCategory.INVALID_INPUT


@pytest.mark.asyncio
async def test_artifact_large_content_returns_only_bounded_summary_and_reference() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(
        repository=repository,
        blob_store=blobs,
        max_inline_read_bytes=128,
    )
    content = ('{"answer":"' + "x" * 256 + '"}').encode()
    write = replace(
        write_request(),
        content=content,
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
    )
    await backend.persist(write)

    result = await backend.execute(read_request(backend))

    artifact = result.output["artifact"]
    assert isinstance(artifact, dict)
    artifact = cast(JSONObject, artifact)
    assert artifact["truncated"] is True
    assert "content" not in artifact
    reference = cast(JSONObject, artifact["reference"])
    assert reference["artifact_id"] == "artifact-1"


@pytest.mark.asyncio
async def test_artifact_read_fails_closed_when_blob_hash_changes() -> None:
    repository = MemoryArtifactRepository()
    blobs = MemoryBlobStore()
    backend = ArtifactBackend(repository=repository, blob_store=blobs)
    await backend.persist(write_request())
    blobs.values[write_request().content_hash] = b'{"answer":"tampered"}'

    with pytest.raises(ToolBackendFailure, match="integrity"):
        await backend.execute(read_request(backend))


@pytest.mark.asyncio
async def test_artifact_store_failure_does_not_log_or_expose_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingBlobStore(MemoryBlobStore):
        async def put(self, key: str, content: bytes, content_hash: str) -> None:
            raise RuntimeError("secret-content=" + content.decode())

    backend = ArtifactBackend(
        repository=MemoryArtifactRepository(),
        blob_store=FailingBlobStore(),
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.persist(write_request())

    assert str(raised.value) == "artifact persistence failed"
    assert "bounded" not in caplog.text
    assert "bounded" not in str(raised.value)


@pytest.mark.asyncio
async def test_artifact_repository_failure_is_safely_classified() -> None:
    class FailingRepository(MemoryArtifactRepository):
        async def create_or_get(self, request: ArtifactCreate) -> tuple[ArtifactRecord, bool]:
            raise RuntimeError("repository-secret")

    backend = ArtifactBackend(repository=FailingRepository(), blob_store=MemoryBlobStore())

    with pytest.raises(ToolBackendFailure) as raised:
        await backend.persist(write_request())

    assert raised.value.category is ToolErrorCategory.PERMANENT_FAILURE
    assert str(raised.value) == "artifact persistence failed"
