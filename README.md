# DocReview Agent Python Service

The service exposes the frozen HTTP contract, trusted-ingress authentication,
workspace-scoped PostgreSQL adapters, resource search, file download, durable
turn processing, and LangGraph orchestration.

The complete setup checklist is in
[`docs/remediation/local-setup.md`](docs/remediation/local-setup.md). The repository
does not contain the historical PostgreSQL migration files. Initialize a new database
from the approved base schema artifact, then apply the append-only
`migrations/025_assistant_session_resource_selection.sql` artifact.

```powershell
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run pyright
uv run docreview-api
```

版本化的 RAG、回答、Agent、安全和性能评测系统见
[`evals/README.md`](evals/README.md)，确定性回归门禁位于
`.github/workflows/evals.yml`。

On the configured Windows development machine, `scripts/start-local.ps1` checks or
starts the local PostgreSQL and Tika dependencies, bootstraps the stable local
identity facts, and starts the API. Web Search is optional and is not registered
when `WEB_SEARCH_URL` is unset.

Local startup reads `.env` from the current working directory. Process environment
variables take precedence over values in `.env`. Start the service from the repository
root so the expected file is found; `.env` is ignored by Git and must not be committed.
Use `.env.example` as the non-secret field template. The template includes placeholders
for all supported settings; replace them before using production mode.

The entrypoint enforces one Uvicorn worker. Production startup additionally
requires an exact CORS allowlist and complete trusted-ingress configuration.
