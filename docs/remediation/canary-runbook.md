# Python 服务发布与回滚手册

本手册只描述 Python 服务的受保护发布流程。它不创建数据库连接、不生成虚假证据，也不
授权绕过人工变更审批。所有外部验证都必须在已批准的 staging 环境执行。

## 发布前置条件

发布负责人必须确认以下条件均有可追溯 artifact：

- 数据库 fuse、事务回滚、并发 claim、幂等冲突和 checkpoint 重启测试通过；
- Runtime worker、Outbox/Projection worker、SSE 重连和 Approval continuation 具备单实例及
  多副本 lease/fencing 证据；
- Provider、Tika、文件存储和 protected ingress 的超时、限流、身份 scope 与失败路径已验证；
- workspace/resource ownership、canonical version/node hash、Run、Commit、Outbox 和
  Projection 对账为零差异；
- queue age、lease、数据库连接池、SSE、Provider、Outbox/Projection lag 的容量和告警门槛已确认；
- 上一版本回滚演练不会删除持久化事实，且已接受请求可以排空或继续恢复。

## 单写 Canary

1. 生成固定 release artifact，绑定 Python 镜像 digest、配置摘要、数据库 ledger digest、
   cohort、窗口、阈值和 rollback commander。
2. 只把批准 cohort 的请求发送到一个 Python writer；不得镜像写请求或创建第二个事实来源。
3. 使用相同 `X-Request-ID` 重试失败请求，验证 acceptance、Tool、Approval、Commit、Outbox
   和 Projection 的幂等性。
4. 观察 SSE sequence、Last-Event-ID replay、worker lease、错误率、队列和 dead letter；任何
   越过阈值的指标都立即停止扩大 cohort。
5. Canary 结束后做 workspace 级 historical/canary reconciliation，并由 change owner 批准是否扩大。

## 停止条件

出现跨 workspace 访问、重复副作用、request hash 冲突未被拒绝、lease generation 失配、
projection 顺序错误、SSE terminal frame 丢失、Provider 超时失控、队列或连接池越过阈值时，
立即停止发送新请求并保留全部 artifact。不得通过降级开关重新启用未验证的旧路径。

## 回滚

回滚只切换受保护入口到上一已验证版本，等待已接受的 Run/Outbox/Projection 排空，并继续
使用同一 PostgreSQL facts。回滚不得删除表、事实、Outbox、Projection、artifact 或 checkpoint，
不得执行未经批准的 schema 收缩。完成后再次运行 reconciliation 和公开 API/SSE smoke tests。

## 当前阶段

Phase 0 仅冻结文档和契约。没有 staging artifact、人工 release approval 或数据库授权时，
发布状态保持 `blocked_pending_prerequisites_and_manual_approval`。
