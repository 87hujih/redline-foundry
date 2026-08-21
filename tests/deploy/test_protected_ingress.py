from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "deploy" / "nginx" / "nginx.conf.template"
SIGNER = ROOT / "deploy" / "nginx" / "trusted_ingress.js"
FIXED_CONFIG = ROOT / "deploy" / "nginx" / "nginx.fixed-identity.conf.template"
FIXED_SIGNER = ROOT / "deploy" / "nginx" / "fixed_identity.js"


def test_protected_ingress_has_one_writer_and_no_fallback() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    assert config.count("upstream docreview_python") == 1
    assert "proxy_pass http://docreview_python" in config
    assert "proxy_next_upstream" not in config


def test_protected_ingress_authenticates_strips_and_resigns_identity() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    assert "auth_request /_identity_authorize" in config
    assert "proxy_ssl_verify on" in config
    assert "auth_client_certificate" in config
    for name in (
        "X-DocReview-Principal-Type",
        "X-DocReview-Principal-ID",
        "X-DocReview-Organization-ID",
        "X-DocReview-Workspace-ID",
        "X-DocReview-Identity-Issued-At",
        "X-DocReview-Roles",
        "X-DocReview-Identity-Signature",
    ):
        assert f'proxy_set_header {name} ""' in config
        assert f"proxy_set_header {name} $" in config


def test_protected_ingress_binds_signature_and_bounds_streams() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    signer = SIGNER.read_text(encoding="utf-8")
    assert "ssl_protocols TLSv1.2 TLSv1.3" in config
    assert "client_max_body_size 20m" in config
    assert "proxy_buffering off" in config
    assert "X-Request-ID $trusted_request_id" in config
    assert "r.method.toUpperCase()" in signer
    assert "r.uri" in signer
    assert "r.variables.auth_workspace_id" in signer
    assert "createHmac('sha256'" in signer
    assert "process.env[identitySecretName]" in signer


def test_fixed_identity_gateway_is_same_origin_and_api_only() -> None:
    config = FIXED_CONFIG.read_text(encoding="utf-8")
    assert "root /srv/docreview/frontend" in config
    assert "try_files $uri $uri/ /index.html" in config
    assert "location ~ ^/api(?:/|$)" in config
    assert "proxy_pass http://docreview_python" in config
    assert config.count("proxy_pass http://docreview_python") == 1
    assert "auth_request" not in config


def test_fixed_identity_gateway_uses_only_server_owned_identity() -> None:
    config = FIXED_CONFIG.read_text(encoding="utf-8")
    signer = FIXED_SIGNER.read_text(encoding="utf-8")
    variables = (
        "DOCREVIEW_FIXED_PRINCIPAL_TYPE",
        "DOCREVIEW_FIXED_PRINCIPAL_ID",
        "DOCREVIEW_FIXED_ORGANIZATION_ID",
        "DOCREVIEW_FIXED_WORKSPACE_ID",
        "DOCREVIEW_FIXED_ROLES",
    )
    for name in variables:
        assert f"env {name};" in config
        assert f"process.env['{name}']" not in config
        assert name in signer
    assert "env AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET;" in config
    assert "process.env[secretName]" in signer

    header_variables = {
        "Principal-Type": "identity_principal_type",
        "Principal-ID": "identity_principal_id",
        "Organization-ID": "identity_organization_id",
        "Workspace-ID": "identity_workspace_id",
        "Identity-Issued-At": "identity_issued_at",
        "Roles": "identity_roles",
        "Identity-Signature": "identity_signature",
    }
    for header, variable in header_variables.items():
        assert f"proxy_set_header X-DocReview-{header} ${variable};" in config
    assert "$http_x_docreview" not in config.lower()


def test_fixed_identity_signature_and_sse_cursor_match_backend_contract() -> None:
    config = FIXED_CONFIG.read_text(encoding="utf-8")
    signer = FIXED_SIGNER.read_text(encoding="utf-8")
    assert "X-Request-ID $trusted_request_id" in config
    assert "Last-Event-ID $http_last_event_id" in config
    assert "proxy_buffering off" in config
    assert "r.variables.trusted_request_id" in signer
    assert "r.method.toUpperCase()" in signer
    assert "r.uri" in signer
    assert "r.variables.identity_workspace_id" in signer
    assert "r.variables.identity_issued_at" in signer
    assert "createHmac('sha256'" in signer
