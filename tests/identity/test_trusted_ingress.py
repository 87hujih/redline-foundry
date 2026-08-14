from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from docreview.identity.trusted_ingress import (
    IdentityRequest,
    TrustedIngressAdapter,
    UntrustedIdentityError,
)

SECRET = "s" * 32
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
PRINCIPAL_ID = "11111111-1111-4111-8111-111111111111"
ORGANIZATION_ID = "22222222-2222-4222-8222-222222222222"
WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"


def signed_headers(
    *,
    method: str = "GET",
    path: str = "/api/agent/runs",
    request_id: str = "stable-request",
    principal_type: str = "user",
    principal_id: str = PRINCIPAL_ID,
    organization_id: str = ORGANIZATION_ID,
    workspace_id: str = WORKSPACE_ID,
    issued_at: str = "2026-08-12T12:00:00Z",
    roles: str = " Admin,owner,admin ",
) -> dict[str, str]:
    canonical = "\n".join(
        (
            "v1",
            request_id,
            method,
            path,
            principal_type,
            principal_id,
            organization_id,
            workspace_id,
            issued_at,
            roles.strip(),
        )
    )
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-DocReview-Principal-Type": principal_type,
        "X-DocReview-Principal-ID": principal_id,
        "X-DocReview-Organization-ID": organization_id,
        "X-DocReview-Workspace-ID": workspace_id,
        "X-DocReview-Identity-Issued-At": issued_at,
        "X-DocReview-Roles": roles,
        "X-DocReview-Identity-Signature": signature,
    }


def adapter() -> TrustedIngressAdapter:
    return TrustedIngressAdapter(
        secret=SECRET,
        trust_source="edge-proxy",
        max_age=timedelta(minutes=5),
        now=lambda: NOW,
    )


def test_secret_minimum_matches_go_utf8_byte_length() -> None:
    value = TrustedIngressAdapter(
        secret="密" * 11,
        trust_source="edge-proxy",
        max_age=timedelta(minutes=5),
    )

    assert value is not None


def test_authenticates_go_canonical_tuple_and_normalizes_roles() -> None:
    scope = adapter().authenticate(
        IdentityRequest(
            method="GET",
            path="/api/agent/runs",
            request_id="stable-request",
            headers=signed_headers(),
        ),
        WORKSPACE_ID,
    )

    assert scope.trusted is True
    assert scope.workspace_id == WORKSPACE_ID
    assert scope.trust_source == "edge-proxy"
    assert scope.issued_at == NOW
    assert scope.principal.type == "user"
    assert scope.principal.id == PRINCIPAL_ID
    assert scope.principal.organization_id == ORGANIZATION_ID
    assert scope.principal.roles == ("admin", "owner")


@pytest.mark.parametrize(
    ("headers", "requested_workspace"),
    [
        ({}, WORKSPACE_ID),
        (signed_headers(principal_type="browser"), WORKSPACE_ID),
        (signed_headers(principal_id="not-a-uuid"), WORKSPACE_ID),
        (signed_headers(issued_at="not-a-time"), WORKSPACE_ID),
        (signed_headers(issued_at="2026-08-12 12:00:00Z"), WORKSPACE_ID),
        (signed_headers(issued_at="2026-08-12T12:00:00.1234567890Z"), WORKSPACE_ID),
        (
            signed_headers(issued_at="2026-08-12T11:54:59Z"),
            WORKSPACE_ID,
        ),
        (
            signed_headers(issued_at="2026-08-12T12:00:31Z"),
            WORKSPACE_ID,
        ),
        (signed_headers(), "44444444-4444-4444-8444-444444444444"),
    ],
)
def test_rejects_untrusted_identity(headers: dict[str, str], requested_workspace: str) -> None:
    with pytest.raises(UntrustedIdentityError):
        adapter().authenticate(
            IdentityRequest(
                method="GET",
                path="/api/agent/runs",
                request_id="stable-request",
                headers=headers,
            ),
            requested_workspace,
        )


def test_signature_is_bound_to_method_path_request_id_and_raw_roles() -> None:
    headers = signed_headers()
    headers["X-DocReview-Roles"] = "admin,owner"

    with pytest.raises(UntrustedIdentityError):
        adapter().authenticate(
            IdentityRequest(
                method="POST",
                path="/api/agent/runs",
                request_id="different-request",
                headers=headers,
            ),
            WORKSPACE_ID,
        )


def test_accepts_signed_service_principal_at_adapter_boundary() -> None:
    headers = signed_headers(principal_type="service", roles="worker")

    scope = adapter().authenticate(
        IdentityRequest(
            method="GET",
            path="/api/agent/runs",
            request_id="stable-request",
            headers=headers,
        ),
        WORKSPACE_ID,
    )

    assert scope.principal.type == "service"
    assert scope.principal.roles == ("worker",)


def test_accepts_rfc3339nano_timestamp_used_in_go_signature() -> None:
    issued_at = "2026-08-12T12:00:00.123456789Z"
    headers = signed_headers(issued_at=issued_at)

    scope = adapter().authenticate(
        IdentityRequest(
            method="GET",
            path="/api/agent/runs",
            request_id="stable-request",
            headers=headers,
        ),
        WORKSPACE_ID,
    )

    assert scope.issued_at.microsecond == 123456
