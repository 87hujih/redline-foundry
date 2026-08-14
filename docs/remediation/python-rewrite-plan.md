# Python + LangGraph 重写计划

## 证据基线

本计划和 Phase 0 scope 以 `G:\gofile\Agent_Project` 在 2026-08-12 的文件系统状态为证据基线。
源仓库当前有用户未提交改动；本次只读这些改动，不把它们回退或格式化。证据优先级为：

1. `apps/server/cmd/server` 的实际构造和启动调用；
2. `apps/server/internal/server/router/router.go` 的实际注册；
3. `apps/web` 的可达页面和 API 客户端；
4. Docker/Compose、CI、发布和本地启动脚本的实际调用；
5. 一次 durable 请求完成所需的 repository、Worker、Tool、Policy、Approval、Commit、Outbox 和 Projection 闭包；
6. 架构文档、测试和历史模块只能解释契约，不能单独扩大 active scope。

不得将“前端库中仍存在的不可达调用”“未注册 Handler”“旧设计文档中的计划”“目录/package 的完整内容”当作生产使用证据。

## Phase 0：范围和契约冻结

当前阶段只完成以下文档：

- `docs/remediation/python-active-scope.md`：逐条 Go 入口、真实调用方、具体类型/方法、行为、Python 目标、测试、处置和行号证据；
- `docs/remediation/api-contract.md`：当前已注册 REST/SSE、安全和错误契约；
- `docs/remediation/persistence-contract.md`：持久化事实、状态机、SQL、事务、幂等和隔离契约；
- `docs/remediation/status.md`：门禁、验证、风险和下一任务。

Phase 0 不创建 Python 业务模块，不连接数据库，不运行 migration/backfill/DDL，不改 Go 或部署配置。完成后必须暂停，等待用户确认。

## 生产重写的目标边界

Python 目标保留一个受保护的 FastAPI/Uvicorn 入口、配置 fail-closed 校验、CORS、健康检查、资源/文件/助手兼容 API、durable Turn Pipeline、Runtime Worker、Projection Worker、trusted-ingress 身份、Workspace/Resource Policy、ToolRuntime、EvidenceSet 检索、Canonical Document/Patch/Commit、Approval continuation、Outbox 和 SSE replay。

LangGraph 只承担 `UnderstandGoal -> AssembleContext -> DecideNextAction -> Act -> Observe -> RenderOutcome` 的有界图编排、类型化 state、interrupt/checkpoint 和图层事件。业务状态、lease、预算、重试、幂等、授权、事务和公开投影仍由项目服务与 PostgreSQL 负责。

## 分阶段路线

### Phase 1：Python 基础服务

建立 Python 3.13、FastAPI、Pydantic、配置、日志、依赖生命周期、健康检查、精确 CORS、trusted-ingress adapter、API 错误 envelope 和不连接数据库的契约测试。保持现有 Go 服务为生产唯一写入方。

### Phase 2：只读兼容和存储适配

按 active scope 迁移资源、当前版本、搜索、文件下载、会话/消息查询和 durable Run/Approval 查询。使用 Psycopg 3 参数化 SQL，复刻现有 SQL 过滤、排序、NULL 和错误映射；不执行 SQL migration 或 backfill。

### Phase 3：Durable Runtime

迁移 TurnCoordinator、Run/Step/Attempt 生命周期、`FOR UPDATE SKIP LOCKED` claim、lease/heartbeat/recovery、retry/cancel/budget 和事务性 Outbox。先用注入 fake/store 做失败路径测试，再在获得授权的 `_test` 数据库验证 round trip。

### Phase 4：LangGraph 编排和工具

实现有界 GraphState、Decision validator、ContextManifest、ToolRuntime、Policy/Approval boundary、EvidenceSet、Canonical Patch Validator/Committer 和模型 gateway。所有外部副作用经过幂等 ToolRuntime/Committer；LangGraph checkpoint 不替代业务事实。

### Phase 5：API/Projection parity

将 stream/non-stream 适配到同一 TurnCoordinator；以持久化事件生成兼容 DTO/SSE，验证断连重连、重复 request、Last-Event-ID、terminal event 和 projection replay。Python/Go 禁止对同一请求双重产生 Tool/Approval/Commit/Outbox 写入。

### Phase 6：离线对账和入口 canary

先 read-only、离线 canonical JSON/hash 对账，再执行受保护入口的单写 durable canary。验证 Worker lease、request/tool/commit 幂等、Approval approve/reject/repeat、Patch conflict、Outbox replay、跨 Workspace 隔离、检索 profile 和 canonical projection。

### Phase 7：切流和收缩

只有在授权数据库、protected ingress、数据 reconciliation、公开行为、回滚、评测和 legacy-removal evidence gate 全部通过后才切流。代码删除和 Schema contract 必须分开审查；不恢复 `AGENT_RUNTIME_MODE=legacy|shadow` 作为当前版本回滚机制。

## 延后或排除

Go `cmd/migrate` 在过渡期继续使用；最终若要移除 Go 工具链，只迁移 ledger/checksum/advisory-lock 语义并复用原 SQL。`agent-runtime-ops` 和 `agent-eval` 先保留 Go，分别作为运维和 CI 基线。未注册的 legacy Task/Job/Approval、旧 Planner/Reviewer/Editor/Executor、legacy/shadow Router、无活跃调用证据的 CLI 与仅服务它们的 helper/repository/test 不进入首批 Python 业务重写。

## 约束和暂停条件

- 不改变公开 API、DTO、SSE、错误码、幂等键、租户边界或 SQL 行为。
- 不通过配置重新暴露 legacy fallback，不复制一套并行事实来源。
- 未获得用户确认前不开始 Phase 1 Python 业务实现。
- 数据库安全条件不满足时，只运行 database-free/static 验证并记录跳过原因。
