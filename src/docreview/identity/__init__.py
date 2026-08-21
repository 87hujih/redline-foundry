"""可信 Principal 与 Workspace 边界。"""

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
