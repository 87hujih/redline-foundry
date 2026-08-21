from __future__ import annotations

from collections.abc import Iterator

from docreview.operations.bootstrap_identity import (
    DEFAULT_LOCAL_IDENTITY,
    bootstrap_local_identity,
)


class Cursor:
    def __init__(self, insert_counts: tuple[int, ...], verification: tuple[object, ...]) -> None:
        self._insert_counts: Iterator[int] = iter(insert_counts)
        self._verification = verification
        self.rowcount = -1

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        del params
        self.rowcount = next(self._insert_counts) if query.lstrip().startswith("INSERT") else -1

    def fetchone(self) -> tuple[object, ...]:
        return self._verification

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class Connection:
    def __init__(self, insert_counts: tuple[int, ...], verification: tuple[object, ...]) -> None:
        self._cursor = Cursor(insert_counts, verification)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def identity_row() -> tuple[object, ...]:
    value = DEFAULT_LOCAL_IDENTITY
    return (
        value.organization_id,
        value.organization_slug,
        value.organization_name,
        "active",
        value.workspace_id,
        value.organization_id,
        value.workspace_slug,
        value.workspace_name,
        "active",
        value.user_id,
        value.external_issuer,
        value.external_subject,
        value.email,
        value.display_name,
        "active",
        value.membership_id,
        value.workspace_id,
        value.user_id,
        "owner",
        "active",
    )


def test_bootstrap_local_identity_is_idempotent_and_preserves_stable_scope() -> None:
    first_connection = Connection((1, 1, 1, 1), identity_row())
    replay_connection = Connection((0, 0, 0, 0), identity_row())

    first = bootstrap_local_identity(first_connection)
    replay = bootstrap_local_identity(replay_connection)

    assert first.identity == replay.identity == DEFAULT_LOCAL_IDENTITY
    assert first.created == ("organization", "workspace", "user", "membership")
    assert replay.created == ()
    assert first_connection.commits == replay_connection.commits == 1
    assert first_connection.rollbacks == replay_connection.rollbacks == 0
