from __future__ import annotations

import inspect

from docreview.storage.postgres.runtime_repository import RuntimeRepository
from docreview.storage.postgres.runtime_sql import (
    APPROVAL_COLUMNS,
    CREATE_APPROVAL_SQL,
    DECIDE_APPROVAL_SQL,
    LOCK_APPROVAL_SQL,
    LOCK_WAITING_TARGET_SQL,
)


def normalized(value: str) -> str:
    return " ".join(value.lower().split())


def test_approval_sql_preserves_existing_schema_facts_and_parameterized_idempotency() -> None:
    columns = normalized(APPROVAL_COLUMNS)
    create = normalized(CREATE_APPROVAL_SQL)
    decision = normalized(DECIDE_APPROVAL_SQL)

    for column in (
        "requested_by_type",
        "requested_by_id",
        "decided_by_type",
        "decided_by_id",
        "decision_reason",
        "decided_at",
    ):
        assert column in columns
    assert "on conflict (workspace_id, run_id, idempotency_key) do nothing" in create
    assert "%s" in create and "%s" in decision
    assert "status = 'pending'" in decision


def test_approval_decision_locks_approval_then_waiting_step_and_run() -> None:
    approval_lock = normalized(LOCK_APPROVAL_SQL)
    target_lock = normalized(LOCK_WAITING_TARGET_SQL)

    assert "for update" in approval_lock
    assert "for update of step, run" in target_lock
    assert "step.id = %s and step.run_id = %s" in target_lock


def test_approval_repository_accepts_a_caller_owned_connection_for_one_transaction() -> None:
    for method in (RuntimeRepository.request_approval, RuntimeRepository.decide_approval):
        assert "connection" in inspect.signature(method).parameters
