"""PostgreSQL adapters for durable runtime snapshots, outcomes and receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from docreview.runtime.codec import canonical_json
from docreview.runtime.models import Outbox
from docreview.runtime.projection import RuntimeSnapshot


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


LOAD_STEP_SNAPSHOT_SQL = """
SELECT run.turn_id::text, run.id::text, step.step_type,
       COALESCE(step.output_json, '{}'::jsonb), step.error_json
FROM agent_runs AS run
JOIN agent_steps AS step ON step.run_id = run.id
WHERE run.id = %s AND step.id = %s
  AND run.runtime_mode = 'durable' AND run.turn_id IS NOT NULL
"""

LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL = """
SELECT run.turn_id::text, run.id::text, run.status, step.step_type,
       COALESCE(step.output_json, '{}'::jsonb), step.error_json
FROM agent_runs AS run
JOIN agent_tool_approvals AS approval ON approval.run_id = run.id
JOIN agent_steps AS step ON step.id = approval.step_id AND step.run_id = run.id
WHERE run.id = %s AND approval.id = %s AND approval.status = 'rejected'
  AND run.runtime_mode = 'durable' AND run.turn_id IS NOT NULL
"""

INSERT_OUTCOME_SQL = """
INSERT INTO agent_turn_outcomes (
    turn_id, idempotency_key, outcome_hash, status, output_json, error_json
)
VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
ON CONFLICT (turn_id, idempotency_key) DO NOTHING
RETURNING id
"""

GET_OUTCOME_SQL = """
SELECT outcome_hash FROM agent_turn_outcomes
WHERE turn_id = %s AND idempotency_key = %s
"""

UPSERT_PUBLIC_PROJECTION_SQL = """
INSERT INTO agent_turn_public_projections (
    turn_id, workspace_id, status, dto_json, content_hash, last_event_sequence
)
SELECT turn.id, turn.workspace_id, %s,
       jsonb_build_object(
           'session', jsonb_build_object(
               'id', session.id::text,
               'title', session.title,
               'web_search_enabled', session.web_search_enabled,
               'last_message_at', session.last_message_at,
               'created_at', session.created_at,
               'updated_at', session.updated_at
           ),
           'messages', COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'id', message.id::text,
                   'role', message.role,
                   'kind', message.kind,
                   'payload', message.payload,
                   'sequence_no', message.sequence_no,
                   'created_at', message.created_at
               ) ORDER BY message.sequence_no, message.id)
               FROM assistant_messages AS message
               WHERE message.session_id = session.id
           ), '[]'::jsonb)
       ), %s,
       COALESCE((SELECT MAX(sequence_no) FROM agent_turn_events WHERE turn_id = turn.id), 0)
FROM agent_turns AS turn
JOIN assistant_sessions AS session ON session.id = turn.session_id
WHERE turn.id = %s AND turn.workspace_id IS NOT NULL
  AND %s IN ('waiting_input', 'waiting_approval', 'succeeded', 'failed', 'cancelled')
ON CONFLICT (turn_id) DO UPDATE
SET status = EXCLUDED.status, dto_json = EXCLUDED.dto_json,
    content_hash = EXCLUDED.content_hash,
    last_event_sequence = GREATEST(
        agent_turn_public_projections.last_event_sequence,
        EXCLUDED.last_event_sequence
    ), updated_at = now()
"""

LOCK_TURN_SQL = """
SELECT turn.session_id::text, turn.status
FROM agent_turns AS turn
JOIN assistant_sessions AS session ON session.id = turn.session_id
WHERE turn.id = %s
FOR UPDATE OF turn, session
"""

TURN_SEQUENCE_BASE_SQL = """
SELECT COALESCE(MAX(sequence_no), 0)::integer
FROM agent_turn_events WHERE turn_id = %s
"""

INSERT_ASSISTANT_MESSAGE_SQL = """
INSERT INTO assistant_messages (
    session_id, turn_id, outcome_id, role, kind, sequence_no, payload
)
SELECT %s, %s, %s, %s, %s,
       COALESCE(MAX(sequence_no), 0) + 1, %s::jsonb
FROM assistant_messages WHERE session_id = %s
RETURNING id::text, sequence_no, created_at
"""

INSERT_TURN_EVENT_SQL = """
INSERT INTO agent_turn_events (turn_id, sequence_no, event_type, payload_json)
VALUES (%s, %s, %s, %s::jsonb)
"""

UPDATE_TURN_SQL = """
UPDATE agent_turns
SET status = %s, output_json = %s::jsonb, error_json = %s::jsonb,
    updated_at = now(), started_at = COALESCE(started_at, now()),
    completed_at = CASE WHEN %s IN ('succeeded', 'failed', 'cancelled')
                        THEN now() ELSE NULL END
WHERE id = %s
"""

UPDATE_SESSION_SQL = """
UPDATE assistant_sessions SET last_message_at = now(), updated_at = now() WHERE id = %s
"""

RECEIPT_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1 FROM outbox_projection_receipts
    WHERE event_id = %s AND projection_name = %s
)
"""

RECEIPT_INSERT_SQL = """
INSERT INTO outbox_projection_receipts (event_id, projection_name, payload_hash)
VALUES (%s, %s, %s)
ON CONFLICT (event_id, projection_name) DO NOTHING
"""

RECEIPT_GET_HASH_SQL = """
SELECT payload_hash FROM outbox_projection_receipts
WHERE event_id = %s AND projection_name = %s
"""


class AsyncCursor(Protocol):
    async def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...
    async def fetchone(self) -> tuple[object, ...] | None: ...
    async def __aenter__(self) -> AsyncCursor: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncConnection(Protocol):
    def cursor(self) -> AsyncCursor: ...
    def transaction(self) -> Any: ...
    async def commit(self) -> None: ...
    async def __aenter__(self) -> AsyncConnection: ...
    async def __aexit__(self, *args: object) -> None: ...


class AsyncPool(Protocol):
    def connection(self) -> AsyncConnection: ...


class RuntimeProjectionRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self._pool = pool

    async def load(self, event: Outbox) -> RuntimeSnapshot:
        run_id = str(event.payload.get("run_id", "")).strip()
        if not run_id:
            raise ValueError("projection event run_id is required")
        if event.event_type == "agent.step.outcome_committed":
            target_id = str(event.payload.get("step_id", "")).strip()
            query = LOAD_STEP_SNAPSHOT_SQL
        elif event.event_type == "agent.tool_approval.rejected":
            target_id = str(event.payload.get("approval_id", "")).strip()
            query = LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL
        else:
            raise ValueError(f"unsupported projection event {event.event_type!r}")
        if not target_id:
            raise ValueError("projection event target id is required")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(query, (run_id, target_id))
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("durable projection snapshot not found")
        if event.event_type == "agent.step.outcome_committed":
            return RuntimeSnapshot(
                turn_id=str(row[0]),
                run_id=str(row[1]),
                run_status=str(event.payload.get("run_status", "")),
                step_type=str(row[2]),
                output=_object(row[3]),
                error=None if row[4] is None else _object(row[4]),
            )
        return RuntimeSnapshot(
            turn_id=str(row[0]),
            run_id=str(row[1]),
            run_status=str(row[2]),
            step_type=str(row[3]),
            output=_object(row[4]),
            error=None if row[5] is None else _object(row[5]),
        )

    async def commit_projection_outcome(
        self,
        turn_id: str,
        idempotency_key: str,
        status: str,
        output: dict[str, object],
        error: dict[str, object] | None,
        message: dict[str, str] | None,
    ) -> None:
        messages: list[dict[str, object]] | None = None
        if message is not None:
            messages = [
                {
                    "role": message["role"],
                    "kind": message["kind"],
                    "payload": {"content": message["content"]},
                }
            ]
        digest = _outcome_hash(status, output, error, messages)
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute(
                INSERT_OUTCOME_SQL,
                (
                    turn_id,
                    idempotency_key,
                    digest,
                    status,
                    canonical_json(output),
                    None if error is None else canonical_json(error),
                ),
            )
            inserted = await cursor.fetchone()
            if inserted is None:
                await cursor.execute(GET_OUTCOME_SQL, (turn_id, idempotency_key))
                existing = await cursor.fetchone()
                if existing is None or str(existing[0]) != digest:
                    raise ValueError("projection outcome idempotency conflict")
                return
            outcome_id = str(inserted[0])
            await cursor.execute(LOCK_TURN_SQL, (turn_id,))
            turn = await cursor.fetchone()
            if turn is None:
                raise LookupError("projection turn not found")
            session_id, current_status = str(turn[0]), str(turn[1])
            if not _can_transition(current_status, status):
                raise ValueError(f"invalid turn transition {current_status!r} -> {status!r}")
            await cursor.execute(TURN_SEQUENCE_BASE_SQL, (turn_id,))
            sequence_row = await cursor.fetchone()
            sequence = int(cast(int, sequence_row[0])) if sequence_row else 0
            if message is not None:
                payload = {"content": message["content"]}
                await cursor.execute(
                    INSERT_ASSISTANT_MESSAGE_SQL,
                    (
                        session_id,
                        turn_id,
                        outcome_id,
                        message["role"],
                        message["kind"],
                        canonical_json(payload),
                        session_id,
                    ),
                )
                message_row = await cursor.fetchone()
                if message_row is None:
                    raise RuntimeError("projection message insert returned no row")
                sequence += 1
                await cursor.execute(
                    INSERT_TURN_EVENT_SQL,
                    (
                        turn_id,
                        sequence,
                        "assistant.message",
                        canonical_json(
                            {
                                "id": str(message_row[0]),
                                "role": message["role"],
                                "kind": message["kind"],
                                "sequence_no": int(cast(int, message_row[1])),
                                "payload": payload,
                                "created_at": _timestamp(cast(datetime, message_row[2])),
                            }
                        ),
                    ),
                )
            sequence += 1
            await cursor.execute(
                INSERT_TURN_EVENT_SQL,
                (
                    turn_id,
                    sequence,
                    f"turn.{status}",
                    canonical_json({"turn_id": turn_id, "status": status}),
                ),
            )
            await cursor.execute(
                UPDATE_TURN_SQL,
                (
                    status,
                    canonical_json(output),
                    None if error is None else canonical_json(error),
                    status,
                    turn_id,
                ),
            )
            await cursor.execute(UPDATE_SESSION_SQL, (session_id,))
            await cursor.execute(
                UPSERT_PUBLIC_PROJECTION_SQL,
                (status, digest, turn_id, status),
            )
            await cursor.execute(
                """
                INSERT INTO outbox_events (
                    aggregate_type, aggregate_id, event_type, idempotency_key, payload_json
                )
                VALUES ('agent_turn', %s, 'agent.turn.outcome_committed', %s, %s::jsonb)
                ON CONFLICT DO NOTHING
                """,
                (
                    turn_id,
                    f"turn-outcome:{outcome_id}",
                    canonical_json(
                        {"turn_id": turn_id, "outcome_id": outcome_id, "status": status}
                    ),
                ),
            )

    async def exists(self, event_id: str, projection_name: str) -> bool:
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(RECEIPT_EXISTS_SQL, (event_id, projection_name))
            row = await cursor.fetchone()
        return bool(row and row[0])

    async def record(self, event_id: str, projection_name: str, payload_hash: str) -> None:
        if not payload_hash.startswith("sha256:") or len(payload_hash) != 71:
            raise ValueError("projection payload hash is invalid")
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(RECEIPT_INSERT_SQL, (event_id, projection_name, payload_hash))
            if int(getattr(cursor, "rowcount", 1)) != 1:
                await cursor.execute(RECEIPT_GET_HASH_SQL, (event_id, projection_name))
                row = await cursor.fetchone()
                if row is None or str(row[0]) != payload_hash:
                    raise ValueError("projection receipt idempotency conflict")
            await connection.commit()


def _object(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("projection JSON must be an object")
    return cast(dict[str, object], value)


def _can_transition(current: str, target: str) -> bool:
    if current in {"accepted", "running"}:
        return target in {
            "running",
            "waiting_input",
            "waiting_approval",
            "succeeded",
            "failed",
            "cancelled",
        }
    if current in {"waiting_input", "waiting_approval"}:
        return target in {"running", "succeeded", "failed", "cancelled"}
    return False


def _outcome_hash(
    status: str,
    output: dict[str, object],
    error: dict[str, object] | None,
    messages: list[dict[str, object]] | None,
) -> str:
    envelope: dict[str, object] = {
        "status": status,
        "output_json": json.loads(canonical_json(output)),
    }
    if error is not None:
        envelope["error_json"] = json.loads(canonical_json(error))
    envelope["messages"] = json.loads(canonical_json(messages))
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


__all__ = [
    "INSERT_OUTCOME_SQL",
    "LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL",
    "LOAD_STEP_SNAPSHOT_SQL",
    "RECEIPT_EXISTS_SQL",
    "RECEIPT_INSERT_SQL",
    "UPSERT_PUBLIC_PROJECTION_SQL",
    "RuntimeProjectionRepository",
]
