"""Trusted principal and workspace boundaries."""

from docreview.identity.trusted_ingress import (
    IdentityRequest,
    Principal,
    TrustedIngressAdapter,
    UntrustedIdentityError,
    WorkspaceScope,
)

__all__ = [
    "IdentityRequest",
    "Principal",
    "TrustedIngressAdapter",
    "UntrustedIdentityError",
    "WorkspaceScope",
]
