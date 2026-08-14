"""Durable-only assistant message write and SSE adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
)
from docreview.runtime.errors import IdempotencyConflictError
from docreview.turn.models import TurnEvent
from docreview.turn.pipeline import PipelineRequest, TurnNotReadyError
from docreview.turn.sse import INTERNAL_ERROR, SSEFrame, event_frames, render_frame

router = APIRouter(prefix="/api/assistant")


def _dependencies(request: Request) -> AppDependencies:
    return request.app.state.dependencies


def _uuid(value: str, message: str, *, optional: bool = False) -> str:
    value = value.strip()
    if optional and not value:
        return ""
    try:
        UUID(value)
    except ValueError as error:
        raise APIError(400, message) from error
    return value


async def _body(request: Request) -> tuple[str, str]:
    try:
        value = await request.json()
    except Exception as error:
        raise APIError(400, "消息内容不能为空") from error
    if not isinstance(value, dict):
        raise APIError(400, "消息内容不能为空")
    body = cast(dict[str, Any], value)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise APIError(400, "消息内容不能为空")
    resource = body.get("resource_id", "")
    if resource is not None and not isinstance(resource, str):
        raise APIError(400, "资源 ID 非法")
    resource_id = _uuid(str(resource or ""), "资源 ID 非法", optional=True)
    return message, resource_id


def _cursor(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as error:
        raise APIError(400, "Last-Event-ID 非法") from error
    if value < 0:
        raise APIError(400, "Last-Event-ID 非法")
    return value


async def _pipeline_request(request: Request, session_id: str | None) -> PipelineRequest:
    message, resource_id = await _body(request)
    if session_id is not None:
        session_id = _uuid(session_id, "会话 ID 非法")
    after_sequence = _cursor(request)
    dependencies = _dependencies(request)
    if dependencies.turn_pipeline is None or dependencies.identity_adapter is None:
        raise APIError(503, "durable agent runtime is unavailable")
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
        raise APIError(403, "durable workspace scope is not trusted")
    return PipelineRequest(
        request_id=str(request.state.request_id),
        trace_id=str(request.state.request_id),
        session_id=session_id,
        message=message,
        workspace_id=workspace_id,
        resource_id=resource_id,
        after_sequence=after_sequence,
        scope=scope,
    )


def _pipeline_error(error: Exception) -> APIError:
    if isinstance(error, TurnNotReadyError):
        return APIError(503, "durable turn state is not ready; retry with the same request id")
    if isinstance(error, PermissionError):
        return APIError(403, "durable workspace scope is not trusted")
    # Frozen Go compatibility currently maps request hash conflicts and missing
    # durable resource scope through the generic failure response.
    if isinstance(error, (IdempotencyConflictError, ValueError)):
        return APIError(500, "处理助手请求失败")
    return APIError(500, "处理助手请求失败")


async def _non_stream(request: Request, session_id: str | None, status_code: int) -> JSONResponse:
    pipeline_request = await _pipeline_request(request, session_id)
    pipeline = _dependencies(request).turn_pipeline
    if pipeline is None:
        raise APIError(503, "durable agent runtime is unavailable")
    try:
        result = await pipeline.execute(pipeline_request, None)
    except Exception as error:
        raise _pipeline_error(error) from error
    return JSONResponse(status_code=status_code, content=result.dto)


async def _stream(request: Request, session_id: str | None) -> StreamingResponse:
    pipeline_request = await _pipeline_request(request, session_id)
    pipeline = _dependencies(request).turn_pipeline
    if pipeline is None:
        raise APIError(503, "durable agent runtime is unavailable")

    async def body() -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        terminal = False

        async def observe(event: TurnEvent) -> None:
            nonlocal terminal
            for frame in event_frames(event):
                terminal = terminal or frame.event in {"done", "error"}
                await queue.put(render_frame(frame))

        task = asyncio.create_task(pipeline.execute(pipeline_request, observe))
        try:
            while not task.done() or not queue.empty():
                if queue.empty():
                    await asyncio.wait({task}, timeout=0.01)
                    continue
                yield await queue.get()
            # Never let cancellation of the SSE observer propagate into the
            # durable pipeline task that owns persisted Run progress.
            result = await asyncio.shield(task)
            if not terminal:
                yield render_frame(SSEFrame(pipeline_request.after_sequence, "done", {}))
            del result
        except asyncio.CancelledError:
            # Acceptance and Runtime execution are durable database facts. A
            # transport cancellation only detaches this observer; a reconnect
            # with the same request id can replay the persisted outcome.
            task.add_done_callback(_consume_background_result)
            raise
        except Exception:
            yield render_frame(
                SSEFrame(pipeline_request.after_sequence, "error", dict(INTERNAL_ERROR))
            )

    return StreamingResponse(
        body(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _consume_background_result(task: asyncio.Task[object]) -> None:
    """Retrieve a detached pipeline exception so it is not reported as unhandled."""

    if not task.cancelled():
        task.exception()


@router.post("/conversations", status_code=201)
async def create_conversation(request: Request) -> JSONResponse:
    return await _non_stream(request, None, 201)


@router.post("/conversations/stream")
async def create_conversation_stream(request: Request) -> StreamingResponse:
    return await _stream(request, None)


@router.post("/sessions/{session_id}/messages")
async def append_message(session_id: str, request: Request) -> JSONResponse:
    return await _non_stream(request, session_id, 200)


@router.post("/sessions/{session_id}/messages/stream")
async def append_message_stream(session_id: str, request: Request) -> StreamingResponse:
    return await _stream(request, session_id)


__all__ = ["router"]
