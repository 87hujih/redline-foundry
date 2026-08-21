from __future__ import annotations

import asyncio

import httpx
import pytest

from docreview.document.tika import (
    HTTPXTikaClient,
    TikaConnectionError,
    TikaEmptyResponseError,
    TikaHTTPStatusError,
    TikaInvalidResponseError,
    TikaResponseTooLargeError,
    TikaTimeoutError,
)


@pytest.mark.asyncio
async def test_tika_client_preserves_request_contract() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, content="解析结果".encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="http://tika.internal:9998/base",
            timeout_ms=45000,
            max_response_bytes=1024,
        )

        result = await client.parse("review.docx", b"binary-document")

    assert result == "解析结果"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert str(request.url) == "http://tika.internal:9998/base/tika"
    assert request.headers["Accept"] == "text/plain"
    assert "Content-Type" not in request.headers
    assert "Authorization" not in request.headers
    assert request.content == b"binary-document"


@pytest.mark.asyncio
async def test_tika_structured_client_requests_tika_3_xml_xhtml_representation() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>text</p></body></html>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="http://tika.internal:9998",
            timeout_ms=45000,
            max_response_bytes=1024,
        )

        result = await client.parse_structured("review.docx", b"binary-document")

    assert result.startswith('<html xmlns="http://www.w3.org/1999/xhtml">')
    assert len(requests) == 1
    assert requests[0].headers["Accept"] == "text/xml"
    assert requests[0].content == b"binary-document"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_tika_http_failures_are_not_retried_or_expose_response(status: int) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"sensitive full response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="https://tika.internal",
            timeout_ms=100,
            max_response_bytes=1024,
        )
        with pytest.raises(TikaHTTPStatusError) as captured:
            await client.parse("review.pdf", b"private file contents")

    assert captured.value.status_code == status
    assert "sensitive full response" not in str(captured.value)
    assert "private file contents" not in str(captured.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_tika_timeout_is_classified_without_retry() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="https://tika.internal",
            timeout_ms=10,
            max_response_bytes=1024,
        )
        with pytest.raises(TikaTimeoutError):
            await client.parse("review.pdf", b"document")

    assert calls == 1


@pytest.mark.asyncio
async def test_tika_cancellation_propagates() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="https://tika.internal",
            timeout_ms=100,
            max_response_bytes=1024,
        )
        with pytest.raises(asyncio.CancelledError):
            await client.parse("review.pdf", b"document")


@pytest.mark.asyncio
async def test_tika_connection_error_is_classified() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="https://tika.internal",
            timeout_ms=100,
            max_response_bytes=1024,
        )
        with pytest.raises(TikaConnectionError):
            await client.parse("review.docx", b"document")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        (b"", TikaEmptyResponseError),
        (b"12345", TikaResponseTooLargeError),
        (b"\xff\xfe", TikaInvalidResponseError),
    ],
)
async def test_tika_rejects_invalid_response_bodies(
    body: bytes, error_type: type[Exception]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body))
    ) as http_client:
        client = HTTPXTikaClient(
            client=http_client,
            base_url="https://tika.internal",
            timeout_ms=100,
            max_response_bytes=4,
        )
        with pytest.raises(error_type):
            await client.parse("review.pdf", b"document")
