"""严格的 SiliconFlow/OpenAI 兼容 embedding provider。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from pydantic import SecretStr

from docreview.knowledge.chunking import (
    REVIEW_STRUCTURE_PROFILE,
    ChunkTokenizer,
    embedding_text_from_metadata,
)
from docreview.providers.base import (
    ProviderError,
    ProviderErrorCategory,
    ProviderHTTPTransport,
    RetryPolicy,
)


@dataclass(frozen=True, slots=True)
class PendingEmbeddingChunk:
    id: str
    workspace_id: str
    resource_id: str
    version_id: str
    content: str
    content_hash: str
    chunk_profile: str
    embedding_profile: str
    metadata: dict[str, object]


class PendingEmbeddingRepository(Protocol):
    async def list_pending_embeddings(
        self, *, chunk_profile: str, embedding_profile: str, limit: int
    ) -> list[PendingEmbeddingChunk]: ...

    async def write_embedding_if_current(
        self,
        chunk: PendingEmbeddingChunk,
        *,
        vector: list[float],
        embedding_model: str,
        embedding_dimensions: int,
        retrieval_index_version: str,
        tokenizer_profile: str,
    ) -> bool: ...

    async def mark_embedding_failed_if_current(
        self, chunk: PendingEmbeddingChunk, *, reason: str
    ) -> bool: ...


class EmbeddingBatchProvider(Protocol):
    async def embed_many(
        self,
        texts: list[str],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]: ...


class ChunkEmbeddingProjector:
    """Runs provider I/O outside the commit transaction and rechecks immutable facts on write."""

    def __init__(
        self,
        *,
        repository: PendingEmbeddingRepository,
        provider: EmbeddingBatchProvider,
        tokenizer: ChunkTokenizer,
        embedding_profile: str,
        embedding_model: str,
        embedding_dimensions: int,
        retrieval_index_version: str,
        batch_size: int = 32,
    ) -> None:
        if (
            not tokenizer.exact
            or not embedding_profile.strip()
            or not embedding_model.strip()
            or not retrieval_index_version.strip()
            or embedding_dimensions <= 0
            or not 0 < batch_size <= 100
        ):
            raise ValueError("无效的 结构化 切块 嵌入 投影 配置")
        self._repository = repository
        self._provider = provider
        self._tokenizer = tokenizer
        self._embedding_profile = embedding_profile
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._retrieval_index_version = retrieval_index_version
        self._batch_size = batch_size

    async def project_once(
        self, *, request_id: str | None = None, trace_id: str | None = None
    ) -> int:
        pending = await self._repository.list_pending_embeddings(
            chunk_profile=REVIEW_STRUCTURE_PROFILE.profile_id,
            embedding_profile=self._embedding_profile,
            limit=self._batch_size,
        )
        accepted: list[tuple[PendingEmbeddingChunk, str]] = []
        for chunk in pending:
            try:
                text = self._validate_chunk(chunk)
            except ValueError:
                await self._repository.mark_embedding_failed_if_current(
                    chunk, reason="chunk_projection_metadata_mismatch"
                )
                continue
            accepted.append((chunk, text))
        if not accepted:
            return 0
        vectors = await self._provider.embed_many(
            [text for _chunk, text in accepted], request_id=request_id, trace_id=trace_id
        )
        if len(vectors) != len(accepted) or any(
            len(vector) != self._embedding_dimensions for vector in vectors
        ):
            raise RuntimeError("嵌入 提供方 维度 不匹配")
        tokenizer_profile = "/".join(
            (self._tokenizer.name, self._tokenizer.version, self._tokenizer.vocabulary_hash)
        )
        written = 0
        for (chunk, _text), vector in zip(accepted, vectors, strict=True):
            if await self._repository.write_embedding_if_current(
                chunk,
                vector=vector,
                embedding_model=self._embedding_model,
                embedding_dimensions=self._embedding_dimensions,
                retrieval_index_version=self._retrieval_index_version,
                tokenizer_profile=tokenizer_profile,
            ):
                written += 1
        return written

    def _validate_chunk(self, chunk: PendingEmbeddingChunk) -> str:
        metadata = chunk.metadata
        if (
            chunk.chunk_profile != REVIEW_STRUCTURE_PROFILE.profile_id
            or chunk.embedding_profile != self._embedding_profile
            or metadata.get("profile_id") != REVIEW_STRUCTURE_PROFILE.profile_id
        ):
            raise ValueError("切块 配置档 不匹配")
        tokenizer_profile = "/".join(
            (self._tokenizer.name, self._tokenizer.version, self._tokenizer.vocabulary_hash)
        )
        if metadata.get("tokenizer_profile") != tokenizer_profile:
            raise ValueError("切块 分词器 不匹配")
        text = embedding_text_from_metadata(chunk.content, metadata)
        digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        if metadata.get("embedding_text_hash") != digest:
            raise ValueError("切块 嵌入 文本 哈希 不匹配")
        tokens = self._tokenizer.count(text)
        if tokens < 0 or tokens > REVIEW_STRUCTURE_PROFILE.child_hard_max_tokens:
            raise ValueError("切块 嵌入 令牌 限制 不匹配")
        return text


class SiliconFlowEmbeddingProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str | SecretStr,
        model: str,
        dimensions: int,
        timeout_ms: int,
        retry_policy: RetryPolicy,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model or not 0 < dimensions <= 65536:
            raise ValueError("无效的 嵌入 提供方 配置")
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
        self._dimensions = dimensions
        self._logger = logger or logging.getLogger("docreview.providers")
        self._clock = clock or time.perf_counter

    async def embed(self, text: str) -> list[float]:
        vectors = await self.embed_many([text])
        return vectors[0]

    async def embed_many(
        self,
        texts: list[str],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        identity = (request_id or "").strip() or "embedding-" + secrets.token_hex(16)
        trace = (trace_id or "").strip() or identity
        started_at = self._clock()
        response = None
        try:
            response = await self._transport.post_json(
                "/embeddings",
                {"model": self._model, "input": texts, "dimensions": self._dimensions},
                request_id=identity,
                trace_id=trace,
            )
            data = response.payload.get("data")
            if not isinstance(data, list):
                raise ValueError("嵌入 数量 不匹配")
            data_items = cast(list[object], data)
            if len(data_items) != len(texts):
                raise ValueError("嵌入 数量 不匹配")
            ordered: list[list[float] | None] = [None] * len(texts)
            for item in data_items:
                if not isinstance(item, dict):
                    raise ValueError("无效的 嵌入 项")
                item_object = cast(dict[str, object], item)
                index = item_object.get("index")
                embedding = item_object.get("embedding")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(texts)
                    or ordered[index] is not None
                    or not isinstance(embedding, list)
                ):
                    raise ValueError("无效的 嵌入 项")
                embedding_values = cast(list[object], embedding)
                if not embedding_values or len(embedding_values) != self._dimensions:
                    raise ValueError("无效的 嵌入 项")
                vector: list[float] = []
                for value in embedding_values:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError("无效的 嵌入 值")
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("无效的 嵌入 值")
                    vector.append(number)
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise ValueError("嵌入 索引 为 不完整")
            vectors = [vector for vector in ordered if vector is not None]
        except ValueError as error:
            failure = ProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                "嵌入提供方返回的响应无效",
                retry_count=response.retry_count if response is not None else 0,
            )
            self._log(identity, trace, started_at, failure=failure, input_count=len(texts))
            raise failure from error
        except ProviderError as error:
            self._log(identity, trace, started_at, failure=error, input_count=len(texts))
            raise
        except asyncio.CancelledError:
            self._log(
                identity,
                trace,
                started_at,
                error_category=ProviderErrorCategory.CANCELLED.value,
                input_count=len(texts),
            )
            raise
        self._log(
            identity,
            trace,
            started_at,
            retry_count=response.retry_count,
            input_count=len(texts),
        )
        return vectors

    def _log(
        self,
        request_id: str,
        trace_id: str,
        started_at: float,
        *,
        failure: ProviderError | None = None,
        retry_count: int = 0,
        error_category: str = "",
        input_count: int,
    ) -> None:
        fields = {
            "event": "provider.request.completed",
            "provider": "siliconflow",
            "model": self._model,
            "request_id": request_id,
            "trace_id": trace_id,
            "latency_ms": round(max(0.0, self._clock() - started_at) * 1000),
            "input_count": input_count,
            "retry_count": failure.retry_count if failure is not None else retry_count,
            "error_category": failure.category.value if failure is not None else error_category,
            "status_code": failure.status_code if failure is not None else None,
        }
        (self._logger.warning if fields["error_category"] else self._logger.info)(
            "provider request completed", extra=fields
        )


__all__ = [
    "ChunkEmbeddingProjector",
    "EmbeddingBatchProvider",
    "PendingEmbeddingChunk",
    "PendingEmbeddingRepository",
    "SiliconFlowEmbeddingProvider",
]
