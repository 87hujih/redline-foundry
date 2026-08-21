"""生产 AI provider 构造与共享 HTTP 生命周期管理。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from docreview.config.settings import Settings
from docreview.document.parser import DocumentParser
from docreview.document.tika import HTTPXTikaClient
from docreview.knowledge.chunking import DeterministicTokenizer
from docreview.providers.base import RetryPolicy
from docreview.providers.embedding import SiliconFlowEmbeddingProvider
from docreview.providers.llm import OpenAIChatGenerator, ProductionModelGateway
from docreview.providers.reranker import SiliconFlowReranker
from docreview.providers.web_search import HTTPWebSearchTransport, WebSearchProvider
from docreview.storage.filestore import LocalFileStore

HTTPClientFactory = Callable[[httpx.Timeout, httpx.Limits], httpx.AsyncClient]


@dataclass(slots=True)
class ProductionProviderDependencies:
    model_gateway: ProductionModelGateway
    embedder: SiliconFlowEmbeddingProvider
    reranker: SiliconFlowReranker
    document_parser: DocumentParser
    file_store: LocalFileStore
    http_client: httpx.AsyncClient = field(repr=False)
    web_search: WebSearchProvider | None = None
    chunk_tokenizer: DeterministicTokenizer | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            await self.file_store.aclose()
        finally:
            await self.http_client.aclose()
        self._closed = True


def _default_client_factory(timeout: httpx.Timeout, limits: httpx.Limits) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False,
    )


async def create_production_provider_dependencies(
    settings: Settings,
    *,
    client_factory: HTTPClientFactory = _default_client_factory,
) -> ProductionProviderDependencies:
    api_key = settings.siliconflow_api_key
    base_url = settings.siliconflow_base_url
    llm_model = settings.llm_model
    embedding_model = settings.embedding_model
    embedding_dim = settings.embedding_dim
    reranker_model = settings.reranker_model
    timeout_ms = settings.llm_timeout_ms
    retry_max = settings.llm_retry_max
    retry_backoff_ms = settings.llm_retry_backoff_ms
    if (
        api_key is None
        or base_url is None
        or llm_model is None
        or embedding_model is None
        or embedding_dim is None
        or reranker_model is None
        or timeout_ms is None
        or retry_max is None
        or retry_backoff_ms is None
    ):
        raise ValueError("生产环境 AI 提供方 配置 不完整")

    timeout_seconds = timeout_ms / 1000
    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10,
        keepalive_expiry=30,
    )
    client = client_factory(timeout, limits)
    file_store: LocalFileStore | None = None
    retry_policy = RetryPolicy(
        max_retries=retry_max,
        base_backoff_ms=retry_backoff_ms,
        max_backoff_ms=max(30000, retry_backoff_ms),
    )
    try:
        generator = OpenAIChatGenerator(
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=llm_model,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy,
        )
        model_gateway = ProductionModelGateway(generator)
        embedder = SiliconFlowEmbeddingProvider(
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=embedding_model,
            dimensions=embedding_dim,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy,
        )
        reranker = SiliconFlowReranker(
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=reranker_model,
            timeout_ms=timeout_ms,
            retry_policy=retry_policy,
        )
        tika = None
        if settings.document_parser in {"tika", "structured"}:
            if settings.tika_url is None or settings.tika_timeout_ms is None:
                raise ValueError("生产环境 Tika 配置 不完整")
            tika = HTTPXTikaClient(
                client=client,
                base_url=settings.tika_url,
                timeout_ms=settings.tika_timeout_ms,
                max_response_bytes=settings.upload_max_bytes,
            )
        document_parser = DocumentParser(
            mode=settings.document_parser,
            tika=tika,
            max_bytes=settings.upload_max_bytes,
        )
        tokenizer_name = (settings.embedding_tokenizer_profile or "").strip()
        if settings.app_env == "production" and not tokenizer_name:
            raise ValueError("生产环境 嵌入 分词器 配置档 不完整")
        chunk_tokenizer = DeterministicTokenizer(
            name=tokenizer_name or "docreview-development-tokenizer",
            version="1",
        )
        file_store = LocalFileStore(settings.upload_storage_dir)
        web_search = None
        if settings.web_search_url is not None:
            transport = HTTPWebSearchTransport(
                client=client,
                base_url=settings.web_search_url,
                api_key=settings.web_search_api_key,
                timeout_ms=settings.web_search_timeout_ms,
            )
            web_search = WebSearchProvider(transport)
    except BaseException as error:
        if file_store is not None:
            try:
                await file_store.aclose()
            except Exception as cleanup_error:
                error.add_note(f"文件存储清理也失败了: {cleanup_error}")
        await client.aclose()
        raise
    return ProductionProviderDependencies(
        model_gateway=model_gateway,
        embedder=embedder,
        reranker=reranker,
        document_parser=document_parser,
        file_store=file_store,
        http_client=client,
        web_search=web_search,
        chunk_tokenizer=chunk_tokenizer,
    )


__all__ = [
    "HTTPClientFactory",
    "ProductionProviderDependencies",
    "create_production_provider_dependencies",
]
