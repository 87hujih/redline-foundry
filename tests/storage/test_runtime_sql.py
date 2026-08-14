from docreview.storage.postgres.runtime_projection_repository import (
    LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL,
    LOAD_STEP_SNAPSHOT_SQL,
    RECEIPT_INSERT_SQL,
    UPSERT_PUBLIC_PROJECTION_SQL,
)
from docreview.storage.postgres.runtime_sql import (
    AUTHORIZE_APPROVAL_DECISION_SQL,
    AUTHORIZE_APPROVAL_RESOURCE_SQL,
    BEGIN_TOOL_SQL,
    CLAIM_OUTBOX_SQL,
    CLAIM_STEP_SQL,
    COMMIT_STEP_OUTCOME_SQL,
    CREATE_RUN_SQL,
    CREATE_STEP_SQL,
    ENQUEUE_OUTBOX_SQL,
    HEARTBEAT_STEP_SQL,
    RECLAIM_TOOL_SQL,
    RECOVER_EXPIRED_STEPS_SQL,
    RETRY_STEP_SQL,
)


def normalized(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_step_claim_preserves_database_lock_and_scope_contract() -> None:
    sql = normalized(CLAIM_STEP_SQL)
    assert "for update of step, run skip locked" in sql
    assert "run.runtime_mode = 'durable'" in sql
    assert "run.workspace_id is not null" in sql
    assert "not exists" in sql and "active.status = 'running'" in sql
    assert "lease_generation = step.lease_generation + 1" in sql


def test_lease_fencing_predicates_are_present_on_heartbeat_retry_and_outcome() -> None:
    for statement in (HEARTBEAT_STEP_SQL, RETRY_STEP_SQL, COMMIT_STEP_OUTCOME_SQL):
        sql = normalized(statement)
        assert "status = 'running'" in sql
        assert "claimed_by = %s" in sql
        assert "lease_generation = %s" in sql
        assert "lease_expires_at > %s" in sql


def test_run_and_step_creation_are_parameterized_and_idempotent() -> None:
    assert "on conflict do nothing" in normalized(CREATE_RUN_SQL)
    assert "on conflict (run_id, step_key) do nothing" in normalized(CREATE_STEP_SQL)
    assert "%s::jsonb" in normalized(CREATE_RUN_SQL)
    assert "%s::jsonb" in normalized(CREATE_STEP_SQL)


def test_outbox_is_transactional_and_claimable_with_skip_locked() -> None:
    assert "on conflict do nothing" in normalized(ENQUEUE_OUTBOX_SQL)
    assert "for update of event skip locked" in normalized(CLAIM_OUTBOX_SQL)
    assert "lease_generation = event.lease_generation + 1" in normalized(CLAIM_OUTBOX_SQL)


def test_recovery_closes_attempts_and_requeues_or_fails_steps() -> None:
    sql = normalized(RECOVER_EXPIRED_STEPS_SQL)
    assert "error_category = 'lease_expired'" in sql
    assert "attempt.completed_at is null" in sql
    assert "then 'queued' else 'failed'" in sql


def test_projection_reads_are_durable_scoped_and_receipts_idempotent() -> None:
    assert "run.runtime_mode = 'durable'" in normalized(LOAD_STEP_SNAPSHOT_SQL)
    assert "run.turn_id is not null" in normalized(LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL)
    assert "approval.status = 'rejected'" in normalized(LOAD_REJECTED_APPROVAL_SNAPSHOT_SQL)
    assert "on conflict (event_id, projection_name) do nothing" in normalized(RECEIPT_INSERT_SQL)
    assert "last_event_sequence" in normalized(UPSERT_PUBLIC_PROJECTION_SQL)
    assert "greatest" in normalized(UPSERT_PUBLIC_PROJECTION_SQL)
    assert "jsonb_build_object( 'session'" in normalized(UPSERT_PUBLIC_PROJECTION_SQL)
    assert "'messages', coalesce" in normalized(UPSERT_PUBLIC_PROJECTION_SQL)
    assert "order by message.sequence_no, message.id" in normalized(UPSERT_PUBLIC_PROJECTION_SQL)


def test_tool_claim_and_approval_decision_are_fenced_and_authorized() -> None:
    begin = normalized(BEGIN_TOOL_SQL)
    reclaim = normalized(RECLAIM_TOOL_SQL)
    approval = normalized(AUTHORIZE_APPROVAL_DECISION_SQL)
    assert "lease_generation" in begin and "attempt_count" in begin
    assert "lease_generation = lease_generation + 1" in reclaim
    assert "lease_expires_at <= %s" in reclaim
    assert "membership.role in ('owner', 'admin')" in approval
    assert "membership.status = 'active'" in approval
    assert "when 'document'" in normalized(AUTHORIZE_APPROVAL_RESOURCE_SQL)
    assert "when 'artifact'" in normalized(AUTHORIZE_APPROVAL_RESOURCE_SQL)
