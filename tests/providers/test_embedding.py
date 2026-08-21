from __future__ import annotations

import httpx
import pytest

from docreview.providers.base import ProviderError, ProviderErrorCategory, RetryPolicy
from docreview.providers.embedding import SiliconFlowEmbeddingProvider


def make_embedder(
    client: httpx.AsyncClient, *, dimensions: int = 3
) -> SiliconFlowEmbeddingProvider:
    return SiliconFlowEmbeddingProvider(
        client=client,
        base_url="https://provider.example/v1",
        api_key="test-provider-key",
        model="test-embedding-model",
        dimensions=dimensions,
        timeout_ms=90000,
        retry_policy=RetryPolicy(max_retries=0, base_backoff_ms=1000),
    )


@pytest.mark.asyncio
async def test_embedding_batch_preserves_input_order_using_response_indexes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [4, 5, 6]},
                    {"index": 0, "embedding": [1, 2, 3]},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vectors = await make_embedder(client).embed_many(
            ["first", "second"], request_id="request-1", trace_id="trace-1"
        )

    assert vectors == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert seen[0].url == "https://provider.example/v1/embeddings"
    assert seen[0].headers["X-Request-ID"] == "request-1"
    assert seen[0].headers["X-Trace-ID"] == "trace-1"
    assert seen[0].read().decode() == (
        '{"model":"test-embedding-model","input":["first","second"],"dimensions":3}'
    )


@pytest.mark.asyncio
async def test_embedding_single_uses_the_same_strict_batch_contract() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vector = await make_embedder(client).embed("one")

    assert vector == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1, 2, 3]}],
        [
            {"index": 0, "embedding": [1, 2]},
            {"index": 1, "embedding": [4, 5, 6]},
        ],
        [
            {"index": 0, "embedding": []},
            {"index": 1, "embedding": [4, 5, 6]},
        ],
        [
            {"index": 0, "embedding": [1, 2, 3]},
            {"index": 0, "embedding": [4, 5, 6]},
        ],
    ],
)
async def test_embedding_rejects_count_dimension_empty_and_duplicate_indexes(
    data: list[dict[str, object]],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_embedder(client).embed_many(
                ["first", "second"], request_id="request-1", trace_id="trace-1"
            )

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_embedding_rejects_non_finite_values(constant: str) -> None:
    body = ('{"data":[{"index":0,"embedding":[1,2,' + constant + "]}]}").encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_embedder(client).embed("one")

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE
