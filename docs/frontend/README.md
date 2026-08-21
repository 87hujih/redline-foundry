# 前端开发约束索引

**状态：** Accepted for v1  
**适用范围：** DocReview Agent Web 前端从 0 到首个可交付版本  
**后端事实来源：** `docs/remediation/api-contract.md`、
`docs/remediation/cross-session-upload-resource-selection-contract.md` 和当前 FastAPI 路由

## 目的

本目录冻结前端开发必须遵守的边界，避免页面、接口和部署由各实现者自行推断。
后续前端代码、设计稿、测试和代码评审都必须以这些文档为验收依据。

## 文档

1. [架构与工程约束](./architecture.md)
2. [UI、交互与设计系统约束](./ui-ux.md)
3. [API、认证与流式协议约束](./api-integration.md)
4. [完整 API 参考](./api-reference.md)
5. [测试与交付验收约束](./quality-gates.md)

## 优先级

发生冲突时按以下顺序处理：

1. 安全、Workspace 隔离和后端公开契约。
2. 数据正确性、请求幂等和 SSE 恢复语义。
3. 可访问性和用户操作可恢复性。
4. 本目录的工程与视觉约束。
5. 页面局部实现偏好。

任何人不得通过前端兼容逻辑绕过 1 至 3 项。发现文档与后端行为不一致时，先停止相关功能合并，
补充契约决策后再修改实现。

## 已冻结的产品范围

首个版本一次性交付全部公开功能，是面向工作用户的文档审阅工作台，不是营销站点。顶级功能域包括：

- Assistant 会话、消息和文档上传。
- 当前会话的单 Resource 选择。
- Resource 列表、详情、检索和导出。
- Agent Run 列表与详情。
- Approval 列表、详情、批准和拒绝。
- 已上传文件下载。

后端当前没有公开接口的能力不得制作成可操作控件，包括会话重命名、取消 Run、修改
`web_search_enabled`、批量审批和多 Resource Turn。

## 已确认的实现决策

- v1 完成上文全部功能，不拆分功能型 MVP。
- 前端不提供注册、登录、退出或用户切换界面。
- 前端与 `/api` 同源，浏览器只发送业务请求。
- protected ingress 使用部署环境提供的固定可信工作区身份为受保护 API 签名；HMAC secret
  永远不进入浏览器。该固定身份必须具有 Approval 所需的 owner/admin 权限。
- 视觉采用高密度、低动效的编辑审阅台风格，以墨黑、纸张白为基础，并使用珊瑚红、青绿和金色
  区分操作与状态；v1 只交付亮色主题。

## 上线前未决项

以下事项不阻塞静态页面和 Mock 开发，但阻塞真实环境发布：

- protected ingress 固定身份的密钥托管、轮换和环境级 Workspace 配置。
- 正式品牌名称与 Logo；当前 `DOCREVIEW` 字标是产品内临时字标。

未决项只能以 feature flag、Mock 或明确的环境适配层隔离，不能在组件中散布临时判断。

## 本地真实联调

`frontend/vite.config.ts` 的 `/api` 代理会从仓库根目录 `.env` 读取
`AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET`，并仅在 Vite 服务端生成固定开发身份签名。默认身份与
本地 bootstrap 数据一致，可用 `DOCREVIEW_FIXED_PRINCIPAL_ID`、
`DOCREVIEW_FIXED_ORGANIZATION_ID`、`DOCREVIEW_FIXED_WORKSPACE_ID` 和
`DOCREVIEW_FIXED_ROLES` 覆盖。上述值不得使用 `VITE_` 前缀，也不得在前端代码中读取。
