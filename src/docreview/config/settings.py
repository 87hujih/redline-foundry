"""只读 Phase 2 服务的 fail-closed 应用配置。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _validate_origin(origin: str) -> None:
    if origin == "*":
        raise ValueError("CORS_ALLOWED_ORIGINS 不能包含通配符")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CORS_ALLOWED_ORIGINS 的来源协议必须是 http 或 https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("CORS_ALLOWED_ORIGINS 不能包含凭据或缺少主机")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("CORS_ALLOWED_ORIGINS 必须只包含来源, 不能带路径、查询参数或片段")


def _repository_root() -> Path | None:
    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return candidate
    return None


def _resolve_storage_root(value: str | Path) -> Path:
    raw = str(value).strip()
    if not raw:
        raise ValueError("UPLOAD_STORAGE_DIR 不能为空")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    protected = {
        Path(resolved.anchor).resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
    }
    repository = _repository_root()
    if repository is not None:
        protected.add(repository.resolve())
    if resolved in protected or (resolved / ".git").exists():
        raise ValueError("UPLOAD_STORAGE_DIR 不能是根目录、用户目录或仓库根目录")
    return resolved


class TrustedIngressSettings(BaseModel):
    """签名 trusted-ingress 兼容适配器的配置。

    本模型校验 Phase 2 HMAC 适配器实际使用的 trusted-ingress 值。
    """

    model_config = ConfigDict(frozen=True)

    secret: SecretStr
    source: str
    max_age_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_secret(self) -> Self:
        if len(self.secret.get_secret_value().encode()) < 32:
            raise ValueError("可信-入口 密钥 至少 32 个字符")
        if not self.source.strip():
            raise ValueError("trusted-ingress 来源不能为空")
        return self


class Settings(BaseSettings):
    """应用配置。

    配置按 pydantic-settings 的默认优先级加载：显式进程环境覆盖仓库根目录
    `.env`，再回退到字段默认值。秘密值只能通过本地未跟踪的 `.env` 或秘密
    管理系统提供，不能写入源码。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
        hide_input_in_errors=True,
        validate_default=True,
    )

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    server_host: str = Field(default="127.0.0.1", validation_alias="SERVER_HOST")
    server_port: int = Field(default=8080, ge=1, le=65535, validation_alias="SERVER_PORT")
    cors_allowed_origins_raw: str = Field(
        default="", validation_alias="CORS_ALLOWED_ORIGINS", repr=False
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    uvicorn_workers: int = Field(default=1, validation_alias="UVICORN_WORKERS")
    runtime_worker_enabled: bool = Field(default=False, validation_alias="RUNTIME_WORKER_ENABLED")
    projection_worker_enabled: bool = Field(
        default=False, validation_alias="PROJECTION_WORKER_ENABLED"
    )
    runtime_worker_id: str | None = Field(default=None, validation_alias="RUNTIME_WORKER_ID")
    # 离线路由/契约测试刻意允许 DATABASE_URL 缺省；生产 Runtime 装配必须在
    # 构造任一 repository 前校验它。
    database_url: SecretStr | None = Field(
        default=None, validation_alias="DATABASE_URL", repr=False
    )
    database_min_size: int = Field(default=2, validation_alias="DATABASE_MIN_SIZE", repr=False)
    database_max_size: int = Field(default=10, validation_alias="DATABASE_MAX_SIZE", repr=False)
    database_timeout_seconds: float = Field(
        default=30.0, validation_alias="DATABASE_POOL_TIMEOUT_SECONDS", repr=False
    )

    siliconflow_api_key: SecretStr | None = Field(
        default=None, validation_alias="SILICONFLOW_API_KEY", repr=False
    )
    siliconflow_base_url: str | None = Field(default=None, validation_alias="SILICONFLOW_BASE_URL")
    llm_model: str | None = Field(default=None, validation_alias="LLM_MODEL")
    embedding_model: str | None = Field(default=None, validation_alias="EMBEDDING_MODEL")
    embedding_dim: int | None = Field(default=None, validation_alias="EMBEDDING_DIM")
    reranker_model: str | None = Field(default=None, validation_alias="RERANKER_MODEL")
    llm_timeout_ms: int | None = Field(default=None, validation_alias="LLM_TIMEOUT_MS")
    llm_retry_max: int | None = Field(default=None, validation_alias="LLM_RETRY_MAX")
    llm_retry_backoff_ms: int | None = Field(default=None, validation_alias="LLM_RETRY_BACKOFF_MS")

    document_parser: str = Field(default="text", validation_alias="DOCUMENT_PARSER")
    chunk_profile: str = Field(
        default="docreview-review-structure-2026-08-17", validation_alias="CHUNK_PROFILE"
    )
    embedding_tokenizer_profile: str | None = Field(
        default=None, validation_alias="EMBEDDING_TOKENIZER_PROFILE"
    )
    embedding_tokenizer_path: Path | None = Field(
        default=None, validation_alias="EMBEDDING_TOKENIZER_PATH"
    )
    tika_url: str | None = Field(default=None, validation_alias="TIKA_URL")
    tika_timeout_ms: int | None = Field(default=None, validation_alias="TIKA_TIMEOUT_MS")
    upload_storage_dir: Path = Field(
        default=Path("data/uploads"), validation_alias="UPLOAD_STORAGE_DIR"
    )
    upload_max_bytes: int = Field(default=20 * 1024 * 1024, validation_alias="UPLOAD_MAX_BYTES")
    web_search_url: str | None = Field(default=None, validation_alias="WEB_SEARCH_URL")
    web_search_api_key: SecretStr | None = Field(
        default=None, validation_alias="WEB_SEARCH_API_KEY", repr=False
    )
    web_search_timeout_ms: int = Field(default=10_000, validation_alias="WEB_SEARCH_TIMEOUT_MS")
    web_search_max_results: int = Field(default=5, validation_alias="WEB_SEARCH_MAX_RESULTS")

    trusted_ingress_secret: SecretStr | None = Field(
        default=None,
        validation_alias="AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET",
        repr=False,
    )
    trusted_ingress_source: str | None = Field(
        default=None, validation_alias="AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE"
    )
    trusted_ingress_max_age_ms: int | None = Field(
        default=None, validation_alias="AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS"
    )

    @field_validator("app_env")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("server_host")
    @classmethod
    def validate_server_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SERVER_HOST 不能为空")
        return value.strip()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 无效")
        return normalized

    @field_validator("siliconflow_api_key")
    @classmethod
    def validate_provider_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        normalized = value.get_secret_value().strip()
        if not normalized:
            raise ValueError("SILICONFLOW_API_KEY 不能为空")
        return SecretStr(normalized)

    @field_validator("llm_model", "embedding_model", "reranker_model")
    @classmethod
    def validate_provider_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("提供方 模型 不能为空")
        return normalized

    @field_validator("siliconflow_base_url")
    @classmethod
    def validate_provider_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("SILICONFLOW_BASE_URL 端口无效") from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("SILICONFLOW_BASE_URL 必须是无凭据、查询或片段的 HTTPS URL")
        return normalized

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dimension(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= 65536:
            raise ValueError("EMBEDDING_DIM 必须在 1 到 65536 之间")
        return value

    @field_validator("llm_timeout_ms")
    @classmethod
    def validate_provider_timeout(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= 600000:
            raise ValueError("LLM_TIMEOUT_MS 必须在 1 到 600000 之间")
        return value

    @field_validator("llm_retry_max")
    @classmethod
    def validate_provider_retry_count(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= 10:
            raise ValueError("LLM_RETRY_MAX 必须在 0 到 10 之间")
        return value

    @field_validator("llm_retry_backoff_ms")
    @classmethod
    def validate_provider_retry_backoff(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= 60000:
            raise ValueError("LLM_RETRY_BACKOFF_MS 必须在 1 到 60000 之间")
        return value

    @field_validator("document_parser")
    @classmethod
    def validate_document_parser(cls, value: str) -> str:
        normalized = value.strip().lower() or "text"
        if normalized not in {"text", "tika", "structured"}:
            raise ValueError("DOCUMENT_PARSER 必须是 文本、tika 或 结构化")
        return normalized

    @field_validator("chunk_profile")
    @classmethod
    def validate_chunk_profile(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != "docreview-review-structure-2026-08-17":
            raise ValueError("CHUNK_PROFILE 必须是批准的结构化切块配置档")
        return normalized

    @field_validator("embedding_tokenizer_profile")
    @classmethod
    def validate_embedding_tokenizer_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("EMBEDDING_TOKENIZER_PROFILE 无效")
        return normalized

    @field_validator("embedding_tokenizer_path")
    @classmethod
    def validate_embedding_tokenizer_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()

    @field_validator("tika_url")
    @classmethod
    def validate_tika_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("TIKA_URL 不能为空")
        parsed = urlsplit(normalized)
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("TIKA_URL 端口无效") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("TIKA_URL 必须是无凭据、查询或片段的 HTTP/HTTPS 服务地址")
        return normalized

    @field_validator("tika_timeout_ms")
    @classmethod
    def validate_tika_timeout(cls, value: int | None) -> int | None:
        if value is not None and not 0 < value <= 600000:
            raise ValueError("TIKA_TIMEOUT_MS 必须在 1 到 600000 之间")
        return value

    @field_validator("upload_storage_dir", mode="before")
    @classmethod
    def validate_upload_storage_dir(cls, value: object) -> Path:
        if not isinstance(value, (str, Path)):
            raise ValueError("UPLOAD_STORAGE_DIR 必须是路径")
        return _resolve_storage_root(value)

    @field_validator("upload_max_bytes")
    @classmethod
    def validate_upload_max_bytes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("UPLOAD_MAX_BYTES 必须大于 0")
        return value

    @field_validator("web_search_url")
    @classmethod
    def validate_web_search_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            not normalized
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WEB_SEARCH_URL 必须是无凭据、查询或片段的 HTTP/HTTPS URL")
        return normalized

    @field_validator("web_search_api_key")
    @classmethod
    def validate_web_search_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        normalized = value.get_secret_value().strip()
        if not normalized:
            raise ValueError("WEB_SEARCH_API_KEY 不能为空")
        return SecretStr(normalized)

    @field_validator("web_search_timeout_ms")
    @classmethod
    def validate_web_search_timeout(cls, value: int) -> int:
        if not 1 <= value <= 120_000:
            raise ValueError("WEB_SEARCH_TIMEOUT_MS 必须在 1 到 120000 之间")
        return value

    @field_validator("web_search_max_results")
    @classmethod
    def validate_web_search_results(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("WEB_SEARCH_MAX_RESULTS 必须在 1 到 20 之间")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError("DATABASE_URL 不能为空")
        parsed = urlsplit(raw)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("DATABASE_URL 必须是 PostgreSQL URL")
        if parsed.path in {"", "/"}:
            raise ValueError("DATABASE_URL 必须包含数据库名")
        return SecretStr(raw)

    @field_validator("database_min_size", "database_max_size")
    @classmethod
    def validate_database_pool_size(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("数据库连接池大小必须在 1 到 100 之间")
        return value

    @field_validator("database_timeout_seconds")
    @classmethod
    def validate_database_timeout(cls, value: float) -> float:
        if value <= 0 or value > 300:
            raise ValueError("DATABASE_POOL_TIMEOUT_SECONDS 必须在 0 到 300 之间")
        return value

    @field_validator("runtime_worker_id")
    @classmethod
    def validate_runtime_worker_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("RUNTIME_WORKER_ID 不能为空")
        return normalized

    @property
    def cors_allowed_origins(self) -> tuple[str, ...]:
        return _csv(self.cors_allowed_origins_raw)

    @property
    def trusted_ingress(self) -> TrustedIngressSettings | None:
        if self.trusted_ingress_secret is None:
            return None
        return TrustedIngressSettings(
            secret=self.trusted_ingress_secret,
            source=self.trusted_ingress_source or "",
            max_age_ms=self.trusted_ingress_max_age_ms or 0,
        )

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Self:
        environment = self.app_env
        if environment not in {"development", "test", "production"}:
            raise ValueError("APP_ENV 必须是 development、test 或 production")

        origins = self.cors_allowed_origins
        if environment == "production" and not origins:
            raise ValueError("生产环境必须配置 CORS_ALLOWED_ORIGINS")
        for origin in origins:
            _validate_origin(origin)

        ingress_values = (
            self.trusted_ingress_secret,
            self.trusted_ingress_source,
            self.trusted_ingress_max_age_ms,
        )
        if environment == "production" and not any(value is not None for value in ingress_values):
            raise ValueError("生产环境必须配置 trusted-ingress")
        if any(value is not None for value in ingress_values):
            if not all(value is not None for value in ingress_values):
                raise ValueError("trusted-ingress 配置必须同时提供密钥、来源和 max_age_ms")
            secret = self.trusted_ingress_secret
            if secret is None:
                raise ValueError("可信-入口 密钥 不能为空")
            TrustedIngressSettings(
                secret=secret,
                source=self.trusted_ingress_source or "",
                max_age_ms=self.trusted_ingress_max_age_ms or 0,
            )
        if self.uvicorn_workers != 1:
            raise ValueError("当前阶段只允许 1 个 Uvicorn 工作进程")
        if self.runtime_worker_enabled != self.projection_worker_enabled:
            raise ValueError("运行时 和 投影 工作进程 必须同时启用或同时关闭")
        if self.database_min_size > self.database_max_size:
            raise ValueError("DATABASE_MIN_SIZE 不能大于 DATABASE_MAX_SIZE")
        if self.document_parser == "tika" or (
            environment == "production" and self.document_parser == "structured"
        ):
            missing_tika = [
                name
                for name, value in (
                    ("TIKA_URL", self.tika_url),
                    ("TIKA_TIMEOUT_MS", self.tika_timeout_ms),
                )
                if value is None
            ]
            if missing_tika:
                raise ValueError("Tika 模式缺少必填配置: " + ", ".join(missing_tika))
        if environment == "production":
            if self.document_parser != "structured":
                raise ValueError("生产 DOCUMENT_PARSER 必须是 结构化")
            if self.embedding_tokenizer_profile is None:
                raise ValueError("生产环境必须配置 EMBEDDING_TOKENIZER_PROFILE")
            if self.database_url is None:
                raise ValueError("生产环境必须配置 DATABASE_URL")
            provider_values = {
                "SILICONFLOW_API_KEY": self.siliconflow_api_key,
                "SILICONFLOW_BASE_URL": self.siliconflow_base_url,
                "LLM_MODEL": self.llm_model,
                "EMBEDDING_MODEL": self.embedding_model,
                "EMBEDDING_DIM": self.embedding_dim,
                "RERANKER_MODEL": self.reranker_model,
                "LLM_TIMEOUT_MS": self.llm_timeout_ms,
                "LLM_RETRY_MAX": self.llm_retry_max,
                "LLM_RETRY_BACKOFF_MS": self.llm_retry_backoff_ms,
            }
            missing = [name for name, value in provider_values.items() if value is None]
            if missing:
                raise ValueError("生产环境提供方缺少必填配置: " + ", ".join(missing))
            if (self.runtime_worker_enabled or self.projection_worker_enabled) and not (
                self.runtime_worker_id or ""
            ).strip():
                raise ValueError("启用生产 运行时 时必须配置 RUNTIME_WORKER_ID")
        return self


class _ExplicitSettings(Settings):
    """显式映射校验器，不接触进程环境、`.env` 或 secrets directory。"""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)


_ENV_FIELDS = {
    "APP_ENV": "app_env",
    "SERVER_HOST": "server_host",
    "SERVER_PORT": "server_port",
    "CORS_ALLOWED_ORIGINS": "cors_allowed_origins_raw",
    "LOG_LEVEL": "log_level",
    "UVICORN_WORKERS": "uvicorn_workers",
    "RUNTIME_WORKER_ENABLED": "runtime_worker_enabled",
    "PROJECTION_WORKER_ENABLED": "projection_worker_enabled",
    "RUNTIME_WORKER_ID": "runtime_worker_id",
    "DATABASE_URL": "database_url",
    "DATABASE_MIN_SIZE": "database_min_size",
    "DATABASE_MAX_SIZE": "database_max_size",
    "DATABASE_POOL_TIMEOUT_SECONDS": "database_timeout_seconds",
    "SILICONFLOW_API_KEY": "siliconflow_api_key",
    "SILICONFLOW_BASE_URL": "siliconflow_base_url",
    "LLM_MODEL": "llm_model",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_DIM": "embedding_dim",
    "RERANKER_MODEL": "reranker_model",
    "LLM_TIMEOUT_MS": "llm_timeout_ms",
    "LLM_RETRY_MAX": "llm_retry_max",
    "LLM_RETRY_BACKOFF_MS": "llm_retry_backoff_ms",
    "DOCUMENT_PARSER": "document_parser",
    "CHUNK_PROFILE": "chunk_profile",
    "EMBEDDING_TOKENIZER_PROFILE": "embedding_tokenizer_profile",
    "EMBEDDING_TOKENIZER_PATH": "embedding_tokenizer_path",
    "TIKA_URL": "tika_url",
    "TIKA_TIMEOUT_MS": "tika_timeout_ms",
    "UPLOAD_STORAGE_DIR": "upload_storage_dir",
    "UPLOAD_MAX_BYTES": "upload_max_bytes",
    "WEB_SEARCH_URL": "web_search_url",
    "WEB_SEARCH_API_KEY": "web_search_api_key",
    "WEB_SEARCH_TIMEOUT_MS": "web_search_timeout_ms",
    "WEB_SEARCH_MAX_RESULTS": "web_search_max_results",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "trusted_ingress_secret",
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "trusted_ingress_source",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "trusted_ingress_max_age_ms",
}


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """从显式映射或进程环境/`.env` 加载配置。

    显式映射用于离线测试并保持与真实进程环境隔离；生产调用不传映射，
    由 ``Settings()`` 负责合并进程环境、`.env` 和默认值。
    """

    if environment is None:
        return Settings()

    values = environment
    normalized = {
        field_name: values[environment_name]
        for environment_name, field_name in _ENV_FIELDS.items()
        if environment_name in values
    }
    return _ExplicitSettings.model_validate(normalized)


__all__ = ["Settings", "TrustedIngressSettings", "load_settings"]
