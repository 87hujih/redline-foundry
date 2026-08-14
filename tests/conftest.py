from collections.abc import Iterator

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_service_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    names = (
        "APP_ENV",
        "SERVER_HOST",
        "SERVER_PORT",
        "CORS_ALLOWED_ORIGINS",
        "LOG_LEVEL",
        "UVICORN_WORKERS",
        "RUNTIME_WORKER_ENABLED",
        "PROJECTION_WORKER_ENABLED",
        "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET",
        "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE",
        "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS",
        "ALLOW_DB_TESTS",
        "TEST_DATABASE_URL",
        "TEST_DATABASE_HOST_ALLOWLIST",
        "DATABASE_URL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    yield
