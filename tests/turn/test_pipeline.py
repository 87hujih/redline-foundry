from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from docreview.identity.trusted_ingress import Principal, WorkspaceScope
from docreview.turn.models import Turn, TurnEvent, TurnRequest, TurnResult, TurnStatus
from docreview.turn.pipeline import (
    DurableOnlyPipeline,
    DurableRunner,
    PipelineRequest,
    PublicProjection,
    TurnNotReadyError,
)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE_ID = "22222222-2222-4222-8222-222222222222"


def scope() -> WorkspaceScope:
    from datetime import UTC, datetime

    return WorkspaceScope(
        principal=Principal(
            "user",
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555",
            ("owner",),
        ),
        workspace_id=WORKSPACE_ID,
        trust_source="edge-hmac-v1",
        trusted=True,
        issued_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


@dataclass
class Coordinator:
    events: list[TurnEvent]
    calls: int = 0
    cursors: list[int] = field(default_factory=lambda: list[int]())

    async def submit(self, request: TurnRequest) -> TurnResult:
        self.calls += 1
        return TurnResult(
            Turn("turn-1", "session-1", "run-1", request.request_id, TurnStatus.ACCEPTED),
            self.calls == 1,
            tuple(self.events[:2]),
        )

    async def stream(self, request: TurnRequest, after_sequence: int, observer: object) -> None:
        self.cursors.append(after_sequence)
        for event in self.events:
            if event.sequence > after_sequence:
                await observer(event)  # type: ignore[operator]


@dataclass
class ProjectionReader:
    value: PublicProjection | None
    calls: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())

    async def get_public_projection(
        self, workspace_id: str, turn_id: str
    ) -> PublicProjection | None:
        self.calls.append((workspace_id, turn_id))
        return self.value


def pipeline_request(after: int = 0) -> PipelineRequest:
    return PipelineRequest(
        request_id="request-1",
        trace_id="request-1",
        session_id=None,
        message="review",
        workspace_id=WORKSPACE_ID,
        resource_id=RESOURCE_ID,
        after_sequence=after,
        scope=scope(),
    )


def persisted_events() -> list[TurnEvent]:
    return [
        TurnEvent("e1", "turn-1", 1, "turn.accepted", {"status": "accepted"}),
        TurnEvent("e2", "turn-1", 2, "run.queued", {"status": "running"}),
        TurnEvent(
            "e3",
            "turn-1",
            3,
            "assistant.message",
            {
                "id": "message-1",
                "role": "assistant",
                "kind": "text",
                "payload": {"content": "done"},
                "sequence_no": 2,
            },
        ),
        TurnEvent("e4", "turn-1", 4, "turn.succeeded", {"status": "succeeded"}),
    ]


@pytest.mark.anyio
async def test_non_stream_and_stream_use_the_same_durable_only_pipeline() -> None:
    coordinator = Coordinator(persisted_events())
    projection = PublicProjection(
        TurnStatus.SUCCEEDED,
        {"session": {"id": "session-1"}, "messages": []},
        4,
    )
    runner = DurableRunner(
        coordinator, ProjectionReader(projection), poll_interval=0.001, max_wait=0.1
    )
    pipeline = DurableOnlyPipeline(runner)

    direct = await pipeline.execute(pipeline_request(), None)
    observed: list[int] = []

    async def observe(event: TurnEvent) -> None:
        observed.append(event.sequence)

    streamed = await pipeline.execute(pipeline_request(after=2), observe)

    assert direct.dto == streamed.dto
    assert observed == [3, 4]
    assert direct.mode == streamed.mode == "durable"
    assert coordinator.calls == 2


@pytest.mark.anyio
async def test_runner_waits_for_persisted_deterministic_projection() -> None:
    coordinator = Coordinator(persisted_events())
    reader = ProjectionReader(None)
    runner = DurableRunner(coordinator, reader, poll_interval=0.001, max_wait=0.01)

    with pytest.raises(TurnNotReadyError):
        await runner.execute(pipeline_request(), None)
    assert reader.calls


@pytest.mark.anyio
async def test_observer_disconnect_propagates_without_runtime_cancel() -> None:
    coordinator = Coordinator(persisted_events())
    projection = PublicProjection(TurnStatus.SUCCEEDED, {"messages": []}, 4)
    runner = DurableRunner(coordinator, ProjectionReader(projection), 0.001, 0.1)

    async def disconnect(_event: TurnEvent) -> None:
        raise ConnectionError("disconnected")

    with pytest.raises(ConnectionError):
        await runner.execute(pipeline_request(), disconnect)

    assert not hasattr(coordinator, "cancel")
    await asyncio.sleep(0)
