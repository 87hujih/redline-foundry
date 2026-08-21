# 项目状态

## 当前定位

这是一个独立的 Python 3.13 服务，使用 FastAPI、Pydantic、Psycopg 3 和 LangGraph。
LangGraph 只负责有界编排；Run、Step、Attempt、Tool、Approval、Commit、Outbox 和
Projection 仍由服务层和 PostgreSQL 持久化。

## 已具备的能力

- FastAPI 应用装配、健康检查、精确 CORS、请求 ID 和 trusted-ingress 校验。
- Workspace/Resource/Principal 隔离，以及资源、文件、Assistant、Run、Approval 查询接口。
- Session 上传 Resource 当前选择接口；成功上传原子切换选择，Turn/Run 仍保存显式单 Resource
  快照，选择状态不会晚绑定到既有运行。
- 非流式和 SSE Turn API，共享 durable acceptance/outcome、幂等键和 Last-Event-ID 重放语义。
- LangGraph 边界、Context Manifest、Evidence 检索、ToolRuntime、Approval continuation、
  Canonical Document/Patch/Commit、Outbox 和 Projection 的类型化接口。
- 无数据库的失败路径、SQL 形态、状态机、序列化、静态装配和安全边界测试。

## 当前验证边界

2026-08-19 的受控本地验证已完成 PostgreSQL 17、pgvector、Tika 3.3、真实 Provider、受保护
ingress、上传/规范投影/检索/Agent 投影的 round-trip。默认测试套件仍由数据库 fuse 隔离测试
数据库，因此数据库集成测试缺少 `TEST_DATABASE_URL` 时保持跳过。staging、容量、故障注入和
回滚演练仍需在对应受控环境单独执行。

## 不变量

- 每个持久化请求只有一个 request scope 和幂等 key；相同 key 的不同 canonical body 必须报冲突。
- 所有查询和写入都在 SQL 边界执行 workspace/resource 授权，不能依赖内存过滤。
- Worker 更新必须带 owner、lease expiry 和 generation 条件；过期 worker 不能覆盖新 claimant。
- 公开 DTO/SSE 只能读取 projection，不能暴露原始 state、context、tool payload 或凭据。
- Commit 必须在事务中重新校验版本、节点 hash、证据和授权，并与 Outbox 一起提交或回滚。

## 下一步

下一步是为 CI 提供独立 `TEST_DATABASE_URL`，再执行 lease fencing、并发切换、容量、staging
和回滚演练；测试数据库不得复用当前开发或生产连接。
