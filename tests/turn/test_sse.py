from __future__ import annotations

import json

import pytest

from docreview.turn.models import TurnEvent
from docreview.turn.sse import SSEFrame, event_frames, render_frame


def event(sequence: int, type_: str, payload: dict[str, object]) -> TurnEvent:
    return TurnEvent(f"e{sequence}", "turn-1", sequence, type_, payload)


@pytest.mark.parametrize(
    ("type_", "public_type"),
    [
        ("turn.accepted", "turn_state"),
        ("turn.running", "turn_state"),
        ("run.queued", "turn_state"),
        ("assistant.message", "message_completed"),
    ],
)
def test_maps_persisted_events_to_frontend_compatible_frames(type_: str, public_type: str) -> None:
    frames = event_frames(event(3, type_, {"status": "running"}))
    assert len(frames) == 1
    assert frames[0].id == 3
    assert frames[0].event == public_type
    if type_ == "assistant.message":
        assert frames[0].data == {"message": {"status": "running"}}


@pytest.mark.parametrize("type_", ["turn.waiting_input", "turn.waiting_approval"])
def test_waiting_event_maps_to_state_and_done_with_same_sequence(type_: str) -> None:
    frames = event_frames(event(7, type_, {"status": type_.removeprefix("turn.")}))
    assert [(frame.id, frame.event) for frame in frames] == [(7, "turn_state"), (7, "done")]


@pytest.mark.parametrize("type_", ["turn.failed", "turn.cancelled"])
def test_failed_event_maps_to_stable_error_and_done_with_same_sequence(type_: str) -> None:
    frames = event_frames(event(9, type_, {"raw": "must not leak"}))
    assert [(frame.id, frame.event) for frame in frames] == [(9, "error"), (9, "done")]
    assert frames[0].data == {
        "code": "assistant_internal_error",
        "message": "持久化轮次结束，但没有可恢复的结果",  # noqa: RUF001
    }


def test_rendered_sse_has_id_event_single_json_object_and_blank_line() -> None:
    rendered = render_frame(SSEFrame(4, "message_completed", {"message": {"id": "m1"}}))
    assert rendered.startswith("id: 4\nevent: message_completed\ndata: ")
    assert rendered.endswith("\n\n")
    data = rendered.split("data: ", 1)[1].strip()
    assert json.loads(data) == {"message": {"id": "m1"}}


def test_invalid_persisted_payload_is_rendered_as_an_empty_object() -> None:
    persisted = TurnEvent("e1", "turn-1", 1, "turn.running", None)  # type: ignore[arg-type]

    frames = event_frames(persisted)

    assert frames == (SSEFrame(1, "turn_state", {}),)


@pytest.mark.parametrize("event_name", ["", "bad\nevent", "bad\revent"])
def test_rejects_invalid_sse_event_names(event_name: str) -> None:
    with pytest.raises(ValueError):
        render_frame(SSEFrame(1, event_name, {}))
