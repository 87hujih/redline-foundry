# DocReview Frontend

React + TypeScript + Vite 实现的 DocReview 文档审阅工作台。

## 功能

- Assistant 会话、文档上传、Resource 选择和持久化 SSE 消息。
- Session 历史与删除。
- Resource 列表、详情、文内检索和 Markdown 导出。
- Agent Run 列表、筛选、步骤、工具调用和 Finding 详情。
- Approval 列表、筛选、详情、批准与拒绝。
- 上传文件下载。

## 本地运行

```powershell
npm install
npm run dev
```

开发服务器位于 `http://127.0.0.1:5173`，并将 `/api` 代理到
`http://127.0.0.1:8080`。FastAPI 的持久化接口仍需要 trusted-ingress identity；
前端不会也不能生成 HMAC 身份签名。连接真实后端时，应让本地 protected ingress 与前端同源，
或在受控开发环境为 Vite 上游配置服务端签名代理。

公开浏览器配置：

```text
VITE_API_BASE=/api
```

## 校验

```powershell
npm run typecheck
npm test
npm run build
npm run test:e2e
```

E2E 使用浏览器路由 Mock，不需要数据库、Provider、Tika 或 trusted-ingress secret。

GitHub Actions 会在前端或前端约束文档变更时使用 Node.js 22 执行 typecheck、单元/契约测试、
生产构建、high severity 依赖审计和 Chromium E2E。E2E 失败时会保留 7 天的 Playwright 诊断产物。
Lint、format check 和自动化可访问性检查尚未配置，不能视为已经通过的 CI 门禁。

## 部署

生产环境将 `dist/` 作为静态资源托管，并将同源 `/api` 转发到 protected ingress。
SPA 路由需要 fallback 到 `index.html`。不得将 FastAPI 端口、HMAC secret 或
`X-DocReview-*` 身份头暴露给浏览器。
