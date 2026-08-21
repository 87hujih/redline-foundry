from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest

from docreview.api.dependencies import AppDependencies
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.storage.models import UploadedFile
from docreview.storage.postgres.errors import FileContentNotFoundError
from tests.trusted_identity import identity_adapter, signed_headers

WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_WORKSPACE_ID = "44444444-4444-4444-8444-444444444444"
FILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@dataclass
class FakeFiles:
    value: UploadedFile | None = None
    error: Exception | None = None
    calls: list[tuple[object, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def get_by_id(self, workspace_id: str, file_id: str) -> UploadedFile | None:
        self.calls.append((workspace_id, file_id))
        if self.error:
            raise self.error
        return self.value


@dataclass
class FakeStore:
    content: bytes = b"original bytes"
    missing: bool = False
    unknown_size: bool = False
    opened: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.opened = []

    async def stat(self, storage_key: str) -> int | None:
        return None if self.unknown_size else len(self.content)

    async def open(self, storage_key: str) -> BytesIO:
        self.opened.append(storage_key)
        if self.missing:
            raise FileContentNotFoundError
        return BytesIO(self.content)


def app(files: FakeFiles | None, store: FakeStore | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            identity_adapter=identity_adapter(),
            uploaded_files=files,
            file_store=store,
        ),
    )


def uploaded(content_type: str = "text/plain") -> UploadedFile:
    return UploadedFile(
        id=FILE_ID,
        resource_id=RESOURCE_ID,
        session_id=None,
        original_filename="notes.txt",
        content_type=content_type,
        size_bytes=15,
        sha256="hash",
        storage_key="workspace/file-1",
        created_at=NOW,
    )


@pytest.mark.anyio
async def test_download_streams_original_bytes_and_binds_workspace() -> None:
    files = FakeFiles(value=uploaded())
    store = FakeStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, store)), base_url="http://test"
    ) as client:
        path = f"/api/files/{FILE_ID}/download"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 200
    assert response.content == b"original bytes"
    assert response.headers["content-type"] == "text/plain"
    assert response.headers["content-disposition"] == "attachment; filename=notes.txt"
    assert files.calls == [(WORKSPACE_ID, FILE_ID)]
    assert store.opened == ["workspace/file-1"]


@pytest.mark.anyio
async def test_download_falls_back_to_octet_stream_and_unknown_size() -> None:
    files = FakeFiles(value=uploaded(content_type=""))
    store = FakeStore(unknown_size=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, store)), base_url="http://test"
    ) as client:
        path = f"/api/files/{FILE_ID}/download"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == b"original bytes"


@pytest.mark.anyio
async def test_download_ignores_stat_failure_like_go() -> None:
    class StatFailureStore(FakeStore):
        async def stat(self, storage_key: str) -> int | None:
            raise OSError("stat unavailable")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(FakeFiles(value=uploaded()), StatFailureStore())),
        base_url="http://test",
    ) as client:
        path = f"/api/files/{FILE_ID}/download"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 200
    assert response.content == b"original bytes"


@pytest.mark.anyio
async def test_download_invalid_missing_and_content_missing_errors() -> None:
    files = FakeFiles(value=None)
    store = FakeStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, store)), base_url="http://test"
    ) as client:
        invalid_path = "/api/files/not-a-uuid/download"
        path = f"/api/files/{FILE_ID}/download"
        invalid = await client.get(
            invalid_path, headers=signed_headers("GET", invalid_path, WORKSPACE_ID)
        )
        headers = signed_headers("GET", path, WORKSPACE_ID)
        missing = await client.get(path, headers=headers)
        files.value = uploaded()
        store.missing = True
        missing_content = await client.get(path, headers=headers)

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "文件 ID 非法"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "文件不存在"}
    assert missing_content.status_code == 404
    assert missing_content.json() == {"error": "文件内容不存在"}


@pytest.mark.anyio
async def test_download_storage_failures_map_to_frozen_errors() -> None:
    files = FakeFiles(error=RuntimeError("db down"))
    store = FakeStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, store)), base_url="http://test"
    ) as client:
        path = f"/api/files/{FILE_ID}/download"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 500
    assert response.json() == {"error": "查询文件失败"}


@pytest.mark.anyio
async def test_download_formats_non_ascii_attachment_filename_safely() -> None:
    value = uploaded()
    files = FakeFiles(
        value=UploadedFile(
            id=value.id,
            resource_id=value.resource_id,
            session_id=value.session_id,
            original_filename='审阅 "终稿".txt',
            content_type=value.content_type,
            size_bytes=value.size_bytes,
            sha256=value.sha256,
            storage_key=value.storage_key,
            created_at=value.created_at,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, FakeStore())), base_url="http://test"
    ) as client:
        path = f"/api/files/{FILE_ID}/download"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=utf-8''" in disposition.lower()
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.anyio
async def test_download_requires_trusted_identity_before_file_lookup() -> None:
    files = FakeFiles(value=uploaded())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, FakeStore())), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/files/{FILE_ID}/download")

    assert response.status_code == 401
    assert response.json() == {"error": "durable identity is required"}
    assert files.calls == []


@pytest.mark.anyio
async def test_download_does_not_reveal_another_workspace_file() -> None:
    class WorkspaceFiles(FakeFiles):
        async def get_by_id(self, workspace_id: str, file_id: str) -> UploadedFile | None:
            self.calls.append((workspace_id, file_id))
            return self.value if workspace_id == WORKSPACE_ID else None

    files = WorkspaceFiles(value=uploaded())
    store = FakeStore()
    path = f"/api/files/{FILE_ID}/download"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(files, store)), base_url="http://test"
    ) as client:
        response = await client.get(
            path, headers=signed_headers("GET", path, OTHER_WORKSPACE_ID)
        )

    assert response.status_code == 404
    assert response.json() == {"error": "文件不存在"}
    assert files.calls == [(OTHER_WORKSPACE_ID, FILE_ID)]
    assert store.opened == []
