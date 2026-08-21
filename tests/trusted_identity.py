from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from docreview.identity.trusted_ingress import TrustedIngressAdapter

SECRET = "test-trusted-ingress-secret-value"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
PRINCIPAL_ID = "11111111-1111-4111-8111-111111111111"
ORGANIZATION_ID = "22222222-2222-4222-8222-222222222222"


def identity_adapter() -> TrustedIngressAdapter:
    return TrustedIngressAdapter(
        secret=SECRET,
        trust_source="test-edge",
        max_age=timedelta(minutes=5),
        now=lambda: NOW,
    )


def signed_headers(method: str, path: str, workspace_id: str) -> dict[str, str]:
    request_id = "trusted-read-request"
    issued_at = "2026-08-20T12:00:00Z"
    roles = "member"
    canonical = "\n".join(
        (
            "v1",
            request_id,
            method,
            path,
            "user",
            PRINCIPAL_ID,
            ORGANIZATION_ID,
            workspace_id,
            issued_at,
            roles,
        )
    )
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Request-ID": request_id,
        "X-DocReview-Principal-Type": "user",
        "X-DocReview-Principal-ID": PRINCIPAL_ID,
        "X-DocReview-Organization-ID": ORGANIZATION_ID,
        "X-DocReview-Workspace-ID": workspace_id,
        "X-DocReview-Identity-Issued-At": issued_at,
        "X-DocReview-Roles": roles,
        "X-DocReview-Identity-Signature": signature,
    }


__all__ = ["identity_adapter", "signed_headers"]
