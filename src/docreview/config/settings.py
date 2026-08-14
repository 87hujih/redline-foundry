"""Fail-closed application settings for the read-only Phase 2 service."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class TrustedIngressSettings(BaseModel):
    """Configuration for the signed trusted-ingress compatibility adapter.

    The model validates the trusted-ingress values consumed by the Phase 2
    HMAC adapter.
    """

    model_config = ConfigDict(frozen=True)

    secret: SecretStr
    source: str
    max_age_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_secret(self) -> Self:
        if len(self.secret.get_secret_value().encode()) < 32:
            raise ValueError("trusted-ingress secret 至少 32 个字符")
        if not self.source.strip():
            raise ValueError("trusted-ingress source 不能为空")
        return self


class Settings(BaseSettings):
    """Application settings.

    The settings source is the process environment only. In particular,
    pydantic-settings is explicitly configured not to read `.env` files.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
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
                raise ValueError("trusted-ingress 配置必须同时提供 secret、source 和 max_age_ms")
            secret = self.trusted_ingress_secret
            if secret is None:
                raise ValueError("trusted-ingress secret 不能为空")
            TrustedIngressSettings(
                secret=secret,
                source=self.trusted_ingress_source or "",
                max_age_ms=self.trusted_ingress_max_age_ms or 0,
            )
        if self.uvicorn_workers != 1:
            raise ValueError("当前阶段只允许 1 个 Uvicorn worker")
        if self.runtime_worker_enabled != self.projection_worker_enabled:
            raise ValueError("Runtime 和 Projection Worker 必须同时启用或同时关闭")
        return self


_ENV_FIELDS = {
    "APP_ENV": "app_env",
    "SERVER_HOST": "server_host",
    "SERVER_PORT": "server_port",
    "CORS_ALLOWED_ORIGINS": "cors_allowed_origins_raw",
    "LOG_LEVEL": "log_level",
    "UVICORN_WORKERS": "uvicorn_workers",
    "RUNTIME_WORKER_ENABLED": "runtime_worker_enabled",
    "PROJECTION_WORKER_ENABLED": "projection_worker_enabled",
    "AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET": "trusted_ingress_secret",
    "AGENT_RUNTIME_TRUSTED_INGRESS_SOURCE": "trusted_ingress_source",
    "AGENT_RUNTIME_TRUSTED_INGRESS_MAX_AGE_MS": "trusted_ingress_max_age_ms",
}


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """Load settings from explicit values or process environment, never `.env`."""

    values = os.environ if environment is None else environment
    normalized = {
        field_name: values[environment_name]
        for environment_name, field_name in _ENV_FIELDS.items()
        if environment_name in values
    }
    return Settings.model_validate(normalized)


__all__ = ["Settings", "TrustedIngressSettings", "load_settings"]
