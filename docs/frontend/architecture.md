# 前端架构与工程约束

**状态：** Draft  
**目标：** 规定首个 Web 前端的技术边界、代码组织和状态所有权。

## 1. 基本假设

- 交付目标是浏览器端业务应用，暂不需要 SEO、服务端渲染或公开营销页面。
- 后端保持独立 FastAPI 服务，前端不得复制业务授权和持久化逻辑。
- 推荐基线为 React、TypeScript 和 Vite；改变框架前必须提交 ADR，说明明确收益和迁移成本。
- 包管理器、Node 版本和依赖准确版本在创建前端工程时一次性冻结并提交 lockfile。
- 前端目录使用仓库根目录下的 `frontend/`，不混入 `src/docreview/`。

## 2. 最小依赖边界

允许的首批基础能力：

| 能力 | 约束 |
| --- | --- |
| 路由 | 使用声明式客户端路由，业务详情页必须有稳定 URL |
| 服务端状态 | 使用统一 Query Cache；不得复制到全局 Store |
| 表单 | 简单表单使用受控或原生表单；复杂校验出现后再引入表单库 |
| 运行时校验 | API 和 SSE 边界使用 schema 校验，组件内部使用静态类型 |
| 图标 | 只使用一个已选定的 SVG 图标库，禁止 Emoji 充当功能图标 |
| 样式 | 使用 design token；业务组件禁止直接写颜色常量 |
| 测试 | 单元/组件测试与真实浏览器 E2E 分层 |

不得为了“以后可能需要”预先引入微前端、SSR、GraphQL、离线同步、多主题引擎或通用工作流引擎。

## 3. 目录和依赖方向

```text
frontend/
  src/
    app/                    # 启动、路由、全局 Provider、错误边界
    api/                    # HTTP client、DTO schema、错误映射
    auth/                   # 浏览器登录态；不包含 HMAC secret
    features/
      assistant/
      sessions/
      resources/
      runs/
      approvals/
      files/
    components/             # 无业务所有权的通用组件
    styles/
      tokens/               # primitive -> semantic -> component
    test/
  e2e/
```

依赖方向固定为：

```text
app -> features -> api
app -> features -> components
features -> components
components -> styles
```

- `api` 不得依赖 React 组件。
- `components` 不得导入具体 feature。
- feature 之间不得读取彼此内部文件；跨 feature 交互通过公开入口或路由完成。
- 单次使用的逻辑留在 feature 内，不为它创建通用抽象。

## 4. 状态所有权

| 状态 | 唯一所有者 |
| --- | --- |
| Session、Resource、Run、Approval 列表与详情 | Query Cache |
| 当前路由对象 ID、筛选条件 | URL path/query |
| 输入框草稿、弹窗开关、展开状态 | 页面或组件本地状态 |
| 当前 SSE 连接、cursor、request ID | 单次 Turn controller |
| 当前 Resource 选择 | 后端 selection 为真相；成功响应后更新缓存 |
| 登录身份 | auth adapter；业务组件只读取公开 session |

禁止把后端对象长期复制到 localStorage。可持久化的客户端数据仅限无敏感信息的 UI 偏好和未发送草稿，
且必须有明确清理规则。Request ID 和 SSE cursor 只在其 Turn 恢复周期内保存。

## 5. 路由约束

建议的稳定路由：

```text
/assistant/:sessionId?
/resources
/resources/:resourceId
/runs
/runs/:runId
/approvals
/approvals/:approvalId
```

- 列表筛选、分页和搜索词进入 URL query，刷新和后退后必须可恢复。
- 删除、批准、拒绝等写操作不得由 GET 路由触发。
- 详情页刷新时必须能独立加载，不依赖从列表页传入的内存对象。
- 路由切换后将焦点移到主内容标题；后退时恢复列表滚动和筛选状态。

## 6. 错误与可观测性

- 所有 HTTP 错误统一解析 `{ "error": string }`，未知响应显示稳定的通用消息。
- 用户界面不得显示堆栈、内部 Provider 信息、原始 Tool payload 或身份签名。
- 前端错误事件至少记录 route、operation、HTTP status、backend `X-Request-ID` 和前端 release；
  不记录文档正文、用户消息、认证信息或上传文件内容。
- 401 交给 auth adapter；403 显示无权限；404 显示对象不可用；409 显示状态冲突并刷新；
  413 显示上传大小错误；5xx/503 提供重试路径。
- React 列表必须使用后端稳定 ID 作为 key，禁止对动态列表使用数组下标。
- 性能优化必须以浏览器测量或 React Profiler 证据为前提，不做猜测性 memoization。

## 7. 环境配置

- 浏览器只接收可公开配置，例如 API 同源前缀、release ID 和 feature flag。
- HMAC secret、Provider key、数据库 URL 和 `X-DocReview-*` 身份事实绝不能进入前端环境变量或 bundle。
- 开发、测试、staging、production 使用同一构建产物优先；运行时注入环境差异。
- API 默认使用相对路径 `/api`，避免组件拼接 host。

## 8. 变更规则

- 新增依赖必须说明用途、bundle/安全影响以及为何现有能力不能满足。
- 新增通用组件必须至少有两个明确使用点；否则留在 feature 内。
- 后端 DTO 或事件发生变化时，先更新 `api-integration.md` 和契约测试，再改组件。
- 不得在同一个功能 PR 中顺带重排无关目录、替换样式体系或升级全部依赖。

