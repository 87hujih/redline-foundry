from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from docreview.api.dependencies import AppDependencies, CompatibilityScope
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.identity.trusted_ingress import TrustedIngressAdapter

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PRINCIPAL_ID = "22222222-2222-4222-8222-222222222222"
ORGANIZATION_ID = "44444444-4444-4444-8444-444444444444"
SECRET = "s" * 32
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def signed_headers(path: str, request_id: str = "upload-request") -> dict[str, str]:
    issued_at = "2026-08-14T12:00:00Z"
    roles = "owner"
    canonical = "\n".join(
        (
            "v1",
            request_id,
            "POST",
            path,
            "user",
            PRINCIPAL_ID,
            ORGANIZATION_ID,
            WORKSPACE_ID,
            issued_at,
            roles,
        )
    )
    return {
        "X-Request-ID": request_id,
        "X-DocReview-Principal-Type": "user",
        "X-DocReview-Principal-ID": PRINCIPAL_ID,
        "X-DocReview-Organization-ID": ORGANIZATION_ID,
        "X-DocReview-Workspace-ID": WORKSPACE_ID,
        "X-DocReview-Identity-Issued-At": issued_at,
        "X-DocReview-Roles": roles,
        "X-DocReview-Identity-Signature": hmac.new(
            SECRET.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }


@dataclass
class Uploader:
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())
    error: Exception | None = None

    async def upload_conversation(
        self,
        workspace_id: str,
        file_name: str,
        content: bytes,
        *,
        principal_type: str,
        principal_id: str,
    ) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.calls.append(
            (
                "conversation",
                workspace_id,
                principal_type,
                principal_id,
                file_name,
                content,
            )
        )
        return {
            "session": {"id": SESSION_ID},
            "resource": None,
            "messages": [],
            "error_message": None,
        }

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
        if self.error is not None:
            raise self.error
        self.calls.append(
            (
                "session",
                workspace_id,
                principal_type,
                principal_id,
                session_id,
                file_name,
                content,
            )
        )
        return {
            "session": {"id": session_id},
            "resource": None,
            "messages": [],
            "error_message": None,
        }


def app(
    uploader: Uploader,
    extensions: list[str] | None = None,
    *,
    with_identity: bool = True,
):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            compatibility_scope=CompatibilityScope(WORKSPACE_ID),
            identity_adapter=(
                TrustedIngressAdapter(
                    secret=SECRET,
                    trust_source="edge",
                    max_age=timedelta(minutes=5),
                    now=lambda: NOW,
                )
                if with_identity
                else None
            ),
            assistant_uploader=uploader,
            upload_policy_extensions=extensions,
        ),
    )


@pytest.mark.anyio
async def test_both_upload_routes_return_frozen_dto_and_status() -> None:
    uploader = Uploader()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(uploader)), base_url="http://test"
    ) as client:
        conversation_path = "/api/assistant/conversations/files"
        conversation = await client.post(
            conversation_path,
            headers=signed_headers(conversation_path),
            files={"file": ("review.md", b"content")},
        )
        session_path = f"/api/assistant/sessions/{SESSION_ID}/files"
        session = await client.post(
            session_path,
            headers=signed_headers(session_path),
            files={"file": ("review.md", b"content")},
        )

    assert conversation.status_code == session.status_code == 200
    assert set(conversation.json()) == {"session", "resource", "messages", "error_message"}
    assert set(session.json()) == {"session", "resource", "messages", "error_message"}
    assert conversation.json()["messages"] == []
    assert session.json()["session"]["id"] == SESSION_ID
    assert uploader.calls[0] == (
        "conversation",
        WORKSPACE_ID,
        "user",
        PRINCIPAL_ID,
        "review.md",
        b"content",
    )
    assert uploader.calls[1] == (
        "session",
        WORKSPACE_ID,
        "user",
        PRINCIPAL_ID,
        SESSION_ID,
        "review.md",
        b"content",
    )


@pytest.mark.anyio
async def test_upload_rejects_invalid_session_missing_file_and_oversize() -> None:
    uploader = Uploader()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(uploader)), base_url="http://test"
    ) as client:
        invalid_path = "/api/assistant/sessions/bad/files"
        invalid = await client.post(
            invalid_path,
            headers=signed_headers(invalid_path),
            files={"file": ("review.md", b"content")},
        )
        conversation_path = "/api/assistant/conversations/files"
        missing = await client.post(
            conversation_path,
            headers=signed_headers(conversation_path),
            files={"other": ("review.md", b"content")},
        )
        oversize = await client.post(
            conversation_path,
            headers=signed_headers(conversation_path),
            files={"file": ("review.md", b"x" * (20 * 1024 * 1024 + 1))},
        )

    assert invalid.status_code == 400 and invalid.json() == {"error": "会话 ID 非法"}
    assert missing.status_code == 400 and missing.json() == {"error": "必须上传文件"}
    assert oversize.status_code == 413 and oversize.json() == {"error": "上传文件过大"}
    assert uploader.calls == []


@pytest.mark.anyio
async def test_upload_uses_configured_production_size_limit() -> None:
    uploader = Uploader()
    application = app(uploader)
    application.state.dependencies = replace(application.state.dependencies, upload_max_bytes=4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/assistant/conversations/files",
            files={"file": ("review.md", b"12345", "text/markdown")},
            headers=signed_headers("/api/assistant/conversations/files"),
        )

    assert response.status_code == 413
    assert uploader.calls == []


@pytest.mark.anyio
async def test_upload_rejects_empty_and_policy_disallowed_files_before_writer() -> None:
    uploader = Uploader()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(uploader, [".md", ".txt"])),
        base_url="http://test",
    ) as client:
        path = "/api/assistant/conversations/files"
        unsupported = await client.post(
            path,
            headers=signed_headers(path),
            files={"file": ("review.pdf", b"content")},
        )
        empty = await client.post(
            path,
            headers=signed_headers(path),
            files={"file": ("review.md", b"")},
        )

    assert unsupported.status_code == 400
    assert unsupported.json()["error"].startswith("不支持的文件格式")
    assert empty.status_code == 400
    assert empty.json() == {"error": "文件内容不能为空"}
    assert uploader.calls == []


@pytest.mark.anyio
async def test_upload_requires_trusted_ingress_before_parsing_or_writing() -> None:
    uploader = Uploader()
    path = "/api/assistant/conversations/files"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(uploader)), base_url="http://test"
    ) as client:
        missing = await client.post(path, files={"file": ("review.md", b"content")})
        invalid = await client.post(
            path,
            headers={**signed_headers(path), "X-DocReview-Workspace-ID": SESSION_ID},
            files={"file": ("review.md", b"content")},
        )

    assert missing.status_code == 401
    assert missing.json() == {"error": "durable identity is required"}
    assert invalid.status_code == 401
    assert invalid.json() == {"error": "durable identity is not trusted"}
    assert uploader.calls == []


@pytest.mark.anyio
async def test_upload_fails_closed_when_identity_adapter_is_missing() -> None:
    uploader = Uploader()
    path = "/api/assistant/conversations/files"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(uploader, with_identity=False)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            path,
            headers=signed_headers(path),
            files={"file": ("review.md", b"content")},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "durable identity adapter is not configured"}
    assert uploader.calls == []


@pytest.mark.anyio
async def test_session_upload_preserves_not_found_and_persistence_error_statuses() -> None:
    session_path = f"/api/assistant/sessions/{SESSION_ID}/files"
    missing_uploader = Uploader(error=LookupError("assistant session not found"))
    failed_uploader = Uploader(error=RuntimeError("database transaction failed"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(missing_uploader)), base_url="http://test"
    ) as client:
        missing = await client.post(
            session_path,
            headers=signed_headers(session_path),
            files={"file": ("review.md", b"content")},
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(failed_uploader)), base_url="http://test"
    ) as client:
        failed = await client.post(
            session_path,
            headers=signed_headers(session_path),
            files={"file": ("review.md", b"content")},
        )

    assert missing.status_code == 404
    assert missing.json() == {"error": "会话不存在"}
    assert failed.status_code == 500
    assert failed.json() == {"error": "上传文件失败"}
