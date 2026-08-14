# API Contract Freeze

**证据时间：** 2026-08-12。本文只描述当前 `router.New` 实际注册的路由。错误 DTO 的共同形态是 JSON object `{ "error": "中文错误消息" }`，除 SSE error 外不承诺额外字段。未注册的前端库调用不属于公开 API。

## Registered routes

| Method | Path | Inputs | Success |
| --- | --- | --- | --- |
| GET | `/healthz` | no input | `200 {"status":"ok","service":"server"}` |
| OPTIONS | `/api/*path` | `Origin` and CORS request headers | `204` empty when origin absent/allowed; `403` for a non-empty disallowed origin |
| GET | `/api/resources` | no query | `200 {"resources":[{"id","title","source_type","created_at"}]}` |
| GET | `/api/resources/:id` | path `id`: UUID | `200 {"resource":summary,"current_version":version|null}` |
| GET | `/api/resources/:id/export` | path `id`: UUID | `200 text/markdown; charset=utf-8`, attachment filename `<sanitized-title-or-resource-id>.md` |
| GET | `/api/resources/:id/search` | path `id`: UUID; query `q` nonblank | `200 {"query":string,"citations":[Citation...]}`, at most five citations |
| GET | `/api/agent/runs` | signed identity headers; query `limit` default 50, 1..100; `status` enum; `resource_id` UUID | `200 {"runs":[RunSummary...]}` |
| GET | `/api/agent/runs/:id` | signed identity headers; path `id`: UUID | `200 {"run":Run,"steps":Step[],"tool_calls":ToolCall[],"approvals":ApprovalView[],"findings":Finding[]}` |
| GET | `/api/agent/approvals` | signed identity headers; query `limit` default 50, 1..100; `status` in pending/approved/rejected/cancelled | `200 {"approvals":[ApprovalSummary...]}` |
| GET | `/api/agent/approvals/:id` | signed identity headers; path `id`: UUID | `200 {"approval":ApprovalSummary}` |
| POST | `/api/agent/approvals/:id/approve` | signed owner/admin identity; path UUID; JSON `{ "reason": nonblank }` | `200 {"approval":Approval}` |
| POST | `/api/agent/approvals/:id/reject` | same as approve | `200 {"approval":Approval}` |
| GET | `/api/assistant/capabilities` | no input | `200 {"upload":{"supported_extensions":[],"accept":string,"hint":string}}` |
| GET | `/api/assistant/sessions` | no input | `200 {"sessions":[Session...]}` |
| GET | `/api/assistant/sessions/:id` | path UUID | `200 {"session":Session,"messages":[Message...]}` |
| DELETE | `/api/assistant/sessions/:id` | path UUID | `204` empty; missing session `404` |
| POST | `/api/assistant/conversations` | JSON `{ "message": nonblank, "resource_id": UUID? }`; durable acceptance actually requires nonblank `resource_id`; `X-Request-ID` optional at HTTP boundary and generated if absent | `201` assistant conversation DTO from durable projection |
| POST | `/api/assistant/conversations/files` | multipart `file`; accepted extension from capabilities; max 20 MiB | `200 {"session":Session,"resource":Resource|null,"messages":Message[],"error_message":string|null}` |
| POST | `/api/assistant/conversations/stream` | same JSON as conversation; durable acceptance requires `resource_id`; `X-Request-ID`, optional `Last-Event-ID` nonnegative integer, signed identity headers and exact workspace/resource | `200 text/event-stream; charset=utf-8` |
| POST | `/api/assistant/sessions/:id/messages` | path UUID; JSON `{ "message": nonblank, "resource_id": UUID? }`; durable acceptance requires `resource_id`; request ID semantics as above | `200` assistant conversation DTO from durable projection |
| POST | `/api/assistant/sessions/:id/messages/stream` | path UUID; same headers/body as append | `200 text/event-stream; charset=utf-8` |
| POST | `/api/assistant/sessions/:id/files` | path UUID; multipart `file`; max 20 MiB | same upload DTO |
| GET | `/api/files/:id/download` | path `id`: UUID | streamed original bytes; `Content-Type` stored type or `application/octet-stream`; `Content-Disposition: attachment; filename=...` |

Route evidence: `apps/server/internal/server/router/router.go:63-97`. The Router has no resource task-context registration; the frontend helper at `apps/web/lib/api/resources.ts:59` is therefore not a current backend contract.

## Success DTO definitions

- `Session`: `id`, `title`, `web_search_enabled`, `last_message_at`, `created_at`, `updated_at`.
- `Message`: `id`, `role`, `kind`, JSON object `payload`, `sequence_no`, `created_at`. The active frontend union preserves `kind` values `text`, `task_suggestion`, `task_created`, `task_status`, `session_file`, and `system`; the corresponding payload objects contain content/search summary, suggestion metadata, created/status task metadata, uploaded-file metadata, or system level/content. Unknown kind/payload is retained as JSON for compatibility and is not authorization data.
- `Resource summary`: `id`, `title`, `source_type`, `created_at`; assistant upload omits `created_at` from its resource summary.
- `Current version`: `id`, `version_number`, `content`, `source`, `created_at`.
- `Citation`: `citation_id`, `resource_id`, optional `section_id/section_type/window`, `section_title`, `snippet`; `window` contains optional `group_id`, `start_order`, `end_order`.
- `RunSummary`: `id`, `workspace_id`, optional `resource_id/session_id/request_id/current_step/pending_approval_id`, `status`, `objective`, `step_count`, `completed_step_count`, `failed_step_count`, `created_at`, `updated_at`.
- Public `Run`: `id`, optional `resource_id/session_id/request_id/current_step/deadline_at/cancel_requested_at`, `status`, `objective`, `created_at`, `updated_at`.
- Public `Step`: `id`, `step_key`, `step_type`, `status`, `attempt_count`, `max_attempts`, optional `next_retry_at`, timestamps.
- Public `ToolCall`: `id`, `step_id`, `tool_name`, `tool_version`, `status`, optional `error_category/started_at/completed_at`.
- `ApprovalSummary`: `id`, `workspace_id`, `run_id`, `step_id`, optional `resource_id/session_id/decision_reason/decided_at`, `objective`, `tool_name`, `tool_version`, `reason`, `status`, JSON `resources`, JSON `payload`, `created_at`.
- Approval decision response is deliberately narrow: `{ "approval": {"id": string, "status": string} }`.

Arrays that have no values are serialized as `[]`, not `null`, at the handler boundary where the Go code normalizes them. Timestamps remain RFC3339 JSON timestamps.

## Common headers and trusted ingress

### Request ID

`RequestContext` reads `X-Request-ID`, trims it, generates a random 32-hex-character ID when absent, stores it as request context `request_id`, and returns the chosen value in response `X-Request-ID` (`apps/server/internal/server/middleware/request_context.go:17-32`). Durable assistant requests bind the client-stable ID into `RequestID` and `TraceID`; the same ID plus the same canonical body replays the accepted Turn. A different body under the same idempotency scope is a persistence idempotency conflict. The current assistant HTTP adapter does not give that conflict a dedicated 409 mapping: non-stream returns generic 500, while a stream already opened returns an SSE `error`. This is a frozen compatibility gap, not permission to create a second Turn.

Although an unsigned/basic route can rely on backend generation, a trusted durable request cannot: the ingress must choose or preserve `X-Request-ID` before computing the HMAC, because that exact value is in the signed tuple. The browser may omit it only when the trusted ingress supplies both the request ID and matching attestation.

### Durable identity headers

Durable assistant and typed-agent endpoints require a trusted proxy attestation:

`X-DocReview-Principal-Type`, `X-DocReview-Principal-ID`, `X-DocReview-Organization-ID`, `X-DocReview-Workspace-ID`, `X-DocReview-Identity-Issued-At`, `X-DocReview-Roles`, `X-DocReview-Identity-Signature`.

The HMAC-SHA256 canonical input is the newline-joined tuple:

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

IDs must be UUIDs; principal type is `user` or `service`; issued time may not be more than 30 seconds in the future and must be within configured max age; signature is lowercase hex and compared in constant time; the signed workspace must equal the requested workspace. Source: `apps/server/internal/agent/identity/adapter.go:21-145`, query authentication `agent_runtime_query.go:178-203`, Turn binding `assistant.go:604-664`.

The trusted ingress is a narrow compatibility boundary, not an IdP. It must strip client-supplied identity headers before signing. Missing, malformed, expired, mismatched or untrusted identity fails closed and cannot call a legacy fallback.

Current authentication coverage is asymmetric and must be reproduced until separately remediated: `/api/agent/runs*`, `/api/agent/approvals*`, and durable conversation/message Turn endpoints validate trusted ingress; health, resource list/detail/export/search, assistant capabilities/session CRUD, assistant uploads, and file download do not invoke `identity.Adapter` in their current handlers. Python must not claim those compatibility routes are tenant-secure merely because the durable Runtime is scoped.

## Body, query and path validation

- UUID path parameters return `400 {"error":"... ID 非法"}` before repository access.
- Blank assistant `message` returns `400 {"error":"消息不能为空"}` (the exact domain error text is retained by the implementation).
- `resource_id` is syntactically optional in the Handler DTO, but the current durable repository requires it together with trusted Principal/Workspace. Invalid UUID is `400`; omitted/blank reaches the current generic durable failure mapping (500 non-stream or SSE error after stream start).
- `Last-Event-ID` must parse as an integer `>= 0`, otherwise `400`.
- Run/approval `limit` defaults to 50 and accepts only 1..100; invalid values return `400`.
- Run/approval filters reject unknown statuses with `400`.
- Upload requires multipart field `file`, supported filename, readable content, and max 20 MiB; oversize is `413`, malformed/missing/unsupported input is `400`.

## Error mapping

| Condition | HTTP | DTO |
| --- | --- | --- |
| Invalid UUID, body, query, limit, status, upload, Last-Event-ID | 400 | `{ "error": message }` |
| Missing/expired/invalid trusted identity | 401 | `{ "error": "durable identity is not trusted" }` or route-specific equivalent |
| Trusted identity lacks approval permission or scope | 403 | `{ "error": "审批权限不足" }` or scope equivalent |
| Resource/session/file/approval/run absent | 404 | `{ "error": "...不存在" }` |
| Current version absent for search | 409 | `{ "error": "资源当前版本不存在，无法检索" }` |
| Approval state/idempotency conflict | 409 | `{ "error": "审批状态冲突" }` |
| Upload larger than configured maximum | 413 | `{ "error": "上传文件过大" }` |
| Runtime/pipeline unavailable | 503 | `{ "error": "durable agent runtime is unavailable" }` |
| Non-deterministic durable turn timeout | 503 | `{ "error": "durable turn state is not ready; retry with the same request id" }` |
| Unconfigured repository/provider or unexpected backend error | 500 | `{ "error": route-specific stable message }` |

Current compatibility exceptions that must be captured by tests: Turn request idempotency conflict and missing durable `resource_id` are not mapped to 409/400 after handler validation; they use the generic durable failure path described above. Changing these status codes is a separate public API remediation, not part of behavior-preserving rewrite.

### Route-family error oracle

Every entry below uses `{ "error": "<exact message>" }` unless marked as SSE. Messages not listed are the route family's generic 500 message.

| Route family | 400 | 401/403 | 404/409 | 500/503 |
| --- | --- | --- | --- | --- |
| resources list | - | - | - | `资源存储未配置`; `查询资源列表失败` |
| resource detail | `资源 ID 非法` | - | 404 `资源不存在` | `资源存储未配置`; `查询资源失败`; `查询资源版本失败` |
| resource export | `资源 ID 非法` | - | 404 `资源不存在`; 404 `资源没有可导出的当前版本` | same storage/query failures |
| resource search | `查询参数 q 不能为空`; `资源 ID 非法` | - | 404 `资源不存在`; 409 `资源当前版本不存在，无法检索` | `资源存储未配置`; `查询资源失败`; `查询资源版本失败`; `检索服务未配置`; `检索资源失败` |
| Run list/detail | `limit 必须介于 1 和 100 之间`; `运行状态无效`; `资源 ID 非法`; `运行 ID 非法` | 401 `Agent 查询身份不可信` | 404 `记录不存在` | 503 `Agent 运行查询服务未配置`; 500 `运行记录查询失败` |
| Approval list/detail | invalid limit/status/ID messages use `审批状态无效` / `审批 ID 非法` | 401 `Agent 查询身份不可信` | 404 `记录不存在` | 503 query service unconfigured; 500 `审批记录查询失败` |
| Approval approve/reject | `审批 ID 非法`; `审批理由不能为空`; fallback `审批请求无效` | 401 `审批身份不可信`; 403 `审批权限不足` | 404 `审批不存在`; 409 `审批状态冲突` | 503 `持久化审批服务未配置`; 500 `审批决策失败` |
| Assistant session read | `<会话 ID> 非法` | - | domain `会话不存在` | `查询会话列表失败`; `查询会话失败` |
| Assistant delete | `会话 ID 非法` | - | `会话不存在` | `删除会话失败` |
| Durable Turn before stream starts | `消息内容不能为空`; `资源 ID 非法`; `Last-Event-ID 非法` | 401 `durable identity is required` / `durable identity is not trusted`; 403 `durable workspace scope is not trusted` | no dedicated request conflict mapping | 503 runtime unavailable/not-ready messages; 500 `处理助手请求失败` |
| Durable Turn after SSE starts | validation occurred before 200 | validation occurred before 200 | no HTTP remap | SSE `error` `{code:"assistant_internal_error",message:"助手暂时不可用，请使用相同 request_id 重试。"}` then close |
| Assistant upload | `必须上传文件`; format policy message; `读取上传文件失败` | 401 `durable identity is required` / `durable identity is not trusted` | domain session 404 | 413 `上传文件过大`; 500 `上传文件失败`; 503 `durable identity adapter is not configured` |
| File download | `文件 ID 非法` | - | `文件不存在`; `文件内容不存在` | `文件下载服务未配置`; `查询文件失败`; `读取文件失败` |

The Python adapter may use structured internal error codes, but the public status and DTO mapping above is frozen. It must not expose database URLs, provider credentials, raw tool inputs/outputs, ContextManifest contents, Attempt data, or internal trace indexes in public Run DTOs.

## SSE contract

Content type is `text/event-stream; charset=utf-8`, with `Cache-Control: no-cache`, `Connection: keep-alive`, immediate flush. A persisted event frame has three lines followed by a blank line: `id: <sequence>`, `event: <event name>`, and `data: <one JSON object>`. Persisted sequences are positive. Synthetic transport errors/done frames may reuse the nonnegative reconnect cursor, including `0`.

Legacy assistant event names retained by the public parser are `session_created`, `session_file`, `message_started`, `message_delta`, `message_completed`, `task_suggestion`, `error`, `turn_state`, and `done`. Durable mapping is:

| Persisted event | Public event | Data | Terminal |
| --- | --- | --- | --- |
| `turn.accepted`, `turn.running`, `run.queued` | `turn_state` | persisted payload | no |
| `assistant.message` | `message_completed` | `{ "message": Message }` (wrapper added if absent) | no |
| `turn.waiting_input`, `turn.waiting_approval` | `turn_state`, then `done` | status payload, then `{}` | yes |
| `turn.succeeded` | `done` | `{}` | yes |
| `turn.failed`, `turn.cancelled` | `error`, then `done` | `{code:"assistant_internal_error",message:"持久化轮次结束，但没有可恢复的结果"}`, then `{}` | yes |
| persisted `done` | same event | `{}` | yes |

Event type names containing CR/LF or blank are rejected. Invalid/empty persisted payloads are rendered as `{}`. The server writes only events with sequence greater than `Last-Event-ID`; a reconnect repeats the same body and `X-Request-ID`. Client `assistant-stream.ts:150-237` tracks the greatest received ID, retries up to a bounded count, and treats a missing `done` as resumable interruption. A broken observer/SSE socket does not roll back Turn acceptance or outcome.

Two terminal frames generated from one persisted waiting/failure/cancellation event reuse that event's same `id`. If pipeline execution fails after HTTP 200 is committed, the adapter emits one `error` with `id` equal to the caller's `Last-Event-ID` cursor and message `助手暂时不可用，请使用相同 request_id 重试。`, then closes without guaranteeing `done`; the frontend treats the error as terminal. A reconnect after a persisted terminal cursor may receive a synthetic `done` at that cursor even when no newer persisted event exists.

## CORS

Configured origins are exact string matches. In production at least one origin is required; wildcard, non-HTTP(S), credential-bearing, or path/query/fragment origins are rejected by configuration. Allowed origins receive `Access-Control-Allow-Origin` equal to the request origin, `Vary: Origin`, methods `GET, POST, PATCH, DELETE, OPTIONS`, headers `Content-Type, X-Request-ID`, and exposed header `X-Request-ID`. Disallowed non-empty-origin preflight returns `403`; no wildcard is emitted. Source: `apps/server/internal/server/middleware/cors.go:11-50` and config validation `apps/server/internal/config/config.go:180-225`.

Current CORS allow-headers do not include `Last-Event-ID` or the `X-DocReview-*` identity headers. Identity headers are expected to be stripped/added at the trusted ingress, not sent by browser code. A cross-origin browser reconnect that explicitly sends `Last-Event-ID` therefore requires same-origin proxying or an ingress/CORS policy outside this backend; widening the backend allowlist would be a separately reviewed behavior/security change.

## Compatibility notes

`GET /api/assistant/sessions` and upload/download are compatibility responsibilities of the assistant/file services, not the old Agent graph. `/api/tasks`, `/api/approvals`, `/api/jobs`, task-suggestion confirmation, and `/api/resources/:id/task-context` are intentionally not registered and must not be reintroduced during Python rewrite without a separate product/API change.
