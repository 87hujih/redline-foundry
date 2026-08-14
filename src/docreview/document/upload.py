"""Assistant upload orchestration and frozen DTO mapping."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from docreview.document.ingestion import ingest
from docreview.document.parser import DocumentParser, UnsupportedFileTypeError
from docreview.storage.filestore import LocalFileStore, StoredFile
from docreview.storage.models import AssistantMessage, AssistantSession, Resource
from docreview.storage.postgres.upload_write import UploadWriteRequest, UploadWriteResult


class UploadMetadataWriter(Protocol):
    async def persist_upload(self, request: UploadWriteRequest) -> UploadWriteResult: ...


class UploadCompensationError(RuntimeError):
    """A metadata failure was followed by a failed new-object cleanup."""

    def __init__(self, upload_error: BaseException, cleanup_error: Exception) -> None:
        super().__init__("upload failed and new file cleanup also failed")
        self.upload_error = upload_error
        self.cleanup_error = cleanup_error


def _session_dto(session: AssistantSession) -> dict[str, object]:
    return {
        "id": session.id,
        "title": session.title,
        "web_search_enabled": session.web_search_enabled,
        "last_message_at": session.last_message_at,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _resource_dto(resource: Resource | None) -> dict[str, object] | None:
    if resource is None:
        return None
    return {
        "id": resource.id,
        "title": resource.title,
        "source_type": resource.source_type,
    }


def _message_dto(message: AssistantMessage) -> dict[str, object]:
    return {
        "id": message.id,
        "role": message.role,
        "kind": message.kind,
        "payload": message.payload,
        "sequence_no": message.sequence_no,
        "created_at": message.created_at,
    }


def _upload_dto(result: UploadWriteResult) -> dict[str, object]:
    return {
        "session": _session_dto(result.session),
        "resource": _resource_dto(result.resource),
        "messages": [_message_dto(message) for message in result.messages],
        "error_message": result.error_message,
    }


def _resource_title(file_name: str, block_types: list[tuple[str, str]]) -> str:
    heading = next(
        (
            text.strip()
            for block_type, text in block_types
            if block_type == "heading" and text.strip()
        ),
        "",
    )
    if heading:
        return heading
    fallback = Path(file_name).stem.strip()
    return fallback or file_name.strip()


class DocumentUploadService:
    def __init__(
        self,
        *,
        parser: DocumentParser,
        store: LocalFileStore,
        metadata: UploadMetadataWriter,
    ) -> None:
        self.parser = parser
        self.store = store
        self.metadata = metadata

    async def upload_session(
        self,
        workspace_id: str,
        session_id: str,
        file_name: str,
        content: bytes,
        *,
        principal_type: str,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            UUID(session_id)
        except (AttributeError, ValueError) as error:
            raise ValueError("assistant session id must be a UUID") from error
        return await self._upload(
            workspace_id=workspace_id,
            principal_type=principal_type,
            principal_id=principal_id,
            create_session=False,
            session_id=session_id,
            file_name=file_name,
            content=content,
        )

    async def upload_conversation(
        self,
        workspace_id: str,
        file_name: str,
        content: bytes,
        *,
        principal_type: str,
        principal_id: str,
    ) -> dict[str, object]:
        return await self._upload(
            workspace_id=workspace_id,
            principal_type=principal_type,
            principal_id=principal_id,
            create_session=True,
            session_id=str(uuid4()),
            file_name=file_name,
            content=content,
        )

    async def _upload(
        self,
        *,
        workspace_id: str,
        principal_type: str,
        principal_id: str,
        create_session: bool,
        session_id: str,
        file_name: str,
        content: bytes,
    ) -> dict[str, object]:
        file_name = file_name.strip()
        if not file_name:
            raise ValueError("文件名不能为空")
        if not content:
            raise ValueError("文件内容不能为空")
        if not self.parser.supports(file_name):
            raise UnsupportedFileTypeError(self.parser.unsupported_message(file_name))

        resource_id = str(uuid4())
        version_id = str(uuid4())
        file_id = str(uuid4())
        message_id = str(uuid4())
        stored = await self.store.save(content)
        try:
            try:
                parsed = await ingest(
                    self.parser,
                    document_id=resource_id,
                    version_id=version_id,
                    file_name=file_name,
                    content=content,
                )
            except UnsupportedFileTypeError:
                raise
            except Exception as error:
                error_message = f"文件导入失败：{error}"  # noqa: RUF001
                resource_title = None
                resource_content = None
                persisted_resource_id = None
                persisted_version_id = None
                message_kind = "system"
                payload: dict[str, object] = {
                    "content": error_message,
                    "level": "error",
                }
                session_title = Path(file_name).stem.strip() or file_name
            else:
                resource_title = _resource_title(
                    file_name,
                    [(block.type, block.text) for block in parsed.parsed.blocks],
                )
                resource_content = "\n\n".join(
                    block.text.strip() for block in parsed.parsed.blocks if block.text.strip()
                )
                persisted_resource_id = resource_id
                persisted_version_id = version_id
                message_kind = "session_file"
                error_message = None
                payload = {
                    "file_name": file_name,
                    "file_id": file_id,
                    "resource_id": resource_id,
                    "resource_title": resource_title,
                    "source_type": "upload",
                    "status": "ready",
                }
                session_title = resource_title

            result = await self.metadata.persist_upload(
                UploadWriteRequest(
                    workspace_id=workspace_id,
                    principal_type=principal_type,
                    principal_id=principal_id,
                    create_session=create_session,
                    session_id=session_id,
                    session_title=session_title,
                    resource_id=persisted_resource_id,
                    version_id=persisted_version_id,
                    resource_title=resource_title,
                    resource_content=resource_content,
                    file_id=file_id,
                    file_name=file_name,
                    content_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
                    stored=stored,
                    message_id=message_id,
                    message_kind=message_kind,
                    message_payload=payload,
                    error_message=error_message,
                )
            )
        except BaseException as error:
            try:
                await self._discard_uncommitted(stored)
            except Exception as cleanup_error:
                raise UploadCompensationError(error, cleanup_error) from cleanup_error
            raise
        return _upload_dto(result)

    async def _discard_uncommitted(self, stored: StoredFile) -> None:
        if stored.created:
            await self.store.delete(stored.storage_key)


__all__ = ["DocumentUploadService", "UploadCompensationError", "UploadMetadataWriter"]
