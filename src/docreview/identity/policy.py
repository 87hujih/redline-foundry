"""成员关系、角色与资源所有权 Policy 边界。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from docreview.identity.trusted_ingress import WorkspaceScope


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class Access(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class Membership:
    workspace_id: str
    user_id: str
    role: MembershipRole
    status: str


@dataclass(frozen=True, slots=True)
class ResourceRef:
    type: str
    id: str
    access: Access


class PolicyRepository(Protocol):
    async def get_active_membership(self, workspace_id: str, user_id: str) -> Membership | None: ...

    async def resource_belongs_to_workspace(
        self, resource_type: str, resource_id: str, workspace_id: str
    ) -> bool: ...


_ROLE_PERMISSIONS: dict[MembershipRole, frozenset[str]] = {
    MembershipRole.VIEWER: frozenset(
        {"document.read", "retrieval.search", "web.search", "artifact.read"}
    ),
    MembershipRole.EDITOR: frozenset(
        {
            "document.read",
            "document.write",
            "retrieval.search",
            "web.search",
            "artifact.read",
            "artifact.write",
            "workflow.request_approval",
        }
    ),
    MembershipRole.ADMIN: frozenset(
        {
            "document.read",
            "document.write",
            "retrieval.search",
            "web.search",
            "artifact.read",
            "artifact.write",
            "workflow.request_approval",
            "workflow.decide_approval",
        }
    ),
    MembershipRole.OWNER: frozenset(
        {
            "document.read",
            "document.write",
            "retrieval.search",
            "web.search",
            "artifact.read",
            "artifact.write",
            "workflow.request_approval",
            "workflow.decide_approval",
        }
    ),
}


class PolicyResolver:
    def __init__(self, repository: PolicyRepository) -> None:
        self._repository = repository

    async def has_permission(self, scope: WorkspaceScope, permission: str) -> bool:
        if (
            not scope.trusted
            or scope.principal.type != "user"
            or not scope.workspace_id
            or not scope.principal.id
            or not permission.strip()
        ):
            return False
        membership = await self._repository.get_active_membership(
            scope.workspace_id, scope.principal.id
        )
        if (
            membership is None
            or membership.status != "active"
            or membership.workspace_id != scope.workspace_id
            or membership.user_id != scope.principal.id
        ):
            return False
        return permission in _ROLE_PERMISSIONS.get(membership.role, frozenset())

    async def authorize_resource(
        self,
        scope: WorkspaceScope,
        resource: ResourceRef,
        *,
        bound_resource_id: str | None = None,
    ) -> bool:
        if (
            not scope.trusted
            or not scope.workspace_id
            or not resource.id.strip()
            or resource.type not in {"document", "artifact", "task"}
            or resource.access not in {Access.READ, Access.WRITE}
        ):
            return False
        if (
            resource.type == "document"
            and bound_resource_id
            and bound_resource_id.strip() != resource.id.strip()
        ):
            return False
        return await self._repository.resource_belongs_to_workspace(
            resource.type, resource.id.strip(), scope.workspace_id
        )


__all__ = [
    "Access",
    "Membership",
    "MembershipRole",
    "PolicyRepository",
    "PolicyResolver",
    "ResourceRef",
]
