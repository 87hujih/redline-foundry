# Python Entry Single-Write Canary and Rollback Runbook

## Status and authority

This runbook is preparation only. It does not authorize production traffic. The current production
writer remains Go until every gate in `parity-report.md` is green and a human change owner approves
the exact release, cohort, thresholds, window and rollback commander.

The canary invariant is **one request, one implementation, one writer**. The ingress must never
mirror a write request to both Go and Python. A failure after Python acceptance must be retried with
the same request ID against Python; it must not fall through to Go and create a second Turn, Run,
Tool call, Approval, Commit or Outbox fact.

## Required inputs

The change record must bind all of the following before the window opens:

- immutable Go and Python image digests and the exact additive migration ledger/checksums;
- an authorized isolated database validation report and production backup/restore reference;
- the protected ingress configuration revision and adversarial header-stripping test result;
- an exact Workspace, Resource, principal/role and request cohort allowlist;
- approved request/concurrency limits, SLOs, abort thresholds and observation duration;
- dashboard/alert links, on-call owner, database owner and rollback commander;
- the previous verified release digest and a completed staging rollback rehearsal;
- a retained reconciliation query/report definition for Turn, Run, Step, Attempt, Tool, Approval,
  Commit, Outbox, Projection and SSE sequence facts.

Missing or ambiguous input keeps Python weight at zero.

## Pre-canary gate

1. Verify migrations 016-024 and Python SQL on an authorized `_test` database through the database
   fuse. Include transaction rollback, uniqueness, concurrent claim, stale lease generation,
   Approval continuation, Commit idempotency, Outbox receipt and crash-recovery cases.
2. Reconcile the exact canary Workspace/Resource cohort: ownership, canonical current version,
   node IDs/hashes, retrieval profile, pending Runs/Approvals, commits, Outbox and projections.
3. Start Python with production dependencies wired fail closed. Confirm Runtime and Projection
   workers recover leases and that a worker failure fails the service health/readiness boundary.
4. Validate ingress stripping and signing using forged, missing, expired, cross-path,
   cross-request-ID and cross-Workspace headers. Browser traffic must never supply trusted headers.
5. Run database-free parity `v1`, then run the authorized database/snapshot parity capture. Both
   artifacts must be retained with their SHA-256 digests and contain no secrets.
6. Confirm capacity headroom for database pool, worker leases, provider/tool rate limits, SSE
   connections and Outbox/Projection drain at the approved canary ceiling.
7. Confirm dashboards are quiet and no pre-existing dead letter, expired lease, projection lag,
   profile mismatch or unresolved approval conflict contaminates the cohort.
8. Obtain explicit human approval. Do not infer approval from a successful build or an empty queue.

## Ingress single-write procedure

1. Keep default routing at Go. Configure one explicit canary allowlist whose entries include the
   exact Workspace and Resource and, where required, principal and request class.
2. Strip all inbound `X-DocReview-*` identity headers. Assign or preserve the stable
   `X-Request-ID`, select one upstream, then sign method/path/request/workspace/principal facts.
3. Record the selected implementation in protected ingress telemetry without exposing the HMAC
   secret. The selection must remain sticky for the request ID and every SSE reconnect.
4. Send a canary request only to Python. Do not send a shadow write to Go. Read-only offline
   comparison may use sanitized persisted captures after the request completes.
5. Reconnect SSE with the same body, request ID and `Last-Event-ID`; confirm strictly advancing
   persisted sequences and the expected terminal order.
6. For an approval case, make the external decision through the signed Python approval route and
   verify the exact bound `CommitPatch` continuation. Never decide the same approval in Go.
7. For a commit case, retry only with the same Python request/tool/commit keys. Verify one canonical
   version, one commit fact and one immutable Outbox event/receipt chain.
8. Reconcile every canary request before admitting another cohort increment. Expansion requires a
   new recorded approval and must remain below the separately validated capacity ceiling.

## Observation and abort conditions

Use the approved numeric thresholds from the capacity gate. The following are unconditional aborts
even when aggregate latency/error thresholds remain green:

- any cross-Workspace read/write, scope override or unsigned trusted identity acceptance;
- duplicate Turn, Run, Tool, Approval continuation, Commit, Outbox event or projection receipt;
- a changed request body accepted under an existing request ID or changed facts under another
  idempotency key;
- lost, duplicated or reordered persisted SSE IDs/terminal frames;
- stale lease generation successfully heartbeating, completing or publishing;
- an Approval decision that resumes the wrong Run/Step/Patch/write key or opposite decisions that
  do not conflict;
- a Patch applied to the wrong base version/node hash or an unauthorized evidence/node reference;
- Outbox dead letter, unexplained projection gap, unknown durable status or unrecoverable worker
  crash;
- public route/status/DTO/error behavior outside the approved parity artifact;
- reaching any approved queue age, error rate, latency, pool, provider, SSE, cost or drain threshold.

## Rollback procedure

1. Set Python selection to zero for **new request IDs** at the protected ingress. Do not route an
   already accepted Python request or reconnect to Go.
2. Preserve sticky Python routing for accepted request IDs while Runtime and Projection workers
   drain their durable Runs, Approvals, Commits and Outbox events. If containment requires blocking
   user traffic, keep workers running unless a database owner determines continued execution is
   unsafe.
3. Stop cohort expansion and new Python approval decisions. Handle already pending approvals under
   the incident/change decision; never recreate them or decide them through Go.
4. Capture read-only diagnostics and reconcile Turn/Run/Step/Attempt/Tool/Approval/Commit/Outbox/
   Projection/SSE facts. Use the same durable IDs and idempotency keys for any authorized recovery.
5. If the Python release itself must be removed, deploy the previous verified release only after
   confirming how accepted Python facts will drain or be reclaimed. Older code must not overwrite
   newer lease generations or misinterpret durable facts.
6. Leave additive migrations and every durable/audit fact in place. Do not drop schema, delete rows,
   reset leases, rewrite statuses, invent keys, reverse commits or mark approvals manually.
7. Verify Go is the sole writer for new requests, Python backlog is zero or explicitly contained,
   public projections are reconciled and alerts have returned below approved thresholds.
8. Retain the ingress change, diagnostics, reconciliation output, incident timeline and decision on
   whether to fix forward or schedule another canary.

## Rollback limits

Changing an application mode to `legacy` is not a rollback path. Executing the same request in Go
after Python may have committed a side effect is prohibited. Schema/data contraction, Go code
deletion and legacy removal remain separate reviews after sustained production evidence.
