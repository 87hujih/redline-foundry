"""Assistant 上传边界的元数据原子写入。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from docreview.document.model import Document
from docreview.knowledge.chunking import (
    REVIEW_STRUCTURE_PROFILE,
    ChunkProjection,
    ChunkTokenizer,
    build_projection,
)
from docreview.storage.filestore import StoredFile
from docreview.storage.models import AssistantMessage, AssistantSession, Resource, ResourceVersion
from docreview.storage.postgres.document_commit import insert_canonical_projection

INSERT_SESSION_SQL = """
INSERT INTO assistant_sessions (
    id, title, workspace_id, created_by_principal_type, created_by_principal_id
)
VALUES (%s, %s, %s, %s, %s)
RETURNING id::text, title, web_search_enabled, last_message_at, created_at, updated_at
"""

LOCK_SESSION_SQL = """
SELECT id::text, title, web_search_enabled, last_message_at, created_at, updated_at
FROM assistant_sessions
WHERE id = %s AND workspace_id = %s
FOR UPDATE
"""

INSERT_RESOURCE_SQL = """
INSERT INTO resources (
    id, title, source_type, source_ref, workspace_id,
    created_by_principal_type, created_by_principal_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING id::text, title, source_type, created_at
"""

INSERT_RESOURCE_VERSION_SQL = """
INSERT INTO resource_versions (id, resource_id, version_number, content, source)
VALUES (%s, %s, %s, %s, %s)
RETURNING id::text, resource_id::text, version_number, content, source, created_at
"""

INSERT_UPLOADED_FILE_SQL = """
INSERT INTO uploaded_files (
    id, resource_id, session_id, original_filename, content_type, size_bytes, sha256,
    storage_key, workspace_id, created_by_principal_type, created_by_principal_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id::text
"""

INSERT_MESSAGE_SQL = """
INSERT INTO assistant_messages (id, session_id, role, kind, sequence_no, payload)
SELECT %s, %s, %s, %s, COALESCE(MAX(message.sequence_no), 0) + 1, %s::jsonb
FROM assistant_messages AS message
WHERE message.session_id = %s
RETURNING id::text, role, kind, payload, sequence_no, created_at
"""

UPDATE_SESSION_TIMESTAMP_SQL = """
UPDATE assistant_sessions
SET last_message_at = %s,
    updated_at = %s,
    selected_resource_id = COALESCE(%s::uuid, selected_resource_id),
    resource_selected_at = CASE
        WHEN %s::uuid IS NULL THEN resource_selected_at
        ELSE %s
    END
WHERE id = %s AND workspace_id = %s
RETURNING id::text, title, web_search_enabled, last_message_at, created_at, updated_at
"""

UPDATE_RESOURCE_VERSION_CANONICAL_SQL = """
UPDATE resource_versions
SET canonical_schema_version = %s, renderer_profile = %s, embedding_profile = %s
WHERE id = %s AND resource_id = %s
"""


@dataclass(frozen=True, slots=True)
class UploadWriteRequest:
    workspace_id: str
    principal_type: str
    principal_id: str
    create_session: bool
    session_id: str
    session_title: str
    resource_id: str | None
    version_id: str | None
    resource_title: str | None
    resource_content: str | None
    file_id: str
    file_name: str
    content_type: str
    stored: StoredFile
    message_id: str
    message_kind: str
    message_payload: dict[str, object]
    error_message: str | None
    canonical_document: Document | None = None


@dataclass(frozen=True, slots=True)
class UploadWriteResult:
    session: AssistantSession
    resource: Resource | None
    version: ResourceVersion | None
    file_id: str
    messages: list[AssistantMessage]
    error_message: str | None


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: tuple[object, ...]) -> Any: ...

    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def __aenter__(self) -> AsyncCursor: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncTransaction(Protocol):
    async def __aenter__(self) -> AsyncTransaction: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...

    def transaction(self) -> AsyncTransaction: ...

    async def __aenter__(self) -> AsyncConnection: ...

    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


type BeforeCommit = Callable[[], Awaitable[None]]


def _session(row: tuple[object, ...]) -> AssistantSession:
    return AssistantSession(
        id=str(row[0]),
        title=str(row[1]),
        web_search_enabled=cast(bool, row[2]),
        last_message_at=cast(datetime, row[3]),
        created_at=cast(datetime, row[4]),
        updated_at=cast(datetime, row[5]),
    )


def _resource(row: tuple[object, ...]) -> Resource:
    return Resource(
        id=str(row[0]),
        title=str(row[1]),
        source_type=str(row[2]),
        created_at=cast(datetime, row[3]),
    )


def _version(row: tuple[object, ...]) -> ResourceVersion:
    return ResourceVersion(
        id=str(row[0]),
        resource_id=str(row[1]),
        version_number=cast(int, row[2]),
        content=str(row[3]),
        source=str(row[4]),
        created_at=cast(datetime, row[5]),
    )


def _message(row: tuple[object, ...]) -> AssistantMessage:
    payload = row[3]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return AssistantMessage(
        id=str(row[0]),
        role=str(row[1]),
        kind=str(row[2]),
        payload=payload,
        sequence_no=cast(int, row[4]),
        created_at=cast(datetime, row[5]),
    )


async def _required_row(cursor: AsyncCursor, fact: str) -> tuple[object, ...]:
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError(f"{fact}写入 未返回数据行")
    return row


def _uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field}必须是 UUID") from error


def _validate(request: UploadWriteRequest) -> None:
    for field, value in (
        ("workspace_id", request.workspace_id),
        ("principal_id", request.principal_id),
        ("session_id", request.session_id),
        ("file_id", request.file_id),
        ("message_id", request.message_id),
    ):
        _uuid(value, field)
    if request.resource_id is None or request.version_id is None:
        if any(
            value is not None
            for value in (
                request.resource_id,
                request.version_id,
                request.resource_title,
                request.resource_content,
            )
        ):
            raise ValueError("资源 事实 必须是 同时提供")
    else:
        _uuid(request.resource_id, "resource_id")
        _uuid(request.version_id, "version_id")
        if request.resource_title is None or request.resource_content is None:
            raise ValueError("资源 事实 必须是 同时提供")
        if request.canonical_document is not None and (
            request.canonical_document.document_id != request.resource_id
            or request.canonical_document.version_id != request.version_id
        ):
            raise ValueError("规范文档身份与上传资源不匹配")
    if request.resource_id is None and request.canonical_document is not None:
        raise ValueError("失败的上传不能包含规范文档")
    if request.principal_type not in {"user", "service"}:
        raise ValueError("principal type must be user or service")


class UploadMetadataRepository:
    def __init__(
        self,
        pool: AsyncPool,
        *,
        tokenizer: ChunkTokenizer | None = None,
        renderer_profile: str = "canonical-v1",
        chunk_profile: str = REVIEW_STRUCTURE_PROFILE.profile_id,
        embedding_profile: str = "embedding-v1",
        require_exact_tokenizer: bool = False,
    ) -> None:
        self._pool = pool
        self._tokenizer = tokenizer
        self._renderer_profile = renderer_profile
        self._chunk_profile = chunk_profile
        self._embedding_profile = embedding_profile
        self._require_exact_tokenizer = require_exact_tokenizer

    async def persist_upload(
        self, request: UploadWriteRequest, before_commit: BeforeCommit
    ) -> UploadWriteResult:
        _validate(request)
        projection = self._projection(request)
        async with self._pool.connection() as connection, connection.transaction():
            # 锁与写入顺序固定为 session -> resource/version -> file -> message。
            session = await self._session(connection, request)
            resource, version = await self._resource_version(connection, request)
            await self._canonical(connection, request, projection)
            await self._uploaded_file(connection, request)
            message = await self._message(connection, request)
            session = await self._update_session(connection, request, message.created_at)
            # 文件发布失败必须发生在 commit 前，使全部元数据随事务一起回滚。
            await before_commit()
        return UploadWriteResult(
            session=session,
            resource=resource,
            version=version,
            file_id=request.file_id,
            messages=[message],
            error_message=request.error_message,
        )

    def _projection(self, request: UploadWriteRequest) -> ChunkProjection | None:
        document = request.canonical_document
        if document is None:
            return None
        return build_projection(
            document,
            tokenizer=self._tokenizer,
            embedding_profile=self._embedding_profile,
            require_exact_tokenizer=self._require_exact_tokenizer,
        )

    async def _canonical(
        self,
        connection: AsyncConnection,
        request: UploadWriteRequest,
        projection: ChunkProjection | None,
    ) -> None:
        document = request.canonical_document
        if document is None or projection is None:
            return
        assert request.resource_id is not None and request.version_id is not None
        async with connection.cursor() as cursor:
            await cursor.execute(
                UPDATE_RESOURCE_VERSION_CANONICAL_SQL,
                (
                    document.schema_version,
                    self._renderer_profile,
                    self._embedding_profile,
                    request.version_id,
                    request.resource_id,
                ),
            )
            await insert_canonical_projection(
                cast(Any, cursor),
                workspace_id=request.workspace_id,
                resource_id=request.resource_id,
                version_id=request.version_id,
                document=document,
                projection=projection,
                renderer_profile=self._renderer_profile,
                chunk_profile=self._chunk_profile,
                embedding_profile=self._embedding_profile,
            )

    async def _session(
        self, connection: AsyncConnection, request: UploadWriteRequest
    ) -> AssistantSession:
        async with connection.cursor() as cursor:
            if request.create_session:
                await cursor.execute(
                    INSERT_SESSION_SQL,
                    (
                        request.session_id,
                        request.session_title,
                        request.workspace_id,
                        request.principal_type,
                        request.principal_id,
                    ),
                )
            else:
                await cursor.execute(
                    LOCK_SESSION_SQL,
                    (request.session_id, request.workspace_id),
                )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("助手 会话 未找到")
        return _session(row)

    async def _resource_version(
        self, connection: AsyncConnection, request: UploadWriteRequest
    ) -> tuple[Resource | None, ResourceVersion | None]:
        if request.resource_id is None:
            return None, None
        async with connection.cursor() as cursor:
            await cursor.execute(
                INSERT_RESOURCE_SQL,
                (
                    request.resource_id,
                    request.resource_title,
                    "upload",
                    request.file_name,
                    request.workspace_id,
                    request.principal_type,
                    request.principal_id,
                ),
            )
            resource = _resource(await _required_row(cursor, "resource"))
        async with connection.cursor() as cursor:
            await cursor.execute(
                INSERT_RESOURCE_VERSION_SQL,
                (
                    request.version_id,
                    request.resource_id,
                    1,
                    request.resource_content,
                    "assistant_upload",
                ),
            )
            version = _version(await _required_row(cursor, "资源版本"))
        return resource, version

    async def _uploaded_file(
        self, connection: AsyncConnection, request: UploadWriteRequest
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                INSERT_UPLOADED_FILE_SQL,
                (
                    request.file_id,
                    request.resource_id,
                    request.session_id,
                    request.file_name,
                    request.content_type,
                    request.stored.size_bytes,
                    request.stored.sha256,
                    request.stored.storage_key,
                    request.workspace_id,
                    request.principal_type,
                    request.principal_id,
                ),
            )
            row = await _required_row(cursor, "已上传文件")
        if str(row[0]) != request.file_id:
            raise RuntimeError("已上传 文件 写入 返回了意外的 ID")

    async def _message(
        self, connection: AsyncConnection, request: UploadWriteRequest
    ) -> AssistantMessage:
        encoded = json.dumps(request.message_payload, ensure_ascii=False, separators=(",", ":"))
        async with connection.cursor() as cursor:
            await cursor.execute(
                INSERT_MESSAGE_SQL,
                (
                    request.message_id,
                    request.session_id,
                    "assistant",
                    request.message_kind,
                    encoded,
                    request.session_id,
                ),
            )
            return _message(await _required_row(cursor, "助手消息"))

    async def _update_session(
        self,
        connection: AsyncConnection,
        request: UploadWriteRequest,
        message_created_at: datetime,
    ) -> AssistantSession:
        async with connection.cursor() as cursor:
            await cursor.execute(
                UPDATE_SESSION_TIMESTAMP_SQL,
                (
                    message_created_at,
                    message_created_at,
                    request.resource_id,
                    request.resource_id,
                    message_created_at,
                    request.session_id,
                    request.workspace_id,
                ),
            )
            return _session(await _required_row(cursor, "助手会话更新"))


__all__ = [
    "INSERT_MESSAGE_SQL",
    "INSERT_RESOURCE_SQL",
    "INSERT_RESOURCE_VERSION_SQL",
    "INSERT_SESSION_SQL",
    "INSERT_UPLOADED_FILE_SQL",
    "LOCK_SESSION_SQL",
    "UPDATE_RESOURCE_VERSION_CANONICAL_SQL",
    "UPDATE_SESSION_TIMESTAMP_SQL",
    "BeforeCommit",
    "UploadMetadataRepository",
    "UploadWriteRequest",
    "UploadWriteResult",
]
