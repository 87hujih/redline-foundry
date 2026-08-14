# Persistence Contract Freeze

**证据时间：** 2026-08-12。Python 必须保留当前 PostgreSQL 表、约束、参数化 SQL 的可观察行为。Phase 0 不连接数据库、不运行 migration/DDL/backfill、不改写 SQL。迁移文件是只读事实来源；未来 Schema 变更必须另行批准并遵守 expand → dual write/backfill → verify → switch read → contract。

## Durable aggregate

```text
Workspace/Principal
  -> Turn -> Run -> Step -> Attempt -> Tool Call / Observation / Manifest
  -> Approval -> Commit -> Outbox -> Projection/Receipt
```

Run/Step/Attempt/Tool/Approval/Commit/Outbox/Projection are facts. LangGraph state/checkpoint, in-memory wake channels, SSE connections and UI state are reconstructable projections and cannot become an alternate source of truth.

## Facts and immutable behavior

| Fact | Current table/repository | State/identity contract | Python invariant |
| --- | --- | --- | --- |
| Run | `agent_runs`; `agentrun.Repository`, `EngineStore` | statuses `queued`, `running`, `waiting_input`, `waiting_approval`, `succeeded`, `failed`, `cancelled`; `(workspace_id, request_id)` unique; legacy/null workspace has partial global request unique index; positive budgets/version | Preserve status checks, current step, deadlines/cancel, state JSON object, optimistic version, and exact workspace/request lookup. |
| Step | `agent_steps` | `(run_id, step_key)` unique; typed input/output/error objects; queued claim; running owner/expiry/heartbeat/generation; attempts/retry; completed only terminal | Claim one runnable step with DB lock; changed type/input/retry policy under same key is conflict. |
| Attempt | `agent_attempts` | `(step_id, attempt_number)` unique; provider/model/prompt/temp/context/trace/usage/cost/latency/retry/finish/error telemetry | Persist start and terminal outcome; nonnegative counters; abandoned lease closes as `lease_expired`. |
| Tool | `tool_calls`; `agenttools.Runtime`, `agentrun.ToolAuditStore` | status `pending/running/succeeded/failed/cancelled`; `(run_id,idempotency_key)` unique when set; tool/version/input/output/error/provenance/audit | Every model/tool call goes through versioned Registry + schema + Policy + rate limit + audit; same key/different facts conflicts. |
| Observation | `agent_observations` | `(run_id, observation_key)` unique; payload object, hash, `novel`; optional tool call | Store full bounded result durably; GraphState keeps only references; repeated hash increments no-progress. |
| Context | `context_manifests` | immutable ordered items, tokenizer, total/reserved/budget and hash; `(run_id,step_id)` lookup | Re-load exact manifest by ID; never rebuild from current retrieval/document. |
| Approval | `agent_tool_approvals`; `agentpolicy.ApprovalStore` | pending/approved/rejected/cancelled; bound workspace/run/request-step/tool/version/write key/resource hash; owner/admin external decision | Request creates pending only. Approve atomically creates exact CommitPatch continuation and queues run; reject terminally fails with `policy_blocked`; repeat same decision replays, opposite conflicts. |
| Commit | `document_patch_commits`; `documentcommit.Committer` + PostgreSQL adapter | `(workspace_id,idempotency_key)` unique; Patch hash/base/new version/outbox/actor binding | Serializable lock/recheck, validate AST/expected hashes/scope/evidence, insert full version bundle + one outbox event atomically; same key/hash returns prior IDs. |
| Outbox | `outbox_events`; `outbox.Repository`, `ProjectionWorker` | `pending/publishing/published/dead_letter`; aggregate/idempotency unique; claim owner/expiry/generation; bounded retry | Event intent is inserted in the writer transaction; publication is retryable and lease fenced; no manual key replacement. |
| Projection | `agent_turn_public_projections`, `outbox_projection_receipts`; `RuntimeProjector` | terminal/waiting public states only; DTO/content hash/last event sequence; `(event_id,projection_name)` receipt unique | Projection reads facts, preserves sequence, writes receipt idempotently, and never exposes raw state/manifest/tool payloads. |

## State machines

### Turn

`accepted -> running -> waiting_input|waiting_approval|succeeded|failed|cancelled`; waiting states may resume to `running` or terminal. Terminal states do not transition. `agent_turn_outcomes` permits `running`, waiting and terminal outcome records; outcome transition is checked by `turn.CanTransition` before writing.

### Run and Step

`queued -> running`; running may retry to queued with deterministic backoff, wait for input/approval, succeed, fail or cancel. Expired running lease requeues if attempts remain, otherwise fails. Waiting approval is resumed only by the bound external decision transaction. Cancellation is idempotent and wakes waiting steps. Run/Step status constraints and completion timestamps are database-enforced.

### Tool and Outbox

Tool: `pending -> running -> succeeded|failed|cancelled`; expired running tool calls may be reclaimed by generation. Outbox: `pending -> publishing -> published|dead_letter`; expired publishing returns to pending. Only declared retryable categories (`rate_limited`, `timeout`, `retryable_upstream`, plus lease expiry at engine boundary) may retry.

## Transaction boundaries

1. **Turn acceptance (`agentturn.Repository.Accept`)**: canonical input JSON/hash and idempotency lookup; one transaction creates/reuses session, user message, `agent_turn`, linked `agent_run`, initial `UnderstandGoal` step, ordered turn events, and `agent.turn.accepted` outbox.
2. **Turn outcome (`CommitOutcome`)**: insert idempotent outcome fact; lock session and turn; validate transition; append assistant/system messages and ordered events; update turn/public projection; insert `agent.turn.outcome_committed` outbox; commit or roll back as one unit.
3. **Step outcome/retry**: existing `pgx.Tx` carries lease-fenced attempt/step/run updates and deterministic outbox insertion. Heartbeat and completion require exact owner, generation and unexpired lease.
4. **Approval decision**: authenticated owner/admin decision locks the approval/run/step and atomically either creates the unique CommitPatch step plus approval outbox or writes rejected terminal state plus rejection outbox.
5. **Canonical commit**: Serializable transaction locks workspace/idempotency, resource/current version, rechecks base version and every expected node hash, writes `resource_versions`, canonical document/nodes/source mappings, derived sections/chunks/profile metadata, `document_patch_commits`, and `document.version.committed` outbox.
6. **Projection publication**: claim/publish/receipt updates are separate transactions from the original fact transaction; receipt and event identity make replay idempotent.

## Claim, lease generation and stale-worker fencing

Step and Outbox claim use one SQL statement with `FOR UPDATE SKIP LOCKED`, setting owner, expiry, heartbeat/attempt and incrementing `lease_generation`. Tool call recovery uses the same owner/expiry/generation pattern. Heartbeat, retry, completion, approval resume and publication all require:

```text
status is the expected running/publishing state
AND claimed_by == worker_id
AND lease_generation == claimed_generation
AND lease_expires_at > now()
```

An expired worker cannot overwrite a newer claimant. Startup/periodic recovery closes abandoned Attempts as `lease_expired`, requeues or fails Steps according to `max_attempts`, and returns expired Outbox publication to `pending`.

## Idempotency keys

- Turn acceptance: `(idempotency_scope, request_id)` where scope is workspace, organization, session, or global compatibility scope; scope is immutable after acceptance.
- Run creation: `(workspace_id, request_id)`; null workspace uses a separate partial global unique index.
- Step: `(run_id, step_key)`.
- Attempt: `(step_id, attempt_number)`.
- Tool: `(run_id, idempotency_key)`.
- Approval request: `(workspace_id, run_id, idempotency_key)`; target write key is deliberately a different domain.
- Patch commit: `(workspace_id, idempotency_key)`.
- Outbox: `(aggregate_type, aggregate_id, idempotency_key)`.
- Projection receipt: `(event_id, projection_name)`.
- Operator action (retained Go baseline): `(workspace_id, request_id)`.

Identical replay returns the stored fact/result. Any changed canonical input, output, tool/version/input, Patch, approval decision or event payload under an existing key returns a conflict. Database uniqueness remains the concurrency guard.

## Workspace, Resource and Principal isolation

Trusted ingress supplies Principal and exact Workspace; request payload/model output cannot supply or override them. Query handlers require signed user identity. Policy resolves active membership/role and Resource ownership. Every runtime repository query and both retrieval channels constrain workspace, resource and exact resolved version. Committer rechecks Workspace/Resource/current version/node authorization inside the transaction. Cross-Workspace targets appear as not-found/denied, never as another tenant's data. Historical nullable workspace columns and pre-profile embeddings remain compatibility facts; Python must not invent backfill or silently broaden scope.

## Current relevant tables and migrations

| Migration | Tables/columns relevant to active closure | Status in Phase 0 |
| --- | --- | --- |
| 001-005, 007-015 | `resources`, `resource_versions`, `resource_chunks`; `assistant_sessions`, `assistant_messages`; `uploaded_files`; grounded structures/sections/chunks; session context/runtime projections | Existing compatibility facts; read/write behavior used by active resource/assistant/upload paths must remain. |
| 006/008-010 | Legacy `tasks`, `approvals`, `execution_jobs`, notifications and task suggestion idempotency | Not registered in current Router; retain Go, do not migrate into Python online closure. |
| 016 | `users`, `organizations`, `workspaces`, `memberships`, `principal_audit_events`; nullable workspace columns on existing tables | Expand-only identity/tenancy facts; no Phase 0 database execution or backfill. |
| 017 | `agent_runs`, `agent_steps`, `context_manifests`, `agent_attempts`, `tool_calls`, `outbox_events` | Durable runtime source of truth; SQL is frozen. |
| 018 | `agent_turns`, `agent_turn_events`, `agent_turn_outcomes`; Turn/Outcome links on messages/runs | Turn request/outcome and SSE replay facts; SQL is frozen. |
| 019 | Tool leases; `agent_artifacts`, `agent_tool_approvals`, rate-limit buckets | Tool/approval/artifact controls; SQL is frozen. |
| 020 | `agent_observations`, `agent_shadow_comparisons` | Observation/evaluation facts; shadow is not a current request path. |
| 021-022 | Canonical AST/nodes/source mappings/Patch commits; retrieval profiles/embedding metadata/indexes | Required by future typed backend; no migration/backfill/traffic switch in Phase 0. |
| 023 | Turn/Run scope columns, public projections, projection receipts, cutover comparisons | Current durable projection contract; append-only. |
| 024 | `agent_runtime_operator_actions` | Go operations audit baseline; Python does not replace in Phase 0. |

Evidence: `apps/server/internal/storage/postgres/migrations/001_mvp_init.sql:7-107`, `004_assistant_sessions.sql:1-24`, `005_uploaded_files.sql:1-19`, `009_assistant_context_snapshots.sql:1-19`, `011_grounded_structured_document_rag_phase1.sql:1-120`, and migrations 016-024 line references above. Exact SQL statements and constraints are the compatibility oracle; Python must use parameterized SQL and preserve ordering, NULL behavior, `ON CONFLICT`, locks, partial indexes, cascade/set-null actions, and error classification.

## SQL behavior that Python must not change

- Do not replace `FOR UPDATE SKIP LOCKED` claims with process-local queues or ORM polling that changes lock/order semantics.
- Do not remove owner, lease expiry, heartbeat or generation predicates from heartbeat/completion/retry/publication updates.
- Do not widen queries and filter Workspace/Resource in memory; scope predicates belong in every SQL read and write boundary.
- Do not change unique/idempotency conflict behavior, canonical JSON/hash calculation, sequence ordering, or `Last-Event-ID` replay.
- Do not make nullable legacy workspace/profile fields non-null, rewrite existing migration files, or delete legacy facts in this rewrite phase.
- Do not make projection, embedding, notification or external provider I/O part of the acceptance transaction unless the existing contract explicitly does so; Outbox remains the transactional handoff.

## Database safety for later phases

No database connection is allowed in Phase 0. Later tests may connect only through the shared test fuse: `ALLOW_DB_TESTS=1`, process-only `TEST_DATABASE_URL`, database name ending `_test`, and exact approved host allowlist. Production `DATABASE_URL` and `.env` must never be fallback test inputs. Any missing condition requires a documented skip, not a connection attempt.
