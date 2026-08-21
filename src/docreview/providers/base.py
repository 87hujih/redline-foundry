"""生产 provider 适配器共享的类型。"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


class ProviderErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    RETRYABLE_UPSTREAM = "retryable_upstream"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    PERMANENT_UPSTREAM = "permanent_upstream"


class ProviderError(Exception):
    """已脱敏且分类的 provider 失败。"""

    def __init__(
        self,
        category: ProviderErrorCategory,
        message: str,
        *,
        status_code: int | None = None,
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_count = retry_count


class ProviderCancelledError(asyncio.CancelledError):
    """保留 asyncio 语义与 provider 分类的取消异常。"""

    category = ProviderErrorCategory.CANCELLED

    def __init__(self, *, retry_count: int = 0) -> None:
        super().__init__("provider request cancelled")
        self.retry_count = retry_count


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int
    base_backoff_ms: int
    max_backoff_ms: int = 30000

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries 必须介于 0 和 10")
        if self.base_backoff_ms <= 0:
            raise ValueError("base_backoff_ms 必须为正数")
        if self.max_backoff_ms < self.base_backoff_ms:
            raise ValueError("max_backoff_ms 必须大于或等于 base_backoff_ms")


@dataclass(frozen=True, slots=True)
class HTTPJSONResponse:
    payload: dict[str, object]
    retry_count: int


Sleeper = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]


class ProviderHTTPTransport:
    """具体 provider 适配器共享的有界 JSON 传输层。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str | SecretStr,
        timeout_ms: int,
        retry_policy: RetryPolicy,
        sleeper: Sleeper = asyncio.sleep,
        jitter: Jitter | None = None,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("提供方基础 URL 无效") from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("提供方基础 URL 无效")
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret.strip() or timeout_ms <= 0 or max_response_bytes <= 0:
            raise ValueError("无效的 提供方 HTTP 配置")
        self._client = client
        self._base_url = normalized_url
        self._api_key = SecretStr(secret.strip())
        self._timeout_seconds = timeout_ms / 1000
        self._retry_policy = retry_policy
        self._sleeper = sleeper
        self._jitter: Jitter = jitter or _default_jitter
        self._max_response_bytes = max_response_bytes

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        request_id: str,
        trace_id: str,
    ) -> HTTPJSONResponse:
        if not path.startswith("/") or not request_id.strip() or not trace_id.strip():
            raise ValueError("提供方请求身份无效")
        # 仅重试已分类的瞬态失败；认证、协议与响应格式错误立即安全拒绝。
        for retry_count in range(self._retry_policy.max_retries + 1):
            try:
                response_payload = await self._post_once(
                    path, payload, request_id=request_id, trace_id=trace_id
                )
                return HTTPJSONResponse(response_payload, retry_count)
            except asyncio.CancelledError as error:
                if isinstance(error, ProviderCancelledError):
                    raise
                raise ProviderCancelledError(retry_count=retry_count) from None
            except (TimeoutError, httpx.TimeoutException):
                failure = ProviderError(
                    ProviderErrorCategory.TIMEOUT,
                    "提供方请求超时",
                    retry_count=retry_count,
                )
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                failure = ProviderError(
                    ProviderErrorCategory.RETRYABLE_UPSTREAM,
                    "提供方传输失败",
                    retry_count=retry_count,
                )
            except ProviderError as error:
                failure = error
            except httpx.HTTPError:
                failure = ProviderError(
                    ProviderErrorCategory.PERMANENT_UPSTREAM,
                    "提供方 请求 失败",
                    retry_count=retry_count,
                )
            if retry_count >= self._retry_policy.max_retries or not _is_retryable(failure):
                failure.retry_count = retry_count
                raise failure from None
            delay = self._backoff_seconds(retry_count)
            try:
                await self._sleeper(delay)
            except asyncio.CancelledError:
                raise ProviderCancelledError(retry_count=retry_count) from None
        raise AssertionError("提供方重试次数已耗尽且没有结果")

    async def _post_once(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        request_id: str,
        trace_id: str,
    ) -> dict[str, object]:
        request = self._client.build_request(
            "POST",
            self._base_url + path,
            headers={
                "Authorization": "Bearer " + self._api_key.get_secret_value(),
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
            },
            json=payload,
        )
        async with asyncio.timeout(self._timeout_seconds):
            response = await self._client.send(request, stream=True)
            try:
                if response.status_code < 200 or response.status_code >= 300:
                    raise _status_error(response.status_code)
                body = await self._read_bounded(response)
            finally:
                await response.aclose()
        try:
            decoded = json.loads(
                body,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                "提供方返回的 JSON 无效",
            ) from error
        if not isinstance(decoded, dict):
            raise ProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                "提供方返回的响应结构无效",
            )
        return cast(dict[str, object], decoded)

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None:
            try:
                if int(declared_size) > self._max_response_bytes:
                    raise ProviderError(
                        ProviderErrorCategory.INVALID_RESPONSE,
                        "提供方响应超出大小限制",
                    )
            except ValueError as error:
                raise ProviderError(
                    ProviderErrorCategory.INVALID_RESPONSE,
                    "提供方 返回了无效的 内容-Length",
                ) from error
        body = bytearray()
        # 流式读取仍执行硬上限，不能把上游 Content-Length 当作可信边界。
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_response_bytes:
                raise ProviderError(
                    ProviderErrorCategory.INVALID_RESPONSE,
                    "提供方响应超出大小限制",
                )
        return bytes(body)

    def _backoff_seconds(self, retry_count: int) -> float:
        exponential_ms = min(
            self._retry_policy.max_backoff_ms,
            self._retry_policy.base_backoff_ms * (2**retry_count),
        )
        jitter_ms = self._jitter(exponential_ms * 0.2)
        return min(self._retry_policy.max_backoff_ms, exponential_ms + jitter_ms) / 1000


def _status_error(status_code: int) -> ProviderError:
    if status_code in {401, 403}:
        category = ProviderErrorCategory.AUTHENTICATION
    elif status_code == 429:
        category = ProviderErrorCategory.RATE_LIMITED
    elif status_code in {502, 503, 504}:
        category = ProviderErrorCategory.RETRYABLE_UPSTREAM
    else:
        category = ProviderErrorCategory.PERMANENT_UPSTREAM
    return ProviderError(
        category,
        f"提供方返回 HTTP 状态码 {status_code}",
        status_code=status_code,
    )


def _is_retryable(error: ProviderError) -> bool:
    return error.category in {
        ProviderErrorCategory.RATE_LIMITED,
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.RETRYABLE_UPSTREAM,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复的 JSON 键")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"无效的 JSON 数字 常量{value}")


def _default_jitter(maximum: float) -> float:
    return random.uniform(0.0, maximum)


__all__ = [
    "HTTPJSONResponse",
    "ProviderCancelledError",
    "ProviderError",
    "ProviderErrorCategory",
    "ProviderHTTPTransport",
    "RetryPolicy",
]
