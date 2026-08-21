# API 契约冻结

**证据时间：** 2026-08-20。本文只描述当前 FastAPI 应用实际注册的路由。错误 DTO 的共同形态是 JSON object `{ "error": "中文错误消息" }`，除 SSE error 外不承诺额外字段。未注册的前端库调用不属于公开 API。

## 已注册路由

| 方法 | 路径 | 输入 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/healthz` | 无输入 | `200 {"status":"ok","service":"server"}` |
| OPTIONS | `/api/*path` | `Origin` 与 CORS 请求 header | origin 缺失或允许时返回空 `204`；非空且不允许时返回 `403` |
| GET | `/api/resources` | 已签名 identity header；无 query | `200 {"resources":[{"id","title","source_type","created_at"}]}` |
| GET | `/api/resources/:id` | 已签名 identity header；path 参数 `id`：UUID | `200 {"resource":summary,"current_version":version|null}` |
| GET | `/api/resources/:id/export` | 已签名 identity header；path 参数 `id`：UUID | `200 text/markdown; charset=utf-8`，附件文件名为 `<sanitized-title-or-resource-id>.md` |
| GET | `/api/resources/:id/search` | 已签名 identity header；path 参数 `id`：UUID；query `q` 非空白 | `200 {"query":string,"citations":[Citation...]}`，最多五条 citation |
| GET | `/api/agent/runs` | 已签名 identity header；query `limit` 默认 50、范围 1..100；`status` 枚举；`resource_id` UUID | `200 {"runs":[RunSummary...]}` |
| GET | `/api/agent/runs/:id` | 已签名 identity header；path 参数 `id`：UUID | `200 {"run":Run,"steps":Step[],"tool_calls":ToolCall[],"approvals":ApprovalView[],"findings":Finding[]}` |
| GET | `/api/agent/approvals` | 已签名 identity header；query `limit` 默认 50、范围 1..100；`status` 为 pending/approved/rejected/cancelled | `200 {"approvals":[ApprovalSummary...]}` |
| GET | `/api/agent/approvals/:id` | 已签名 identity header；path 参数 `id`：UUID | `200 {"approval":ApprovalSummary}` |
| POST | `/api/agent/approvals/:id/approve` | 已签名的 owner/admin 身份；path UUID；JSON `{ "reason": nonblank }` | `200 {"approval":Approval}` |
| POST | `/api/agent/approvals/:id/reject` | 与 approve 相同 | `200 {"approval":Approval}` |
| GET | `/api/assistant/capabilities` | 无输入 | `200 {"upload":{"supported_extensions":[],"accept":string,"hint":string}}` |
| GET | `/api/assistant/sessions` | 已签名 identity header | `200 {"sessions":[Session...]}` |
| GET | `/api/assistant/sessions/:id` | 已签名 identity header；path UUID | `200 {"session":Session,"messages":[Message...]}` |
| DELETE | `/api/assistant/sessions/:id` | 已签名 identity header；path UUID | 空 `204`；session 缺失时为 `404` |
| POST | `/api/assistant/conversations` | JSON `{ "message": nonblank, "resource_id": UUID? }`；持久化 acceptance 实际要求非空 `resource_id`；HTTP 边界可不传 `X-Request-ID`，缺失时自动生成 | `201`，来自持久化 projection 的 Assistant conversation DTO |
| POST | `/api/assistant/conversations/files` | multipart `file`；扩展名必须在 capabilities 允许范围内；最大 20 MiB | `200 {"session":Session,"resource":Resource|null,"messages":Message[],"error_message":string|null}` |
| POST | `/api/assistant/conversations/stream` | 与 conversation 相同的 JSON；持久化 acceptance 要求 `resource_id`；`X-Request-ID`、可选的非负整数 `Last-Event-ID`、已签名 identity header 及准确 workspace/resource | `200 text/event-stream; charset=utf-8` |
| POST | `/api/assistant/sessions/:id/messages` | path UUID；JSON `{ "message": nonblank, "resource_id": UUID? }`；持久化 acceptance 要求 `resource_id`；request ID 语义同上 | `200`，来自持久化 projection 的 Assistant conversation DTO |
| POST | `/api/assistant/sessions/:id/messages/stream` | path UUID；header/body 与 append 相同 | `200 text/event-stream; charset=utf-8` |
| POST | `/api/assistant/sessions/:id/files` | path UUID；multipart `file`；最大 20 MiB | 与 upload DTO 相同 |
| GET | `/api/files/:id/download` | 已签名 identity header；path 参数 `id`：UUID | 流式返回原始字节；`Content-Type` 为存储类型或 `application/octet-stream`；`Content-Disposition: attachment; filename=...` |

路由证据：`src/docreview/api/main.py` 注册的 FastAPI routers，以及 `src/docreview/api/routes/`
中的 endpoint 定义。当前应用未注册 resource task-context，因此该路径不属于公开 API 契约。

## 成功 DTO 定义

- `Session`：`id`、`title`、`web_search_enabled`、`last_message_at`、`created_at`、`updated_at`。
- `Message`：`id`、`role`、`kind`、JSON object `payload`、`sequence_no`、`created_at`。当前前端 union 保留 `kind` 值 `text`、`task_suggestion`、`task_created`、`task_status`、`session_file` 和 `system`；对应 payload object 分别包含内容/检索摘要、建议元数据、已创建/状态任务元数据、上传文件元数据或 system level/content。未知 kind/payload 为兼容性保留为 JSON，不能作为授权数据。
- `Resource summary`：`id`、`title`、`source_type`、`created_at`；Assistant upload 的 resource summary 不含 `created_at`。
- `Current version`：`id`、`version_number`、`content`、`source`、`created_at`。
- `Citation`：`citation_id`、`resource_id`、可选的 `section_id/section_type/window`、`section_title`、`snippet`；`window` 包含可选的 `group_id`、`start_order`、`end_order`。
- `RunSummary`：`id`、`workspace_id`、可选的 `resource_id/session_id/request_id/current_step/pending_approval_id`、`status`、`objective`、`step_count`、`completed_step_count`、`failed_step_count`、`created_at`、`updated_at`。
- 公开 `Run`：`id`、可选的 `resource_id/session_id/request_id/current_step/deadline_at/cancel_requested_at`、`status`、`objective`、`created_at`、`updated_at`。
- 公开 `Step`：`id`、`step_key`、`step_type`、`status`、`attempt_count`、`max_attempts`、可选的 `next_retry_at` 和时间戳。
- 公开 `ToolCall`：`id`、`step_id`、`tool_name`、`tool_version`、`status`、可选的 `error_category/started_at/completed_at`。
- `ApprovalSummary`：`id`、`workspace_id`、`run_id`、`step_id`、可选的 `resource_id/session_id/decision_reason/decided_at`、`objective`、`tool_name`、`tool_version`、`reason`、`status`、JSON `resources`、JSON `payload`、`created_at`。
- Approval decision response 刻意保持精简：`{ "approval": {"id": string, "status": string} }`。

空数组在响应模型和 handler 边界序列化为 `[]` 而非 `null`。时间戳保持 RFC3339 JSON timestamp。

## 通用 header 与 trusted ingress

### Request ID

Request ID middleware 读取并去除 `X-Request-ID` 两端空白；缺失时生成随机的 32 位十六进制 ID，
将其写入 request state，并在响应 `X-Request-ID` 中返回所选值（`src/docreview/api/main.py`）。
持久化 Assistant 请求把客户端稳定 ID 绑定到 `RequestID` 和 `TraceID`；相同 ID 与相同 canonical
body 会 replay 已接受的 Turn。同一幂等 scope 下 body 不同会形成持久化幂等冲突。当前 Assistant
HTTP adapter 没有为该冲突提供专用的 409 映射：非 stream 返回通用 500，已打开的 stream 返回
SSE `error`。这是冻结的兼容性缺口，不代表可以创建第二个 Turn。

未签名的基础路由可以依赖 backend 生成 request ID，但受信的持久化请求不能如此：ingress 必须在计算 HMAC 前选择或保留 `X-Request-ID`，因为签名 tuple 包含该准确值。只有 trusted ingress 同时提供 request ID 与匹配 attestation 时，浏览器才可以省略该 header。

### 持久化身份 header

除 health、CORS preflight 和 Assistant capabilities 外，所有 `/api` 业务 endpoint 都要求 trusted proxy attestation：

`X-DocReview-Principal-Type`, `X-DocReview-Principal-ID`, `X-DocReview-Organization-ID`, `X-DocReview-Workspace-ID`, `X-DocReview-Identity-Issued-At`, `X-DocReview-Roles`, `X-DocReview-Identity-Signature`.

HMAC-SHA256 的规范输入是以下 tuple 以换行连接后的结果：

```text
v1
request_id
HTTP_METHOD
request_path
principal_type
principal_id
organization_id
workspace_id
issued_at_rfc3339nano
comma_separated_roles
```

ID 必须是 UUID；principal type 为 `user` 或 `service`；签发时间最多可比当前时间超前 30 秒，
且必须处于配置的最大有效期内；signature 为小写十六进制并采用常量时间比较；签名 workspace
必须等于请求 workspace。实现位于 `src/docreview/identity/trusted_ingress.py`，各路由通过
`src/docreview/api/dependencies.py` 绑定身份和 scope。

Trusted ingress 是窄的兼容边界，不是 IdP。它必须在签名之前剥离客户端提供的 identity header。
身份缺失、格式错误、过期、不匹配或不可信时必须 fail-closed，不能调用 legacy fallback。

资源、会话、文件、Run、Approval、持久化 Turn、资源选择和上传 handler 均从已验证的
`WorkspaceScope` 获取 workspace ID，再传给 repository。客户端不能通过固定 compatibility scope 或
自行提供未签名 workspace 来选择租户。跨 Workspace 的资源、会话和文件 ID 按不存在处理并返回原有
404，不能形成对象存在性 oracle。浏览器不得持有 HMAC secret；identity header 必须由同源 trusted
ingress 在剥离客户端同名 header 后生成。

## Body、query 与 path 校验

- UUID path parameter 在访问 repository 前返回 `400 {"error":"... ID 非法"}`。
- 空白 Assistant `message` 返回 `400 {"error":"消息不能为空"}`（实现保留准确的 domain error 文本）。
- `resource_id` 在 Handler DTO 的语法层面可选，但当前持久化 repository 要求它与 trusted Principal/Workspace 一起提供。UUID 非法返回 `400`；省略/空白时进入当前通用持久化失败映射（非 stream 为 500，stream 启动后为 SSE error）。
- `Last-Event-ID` 必须解析为 `>= 0` 的整数，否则返回 `400`。
- Run/approval `limit` 默认 50，仅接受 1..100；无效值返回 `400`。
- Run/approval filter 拒绝未知 status 并返回 `400`。
- Upload 要求 multipart field `file`、受支持 filename、可读内容和最大 20 MiB；超大返回 `413`，格式错误/
  缺失/不支持输入返回 `400`。

## 错误映射

| 条件 | HTTP | DTO |
| --- | --- | --- |
| UUID、body、query、limit、status、upload 或 `Last-Event-ID` 非法 | 400 | `{ "error": message }` |
| trusted identity 缺失、过期或无效 | 401 | `{ "error": "durable identity is not trusted" }` 或路由专用等价消息 |
| trusted identity 缺少 Approval 权限或 scope | 403 | `{ "error": "审批权限不足" }` 或 scope 等价消息 |
| Resource/session/file/Approval/Run 缺失 | 404 | `{ "error": "...不存在" }` |
| 检索时 current version 缺失 | 409 | `{ "error": "资源当前版本不存在，无法检索" }` |
| Approval 状态/幂等冲突 | 409 | `{ "error": "审批状态冲突" }` |
| Upload 超过配置的最大值 | 413 | `{ "error": "上传文件过大" }` |
| Runtime/pipeline 不可用 | 503 | `{ "error": "durable agent runtime is unavailable" }` |
| 非确定性的持久化 Turn timeout | 503 | `{ "error": "durable turn state is not ready; retry with the same request id" }` |
| repository/Provider 未配置或 backend 意外失败 | 500 | `{ "error": 路由专用稳定消息 }` |

测试必须捕获的当前兼容例外：Turn request 幂等冲突和缺少持久化 `resource_id` 在 handler 校验后
不会映射为 409/400，而是使用上文的通用持久化失败路径。改变这些状态码属于单独的公开 API 修复，
不属于本契约的行为保持范围。

### 路由族错误 oracle

除标记为 SSE 的条目外，下表均使用 `{ "error": "<exact message>" }`；未列出的消息使用对应路由族的通用 500 消息。

| 路由族 | 400 | 401/403 | 404/409 | 500/503 |
| --- | --- | --- | --- | --- |
| resource 列表 | - | 401/403 trusted ingress 通用消息 | - | `资源存储未配置`；`查询资源列表失败` |
| resource 详情 | `资源 ID 非法` | 401/403 trusted ingress 通用消息 | 404 `资源不存在` | `资源存储未配置`；`查询资源失败`；`查询资源版本失败` |
| resource 导出 | `资源 ID 非法` | 401/403 trusted ingress 通用消息 | 404 `资源不存在`；404 `资源没有可导出的当前版本` | 与上述 storage/query 失败相同 |
| resource 检索 | `查询参数 q 不能为空`；`资源 ID 非法` | 401/403 trusted ingress 通用消息 | 404 `资源不存在`；409 `资源当前版本不存在，无法检索` | `资源存储未配置`；`查询资源失败`；`查询资源版本失败`；`检索服务未配置`；`检索资源失败` |
| Run 列表/详情 | `limit 必须介于 1 和 100 之间`；`运行状态无效`；`资源 ID 非法`；`运行 ID 非法` | 401 `Agent 查询身份不可信` | 404 `记录不存在` | 503 `Agent 运行查询服务未配置`；500 `运行记录查询失败` |
| Approval 列表/详情 | limit/status/ID 非法时使用 `审批状态无效` / `审批 ID 非法` | 401 `Agent 查询身份不可信` | 404 `记录不存在` | query service 未配置时为 503；500 `审批记录查询失败` |
| Approval approve/reject | `审批 ID 非法`；`审批理由不能为空`；fallback 为 `审批请求无效` | 401 `审批身份不可信`；403 `审批权限不足` | 404 `审批不存在`；409 `审批状态冲突` | 503 `持久化审批服务未配置`；500 `审批决策失败` |
| Assistant session 读取 | `<会话 ID> 非法` | 401/403 trusted ingress 通用消息 | domain `会话不存在` | `查询会话列表失败`；`查询会话失败` |
| Assistant 删除 | `会话 ID 非法` | 401/403 trusted ingress 通用消息 | `会话不存在` | `删除会话失败` |
| stream 启动前的持久化 Turn | `消息内容不能为空`；`资源 ID 非法`；`Last-Event-ID 非法` | 401 `durable identity is required` / `durable identity is not trusted`；403 `durable workspace scope is not trusted` | 无专用 request conflict 映射 | Runtime 不可用/未就绪消息为 503；500 `处理助手请求失败` |
| SSE 启动后的持久化 Turn | validation 已在 200 前完成 | validation 已在 200 前完成 | 不重新映射 HTTP | SSE `error` `{code:"assistant_internal_error",message:"助手暂时不可用，请使用相同 request_id 重试。"}`，随后关闭 |
| Assistant upload | `必须上传文件`；格式 policy 消息；`读取上传文件失败` | 401 `durable identity is required` / `durable identity is not trusted` | domain session 404 | 413 `上传文件过大`；500 `上传文件失败`；503 `durable identity adapter is not configured` |
| 文件下载 | `文件 ID 非法` | 401/403 trusted ingress 通用消息 | `文件不存在`；`文件内容不存在` | `文件下载服务未配置`；`查询文件失败`；`读取文件失败` |

新增受保护读取路由的 trusted ingress 通用消息为：缺少签名时 401
`durable identity is required`，签名无效时 401 `durable identity is not trusted`，已认证 scope
与请求 workspace 不一致时 403 `durable workspace scope is not trusted`，identity adapter 未装配时
503 `trusted identity adapter is not configured`。鉴权发生在 path/query 和 repository 校验之前。

Python 适配器可以使用结构化内部错误码，但上面的公开状态和 DTO 映射已冻结。公开 Run DTO 不得暴露
数据库 URL、provider credential、原始 Tool input/output、ContextManifest 内容、Attempt 数据或内部 trace index。

## SSE 契约

Content type 为 `text/event-stream; charset=utf-8`，带 `Cache-Control: no-cache`、`Connection: keep-alive`，
并立即 flush。持久化 event frame 由三行及一个空行组成：`id: <sequence>`、`event: <event name>`、
`data: <one JSON object>`。持久化 sequence 为正数。Synthetic transport error/done frame 可以复用非负
reconnect cursor，包括 `0`。

公开 parser 保留的 legacy Assistant event name 为 `session_created`、`session_file`、`message_started`、
`message_delta`、`message_completed`、`task_suggestion`、`error`、`turn_state`、`done`。持久化映射如下：

| 持久化 event | 公开 event | Data | 是否 terminal |
| --- | --- | --- | --- |
| `turn.accepted`, `turn.running`, `run.queued` | `turn_state` | 持久化 payload | 否 |
| `assistant.message` | `message_completed` | `{ "message": Message }`（缺少 wrapper 时补充） | 否 |
| `turn.waiting_input`, `turn.waiting_approval` | `turn_state`，随后为 `done` | status payload，随后为 `{}` | 是 |
| `turn.succeeded` | `done` | `{}` | 是 |
| `turn.failed`, `turn.cancelled` | `error`，随后为 `done` | `{code:"assistant_internal_error",message:"持久化轮次结束，但没有可恢复的结果"}`，随后为 `{}` | 是 |
| 持久化 `done` | 同名 event | `{}` | 是 |

包含 CR/LF 或为空的 event type name 会被拒绝。无效/空的持久化 payload 渲染为 `{}`。Server 只写入
sequence 大于 `Last-Event-ID` 的 event；reconnect 重复相同 body 与 `X-Request-ID`。Client
`assistant-stream.ts:150-237` 跟踪收到的最大 ID，在有界次数内 retry，并将缺少 `done` 视为可恢复中断。
损坏的 observer/SSE socket 不会 rollback Turn acceptance 或 outcome。

由一个持久化 waiting/failure/cancellation event 生成的两个 terminal frame 复用该 event 的同一 `id`。
若 pipeline execution 在 HTTP 200 提交后失败，adapter 发出一个 `error`，其 `id` 等于调用方的
`Last-Event-ID` cursor，message 为 `助手暂时不可用，请使用相同 request_id 重试。`，随后关闭且不保证
`done`；frontend 将该 error 视为 terminal。持久化 terminal cursor 后的 reconnect 即使没有更新的
持久化 event，也可能在该 cursor 收到 synthetic `done`。

## CORS

配置 origin 使用精确字符串匹配。生产至少要求一个 origin；wildcard、非 HTTP(S)、带 credential 或
含 path/query/fragment 的 origin 会被配置拒绝。允许的 origin 收到与 request origin 相等的
`Access-Control-Allow-Origin`、`Vary: Origin`、方法 `GET, POST, PATCH, DELETE, OPTIONS`、header
`Content-Type, X-Request-ID` 及暴露的 `X-Request-ID`。不允许的非空 origin preflight 返回 `403`；不发送
wildcard。实现和配置校验位于 `src/docreview/api/main.py` 与
`src/docreview/config/settings.py`。

当前 CORS allow-headers 不包含 `Last-Event-ID` 或 `X-DocReview-*` identity header。Identity header
应在 trusted ingress 剥离/添加，而不是由浏览器代码发送。因此，跨源浏览器重连若明确发送
`Last-Event-ID`，需要同源代理或本 backend 之外的 ingress/CORS policy；扩大 backend allowlist
属于必须单独审查的行为/安全变更。

## 兼容性说明

`GET /api/assistant/sessions` 与 upload/download 是 Assistant/file service 的兼容职责，不属于
Agent graph。`/api/tasks`、`/api/approvals`、`/api/jobs`、task-suggestion confirmation 和
`/api/resources/:id/task-context` 当前未注册；若没有单独的产品/API 变更，不得引入这些路径。
