from __future__ import annotations

import httpx
import pytest

from docreview.providers.web_search import (
    HTTPWebSearchTransport,
    WebSearchError,
    WebSearchProvider,
)


@pytest.mark.anyio
async def test_web_search_is_bounded_and_preserves_request_identity() -> None:
    received: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received
        received = request
        return httpx.Response(
            200,
            json={
                "items": [
                    {"title": "A", "url": "https://example.com/a", "snippet": "one"},
                    {"title": "B", "url": "https://example.com/b", "snippet": "two"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = WebSearchProvider(
            HTTPWebSearchTransport(
                client=client,
                base_url="https://search.example/api",
                api_key="secret",
                timeout_ms=1_000,
            )
        )
        result = await provider.search(
            "policy", limit=1, request_id="request-1", trace_id="trace-1"
        )

    assert [item.title for item in result.items] == ["A"]
    assert received is not None
    assert received.headers["X-Request-ID"] == "request-1"
    assert received.headers["X-Trace-ID"] == "trace-1"
    assert received.url.params["q"] == "policy"
    assert received.url.params["limit"] == "1"


@pytest.mark.anyio
async def test_web_search_rejects_invalid_or_oversize_responses() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"{}"))
    ) as client:
        transport = HTTPWebSearchTransport(
            client=client,
            base_url="https://search.example",
            api_key=None,
            timeout_ms=1_000,
            max_response_bytes=1,
        )
        with pytest.raises(WebSearchError, match="size"):
            await transport.search("query", 1, request_id="request", trace_id="trace")


@pytest.mark.anyio
async def test_web_search_rejects_duplicate_response_keys() -> None:
    body = b'{"items":[],"items":[]}'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body))
    ) as client:
        transport = HTTPWebSearchTransport(
            client=client,
            base_url="https://search.example",
            api_key=None,
            timeout_ms=1_000,
        )
        with pytest.raises(WebSearchError, match="invalid JSON"):
            await transport.search("query", 1, request_id="request", trace_id="trace")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "https://user:secret@example.com/result", "//example.com/result"],
)
async def test_web_search_rejects_unsafe_result_urls(url: str) -> None:
    class ResultTransport:
        async def search(self, query: str, limit: int, *, request_id: str, trace_id: str) -> object:
            return {"items": [{"title": "unsafe", "url": url, "snippet": "value"}]}

    provider = WebSearchProvider(ResultTransport())
    with pytest.raises(WebSearchError, match="URL is unsafe"):
        await provider.search("query", request_id="request", trace_id="trace")
