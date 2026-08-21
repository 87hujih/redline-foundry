"""Idempotent local organization, workspace, user, and membership bootstrap."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

import psycopg

from docreview.config.settings import load_settings

INSERT_ORGANIZATION_SQL = """
INSERT INTO organizations (id, slug, name, status)
VALUES (%s, %s, %s, 'active')
ON CONFLICT DO NOTHING
"""

INSERT_WORKSPACE_SQL = """
INSERT INTO workspaces (id, organization_id, slug, name, status)
VALUES (%s, %s, %s, %s, 'active')
ON CONFLICT DO NOTHING
"""

INSERT_USER_SQL = """
INSERT INTO users (
    id, external_issuer, external_subject, email, display_name, status
)
VALUES (%s, %s, %s, %s, %s, 'active')
ON CONFLICT DO NOTHING
"""

INSERT_MEMBERSHIP_SQL = """
INSERT INTO memberships (id, workspace_id, user_id, role, status)
VALUES (%s, %s, %s, 'owner', 'active')
ON CONFLICT DO NOTHING
"""

VERIFY_IDENTITY_SQL = """
SELECT organization.id::text, organization.slug, organization.name, organization.status,
       workspace.id::text, workspace.organization_id::text, workspace.slug, workspace.name,
       workspace.status, account.id::text, account.external_issuer, account.external_subject,
       account.email, account.display_name, account.status, membership.id::text,
       membership.workspace_id::text, membership.user_id::text, membership.role,
       membership.status
FROM organizations AS organization
JOIN workspaces AS workspace
  ON workspace.id = %s AND workspace.organization_id = organization.id
JOIN users AS account ON account.id = %s
JOIN memberships AS membership
  ON membership.id = %s
 AND membership.workspace_id = workspace.id
 AND membership.user_id = account.id
WHERE organization.id = %s
"""


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    organization_id: str
    organization_slug: str
    organization_name: str
    workspace_id: str
    workspace_slug: str
    workspace_name: str
    user_id: str
    external_issuer: str
    external_subject: str
    email: str
    display_name: str
    membership_id: str


DEFAULT_LOCAL_IDENTITY = LocalIdentity(
    organization_id="11111111-1111-4111-8111-111111111111",
    organization_slug="docreview-local",
    organization_name="DocReview Local",
    workspace_id="22222222-2222-4222-8222-222222222222",
    workspace_slug="default",
    workspace_name="Default Workspace",
    user_id="33333333-3333-4333-8333-333333333333",
    external_issuer="docreview-local",
    external_subject="owner",
    email="owner@docreview.local",
    display_name="Local Owner",
    membership_id="44444444-4444-4444-8444-444444444444",
)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    identity: LocalIdentity
    created: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"identity": asdict(self.identity), "created": list(self.created)}


class BootstrapCursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: tuple[object, ...]) -> Any: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def __enter__(self) -> BootstrapCursor: ...
    def __exit__(self, *args: object) -> None: ...


class BootstrapConnection(Protocol):
    def cursor(self) -> BootstrapCursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def _expected_row(identity: LocalIdentity) -> tuple[object, ...]:
    return (
        identity.organization_id,
        identity.organization_slug,
        identity.organization_name,
        "active",
        identity.workspace_id,
        identity.organization_id,
        identity.workspace_slug,
        identity.workspace_name,
        "active",
        identity.user_id,
        identity.external_issuer,
        identity.external_subject,
        identity.email,
        identity.display_name,
        "active",
        identity.membership_id,
        identity.workspace_id,
        identity.user_id,
        "owner",
        "active",
    )


def bootstrap_local_identity(
    connection: BootstrapConnection,
    identity: LocalIdentity = DEFAULT_LOCAL_IDENTITY,
) -> BootstrapResult:
    created: list[str] = []
    statements = (
        (
            "organization",
            INSERT_ORGANIZATION_SQL,
            (identity.organization_id, identity.organization_slug, identity.organization_name),
        ),
        (
            "workspace",
            INSERT_WORKSPACE_SQL,
            (
                identity.workspace_id,
                identity.organization_id,
                identity.workspace_slug,
                identity.workspace_name,
            ),
        ),
        (
            "user",
            INSERT_USER_SQL,
            (
                identity.user_id,
                identity.external_issuer,
                identity.external_subject,
                identity.email,
                identity.display_name,
            ),
        ),
        (
            "membership",
            INSERT_MEMBERSHIP_SQL,
            (identity.membership_id, identity.workspace_id, identity.user_id),
        ),
    )
    try:
        with connection.cursor() as cursor:
            for name, query, params in statements:
                cursor.execute(query, params)
                if cursor.rowcount not in {0, 1}:
                    raise RuntimeError(f"unexpected {name} bootstrap row count")
                if cursor.rowcount == 1:
                    created.append(name)
            cursor.execute(
                VERIFY_IDENTITY_SQL,
                (
                    identity.workspace_id,
                    identity.user_id,
                    identity.membership_id,
                    identity.organization_id,
                ),
            )
            row = cursor.fetchone()
            if row != _expected_row(identity):
                raise RuntimeError("local identity conflicts with existing tenancy facts")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return BootstrapResult(identity=identity, created=tuple(created))


def main() -> None:
    settings = load_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required")
    try:
        with psycopg.connect(settings.database_url.get_secret_value()) as connection:
            result = bootstrap_local_identity(cast(BootstrapConnection, connection))
    except Exception as error:
        print(f"identity bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True))


__all__ = [
    "DEFAULT_LOCAL_IDENTITY",
    "BootstrapResult",
    "LocalIdentity",
    "bootstrap_local_identity",
    "main",
]
