# 启动与使用准备

本文只描述当前仓库实际支持的启动边界。启动脚本不会生成或改写 PostgreSQL migration，
也不会把示例凭据当作可用凭据。

## 当前状态

当前应用有两个实际运行层级：

- `development`：可以启动 FastAPI 进程和 `/healthz`，但默认 `AppDependencies` 为空，
  不会自动装配 PostgreSQL、Provider 或 durable worker；不能据此判断完整功能可用。
- `production`：启动时必须先构造 Provider、打开 PostgreSQL pool，再装配 repository、
  LangGraph、Runtime worker 和 Projection worker。任一依赖缺失都会 fail closed。

当前仓库不包含 001-024 历史 migration 或 Alembic 配置。新的开发机器必须先从已批准的
基础 schema artifact 初始化数据库，再应用追加式
`migrations/025_assistant_session_resource_selection.sql`；不得根据 Python SQL 自行重写基础
schema。当前本地启动脚本只会启动已经初始化的 `.runtime/postgres-data-17-v2` 数据目录，
不会自动执行 migration。

## 软件与服务前置条件

本地 Python 版本必须满足 `>=3.13,<3.14`，并安装 `uv`。生产环境还需要：

- PostgreSQL 和当前契约要求的完整 schema；如果批准的 schema 使用 PostgreSQL vector
  类型，还必须按 migration 要求安装对应扩展。
- 可从 Python 服务访问的 Tika HTTP 服务。
- SiliconFlow 或兼容 OpenAI API 的 LLM、Embedding、Reranker 服务，以及正确的模型和维度。
- Web Search 是可选能力；未配置时不会注册 `web.search` 工具，其他 durable worker 正常运行。
- 可写的本地内容寻址文件目录。当前实现没有 S3/MinIO 适配器。
- 生产入口所需的 HTTPS、身份授权服务、mTLS 证书、Nginx njs 模块和 HMAC secret。

## 配置步骤

1. 在仓库根目录复制 `.env.example` 为未跟踪的 `.env`。
2. 替换实际启用能力对应的 `replace-with-*`、`provider.example` 和 `tika.example` 值。
   不使用 Web Search 时不要设置 `WEB_SEARCH_URL`/`WEB_SEARCH_API_KEY`。不要提交 `.env`。
3. 开发模式至少保持 `RUNTIME_WORKER_ENABLED` 与 `PROJECTION_WORKER_ENABLED` 同值，
   并保持 `UVICORN_WORKERS=1`。
4. 完整生产模式必须设置：
   `APP_ENV=production`、非空 `CORS_ALLOWED_ORIGINS`、`DATABASE_URL`、
   `DOCUMENT_PARSER=structured`、`TIKA_URL`/`TIKA_TIMEOUT_MS`、全部 Provider 字段、
   `EMBEDDING_TOKENIZER_PROFILE` 和完整 trusted-ingress 三元组。
5. 要实际处理 Assistant/Agent 请求，必须同时设置
   `RUNTIME_WORKER_ENABLED=true`、`PROJECTION_WORKER_ENABLED=true`、唯一的
   `RUNTIME_WORKER_ID`。Web Search 不属于必填项。
6. 生产 `UPLOAD_STORAGE_DIR` 应使用绝对路径，且不能是仓库根目录、用户目录或系统根目录。

## 安装、校验与启动

当前 Windows 本地环境可从仓库根目录执行：

```powershell
.\scripts\start-local.ps1
```

该脚本依次检查或启动 `127.0.0.1:55432` 的仓库内 PostgreSQL、
`127.0.0.1:9998` 的 `apache/tika:3.3.0.0` 容器，执行幂等身份初始化，最后以前台
方式启动 API。只查看三个服务的状态：

```powershell
.\scripts\start-local.ps1 -StatusOnly
```

分步执行时使用：

```powershell
uv sync --locked --dev
uv lock --check
uv run docreview-init-local
uv run pyright
uv run docreview-api
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

生产服务必须通过 `deploy/nginx/nginx.conf.template` 对应的受保护入口访问。Python 进程
不应直接暴露给不可信客户端；Ingress 必须先剥离客户端伪造的身份头，再调用授权服务并
生成 HMAC attestation。

## 首次使用准备

- 执行 `uv run docreview-init-local` 幂等准备本地 organization、workspace、user 和 owner
  membership；重复执行不会创建重复事实。
- 确认身份授权服务返回的 principal、organization、workspace ID 都是规范 UUID。
- 通过受保护入口获得合法的 `X-DocReview-*` attestation；不要手工信任客户端传入的身份头。
- 先上传文档或准备已有 resource/current version。持久化 Assistant Turn 的 DTO 虽允许
  `resource_id` 省略，但当前 durable repository 实际要求有效的 resource scope。
- 使用稳定的 `X-Request-ID` 重试同一请求；SSE 重连使用 `Last-Event-ID`，不能创建第二个事实。
- 本地直连 API 仍必须使用 `.env` 中 trusted-ingress secret 生成 HMAC 身份头；正式环境由
  `deploy/nginx/nginx.conf.template` 对应的受保护入口完成签名，不能绕过该边界。

## 数据库测试安全

数据库 round-trip 测试不会读取生产 `DATABASE_URL`。只有同时满足以下条件才允许连接测试库：

- 进程环境 `ALLOW_DB_TESTS=1`；
- 仅进程环境提供 `TEST_DATABASE_URL`；
- 数据库名以 `_test` 结尾；
- host 位于准确的 `TEST_DATABASE_HOST_ALLOWLIST`。

缺少任一条件时，测试必须 skip/fail closed，不能尝试连接生产数据库或 `.env` 中的数据库。

## 发布前检查

发布前还需要保留数据库事务/lease/idempotency/checkpoint 证据、Provider/Tika/文件存储/Ingress
失败路径证据、SSE replay 和 Approval continuation 证据，以及 queue、lease、pool、SSE、
Outbox/Projection lag 的监控和回滚 artifact。具体门禁见
`docs/remediation/canary-runbook.md`。
