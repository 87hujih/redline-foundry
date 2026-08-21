"""在线 Assistant 文档上传适配器。"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.document.parser import UnsupportedFileTypeError
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
    WorkspaceScope,
)

router = APIRouter(prefix="/api/assistant")


async def _content(
    request: Request,
    max_bytes: int = 20 * 1024 * 1024,
    extensions: list[str] | None = None,
) -> tuple[str, bytes]:
    raw = await request.body()
    if len(raw) > max_bytes + 1024 * 1024:
        raise APIError(413, "上传文件过大")
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise APIError(400, "必须上传文件")
    envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    part = next(
        (
            candidate
            for candidate in message.iter_parts()
            if candidate.get_filename()
            and candidate.get_param("name", header="content-disposition") == "file"
        ),
        None,
    )
    if part is None:
        raise APIError(400, "必须上传文件")
    filename = part.get_filename() or ""
    payload = part.get_payload(decode=True)
    content = payload if isinstance(payload, bytes) else b""
    if len(content) > max_bytes:
        raise APIError(413, "上传文件过大")
    if not filename.strip():
        raise APIError(400, "文件名不能为空")
    if extensions is not None:
        allowed = {value.strip().lower() for value in extensions if value.strip()}
        suffix = Path(filename).suffix.lower()
        if suffix not in allowed:
            supported = ",".join(value for value in extensions if value.strip())
            raise APIError(
                400,
                f"不支持的文件格式：{suffix or '(无扩展名)'}。当前支持：{supported}。",  # noqa: RUF001
            )
    if not content:
        raise APIError(400, "文件内容不能为空")
    return filename, content


def _uploader(request: Request):
    dependencies: AppDependencies = request.app.state.dependencies
    if dependencies.assistant_uploader is None:
        raise APIError(503, "上传服务未配置")
    return dependencies.assistant_uploader


def _trusted_scope(request: Request) -> WorkspaceScope:
    dependencies: AppDependencies = request.app.state.dependencies
    if dependencies.identity_adapter is None:
        raise APIError(503, "durable identity adapter is not configured")
    if not request.headers.get(HEADER_SIGNATURE, "").strip():
        raise APIError(401, "durable identity is required")
    workspace_id = request.headers.get(HEADER_WORKSPACE_ID, "").strip()
    try:
        scope = dependencies.identity_adapter.authenticate(
            IdentityRequest(
                method=request.method,
                path=request.url.path,
                request_id=str(request.state.request_id),
                headers=request.headers,
            ),
            workspace_id,
        )
    except UntrustedIdentityError as error:
        raise APIError(401, "durable identity is not trusted") from error
    if not scope.trusted or scope.workspace_id != workspace_id:
        raise APIError(401, "durable identity is not trusted")
    return scope


@router.post("/conversations/files", status_code=200)
async def upload_conversation_file(
    request: Request,
) -> dict[str, object]:
    scope = _trusted_scope(request)
    dependencies: AppDependencies = request.app.state.dependencies
    filename, content = await _content(
        request,
        max_bytes=dependencies.upload_max_bytes or 20 * 1024 * 1024,
        extensions=dependencies.upload_policy_extensions,
    )
    try:
        return await _uploader(request).upload_conversation(
            scope.workspace_id,
            filename,
            content,
            principal_type=scope.principal.type,
            principal_id=scope.principal.id,
        )
    except UnsupportedFileTypeError as error:
        raise APIError(400, str(error)) from error
    except APIError:
        raise
    except Exception as error:
        raise APIError(500, "上传文件失败") from error


@router.post("/sessions/{session_id}/files", status_code=200)
async def upload_session_file(session_id: str, request: Request) -> dict[str, object]:
    scope = _trusted_scope(request)
    dependencies: AppDependencies = request.app.state.dependencies
    try:
        UUID(session_id.strip())
    except ValueError as error:
        raise APIError(400, "会话 ID 非法") from error
    filename, content = await _content(
        request,
        max_bytes=dependencies.upload_max_bytes or 20 * 1024 * 1024,
        extensions=dependencies.upload_policy_extensions,
    )
    try:
        return await _uploader(request).upload_session(
            scope.workspace_id,
            session_id,
            filename,
            content,
            principal_type=scope.principal.type,
            principal_id=scope.principal.id,
        )
    except UnsupportedFileTypeError as error:
        raise APIError(400, str(error)) from error
    except LookupError as error:
        raise APIError(404, "会话不存在") from error
    except APIError:
        raise
    except Exception as error:
        raise APIError(500, "上传文件失败") from error


__all__ = ["router"]
