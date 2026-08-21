# 跨 Session 上传资源选择：Phase 0 契约冻结

**状态：** `implemented_and_verified_2026-08-19`

**证据日期：** 2026-08-19

**适用范围：** 当前仓库实际装配的 Python + FastAPI + LangGraph 服务

本文保留最初 Phase 0 冻结的范围、接口、数据模型、事务边界、错误状态、兼容性和实施顺序，
作为后续实现的审计依据。用户确认后，Phase 1-4 已按本文约束完成。

## 1. 当前证据与需要修复的缺口

当前实现已经提供本功能所需的大部分持久化事实：

- `POST /api/assistant/conversations/files` 和
  `POST /api/assistant/sessions/{session_id}/files` 在解析成功时，于同一上传事务中创建
  Workspace 级 `resources`、首个 `resource_versions`、`uploaded_files` 和 Session message。
- `GET /api/resources` 按 `compatibility_scope.workspace_id` 返回 Resource summary，包含客户端可用于
  识别上传资源的 `source_type`。
- 四个消息接口继续接受单个 `resource_id`。`TurnRequest`、`agent_turns`、`agent_runs` 及 Run
  `state_json` 已保存该单值，ToolRuntime 再以 Run 的 Workspace/Resource 快照做权限绑定。
- `assistant_sessions` 当前没有“当前选择 Resource”的持久化字段；Session DTO 也不包含该字段。
- 当前 Turn acceptance 对已有 Session 的锁只按 Session ID 查询，且在创建 Turn/Run 前没有显式地把
  TrustedIdentity Workspace、Session Workspace 和 Resource Workspace 作为同一 SQL 授权条件验证。

因此目标不是引入新的检索模型，而是增加一个可变的 Session 当前选择，并补齐 acceptance 边界的
Workspace 一致性验证。可变选择不能成为已创建 Run 的授权来源。

## 2. 范围冻结

### 2.1 本功能包含

1. 上传解析成功后仍创建且只创建一个 Workspace 级 Resource；成功上传将该 Resource 设为上传所在
   Session 的当前选择。
2. 客户端复用 `GET /api/resources`，并以 `source_type == "upload"` 在客户端形成可选择的上传文档列表。
3. 新增独立接口读取、设置一个 Session 的当前上传 Resource；不改变现有 Session DTO。
4. 设置选择时，在数据库事务内验证 TrustedIdentity、Session、Resource 的 `workspace_id` 完全相同。
5. 消息接口继续使用请求体中显式的单个 `resource_id`。Turn acceptance 在事务内验证其 Workspace，
   并把准确值快照到 Turn/Run；不从 Session 当前选择动态回填或晚绑定。
6. 同一 Session 可切换到另一个上传 Resource；切换只影响后续客户端选择和后续以新 ID 发送的消息，
   不更新任何既有 Turn、Run、Step、Tool、Approval、Commit、Outbox、Projection 或 checkpoint。

### 2.2 明确不包含

- 多 Resource 请求 DTO、数组形式的 `resource_ids`、跨文档检索、合并 rerank 或多文档 Context。
- 修改现有 HTTP 方法/路径、现有成功 DTO、现有错误 envelope、SSE event、`Last-Event-ID` 或重放语义。
- 将 Session 当前选择作为授权事实，或替代 Turn/Run 的 Workspace/Resource/Principal 快照。
- 修改 ToolRuntime policy、Approval binding、Commit fencing、审计事实、幂等 key、lease generation 或
  LangGraph state/checkpoint 所有权。
- 为未成功解析、没有 Resource 的上传记录提供选择能力。
- 新增前端。本仓库没有已装配前端，本功能只交付后端接口与客户端接入契约。
- backfill 历史 Session、重写既有 migration、数据库访问、部署变更或外部流量操作。

## 3. HTTP 接口冻结

### 3.1 保持不变的接口

- `GET /api/resources` 的方法、路径、query、成功 DTO 和既有错误语义不变。客户端从返回的
  `resources` 中筛选 `source_type == "upload"`。列表结果只是候选展示，不是授权凭据。
- 所有 conversation/session message 与 stream 路径保持不变，请求 DTO 仍为
  `{ "message": nonblank, "resource_id": UUID? }`。持久化 acceptance 仍要求非空 Resource。
- 两个上传路径及其成功/错误 DTO 保持不变。
- `GET /api/assistant/sessions` 和 `GET /api/assistant/sessions/{session_id}` 的 Session DTO 不增加字段。

现有 `GET /api/resources` 使用固定 compatibility Workspace，而不是 TrustedIdentity。为保持兼容，本功能
不改变该路由的认证和 Workspace 解析。新选择写入和 Turn acceptance 必须按签名 Workspace 重新查询，
绝不能把列表返回视为授权。部署方必须保证用于该客户端流程的 compatibility Workspace 与 trusted
ingress Workspace 一致；不一致时，候选会在选择接口以不泄露存在性的 `404` 被拒绝。

### 3.2 新增接口

| 方法 | 路径 | 输入 | 成功响应 |
| --- | --- | --- | --- |
| `GET` | `/api/assistant/sessions/{session_id}/resource-selection` | 已签名 TrustedIdentity；Session UUID | `200 {"resource_id": UUID|null}` |
| `PUT` | `/api/assistant/sessions/{session_id}/resource-selection` | 已签名 TrustedIdentity；Session UUID；JSON `{"resource_id": UUID}` | `200 {"resource_id": UUID}` |

两个接口都使用现有 trusted-ingress header、签名 tuple、Request ID middleware 和
`{ "error": "..." }` 错误 envelope。HMAC 必须覆盖准确 HTTP method、准确新路径和最终
`X-Request-ID`。

`GET` 对存在但从未选择 Resource 的 Session 返回 `200 {"resource_id": null}`。它不复制 Resource
summary；客户端以 ID 关联 `GET /api/resources` 的结果。

`PUT` 只接受 Workspace 内 `source_type = 'upload'` 的 Resource。缺少、`null`、非字符串或非 UUID 的
`resource_id` 均为输入错误。接口本阶段不提供清空选择的 `DELETE` 或 `null` 写入语义。

重复 `PUT` 同一个 `(workspace_id, session_id, resource_id)` 必须幂等：返回相同 `200` DTO，不创建
消息、Turn、Run、Outbox 或审计替代事实，也不再次改变选择时间或 Session `updated_at`。并发切换由
Session row lock 串行化，最终选择为最后完成的不同值写入。

## 4. 消息与选择的优先级

消息请求体中的 `resource_id` 是该次 Turn 的唯一权威 Resource：

1. 服务不在 `resource_id` 缺失时读取 Session 选择作为 fallback。这样保持现有缺失字段错误、canonical
   input/hash、Request ID 幂等和 SSE replay 行为。
2. 服务不要求消息体 Resource 必须等于 Session 当前选择。旧客户端可继续只发送明确 Resource，而无需
   先调用新接口；消息请求也不会隐式改写 Session 当前选择。
3. 新客户端先 `PUT` 选择，再在每次 message/stream 请求中发送同一个 `resource_id`。切换时先成功
   `PUT` 新值，再以新值发送后续消息。
4. acceptance 以消息体值创建不可变快照。之后的 `PUT` 只改 Session 当前选择，不查询或更新既有 Run。

这一优先级是兼容性核心：Session 选择是可恢复的客户端状态，Turn/Run 快照才是 Runtime、Tool、Approval
与审计链的持久化授权上下文。

## 5. 数据模型冻结

用户确认后，只允许新增一份 append-only migration；不得修改仓库外的历史 migration 或现有 migration
SQL。仓库当前不包含历史 migration 文件，文档记录的最新 schema owner 序号为 024，因此拟议 artifact
名为 `025_assistant_session_resource_selection.sql`；在 schema owner 指定正式目录/序号时，只可调整文件
位置和序号，不可改变下列语义。

### 5.1 新增列与约束

在 `assistant_sessions` 上追加：

- `selected_resource_id uuid NULL`
- `resource_selected_at timestamptz NULL`

追加完整 Workspace FK 所需的唯一键和约束：

- `resources (workspace_id, id)` 的 named `UNIQUE` 约束；现有全局 Resource PK 保持不变。
- `assistant_sessions (workspace_id, selected_resource_id)` 到
  `resources (workspace_id, id)` 的 named composite FK，`ON UPDATE RESTRICT ON DELETE RESTRICT`。
- named `CHECK`：`selected_resource_id` 与 `resource_selected_at` 必须同时为 `NULL` 或同时非 `NULL`。

不 backfill。所有历史 Session 的两个新列为 `NULL`，旧代码可忽略新增 nullable 列。`source_type='upload'`
资格由 repository 的参数化 SQL 在写入时验证；FK 只负责不可绕过的 Workspace/Resource 引用完整性。

当前选择是可变 Session 状态，不记录到 LangGraph checkpoint，也不新增 Approval/Tool/Commit/Outbox 类型。
Resource 的现有 `created_by_principal_*`、Turn/Run 的 Principal 快照和已有审计模型保持权威。

## 6. SQL 与事务边界

### 6.1 读取选择

选择 reader 必须以一个 Workspace-scoped join 查询 Session 和可选 Resource。Session predicate 必须同时
包含 `session.id = ? AND session.workspace_id = trusted_workspace_id`；非空选择必须能通过相同 Workspace
join 回到 Resource。禁止先按 ID 全局读取后在 Python 内过滤。

### 6.2 设置或切换选择

一个 PostgreSQL 事务按固定顺序执行：

1. `SELECT ... FROM assistant_sessions WHERE id = ? AND workspace_id = ? FOR UPDATE`。
2. `SELECT ... FROM resources WHERE id = ? AND workspace_id = ? AND source_type = 'upload'
   FOR KEY SHARE`。
3. 仅当值不同时，更新 `selected_resource_id`、`resource_selected_at` 和 Session `updated_at`；相同值走
   无写入 replay。
4. 返回数据库中的准确 `resource_id` 后 commit；任一步失败则整体 rollback。

锁顺序固定为 Session -> Resource，与现有 Session upload 的 Session-first 顺序一致。选择事务不写
Assistant message、Turn、Run 或 Outbox，也不调用 Provider、Tool 或 LangGraph。

### 6.3 上传

现有上传事务顺序保持 `session -> resource/version -> uploaded_file -> message -> session`。仅在解析成功且
Resource/Version 已创建后，于同一事务把 `selected_resource_id` 设为该 Resource，并设置选择时间。

- 上传到新 Session：成功时初始选择为新 Resource。
- 上传到已有 Session：成功时切换为新 Resource。
- 解析失败并返回 `resource: null`：已有 Session 选择保持不变；新 Session 选择保持 `NULL`。
- 选择字段更新、文件 promote 或任一既有元数据写入失败：维持现有整体 rollback 与文件补偿语义。

上传响应 DTO、message kind/payload 和 Resource/Version/File 所有权字段均不改变。

### 6.4 Turn acceptance 与快照

既有 message/stream 请求进入 acceptance 后，在创建新事实的同一 PostgreSQL 事务内：

1. 保持 `(idempotency_scope, request_id)` 与 canonical input hash 的 replay/conflict 检查。
2. 对已有 Session，按 `session.id + trusted workspace_id` 加锁；新 conversation 只在 trusted Workspace
   内创建 Session。
3. 按 `resource.id + trusted workspace_id` 锁定/验证消息体 Resource。此处不限制 `source_type`，以保持
   旧客户端可使用任一既有单 Resource 的兼容行为。
4. 只有 Workspace 三方一致时，才创建 user message、Turn、Run、初始 Step、events 和 Outbox。
5. 同一个消息体 `resource_id` 原样写入 `agent_turns.resource_id`、`agent_runs.resource_id` 和既有 Run
   state。Session 选择不参与 Run scope 解析。

无效 scope 使整个新 acceptance 回滚。已命中的完全相同幂等 replay 返回既有事实，不重新绑定当前
Session 选择；同 Request ID 不同 body 仍按既有幂等冲突处理。

## 7. 隔离与不泄露规则

- 新选择接口和持久化消息写入在数据库访问前必须先通过 TrustedIdentity 校验；签名 Workspace 是唯一
  请求 Workspace 来源。body、path、Resource summary、模型输出或 Session 选择都不能覆盖它。
- Session 不存在与 Session 属于其他 Workspace 使用同一 not-found 分支。
- Resource 不存在、属于其他 Workspace 或不是可选择的 upload Resource，在选择接口使用同一
  `404 {"error":"资源不存在"}`。不得用 `403`、不同消息、Resource title/source type 或计时分支揭示
  其他 Workspace 是否存在该 ID。
- message acceptance 同样以 Workspace-scoped SQL 验证 Resource。为保持现有消息路由错误语义，
  Resource 缺失与跨 Workspace 都进入现有通用持久化失败映射，而不是为既有路径新增可区分 oracle。
- composite FK 是最后防线；repository 仍必须显式带 Workspace predicate，不能依赖 FK 异常授权。
- ToolRuntime 继续从已持久化 Run 读取 Resource，并要求 `resource.workspace_id = run.workspace_id`；不得
  改为读取 Session 当前选择。

## 8. 错误状态冻结

除 SSE 已打开后的错误帧外，响应保持 `{ "error": "<message>" }`。

| 条件 | HTTP | 错误消息/行为 |
| --- | --- | --- |
| 新接口 Session UUID 非法 | `400` | `会话 ID 非法` |
| `PUT` body 非 object，或 `resource_id` 缺失/null/非字符串/非 UUID | `400` | `资源 ID 非法` |
| trusted identity header 缺失 | `401` | 沿用 Assistant durable 路由的 `持久化 身份 为必填项` |
| trusted identity 无效、过期或签名不匹配 | `401` | `持久化 身份 不可信` |
| 已认证 scope 与 header Workspace 不一致 | `403` | `持久化 工作区 范围 不可信` |
| Session 不存在或属于其他 Workspace | `404` | `会话不存在` |
| Resource 不存在、跨 Workspace 或不是 upload Resource | `404` | `资源不存在` |
| selection repository/依赖未装配 | `503` | `会话资源选择不可用` |
| 选择读写的意外持久化失败 | `500` | GET 为 `查询会话资源选择失败`；PUT 为 `更新会话资源选择失败` |
| 重复选择同一 Resource | `200` | 返回相同 DTO，不产生副作用 |

现有接口的状态和消息保持 `docs/remediation/api-contract.md` 所冻结的行为，包括：消息体缺少持久化
`resource_id` 的非 stream 通用 `500`、stream 打开后的 SSE `error`，以及同 Request ID 不同 body 的
既有幂等冲突映射。本功能不顺带修复这些兼容性缺口。

## 9. 客户端接入契约

无前端代码在本仓库交付。客户端流程固定如下：

1. 调用既有 `GET /api/resources`，显示 `source_type == "upload"` 的 Resource。
2. 打开 Session 时调用新 `GET .../resource-selection` 恢复当前选择；`null` 表示尚未选择。
3. 用户选择/切换后，以签名的 `PUT .../resource-selection` 持久化；只有收到 `200` 才更新本地当前值。
4. 每次发送非 stream 或 stream 消息时，在既有 body 中显式传同一个 `resource_id`。
5. 切换不会取消或重定向旧 Run。客户端查看旧消息/Run 时，应使用该 Run/Turn 的 Resource 快照，而不是
   Session 当前选择。

## 10. 兼容性验收条件

- 路由注册快照只增加上述两个新路径；所有既有 method/path 不变。
- 所有既有请求/响应 DTO、错误 envelope、Request ID、SSE 名称/顺序、`Last-Event-ID` 和 replay 不变。
- Turn canonical JSON/hash 仍包含消息体单个 `resource_id`；Session 选择不加入该 hash。
- 一个 Turn/Run 仍只有一个 Resource；Graph、Context、Evidence 和 Tool schema 不出现 Resource 数组。
- 切换前创建的 Run、pending Approval、Tool binding 和 Commit target 保持原 Resource。
- 跨 Workspace 与不存在 Resource 在相同接口上不可区分；scope predicate 位于 SQL，而非 Python 后过滤。
- 上传失败不清空旧选择，上传成功的 Resource/Version/File/Message/选择作为同一事务提交或回滚。
- migration 只追加 nullable 列/约束，不 backfill、不改写历史 SQL；旧应用版本可忽略新增列。

## 11. 后续阶段和门禁

### Phase 1：失败路径与兼容性测试

用户确认本 Phase 0 后，先只补测试并停下审查。测试必须先覆盖：trusted identity 缺失/无效、Session
跨 Workspace/不存在、Resource 跨 Workspace/不存在/非 upload、GET null、重复 PUT、并发写入 SQL
锁形态、成功/失败上传选择、消息 Resource scope、显式 body 优先、缺失 body 不 fallback、切换后旧
Run 不变、路由/DTO/SSE 回归。该阶段不实现业务行为。

### Phase 2：数据与 repository

Phase 1 获确认后，新增 append-only migration、selection repository、上传事务选择写入及 Turn acceptance
Workspace 验证。不得改写已有 migration；阶段结束后停下审查。

### Phase 3：API 与生产装配

Phase 2 获确认后，新增 route/dependency protocol/生产 assembly，保持现有路由与 DTO；阶段结束后停下审查。

### Phase 4：无数据库验证

Phase 3 获确认后，运行 repository 允许的 database-free tests、`ruff check`、`ruff format --check`、
`pyright` 和 compile 检查。数据库 round-trip 只有同时满足共享 fuse 时才允许执行；否则明确记录未执行。

## 12. 本 Phase 0 验证与未执行项

本阶段只读取 FastAPI 装配、route、DTO、upload/Turn/Resource repository SQL、现有无数据库测试和
`docs/remediation/` 契约。未读取 `.env`，未启动服务，未调用 Provider/Tika，未连接 PostgreSQL，未运行
migration/DDL/backfill，未创建测试或 Python 实现，未改变部署、文件存储或外部流量。

本契约需用户明确确认后才能进入 Phase 1。
