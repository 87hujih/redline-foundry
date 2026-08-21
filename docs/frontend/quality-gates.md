# 前端测试与交付验收约束

**状态：** Draft  
**目标：** 用可重复检查证明前端满足业务、安全、恢复和可访问性要求。

## 1. 测试分层

| 层级 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| Unit | SSE parser、schema、消息合并、状态映射、格式化 | 浏览器布局 |
| Component | 表单、Dialog、消息类型、loading/error/empty 状态 | 真实后端持久化 |
| Contract | Mock response 与冻结 DTO/SSE fixture 一致 | 后端内部模型 |
| E2E | 上传、选择、对话、重连、Run、Approval、下载 | 穷举所有视觉组合 |
| Visual/A11y | 视口布局、键盘、焦点、对比度和语义 | 领域事务正确性 |

测试只 mock 网络边界，不 mock 被测业务函数本身。关键 E2E 必须能在受控测试身份和隔离 Workspace
运行，不能连接生产数据库或使用生产凭据。

## 2. 必测单元行为

### API 与 schema

- `{ "error": string }`、204、Blob 和非 JSON 错误响应。
- 已知 message kind 的合法与非法 payload。
- 未知 message kind 的兼容占位。
- RFC3339 时间、UUID、nullable selection 和空数组。

### SSE

- 一个 chunk 多帧、一个 frame 多 chunk、CRLF/LF 和末尾残片。
- 合法 `id/event/data`、非法 JSON、空 event 和未知 event。
- `message_completed` wrapper 正常化。
- 重复 ID、乱序 ID，以及同 ID 的 terminal `error` + `done`。
- 未收到 terminal 的连接关闭触发有限重连。
- 重连保持相同 request ID/body 并发送最大 `Last-Event-ID`。
- terminal、route change 和手动 abort 后资源全部释放。

### 数据合并

- message ID 去重并按 `sequence_no` 排序。
- pending message 被持久化 message 替换。
- Resource PUT 失败保持原 selection。
- 上传失败不清空已有 selection。
- 切换 Resource 不改变旧 Run 展示的 Resource 快照。

## 3. 必测端到端流程

1. 上传支持的文档，创建 Session，选择返回的 Resource，发送消息并收到 `done`。
2. 刷新页面，恢复 Session、messages 和 resource selection。
3. SSE 中途断开，用相同 request ID/body/cursor 恢复且不重复消息或 Turn。
4. 上传不支持格式、超过限制、解析失败，页面给出可恢复反馈。
5. 在已有 Session 上传新文档，成功切换；失败保持旧选择。
6. 浏览 Resource，执行检索、Markdown 导出和文件下载。
7. 查看 Run 列表/详情，状态、Step、Tool Call 和 Approval 关联正确。
8. Owner/Admin 填写 reason 后批准或拒绝；重复决策显示 409 并刷新。
9. 401 恢复登录、403 无权限、404 不可用、503 暂时不可用均保留安全状态。
10. 跨 Workspace Session/Resource/File/Run/Approval 不可访问，页面不泄露对象存在性。

## 4. 组件状态覆盖

以下组件至少覆盖 default、hover、focus-visible、active、disabled、loading、error：

- Button、IconButton、Link。
- TextField、Textarea、Select、FileUpload。
- Dialog、Drawer、Tooltip、Toast。
- SessionListItem、Message、ResourceSelector。
- StatusBadge、DataTable/List、ApprovalDecisionForm。

Snapshot 不能作为交互正确性的唯一证据。测试必须断言用户可见结果、焦点和网络调用。

## 5. 可访问性门禁

- 自动检查不得出现严重或高优先级 a11y violation。
- 全部关键流程可只用键盘完成。
- 每个页面有唯一 H1、合理 landmark 和 skip link。
- Icon-only button 有 accessible name；装饰图标对辅助技术隐藏。
- Dialog 无焦点逃逸，关闭后焦点返回触发器。
- 失败表单聚焦首个错误或错误摘要；inline error 与字段关联。
- 状态不只靠颜色表达；normal text 对比度至少 4.5:1。
- `prefers-reduced-motion` 下没有非必要位移动画。
- 200% 文本缩放和 375px 视口仍可完成核心流程。

## 6. 视觉与响应式门禁

Playwright 截图至少覆盖 375x812、768x1024、1024x768、1440x900，并检查：

- Assistant 有/无消息、上传中、SSE 运行中和错误状态。
- Resource/Run/Approval 的列表、空状态和详情。
- Drawer、Dialog、长标题、长错误、长 UUID 和 Markdown 内容。
- 无页面级横向滚动、遮挡、布局跳动和按钮文字溢出。
- Sticky header/composer 不遮挡内容或键盘焦点。

视觉基线只在设计决策明确变化时更新；不得用批量更新截图掩盖回归。

## 7. 性能预算

首版不冻结容易失真的绝对 bundle 数字，但冻结以下行为门槛：

- 按顶级 route 拆分代码；初始 Assistant 页面不加载 Run/Approval 详情代码。
- 不引入未使用的完整图标包、编辑器或图表库。
- 页面加载和异步内容预留稳定空间，目标 CLS < 0.1。
- 输入、点击和滚动不执行同步重计算；交互反馈目标在 100ms 内出现。
- 发现长列表性能问题时先提供 Profiler/Performance trace，再决定分页或虚拟化。
- 上传与 SSE 不把完整文件或无限事件历史长期留在内存。

## 8. 安全门禁

构建产物和浏览器存储中不得出现：

- trusted-ingress HMAC secret 或 `X-DocReview-*` 签名事实。
- Provider key、数据库 URL、内部服务地址和生产凭据。
- 文档正文、用户消息或原始 Tool payload 的遥测副本。

还必须验证：

- Markdown/XSS 恶意 fixture 不执行脚本、事件属性或危险 URL。
- 外链不能获得 opener。
- Cookie 认证写请求具有约定的 CSRF 防护。
- 客户端伪造 identity header 会在 ingress 被剥离。
- Source map 发布策略符合环境要求，不暴露 secret 或内部路径。

## 9. CI 合并门禁

当前 `.github/workflows/frontend.yml` 已自动执行 TypeScript typecheck、unit/contract tests、
production build、high severity dependency audit 和 Chromium Playwright E2E。以下完整目标清单中，
format check、lint 和 automated accessibility checks 尚未接入，发布前必须人工检查，后续引入对应
工具时再升级为 required checks，不得把未执行项标记为已通过。

前端功能 PR 合并前必须通过：

```text
format check
lint
TypeScript typecheck
unit/component tests
production build
contract fixture tests
critical Playwright E2E
automated accessibility checks
```

涉及 SSE、认证、Resource selection、Approval 或上传的变更，必须增加对应失败路径测试。
只修改文案或静态样式时可缩小测试范围，但仍需 typecheck、相关组件测试和目标视口截图。

## 10. 发布验收清单

- [ ] 浏览器只通过 protected ingress 访问 `/api`。
- [ ] FastAPI 未直接暴露，所有兼容路由的生产隔离方案已确认。
- [ ] staging 使用真实上传、Tika、Provider 和 durable worker 完成主链路。
- [ ] SSE 断网、刷新、重复恢复和 terminal 行为通过。
- [ ] Workspace 越权负向测试通过。
- [ ] Approval 权限和状态冲突测试通过。
- [ ] 关键视口、键盘、屏幕阅读器和 reduced motion 验收通过。
- [ ] 前端 release、错误监控和 backend request ID 可关联。
- [ ] 回滚到上一前端静态版本不要求数据库或 API 回滚。
