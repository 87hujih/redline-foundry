from __future__ import annotations

import json

import httpx
import pytest

from docreview.providers.base import ProviderError, ProviderErrorCategory, RetryPolicy
from docreview.providers.reranker import SiliconFlowReranker


def make_reranker(client: httpx.AsyncClient) -> SiliconFlowReranker:
    return SiliconFlowReranker(
        client=client,
        base_url="https://provider.example/v1",
        api_key="test-provider-key",
        model="test-reranker-model",
        timeout_ms=90000,
        retry_policy=RetryPolicy(max_retries=0, base_backoff_ms=1000),
    )


@pytest.mark.asyncio
async def test_reranker_sends_siliconflow_contract_and_sorts_deterministically() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await make_reranker(client).rerank(" query ", ["first", "second", "third"], 3)

    assert [(result.index, result.score) for result in results] == [
        (1, 0.9),
        (0, 0.8),
        (2, 0.8),
    ]
    assert seen[0].url == "https://provider.example/v1/rerank"
    assert json.loads(seen[0].content) == {
        "model": "test-reranker-model",
        "query": "query",
        "documents": ["first", "second", "third"],
        "top_n": 3,
        "return_documents": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.7}],
        [{"index": 2, "relevance_score": 0.8}],
        [{"index": -1, "relevance_score": 0.8}],
        [{"index": 0}],
        [],
    ],
)
async def test_reranker_rejects_duplicate_out_of_range_or_missing_results(
    results: list[dict[str, object]],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_reranker(client).rerank("query", ["first", "second"], 2)

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_reranker_rejects_non_finite_scores(constant: str) -> None:
    body = ('{"results":[{"index":0,"relevance_score":' + constant + "}]}").encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_reranker(client).rerank("query", ["first"], 1)

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_reranker_rejects_invalid_json_and_skips_empty_documents() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = make_reranker(client)
        assert await reranker.rerank("query", [], 3) == []
        with pytest.raises(ProviderError) as captured:
            await reranker.rerank("query", ["first"], 1)

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE
    assert calls == 1
