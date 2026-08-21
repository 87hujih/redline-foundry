# DocReview Agent API Reference

面向 Web 前端的在线 API 参考。本文以当前 `FastAPI` 实际注册的路由为准（证据：
`src/docreview/api/main.py` 与 `src/docreview/api/routes/`）。后端关闭了 Swagger/OpenAPI
（`docs_url`、`redoc_url`、`openapi_url` 均为 `None`），因此本文件是前端调用契约的单一入口。

## 基本约定

- 默认 API 前缀为 `/api`；前端可通过 `VITE_API_BASE` 覆盖（默认 `/api`）。
- JSON 使用 UTF-8；时间字段为 RFC3339 UTC 字符串，例如 `2026-08-20T12:00:00Z`。
- 所有非成功 JSON 错误均为 `{ "error": "中文错误消息" }`。响应可能带 `X-Request-ID`，应在日志和错误反馈中保留。
- 浏览器跨源预检由 `OPTIONS /api/*path` 处理；允许 origin 返回 `204`，不允许的非空 origin 返回 `403`。生产前端优先使用同源代理。
- UUID 参数必须是标准 UUID。空消息、空搜索词和非法 query/header 返回 `400`。
- 同源请求使用 `credentials: same-origin`。持久化 Turn、上传、Run/Approval 接口由受信 ingress 注入身份 header；浏览器不应自行伪造这些 header。

## 接口总览

| 方法 | 路径 | 用途 | 成功状态 |
| --- | --- | --- | --- |
| GET | `/healthz` | 服务健康检查 | 200 |
| GET | `/api/assistant/capabilities` | 上传能力 | 200 |
| GET | `/api/assistant/sessions` | 会话列表 | 200 |
| GET | `/api/assistant/sessions/{session_id}` | 会话及消息 | 200 |
| GET | `/api/assistant/sessions/{session_id}/resource-selection` | 读取当前资源选择 | 200 |
| PUT | `/api/assistant/sessions/{session_id}/resource-selection` | 设置当前资源选择 | 200 |
| DELETE | `/api/assistant/sessions/{session_id}` | 删除会话 | 204 |
| POST | `/api/assistant/conversations` | 创建会话并发送首条消息 | 201 |
| POST | `/api/assistant/sessions/{session_id}/messages` | 发送消息 | 200 |
| POST | `/api/assistant/conversations/stream` | 创建会话并以 SSE 返回 | 200 |
| POST | `/api/assistant/sessions/{session_id}/messages/stream` | 发送消息并以 SSE 返回 | 200 |
| POST | `/api/assistant/conversations/files` | 上传文件并创建会话 | 200 |
| POST | `/api/assistant/sessions/{session_id}/files` | 向会话上传文件 | 200 |
| GET | `/api/resources` | 资源列表 | 200 |
| GET | `/api/resources/{resource_id}` | 资源详情和当前版本 | 200 |
| GET | `/api/resources/{resource_id}/search?q=...` | 资源检索 | 200 |
| GET | `/api/resources/{resource_id}/export` | 导出 Markdown | 200 |
| GET | `/api/files/{file_id}/download` | 下载原始文件 | 200 |
| GET | `/api/agent/runs` | Run 列表 | 200 |
| GET | `/api/agent/runs/{run_id}` | Run 详情 | 200 |
| GET | `/api/agent/approvals` | Approval 列表 | 200 |
| GET | `/api/agent/approvals/{approval_id}` | Approval 详情 | 200 |
| POST | `/api/agent/approvals/{approval_id}/approve` | 批准 | 200 |
| POST | `/api/agent/approvals/{approval_id}/reject` | 拒绝 | 200 |

## 通用数据结构

字段定义与前端 TypeScript 类型保持一致，见 [`frontend/src/api/types.ts`](../../frontend/src/api/types.ts)。

`Session`：`id`、`title`、`web_search_enabled`、`last_message_at`、`created_at`、`updated_at`。

`Message`：`id`、`role`、`kind`、`payload`（JSON object）、`sequence_no`、`created_at`。已知 `kind` 包括
`text`、`task_suggestion`、`task_created`、`task_status`、`session_file`、`system`；未知值必须按兼容数据保留。

`Resource`：`id`、`title`、`source_type`、`created_at`（上传响应中的 resource 可能没有 `created_at`）。

`ResourceVersion`：`id`、`version_number`、`content`、`source`、`created_at`。

`Citation`：`citation_id`、`resource_id`、`section_title`、`snippet`，以及可选 `section_id`、`section_type`、
`window`（`group_id`、`start_order`、`end_order`）。检索最多返回 5 条。

## Assistant 与会话

### GET `/api/assistant/capabilities`

响应：`{ "upload": { "supported_extensions": [".pdf"], "accept": ".pdf", "hint": "支持 pdf" } }`。
未配置上传时三个字段分别为 `[]`、`""`、`"当前服务未开放文件上传"`。上传前应使用 `accept` 限制文件选择器。

### GET `/api/assistant/sessions`

响应：`{ "sessions": Session[] }`。

### GET `/api/assistant/sessions/{session_id}`

响应：`{ "session": Session, "messages": Message[] }`。会话不存在返回 `404`。

### GET `/api/assistant/sessions/{session_id}/resource-selection`

响应：`{ "resource_id": string | null }`。此接口需要 trusted ingress 的 workspace 身份。

### PUT `/api/assistant/sessions/{session_id}/resource-selection`

请求：`{ "resource_id": "<UUID>" }`。响应：`{ "resource_id": "<UUID>" }`。会话或资源不存在均为 `404`。

### DELETE `/api/assistant/sessions/{session_id}`

成功返回空 body 和 `204`；不存在返回 `404`。

### POST `/api/assistant/conversations`、POST `/api/assistant/sessions/{session_id}/messages`

请求 JSON：

```json
{ "message": "请审阅这份文档", "resource_id": "<UUID>" }
```

`message` 必填且不能全为空白；`resource_id` 在 HTTP DTO 中可选，但持久化运行时要求提供。创建会话返回
`201`，追加消息返回 `200`；两者响应均为 `{ "session": Session, "messages": Message[] }`（以持久化 projection 为准）。

### POST `/api/assistant/conversations/files`、POST `/api/assistant/sessions/{session_id}/files`

使用 `multipart/form-data`，字段名必须为 `file`。默认最大 20 MiB，扩展名必须属于 capabilities 返回的列表。
响应：

```json
{ "session": Session, "resource": Resource | null, "messages": Message[], "error_message": string | null }
```

文件缺失、空内容、扩展名不支持返回 `400`；超过大小返回 `413`。

## SSE 流式消息

两个 stream endpoint 的请求 body 与普通消息相同，必须发送 `Accept: text/event-stream`、
`Content-Type: application/json` 和稳定的 `X-Request-ID`。重连时可发送非负整数 `Last-Event-ID`；服务只返回
sequence 大于该值的事件。响应头为 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`。

每帧格式如下，帧之间以空行分隔：

```text
id: 12
event: message_completed
data: {"message":{"id":"...","kind":"text"}}

```

公开事件名：`session_created`、`session_file`、`message_started`、`message_delta`、`message_completed`、
`task_suggestion`、`turn_state`、`error`、`done`。`done` 表示正常终止；`error` 为终止性错误，data 通常为
`{ "code": "assistant_internal_error", "message": "..." }`。网络中断不会撤销已接受的 Turn，客户端应使用相同
`X-Request-ID` 和最新 cursor 重连，并去重已处理的 `id`。

## Resources 与文件

### GET `/api/resources`

响应：`{ "resources": Resource[] }`。

### GET `/api/resources/{resource_id}`

响应：`{ "resource": Resource, "current_version": ResourceVersion | null }`。

### GET `/api/resources/{resource_id}/search?q={query}`

`q` 必填且不能为空白。响应：`{ "query": string, "citations": Citation[] }`。没有当前版本返回 `409`。

### GET `/api/resources/{resource_id}/export`

响应为 `text/markdown; charset=utf-8`，并带附件 `Content-Disposition`。没有可导出的当前版本返回 `404`。

### GET `/api/files/{file_id}/download`

响应为原始字节流，`Content-Type` 使用存储类型（缺省 `application/octet-stream`），并带 `Content-Length`（若可得）
和附件文件名。文件或文件内容不存在返回 `404`。

## Agent Runs 与 Approvals

以下四类查询及决策接口需要 trusted ingress 身份和 workspace scope。身份 header 由受信代理注入：
`X-DocReview-Principal-Type`、`X-DocReview-Principal-ID`、`X-DocReview-Organization-ID`、
`X-DocReview-Workspace-ID`、`X-DocReview-Identity-Issued-At`、`X-DocReview-Roles`、
`X-DocReview-Identity-Signature`。前端只需通过同源/受保护代理调用。

### GET `/api/agent/runs`

Query：`limit`（默认 50，范围 1..100）、`status`（`queued`、`running`、`waiting_input`、`waiting_approval`、
`succeeded`、`failed`、`cancelled`）、`resource_id`（UUID）。响应：`{ "runs": RunSummary[] }`。

### GET `/api/agent/runs/{run_id}`

响应：`{ "run": PublicRun, "steps": RunStep[], "tool_calls": ToolCall[], "approvals": ApprovalView[], "findings": Finding[] }`。
公开 DTO 不包含凭据、原始 tool input/output 或内部 trace 数据。

### GET `/api/agent/approvals`

Query：`limit`（默认 50，范围 1..100）、`status`（`pending`、`approved`、`rejected`、`cancelled`）。响应：
`{ "approvals": Approval[] }`。

### GET `/api/agent/approvals/{approval_id}`

响应：`{ "approval": Approval }`。

### POST `/api/agent/approvals/{approval_id}/approve`、`/reject`

请求 JSON：`{ "reason": "批准理由" }`，理由不能为空。响应：`{ "approval": { "id": string, "status": string } }`。
无权限 `403`，状态冲突 `409`。

## 错误与重试

| 状态 | 前端处理 |
| --- | --- |
| 400 | 修正请求参数后再提交 |
| 401/403 | 交给登录态/受保护代理处理，不在组件内伪造身份 |
| 404 | 显示资源已不存在并刷新列表 |
| 409 | 刷新详情；Approval 冲突不要重复提交 |
| 413 | 提示文件大小限制 |
| 503 | 可重试；Turn 使用原 `X-Request-ID` |
| 500 | 显示通用错误并记录 `X-Request-ID` |

未注册接口（例如 `/api/tasks`、`/api/jobs`、`/api/resources/{id}/task-context`）不属于当前 API，前端不得调用。
