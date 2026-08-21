"""持久化 Assistant Turn 的 PostgreSQL 事实闭环。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, cast

from docreview.runtime.errors import IdempotencyConflictError
from docreview.turn.models import AcceptTurn, Turn, TurnEvent, TurnStatus
from docreview.turn.pipeline import PublicProjection

CREATE_TURN_SQL = """
INSERT INTO agent_turns (
    organization_id, workspace_id, resource_id, session_id, idempotency_scope,
    request_id, trace_id, principal_type, principal_id, trust_source, runtime_mode,
    input_json, input_hash
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (idempotency_scope, request_id) DO NOTHING
RETURNING id::text, session_id::text, request_id, status, created_at, updated_at
"""

ACCEPT_FACTS_SQL = """
WITH locked_session AS MATERIALIZED (
    SELECT id FROM assistant_sessions WHERE id = %s AND workspace_id = %s FOR UPDATE
),
created_session AS (
    INSERT INTO assistant_sessions (workspace_id, title)
    SELECT %s, %s WHERE %s::uuid IS NULL RETURNING id
),
selected_session AS MATERIALIZED (
    SELECT id FROM locked_session UNION ALL SELECT id FROM created_session
),
locked_resource AS MATERIALIZED (
    SELECT id FROM resources
    WHERE id = %s AND workspace_id = %s
    FOR KEY SHARE
),
linked_turn AS (
    UPDATE agent_turns SET session_id = selected_session.id, updated_at = %s
    FROM selected_session, locked_resource WHERE agent_turns.id = %s
    RETURNING agent_turns.id, selected_session.id AS session_id
),
inserted_message AS (
    INSERT INTO assistant_messages (
        session_id, turn_id, role, kind, sequence_no, payload, created_at
    )
    SELECT linked_turn.session_id, linked_turn.id, 'user', 'text',
           COALESCE((SELECT MAX(sequence_no) FROM assistant_messages
                     WHERE session_id = linked_turn.session_id), 0) + 1,
           jsonb_build_object('content', %s::jsonb ->> 'message'), %s
    FROM linked_turn RETURNING session_id
),
updated_existing_session AS (
    UPDATE assistant_sessions SET last_message_at = %s, updated_at = %s
    FROM inserted_message, locked_session
    WHERE assistant_sessions.id = inserted_message.session_id
      AND assistant_sessions.id = locked_session.id
    RETURNING assistant_sessions.id
),
updated_session AS MATERIALIZED (
    SELECT id FROM created_session
    UNION ALL
    SELECT id FROM updated_existing_session
),
inserted_run AS (
    INSERT INTO agent_runs (
        organization_id, workspace_id, resource_id, session_id, request_id, trace_id, turn_id,
        principal_type, principal_id, trust_source, runtime_mode,
        status, objective, max_steps, max_tool_calls, state_json
    )
    SELECT %s, %s, %s, updated_session.id, 'turn:' || %s, %s, %s,
           %s, %s, %s, %s, 'queued', %s::jsonb ->> 'message', 64, 32,
           jsonb_build_object('turn_id', %s::text, 'resource_id', %s::text,
                              'runtime_mode', %s::text)
    FROM updated_session RETURNING id, session_id
),
inserted_step AS (
    INSERT INTO agent_steps (run_id, step_key, step_type, input_json, max_attempts)
    SELECT inserted_run.id, 'understand_goal:1', 'UnderstandGoal',
           jsonb_build_object(
               'run_id', inserted_run.id::text,
               'request_fact_id', %s::text,
               'current_node', 'UnderstandGoal',
               'budget', jsonb_build_object(
                   'fact_id', 'budget:' || inserted_run.id::text || ':0',
                   'steps_remaining', 64,
                   'tool_calls_remaining', 32,
                   'tokens_remaining', NULL,
                   'cost_remaining', NULL,
                   'deadline_exceeded', false,
                   'exhausted_reason', NULL
               )
           ), 5
    FROM inserted_run RETURNING id, run_id
),
inserted_events AS (
    INSERT INTO agent_turn_events (turn_id, sequence_no, event_type, payload_json, created_at)
    SELECT %s::uuid, 1, 'turn.accepted', jsonb_build_object('turn_id', %s::text), %s
    UNION ALL
    SELECT %s::uuid, 2, 'run.queued', jsonb_build_object('run_id', inserted_step.run_id::text), %s
    FROM inserted_step RETURNING turn_id
),
inserted_outbox AS (
    INSERT INTO outbox_events (
        aggregate_type, aggregate_id, event_type, idempotency_key, payload_json, status, created_at
    )
    SELECT 'agent_turn', %s, 'agent.turn.accepted', 'turn-accepted:' || %s,
           jsonb_build_object('turn_id', %s::text, 'run_id', inserted_step.run_id::text),
           'pending', %s
    FROM inserted_step RETURNING id
)
SELECT inserted_run.session_id::text, inserted_run.id::text FROM inserted_run, inserted_outbox
"""

SELECT_TURN_SQL = """
SELECT turn.id::text, turn.session_id::text, run.id::text, turn.request_id,
       turn.status, turn.created_at, turn.updated_at, turn.input_hash
FROM agent_turns AS turn
LEFT JOIN agent_runs AS run ON run.turn_id = turn.id
WHERE turn.idempotency_scope = %s AND turn.request_id = %s
"""

LIST_TURN_EVENTS_SQL = """
SELECT event.id::text, event.turn_id::text, event.sequence_no, event.event_type,
       event.payload_json, event.created_at
FROM agent_turn_events AS event
WHERE event.turn_id = %s AND event.sequence_no > %s
ORDER BY event.sequence_no ASC, event.id ASC
"""

GET_PUBLIC_PROJECTION_SQL = """
SELECT projection.status, projection.dto_json, projection.last_event_sequence
FROM agent_turn_public_projections AS projection
WHERE projection.workspace_id = %s AND projection.turn_id = %s
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def fetchall(self) -> list[tuple[object, ...]]: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self) -> Any: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class TurnRepository:
    def __init__(self, pool: AsyncPool, *, now: Callable[[], datetime] | None = None) -> None:
        from datetime import UTC

        self._pool = pool
        self._now = now or (lambda: datetime.now(UTC))

    async def accept(self, value: AcceptTurn) -> tuple[Turn, bool]:
        request = value.request
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                CREATE_TURN_SQL,
                (
                    request.organization_id or None,
                    request.workspace_id,
                    request.resource_id,
                    request.session_id,
                    value.idempotency_scope,
                    request.request_id,
                    request.trace_id or None,
                    request.principal_type,
                    request.principal_id,
                    request.trust_source,
                    request.runtime_mode,
                    value.input_json,
                    value.input_hash,
                ),
            )
            inserted = await cursor.fetchone()
            if inserted is None:
                await cursor.execute(SELECT_TURN_SQL, (value.idempotency_scope, request.request_id))
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("轮次 幂等 查询 未返回数据行")
                if str(row[7]) != value.input_hash:
                    raise IdempotencyConflictError("轮次 请求 幂等 冲突")
                return _turn(row), False
            turn_id = str(inserted[0])
            now = self._now()
            title = request.message.strip()[:80].strip()
            await cursor.execute(
                ACCEPT_FACTS_SQL,
                _accept_fact_params(value, turn_id, title, now),
            )
            facts = await cursor.fetchone()
            if facts is None:
                raise LookupError("助手 会话 不存在")
            return Turn(
                id=turn_id,
                session_id=str(facts[0]),
                run_id=str(facts[1]),
                request_id=request.request_id,
                status=TurnStatus(str(inserted[3])),
                created_at=cast(datetime, inserted[4]),
                updated_at=cast(datetime, inserted[5]),
            ), True

    async def list_events(self, turn_id: str, after_sequence: int) -> list[TurnEvent]:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(LIST_TURN_EVENTS_SQL, (turn_id, after_sequence))
            rows = await cursor.fetchall()
        return [_event(row) for row in rows]

    async def get_public_projection(
        self, workspace_id: str, turn_id: str
    ) -> PublicProjection | None:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(GET_PUBLIC_PROJECTION_SQL, (workspace_id, turn_id))
            row = await cursor.fetchone()
        if row is None:
            return None
        dto = row[1]
        if isinstance(dto, str):
            dto = json.loads(dto)
        if not isinstance(dto, dict):
            raise ValueError("公开 投影 DTO 必须是对象")
        return PublicProjection(
            status=TurnStatus(str(row[0])),
            dto=cast(dict[str, Any], dto),
            last_event_sequence=int(cast(int, row[2])),
        )


def _turn(row: tuple[object, ...]) -> Turn:
    return Turn(
        id=str(row[0]),
        session_id=str(row[1]),
        run_id=str(row[2]),
        request_id=str(row[3]),
        status=TurnStatus(str(row[4])),
        created_at=cast(datetime, row[5]),
        updated_at=cast(datetime, row[6]),
    )


def _event(row: tuple[object, ...]) -> TurnEvent:
    payload = row[4]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("轮次 事件 载荷 必须是对象")
    return TurnEvent(
        id=str(row[0]),
        turn_id=str(row[1]),
        sequence=int(cast(int, row[2])),
        type=str(row[3]),
        payload=cast(dict[str, Any], payload),
        created_at=cast(datetime, row[5]),
    )


def _accept_fact_params(
    value: AcceptTurn, turn_id: str, title: str, now: datetime
) -> tuple[object, ...]:
    request = value.request
    return (
        request.session_id,
        request.workspace_id,
        request.workspace_id,
        title,
        request.session_id,
        request.resource_id,
        request.workspace_id,
        now,
        turn_id,
        value.input_json,
        now,
        now,
        now,
        request.organization_id or None,
        request.workspace_id,
        request.resource_id,
        turn_id,
        request.trace_id or None,
        turn_id,
        request.principal_type,
        request.principal_id,
        request.trust_source,
        request.runtime_mode,
        value.input_json,
        turn_id,
        request.resource_id,
        request.runtime_mode,
        turn_id,
        turn_id,
        turn_id,
        now,
        turn_id,
        now,
        turn_id,
        turn_id,
        turn_id,
        now,
    )


__all__ = [
    "ACCEPT_FACTS_SQL",
    "CREATE_TURN_SQL",
    "GET_PUBLIC_PROJECTION_SQL",
    "LIST_TURN_EVENTS_SQL",
    "SELECT_TURN_SQL",
    "TurnRepository",
]
