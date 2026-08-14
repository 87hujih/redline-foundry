import pytest
from pydantic import ValidationError

from docreview.config.settings import load_settings

VALID_PRODUCTION_ENV = {
    "APP_ENV": "production",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com,https://admin.example.com",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "s" * 32,
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "300000",
}


def test_loads_valid_production_settings() -> None:
    settings = load_settings(VALID_PRODUCTION_ENV)

    assert settings.app_env == "production"
    assert settings.cors_allowed_origins == (
        "https://app.example.com",
        "https://admin.example.com",
    )
    assert settings.trusted_ingress is not None
    assert settings.trusted_ingress.source == "edge-proxy"
    assert settings.uvicorn_workers == 1
    assert settings.runtime_worker_enabled is False
    assert settings.projection_worker_enabled is False


def test_trusted_ingress_secret_minimum_counts_utf8_bytes() -> None:
    settings = load_settings(
        VALID_PRODUCTION_ENV | {"AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "密" * 11}
    )

    assert settings.trusted_ingress is not None


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"CORS_ALLOWED_ORIGINS": ""}, "CORS_ALLOWED_ORIGINS"),
        ({"CORS_ALLOWED_ORIGINS": "*"}, "通配符"),
        ({"CORS_ALLOWED_ORIGINS": "ftp://app.example.com"}, "http 或 https"),
        ({"CORS_ALLOWED_ORIGINS": "https://user@app.example.com"}, "凭据"),
        ({"CORS_ALLOWED_ORIGINS": "https://app.example.com/path"}, "路径"),
        ({"AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "short"}, "至少 32"),
        ({"AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": ""}, "trusted-ingress"),
        ({"AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "0"}, "greater than 0"),
        ({"UVICORN_WORKERS": "2"}, "1"),
        ({"RUNTIME_WORKER_ENABLED": "true"}, "同时启用"),
        ({"PROJECTION_WORKER_ENABLED": "true"}, "同时启用"),
        ({"SERVER_HOST": "  "}, "SERVER_HOST"),
        ({"LOG_LEVEL": "verbose"}, "LOG_LEVEL"),
    ],
)
def test_production_settings_fail_closed(override: dict[str, str], match: str) -> None:
    environment = VALID_PRODUCTION_ENV | override

    with pytest.raises(ValidationError, match=match):
        load_settings(environment)


def test_partial_trusted_ingress_configuration_fails_in_development() -> None:
    with pytest.raises(ValidationError, match="trusted-ingress"):
        load_settings({"AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy"})


def test_production_requires_trusted_ingress_configuration() -> None:
    with pytest.raises(ValidationError, match="trusted-ingress"):
        load_settings(
            {
                "APP_ENV": "production",
                "CORS_ALLOWED_ORIGINS": "https://app.example.com",
            }
        )


def test_settings_do_not_consume_unlisted_values() -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql://production.example/prod",
            "UNRELATED_SECRET": "must-not-be-loaded",
        }
    )

    assert settings.app_env == "development"
    assert "database" not in repr(settings).lower()
    assert "must-not-be-loaded" not in repr(settings)


def test_normalizes_environment_host_and_log_level() -> None:
    settings = load_settings(
        {"APP_ENV": " TEST ", "SERVER_HOST": " 127.0.0.1 ", "LOG_LEVEL": "warning"}
    )

    assert settings.app_env == "test"
    assert settings.server_host == "127.0.0.1"
    assert settings.log_level == "WARNING"


def test_runtime_and_projection_workers_may_only_be_enabled_together() -> None:
    settings = load_settings(
        {"RUNTIME_WORKER_ENABLED": "true", "PROJECTION_WORKER_ENABLED": "true"}
    )

    assert settings.runtime_worker_enabled is True
    assert settings.projection_worker_enabled is True
