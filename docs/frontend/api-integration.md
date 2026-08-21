# API、认证与流式协议约束

**状态：** Draft  
**原则：** 后端公开契约和持久化事实是唯一数据真相，前端不得猜测或补造领域状态。

## 1. 契约来源

实现前必须同时检查：

- `docs/remediation/api-contract.md`
- `docs/remediation/cross-session-upload-resource-selection-contract.md`
- `src/docreview/api/routes/` 中实际注册路由

当前 FastAPI 禁用公开 OpenAPI。前端在 `api/` 中维护最小 TypeScript DTO 和运行时 schema；
schema 只描述实际消费字段，不把任意 JSON 强制断言成完整领域对象。

契约冲突处理顺序：实际受测路由行为 > 最新专项冻结契约 > 通用 API 契约 > 前端假设。
发现冲突必须补后端契约或测试，不得在 UI 内按环境猜测响应形态。

## 2. 浏览器认证边界

浏览器不得生成或发送以下 trusted identity header：

```text
X-DocReview-Principal-Type
X-DocReview-Principal-ID
X-DocReview-Organization-ID
X-DocReview-Workspace-ID
X-DocReview-Identity-Issued-At
X-DocReview-Roles
X-DocReview-Identity-Signature
```

浏览器也不得获得 shared HMAC secret。正式请求必须经过 protected ingress：

```text
Browser session -> protected ingress/auth -> signed upstream request -> FastAPI
```

- Ingress 必须删除客户端传入的同名身份头，再根据已验证身份重建。
- 前端业务状态不得接受 URL、localStorage 或响应正文提供的 Workspace ID 作为授权事实。
- 若使用 Cookie 登录态，写操作必须完成 CSRF 方案；若使用 Bearer，token 只交给 auth adapter。
- FastAPI 当前存在鉴权覆盖不对称；在后端修复或网关封闭所有 `/api/*` 前不得生产发布。

## 3. HTTP client

- 所有请求经唯一 `apiClient`，业务组件不得直接调用 `fetch`，SSE transport 除外但仍复用公共配置。
- 默认使用同源相对路径；默认 `Accept: application/json`，上传除外。
- JSON 错误只依赖 `{ "error": string }`，不得依赖未冻结字段。
- 读取响应中的 `X-Request-ID` 并关联错误日志。
- 不自动重试写请求。只有契约明确幂等且能复用完全相同 request ID/body 时才允许恢复。
- 204 响应不得按 JSON 解析；文件和 Markdown 导出按 Blob/文本响应处理。
- 时间戳在 API 层保留 RFC3339 字符串，在展示层本地化。

## 4. Request ID 与幂等

- 每次新 Turn 在客户端创建稳定的 request ID。
- 同一次 Turn 重试必须复用相同 request ID、相同 session path 和 canonical body。
- 用户编辑 message 或切换 resource 后属于新 Turn，必须生成新 request ID。
- request ID 不得在不同会话或不同操作之间复用。
- 已出现不确定超时或 SSE 断线时，禁止“换一个 ID 再试”，否则可能创建重复事实。

普通查询和非 durable 操作可依赖 ingress/backend 生成 request ID；持久化 Turn 的 request ID
必须由前端 transport 与 ingress 共同保持稳定。

## 5. Session 与 Resource 工作流

### 打开已有 Session

1. 请求 Session 详情和 messages。
2. 请求 `GET /api/assistant/sessions/{id}/resource-selection`。
3. selection 为 `null` 时禁用发送并引导选择或上传文档。
4. selection 非空时，以该值初始化 composer scope。

### 选择 Resource

1. Resource 选择器只展示 `source_type == "upload"` 的可选资源。
2. 调用 `PUT .../resource-selection`，body 为 `{ "resource_id": UUID }`。
3. 仅在 200 后更新 Query Cache 和选中态。
4. 失败时保留原选中值；不能 optimistic commit 后不回滚。

### 上传

- accept 和提示从 `/api/assistant/capabilities` 读取。
- 前端可提前拦截明显不支持的扩展名和超过 20 MiB 的文件，但后端结果仍是权威。
- 新会话使用 `/api/assistant/conversations/files`；已有会话使用
  `/api/assistant/sessions/{id}/files`。
- `resource: null` 是合法解析失败结果，必须展示 `error_message`，不能假设上传成功即产生 Resource。
- 上传到已有 Session 成功后，新的 Resource 成为当前选择；失败不得清空旧选择。

### 发送消息

- body 始终显式提供非空 `message` 和当前 `resource_id`。
- 旧 Run/Turn 的 Resource 以其持久化快照为准，不根据当前 Session selection 重标记。
- 前端不得实现多 Resource body 或 selection fallback。

## 6. SSE transport

流式 endpoint 使用 POST，禁止用原生 `EventSource`。使用 `fetch` + `ReadableStream`，解析：

```text
id: <non-negative integer>
event: <event name>
data: <one JSON object>

```

必须支持的事件：

| event | 客户端动作 |
| --- | --- |
| `session_created` | 记录新 Session 并更新 URL |
| `session_file` | 合并文件消息 |
| `message_started` | 创建临时显示状态，不作为持久化消息真相 |
| `message_delta` | 仅更新当前临时内容 |
| `message_completed` | 以 message ID/sequence 合并持久化消息 |
| `task_suggestion` | 按受支持消息类型展示 |
| `turn_state` | 更新紧凑运行状态 |
| `error` | 结束当前 transport，提供同 ID 恢复或明确失败 |
| `done` | 正常终止并刷新相关 projection |

### Parser 约束

- 按空行分 frame，兼容网络 chunk 在任意字节边界断开。
- `data` 必须先 JSON parse 再运行 schema 校验，禁止字符串拼接解析。
- 只接受合法、非空 event name；未知 event 记录后忽略，不得导致整个会话崩溃。
- 跟踪收到的最大 event ID；重复或更小 ID 不重复应用副作用。
- 同一个持久化 terminal event 可能产生相同 ID 的 `error/done` 两帧，去重键必须包含 event name。
- 连接关闭但未收到 `done` 或 terminal `error` 时视为可恢复中断。

### 重连约束

- 在有限次数内重连，并使用退避；不得无限后台循环。
- 重连复用相同 endpoint、request ID、body，并携带最大 `Last-Event-ID`。
- 当前 FastAPI CORS allow-header 不包含 `Last-Event-ID`；浏览器必须通过同源代理或由 protected
  ingress 明确处理该 header，不得通过前端修改请求规避预检。
- 页面隐藏、离线或用户离开 Session 时暂停；回到页面后由明确 controller 决定恢复。
- Abort 只关闭浏览器连接，不代表后端 Turn 被取消，UI 不得显示“任务已取消”。
- 收到 terminal 后释放 reader、AbortController 和 timer，避免跨会话事件污染。

## 7. 消息合并

- 排序依据为 `sequence_no`，相同 message ID 只能有一个持久化实例。
- 临时消息使用前端命名空间 ID，收到持久化消息后替换而非追加。
- `payload` 是不可信 JSON；按 `kind` 校验已知字段，未知字段不能用于授权或路由。
- 已知 kind：`text`、`task_suggestion`、`task_created`、`task_status`、`session_file`、`system`。
- 未知 kind 使用兼容占位；不得直接把对象序列化成富文本 HTML。

## 8. 状态码策略

| HTTP | 行为 |
| --- | --- |
| 400 | 显示后端错误，聚焦相关输入；不自动重试 |
| 401 | 进入统一认证恢复；保留未发送草稿 |
| 403 | 显示无权限；不尝试更换 Workspace 绕过 |
| 404 | 显示不可用；跨 Workspace 与不存在保持同一表现 |
| 409 | 显示冲突并刷新服务端状态 |
| 413 | 显示上传过大并保留 Session |
| 500 | 显示通用失败和 request ID；Turn 按幂等规则恢复 |
| 503 | 显示暂时不可用；只对允许的操作提供重试 |

当前 Turn 幂等冲突是冻结的兼容例外：非流式请求进入通用 500，流式请求在连接打开后进入 SSE
`error`，不能按通用 409 分支处理，也不能因此创建新的 request ID。

## 9. 下载与内容安全

- 文件名优先使用 `Content-Disposition`，并对展示文本进行转义。
- 文件下载和 Resource export 不在页面中注入执行。
- Markdown 渲染禁止 raw HTML，或使用严格 allowlist sanitizer。
- 外链使用 `rel="noopener noreferrer"`；不自动加载文档中的远程图片或追踪资源。
- Blob URL 使用完成后及时 `URL.revokeObjectURL`。
