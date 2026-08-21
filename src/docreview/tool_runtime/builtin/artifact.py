"""供 Runtime 结果与 artifact.read 使用的有界内容寻址 Artifact 后端。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from docreview.tool_runtime.models import (
    ArtifactReference,
    ArtifactWriteRequest,
    BackendRequest,
    Provenance,
    ToolBackendFailure,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolVersion,
)
from docreview.tool_runtime.schema import (
    JSONObject,
    JSONValue,
    canonical_json_bytes,
    decode_json_object,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_METADATA_KEYS = 32
_MAX_METADATA_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class ArtifactCreate:
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    idempotency_key: str
    data_classification: str
    mime_type: str
    artifact_type: str
    content_hash: str
    size_bytes: int
    blob_key: str
    content: JSONObject
    metadata: JSONObject
    provenance: tuple[Provenance, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    workspace_id: str
    run_id: str
    step_id: str
    resource_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    idempotency_key: str
    data_classification: str
    mime_type: str
    artifact_type: str
    content_hash: str
    size_bytes: int
    blob_key: str
    metadata: JSONObject = field(default_factory=lambda: dict[str, JSONValue]())
    provenance: tuple[Provenance, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ArtifactRepository(Protocol):
    async def create_or_get(self, request: ArtifactCreate) -> tuple[ArtifactRecord, bool]: ...

    async def get(self, workspace_id: str, artifact_id: str) -> ArtifactRecord | None: ...


class ArtifactBlobStore(Protocol):
    async def put(self, key: str, content: bytes, content_hash: str) -> None: ...

    async def get(self, key: str) -> bytes | None: ...


class ArtifactBackend:
    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        blob_store: ArtifactBlobStore,
        max_inline_read_bytes: int = 64 * 1_024,
        now: Clock | None = None,
    ) -> None:
        if not 128 <= max_inline_read_bytes <= 4 * 1_024 * 1_024:
            raise ValueError("制品内联读取限制无效")
        self._repository = repository
        self._blob_store = blob_store
        self._max_inline_read_bytes = max_inline_read_bytes
        self._now = now or (lambda: datetime.now(UTC))

    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference:
        _validate_write_request(request)
        metadata = dict(request.metadata or {})
        create = ArtifactCreate(
            workspace_id=request.workspace_id,
            run_id=request.run_id,
            step_id=request.step_id,
            resource_id=request.resource_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            idempotency_key=request.idempotency_key,
            data_classification="internal",
            mime_type="application/json",
            artifact_type="tool_result",
            content_hash=request.content_hash,
            size_bytes=len(request.content),
            blob_key=request.content_hash,
            content=decode_json_object(request.content),
            metadata=metadata,
            provenance=request.provenance,
            created_at=self._now().astimezone(UTC),
        )
        try:
            await self._blob_store.put(create.blob_key, request.content, request.content_hash)
            record, _ = await self._repository.create_or_get(create)
        except ToolBackendFailure:
            raise
        except Exception as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "artifact persistence failed"
            ) from error
        if not _same_artifact(create, record):
            raise ToolBackendFailure(ToolErrorCategory.IDEMPOTENCY_CONFLICT, "制品 幂等 冲突")
        return _reference(record)

    async def execute(self, request: BackendRequest) -> ToolResult:
        if request.definition.name.value != "artifact.read":
            raise ToolBackendFailure(ToolErrorCategory.PERMANENT_FAILURE, "不支持该制品工具")
        artifact_id = _artifact_id(request.tool_input.get("artifact_id"))
        try:
            record = await self._repository.get(request.context.workspace_id, artifact_id)
        except Exception as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "制品仓库读取失败"
            ) from error
        if record is None:
            raise ToolBackendFailure(ToolErrorCategory.NOT_FOUND, "制品 未找到")
        if (
            record.workspace_id != request.context.workspace_id
            or record.run_id != request.context.run_id
            or record.resource_id != request.context.resource_id
        ):
            raise ToolBackendFailure(
                ToolErrorCategory.UNAUTHORIZED,
                "制品与可信执行范围不匹配",
            )
        try:
            content = await self._blob_store.get(record.blob_key)
        except Exception as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "制品内容读取失败"
            ) from error
        if content is None:
            raise ToolBackendFailure(ToolErrorCategory.NOT_FOUND, "制品 内容 未找到")
        if (
            len(content) != record.size_bytes
            or "sha256:" + hashlib.sha256(content).hexdigest() != record.content_hash
        ):
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "artifact content integrity check failed"
            )
        inline = len(content) <= self._max_inline_read_bytes
        artifact: JSONObject = {
            "id": record.artifact_id,
            "uri": "artifact://" + record.artifact_id,
            "workspace_id": record.workspace_id,
            "data_classification": record.data_classification,
            "mime_type": record.mime_type,
            "type": record.artifact_type,
            "size_bytes": record.size_bytes,
            "content_hash": record.content_hash,
            "summary": f"{record.size_bytes}-byte {record.mime_type} {record.artifact_type}",
            "truncated": not inline,
            "reference": {
                "artifact_id": record.artifact_id,
                "uri": "artifact://" + record.artifact_id,
                "content_hash": record.content_hash,
                "size_bytes": record.size_bytes,
            },
            "created_at": _timestamp(record.created_at),
        }
        if record.metadata:
            artifact["metadata"] = dict(record.metadata)
        if inline:
            try:
                artifact["content"] = decode_json_object(content)
            except ValueError as error:
                raise ToolBackendFailure(
                    ToolErrorCategory.PERMANENT_FAILURE, "制品 内容 无效"
                ) from error
        return ToolResult(
            output={"artifact": artifact},
            provenance=(
                Provenance(
                    source_type="artifact",
                    source_id=record.artifact_id,
                    resource_id=record.resource_id,
                    content_hash=record.content_hash,
                    trust_level="untrusted",
                ),
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        return None


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _validate_write_request(request: ArtifactWriteRequest) -> None:
    values = (
        request.workspace_id,
        request.run_id,
        request.step_id,
        request.resource_id,
        request.idempotency_key,
    )
    if any(not value.strip() or value != value.strip() for value in values):
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "制品 绑定 无效")
    if not request.content or _SHA256.fullmatch(request.content_hash) is None:
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "制品 内容 无效")
    if "sha256:" + hashlib.sha256(request.content).hexdigest() != request.content_hash:
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "制品 内容 哈希 无效")
    metadata = request.metadata
    if metadata is None:
        return
    if len(metadata) > _MAX_METADATA_KEYS or any(
        not key.strip() or key != key.strip() or len(key) > 100 for key in metadata
    ):
        raise ToolBackendFailure(
            ToolErrorCategory.INVALID_INPUT, "artifact metadata exceeds bounds"
        )
    try:
        metadata_bytes = canonical_json_bytes(metadata)
    except (TypeError, ValueError) as error:
        raise ToolBackendFailure(
            ToolErrorCategory.INVALID_INPUT, "artifact metadata is not valid JSON"
        ) from error
    if len(metadata_bytes) > _MAX_METADATA_BYTES:
        raise ToolBackendFailure(
            ToolErrorCategory.INVALID_INPUT, "artifact metadata exceeds bounds"
        )


def _same_artifact(request: ArtifactCreate, record: ArtifactRecord) -> bool:
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


def _reference(record: ArtifactRecord) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=record.artifact_id,
        uri="artifact://" + record.artifact_id,
        content_hash=record.content_hash,
        size_bytes=record.size_bytes,
        workspace_id=record.workspace_id,
        run_id=record.run_id,
        step_id=record.step_id,
        tool_name=record.tool_name,
        tool_version=record.tool_version,
    )


def _artifact_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "://" in value
        or "/" in value
        or "\\" in value
    ):
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "制品 ID 无效")
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ArtifactBackend",
    "ArtifactBlobStore",
    "ArtifactCreate",
    "ArtifactRecord",
    "ArtifactRepository",
]
