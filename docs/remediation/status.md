# Python Rewrite Status

## Current phase

**Phase 7: Go/Python 对账和 Canary 准备，完成离线实现并暂停。**

This user-defined Phase 7 adds versioned capture-only parity evidence, re-audits the active Go
production scope, and prepares a protected-ingress single-write canary/rollback procedure. It does
not execute a production canary, connect to PostgreSQL, migrate legacy Agent workflows, or delete Go
code. Evidence is the read-only `G:\gofile\Agent_Project` filesystem state inspected on 2026-08-14.
All writes are below `G:\gofile\docs_reviewAgent`.

## Assistant upload persistence remediation

- Closed the audited Assistant upload persistence gap without changing routes, public DTOs,
  status codes or schema. Successful conversation/session uploads now generate UUID v4 Resource,
  ResourceVersion, UploadedFile, Session/Message facts as applicable and return only the frozen
  `session`, `resource`, `messages` and `error_message` fields.
- `UploadMetadataRepository.persist_upload` uses one injected connection and one transaction for
  Session lock/create, Resource, ResourceVersion, UploadedFile, Message and Session timestamp. Its
  write helpers share that connection and never commit independently. Existing Session uploads
  lock by exact `session_id + workspace_id`; new Session, Resource and UploadedFile ownership all
  receive the trusted Workspace and Principal.
- Corrected `INSERT_RESOURCE_VERSION_SQL` binding to `version_id, resource_id`; UploadedFile now
  binds both Session and Resource in its initial insert. Repository validation rejects non-UUID
  Resource, Version, File, Session and Message identifiers before opening a transaction.
- Preserved Go upload failure semantics: unsupported extensions remain 400, missing Sessions remain
  404, persistence failures remain generic 500, and parser failures return a complete 200 failure
  DTO with `resource: null`, one persisted system Message and `error_message`.
- File writes use a temporary object and remove it on publication failure. A failed/cancelled
  database transaction removes only a content object created by that request; a pre-existing
  content-addressed object is retained. Cleanup failure raises `UploadCompensationError` carrying
  both the original upload error and cleanup error.
- Added direct `UploadMetadataRepository` tests for SQL parameter order, all fact INSERTs, one
  transaction, rollback at every write step, UUID rejection, parser-failure facts, UploadedFile
  binding and exact Workspace locking. Service/route tests cover frozen DTOs, Principal propagation,
  empty/oversize/extension/parser/persistence failures, cancellation and file cleanup.
- PostgreSQL SQL parsing, UUID/JSON decoding, foreign keys, uniqueness, row locks and actual rollback
  remain `BLOCKED`: this task deliberately opened no database connection and had no authorized
  `_test` PostgreSQL round trip. Database pool, provider and production dependency assembly were not
  started; work is paused after this first audited repair.

## Completed scope

- Added locked `langgraph>=1.2.9,<1.3` dependency and a dedicated `docreview.agent_graph` package.
- Added strict Pydantic `Decision`, `Action`, `Observation`, `Finding`, `Patch` and operation
  schemas. Model JSON rejects duplicate keys, trailing values, unknown fields, illegal actions,
  non-object `tool_input`, invalid hashes, duplicate evidence references and invalid patch shapes.
- Added bounded reference-only `GraphState`: full evidence, document content, model/tool payloads,
  Findings and Patch bodies remain outside the checkpoint as fact/Artifact references.
- Implemented `UnderstandGoal`, `AssembleContext`, `DecideNextAction`, `RetrieveEvidence`,
  `ReadDocumentNodes`, `AnalyzeEvidence`, `GeneratePatch`, `ValidatePatch`, `RequestApproval`,
  `AwaitApproval`, `CommitPatch`, `RenderOutcome`, plus `AwaitUserInput` for restart-safe
  interrupt semantics. Graph nodes only emit typed Runtime commands; they do not call providers,
  tools, repositories, approval stores or committers.
- Added deterministic action validation, closed tool/version mapping, observation hash/no-progress
  tracking, cycle/observation bounds and Runtime-provided step/tool/token/cost/deadline stop checks.
- Added project `ProjectCheckpointer` adapter with `thread_id == run_id` fencing, bounded JSON
  checkpoint/write sizes, safe LangGraph interrupt markers, parent checkpoints and pending writes;
  included an offline in-memory repository with the same contract.
- Added `ProjectRuntimeBoundary` for ModelGateway, ContextAssembler, ToolRuntime, FactRecorder and
  Committer dispatch. Waiting approval/user-input commands are returned to Durable Runtime and are
  never handled as side effects by the graph adapter.
- Added `LangGraphExecutor`: one graph node per durable Step invocation, StepSpec continuation,
  Runtime-owned WAIT_INPUT/WAIT_APPROVAL handling, checkpoint resume and terminal outcome mapping.
- Added 15 Phase 5 tests covering strict rejection, action binding, checkpoint safety, success,
  retrieval observation, no-progress/budget stops, patch validation, approval/commit chain,
  mismatched resume, Runtime boundary rejection, one-node-per-Step executor behavior, Step-result
  replay after a crash window and cross-Step approval checkpoint resume.

- Added typed Run, Step, Attempt, Tool, ContextManifest, Approval and Outbox facts, statuses,
  classified errors, WorkItem, execution commands and immutable lease identity.
- Added active parameterized Psycopg repository methods for Run/Step/Attempt/Manifest,
  Tool audit, Approval, cancellation/recovery, transactional Outbox and Projection receipts.
- Run, initial Step and `agent.run.created` Outbox intent are created in one transaction;
  duplicate request/step/event keys replay only when all canonical facts match.
- Step and Outbox claim use one data-modifying CTE with `FOR UPDATE ... SKIP LOCKED`, stable
  ordering, durable trusted scope, one-running-Step-per-Run and database lease generation.
- Heartbeat, retry, Step outcome, Tool outcome and Outbox publication require exact status,
  owner, generation and unexpired lease. A heartbeat fencing failure propagates immediately.
- RuntimeEngine implements bounded attempt/Step/Run timeouts, classified retries, deterministic
  capped exponential backoff, Run budget checks, cancel-before-execute and stable
  `agent-step:<step_id>` Tool idempotency keys. Invalid executor telemetry is persisted as
  `invalid_input`; exceptions are not downgraded to success.
- Crash recovery closes the current Attempt as `lease_expired`, requeues or fails its Step by
  `max_attempts`, resets the Run, and requeues expired Outbox publication. No in-memory state is
  treated as authoritative; wake/poll state remains only an optimization boundary.
- Tool audit atomically creates/locks/reclaims calls, compares tool/version/Step/input under the
  Run idempotency key, increments generation/attempts and fences terminal audit updates.
- Approval request verifies Run/Step Workspace scope, canonical resource hash and resource
  ownership. External decisions require an active owner/admin user; reject atomically fails
  Run/Step, while approve validates the bound continuation/Patch/write key and creates the
  unique `CommitPatch` Step before waking the Run. Both paths enqueue decision Outbox intent.
- Step outcome/retry commits update Step and optimistic-versioned Run and enqueue an Outbox
  event in the same transaction. Outcome and retry replays compare full canonical payload/hash.
- Projection Worker uses filtered Outbox claims, lease generation fencing, capped retry and
  dead letter. Runtime projection snapshots require durable Run/Turn and exact Step/Approval.
  Turn outcome, messages, ordered events, public projection and a new outcome Outbox intent are
  written atomically; event-ID receipts make replay idempotent after crash gaps.
- No legacy Agent workflow, legacy/shadow Router or fallback path was migrated. The active durable
  write routes and paired Runtime/Projection lifecycle are documented below.

## Phase 6 completed scope

- Implemented all active assistant/approval write surfaces: conversation and session message POST
  routes (stream and non-stream), conversation/session uploads, session DELETE, and approval
  approve/reject. Validation, trusted-ingress scope, request-ID response correlation, DTOs and
  frozen error mappings remain at the handler boundary.
- Added `TurnCoordinator` acceptance/replay over the durable Turn store. Canonical Go-compatible
  input JSON/hash, workspace idempotency scope, duplicate request replay and same-ID changed-body
  conflicts are enforced before persistence access can diverge.
- Added `DurableRunner` and `DurableOnlyPipeline`. Stream and non-stream adapters call the same
  durable pipeline, wait for a deterministic persisted public projection, and never restore a
  legacy/shadow router or fallback path.
- Public projection writes are transactional with the Turn outcome message/event update and
  outcome Outbox intent. Projection receipts are event-ID/hash idempotent; ordered positive
  sequences are read back from persisted Turn events rather than process-local state.
- Added frozen SSE mapping and rendering for turn state, assistant message, waiting, success,
  failure/cancellation and done events. Frames enforce nonnegative IDs, safe event names and
  object payloads; invalid persisted payloads render as `{}` without leaking internal state.
- `Last-Event-ID` replay emits only sequences greater than the reconnect cursor. The frontend
  fixture is generated from the same public frame mapper and frontend tests verify the same
  `X-Request-ID` plus advancing cursor across reconnects.
- SSE observer cancellation detaches the transport only; the durable pipeline task is shielded
  from cancellation, so acceptance/outcome continues and a later same-ID request can replay it.
- Approval decisions retain trusted user authorization and atomically validate the checkpoint
  thread/step, approval identity, Patch validity and target write key before creating the unique
  `CommitPatch` continuation step; reject atomically fails the waiting Run/Step. Repeat decisions
  replay only when the canonical decision matches.
- Runtime and Projection workers now have paired FastAPI lifespan ownership. Startup reclaims a
  sibling when either worker fails fast; shutdown joins both workers and surfaces worker errors.
  Configuration permits workers only when Runtime and Projection are enabled together.
- Upload handlers enforce multipart field, 20 MiB bound, empty-content and configured extension
  policy before invoking the uploader. No compatibility route was added outside the active router.

## Phase 7 completed scope

- Added `docreview.parity` runner version `1.0.0`, a `docreview-parity` CLI, versioned `v1` manifest,
  separate Go/Python captures and deterministic machine result under `dist/parity/v1/result.json`.
- The runner is capture-only: it has no HTTP, provider, Tool or database execution adapters and
  rejects either capture when `real_side_effects_executed=true`. The same request is never executed
  against both implementations.
- Added 12 comparison categories covering routes/status, public JSON DTO, error mappings, SSE
  event/id/data/terminal order, Decision/validated Tool intent, Evidence/citation/node identity,
  Patch/hash, Run/Step/Attempt/Tool/Outbox statuses, Approval continuation, Commit/Outbox facts,
  Workspace isolation and retry/timeout/crash recovery.
- Generated `docs/remediation/parity-report.md`: all 12 fixed-fixture/static-contract scenarios
  match, with zero authorized database snapshots and zero production/canary requests. The report
  explicitly separates contract equality from database, multi-worker, provider, ingress, load and
  production evidence.
- Re-audited `python-active-scope.md` against current Router, server assembly, web callers,
  deployment/CI and source worktree changes. Every active entry is now classified as migrated
  offline or explicitly retained in Go; partial production closures block canary rather than being
  silently treated as complete.
- Added `canary-runbook.md` with the one-request/one-writer invariant, sticky SSE retry, preflight
  gates, cohort control, unconditional abort conditions, fact reconciliation and rollback that
  preserves accepted Runs, approvals, commits, Outbox events, projections and additive schema.
- Aligned Python with two current Go worktree behaviors: assistant uploads now require trusted
  ingress and fail closed without an identity adapter; persisted failed/cancelled Turn events use
  the Go terminal error message while post-200 pipeline exceptions retain the retry message.
- Recorded all unmet database, production assembly, protected-ingress, data-reconciliation,
  capacity, observability, rollback-rehearsal and human-approval gates. No Go code was removed.

## Validation

Run from `G:\gofile\docs_reviewAgent` with the locked Python environment:

- `uv lock --check`: passed; 59 packages resolved from the locked graph.
- `uv run pytest -q`: passed, 239 tests.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed, 128 files formatted.
- `uv run pyright`: passed with 0 errors, 0 warnings and 0 information messages.
- `uv run python -m compileall -q src`: passed.
- `uv run docreview-parity --manifest tests/fixtures/parity/v1/manifest.json --result
  dist/parity/v1/result.json --report docs/remediation/parity-report.md --check`: passed.
- Frontend source oracle (`G:\gofile\Agent_Project\apps\web`, read-only): `npm test -- --run`
  passed, 104 tests; `npm run lint` passed; `npm run build` passed during Phase 6 and was not rerun.
- Source Go handler oracle (read-only, database-free selection): five durable turn/upload tests
  passed with a filtered `go test ./apps/server/internal/server/handlers -run '^(...)$' -count=1`.
- Source Go orchestration oracle: five strict Decision/Action tests passed with a filtered
  `go test ./apps/server/internal/agent/orchestration -run '^(...)$' -count=1`.
- Source Go Patch oracle: three strict parse/versioned-node tests passed with a filtered
  `go test ./apps/server/internal/document/patch -run '^(...)$' -count=1`.

New tests cover success, retry/backoff, timeout scopes, cancellation, invalid telemetry,
stable duplicate-execution Tool keys, stale heartbeat propagation, projection retry/dead-letter,
receipt-gap replay, crash rollback between Run and Step creation, and static SQL assertions for
claim locks, trusted scope, fencing, recovery, Tool reclaim, Approval authorization, Outbox and
Projection receipts. Phase 6 adds handler write-route coverage, same-pipeline stream/non-stream
coverage, Last-Event-ID replay, same-ID idempotency, SSE terminal sequence mapping, malformed
payload normalization, upload policy failures, lifecycle fail-fast/shutdown propagation and
fixture generation from public frames. Phase 7 adds runner safety/diff-path tests, required-category
coverage, generated-report gate coverage, trusted upload failure paths and separate persisted versus
transport SSE terminal errors. The source repository remains read-only and no source file was
intentionally modified, formatted, staged or committed.

The Assistant upload remediation adds direct transaction/repository coverage for UUIDs, Version
parameter order, complete success/failure facts, rollback injection at each write, exact DTOs,
Workspace/Principal ownership, existing-Session isolation and filesystem compensation. These are
database-free tests and do not replace PostgreSQL round-trip evidence.

## Intentionally not performed

No PostgreSQL connection or database integration test was run. The required process-only
`TEST_DATABASE_URL` ending in `_test`, `ALLOW_DB_TESTS=1` and approved test-host allowlist were
not provided together, so Psycopg round trips, SQL decoding, concurrent locks, transaction
rollback and schema compatibility remain unverified. Validation did not read `.env`,
`DATABASE_URL` or any secret and did not fall back to another database. No migration, DDL, backfill, repair,
destructive SQL, external provider call, deployment change, production/public write traffic or
database-backed LangGraph checkpoint validation was performed. No authorized isolated database
snapshot was supplied. No protected production ingress, production dependency assembly, load/soak
test, canary request, traffic switch, rollback rehearsal, commit/push/PR or Go deletion occurred.

## Rollback

This phase made no intentional source-repository, database, deployment or traffic mutation. Roll
back Phase 7 by removing `src/docreview/parity/`, `tests/parity/`, `tests/fixtures/parity/v1/`, the
`docreview-parity` script, generated `dist/parity/v1/result.json`, `parity-report.md` and
`canary-runbook.md`; revert the upload trusted-ingress and persisted-terminal SSE compatibility
changes with their tests; restore the prior active-scope/API/status documents. Leave PostgreSQL,
deployment, traffic, Go source and migrations untouched. Earlier phase rollback scopes remain
independent.

Roll back only this Assistant upload repair by reverting `document/upload.py`,
`storage/postgres/upload_write.py`, the `StoredFile` creation/cleanup additions, trusted Principal
arguments in the upload route/dependency Protocol, and the paired upload/filestore tests. This
rollback changes no schema or stored database data because neither was touched.

## Residual risks

- SQL syntax, UUID/JSON/numeric decoding, uniqueness conflicts, lock ordering, `SKIP LOCKED`
  fairness and cross-implementation canonical hash parity need an authorized `_test` PostgreSQL
  round trip before runtime assembly.
- Multi-worker lease contention, crash after external Tool side effect, long-running heartbeat,
  cancellation races, Projection receipt gaps and dead-letter operations need staging fault tests.
- Approval continuation validation covers the frozen identity/Patch/write-key/input binding, but
  the future typed orchestration layer must apply the full strict StepEnvelope schema again.
- Runtime/Projection lifecycle ownership is implemented at the application boundary, but production
  repository/provider construction, worker capacity tuning, alerting and protected-ingress
  single-writer routing still require an authorized deployment assembly.
- The Checkpointer adapter is offline-validated only; no PostgreSQL checkpoint tables or repository
  wiring were added. Real process-restart recovery, checkpoint retention/pruning and concurrent
  checkpoint claims require an approved `_test` schema contract.
- PostgreSQL Approval continuation SQL and the offline LangGraph adapter are contract-tested for
  checkpoint/Patch/write-key binding; an authorized database round trip is still required before
  production approval resume can be enabled.
- `ProjectRuntimeBoundary` is an offline protocol adapter. Production ModelGateway, Evidence,
  ToolRuntime, Artifact, Approval and Committer implementations remain unassembled by design.
- Fixed/static parity PASS does not prove the current Python implementation emits the complete rich
  Go EvidenceSet provenance DTO through a production provider boundary; that closure remains an
  explicit Go-retained blocker in `python-active-scope.md`.
- Assistant upload ownership, DTO and Resource/Version/File/Session/Message transaction closure are
  implemented and database-free tested, but SQL execution, UUID/JSON decoding, FK/unique behavior,
  row locking and rollback still require an authorized `_test` PostgreSQL round trip. Content-file
  compensation is also not validated across multiple processes sharing the same storage root.
- The source worktree is dirty and may change again; any later canary review must regenerate the Go
  capture from a newly recorded source revision before approval.

## Next task, blocked pending confirmation

Phase 7 offline parity and canary preparation are complete. Production canary remains blocked by
every `blocked` gate in `parity-report.md`, especially the authorized `_test` database round trip,
production dependency assembly, protected ingress, reconciliation, capacity/alerts and rollback
rehearsal. After those are green, a human must approve the exact canary cohort and window. Until
then Go remains the production single writer and PostgreSQL durable facts remain authoritative.

The first post-audit repair, Assistant upload persistence, is complete at the database-free level.
Per task scope, work is paused here and does not proceed to database pool, provider or production
assembly work without a new explicit instruction.

## Gate decision

The requested Phase 7 versioned reconciliation runner, report, active-scope audit and single-write
canary/rollback preparation are complete with database-free evidence. The gate decision is
`blocked_pending_prerequisites_and_manual_approval`. Work is intentionally paused before any
production canary and awaits human approval only after all prerequisite evidence is supplied.
