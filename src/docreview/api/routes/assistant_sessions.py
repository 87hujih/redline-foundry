"""Assistant session compatibility query routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.storage.postgres.errors import SessionNotFoundError

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


def _workspace(dependencies: AppDependencies, error_message: str) -> str:
    if dependencies.compatibility_scope is None:
        raise APIError(500, error_message)
    return dependencies.compatibility_scope.workspace_id


@router.get("/sessions")
async def list_sessions(request: Request) -> dict[str, object]:
    dependencies = _deps(request)
    workspace_id = _workspace(dependencies, "查询会话列表失败")
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
    session_id = _uuid(session_id)
    dependencies = _deps(request)
    workspace_id = _workspace(dependencies, "查询会话失败")
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


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> Response:
    session_id = _uuid(session_id)
    dependencies = _deps(request)
    workspace_id = _workspace(dependencies, "删除会话失败")
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
