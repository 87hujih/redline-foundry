from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
import pytest

from docreview.agent_graph.models import NodeName, RuntimeRequest, RuntimeTarget
from docreview.providers.base import (
    ProviderCancelledError,
    ProviderError,
    ProviderErrorCategory,
    RetryPolicy,
)
from docreview.providers.llm import ChatRequest, OpenAIChatGenerator, ProductionModelGateway

VALID_RESPONSE = {
    "choices": [{"message": {"content": '{"action":"finish"}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


def make_generator(
    client: httpx.AsyncClient,
    *,
    max_retries: int = 2,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_response_bytes: int = 1024 * 1024,
    logger: logging.Logger | None = None,
    clock: Callable[[], float] | None = None,
) -> OpenAIChatGenerator:
    return OpenAIChatGenerator(
        client=client,
        base_url="https://provider.example/v1",
        api_key="test-provider-key",
        model="test-chat-model",
        timeout_ms=90000,
        retry_policy=RetryPolicy(max_retries=max_retries, base_backoff_ms=1000),
        sleeper=sleeper,
        jitter=lambda _maximum: 0.0,
        max_response_bytes=max_response_bytes,
        logger=logger,
        clock=clock,
    )


def chat_request() -> ChatRequest:
    return ChatRequest(
        system="system contract",
        user='{"node_input":{}}',
        request_id="request-1",
        trace_id="trace-1",
        temperature=0,
    )


@pytest.mark.asyncio
async def test_chat_generator_preserves_request_contract_and_returns_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{ "action": "finish" }'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = OpenAIChatGenerator(
            client=client,
            base_url="https://provider.example/v1",
            api_key="test-provider-key",
            model="test-chat-model",
            timeout_ms=90000,
            retry_policy=RetryPolicy(max_retries=2, base_backoff_ms=1000),
        )
        response = await generator.generate(
            ChatRequest(
                system="system contract",
                user='{"node_input":{}}',
                request_id="request-1",
                trace_id="trace-1",
                temperature=0,
            )
        )

    assert response.content == '{ "action": "finish" }'
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 7
    assert response.usage.total_tokens == 18
    assert response.retry_count == 0
    assert len(seen) == 1
    assert seen[0].url == "https://provider.example/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer test-provider-key"
    assert seen[0].headers["X-Request-ID"] == "request-1"
    assert seen[0].headers["X-Trace-ID"] == "trace-1"
    assert json.loads(seen[0].content) == {
        "model": "test-chat-model",
        "messages": [
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": '{"node_input":{}}'},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": [], "usage": VALID_RESPONSE["usage"]},
        {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
            "usage": VALID_RESPONSE["usage"],
        },
        {
            "choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}],
            "usage": VALID_RESPONSE["usage"],
        },
        {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
            "usage": VALID_RESPONSE["usage"],
        },
    ],
)
async def test_chat_generator_rejects_empty_or_malformed_responses(
    response: dict[str, object],
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_generator(client).generate(chat_request())

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE
    assert calls == 1


@pytest.mark.asyncio
async def test_chat_generator_retries_429_and_supported_5xx_with_stable_identity() -> None:
    statuses = [429, 503, 200]
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = statuses.pop(0)
        return httpx.Response(status, json=VALID_RESPONSE if status == 200 else {"error": "x"})

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await make_generator(client, sleeper=sleeper).generate(chat_request())

    assert response.retry_count == 2
    assert sleeps == [1.0, 2.0]
    assert [request.headers["X-Request-ID"] for request in requests] == ["request-1"] * 3
    assert [request.headers["X-Trace-ID"] for request in requests] == ["trace-1"] * 3
    assert [request.content for request in requests] == [requests[0].content] * 3


@pytest.mark.asyncio
async def test_chat_generator_retries_timeout_without_real_sleep() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=VALID_RESPONSE)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await make_generator(client, sleeper=sleeper).generate(chat_request())

    assert response.retry_count == 1
    assert calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (429, ProviderErrorCategory.RATE_LIMITED),
        (503, ProviderErrorCategory.RETRYABLE_UPSTREAM),
    ],
)
async def test_chat_generator_preserves_retryable_category_after_exhaustion(
    status: int, category: ProviderErrorCategory
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "not exposed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_generator(client, max_retries=0).generate(chat_request())

    assert captured.value.category is category
    assert captured.value.retry_count == 0


@pytest.mark.asyncio
async def test_chat_generator_preserves_timeout_category_after_exhaustion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_generator(client, max_retries=0).generate(chat_request())

    assert captured.value.category is ProviderErrorCategory.TIMEOUT
    assert captured.value.retry_count == 0


@pytest.mark.asyncio
async def test_chat_generator_respects_caller_cancellation_during_backoff() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def cancelled_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderCancelledError):
            await make_generator(client, sleeper=cancelled_sleep).generate(chat_request())

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, ProviderErrorCategory.AUTHENTICATION),
        (400, ProviderErrorCategory.PERMANENT_UPSTREAM),
    ],
)
async def test_chat_generator_does_not_retry_permanent_statuses(
    status: int, category: ProviderErrorCategory
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"authorization": "must-not-be-reported"})

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_generator(client, sleeper=sleeper).generate(chat_request())

    assert captured.value.category is category
    assert captured.value.status_code == status
    assert calls == 1
    assert sleeps == []
    assert "authorization" not in str(captured.value).lower()
    assert "test-provider-key" not in repr(captured.value)


@pytest.mark.asyncio
async def test_chat_generator_classifies_cancellation_without_retrying() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderCancelledError) as captured:
            await make_generator(client, sleeper=sleeper).generate(chat_request())

    assert captured.value.category is ProviderErrorCategory.CANCELLED
    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_chat_generator_rejects_oversized_response_without_retrying() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Length": "33"},
            content=b"x" * 33,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as captured:
            await make_generator(client, max_response_bytes=32).generate(chat_request())

    assert captured.value.category is ProviderErrorCategory.INVALID_RESPONSE
    assert calls == 1


@pytest.mark.asyncio
async def test_chat_generator_logs_only_safe_metadata(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.docreview.provider")
    times = iter([10.0, 10.025])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=VALID_RESPONSE)

    with caplog.at_level(logging.INFO, logger=logger.name):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await make_generator(client, logger=logger, clock=lambda: next(times)).generate(
                ChatRequest(
                    system="secret system prompt",
                    user="sensitive document content",
                    request_id="request-1",
                    trace_id="trace-1",
                )
            )

    record = caplog.records[-1]
    assert record.__dict__["provider"] == "openai-compatible"
    assert record.__dict__["model"] == "test-chat-model"
    assert record.__dict__["latency_ms"] == 25
    assert record.__dict__["input_tokens"] == 11
    assert record.__dict__["output_tokens"] == 7
    assert record.__dict__["total_tokens"] == 18
    assert record.__dict__["retry_count"] == 0
    assert record.__dict__["error_category"] == ""
    assert "test-provider-key" not in caplog.text
    assert "secret system prompt" not in caplog.text
    assert "sensitive document content" not in caplog.text


@pytest.mark.asyncio
async def test_production_model_gateway_uses_typed_contract_and_returns_raw_content() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json=VALID_RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductionModelGateway(make_generator(client))
        content = await gateway.invoke(
            RuntimeRequest(
                request_id="request-1",
                run_id="run-1",
                node=NodeName.DECIDE_NEXT_ACTION,
                target=RuntimeTarget.MODEL_GATEWAY,
                operation="decide_next_action",
                payload={"context_manifest_id": "manifest-1"},
                idempotency_hint="step-1",
            )
        )

    assert content == '{"action":"finish"}'
    assert seen_body["temperature"] == 0
    messages = seen_body["messages"]
    assert isinstance(messages, list)
    message_items = cast(list[object], messages)
    system_message = cast(dict[str, object], message_items[0])
    user_message = cast(dict[str, object], message_items[1])
    system = cast(str, system_message["content"])
    assert "Output contract decision.v1" in system
    assert "retrieve_evidence requires tool_name retrieval.search" in system
    assert "read_nodes requires tool_name document.read_nodes" in system
    assert "All other actions require an empty tool_name and empty tool_input" in system
    assert "lexical evidence is available" in system
    assert "finding_refs is non-empty, choose finish" in system
    assert json.loads(cast(str, user_message["content"])) == {
        "node": "DecideNextAction",
        "node_input": {"context_manifest_id": "manifest-1"},
    }


@pytest.mark.asyncio
async def test_render_outcome_prompt_requires_a_complete_user_facing_answer() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json=VALID_RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductionModelGateway(make_generator(client))
        await gateway.invoke(
            RuntimeRequest(
                request_id="request-1",
                run_id="run-1",
                node=NodeName.RENDER_OUTCOME,
                target=RuntimeTarget.MODEL_GATEWAY,
                operation="render_outcome",
                payload={"context_manifest_id": "manifest-1"},
                idempotency_hint="step-1",
            )
        )

    messages = cast(list[object], seen_body["messages"])
    system = cast(str, cast(dict[str, object], messages[0])["content"])
    assert "final user-facing answer" in system
    assert "Do not describe a next step" in system
    assert "Never invent a citation" in system
