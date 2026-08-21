"""Trusted-ingress workspace scope shared by tenant data routes."""

from __future__ import annotations

from fastapi import Request

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
    WorkspaceScope,
)


def trusted_workspace_scope(request: Request, dependencies: AppDependencies) -> WorkspaceScope:
    if dependencies.identity_adapter is None:
        raise APIError(503, "trusted identity adapter is not configured")
    if not request.headers.get(HEADER_SIGNATURE, "").strip():
        raise APIError(401, "durable identity is required")

    workspace_id = request.headers.get(HEADER_WORKSPACE_ID, "").strip()
    try:
        scope = dependencies.identity_adapter.authenticate(
            IdentityRequest(
                method=request.method,
                path=request.url.path,
                request_id=str(request.state.request_id),
                headers=request.headers,
            ),
            workspace_id,
        )
    except UntrustedIdentityError as error:
        raise APIError(401, "durable identity is not trusted") from error
    if not scope.trusted or scope.workspace_id != workspace_id:
        raise APIError(403, "durable workspace scope is not trusted")
    return scope


__all__ = ["trusted_workspace_scope"]
