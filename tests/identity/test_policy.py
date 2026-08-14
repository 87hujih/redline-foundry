from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from docreview.identity.policy import (
    Access,
    Membership,
    MembershipRole,
    PolicyResolver,
    ResourceRef,
)
from docreview.identity.trusted_ingress import Principal, WorkspaceScope

WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_WORKSPACE_ID = "44444444-4444-4444-8444-444444444444"
USER_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"


def scope(*, principal_type: str = "user", workspace_id: str = WORKSPACE_ID) -> WorkspaceScope:
    from datetime import UTC, datetime

    return WorkspaceScope(
        principal=Principal(
            type=principal_type,
            id=USER_ID,
            organization_id="22222222-2222-4222-8222-222222222222",
            roles=(),
        ),
        workspace_id=workspace_id,
        trust_source="edge",
        trusted=True,
        issued_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


@dataclass
class FakePolicyRepository:
    membership: Membership | None
    owned: bool = True
    membership_calls: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    ownership_calls: list[tuple[str, str, str]] = field(
        default_factory=lambda: list[tuple[str, str, str]]()
    )

    async def get_active_membership(self, workspace_id: str, user_id: str) -> Membership | None:
        self.membership_calls.append((workspace_id, user_id))
        return self.membership

    async def resource_belongs_to_workspace(
        self, resource_type: str, resource_id: str, workspace_id: str
    ) -> bool:
        self.ownership_calls.append((resource_type, resource_id, workspace_id))
        return self.owned


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        (MembershipRole.OWNER, "workflow.decide_approval", True),
        (MembershipRole.ADMIN, "workflow.decide_approval", True),
        (MembershipRole.EDITOR, "workflow.decide_approval", False),
        (MembershipRole.EDITOR, "workflow.request_approval", True),
        (MembershipRole.VIEWER, "document.write", False),
        (MembershipRole.VIEWER, "document.read", True),
        (MembershipRole.VIEWER, "retrieval.search", True),
    ],
)
async def test_permission_comes_from_active_membership_not_signed_roles(
    role: MembershipRole, permission: str, expected: bool
) -> None:
    repository = FakePolicyRepository(
        Membership(workspace_id=WORKSPACE_ID, user_id=USER_ID, role=role, status="active")
    )

    allowed = await PolicyResolver(repository).has_permission(scope(), permission)

    assert allowed is expected
    assert repository.membership_calls == [(WORKSPACE_ID, USER_ID)]


@pytest.mark.anyio
async def test_service_principal_and_missing_membership_fail_closed() -> None:
    repository = FakePolicyRepository(None)
    resolver = PolicyResolver(repository)

    assert await resolver.has_permission(scope(principal_type="service"), "document.read") is False
    assert await resolver.has_permission(scope(), "document.read") is False
    assert repository.membership_calls == [(WORKSPACE_ID, USER_ID)]


@pytest.mark.anyio
async def test_document_ownership_is_checked_in_exact_workspace() -> None:
    repository = FakePolicyRepository(
        Membership(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            role=MembershipRole.VIEWER,
            status="active",
        )
    )

    allowed = await PolicyResolver(repository).authorize_resource(
        scope(), ResourceRef(type="document", id=RESOURCE_ID, access=Access.READ)
    )

    assert allowed is True
    assert repository.ownership_calls == [("document", RESOURCE_ID, WORKSPACE_ID)]


@pytest.mark.anyio
async def test_cross_workspace_or_resource_binding_mismatch_fails_closed() -> None:
    repository = FakePolicyRepository(None, owned=True)
    resolver = PolicyResolver(repository)

    assert (
        await resolver.authorize_resource(
            scope(workspace_id=OTHER_WORKSPACE_ID),
            ResourceRef(type="document", id=RESOURCE_ID, access=Access.READ),
            bound_resource_id="66666666-6666-4666-8666-666666666666",
        )
        is False
    )
    assert repository.ownership_calls == []


@pytest.mark.anyio
async def test_membership_facts_must_match_scope_and_active_status() -> None:
    repository = FakePolicyRepository(
        Membership(
            workspace_id=OTHER_WORKSPACE_ID,
            user_id=USER_ID,
            role=MembershipRole.ADMIN,
            status="inactive",
        )
    )

    assert await PolicyResolver(repository).has_permission(scope(), "document.read") is False


@pytest.mark.anyio
async def test_unknown_resource_access_fails_closed() -> None:
    repository = FakePolicyRepository(None)
    resource = ResourceRef(type="document", id=RESOURCE_ID, access="unknown")  # type: ignore[arg-type]

    assert await PolicyResolver(repository).authorize_resource(scope(), resource) is False
