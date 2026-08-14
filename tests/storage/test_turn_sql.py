from __future__ import annotations

import re

from docreview.storage.postgres.turn import (
    ACCEPT_FACTS_SQL,
    CREATE_TURN_SQL,
    GET_PUBLIC_PROJECTION_SQL,
    LIST_TURN_EVENTS_SQL,
    SELECT_TURN_SQL,
)


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def test_turn_acceptance_is_one_transactional_fact_closure() -> None:
    create = compact(CREATE_TURN_SQL)
    facts = compact(ACCEPT_FACTS_SQL)

    assert "on conflict (idempotency_scope, request_id) do nothing" in create
    assert "insert into assistant_messages" in facts
    assert "insert into agent_runs" in facts
    assert "'durable'" not in facts  # runtime mode is a bound value, not a fallback default.
    assert "insert into agent_steps" in facts
    assert "'understand_goal:1', 'understandgoal'" in facts
    assert "insert into agent_turn_events" in facts
    assert "'turn.accepted'" in facts and "'run.queued'" in facts
    assert "insert into outbox_events" in facts
    assert "'agent.turn.accepted'" in facts


def test_turn_replay_is_scoped_hashed_and_strictly_sequenced() -> None:
    select = compact(SELECT_TURN_SQL)
    events = compact(LIST_TURN_EVENTS_SQL)
    projection = compact(GET_PUBLIC_PROJECTION_SQL)

    assert "where turn.idempotency_scope = %s and turn.request_id = %s" in select
    assert "turn.input_hash" in select
    assert "event.sequence_no > %s" in events
    assert "order by event.sequence_no asc, event.id asc" in events
    assert "projection.workspace_id = %s" in projection
    assert "projection.turn_id = %s" in projection
