"""严格的 SiliconFlow reranker provider。"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
from pydantic import SecretStr

from docreview.knowledge.evidence import RerankResult
from docreview.providers.base import (
    ProviderError,
    ProviderErrorCategory,
    ProviderHTTPTransport,
    RetryPolicy,
)


class SiliconFlowReranker:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str | SecretStr,
        model: str,
        timeout_ms: int,
        retry_policy: RetryPolicy,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
        max_response_bytes: int = 1024 * 1024,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("invalid reranker provider configuration")
        self._transport = ProviderHTTPTransport(
            client=client,
            base_url=base_url,
            api_key=api_key,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy,
            sleeper=sleeper,
            jitter=jitter,
            max_response_bytes=max_response_bytes,
        )
        self._model = normalized_model
        self._logger = logger or logging.getLogger("docreview.providers")
        self._clock = clock or time.perf_counter

    async def rerank(
        self,
        query: str,
        documents: list[str],
        limit: int,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        top_n = limit if 0 < limit <= len(documents) else len(documents)
        identity = (request_id or "").strip() or "rerank-" + secrets.token_hex(16)
        trace = (trace_id or "").strip() or identity
        started_at = self._clock()
        response = None
        try:
            response = await self._transport.post_json(
                "/rerank",
                {
                    "model": self._model,
                    "query": query.strip(),
                    "documents": documents,
                    "top_n": top_n,
                    "return_documents": False,
                },
                request_id=identity,
                trace_id=trace,
            )
            raw_results = response.payload.get("results")
            if not isinstance(raw_results, list):
                raise ValueError("无效的 重排序器 结果 数量")
            result_items = cast(list[object], raw_results)
            if not result_items or len(result_items) > top_n:
                raise ValueError("无效的 重排序器 结果 数量")
            seen: set[int] = set()
            results: list[RerankResult] = []
            for item in result_items:
                if not isinstance(item, dict):
                    raise ValueError("无效的 重排序器 结果")
                item_object = cast(dict[str, object], item)
                index = item_object.get("index")
                score = item_object.get("relevance_score")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(documents)
                    or index in seen
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                ):
                    raise ValueError("无效的 重排序器 结果")
                seen.add(index)
                results.append(RerankResult(index=index, score=float(score)))
            results.sort(key=lambda item: (-item.score, item.index))
        except ValueError as error:
            failure = ProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                "重排序提供方返回的响应无效",
                retry_count=response.retry_count if response is not None else 0,
            )
            self._log(identity, trace, started_at, failure=failure)
            raise failure from error
        except ProviderError as error:
            self._log(identity, trace, started_at, failure=error)
            raise
        except asyncio.CancelledError:
            self._log(
                identity,
                trace,
                started_at,
                error_category=ProviderErrorCategory.CANCELLED.value,
            )
            raise
        self._log(identity, trace, started_at, retry_count=response.retry_count)
        return results

    def _log(
        self,
        request_id: str,
        trace_id: str,
        started_at: float,
        *,
        failure: ProviderError | None = None,
        retry_count: int = 0,
        error_category: str = "",
    ) -> None:
        fields = {
            "event": "provider.request.completed",
            "provider": "siliconflow",
            "model": self._model,
            "request_id": request_id,
            "trace_id": trace_id,
            "latency_ms": round(max(0.0, self._clock() - started_at) * 1000),
            "retry_count": failure.retry_count if failure is not None else retry_count,
            "error_category": failure.category.value if failure is not None else error_category,
            "status_code": failure.status_code if failure is not None else None,
        }
        (self._logger.warning if fields["error_category"] else self._logger.info)(
            "provider request completed", extra=fields
        )


__all__ = ["SiliconFlowReranker"]
