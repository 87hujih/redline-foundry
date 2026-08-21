"""有界、可接入 Policy 的 Web Search provider。

Provider 只负责远程 search/fetch I/O。Workspace 授权、rate limit、audit 和
approval 仍由 ToolRuntime 负责。传输层可注入, 单元测试不会访问公网。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    title: str
    url: str
    snippet: str
    source: str = "web"


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    query: str
    items: tuple[WebSearchItem, ...]
    provider: str


class WebSearchError(RuntimeError):
    pass


class WebSearchTransport(Protocol):
    async def search(self, query: str, limit: int, *, request_id: str, trace_id: str) -> object: ...


class HTTPWebSearchTransport:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str | SecretStr | None,
        timeout_ms: int,
        provider_name: str = "web-search",
        max_response_bytes: int = 1_048_576,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("WEB_SEARCH_URL 必须是 HTTP(S) URL")
        if timeout_ms <= 0 or max_response_bytes <= 0 or not provider_name.strip():
            raise ValueError("网页搜索传输配置无效")
        self._client = client
        self._url = base_url.rstrip("/")
        self._api_key = (
            api_key.get_secret_value() if isinstance(api_key, SecretStr) else (api_key or "")
        ).strip()
        self._timeout = timeout_ms / 1000
        self._provider_name = provider_name.strip()
        self._max_response_bytes = max_response_bytes

    async def search(self, query: str, limit: int, *, request_id: str, trace_id: str) -> object:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("网页搜索查询和限制无效")
        headers = {"Accept": "application/json", "X-Request-ID": request_id, "X-Trace-ID": trace_id}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.get(
                    self._url + "/search",
                    params={"q": query.strip(), "limit": limit},
                    headers=headers,
                )
        except (TimeoutError, httpx.TimeoutException) as error:
            raise WebSearchError("网页搜索请求超时") from error
        except httpx.HTTPError as error:
            raise WebSearchError("网页搜索传输失败") from error
        if response.status_code == 429:
            raise WebSearchError("网页搜索速率超出限制")
        if response.status_code < 200 or response.status_code >= 300:
            raise WebSearchError("网页搜索提供方拒绝了请求")
        body = response.content
        if len(body) > self._max_response_bytes:
            raise WebSearchError("web search response size exceeds the limit")
        try:
            return json.loads(body, object_pairs_hook=_unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise WebSearchError("web search returned invalid JSON") from error

    @property
    def provider_name(self) -> str:
        return self._provider_name


class WebSearchProvider:
    def __init__(self, transport: WebSearchTransport, *, provider_name: str = "web-search") -> None:
        if not provider_name.strip():
            raise ValueError("必须提供网页搜索提供方名称")
        self._transport = transport
        self.provider_name = provider_name.strip()

    async def search(
        self, query: str, *, limit: int = 5, request_id: str = "", trace_id: str = ""
    ) -> WebSearchResponse:
        query = query.strip()
        if not query or not 1 <= limit <= 20 or not request_id.strip() or not trace_id.strip():
            raise ValueError("网页搜索请求无效")
        raw = await self._transport.search(query, limit, request_id=request_id, trace_id=trace_id)
        items = _decode_items(raw, limit)
        return WebSearchResponse(query=query, items=tuple(items), provider=self.provider_name)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("网页搜索响应包含重复键")
        result[key] = value
    return result


def _decode_items(raw: object, limit: int) -> list[WebSearchItem]:
    if not isinstance(raw, dict):
        raise WebSearchError("网页搜索响应必须是对象")
    payload = cast(dict[str, object], raw)
    values: object = payload.get("items", payload.get("results", []))
    if not isinstance(values, list):
        raise WebSearchError("网页搜索响应项目必须是数组")
    items: list[WebSearchItem] = []
    for value in cast(list[object], values)[:limit]:
        if not isinstance(value, dict):
            raise WebSearchError("网页搜索结果项必须是对象")
        item_payload = cast(dict[str, object], value)
        title: object = item_payload.get("title")
        url: object = item_payload.get("url", item_payload.get("link"))
        snippet: object = item_payload.get("snippet", item_payload.get("description", ""))
        if not isinstance(title, str) or not title.strip():
            raise WebSearchError("网页搜索结果标识无效")
        if not isinstance(url, str) or not url.strip():
            raise WebSearchError("网页搜索结果标识无效")
        if not isinstance(snippet, str):
            raise WebSearchError("网页搜索结果摘要无效")
        items.append(WebSearchItem(title.strip(), _result_url(url), snippet.strip()))
    return items


def _result_url(value: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise WebSearchError("web search result URL is unsafe")
    return normalized


__all__ = [
    "HTTPWebSearchTransport",
    "WebSearchError",
    "WebSearchItem",
    "WebSearchProvider",
    "WebSearchResponse",
    "WebSearchTransport",
]
