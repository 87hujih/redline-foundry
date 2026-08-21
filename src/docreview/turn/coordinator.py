"""无状态的幂等 Turn 接受与事件 replay coordinator。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from docreview.turn.models import AcceptTurn, Turn, TurnEvent, TurnRequest, TurnResult


class TurnStore(Protocol):
    async def accept(self, value: AcceptTurn) -> tuple[Turn, bool]: ...

    async def list_events(self, turn_id: str, after_sequence: int) -> list[TurnEvent]: ...


Observer = Callable[[TurnEvent], Awaitable[None]]


def _canonical_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _uuid(value: str, name: str, *, optional: bool = False) -> str:
    normalized = value.strip()
    if optional and not normalized:
        return ""
    try:
        UUID(normalized)
    except ValueError as error:
        raise ValueError(f"{name}必须是 UUID") from error
    return normalized


def prepare(request: TurnRequest) -> AcceptTurn:
    request = replace(
        request,
        request_id=request.request_id.strip(),
        trace_id=request.trace_id.strip(),
        organization_id=request.organization_id.strip(),
        workspace_id=request.workspace_id.strip(),
        resource_id=request.resource_id.strip(),
        session_id=(request.session_id or "").strip() or None,
        principal_type=request.principal_type.strip().lower(),
        principal_id=request.principal_id.strip(),
        trust_source=request.trust_source.strip(),
        runtime_mode=request.runtime_mode.strip().lower(),
    )
    if not request.request_id or not request.message.strip():
        raise ValueError("request_id and message are required")
    if request.runtime_mode != "durable":
        raise ValueError("仅支持持久化运行时模式")
    if request.principal_type not in {"user", "service"}:
        raise ValueError("principal_type 无效")
    for name, value, optional in (
        ("organization_id", request.organization_id, True),
        ("workspace_id", request.workspace_id, False),
        ("resource_id", request.resource_id, False),
        ("session_id", request.session_id or "", True),
        ("principal_id", request.principal_id, False),
    ):
        _uuid(value, name, optional=optional)
    if not request.trust_source:
        raise ValueError("持久化 请求 需要 可信 范围")

    # 插入顺序固定，从而保持 input hash 一致。
    payload: dict[str, object] = {"message": request.message}
    for key, value in (
        ("organization_id", request.organization_id),
        ("workspace_id", request.workspace_id),
        ("resource_id", request.resource_id),
        ("session_id", request.session_id),
        ("principal_type", request.principal_type),
        ("principal_id", request.principal_id),
        ("trust_source", request.trust_source),
        ("runtime_mode", request.runtime_mode),
    ):
        if value:
            payload[key] = value
    encoded = _canonical_json(payload)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return AcceptTurn(
        request=request,
        idempotency_scope=f"workspace:{request.workspace_id}",
        input=payload,
        input_json=encoded,
        input_hash=f"sha256:{digest}",
    )


class TurnCoordinator:
    """协调持久化事实；不持有进程内 Run 状态。"""

    def __init__(self, store: TurnStore) -> None:
        self._store = store

    async def submit(self, request: TurnRequest) -> TurnResult:
        accepted = prepare(request)
        turn, created = await self._store.accept(accepted)
        events = await self._store.list_events(turn.id, 0)
        return TurnResult(turn=turn, created=created, events=tuple(events))

    async def stream(self, request: TurnRequest, after_sequence: int, observer: Observer) -> None:
        if after_sequence < 0:
            raise ValueError("after_sequence 必须为非负数")
        result = await self.submit(request)
        for event in result.events:
            if event.sequence > after_sequence:
                await observer(event)


__all__ = ["Observer", "TurnCoordinator", "TurnStore", "prepare"]
