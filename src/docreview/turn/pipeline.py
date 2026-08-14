"""Single durable Turn pipeline shared by HTTP and SSE transports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from docreview.identity.trusted_ingress import WorkspaceScope
from docreview.turn.models import Turn, TurnEvent, TurnRequest, TurnResult, TurnStatus


class TurnNotReadyError(TimeoutError):
    """The accepted Turn has not reached a deterministic public projection."""


@dataclass(frozen=True, slots=True)
class PublicProjection:
    status: TurnStatus
    dto: dict[str, Any]
    last_event_sequence: int


@dataclass(frozen=True, slots=True)
class PipelineRequest:
    request_id: str
    trace_id: str
    session_id: str | None
    message: str
    workspace_id: str
    resource_id: str
    after_sequence: int
    scope: WorkspaceScope


@dataclass(frozen=True, slots=True)
class PipelineResult:
    mode: str
    dto: dict[str, Any]
    events: tuple[TurnEvent, ...]
    turn: Turn


Observer = Callable[[TurnEvent], Awaitable[None]]


class Coordinator(Protocol):
    async def submit(self, request: TurnRequest) -> TurnResult: ...

    async def stream(
        self, request: TurnRequest, after_sequence: int, observer: Observer
    ) -> None: ...


class ProjectionReader(Protocol):
    async def get_public_projection(
        self, workspace_id: str, turn_id: str
    ) -> PublicProjection | None: ...


class Runner(Protocol):
    async def execute(
        self, request: PipelineRequest, observer: Observer | None
    ) -> PipelineResult: ...


class DurableRunner:
    def __init__(
        self,
        coordinator: Coordinator,
        projections: ProjectionReader,
        poll_interval: float,
        max_wait: float,
    ) -> None:
        if poll_interval <= 0 or max_wait <= 0:
            raise ValueError("durable runner polling bounds must be positive")
        self._coordinator = coordinator
        self._projections = projections
        self._poll_interval = poll_interval
        self._max_wait = max_wait

    async def execute(self, request: PipelineRequest, observer: Observer | None) -> PipelineResult:
        self._validate_scope(request)
        turn_request = self._turn_request(request)
        accepted = await self._coordinator.submit(turn_request)
        indexed: dict[int, TurnEvent] = {
            event.sequence: event for event in accepted.events if event.sequence > 0
        }
        await _observe_after(indexed, request.after_sequence, observer)
        cursor = max(indexed, default=0)
        deadline = asyncio.get_running_loop().time() + self._max_wait

        while True:
            projection = await self._deterministic_projection(
                request.workspace_id, accepted.turn, indexed
            )
            if projection is not None:
                return PipelineResult(
                    mode="durable",
                    dto=projection.dto,
                    events=tuple(indexed[key] for key in sorted(indexed)),
                    turn=accepted.turn,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise TurnNotReadyError(
                    "durable turn state is not ready; retry with the same request id"
                )

            observed: list[TurnEvent] = []

            async def capture(
                event: TurnEvent, observed_events: list[TurnEvent] = observed
            ) -> None:
                observed_events.append(event)
                if event.sequence > request.after_sequence and observer is not None:
                    await observer(event)

            await self._coordinator.stream(turn_request, cursor, capture)
            for event in observed:
                indexed[event.sequence] = event
                cursor = max(cursor, event.sequence)
            if not observed:
                await asyncio.sleep(self._poll_interval)

    async def _deterministic_projection(
        self, workspace_id: str, turn: Turn, events: dict[int, TurnEvent]
    ) -> PublicProjection | None:
        status = _deterministic_status(events)
        if status is None:
            return None
        projection = await self._projections.get_public_projection(workspace_id, turn.id)
        if (
            projection is None
            or projection.status is not status
            or projection.last_event_sequence < max(events, default=0)
        ):
            return None
        return projection

    @staticmethod
    def _validate_scope(request: PipelineRequest) -> None:
        scope = request.scope
        if (
            not scope.trusted
            or not scope.trust_source.strip()
            or scope.workspace_id != request.workspace_id.strip()
            or not scope.principal.type.strip()
            or not scope.principal.id.strip()
        ):
            raise PermissionError("durable workspace scope is not trusted")

    @staticmethod
    def _turn_request(request: PipelineRequest) -> TurnRequest:
        return TurnRequest(
            request_id=request.request_id,
            trace_id=request.trace_id,
            organization_id=request.scope.principal.organization_id,
            workspace_id=request.workspace_id,
            resource_id=request.resource_id,
            session_id=request.session_id,
            message=request.message,
            principal_type=request.scope.principal.type,
            principal_id=request.scope.principal.id,
            trust_source=request.scope.trust_source,
        )


class DurableOnlyPipeline:
    """Production pipeline without a legacy router, shadow path, or fallback."""

    def __init__(self, durable: Runner) -> None:
        self._durable = durable

    async def execute(self, request: PipelineRequest, observer: Observer | None) -> PipelineResult:
        result = await self._durable.execute(request, observer)
        if result.mode != "durable":
            raise RuntimeError("durable-only runner returned an invalid mode")
        return result


async def _observe_after(
    events: dict[int, TurnEvent], after_sequence: int, observer: Observer | None
) -> None:
    if observer is None:
        return
    for sequence in sorted(events):
        if sequence > after_sequence:
            await observer(events[sequence])


def _deterministic_status(events: dict[int, TurnEvent]) -> TurnStatus | None:
    for sequence in sorted(events, reverse=True):
        event_type = events[sequence].type
        if not event_type.startswith("turn."):
            continue
        try:
            status = TurnStatus(event_type.removeprefix("turn."))
        except ValueError:
            continue
        if status.deterministic:
            return status
    return None


__all__ = [
    "DurableOnlyPipeline",
    "DurableRunner",
    "Observer",
    "PipelineRequest",
    "PipelineResult",
    "PublicProjection",
    "TurnNotReadyError",
]
