# DocReview Agent Python Service

Phase 2 adds the frozen read-only HTTP compatibility surface, trusted-ingress
authentication, workspace-scoped Psycopg repository adapters, resource search,
and original file download. It starts no Runtime Worker or Projection Worker
and registers no write endpoint.

```powershell
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run pyright
uv run docreview-api
```

The entrypoint enforces one Uvicorn worker. Production startup additionally
requires an exact CORS allowlist and complete trusted-ingress configuration.
