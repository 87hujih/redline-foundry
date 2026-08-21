"""Assistant 会话兼容查询路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.api.trusted_scope import trusted_workspace_scope
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
)
from docreview.storage.postgres.errors import RecordNotFoundError, SessionNotFoundError

router = APIRouter(prefix="/api/assistant")


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid(value: str) -> str:
    value = value.strip()
    try:
        UUID(value)
    except ValueError as error:
        raise APIError(400, "会话 ID 非法") from error
    return value


def _deps(request: Request) -> AppDependencies:
    return request.app.state.dependencies


def _session(value: Any) -> dict[str, object]:
    return {
        "id": value.id,
        "title": value.title,
        "web_search_enabled": value.web_search_enabled,
        "last_message_at": _time(value.last_message_at),
        "created_at": _time(value.created_at),
        "updated_at": _time(value.updated_at),
    }


def _message(value: Any) -> dict[str, object]:
    return {
        "id": value.id,
        "role": value.role,
        "kind": value.kind,
        "payload": value.payload,
        "sequence_no": value.sequence_no,
        "created_at": _time(value.created_at),
    }


def _trusted_workspace(request: Request, dependencies: AppDependencies) -> str:
    if not request.headers.get(HEADER_SIGNATURE, "").strip():
        raise APIError(401, "持久化 身份 为必填项")
    if dependencies.identity_adapter is None:
        raise APIError(503, "会话资源选择不可用")
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
        raise APIError(401, "持久化 身份 不可信") from error
    if not scope.trusted or scope.workspace_id != workspace_id:
        raise APIError(403, "持久化 工作区 范围 不可信")
    return workspace_id


async def _selection_body(request: Request) -> str:
    try:
        value = await request.json()
    except Exception as error:
        raise APIError(400, "资源 ID 非法") from error
    if not isinstance(value, dict):
        raise APIError(400, "资源 ID 非法")
    body = cast(dict[str, Any], value)
    resource_id = body.get("resource_id")
    if not isinstance(resource_id, str):
        raise APIError(400, "资源 ID 非法")
    resource_id = resource_id.strip()
    try:
        parsed = UUID(resource_id)
    except ValueError as error:
        raise APIError(400, "资源 ID 非法") from error
    return str(parsed)


@router.get("/sessions")
async def list_sessions(request: Request) -> dict[str, object]:
    dependencies = _deps(request)
    workspace_id = trusted_workspace_scope(request, dependencies).workspace_id
    assistant = dependencies.assistant
    if assistant is None:
        raise APIError(500, "查询会话列表失败")
    try:
        sessions = await assistant.list_sessions(workspace_id)
    except Exception as error:
        raise APIError(500, "查询会话列表失败") from error
    return {"sessions": [_session(value) for value in sessions]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, object]:
    dependencies = _deps(request)
    workspace_id = trusted_workspace_scope(request, dependencies).workspace_id
    session_id = _uuid(session_id)
    assistant = dependencies.assistant
    if assistant is None:
        raise APIError(500, "查询会话失败")
    try:
        result = await assistant.get_conversation(workspace_id, session_id)
    except SessionNotFoundError as error:
        raise APIError(404, "会话不存在") from error
    except Exception as error:
        raise APIError(500, "查询会话失败") from error
    if result is None:
        raise APIError(404, "会话不存在")
    session, messages = result
    return {"session": _session(session), "messages": [_message(value) for value in messages]}


@router.get("/sessions/{session_id}/resource-selection")
async def get_resource_selection(session_id: str, request: Request) -> dict[str, object]:
    session_id = _uuid(session_id)
    dependencies = _deps(request)
    workspace_id = _trusted_workspace(request, dependencies)
    repository = dependencies.assistant_resource_selection
    if repository is None:
        raise APIError(503, "会话资源选择不可用")
    try:
        resource_id = await repository.get_resource_selection(workspace_id, session_id)
    except SessionNotFoundError as error:
        raise APIError(404, "会话不存在") from error
    except Exception as error:
        raise APIError(500, "查询会话资源选择失败") from error
    return {"resource_id": resource_id}


@router.put("/sessions/{session_id}/resource-selection")
async def set_resource_selection(session_id: str, request: Request) -> dict[str, object]:
    session_id = _uuid(session_id)
    dependencies = _deps(request)
    workspace_id = _trusted_workspace(request, dependencies)
    resource_id = await _selection_body(request)
    repository = dependencies.assistant_resource_selection
    if repository is None:
        raise APIError(503, "会话资源选择不可用")
    try:
        selected = await repository.set_resource_selection(workspace_id, session_id, resource_id)
    except SessionNotFoundError as error:
        raise APIError(404, "会话不存在") from error
    except RecordNotFoundError as error:
        raise APIError(404, "资源不存在") from error
    except Exception as error:
        raise APIError(500, "更新会话资源选择失败") from error
    return {"resource_id": selected}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> Response:
    dependencies = _deps(request)
    workspace_id = trusted_workspace_scope(request, dependencies).workspace_id
    session_id = _uuid(session_id)
    repository = dependencies.assistant_writer
    if repository is None:
        raise APIError(500, "删除会话失败")
    try:
        deleted = await repository.delete_session(workspace_id, session_id)
    except Exception as error:
        raise APIError(500, "删除会话失败") from error
    if not deleted:
        raise APIError(404, "会话不存在")
    return Response(status_code=204)


__all__ = ["router"]
