# Go/Python Parity Report

**Evidence date:** 2026-08-14  
**Runner:** `1.0.0`  
**Fixture:** `v1`  
**Go capture SHA-256:** `8f8d1eb50d40d7b9a96f0a2b16ac98cb9d5a3b70346a67b3bbf9d8ddf242d31a`  
**Python capture SHA-256:** `8eaa59695768dd4f22c340c206f6e254c9fa189dbc495cd96e44136da9faf97a`

## Safety boundary

The runner is capture-only. It does not send HTTP requests, call providers or tools, open a database connection, run migrations, or execute commit/outbox side effects. Both captures declare `real_side_effects_executed=false`; the runner rejects a capture that declares otherwise. No request is executed against both implementations.

## Summary

- Scenarios: 12
- Passed: 12
- Failed: 0
- Authorized database snapshot captures: 0
- Production/canary requests: 0

| Category | Passed | Result |
| --- | --- | --- |
| `approval_continuation` | 1/1 | PASS |
| `commit_outbox` | 1/1 | PASS |
| `decision_tool_intent` | 1/1 | PASS |
| `error_codes` | 1/1 | PASS |
| `evidence_citation_node_id` | 1/1 | PASS |
| `json_dto` | 1/1 | PASS |
| `patch_hash` | 1/1 | PASS |
| `retry_timeout_crash_recovery` | 1/1 | PASS |
| `routing_http_status` | 1/1 | PASS |
| `run_step_attempt_status` | 1/1 | PASS |
| `sse_sequence` | 1/1 | PASS |
| `workspace_isolation` | 1/1 | PASS |

## Scenario results

| 场景 | 分类 | 证据级别 | 结果 |
| --- | --- | --- | --- |
| `http.route-table-and-status` | `routing_http_status` | `fixed_fixture` | PASS |
| `json.public-dto` | `json_dto` | `fixed_fixture` | PASS |
| `errors.public-mapping` | `error_codes` | `static_contract` | PASS |
| `sse.persisted-and-terminal-order` | `sse_sequence` | `fixed_fixture` | PASS |
| `orchestration.decision-tool-intent` | `decision_tool_intent` | `fixed_fixture` | PASS |
| `evidence.identity-citation-node` | `evidence_citation_node_id` | `static_contract` | PASS |
| `document.patch-and-hash` | `patch_hash` | `fixed_fixture` | PASS |
| `runtime.fact-statuses` | `run_step_attempt_status` | `static_contract` | PASS |
| `approval.bound-continuation` | `approval_continuation` | `static_contract` | PASS |
| `commit.transactional-outbox` | `commit_outbox` | `static_contract` | PASS |
| `tenancy.workspace-isolation` | `workspace_isolation` | `static_contract` | PASS |
| `recovery.retry-timeout-crash` | `retry_timeout_crash_recovery` | `static_contract` | PASS |

## Differences

- 固定 fixture 与静态契约捕获没有差异。

A PASS means the versioned fixed fixture or static contract capture is equal. It is not database round-trip, multi-worker, provider, ingress, load, or production evidence.

## Unmet release gates

| Gate | 领域 | 状态 | 要求 | 尚缺证据 |
| --- | --- | --- | --- | --- |
| `database-roundtrip` | database | `blocked` | Run migrations 016-024 and all parity transaction/lease/idempotency cases only on an authorized isolated database ending in _test. | No ALLOW_DB_TESTS=1, TEST_DATABASE_URL, approved host allowlist, snapshot provenance, concurrency run, or checkpoint round trip was supplied. |
| `protected-ingress` | ingress | `blocked` | A production ingress must strip caller identity headers, assign/sign request identity, enforce TLS/authn, exact workspace scope, clock skew, body/stream limits, and single-writer routing. | Only a development loopback proxy exists in the source worktree; no production ingress config or adversarial verification was authorized. |
| `production-assembly` | canary | `blocked` | Wire the Python pool, repositories, ModelGateway, ContextAssembler, ToolRuntime, Committer, checkpointer, Runtime worker and Projection worker with fail-closed startup. | The Python contracts and offline adapters exist, but the production dependency assembly and upload ownership/atomic graph write closure are incomplete. |
| `canary-data-reconciliation` | canary | `blocked` | Reconcile workspace/organization ownership, current canonical versions, node hashes, retrieval profiles, pending approvals, runs, commits, outbox and projections for the canary cohort. | No authorized database snapshot or retained reconciliation artifact was provided. |
| `canary-capacity` | capacity | `blocked` | Prove queue age, lease heartbeat margin, worker concurrency, SSE connection budget, provider/tool limits, database pool headroom and outbox/projection drain under staged load. | No load profile, SLO, replica count, pool sizing, rate-limit budget, soak run, or saturation rollback threshold exists. |
| `observability-alerting` | canary | `blocked` | Dashboards and alerts must cover Run/Step/Attempt errors, lease recovery, approval age, outbox lag/dead letters, projection lag, retrieval degradation, patch conflicts and cross-workspace denials. | No deployed metrics/alerts or on-call acknowledgement artifact was provided. |
| `rollback-rehearsal` | rollback | `blocked` | Rehearse ingress removal, accepted-run drain, outbox/projection reconciliation, previous-release deployment and forward recovery without deleting durable facts. | No staging rehearsal proves the previous release can coexist with additive schema and drained Python facts. |
| `manual-canary-approval` | canary | `blocked` | A human change owner must approve the exact cohort, window, thresholds, rollback commander and release artifact after every prerequisite is green. | Approval is intentionally pending; this phase stops before production traffic. |

## Canary verdict

`blocked_pending_prerequisites_and_manual_approval`. 当前不得执行生产 canary; 完成全部阻塞 gate 后, 仍需人工批准入口单写 canary。

The operational sequence and rollback steps are in `docs/remediation/canary-runbook.md`.
