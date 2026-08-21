from pathlib import Path

import pytest
from pydantic import ValidationError

from docreview.config.settings import Settings, load_settings

VALID_PRODUCTION_ENV = {
    "APP_ENV": "production",
    "CORS_ALLOWED_ORIGINS": "https://app.example.com,https://admin.example.com",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "s" * 32,
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "edge-proxy",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "300000",
    "SILICONFLOW_API_KEY": "test-siliconflow-key",
    "SILICONFLOW_BASE_URL": "https://provider.example/v1",
    "LLM_MODEL": "test-chat-model",
    "EMBEDDING_MODEL": "test-embedding-model",
    "EMBEDDING_DIM": "1024",
    "RERANKER_MODEL": "test-reranker-model",
    "LLM_TIMEOUT_MS": "90000",
    "LLM_RETRY_MAX": "2",
    "LLM_RETRY_BACKOFF_MS": "1000",
    "DATABASE_URL": "postgresql://database.example/docreview",
    "DOCUMENT_PARSER": "structured",
    "TIKA_URL": "http://tika.internal:9998",
    "TIKA_TIMEOUT_MS": "45000",
    "EMBEDDING_TOKENIZER_PROFILE": "docreview-production-tokenizer",
}


def test_settings_configure_local_dotenv_source() -> None:
    assert Settings.model_config.get("env_file") == ".env"
    assert Settings.model_config.get("env_file_encoding") == "utf-8"


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
    assert settings.document_parser == "structured"
    assert settings.upload_storage_dir.is_absolute()
    assert settings.upload_max_bytes == 20 * 1024 * 1024


def test_loads_tika_and_upload_storage_configuration(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"

    settings = load_settings(
        VALID_PRODUCTION_ENV
        | {
            "DOCUMENT_PARSER": " structured ",
            "TIKA_URL": "http://tika.internal:9998/base/",
            "TIKA_TIMEOUT_MS": "45000",
            "UPLOAD_STORAGE_DIR": str(storage_root),
            "UPLOAD_MAX_BYTES": "4096",
        }
    )

    assert settings.document_parser == "structured"
    assert settings.tika_url == "http://tika.internal:9998/base"
    assert settings.tika_timeout_ms == 45000
    assert settings.upload_storage_dir == storage_root.resolve()
    assert settings.upload_max_bytes == 4096


@pytest.mark.parametrize("missing", ["TIKA_URL", "TIKA_TIMEOUT_MS"])
def test_production_tika_configuration_fails_closed_when_missing(missing: str) -> None:
    environment = VALID_PRODUCTION_ENV | {
        "DOCUMENT_PARSER": "structured",
        "TIKA_URL": "http://tika.internal:9998",
        "TIKA_TIMEOUT_MS": "30000",
    }
    del environment[missing]

    with pytest.raises(ValidationError, match=missing):
        load_settings(environment)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://tika.internal:9998",
        "http://user:password@tika.internal:9998",
        "http://tika.internal:9998/#fragment",
        "http://tika.internal:9998/?request_override=true",
        "http:///missing-host",
        "http://tika.internal:not-a-port",
    ],
)
def test_tika_url_rejects_unsafe_service_addresses(url: str) -> None:
    with pytest.raises(ValidationError, match="TIKA_URL"):
        load_settings(
            VALID_PRODUCTION_ENV
            | {
                "DOCUMENT_PARSER": "structured",
                "TIKA_URL": url,
                "TIKA_TIMEOUT_MS": "30000",
            }
        )


@pytest.mark.parametrize("mode", ["auto", "textract", "tika-or-text"])
def test_document_parser_rejects_unknown_modes(mode: str) -> None:
    with pytest.raises(ValidationError, match="DOCUMENT_PARSER"):
        load_settings({"DOCUMENT_PARSER": mode})


@pytest.mark.parametrize("value", ["0", "-1"])
def test_upload_max_bytes_must_be_positive(value: str) -> None:
    with pytest.raises(ValidationError, match="UPLOAD_MAX_BYTES"):
        load_settings({"UPLOAD_MAX_BYTES": value})


def test_upload_storage_root_rejects_broad_deletion_targets() -> None:
    unsafe = {Path.cwd().resolve(), Path.home().resolve(), Path(Path.cwd().anchor).resolve()}

    for root in unsafe:
        with pytest.raises(ValidationError, match="UPLOAD_STORAGE_DIR"):
            load_settings({"UPLOAD_STORAGE_DIR": str(root)})


def test_upload_storage_root_rejects_any_repository_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)

    with pytest.raises(ValidationError, match="UPLOAD_STORAGE_DIR"):
        load_settings({"UPLOAD_STORAGE_DIR": str(repository)})


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


def test_production_requires_database_url() -> None:
    environment = VALID_PRODUCTION_ENV.copy()
    del environment["DATABASE_URL"]

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        load_settings(environment)


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


def test_production_runtime_allows_web_search_to_be_disabled() -> None:
    settings = load_settings(
        VALID_PRODUCTION_ENV
        | {
            "DATABASE_URL": "postgresql://database.example/docreview",
            "RUNTIME_WORKER_ENABLED": "true",
            "PROJECTION_WORKER_ENABLED": "true",
            "RUNTIME_WORKER_ID": "worker-1",
        }
    )

    assert settings.runtime_worker_enabled is True
    assert settings.web_search_url is None


def test_loads_strict_production_provider_settings_without_exposing_key() -> None:
    settings = load_settings(VALID_PRODUCTION_ENV)

    assert settings.siliconflow_base_url == "https://provider.example/v1"
    assert settings.llm_model == "test-chat-model"
    assert settings.embedding_model == "test-embedding-model"
    assert settings.embedding_dim == 1024
    assert settings.reranker_model == "test-reranker-model"
    assert settings.llm_timeout_ms == 90000
    assert settings.llm_retry_max == 2
    assert settings.llm_retry_backoff_ms == 1000
    assert "test-siliconflow-key" not in repr(settings)


@pytest.mark.parametrize(
    "missing",
    [
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "LLM_MODEL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "RERANKER_MODEL",
        "LLM_TIMEOUT_MS",
        "LLM_RETRY_MAX",
        "LLM_RETRY_BACKOFF_MS",
    ],
)
def test_production_provider_configuration_fails_closed_when_missing(missing: str) -> None:
    environment = VALID_PRODUCTION_ENV.copy()
    del environment[missing]

    with pytest.raises(ValidationError, match=missing):
        load_settings(environment)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"SILICONFLOW_BASE_URL": "http://provider.example/v1"}, "SILICONFLOW_BASE_URL"),
        ({"SILICONFLOW_BASE_URL": "https://user@provider.example/v1"}, "SILICONFLOW_BASE_URL"),
        ({"SILICONFLOW_BASE_URL": "https://provider.example/v1?key=value"}, "SILICONFLOW_BASE_URL"),
        ({"LLM_TIMEOUT_MS": "0"}, "LLM_TIMEOUT_MS"),
        ({"LLM_RETRY_MAX": "-1"}, "LLM_RETRY_MAX"),
        ({"LLM_RETRY_BACKOFF_MS": "0"}, "LLM_RETRY_BACKOFF_MS"),
        ({"EMBEDDING_DIM": "0"}, "EMBEDDING_DIM"),
    ],
)
def test_provider_configuration_rejects_unsafe_values(override: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        load_settings(VALID_PRODUCTION_ENV | override)


def test_provider_key_is_redacted_from_validation_errors() -> None:
    exposed = "test-key-that-must-never-appear"

    with pytest.raises(ValidationError) as captured:
        load_settings(
            VALID_PRODUCTION_ENV
            | {
                "SILICONFLOW_API_KEY": exposed,
                "SILICONFLOW_BASE_URL": "not-a-url",
            }
        )

    assert exposed not in str(captured.value)
    assert exposed not in repr(captured.value)
