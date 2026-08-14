# Python 重写完成度审计报告

**审计日期：** 2026-08-14  
**Go 源项目（只读）：** `G:\gofile\Agent_Project`  
**Python 项目：** `G:\gofile\docs_reviewAgent`

## 审计口径

- `PASS`：当前代码和本次可重复验证证据共同满足通过标准。
- `FAIL`：已确认存在实现缺失、实现错误、契约冲突或仍在使用的依赖。
- `BLOCKED`：实现或静态证据存在，但缺少授权数据库、protected ingress、staging、canary 或其他必须的运行证据。跳过的测试不计为 `PASS`。
- Active scope 按 `python-active-scope.md` 主表的 20 个条目计算，其中 17 个标记为迁移，3 个明确保留 Go。
- 功能通过率采用生产完成口径：17 个迁移条目只有同时具备完整生产代码闭包和要求的环境证据才计 `PASS`。

## 审计结果

| 检查项 | 通过标准 | 证据文件 | 结果 | 阻塞原因 |
| --- | --- | --- | --- | --- |
| Active scope：server main | Python 入口能 fail-closed 装配数据库池、repository、provider、文件存储、Runtime/Projection lifecycle，并可作为生产服务启动 | `src/docreview/api/main.py:128-176`; `src/docreview/api/dependencies.py:109-125`; `src/docreview/config/settings.py:67-90` | FAIL | `create_app()` 默认注入空 `AppDependencies`；没有生产数据库、provider、Tika、文件存储或 pool 构造，业务路由启动后只会返回未配置错误 |
| Active scope：server runtime core | ModelGateway、Context、Evidence、ToolRuntime、Policy、Approval、Committer、checkpointer、Runtime/Projection worker 完整装配 | `src/docreview/agent_graph/boundary.py:34-73`; `src/docreview/agent_graph/runtime.py:37-41`; `src/docreview/api/main.py:141` | FAIL | 当前主要是 Protocol、离线 adapter 和测试替身；没有生产依赖图 |
| Active scope：Router base | health、request ID、精确 CORS、preflight 与当前 Go 行为一致 | `src/docreview/api/main.py:31-125,148-159`; `tests/api/test_application.py:38-179` | PASS | - |
| Active scope：Resource routes | 路由、DTO、错误、workspace SQL 在授权 PostgreSQL 上 round-trip 通过并完成生产装配 | `src/docreview/api/routes/resources.py`; `src/docreview/storage/postgres/resources.py`; `tests/api/test_resources.py`; `tests/storage/test_resource_sql.py` | BLOCKED | 无授权数据库 round-trip，且生产入口未装配 repository/search provider |
| Active scope：File route | 文件元数据、真实存储、headers、缺失对象与流关闭在生产装配中验证 | `src/docreview/api/routes/files.py`; `src/docreview/storage/filestore.py`; `src/docreview/storage/postgres/uploaded_files.py`; `tests/api/test_file_download.py` | BLOCKED | 无真实数据库/对象存储装配和 round-trip 证据 |
| Active scope：Run query routes | 签名身份、workspace 过滤、公开字段白名单和排序在真实数据库验证 | `src/docreview/api/routes/agent_runs.py`; `src/docreview/storage/postgres/agent_queries.py`; `tests/api/test_agent_runs.py` | BLOCKED | 无授权数据库和 protected ingress 证据 |
| Active scope：Typed approval query/write | approve/reject/repeat/conflict 与 continuation 绑定在真实锁和事务中验证 | `src/docreview/api/routes/agent_approvals.py`; `src/docreview/storage/postgres/runtime_repository.py`; `tests/storage/test_approval_continuation.py` | BLOCKED | 只有 fake/static SQL 证据；无 PostgreSQL 锁、事务和并发 round-trip |
| Active scope：Assistant query/compat | session/message/delete DTO 和 workspace-scoped persistence 在生产装配验证 | `src/docreview/api/routes/assistant_sessions.py`; `src/docreview/storage/postgres/assistant.py`; `src/docreview/storage/postgres/assistant_write.py` | BLOCKED | 无授权数据库 round-trip和生产 repository 装配 |
| Active scope：Assistant non-stream Turn | 相同 request ID/body 幂等、投影等待、201/200 DTO 在真实 durable runtime 通过 | `src/docreview/api/routes/assistant_turns.py`; `src/docreview/turn/pipeline.py`; `src/docreview/turn/coordinator.py` | BLOCKED | 无数据库、protected ingress、生产 worker 和 canary 证据 |
| Active scope：Assistant SSE Turn | event/id/data、Last-Event-ID、断线重连、terminal 顺序在真实入口和持久化事件上通过 | `src/docreview/turn/sse.py`; `src/docreview/api/routes/assistant_turns.py:136-181`; `tests/turn/test_sse.py`; `tests/api/test_assistant_turns.py` | BLOCKED | 离线测试通过，但没有 protected ingress、真实持久化重连或 canary 证据 |
| Active scope：Assistant upload | 创建合法 Resource/Version/File/Session/Message 完整图，保持 DTO、ownership 和失败一致性 | `src/docreview/document/upload.py`; `src/docreview/storage/postgres/upload_write.py`; `src/docreview/api/routes/assistant_uploads.py`; `tests/document/test_upload.py`; `tests/storage/test_upload_write.py` | BLOCKED | UUID、SQL 参数顺序、完整事实图、单事务、UploadedFile 绑定、Workspace/Principal ownership、冻结 DTO 和失败补偿均已 database-free 验证；仍缺授权 PostgreSQL 对 SQL/UUID/JSON/FK/unique/row-lock/rollback 的真实 round-trip，且生产入口尚未装配 upload repository/store |
| Active scope：Legacy assistant internals | 明确保留 Go 且 Python 不重新引入 legacy/shadow fallback | `docs/remediation/python-active-scope.md:34,56-57`; `src/docreview/turn/pipeline.py:169-179` | PASS | - |
| Active scope：Turn acceptance/outcome | canonical hash、请求幂等、Turn/Run/Step/Event/Outbox 原子事务和 projection 在 PostgreSQL 验证 | `src/docreview/turn/coordinator.py`; `src/docreview/storage/postgres/turn.py`; `tests/turn/test_coordinator.py`; `tests/storage/test_turn_sql.py` | BLOCKED | 无授权 PostgreSQL transaction/rollback/idempotency round-trip |
| Active scope：Run/Step/Attempt engine | claim、heartbeat、generation fencing、retry、timeout、cancel、recovery 在多 worker PostgreSQL 场景通过 | `src/docreview/runtime/engine.py`; `src/docreview/storage/postgres/runtime_sql.py`; `src/docreview/storage/postgres/runtime_repository.py`; `tests/runtime/test_runtime_engine.py` | BLOCKED | fake/static SQL 测试通过，但无真实 `SKIP LOCKED`、锁竞争、时钟和 crash recovery 证据 |
| Active scope：Tool/Policy/Artifact | 生产 registry、builtin tools、provider、rate limiter、artifact store 和审计闭包可运行 | `src/docreview/agent_graph/boundary.py:34-73`; `src/docreview/agent_graph/graph.py`; `src/docreview/storage/postgres/runtime_repository.py` | FAIL | 只有 Protocol/graph command/SQL 边界，没有生产 ToolRuntime、registry、builtin backend、rate limiter、artifact/provider 实例 |
| Active scope：Typed orchestration/context/evidence | 生产 ModelGateway、ContextAssembler、Evidence provider 与完整 provenance DTO 装配并通过代表性语料 | `src/docreview/agent_graph`; `src/docreview/knowledge/evidence.py`; `docs/remediation/status.md:207-211` | FAIL | ModelGateway/Context/Evidence 均未生产装配；完整 Go EvidenceSet provenance 和 provider 质量未实现/未验证 |
| Active scope：Canonical document/Patch/Commit | PostgreSQL CommitStore 以 Serializable 事务写完整 canonical bundle、commit 和 outbox | `src/docreview/document/commit.py:34-128`; `tests/document/test_phase3_golden.py` | FAIL | 只有 `CommitStore` Protocol 和纯业务校验；没有 PostgreSQL canonical bundle writer/Serializable transaction 实现 |
| Active scope：Outbox/Projection | 真实数据库 claim、fencing、retry、dead-letter、receipt gap replay 和 lag 排空通过 | `src/docreview/runtime/projection.py`; `src/docreview/storage/postgres/runtime_projection_repository.py`; `tests/runtime/test_projection_worker.py` | BLOCKED | 无 PostgreSQL round-trip、多 worker、dead-letter 运维和容量证据 |
| Active scope：Go migrator | 过渡期明确保留，部署仍以 ledger/checksum/advisory-lock 语义运行 | `docs/remediation/python-active-scope.md:46,90`; `G:\gofile\Agent_Project\apps\server\Dockerfile`; `G:\gofile\Agent_Project\deploy\docker-compose.prod.yml` | PASS | - |
| Active scope：Operations/evaluation CLIs | 明确保留 Go，CI/运维调用被记录 | `docs/remediation/python-active-scope.md:47,91`; `G:\gofile\Agent_Project\.github\workflows\ci.yml:37-55`; `G:\gofile\Agent_Project\apps\server\Dockerfile` | PASS | - |
| Active scope 主表覆盖率 | 每个 active Go caller 有 Python 实现证据或明确保留决策 | `docs/remediation/python-active-scope.md:13-47` | PASS | 20/20 条目均有处置 |
| Active scope Phase 7 汇总完整性 | “覆盖所有 active 条目”的汇总表逐项包含主表条目 | `docs/remediation/python-active-scope.md:34,69-93` | FAIL | Phase 7 汇总表只有 19 行，遗漏主表的 `Legacy assistant internals`，虽主表已有保留决策但汇总声明不准确 |
| 未记录的 Python/Go 依赖 | 当前生产、CI、部署和开发调用均在 scope 或保留决策中归属 | `G:\gofile\Agent_Project\scripts\dev\start-local.ps1:40-50`; `G:\gofile\Agent_Project\apps\server\cmd\local-dev-bootstrap` | FAIL | 当前本地启动显式依赖 Go migrator、`local-dev-bootstrap` 和 Go server；`local-dev-bootstrap` 未在 Python active-scope 条目中单列保留决策 |
| 当前注册路由覆盖 | Python 注册当前 Go Router 的全部业务路由，且不恢复未注册 legacy 路由 | `G:\gofile\Agent_Project\apps\server\internal\server\router\router.go:48-97`; `src/docreview/api/main.py:151-159`; `tests/api/test_application.py:145-179` | PASS | - |
| HTTP method/status/DTO/error adapter | 除已单列的 upload 生产闭包外，冻结 REST 行为有路由级测试和固定 fixture | `docs/remediation/api-contract.md`; `tests/api`; `dist/parity/v1/result.json` | PASS | - |
| API 契约文档自洽性 | 同一文档对当前认证、状态和错误行为无矛盾 | `docs/remediation/api-contract.md:84,128-130`; `G:\gofile\Agent_Project\apps\server\internal\server\handlers\assistant.go:453-538` | FAIL | `api-contract.md:84` 仍称 upload 不调用 identity adapter，但当前 Go/Python upload 均要求 trusted ingress，且同文档错误表已记录 401/503 |
| SSE event/sequence/Last-Event-ID 离线契约 | 固定事件映射、正序 sequence、cursor 过滤、同 ID terminal frame 和 transport error 均通过 | `src/docreview/turn/sse.py`; `src/docreview/turn/pipeline.py:83-139`; `tests/turn/test_sse.py`; `tests/api/test_assistant_turns.py` | PASS | - |
| request_id 幂等和重连 | 同 request ID/body 在真实事务、入口 sticky routing 和 SSE 重连中只产生一组事实 | `src/docreview/turn/coordinator.py:46-99`; `docs/remediation/canary-runbook.md:41-75` | BLOCKED | 只有 fake/fixture；无授权数据库、protected ingress sticky routing 或 canary replay |
| Run/Step/Attempt 类型和离线状态机 | 状态、预算、错误分类和 engine 分支有单元测试 | `src/docreview/runtime/models.py`; `src/docreview/runtime/engine.py`; `tests/runtime/test_runtime_engine.py` | PASS | - |
| lease/fencing/retry/recovery 生产证明 | 真实 PostgreSQL 多 worker 能拒绝 stale generation 并可靠恢复 | `src/docreview/storage/postgres/runtime_sql.py`; `tests/storage/test_runtime_sql.py`; `docs/remediation/parity-report.md:63` | BLOCKED | 没有数据库 round-trip、并发锁和 staging fault injection |
| Approval continuation 生产证明 | approve/reject/repeat/conflict 在真实锁事务和 checkpoint 绑定中通过 | `src/docreview/storage/postgres/runtime_repository.py`; `tests/storage/test_approval_continuation.py`; `docs/remediation/status.md:204-206` | BLOCKED | 无授权数据库和数据库-backed checkpoint 验证 |
| LangGraph checkpoint/restart | 生产持久化 repository 支持进程重启、retention、并发 checkpoint claim | `src/docreview/agent_graph/checkpoint.py:57-173`; `docs/remediation/status.md:201-203` | FAIL | 只有 in-memory repository 和 adapter；没有 PostgreSQL checkpoint repository/schema wiring |
| Workspace 隔离 | ingress、SQL、Policy、Resource ownership、跨 workspace 拒绝在真实数据库/入口验证 | `src/docreview/identity/trusted_ingress.py`; `src/docreview/identity/policy.py`; `tests/storage/test_identity_sql.py`; `tests/identity` | BLOCKED | 静态/单元测试通过，但无 production ingress、数据库 round-trip 和历史数据 reconciliation |
| pytest | 当前锁定环境完整测试无失败、无 skip 伪装通过 | `pyproject.toml`; `tests` | PASS | 本次 `uv run pytest -q`：239 passed |
| Ruff | lint 和 format check 均通过 | `pyproject.toml`; `src`; `tests` | PASS | 本次 `uv run ruff check .` 与 `uv run ruff format --check .` 均通过 |
| Pyright/compile | strict Pyright 和 Python 编译检查通过 | `pyproject.toml`; `src`; `tests` | PASS | 本次 `uv run pyright`：0 errors；`compileall` 通过 |
| 前端测试 | 当前前端快照完整 Vitest 通过 | `G:\gofile\Agent_Project\apps\web` | PASS | 在排除 `.env*` 的只读临时副本执行：31 files、107 tests passed |
| 前端 lint | 当前前端快照 lint 无 warning/error | `G:\gofile\Agent_Project\apps\web\package.json` | PASS | 临时副本 `npm run lint` 通过 |
| 前端 build | 当前前端快照 production build 通过 | `G:\gofile\Agent_Project\apps\web\package.json`; `G:\gofile\Agent_Project\apps\web\app\api\[...path]\route.ts` | PASS | 临时副本 `npm run build` 通过 |
| Go/Python parity fixtures | 版本化 fixture/static contract runner 12 类全部相等，且明确不是生产证据 | `tests/fixtures/parity/v1`; `dist/parity/v1/result.json`; `docs/remediation/parity-report.md` | PASS | 12/12 passed；证据级别仅 fixed fixture/static contract |
| Upload persistence 测试覆盖 | 真实 `UploadMetadataRepository` 参数、UUID、完整 DTO 和中途失败一致性有测试 | `tests/storage/test_upload_write.py`; `tests/document/test_upload.py`; `tests/api/test_assistant_uploads.py`; `tests/storage/test_filestore.py` | PASS | 直接调用真实 repository，覆盖 Version/Resource 参数顺序、五类 UUID、完整成功/解析失败事实、逐步 rollback、Workspace/Principal ownership、UploadedFile 绑定、冻结 DTO、输入错误和文件补偿；不把 fake/static 结果冒充 PostgreSQL round-trip |
| 授权数据库 round-trip | 通过 fuse 在 `_test` PostgreSQL 验证 SQL、锁、事务、解码、幂等和 rollback | `src/docreview/testsupport/database.py`; `docs/remediation/status.md:169-177`; `docs/remediation/parity-report.md:63` | BLOCKED | 未提供 `ALLOW_DB_TESTS=1`、`TEST_DATABASE_URL`、allowlist 和授权数据库 provenance；本次未连接数据库 |
| Staging E2E | provider、Tika、存储、worker、SSE、approval、commit、outbox 全链路通过 | `docs/remediation/parity-report.md:64-69`; `docs/remediation/canary-runbook.md` | BLOCKED | 没有 staging 环境、真实依赖装配或保留的 E2E artifact |
| Canary 证据 | protected ingress 单写 cohort 有请求、对账、阈值和人工批准 | `docs/remediation/parity-report.md:63-72`; `docs/remediation/canary-runbook.md` | BLOCKED | 0 production/canary requests；所有 canary gates 仍 blocked |
| Provider 生产风险 | Model/Embedding/Reranker/Web provider 实现、超时、限流、降级和质量证据齐全 | `src/docreview/agent_graph/boundary.py`; `src/docreview/knowledge/evidence.py`; `docs/remediation/status.md:207-211` | FAIL | Python 只有 Protocol/算法边界，没有生产 provider 实现或装配 |
| Tika 生产风险 | Tika client、配置、连接/超时、代表性 DOCX/PDF fixture 和故障策略齐全 | `src/docreview/document/parser.py:36-82`; `G:\gofile\Agent_Project\docker-compose.yml:13-18` | FAIL | Python 只有 `TikaClient` Protocol；无 client/config/deploy 装配，源项目 Tika sidecar 仍是 Go 本地流程依赖 |
| 文件存储生产风险 | root 配置、权限、容量、原子 metadata/resource/message 一致性和恢复策略完成 | `src/docreview/storage/filestore.py`; `src/docreview/document/upload.py`; `src/docreview/storage/postgres/upload_write.py` | FAIL | 数据库事实已收口为单事务并具有新文件补偿测试，但仍无生产 root/config/权限/容量、共享存储多进程竞态、孤儿清扫和生产装配证据 |
| 数据库连接池生产风险 | pool DSN、size、timeout、health、shutdown、headroom 与多副本预算完成 | `pyproject.toml`; `src/docreview/config/settings.py`; `src/docreview/api/main.py` | FAIL | 虽依赖 `psycopg[binary,pool]`，但无 DATABASE_URL/pool 配置、构造、生命周期或容量参数 |
| 多副本/lease 竞争 | 多 replica 下 claim、公平性、heartbeat margin、stale worker 和 shutdown 通过 staging | `src/docreview/runtime/lifecycle.py`; `docs/remediation/parity-report.md:67`; `docs/remediation/status.md:194-200` | BLOCKED | 无 replica count、并发数据库测试、load/soak 或 fault test |
| SSE 断线的离线处理 | observer 取消不取消 durable pipeline，重连可按 cursor 回放 | `src/docreview/api/routes/assistant_turns.py:152-188`; `tests/api/test_assistant_turns.py`; `tests/turn/test_pipeline.py` | PASS | - |
| SSE 连接预算/队列积压 | 有连接上限、queue age/outbox lag 指标、容量阈值和告警 | `docs/remediation/parity-report.md:67-68`; `docs/remediation/canary-runbook.md:77-95` | BLOCKED | 无容量模型、SLO、dashboard、alert、soak 或 backlog drain 证据 |
| 回滚和数据保留设计 | 回滚不双写、不删除 durable/audit facts，并保持 accepted request sticky | `docs/remediation/canary-runbook.md:97-115`; `docs/remediation/status.md:179-187` | PASS | - |
| 回滚演练 | staging 完成摘流、drain、reconcile、上一版本部署和 fix-forward | `docs/remediation/parity-report.md:69`; `docs/remediation/canary-runbook.md` | BLOCKED | 没有实际 rehearsal artifact |
| 前端生产依赖安全 | 当前锁定生产依赖无已知 high/critical 漏洞或有批准的处置 | `G:\gofile\Agent_Project\apps\web\package.json`; `G:\gofile\Agent_Project\apps\web\package-lock.json` | FAIL | 本次临时副本 `npm audit --omit=dev` 报告 3 个 high（`next`、`nanoid`、`postcss`）；无审计处置记录 |
| Go caller/config/deploy 依赖 | Python 已替换生产 server 镜像、migrate、配置和启动路径，Go 不再是生产必要条件 | `G:\gofile\Agent_Project\apps\server\Dockerfile`; `G:\gofile\Agent_Project\deploy\docker-compose.prod.yml`; `G:\gofile\Agent_Project\scripts\dev\start-local.ps1` | FAIL | 生产 server/migrate 镜像、生产 Compose 和本地启动仍全部调用 Go |
| Go CI/operations 依赖 | CI、evaluation、ops、removal gate 不再要求待删除 Go 代码，或有明确长期保留边界 | `G:\gofile\Agent_Project\.github\workflows\ci.yml`; `G:\gofile\Agent_Project\apps\server\Dockerfile`; `docs/remediation/python-active-scope.md:90-91` | FAIL | CI 仍执行 Go 全后端测试、agent-eval 和 Go server image build；ops/eval/migrator 明确保留 |
| Legacy removal gate | 当前 caller/config 审计已刷新，报告退出码 0、`eligible=true`、所有生产证据满足 | `G:\gofile\Agent_Project\docs\remediation\legacy-removal-report.current.json`; `G:\gofile\Agent_Project\docs\remediation\status.md:9,235,277` | BLOCKED | 当前报告 `eligible=false` 且是旧 caller 快照；源状态明确要求重新生成，数据库/ingress/canary/rollback 等证据仍缺失 |
| 生产切流资格 | 所有数据库、assembly、ingress、reconciliation、capacity、alert、rollback 和 approval gate 通过 | `docs/remediation/parity-report.md`; `docs/remediation/canary-runbook.md`; 本报告 | BLOCKED | 存在实现 FAIL，且数据库、ingress、staging、canary 证据均缺失 |
| 删除 Go 资格 | Python 完整接管生产和保留工具，removal gate 为 eligible，且可逐项独立删除 | `docs/remediation/python-active-scope.md`; `G:\gofile\Agent_Project\docs\remediation\legacy-removal-report.current.json`; 本报告 | FAIL | Go 仍是生产 server/migrator/CI/ops/eval 依赖；removal gate 不合格；Python 生产闭包不完整 |

## 计算与结论

- **Active scope 覆盖率：** `20/20 = 100%`。含义仅是主表中的每个 active 条目已有 Python 实现证据或明确保留 Go 决策，不代表生产迁移完成。Phase 7 汇总表遗漏 1 个已保留条目，且当前本地 `local-dev-bootstrap` Go 依赖未单列。
- **生产功能通过率：** `1/17 = 5.9%`。17 个“迁移”条目中，仅 Router base 同时满足当前生产完成标准；其余为 5 个 `FAIL` 和 11 个 `BLOCKED`。3 个明确保留 Go 的条目不计入功能通过率。
- **阻塞项：** Python 生产依赖装配；upload 授权 PostgreSQL round-trip/生产装配；provider/Tika/文件存储/pool；数据库-backed checkpoint；protected ingress；历史数据 reconciliation；多副本/容量/告警；staging E2E；canary；回滚演练；人工批准；Go deploy/CI/工具依赖；未刷新且 `eligible=false` 的 removal report。
- **是否允许生产切流：** 不允许，结论为 `BLOCKED`。
- **是否允许删除 Go：** 不允许，结论为 `FAIL`；任何物理删除还同时受 removal gate 的 `BLOCKED` 状态约束。

## 本次验证

- Python：本次修复后 `uv run pytest -q`（239 passed）、`uv run ruff check .`、`uv run ruff format --check .`（128 files）、`uv run pyright`（0 errors）和 `uv run python -m compileall -q src` 均通过；此前记录的 `uv lock --check` 与 `uv run docreview-parity ... --check` 结果未在本任务重跑。
- Frontend：为保持 Go 源项目只读，在显式排除 `.env*`、`node_modules`、`.next` 和缓存的临时副本运行 `npm ci`、`npm test -- --run`（107 passed）、`npm run lint`、`npm run build`，均通过。
- 未运行任何数据库连接、migration、DDL、backfill、reindex、repair、生产 provider 调用、staging E2E 或 canary。它们均未记为 `PASS`。
