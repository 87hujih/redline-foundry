# 持久化契约冻结

**证据时间：** 2026-08-12。Python 必须保留当前 PostgreSQL 表、约束、参数化 SQL 的可观察行为。Phase 0 不连接数据库、不运行 migration/DDL/backfill、不改写 SQL。迁移文件是只读事实来源；未来 Schema 变更必须另行批准并遵守 expand → dual write/backfill → verify → switch read → contract。

## 持久化聚合

```text
Workspace/Principal
  -> Turn -> Run -> Step -> Attempt -> Tool Call / Observation / Manifest
  -> Approval -> Commit -> Outbox -> Projection/Receipt
```

Run/Step/Attempt/Tool/Approval/Commit/Outbox/Projection 都是事实。LangGraph state/checkpoint、
内存唤醒 channel、SSE 连接和 UI state 都是可重建 projection，不能成为另一事实源。

## 事实与不可变行为

| 事实 | 当前表/repository | 状态/身份契约 | Python 不变量 |
| --- | --- | --- | --- |
| Run | `agent_runs`; `agentrun.Repository`, `EngineStore` | status `queued`、`running`、`waiting_input`、`waiting_approval`、`succeeded`、`failed`、`cancelled`；`(workspace_id, request_id)` 唯一；legacy/null workspace 使用 partial global request unique index；正预算/version | 保留 status 校验、current step、deadline/cancel、state JSON object、optimistic version 与精确 workspace/request 查询。 |
| Step | `agent_steps` | `(run_id, step_key)` 唯一；类型化 input/output/error object；queued claim；running owner/expiry/heartbeat/generation；attempt/retry；completed 仅为 terminal | 用 DB lock claim 一个可运行 Step；同 key 下 type/input/retry policy 变化即 conflict。 |
| Attempt | `agent_attempts` | `(step_id, attempt_number)` 唯一；provider/model/prompt/temp/context/trace/usage/cost/latency/retry/finish/error telemetry | 持久化 start 与 terminal outcome；counter 非负；遗弃 lease 关闭为 `lease_expired`。 |
| Tool | `tool_calls`; `agenttools.Runtime`, `agentrun.ToolAuditStore` | status `pending/running/succeeded/failed/cancelled`；设置时 `(run_id,idempotency_key)` 唯一；tool/version/input/output/error/provenance/audit | 每个 model/tool call 都经过 versioned Registry + schema + Policy + rate limit + audit；同 key 不同事实产生 conflict。 |
| Observation | `agent_observations` | `(run_id, observation_key)` 唯一；payload object、hash、`novel`；可选 Tool call | 持久化保存完整有界结果；GraphState 只保留 reference；重复 hash 增加 no-progress。 |
| Context | `context_manifests` | 不可变有序 item、tokenizer、total/reserved/budget 与 hash；按 `(run_id,step_id)` 查询 | 按 ID 重新加载准确 manifest；绝不从当前 retrieval/document 重建。 |
| Approval | `agent_tool_approvals`; `agentpolicy.ApprovalStore` | pending/approved/rejected/cancelled；绑定 workspace/run/request-step/tool/version/write key/resource hash；owner/admin external decision | Request 只创建 pending。Approve 原子创建准确 CommitPatch continuation 并排队 Run；reject 以 `policy_blocked` terminal failure；相同 decision replay，相反 decision conflict。 |
| Commit | `document_patch_commits`; `documentcommit.Committer` + PostgreSQL adapter | `(workspace_id,idempotency_key)` 唯一；Patch hash/base/new version/outbox/actor binding | Serializable lock/recheck，校验 AST/expected hash/scope/evidence，原子插入完整 version bundle + 一个 outbox event；相同 key/hash 返回既有 ID。 |
| Outbox | `outbox_events`; `outbox.Repository`, `ProjectionWorker` | `pending/publishing/published/dead_letter`；aggregate/idempotency 唯一；claim owner/expiry/generation；有界 retry | Event intent 在 writer transaction 中插入；publication 可 retry 且受 lease fencing；不手工替换 key。 |
| Projection | `agent_turn_public_projections`, `outbox_projection_receipts`; `RuntimeProjector` | 仅 terminal/waiting public state；DTO/content hash/last event sequence；`(event_id,projection_name)` receipt 唯一 | Projection 读取事实、保留 sequence、幂等写 receipt，绝不暴露 raw state/manifest/tool payload。 |

## 状态机

### Turn

`accepted -> running -> waiting_input|waiting_approval|succeeded|failed|cancelled`；waiting state 可恢复到
`running` 或 terminal，terminal state 不再迁移。`agent_turn_outcomes` 允许 running、waiting、terminal
outcome record，写入前由 `turn.CanTransition` 校验 transition。

### Run 与 Step

`queued -> running`；running 可用确定性 backoff retry 到 queued，可等待 input/approval、succeed、fail 或 cancel。
过期 running lease 在仍有 attempt 时 requeue，否则 fail。Waiting approval 只能由绑定的 external decision
transaction 恢复。Cancellation 幂等并唤醒 waiting Step。Run/Step status constraint 与完成时间由数据库强制。

### Tool 与 Outbox

Tool：`pending -> running -> succeeded|failed|cancelled`；过期 running Tool call 可按 generation reclaim。
Outbox：`pending -> publishing -> published|dead_letter`；过期 publishing 返回 pending。只有声明的可重试
类别（`rate_limited`、`timeout`、`retryable_upstream`，以及 engine boundary 的 lease expiry）可以 retry。

## 事务边界

1. **Turn acceptance (`agentturn.Repository.Accept`)**：计算规范 input JSON/hash 并查幂等；一个事务创建/复用
   session、user message、`agent_turn`、关联 `agent_run`、初始 `UnderstandGoal` Step、有序 Turn event 与
   `agent.turn.accepted` Outbox。
2. **Turn outcome (`CommitOutcome`)**：插入幂等 outcome fact；锁 session/Turn；校验 transition；追加
   Assistant/system message 与有序 event；更新 Turn/public projection；插入 `agent.turn.outcome_committed`
   Outbox；作为一个单元 commit 或 rollback。
3. **Step outcome/retry**：Psycopg transaction 承载带 lease fencing 的 attempt/Step/Run update 与确定性
   Outbox insertion。Heartbeat/completion 要求精确 owner、generation 与未过期 lease。
4. **Approval decision**：已认证 owner/admin decision 锁定 Approval/Run/Step，原子地创建唯一 CommitPatch
   Step 与 Approval Outbox，或写入 rejected terminal state 与 rejection Outbox。
5. **Canonical commit**：Serializable transaction 锁定 workspace/idempotency、resource/current version，
   重新检查 base version 与每个 expected node hash，写入 `resource_versions`、规范 document/node/source mapping、
   派生 section/chunk/profile metadata、`document_patch_commits` 与 `document.version.committed` Outbox。
6. **Projection publication**：claim/publish/receipt update 与原始 fact transaction 分离；receipt 与 event identity
   使 replay 幂等。

## Claim、lease generation 与过期 worker fencing

Step/Outbox claim 使用一条带 `FOR UPDATE SKIP LOCKED` 的 SQL，设置 owner、expiry、heartbeat/attempt 并递增
`lease_generation`。Tool call recovery 使用相同 owner/expiry/generation 模式。Heartbeat、retry、completion、
Approval resume、publication 都要求：

```text
status 为预期的 running/publishing 状态
AND claimed_by == worker_id
AND lease_generation == claimed_generation
AND lease_expires_at > now()
```

过期 worker 不能覆盖更新的 claimant。启动/周期恢复会将遗弃 Attempt 关闭为 `lease_expired`，
按 `max_attempts` 重新排队或失败 Step，并将过期的 Outbox 发布退回 `pending`。

## 幂等 key

- Turn acceptance：`(idempotency_scope, request_id)`，scope 可以是 workspace、organization、session 或 global compatibility scope；acceptance 后 scope 不可变。
- Run creation：`(workspace_id, request_id)`；null workspace 使用独立的 partial global unique index。
- Step：`(run_id, step_key)`。
- Attempt：`(step_id, attempt_number)`。
- Tool：`(run_id, idempotency_key)`。
- Approval request：`(workspace_id, run_id, idempotency_key)`；target write key 刻意属于不同 domain。
- Patch commit：`(workspace_id, idempotency_key)`。
- Outbox：`(aggregate_type, aggregate_id, idempotency_key)`。
- Projection receipt：`(event_id, projection_name)`。
- Operator action：`(workspace_id, request_id)`。

相同 replay 返回已存储的事实/结果。在既有 key 下，任何规范 input、output、tool/version/input、
Patch、approval decision 或 event payload 的变化都会返回 conflict。数据库唯一性仍是并发保护。

## Workspace、Resource 与 Principal 隔离

Trusted ingress 提供 Principal 与准确 Workspace；request payload/model output 不能提供或覆盖它们。Query handler
要求签名 user identity。Policy 解析 active membership/role 与 Resource ownership。每个 Runtime repository query
及两个 retrieval channel 都限制 workspace、resource 与准确 resolved version。Committer 在事务内重新检查
Workspace/Resource/current version/node authorization。跨 Workspace target 只显示 not-found/denied，绝不显示
其他租户数据。历史可空 workspace column 与 profile 前 embedding 是兼容事实；Python 不得臆造 backfill 或
静默扩大 scope。

## 当前相关表与 migration

| Migration | 与当前闭包相关的表/列 | Phase 0 状态 |
| --- | --- | --- |
| 001-005, 007-015 | `resources`、`resource_versions`、`resource_chunks`；`assistant_sessions`、`assistant_messages`；`uploaded_files`；grounded structure/section/chunk；session context/runtime projection | 既有兼容事实；当前 resource/Assistant/upload 路径使用的读写行为必须保留。 |
| 006/008-010 | 历史 `tasks`、`approvals`、`execution_jobs`、notification 与 task suggestion 幂等 | 当前 FastAPI 应用未注册；不属于在线闭包。 |
| 016 | `users`、`organizations`、`workspaces`、`memberships`、`principal_audit_events`；既有表中可空的 workspace 列 | 仅扩展的 identity/tenancy 事实；Phase 0 不执行数据库操作或 backfill。 |
| 017 | `agent_runs`、`agent_steps`、`context_manifests`、`agent_attempts`、`tool_calls`、`outbox_events` | 持久化 Runtime 的事实源；SQL 已冻结。 |
| 018 | `agent_turns`、`agent_turn_events`、`agent_turn_outcomes`；message/run 上的 Turn/Outcome 关联 | Turn request/outcome 与 SSE replay 事实；SQL 已冻结。 |
| 019 | Tool lease；`agent_artifacts`、`agent_tool_approvals`、rate-limit bucket | Tool/Approval/artifact 控制；SQL 已冻结。 |
| 020 | `agent_observations`、`agent_shadow_comparisons` | Observation/evaluation 事实；shadow 不是当前 request 路径。 |
| 021-022 | Canonical AST/node/source mapping/Patch commit；retrieval profile/embedding metadata/index | 未来 typed backend 所需；Phase 0 不执行 migration/backfill/流量切换。 |
| 023 | Turn/Run scope 列、公开 projection、projection receipt、cutover comparison | 当前持久化 projection 契约；仅追加。 |
| 024 | `agent_runtime_operator_actions` | 运维审计事实；Phase 0 不执行数据库变更。 |

契约证据位于 `src/docreview/storage/postgres/` 的参数化 SQL、repository adapter，以及 `tests/storage/`
的 SQL/事务测试。准确 SQL statement 与 constraint 是持久化契约；实现必须保留顺序、NULL 行为、
`ON CONFLICT`、lock、partial index、cascade/set-null action 与 error classification。

## Python 不得改变的 SQL 行为

- 不得用改变 lock/order 语义的进程内 queue 或 ORM polling 替换 `FOR UPDATE SKIP LOCKED` claim。
- 不得从 heartbeat/completion/retry/publication update 移除 owner、lease expiry、heartbeat 或 generation predicate。
- 不得放宽 query 后在内存中过滤 Workspace/Resource；scope predicate 必须位于每个 SQL read/write boundary。
- 不得改变 unique/idempotency conflict 行为、canonical JSON/hash 计算、sequence order 或 `Last-Event-ID` replay。
- 未经数据库变更审批，不得将可空历史 workspace/profile 字段改为非空，不得改写既有 migration file，
  也不得删除历史事实。
- 除非既有契约明确要求，不得把 projection、embedding、notification 或 external provider I/O 放入 acceptance 事务；
  Outbox 仍是事务性交接点。

## 后续阶段的数据库安全

Phase 0 不允许数据库连接。后续测试只有通过共享 test fuse 才能连接：`ALLOW_DB_TESTS=1`、
仅进程环境的 `TEST_DATABASE_URL`、以 `_test` 结尾的数据库名以及准确的批准 host allowlist。
生产 `DATABASE_URL` 和 `.env` 绝不能作为测试 fallback。任一条件缺失都必须记录 skip，不能尝试连接。
