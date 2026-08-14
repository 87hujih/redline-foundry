"""Fail-closed PostgreSQL test fuse.

This module is test support only. It never reads `.env` and never falls back to
the production `DATABASE_URL`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import psycopg
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr


class DatabaseTestFuseError(ValueError):
    """Raised before any database connection factory can be called."""


@dataclass(frozen=True, slots=True)
class TestDatabaseConfig:
    __test__ = False

    dsn: SecretStr
    database_name: str
    hosts: tuple[str, ...]
    host_allowlist: tuple[str, ...]


def _values(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def load_test_database_config(
    environment: Mapping[str, str] | None = None,
) -> TestDatabaseConfig:
    values = _values(environment)
    if values.get("ALLOW_DB_TESTS", "").strip() != "1":
        raise DatabaseTestFuseError("ALLOW_DB_TESTS=1 is required")

    raw_dsn = values.get("TEST_DATABASE_URL", "").strip()
    if not raw_dsn:
        raise DatabaseTestFuseError("TEST_DATABASE_URL is required; DATABASE_URL is never used")
    try:
        connection_parameters = conninfo_to_dict(raw_dsn)
    except ProgrammingError as error:
        raise DatabaseTestFuseError("TEST_DATABASE_URL 无效") from error
    database_value = connection_parameters.get("dbname")
    hosts_value = connection_parameters.get("host")
    if not isinstance(database_value, str) or not isinstance(hosts_value, str):
        raise DatabaseTestFuseError("TEST_DATABASE_URL 无效")
    database_name = database_value.strip()
    raw_hosts = hosts_value
    hosts = tuple(item.strip().lower() for item in raw_hosts.split(",") if item.strip())
    if not database_name or not hosts:
        raise DatabaseTestFuseError("TEST_DATABASE_URL 无效")
    if not database_name.endswith("_test"):
        raise DatabaseTestFuseError("数据库名必须以 _test 结尾")

    allowlist = tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in values.get("TEST_DATABASE_HOST_ALLOWLIST", "").split(",")
            if item.strip()
        )
    )
    if not allowlist:
        raise DatabaseTestFuseError("TEST_DATABASE_HOST_ALLOWLIST 不能为空")
    if any(host not in allowlist for host in hosts):
        raise DatabaseTestFuseError("数据库 host 不在 TEST_DATABASE_HOST_ALLOWLIST 中")
    return TestDatabaseConfig(
        dsn=SecretStr(raw_dsn),
        database_name=database_name,
        hosts=hosts,
        host_allowlist=allowlist,
    )


async def create_test_database_connection(
    environment: Mapping[str, str] | None = None,
    *,
    connection_factory: Callable[[str], Awaitable[object]] | None = None,
) -> object:
    config = load_test_database_config(environment)
    if connection_factory is None:
        return await psycopg.AsyncConnection.connect(config.dsn.get_secret_value())
    return await connection_factory(config.dsn.get_secret_value())


__all__ = [
    "DatabaseTestFuseError",
    "TestDatabaseConfig",
    "create_test_database_connection",
    "load_test_database_config",
]
