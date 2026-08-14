"""Mapping from persisted Turn events to the frozen public SSE protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from docreview.turn.models import TurnEvent

INTERNAL_ERROR = {
    "code": "assistant_internal_error",
    "message": "助手暂时不可用，请使用相同 request_id 重试。",  # noqa: RUF001
}
PERSISTED_TERMINAL_ERROR = {
    "code": "assistant_internal_error",
    "message": "持久化轮次结束，但没有可恢复的结果",  # noqa: RUF001
}


@dataclass(frozen=True, slots=True)
class SSEFrame:
    id: int
    event: str
    data: dict[str, Any]


def event_frames(event: TurnEvent) -> tuple[SSEFrame, ...]:
    payload = _payload(cast(object, event.payload))
    if event.type in {"turn.accepted", "turn.running", "run.queued"}:
        return (SSEFrame(event.sequence, "turn_state", payload),)
    if event.type == "assistant.message":
        message = payload.get("message")
        data = payload if isinstance(message, dict) else {"message": payload}
        return (SSEFrame(event.sequence, "message_completed", data),)
    if event.type in {"turn.waiting_input", "turn.waiting_approval"}:
        return (
            SSEFrame(event.sequence, "turn_state", payload),
            SSEFrame(event.sequence, "done", {}),
        )
    if event.type == "turn.succeeded":
        return (SSEFrame(event.sequence, "done", {}),)
    if event.type in {"turn.failed", "turn.cancelled"}:
        return (
            SSEFrame(event.sequence, "error", dict(PERSISTED_TERMINAL_ERROR)),
            SSEFrame(event.sequence, "done", {}),
        )
    if event.type == "done":
        return (SSEFrame(event.sequence, "done", {}),)
    return (SSEFrame(event.sequence, event.type, payload),)


def render_frame(frame: SSEFrame) -> str:
    event_name = frame.event.strip()
    if not event_name or "\r" in event_name or "\n" in event_name or frame.id < 0:
        raise ValueError("SSE event name or id is invalid")
    payload = _payload(cast(object, frame.data))
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {frame.id}\nevent: {event_name}\ndata: {encoded}\n\n"


def _payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, Any], value)


__all__ = [
    "INTERNAL_ERROR",
    "PERSISTED_TERMINAL_ERROR",
    "SSEFrame",
    "event_frames",
    "render_frame",
]
