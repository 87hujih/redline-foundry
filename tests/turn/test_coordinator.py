from __future__ import annotations

from dataclasses import replace

import pytest

from docreview.runtime.errors import IdempotencyConflictError
from docreview.turn.coordinator import TurnCoordinator
from docreview.turn.models import AcceptTurn, Turn, TurnEvent, TurnRequest, TurnStatus


class Store:
    def __init__(self) -> None:
        self.accepted_hash = ""
        self.turn = Turn(
            id="turn-1",
            session_id="session-1",
            run_id="run-1",
            request_id="request-1",
            status=TurnStatus.ACCEPTED,
        )
        self.created_facts = 0
        self.inputs: list[AcceptTurn] = []
        self.events = [
            TurnEvent("event-1", "turn-1", 1, "turn.accepted", {"turn_id": "turn-1"}),
            TurnEvent("event-2", "turn-1", 2, "run.queued", {"run_id": "run-1"}),
        ]

    async def accept(self, value: AcceptTurn) -> tuple[Turn, bool]:
        self.inputs.append(value)
        digest = value.input_hash
        if self.accepted_hash:
            if digest != self.accepted_hash:
                raise IdempotencyConflictError("turn request idempotency conflict")
            return self.turn, False
        self.accepted_hash = digest
        self.created_facts += 1
        return self.turn, True

    async def list_events(self, turn_id: str, after_sequence: int) -> list[TurnEvent]:
        assert turn_id == "turn-1"
        return [event for event in self.events if event.sequence > after_sequence]


def request() -> TurnRequest:
    return TurnRequest(
        request_id=" request-1 ",
        trace_id=" request-1 ",
        organization_id="55555555-5555-4555-8555-555555555555",
        workspace_id="11111111-1111-4111-8111-111111111111",
        resource_id="22222222-2222-4222-8222-222222222222",
        session_id=None,
        message="review this",
        principal_type="USER",
        principal_id="44444444-4444-4444-8444-444444444444",
        trust_source="edge-hmac-v1",
    )


@pytest.mark.anyio
async def test_duplicate_request_reuses_one_persisted_turn_and_events() -> None:
    store = Store()
    coordinator = TurnCoordinator(store)

    first = await coordinator.submit(request())
    second = await coordinator.submit(request())

    assert first.created is True
    assert second.created is False
    assert first.turn.id == second.turn.id
    assert [event.sequence for event in second.events] == [1, 2]
    assert store.created_facts == 1
    accepted = store.inputs[0]
    assert accepted.input == {
        "message": "review this",
        "organization_id": "55555555-5555-4555-8555-555555555555",
        "workspace_id": "11111111-1111-4111-8111-111111111111",
        "resource_id": "22222222-2222-4222-8222-222222222222",
        "principal_type": "user",
        "principal_id": "44444444-4444-4444-8444-444444444444",
        "trust_source": "edge-hmac-v1",
        "runtime_mode": "durable",
    }
    assert accepted.input_hash.startswith("sha256:")


@pytest.mark.anyio
async def test_same_request_id_with_changed_resource_conflicts() -> None:
    coordinator = TurnCoordinator(Store())
    await coordinator.submit(request())

    with pytest.raises(IdempotencyConflictError):
        await coordinator.submit(
            replace(request(), resource_id="33333333-3333-4333-8333-333333333333")
        )


@pytest.mark.anyio
async def test_observer_failure_does_not_cancel_or_mutate_accepted_turn() -> None:
    store = Store()
    coordinator = TurnCoordinator(store)

    async def broken(_event: TurnEvent) -> None:
        raise ConnectionError("client disconnected")

    with pytest.raises(ConnectionError):
        await coordinator.stream(request(), 0, broken)

    replayed: list[int] = []

    async def observe(event: TurnEvent) -> None:
        replayed.append(event.sequence)

    await coordinator.stream(request(), 1, observe)
    assert replayed == [2]
    assert store.created_facts == 1


@pytest.mark.anyio
async def test_invalid_request_fails_before_store_access() -> None:
    store = Store()
    coordinator = TurnCoordinator(store)

    with pytest.raises(ValueError, match="request_id and message are required"):
        await coordinator.submit(replace(request(), request_id=" "))
    with pytest.raises(ValueError, match="after_sequence"):
        await coordinator.stream(request(), -1, lambda _event: None)  # type: ignore[arg-type]
    assert store.inputs == []
