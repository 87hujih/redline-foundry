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
    assert "'durable'" not in facts  # Runtime mode 是绑定值，不是 fallback 默认值。
    assert "insert into agent_steps" in facts
    assert "'understand_goal:1', 'understandgoal'" in facts
    assert "insert into agent_turn_events" in facts
    assert "'turn.accepted'" in facts and "'run.queued'" in facts
    assert "select %s::uuid, 1, 'turn.accepted'" in facts
    assert "select %s::uuid, 2, 'run.queued'" in facts
    assert (
        "jsonb_build_object('turn_id', %s::text, 'resource_id', %s::text, "
        "'runtime_mode', %s::text)" in facts
    )
    assert "'run_id', inserted_run.id::text" in facts
    assert "'request_fact_id', %s::text" in facts
    assert "'current_node', 'understandgoal'" in facts
    assert "'fact_id', 'budget:' || inserted_run.id::text || ':0'" in facts
    assert "'steps_remaining', 64" in facts
    assert "'tool_calls_remaining', 32" in facts
    assert "'message'" not in facts.partition("inserted_step as")[2].partition(")")[0]
    assert "jsonb_build_object('turn_id', %s::text)" in facts
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
