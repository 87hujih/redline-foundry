"""有界 Web Search provider 的 ToolRuntime 适配器。"""

from __future__ import annotations

import hashlib
import json
from typing import cast

from docreview.providers.web_search import WebSearchError, WebSearchProvider
from docreview.tool_runtime.models import (
    BackendRequest,
    Provenance,
    ToolBackendFailure,
    ToolErrorCategory,
    ToolResult,
)


class WebSearchBackend:
    def __init__(self, provider: WebSearchProvider) -> None:
        self._provider = provider

    async def execute(self, request: BackendRequest) -> ToolResult:
        query = request.tool_input.get("query")
        limit = request.tool_input.get("limit", 5)
        if (
            not isinstance(query, str)
            or not query.strip()
            or isinstance(limit, bool)
            or not isinstance(limit, int)
        ):
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "网页搜索输入无效")
        if not 1 <= limit <= 20:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "网页搜索限制无效")
        try:
            response = await self._provider.search(
                query,
                limit=limit,
                request_id=request.context.request_id,
                trace_id=request.context.trace_id,
            )
        except WebSearchError as error:
            raise ToolBackendFailure(ToolErrorCategory.RETRYABLE_UPSTREAM, str(error)) from error
        except (TimeoutError, ValueError) as error:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "网页搜索失败") from error
        output = {
            "query": response.query,
            "provider": response.provider,
            "items": [
                {
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "source": item.source,
                }
                for item in response.items
            ],
        }
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        return ToolResult(
            output=cast(dict[str, object], output),  # type: ignore[arg-type]
            provenance=(
                Provenance(
                    source_type="web_search",
                    source_id=response.provider,
                    trust_level="untrusted",
                    content_hash=digest,
                    provider=response.provider,
                ),
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        # 搜索虽是只读，但中断请求不得在没有调用方显式重试时由审计层重放。
        del request
        return None


__all__ = ["WebSearchBackend"]
