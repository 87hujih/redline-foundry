"""Original uploaded file download endpoint."""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.storage.postgres.errors import FileContentNotFoundError

router = APIRouter(prefix="/api/files")
_MIME_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _uuid(value: str) -> str:
    value = value.strip()
    try:
        UUID(value)
    except ValueError as error:
        raise APIError(400, "文件 ID 非法") from error
    return value


@router.get("/{file_id}/download")
async def download_file(file_id: str, request: Request) -> StreamingResponse:
    dependencies: AppDependencies = request.app.state.dependencies
    if (
        dependencies.uploaded_files is None
        or dependencies.file_store is None
        or dependencies.compatibility_scope is None
    ):
        raise APIError(500, "文件下载服务未配置")
    file_id = _uuid(file_id)
    workspace_id = dependencies.compatibility_scope.workspace_id
    try:
        value = await dependencies.uploaded_files.get_by_id(workspace_id, file_id)
    except Exception as error:
        raise APIError(500, "查询文件失败") from error
    if value is None:
        raise APIError(404, "文件不存在")

    try:
        size = await dependencies.file_store.stat(value.storage_key)
    except Exception:
        size = None

    try:
        stream = await dependencies.file_store.open(value.storage_key)
    except FileContentNotFoundError as error:
        raise APIError(404, "文件内容不存在") from error
    except Exception as error:
        raise APIError(500, "读取文件失败") from error

    async def body() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk_result = stream.read(64 * 1024)
                chunk = await chunk_result if inspect.isawaitable(chunk_result) else chunk_result
                if not chunk:
                    break
                yield chunk
        finally:
            close_result = stream.close()
            if inspect.isawaitable(close_result):
                await close_result

    content_type = value.content_type.strip() or "application/octet-stream"
    filename = value.original_filename or "download"
    disposition = _attachment_disposition(filename)
    headers = {
        "Content-Disposition": disposition,
        "Content-Type": content_type,
    }
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(body(), media_type=content_type, headers=headers)


def _attachment_disposition(filename: str) -> str:
    if "\r" in filename or "\n" in filename:
        return "attachment"
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        return "attachment; filename*=utf-8''" + quote(filename, safe="!#$&+-.^_`|~")
    if _MIME_TOKEN.fullmatch(filename):
        return "attachment; filename=" + filename
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{escaped}"'


__all__ = ["router"]
