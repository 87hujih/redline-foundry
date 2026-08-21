"""HMAC-SHA256 trusted-ingress 兼容适配器。"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

HEADER_PRINCIPAL_TYPE = "X-DocReview-Principal-Type"
HEADER_PRINCIPAL_ID = "X-DocReview-Principal-ID"
HEADER_ORGANIZATION_ID = "X-DocReview-Organization-ID"
HEADER_WORKSPACE_ID = "X-DocReview-Workspace-ID"
HEADER_ISSUED_AT = "X-DocReview-Identity-Issued-At"
HEADER_ROLES = "X-DocReview-Roles"
HEADER_SIGNATURE = "X-DocReview-Identity-Signature"

_LOWER_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class UntrustedIdentityError(ValueError):
    """Ingress attestation 缺失、无效或超出 scope。"""


@dataclass(frozen=True, slots=True)
class Principal:
    type: str
    id: str
    organization_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    principal: Principal
    workspace_id: str
    trust_source: str
    trusted: bool
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class IdentityRequest:
    method: str
    path: str
    request_id: str
    headers: Mapping[str, str]


def _header(headers: Mapping[str, str], name: str) -> str:
    expected = name.casefold()
    return next(
        (str(value).strip() for key, value in headers.items() if key.casefold() == expected),
        "",
    )


def _parse_uuid(value: str) -> str:
    try:
        UUID(value)
    except (ValueError, AttributeError) as error:
        raise UntrustedIdentityError("持久化 身份 不可信") from error
    return value


def _parse_rfc3339(value: str) -> datetime:
    if (
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})",
            value,
        )
        is None
    ):
        raise UntrustedIdentityError("持久化 身份 不可信")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UntrustedIdentityError("持久化 身份 不可信") from error
    if parsed.tzinfo is None:
        raise UntrustedIdentityError("持久化 身份 不可信")
    return parsed.astimezone(UTC)


class TrustedIngressAdapter:
    def __init__(
        self,
        *,
        secret: str,
        trust_source: str,
        max_age: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        secret_bytes = secret.encode()
        if len(secret_bytes) < 32 or not trust_source.strip() or max_age <= timedelta(0):
            raise ValueError("可信 入口 需要 32-字节 密钥, 来源, 和 最大有效期")
        self._secret = secret_bytes
        self._trust_source = trust_source.strip()
        self._max_age = max_age
        self._now = now or (lambda: datetime.now(UTC))

    def authenticate(self, request: IdentityRequest, requested_workspace_id: str) -> WorkspaceScope:
        method = request.method.strip().upper()
        path = request.path.strip()
        request_id = request.request_id.strip()
        requested_workspace_id = requested_workspace_id.strip()
        if not method or not path or not request_id:
            raise UntrustedIdentityError("持久化 身份 不可信")

        principal_type = _header(request.headers, HEADER_PRINCIPAL_TYPE).lower()
        principal_id = _header(request.headers, HEADER_PRINCIPAL_ID)
        organization_id = _header(request.headers, HEADER_ORGANIZATION_ID)
        workspace_id = _header(request.headers, HEADER_WORKSPACE_ID)
        issued_at_raw = _header(request.headers, HEADER_ISSUED_AT)
        roles_raw = _header(request.headers, HEADER_ROLES)
        signature = _header(request.headers, HEADER_SIGNATURE)

        if principal_type not in {"user", "service"}:
            raise UntrustedIdentityError("持久化 身份 不可信")
        _parse_uuid(principal_id)
        _parse_uuid(organization_id)
        _parse_uuid(workspace_id)

        issued_at = _parse_rfc3339(issued_at_raw)
        now = self._now().astimezone(UTC)
        if issued_at > now + timedelta(seconds=30) or now - issued_at > self._max_age:
            raise UntrustedIdentityError("持久化 身份 不可信")
        if _LOWER_SHA256_HEX.fullmatch(signature) is None:
            raise UntrustedIdentityError("持久化 身份 不可信")

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
                issued_at_raw,
                roles_raw,
            )
        )
        expected = hmac.new(self._secret, canonical.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise UntrustedIdentityError("持久化 身份 不可信")
        if not requested_workspace_id or workspace_id != requested_workspace_id:
            raise UntrustedIdentityError("持久化 身份 不可信")

        roles = tuple(
            sorted({role.strip().lower() for role in roles_raw.split(",") if role.strip()})
        )
        return WorkspaceScope(
            principal=Principal(
                type=principal_type,
                id=principal_id,
                organization_id=organization_id,
                roles=roles,
            ),
            workspace_id=workspace_id,
            trust_source=self._trust_source,
            trusted=True,
            issued_at=issued_at,
        )


__all__ = [
    "HEADER_ORGANIZATION_ID",
    "HEADER_PRINCIPAL_ID",
    "HEADER_PRINCIPAL_TYPE",
    "HEADER_ROLES",
    "HEADER_SIGNATURE",
    "HEADER_WORKSPACE_ID",
    "IdentityRequest",
    "Principal",
    "TrustedIngressAdapter",
    "UntrustedIdentityError",
    "WorkspaceScope",
]
