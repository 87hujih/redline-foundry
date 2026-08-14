# Python + LangGraph Rewrite Governance

## Scope

This repository is the Python + LangGraph rewrite target for `G:\gofile\Agent_Project`.
The Go repository is read-only source evidence. All new Python code and governance
documents belong in this repository only.

The active scope is defined by the production `apps/server/cmd/server` assembly,
the routes registered by the current Router, API calls that are actually reachable
from `apps/web`, deployment/CI invocation evidence, and the durable request closure
required to complete a request. Do not translate a Go package or directory merely
because it exists.

## Source safety

- Never modify, format, delete, stage, commit, or submit changes in
  `G:\gofile\Agent_Project`.
- Never read, print, copy, or infer values from `.env` files, API keys, tokens, or
  database passwords. Configuration names may be documented; secret values may not.
- Do not connect to PostgreSQL. Do not run migrations, DDL, backfills, reindexing,
  repair, replay, destructive SQL, or commands that could perform them.
- Preserve the source worktree exactly, including unrelated user changes.

## Compatibility

The Python service must preserve the current HTTP methods and paths, DTO shapes,
error status/DTO behavior, SSE event names and replay semantics, `X-Request-ID`,
`Last-Event-ID`, workspace/resource/principal isolation, database idempotency keys,
lease-generation fencing, and transactional boundaries documented under
`docs/remediation/`.

LangGraph is an orchestration implementation detail. It does not replace durable
Run/Step/Attempt/Tool/Approval/Commit/Outbox/Projection facts or their PostgreSQL
transactions.

## Delivery flow

1. Freeze scope and contracts before implementing Python business logic.
2. Add failure-path and compatibility tests before changing behavior where practical.
3. Use the existing SQL and repository behavior as the compatibility oracle; do not
   rewrite migration SQL without explicit approval.
4. Verify with database-free tests and static checks. Any skipped database check must
   state that the no-connection rule or test fuse prevented it.
5. Keep every phase independently reviewable. Do not begin the next phase without
   explicit user confirmation.

## Phase gate

Phase 0 produces only governance and contract documents. It must stop before Python
business implementation, database access, migration work, deployment changes, or Go
source edits.
