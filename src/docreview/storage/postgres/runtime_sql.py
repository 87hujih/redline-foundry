"""Frozen PostgreSQL statements for the durable Python runtime."""

RUN_COLUMNS = """
id::text, organization_id::text, workspace_id::text, session_id::text, request_id,
trace_id, status, objective, current_step, max_steps, max_tool_calls, token_budget,
cost_budget, deadline_at, cancel_requested_at, state_json, version, created_at, updated_at,
resource_id::text, principal_type, principal_id::text, trust_source, runtime_mode
"""

STEP_COLUMNS = """
id::text, run_id::text, step_key, step_type, status, input_json, output_json,
error_json, claimed_by, lease_expires_at, heartbeat_at, lease_generation,
attempt_count, max_attempts, next_retry_at, started_at, completed_at, created_at, updated_at
"""

ATTEMPT_COLUMNS = """
id::text, step_id::text, attempt_number, provider, model, prompt_version, temperature,
context_manifest_id::text, trace_id, input_tokens, output_tokens, cost, latency_ms,
retry_count, finish_reason, error_category, started_at, completed_at
"""

CONTEXT_MANIFEST_COLUMNS = """
id::text, run_id::text, step_id::text, token_budget, reserved_output_tokens,
tokenizer, items_json, total_tokens, content_hash, created_at
"""

TOOL_COLUMNS = """
id::text, run_id::text, step_id::text, tool_name, tool_version, input_json,
output_json, status, idempotency_key, error_json, error_category, claimed_by,
lease_expires_at, lease_generation, attempt_count, started_at, completed_at, created_at
"""

OUTBOX_COLUMNS = """
id::text, aggregate_type, aggregate_id, event_type, idempotency_key, payload_json,
status, attempt_count, next_attempt_at, claimed_by, lease_expires_at,
lease_generation, error_json, created_at, published_at
"""

APPROVAL_COLUMNS = """
id::text, workspace_id::text, run_id::text, step_id::text, tool_name, tool_version,
idempotency_key, resources_json, resources_hash, payload_json, reason, status, created_at
"""

CREATE_APPROVAL_SQL = f"""
INSERT INTO agent_tool_approvals (
    workspace_id, run_id, step_id, tool_name, tool_version, idempotency_key,
    resources_json, resources_hash, payload_json, reason, requested_by_type, requested_by_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s)
ON CONFLICT (workspace_id, run_id, idempotency_key) DO NOTHING
RETURNING {APPROVAL_COLUMNS}
"""

GET_APPROVAL_SQL = f"""
SELECT {APPROVAL_COLUMNS} FROM agent_tool_approvals
WHERE workspace_id = %s AND run_id = %s AND idempotency_key = %s
"""

DECIDE_APPROVAL_SQL = """
UPDATE agent_tool_approvals
SET status = %s, decision_reason = %s, decided_by_type = %s,
    decided_by_id = %s, decided_at = %s
WHERE id = %s AND workspace_id = %s AND status = 'pending'
RETURNING id::text, workspace_id::text, run_id::text, step_id::text,
          tool_name, tool_version, idempotency_key, resources_json,
          resources_hash, payload_json, reason, status, created_at
"""

LOCK_APPROVAL_SQL = """
SELECT status, run_id::text, step_id::text, tool_name, tool_version,
       idempotency_key, payload_json, COALESCE(decided_by_type, ''),
       COALESCE(decided_by_id, ''), COALESCE(decision_reason, '')
FROM agent_tool_approvals
WHERE id = %s AND workspace_id = %s
FOR UPDATE
"""

AUTHORIZE_APPROVAL_DECISION_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM memberships AS membership
    JOIN users AS account ON account.id = membership.user_id
    JOIN workspaces AS workspace ON workspace.id = membership.workspace_id
    WHERE membership.workspace_id = %s AND membership.user_id = %s
      AND membership.status = 'active' AND account.status = 'active'
      AND workspace.status = 'active' AND membership.role IN ('owner', 'admin')
)
"""

AUTHORIZE_APPROVAL_RESOURCE_SQL = """
SELECT CASE %s
    WHEN 'document' THEN EXISTS (
        SELECT 1 FROM resources WHERE id = %s AND workspace_id = %s
    )
    WHEN 'artifact' THEN EXISTS (
        SELECT 1 FROM agent_artifacts WHERE id = %s AND workspace_id = %s
    )
    WHEN 'task' THEN EXISTS (
        SELECT 1 FROM tasks WHERE id = %s AND workspace_id = %s
    )
    ELSE false
END
"""

LOCK_WAITING_TARGET_SQL = """
SELECT step.status, run.status, step.output_json
FROM agent_steps AS step
JOIN agent_runs AS run ON run.id = step.run_id
WHERE step.id = %s AND step.run_id = %s
FOR UPDATE OF step, run
"""

FAIL_REJECTED_STEP_SQL = """
UPDATE agent_steps
SET status = 'failed', error_json = %s::jsonb, completed_at = %s, updated_at = %s
WHERE id = %s AND status = 'waiting_approval'
"""

FAIL_REJECTED_RUN_SQL = """
UPDATE agent_runs
SET status = 'failed', current_step = NULL, updated_at = %s, version = version + 1
WHERE id = %s AND status = 'waiting_approval'
"""

CREATE_APPROVED_STEP_SQL = """
INSERT INTO agent_steps (run_id, step_key, step_type, input_json, max_attempts)
VALUES (%s, %s, 'CommitPatch', %s::jsonb, 5)
ON CONFLICT (run_id, step_key) DO NOTHING
"""

GET_STEP_INPUT_SQL = """
SELECT step_type, input_json FROM agent_steps WHERE run_id = %s AND step_key = %s
"""

SUCCEED_APPROVAL_STEP_SQL = """
UPDATE agent_steps
SET status = 'succeeded', completed_at = %s, updated_at = %s
WHERE id = %s AND status = 'waiting_approval'
"""

QUEUE_APPROVAL_RUN_SQL = """
UPDATE agent_runs
SET status = 'queued', current_step = %s, updated_at = %s, version = version + 1
WHERE id = %s AND status = 'waiting_approval'
"""

CREATE_RUN_SQL = f"""
INSERT INTO agent_runs (
    organization_id, workspace_id, session_id, request_id, trace_id, objective,
    max_steps, max_tool_calls, token_budget, cost_budget, deadline_at, state_json,
    resource_id, principal_type, principal_id, trust_source, runtime_mode
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
        %s, %s, %s, %s, 'durable')
ON CONFLICT DO NOTHING
RETURNING {RUN_COLUMNS}
"""

GET_RUN_BY_REQUEST_SQL = f"""
SELECT {RUN_COLUMNS}
FROM agent_runs
WHERE workspace_id IS NOT DISTINCT FROM %s AND request_id = %s
"""

CREATE_STEP_SQL = f"""
INSERT INTO agent_steps (run_id, step_key, step_type, input_json, max_attempts)
VALUES (%s, %s, %s, %s::jsonb, %s)
ON CONFLICT (run_id, step_key) DO NOTHING
RETURNING {STEP_COLUMNS}
"""

GET_STEP_BY_KEY_SQL = f"""
SELECT {STEP_COLUMNS} FROM agent_steps WHERE run_id = %s AND step_key = %s
"""

CLAIM_STEP_SQL = f"""
WITH candidate AS (
    SELECT step.id AS candidate_step_id
    FROM agent_steps AS step
    JOIN agent_runs AS run ON run.id = step.run_id
    WHERE step.status = 'queued'
      AND (step.next_retry_at IS NULL OR step.next_retry_at <= %s)
      AND step.attempt_count < step.max_attempts
      AND run.status IN ('queued', 'running')
      AND run.runtime_mode = 'durable'
      AND run.principal_type IS NOT NULL
      AND run.principal_id IS NOT NULL
      AND run.trust_source IS NOT NULL
      AND length(btrim(run.trust_source)) > 0
      AND run.workspace_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM agent_steps AS active
          WHERE active.run_id = step.run_id AND active.status = 'running'
      )
    ORDER BY (
        run.cancel_requested_at IS NOT NULL
        OR (run.deadline_at IS NOT NULL AND run.deadline_at <= %s)
    ) DESC, step.next_retry_at NULLS FIRST, step.created_at, step.id
    FOR UPDATE OF step, run SKIP LOCKED
    LIMIT 1
)
UPDATE agent_steps AS step
SET status = 'running', claimed_by = %s, lease_expires_at = %s, heartbeat_at = %s,
    lease_generation = step.lease_generation + 1,
    attempt_count = step.attempt_count + 1, next_retry_at = NULL, error_json = NULL,
    started_at = COALESCE(step.started_at, %s), updated_at = %s
FROM candidate
WHERE step.id = candidate.candidate_step_id
RETURNING {STEP_COLUMNS}
"""

CLAIM_RUN_SQL = """
UPDATE agent_runs
SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
    current_step = %s, updated_at = %s, version = version + 1
WHERE id = %s AND status IN ('queued', 'running')
RETURNING version
"""

LOAD_WORK_SQL = """
SELECT run.version, run.deadline_at, run.cancel_requested_at,
       run.max_steps, run.max_tool_calls, run.token_budget, run.cost_budget,
       (SELECT COUNT(*) FROM agent_steps WHERE run_id = run.id)::integer,
       (SELECT COUNT(*) FROM tool_calls WHERE run_id = run.id)::integer,
       COALESCE((SELECT SUM(COALESCE(attempt.input_tokens, 0) +
                                  COALESCE(attempt.output_tokens, 0))
                 FROM agent_attempts AS attempt
                 JOIN agent_steps AS used_step ON used_step.id = attempt.step_id
                 WHERE used_step.run_id = run.id), 0)::bigint,
       COALESCE((SELECT SUM(COALESCE(attempt.cost, 0))
                 FROM agent_attempts AS attempt
                 JOIN agent_steps AS used_step ON used_step.id = attempt.step_id
                 WHERE used_step.run_id = run.id), 0)::double precision
FROM agent_runs AS run WHERE run.id = %s
"""

HEARTBEAT_STEP_SQL = """
UPDATE agent_steps
SET heartbeat_at = %s, lease_expires_at = %s, updated_at = %s
WHERE id = %s AND status = 'running' AND claimed_by = %s AND lease_generation = %s
  AND lease_expires_at > %s
"""

RETRY_STEP_SQL = """
UPDATE agent_steps
SET status = 'queued', error_json = %s::jsonb, claimed_by = NULL,
    lease_expires_at = NULL, heartbeat_at = NULL, next_retry_at = %s, updated_at = %s
WHERE id = %s AND status = 'running' AND claimed_by = %s AND lease_generation = %s
  AND lease_expires_at > %s AND attempt_count < max_attempts
"""

COMMIT_STEP_OUTCOME_SQL = """
UPDATE agent_steps
SET status = %s, output_json = %s::jsonb, error_json = %s::jsonb,
    claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
    completed_at = %s, updated_at = %s
WHERE id = %s AND status = 'running' AND claimed_by = %s
  AND lease_generation = %s AND lease_expires_at > %s
"""

COMMIT_RUN_OUTCOME_SQL = """
UPDATE agent_runs
SET status = %s, current_step = %s, updated_at = %s, version = version + 1
WHERE id = %s AND status = 'running' AND version = %s
"""

REQUEST_CANCEL_SQL = """
WITH target AS (
    UPDATE agent_runs
    SET cancel_requested_at = COALESCE(cancel_requested_at, %s),
        status = CASE WHEN status IN ('waiting_input', 'waiting_approval')
                      THEN 'queued' ELSE status END,
        updated_at = %s, version = version + 1
    WHERE id = %s AND status IN ('queued', 'running', 'waiting_input', 'waiting_approval')
      AND cancel_requested_at IS NULL
    RETURNING id
), existing AS (
    SELECT id FROM agent_runs
    WHERE id = %s AND status IN ('queued', 'running', 'waiting_input', 'waiting_approval')
      AND cancel_requested_at IS NOT NULL
), awakened AS (
    UPDATE agent_steps
    SET status = 'queued', next_retry_at = %s, completed_at = NULL,
        claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL, updated_at = %s
    WHERE run_id IN (SELECT id FROM target)
      AND status IN ('waiting_input', 'waiting_approval')
    RETURNING id
)
SELECT EXISTS (SELECT 1 FROM target) OR EXISTS (SELECT 1 FROM existing),
       COUNT(*)::integer FROM awakened
"""

RECOVER_EXPIRED_STEPS_SQL = """
WITH expired AS (
    UPDATE agent_steps
    SET status = CASE WHEN attempt_count < max_attempts THEN 'queued' ELSE 'failed' END,
        claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
        next_retry_at = CASE WHEN attempt_count < max_attempts THEN %s ELSE NULL END,
        completed_at = CASE WHEN attempt_count < max_attempts THEN NULL ELSE %s END,
        error_json = jsonb_build_object(
            'category', 'lease_expired', 'retryable', attempt_count < max_attempts
        ), updated_at = %s
    WHERE status = 'running' AND lease_expires_at <= %s
    RETURNING id, run_id, status, attempt_count
), close_attempts AS (
    UPDATE agent_attempts AS attempt
    SET error_category = 'lease_expired',
        finish_reason = COALESCE(attempt.finish_reason, 'worker_lease_expired'),
        completed_at = %s
    FROM expired
    WHERE attempt.step_id = expired.id
      AND attempt.attempt_number = expired.attempt_count
      AND attempt.completed_at IS NULL
), reset_runs AS (
    UPDATE agent_runs AS run
    SET status = CASE
            WHEN EXISTS (SELECT 1 FROM expired
                         WHERE expired.run_id = run.id AND expired.status = 'queued')
            THEN 'queued' ELSE 'failed' END,
        current_step = NULL, updated_at = %s, version = version + 1
    WHERE run.id IN (SELECT expired.run_id FROM expired) AND run.status = 'running'
)
SELECT COUNT(*) FILTER (WHERE status = 'queued')::integer,
       COUNT(*) FILTER (WHERE status = 'failed')::integer FROM expired
"""

CREATE_ATTEMPT_SQL = f"""
INSERT INTO agent_attempts (step_id, attempt_number, trace_id, started_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (step_id, attempt_number) DO NOTHING
RETURNING {ATTEMPT_COLUMNS}
"""

GET_ATTEMPT_SQL = f"""
SELECT {ATTEMPT_COLUMNS} FROM agent_attempts
WHERE step_id = %s AND attempt_number = %s
"""

FINISH_ATTEMPT_SQL = """
UPDATE agent_attempts
SET provider = COALESCE(NULLIF(%s, ''), provider),
    model = COALESCE(NULLIF(%s, ''), model),
    prompt_version = COALESCE(NULLIF(%s, ''), prompt_version),
    temperature = COALESCE(%s, temperature),
    context_manifest_id = COALESCE(NULLIF(%s, '')::uuid, context_manifest_id),
    input_tokens = %s, output_tokens = %s, cost = %s, latency_ms = %s,
    retry_count = %s, finish_reason = %s, error_category = %s, completed_at = %s
WHERE id = %s AND completed_at IS NULL
"""

CREATE_CONTEXT_MANIFEST_SQL = f"""
INSERT INTO context_manifests (
    run_id, step_id, token_budget, reserved_output_tokens, tokenizer,
    items_json, total_tokens, content_hash
)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
RETURNING {CONTEXT_MANIFEST_COLUMNS}
"""

GET_CONTEXT_MANIFEST_SQL = f"""
SELECT {CONTEXT_MANIFEST_COLUMNS} FROM context_manifests WHERE id = %s
"""

BEGIN_TOOL_SQL = f"""
INSERT INTO tool_calls (
    run_id, step_id, tool_name, tool_version, input_json, status, idempotency_key,
    started_at, claimed_by, lease_expires_at, lease_generation, attempt_count
)
VALUES (%s, %s, %s, %s, %s::jsonb, 'running', %s, %s, %s, %s, 1, 1)
ON CONFLICT DO NOTHING
RETURNING {TOOL_COLUMNS}
"""

LOCK_TOOL_BY_KEY_SQL = f"""
SELECT {TOOL_COLUMNS} FROM tool_calls
WHERE run_id = %s AND idempotency_key = %s
FOR UPDATE
"""

RECLAIM_TOOL_SQL = f"""
UPDATE tool_calls
SET status = 'running', claimed_by = %s, lease_expires_at = %s,
    lease_generation = lease_generation + 1, attempt_count = attempt_count + 1,
    started_at = COALESCE(started_at, %s)
WHERE id = %s AND (
    status = 'pending' OR (status = 'running' AND
    (lease_expires_at IS NULL OR lease_expires_at <= %s))
)
RETURNING {TOOL_COLUMNS}
"""

FINISH_TOOL_SQL = """
UPDATE tool_calls
SET status = %s, output_json = %s::jsonb, error_json = %s::jsonb,
    error_category = %s, latency_ms = %s, completed_at = %s,
    attempt_count = attempt_count + GREATEST(%s - 1, 0),
    claimed_by = NULL, lease_expires_at = NULL
WHERE id = %s AND status = 'running' AND claimed_by = %s
  AND lease_generation = %s AND lease_expires_at > %s
"""

CLAIM_OUTBOX_SQL = f"""
WITH candidates AS (
    SELECT event.id AS candidate_event_id
    FROM outbox_events AS event
    WHERE event.status = 'pending'
      AND (event.next_attempt_at IS NULL OR event.next_attempt_at <= %s)
      AND (%s::text[] IS NULL OR event.event_type = ANY(%s::text[]))
    ORDER BY event.next_attempt_at NULLS FIRST, event.created_at, event.id
    FOR UPDATE OF event SKIP LOCKED
    LIMIT %s
)
UPDATE outbox_events AS event
SET status = 'publishing', claimed_by = %s, lease_expires_at = %s,
    lease_generation = event.lease_generation + 1,
    attempt_count = event.attempt_count + 1, next_attempt_at = NULL
FROM candidates
WHERE event.id = candidates.candidate_event_id
RETURNING {OUTBOX_COLUMNS}
"""

ENQUEUE_OUTBOX_SQL = f"""
INSERT INTO outbox_events (
    aggregate_type, aggregate_id, event_type, idempotency_key, payload_json, next_attempt_at
)
VALUES (%s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT DO NOTHING
RETURNING {OUTBOX_COLUMNS}
"""

GET_OUTBOX_BY_KEY_SQL = f"""
SELECT {OUTBOX_COLUMNS} FROM outbox_events
WHERE aggregate_type = %s AND aggregate_id = %s AND idempotency_key = %s
"""

MARK_OUTBOX_PUBLISHED_SQL = """
UPDATE outbox_events
SET status = 'published', published_at = %s, error_json = NULL,
    claimed_by = NULL, lease_expires_at = NULL
WHERE id = %s AND status = 'publishing' AND claimed_by = %s
  AND lease_generation = %s AND lease_expires_at > %s
"""

RETRY_OUTBOX_SQL = """
UPDATE outbox_events
SET status = %s, error_json = %s::jsonb, next_attempt_at = %s,
    claimed_by = NULL, lease_expires_at = NULL
WHERE id = %s AND status = 'publishing' AND claimed_by = %s
  AND lease_generation = %s AND lease_expires_at > %s
"""

RECOVER_OUTBOX_SQL = """
UPDATE outbox_events
SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL,
    next_attempt_at = %s,
    error_json = jsonb_build_object('category', 'lease_expired', 'retryable', true)
WHERE status = 'publishing' AND lease_expires_at <= %s
"""

__all__ = [
    "APPROVAL_COLUMNS",
    "ATTEMPT_COLUMNS",
    "AUTHORIZE_APPROVAL_DECISION_SQL",
    "AUTHORIZE_APPROVAL_RESOURCE_SQL",
    "BEGIN_TOOL_SQL",
    "CLAIM_OUTBOX_SQL",
    "CLAIM_RUN_SQL",
    "CLAIM_STEP_SQL",
    "COMMIT_RUN_OUTCOME_SQL",
    "COMMIT_STEP_OUTCOME_SQL",
    "CONTEXT_MANIFEST_COLUMNS",
    "CREATE_APPROVAL_SQL",
    "CREATE_APPROVED_STEP_SQL",
    "CREATE_ATTEMPT_SQL",
    "CREATE_CONTEXT_MANIFEST_SQL",
    "CREATE_RUN_SQL",
    "CREATE_STEP_SQL",
    "DECIDE_APPROVAL_SQL",
    "ENQUEUE_OUTBOX_SQL",
    "FAIL_REJECTED_RUN_SQL",
    "FAIL_REJECTED_STEP_SQL",
    "FINISH_ATTEMPT_SQL",
    "FINISH_TOOL_SQL",
    "GET_APPROVAL_SQL",
    "GET_ATTEMPT_SQL",
    "GET_CONTEXT_MANIFEST_SQL",
    "GET_OUTBOX_BY_KEY_SQL",
    "GET_RUN_BY_REQUEST_SQL",
    "GET_STEP_BY_KEY_SQL",
    "GET_STEP_INPUT_SQL",
    "HEARTBEAT_STEP_SQL",
    "LOAD_WORK_SQL",
    "LOCK_APPROVAL_SQL",
    "LOCK_TOOL_BY_KEY_SQL",
    "LOCK_WAITING_TARGET_SQL",
    "MARK_OUTBOX_PUBLISHED_SQL",
    "OUTBOX_COLUMNS",
    "QUEUE_APPROVAL_RUN_SQL",
    "RECLAIM_TOOL_SQL",
    "RECOVER_EXPIRED_STEPS_SQL",
    "RECOVER_OUTBOX_SQL",
    "REQUEST_CANCEL_SQL",
    "RETRY_OUTBOX_SQL",
    "RETRY_STEP_SQL",
    "RUN_COLUMNS",
    "STEP_COLUMNS",
    "SUCCEED_APPROVAL_STEP_SQL",
    "TOOL_COLUMNS",
]
