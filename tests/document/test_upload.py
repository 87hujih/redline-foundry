from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from docreview.document.parser import DocumentParser
from docreview.document.upload import DocumentUploadService, UploadCompensationError
from docreview.storage.filestore import LocalFileStore
from docreview.storage.models import AssistantMessage, AssistantSession, Resource, ResourceVersion
from docreview.storage.postgres.upload_write import UploadWriteRequest, UploadWriteResult

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
PRINCIPAL_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@dataclass
class MetadataWriter:
    request: UploadWriteRequest | None = None

    async def persist_upload(self, request: UploadWriteRequest) -> UploadWriteResult:
        self.request = request
        resource = (
            None
            if request.resource_id is None
            else Resource(request.resource_id, str(request.resource_title), "upload", NOW)
        )
        version = (
            None
            if request.version_id is None or request.resource_id is None
            else ResourceVersion(
                request.version_id,
                request.resource_id,
                1,
                str(request.resource_content),
                "assistant_upload",
                NOW,
            )
        )
        return UploadWriteResult(
            session=AssistantSession(
                request.session_id,
                request.session_title,
                False,
                NOW,
                NOW,
                NOW,
            ),
            resource=resource,
            version=version,
            file_id=request.file_id,
            messages=[
                AssistantMessage(
                    request.message_id,
                    "assistant",
                    request.message_kind,
                    request.message_payload,
                    1,
                    NOW,
                )
            ],
            error_message=request.error_message,
        )


class FailingTika:
    async def parse(self, file_name: str, content: bytes) -> str:
        raise RuntimeError("parser offline")


@dataclass
class FailingMetadataWriter:
    request: UploadWriteRequest | None = None

    async def persist_upload(self, request: UploadWriteRequest) -> UploadWriteResult:
        self.request = request
        raise RuntimeError("database transaction failed")


@dataclass
class CancellingMetadataWriter:
    request: UploadWriteRequest | None = None

    async def persist_upload(self, request: UploadWriteRequest) -> UploadWriteResult:
        self.request = request
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_conversation_upload_returns_frozen_dto_with_owned_uuid_fact_graph(
    tmp_path: Path,
) -> None:
    metadata = MetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    dto = await service.upload_conversation(
        WORKSPACE_ID,
        "review.md",
        b"# Review\n\nBody",
        principal_type="user",
        principal_id=PRINCIPAL_ID,
    )

    request = metadata.request
    assert request is not None
    assert request.workspace_id == WORKSPACE_ID
    assert request.principal_type == "user"
    assert request.principal_id == PRINCIPAL_ID
    assert request.create_session is True
    assert all(
        UUID(value).version == 4
        for value in (
            str(request.resource_id),
            str(request.version_id),
            request.file_id,
            request.session_id,
            request.message_id,
        )
    )
    assert request.resource_id is not None
    assert request.message_payload == {
        "file_name": "review.md",
        "file_id": request.file_id,
        "resource_id": request.resource_id,
        "resource_title": "Review",
        "source_type": "upload",
        "status": "ready",
    }
    assert request.stored.created is True
    assert await store.stat(request.stored.storage_key) == len(b"# Review\n\nBody")

    assert set(dto) == {"session", "resource", "messages", "error_message"}
    assert dto["session"] == {
        "id": request.session_id,
        "title": "Review",
        "web_search_enabled": False,
        "last_message_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    assert dto["resource"] == {
        "id": request.resource_id,
        "title": "Review",
        "source_type": "upload",
    }
    assert dto["messages"] == [
        {
            "id": request.message_id,
            "role": "assistant",
            "kind": "session_file",
            "payload": request.message_payload,
            "sequence_no": 1,
            "created_at": NOW,
        }
    ]
    assert dto["error_message"] is None


@pytest.mark.asyncio
async def test_parser_failure_persists_one_complete_failure_dto_and_no_resource(
    tmp_path: Path,
) -> None:
    metadata = MetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(mode="tika", tika=FailingTika()),
        store=store,
        metadata=metadata,
    )

    dto = await service.upload_conversation(
        WORKSPACE_ID,
        "review.pdf",
        b"%PDF-1.7",
        principal_type="user",
        principal_id=PRINCIPAL_ID,
    )

    request = metadata.request
    assert request is not None
    assert request.resource_id is None
    assert request.version_id is None
    assert request.resource_title is None
    assert request.resource_content is None
    assert request.message_kind == "system"
    assert request.error_message == "文件导入失败：Tika 解析失败"  # noqa: RUF001
    assert request.message_payload == {
        "content": "文件导入失败：Tika 解析失败",  # noqa: RUF001
        "level": "error",
    }
    assert await store.stat(request.stored.storage_key) == len(b"%PDF-1.7")
    assert dto == {
        "session": {
            "id": request.session_id,
            "title": "review",
            "web_search_enabled": False,
            "last_message_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        },
        "resource": None,
        "messages": [
            {
                "id": request.message_id,
                "role": "assistant",
                "kind": "system",
                "payload": request.message_payload,
                "sequence_no": 1,
                "created_at": NOW,
            }
        ],
        "error_message": "文件导入失败：Tika 解析失败",  # noqa: RUF001
    }


@pytest.mark.asyncio
async def test_database_failure_removes_only_a_newly_created_file(tmp_path: Path) -> None:
    metadata = FailingMetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    with pytest.raises(RuntimeError, match="database transaction failed"):
        await service.upload_conversation(
            WORKSPACE_ID,
            "review.md",
            b"# Review\n\nBody",
            principal_type="user",
            principal_id=PRINCIPAL_ID,
        )

    request = metadata.request
    assert request is not None and request.stored.created is True
    assert await store.stat(request.stored.storage_key) is None


@pytest.mark.asyncio
async def test_database_failure_keeps_a_preexisting_content_addressed_file(
    tmp_path: Path,
) -> None:
    metadata = FailingMetadataWriter()
    store = LocalFileStore(tmp_path)
    existing = await store.save(b"# Review\n\nBody")
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    with pytest.raises(RuntimeError, match="database transaction failed"):
        await service.upload_conversation(
            WORKSPACE_ID,
            "review.md",
            b"# Review\n\nBody",
            principal_type="user",
            principal_id=PRINCIPAL_ID,
        )

    request = metadata.request
    assert request is not None and request.stored.created is False
    assert request.stored.storage_key == existing.storage_key
    assert await store.stat(existing.storage_key) == len(b"# Review\n\nBody")


@pytest.mark.asyncio
async def test_cancelled_database_transaction_removes_a_new_file(tmp_path: Path) -> None:
    metadata = CancellingMetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.upload_conversation(
            WORKSPACE_ID,
            "review.md",
            b"# Review\n\nBody",
            principal_type="user",
            principal_id=PRINCIPAL_ID,
        )

    request = metadata.request
    assert request is not None
    assert await store.stat(request.stored.storage_key) is None


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_both_failure_causes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = FailingMetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    async def fail_delete(storage_key: str) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(store, "delete", fail_delete)

    with pytest.raises(UploadCompensationError) as captured:
        await service.upload_conversation(
            WORKSPACE_ID,
            "review.md",
            b"# Review\n\nBody",
            principal_type="user",
            principal_id=PRINCIPAL_ID,
        )

    assert isinstance(captured.value.upload_error, RuntimeError)
    assert str(captured.value.upload_error) == "database transaction failed"
    assert isinstance(captured.value.cleanup_error, OSError)
    assert str(captured.value.cleanup_error) == "cleanup failed"


@pytest.mark.asyncio
async def test_file_publication_failure_never_starts_metadata_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = MetadataWriter()
    store = LocalFileStore(tmp_path)
    service = DocumentUploadService(
        parser=DocumentParser(),
        store=store,
        metadata=metadata,
    )

    def fail_replace(source: Path, target: Path) -> Path:
        raise OSError("disk write failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk write failed"):
        await service.upload_conversation(
            WORKSPACE_ID,
            "review.md",
            b"# Review\n\nBody",
            principal_type="user",
            principal_id=PRINCIPAL_ID,
        )

    assert metadata.request is None
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
