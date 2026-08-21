from __future__ import annotations

from dataclasses import replace

import pytest

from docreview.document.ingestion import ingest
from docreview.document.parser import DocumentParser
from docreview.knowledge.chunking import DeterministicTokenizer, build_chunks
from docreview.providers.embedding import (
    ChunkEmbeddingProjector,
    PendingEmbeddingChunk,
)


class Repository:
    def __init__(self, chunk: PendingEmbeddingChunk) -> None:
        self.chunk = chunk
        self.list_scope: tuple[str, str, int] | None = None
        self.writes: list[tuple[PendingEmbeddingChunk, list[float], str]] = []
        self.failed: list[str] = []

    async def list_pending_embeddings(
        self, *, chunk_profile: str, embedding_profile: str, limit: int
    ) -> list[PendingEmbeddingChunk]:
        self.list_scope = (chunk_profile, embedding_profile, limit)
        return [self.chunk]

    async def write_embedding_if_current(
        self,
        chunk: PendingEmbeddingChunk,
        *,
        vector: list[float],
        embedding_model: str,
        embedding_dimensions: int,
        retrieval_index_version: str,
        tokenizer_profile: str,
    ) -> bool:
        assert embedding_model == "embedding-model"
        assert embedding_dimensions == 2
        assert retrieval_index_version == "hnsw-cosine-v1"
        self.writes.append((chunk, vector, tokenizer_profile))
        return True

    async def mark_embedding_failed_if_current(
        self, chunk: PendingEmbeddingChunk, *, reason: str
    ) -> bool:
        assert chunk.id == self.chunk.id
        self.failed.append(reason)
        return True


class Provider:
    async def embed_many(
        self,
        texts: list[str],
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[list[float]]:
        assert texts and request_id == "request-1" and trace_id == "trace-1"
        return [[0.1, 0.2] for _ in texts]


async def pending_chunk() -> PendingEmbeddingChunk:
    document = (
        await ingest(
            DocumentParser(mode="structured"),
            document_id="resource-1",
            version_id="version-1",
            file_name="review.md",
            content=b"# Review\n\nContent",
        )
    ).document
    chunk = build_chunks(document, tokenizer=DeterministicTokenizer.for_testing())[0]
    return PendingEmbeddingChunk(
        id="chunk-1",
        workspace_id="workspace-1",
        resource_id="resource-1",
        version_id="version-1",
        content=chunk.content,
        content_hash=chunk.content_hash,
        chunk_profile=chunk.metadata["profile_id"],
        embedding_profile="embedding-v1",
        metadata=chunk.metadata,
    )


@pytest.mark.asyncio
async def test_embedding_projection_rechecks_profile_tokenizer_and_fragment_before_write() -> None:
    chunk = await pending_chunk()
    repository = Repository(chunk)
    tokenizer = DeterministicTokenizer.for_testing()
    projector = ChunkEmbeddingProjector(
        repository=repository,
        provider=Provider(),
        tokenizer=tokenizer,
        embedding_profile="embedding-v1",
        embedding_model="embedding-model",
        embedding_dimensions=2,
        retrieval_index_version="hnsw-cosine-v1",
    )

    assert await projector.project_once(request_id="request-1", trace_id="trace-1") == 1
    assert repository.list_scope == (
        "docreview-review-structure-2026-08-17",
        "embedding-v1",
        32,
    )
    assert repository.writes[0][0].id == "chunk-1"
    assert not repository.failed


@pytest.mark.asyncio
async def test_embedding_projection_marks_tampered_metadata_failed_without_provider_io() -> None:
    chunk = await pending_chunk()
    metadata = dict(chunk.metadata)
    metadata["embedding_text_hash"] = "sha256:" + "0" * 64
    repository = Repository(replace(chunk, metadata=metadata))
    projector = ChunkEmbeddingProjector(
        repository=repository,
        provider=Provider(),
        tokenizer=DeterministicTokenizer.for_testing(),
        embedding_profile="embedding-v1",
        embedding_model="embedding-model",
        embedding_dimensions=2,
        retrieval_index_version="hnsw-cosine-v1",
    )

    assert await projector.project_once() == 0
    assert repository.failed == ["chunk_projection_metadata_mismatch"]
