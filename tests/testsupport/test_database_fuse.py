from collections.abc import Awaitable, Callable

import pytest

from docreview.testsupport.database import (
    DatabaseTestFuseError,
    TestDatabaseConfig,
    create_test_database_connection,
    load_test_database_config,
)

SAFE_ENV = {
    "ALLOW_DB_TESTS": "1",
    "TEST_DATABASE_URL": "postgresql://tester:secret@127.0.0.1/agent_project_test",
    "TEST_DATABASE_HOST_ALLOWLIST": "127.0.0.1,localhost",
}


@pytest.mark.parametrize(
    ("environment", "match"),
    [
        ({}, "ALLOW_DB_TESTS=1"),
        ({"ALLOW_DB_TESTS": "true"}, "ALLOW_DB_TESTS=1"),
        (
            {
                "ALLOW_DB_TESTS": "1",
                "DATABASE_URL": "postgresql://127.0.0.1/production",
                "TEST_DATABASE_HOST_ALLOWLIST": "127.0.0.1",
            },
            "TEST_DATABASE_URL",
        ),
        (SAFE_ENV | {"TEST_DATABASE_URL": "postgresql://127.0.0.1/agent_project"}, "_test"),
        (
            SAFE_ENV | {"TEST_DATABASE_URL": "postgresql://database.internal/agent_project_test"},
            "ALLOWLIST",
        ),
        (SAFE_ENV | {"TEST_DATABASE_HOST_ALLOWLIST": ""}, "ALLOWLIST"),
        (SAFE_ENV | {"TEST_DATABASE_URL": "not a connection string"}, "无效"),
    ],
)
def test_database_fuse_rejects_unsafe_configuration(
    environment: dict[str, str], match: str
) -> None:
    with pytest.raises(DatabaseTestFuseError, match=match):
        load_test_database_config(environment)


def test_database_fuse_accepts_only_explicit_safe_test_target() -> None:
    config = load_test_database_config(SAFE_ENV)

    assert config.database_name == "agent_project_test"
    assert config.hosts == ("127.0.0.1",)
    assert config.dsn.get_secret_value() == SAFE_ENV["TEST_DATABASE_URL"]
    assert "secret" not in repr(config)


@pytest.mark.anyio
async def test_unsafe_configuration_fails_before_connection_factory() -> None:
    called = False

    async def fake_factory(_dsn: str) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(DatabaseTestFuseError, match="_test"):
        await create_test_database_connection(
            SAFE_ENV | {"TEST_DATABASE_URL": "postgresql://127.0.0.1/production"},
            connection_factory=fake_factory,
        )

    assert called is False


@pytest.mark.anyio
async def test_safe_configuration_invokes_injected_non_network_factory() -> None:
    received_dsn: str | None = None
    sentinel = object()

    async def fake_factory(dsn: str) -> object:
        nonlocal received_dsn
        received_dsn = dsn
        return sentinel

    connection = await create_test_database_connection(
        SAFE_ENV,
        connection_factory=fake_factory,
    )

    assert connection is sentinel
    assert received_dsn == SAFE_ENV["TEST_DATABASE_URL"]


def test_connection_factory_contract_is_async() -> None:
    factory: Callable[[str], Awaitable[object]]

    async def fake_factory(_dsn: str) -> object:
        return object()

    factory = fake_factory
    assert callable(factory)
    assert TestDatabaseConfig.__test__ is False
