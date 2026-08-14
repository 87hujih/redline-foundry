from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from docreview.storage.filestore import StoredFile
from docreview.storage.postgres.upload_write import (
    INSERT_MESSAGE_SQL,
    INSERT_RESOURCE_SQL,
    INSERT_RESOURCE_VERSION_SQL,
    INSERT_SESSION_SQL,
    INSERT_UPLOADED_FILE_SQL,
    LOCK_SESSION_SQL,
    UPDATE_SESSION_TIMESTAMP_SQL,
    UploadMetadataRepository,
    UploadWriteRequest,
)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
PRINCIPAL_ID = "22222222-2222-4222-8222-222222222222"
RESOURCE_ID = "33333333-3333-4333-8333-333333333333"
VERSION_ID = "44444444-4444-4444-8444-444444444444"
FILE_ID = "55555555-5555-4555-8555-555555555555"
SESSION_ID = "66666666-6666-4666-8666-666666666666"
MESSAGE_ID = "77777777-7777-4777-8777-777777777777"
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class Transaction:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> Transaction:
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            if self.connection.fail_commit:
                self.connection.rolled_back = True
                raise RuntimeError("injected transaction commit failure")
            self.connection.committed = True
        else:
            self.connection.rolled_back = True


class Cursor:
    def __init__(self, fail_query: str | None = None) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.current_query = ""
        self.fail_query = fail_query

    async def __aenter__(self) -> Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...]) -> Any:
        self.current_query = query
        self.executions.append((query, params))
        if query == self.fail_query:
            raise RuntimeError("injected persistence failure")

    async def fetchone(self) -> tuple[object, ...] | None:
        rows: dict[str, tuple[object, ...]] = {
            INSERT_SESSION_SQL: (SESSION_ID, "Review", False, NOW, NOW, NOW),
            LOCK_SESSION_SQL: (SESSION_ID, "Review", False, NOW, NOW, NOW),
            INSERT_RESOURCE_SQL: (RESOURCE_ID, "Review", "upload", NOW),
            INSERT_RESOURCE_VERSION_SQL: (
                VERSION_ID,
                RESOURCE_ID,
                1,
                "# Review\nBody",
                "assistant_upload",
                NOW,
            ),
            INSERT_UPLOADED_FILE_SQL: (FILE_ID,),
            INSERT_MESSAGE_SQL: (
                MESSAGE_ID,
                "assistant",
                "session_file",
                {
                    "file_name": "review.md",
                    "file_id": FILE_ID,
                    "resource_id": RESOURCE_ID,
                    "resource_title": "Review",
                    "source_type": "upload",
                    "status": "ready",
                },
                1,
                NOW,
            ),
            UPDATE_SESSION_TIMESTAMP_SQL: (SESSION_ID, "Review", False, NOW, NOW, NOW),
        }
        return rows[self.current_query]


class Connection:
    def __init__(self, fail_query: str | None = None, *, fail_commit: bool = False) -> None:
        self.cursor_value = Cursor(fail_query)
        self.fail_commit = fail_commit
        self.transaction_entries = 0
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def cursor(self) -> Cursor:
        return self.cursor_value

    def transaction(self) -> Any:
        return Transaction(self)


class Pool:
    def __init__(self, fail_query: str | None = None, *, fail_commit: bool = False) -> None:
        self.connection_value = Connection(fail_query, fail_commit=fail_commit)

    def connection(self) -> Connection:
        return self.connection_value


@pytest.mark.asyncio
async def test_persist_upload_creates_one_owned_fact_graph_in_one_transaction() -> None:
    pool = Pool()
    repository = UploadMetadataRepository(pool)
    payload: dict[str, object] = {
        "file_name": "review.md",
        "file_id": FILE_ID,
        "resource_id": RESOURCE_ID,
        "resource_title": "Review",
        "source_type": "upload",
        "status": "ready",
    }

    result = await repository.persist_upload(
        UploadWriteRequest(
            workspace_id=WORKSPACE_ID,
            principal_type="user",
            principal_id=PRINCIPAL_ID,
            create_session=True,
            session_id=SESSION_ID,
            session_title="Review",
            resource_id=RESOURCE_ID,
            version_id=VERSION_ID,
            resource_title="Review",
            resource_content="# Review\nBody",
            file_id=FILE_ID,
            file_name="review.md",
            content_type="text/markdown",
            stored=StoredFile(
                sha256="a" * 64,
                size_bytes=13,
                storage_key="aa/" + "a" * 64,
                created=True,
            ),
            message_id=MESSAGE_ID,
            message_kind="session_file",
            message_payload=payload,
            error_message=None,
        )
    )

    assert pool.connection_value.transaction_entries == 1
    assert pool.connection_value.committed is True
    assert pool.connection_value.rolled_back is False
    assert result.session.id == SESSION_ID
    assert result.resource is not None and result.resource.id == RESOURCE_ID
    assert result.messages[0].id == MESSAGE_ID
    assert all(
        UUID(value).version == 4
        for value in (RESOURCE_ID, VERSION_ID, FILE_ID, SESSION_ID, MESSAGE_ID)
    )

    executions = pool.connection_value.cursor_value.executions
    assert {query for query, _ in executions} == {
        INSERT_SESSION_SQL,
        INSERT_RESOURCE_SQL,
        INSERT_RESOURCE_VERSION_SQL,
        INSERT_UPLOADED_FILE_SQL,
        INSERT_MESSAGE_SQL,
        UPDATE_SESSION_TIMESTAMP_SQL,
    }
    assert next(params for query, params in executions if query == INSERT_RESOURCE_SQL) == (
        RESOURCE_ID,
        "Review",
        "upload",
        "review.md",
        WORKSPACE_ID,
        "user",
        PRINCIPAL_ID,
    )
    assert next(params for query, params in executions if query == INSERT_RESOURCE_VERSION_SQL) == (
        VERSION_ID,
        RESOURCE_ID,
        1,
        "# Review\nBody",
        "assistant_upload",
    )
    assert next(params for query, params in executions if query == INSERT_UPLOADED_FILE_SQL) == (
        FILE_ID,
        RESOURCE_ID,
        SESSION_ID,
        "review.md",
        "text/markdown",
        13,
        "a" * 64,
        "aa/" + "a" * 64,
        WORKSPACE_ID,
        "user",
        PRINCIPAL_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_query",
    [
        INSERT_SESSION_SQL,
        INSERT_RESOURCE_SQL,
        INSERT_RESOURCE_VERSION_SQL,
        INSERT_UPLOADED_FILE_SQL,
        INSERT_MESSAGE_SQL,
        UPDATE_SESSION_TIMESTAMP_SQL,
    ],
)
async def test_any_upload_fact_failure_rolls_back_the_whole_transaction(
    fail_query: str,
) -> None:
    pool = Pool(fail_query)
    repository = UploadMetadataRepository(pool)

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await repository.persist_upload(
            UploadWriteRequest(
                workspace_id=WORKSPACE_ID,
                principal_type="user",
                principal_id=PRINCIPAL_ID,
                create_session=True,
                session_id=SESSION_ID,
                session_title="Review",
                resource_id=RESOURCE_ID,
                version_id=VERSION_ID,
                resource_title="Review",
                resource_content="# Review\nBody",
                file_id=FILE_ID,
                file_name="review.md",
                content_type="text/markdown",
                stored=StoredFile(
                    sha256="a" * 64,
                    size_bytes=13,
                    storage_key="aa/" + "a" * 64,
                    created=True,
                ),
                message_id=MESSAGE_ID,
                message_kind="session_file",
                message_payload={"status": "ready"},
                error_message=None,
            )
        )

    assert pool.connection_value.transaction_entries == 1
    assert pool.connection_value.rolled_back is True
    assert pool.connection_value.committed is False


@pytest.mark.asyncio
async def test_existing_session_upload_locks_the_exact_workspace_before_writing() -> None:
    pool = Pool()
    repository = UploadMetadataRepository(pool)

    await repository.persist_upload(
        UploadWriteRequest(
            workspace_id=WORKSPACE_ID,
            principal_type="user",
            principal_id=PRINCIPAL_ID,
            create_session=False,
            session_id=SESSION_ID,
            session_title="ignored for an existing session",
            resource_id=RESOURCE_ID,
            version_id=VERSION_ID,
            resource_title="Review",
            resource_content="Body",
            file_id=FILE_ID,
            file_name="review.md",
            content_type="text/markdown",
            stored=StoredFile(
                sha256="a" * 64,
                size_bytes=4,
                storage_key="aa/" + "a" * 64,
                created=True,
            ),
            message_id=MESSAGE_ID,
            message_kind="session_file",
            message_payload={"status": "ready"},
            error_message=None,
        )
    )

    executions = pool.connection_value.cursor_value.executions
    assert (LOCK_SESSION_SQL, (SESSION_ID, WORKSPACE_ID)) in executions
    assert all(query != INSERT_SESSION_SQL for query, _ in executions)
    assert pool.connection_value.committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["resource_id", "version_id", "file_id", "session_id", "message_id"],
)
async def test_repository_rejects_non_uuid_fact_ids_before_opening_a_transaction(
    field: str,
) -> None:
    pool = Pool()
    repository = UploadMetadataRepository(pool)
    request = UploadWriteRequest(
        workspace_id=WORKSPACE_ID,
        principal_type="user",
        principal_id=PRINCIPAL_ID,
        create_session=True,
        session_id=SESSION_ID,
        session_title="Review",
        resource_id=RESOURCE_ID,
        version_id=VERSION_ID,
        resource_title="Review",
        resource_content="Body",
        file_id=FILE_ID,
        file_name="review.md",
        content_type="text/markdown",
        stored=StoredFile(
            sha256="a" * 64,
            size_bytes=4,
            storage_key="aa/" + "a" * 64,
            created=True,
        ),
        message_id=MESSAGE_ID,
        message_kind="session_file",
        message_payload={"status": "ready"},
        error_message=None,
    )

    with pytest.raises(ValueError, match="UUID"):
        await repository.persist_upload(replace(request, **{field: "not-a-uuid"}))

    assert pool.connection_value.transaction_entries == 0
    assert pool.connection_value.cursor_value.executions == []


@pytest.mark.asyncio
async def test_parser_failure_facts_commit_without_a_partial_resource_graph() -> None:
    pool = Pool()
    repository = UploadMetadataRepository(pool)

    await repository.persist_upload(
        UploadWriteRequest(
            workspace_id=WORKSPACE_ID,
            principal_type="user",
            principal_id=PRINCIPAL_ID,
            create_session=True,
            session_id=SESSION_ID,
            session_title="review",
            resource_id=None,
            version_id=None,
            resource_title=None,
            resource_content=None,
            file_id=FILE_ID,
            file_name="review.pdf",
            content_type="application/pdf",
            stored=StoredFile(
                sha256="a" * 64,
                size_bytes=8,
                storage_key="aa/" + "a" * 64,
                created=True,
            ),
            message_id=MESSAGE_ID,
            message_kind="system",
            message_payload={"content": "文件导入失败", "level": "error"},
            error_message="文件导入失败",
        )
    )

    executions = pool.connection_value.cursor_value.executions
    assert all(
        query not in {INSERT_RESOURCE_SQL, INSERT_RESOURCE_VERSION_SQL} for query, _ in executions
    )
    assert (
        next(params for query, params in executions if query == INSERT_UPLOADED_FILE_SQL)[1] is None
    )
    message_params = next(params for query, params in executions if query == INSERT_MESSAGE_SQL)
    assert message_params[3] == "system"
    assert pool.connection_value.committed is True


@pytest.mark.asyncio
async def test_transaction_commit_failure_exposes_no_committed_fact_graph() -> None:
    pool = Pool(fail_commit=True)
    repository = UploadMetadataRepository(pool)

    with pytest.raises(RuntimeError, match="injected transaction commit failure"):
        await repository.persist_upload(
            UploadWriteRequest(
                workspace_id=WORKSPACE_ID,
                principal_type="user",
                principal_id=PRINCIPAL_ID,
                create_session=True,
                session_id=SESSION_ID,
                session_title="Review",
                resource_id=RESOURCE_ID,
                version_id=VERSION_ID,
                resource_title="Review",
                resource_content="Body",
                file_id=FILE_ID,
                file_name="review.md",
                content_type="text/markdown",
                stored=StoredFile(
                    sha256="a" * 64,
                    size_bytes=4,
                    storage_key="aa/" + "a" * 64,
                    created=True,
                ),
                message_id=MESSAGE_ID,
                message_kind="session_file",
                message_payload={"status": "ready"},
                error_message=None,
            )
        )

    assert pool.connection_value.transaction_entries == 1
    assert pool.connection_value.committed is False
    assert pool.connection_value.rolled_back is True
