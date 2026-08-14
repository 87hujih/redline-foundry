from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from docreview.knowledge.legacy_search import LegacySearchService
from docreview.storage.models import ResourceVersion, SearchChunk, SearchSection

WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"
VERSION_ID = "66666666-6666-4666-8666-666666666666"


@dataclass
class FakeSearchRepository:
    version: ResourceVersion | None
    sections: list[SearchSection] = field(default_factory=lambda: list[SearchSection]())
    chunks: list[SearchChunk] = field(default_factory=lambda: list[SearchChunk]())
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def get_current_version(
        self, workspace_id: str, resource_id: str
    ) -> ResourceVersion | None:
        self.calls.append(("version", workspace_id, resource_id))
        return self.version

    async def search_chunks_by_version(
        self, workspace_id: str, version_id: str, vector: str, limit: int
    ) -> list[SearchChunk]:
        self.calls.append(("semantic", workspace_id, version_id, vector, limit))
        return self.chunks

    async def search_chunks_lexical_by_version(
        self, workspace_id: str, version_id: str, query: str, limit: int
    ) -> list[SearchChunk]:
        self.calls.append(("lexical", workspace_id, version_id, query, limit))
        return self.chunks

    async def list_sections_by_version(
        self, workspace_id: str, version_id: str
    ) -> list[SearchSection]:
        self.calls.append(("sections", workspace_id, version_id))
        return self.sections

    async def list_chunks_by_version(self, workspace_id: str, version_id: str) -> list[SearchChunk]:
        self.calls.append(("chunks", workspace_id, version_id))
        return self.chunks


@dataclass
class FakeEmbedder:
    async def embed(self, query: str) -> str | None:
        return f"[{query}]"


@dataclass
class FakeReranker:
    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]:
        return list(range(min(top_n, len(documents))))


def version() -> ResourceVersion:
    return ResourceVersion(
        id=VERSION_ID,
        resource_id=RESOURCE_ID,
        version_number=1,
        content="content",
        source="upload",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


@pytest.mark.anyio
async def test_general_search_uses_current_version_and_scoped_candidates() -> None:
    repository = FakeSearchRepository(
        version(),
        chunks=[
            SearchChunk(
                id="chunk-1",
                resource_id=RESOURCE_ID,
                version_id=VERSION_ID,
                chunk_index=1,
                section_title="Summary",
                content="Policy evidence",
            )
        ],
    )

    citations = await LegacySearchService(
        repository, embedder=FakeEmbedder(), reranker=FakeReranker()
    ).search_by_resource(WORKSPACE_ID, RESOURCE_ID, "policy", 5)

    assert citations[0].citation_id == "cite_1"
    assert [call[1] for call in repository.calls] == [WORKSPACE_ID] * 3
    assert repository.calls[0] == ("version", WORKSPACE_ID, RESOURCE_ID)


@pytest.mark.anyio
async def test_grounded_section_queries_do_not_require_external_providers() -> None:
    repository = FakeSearchRepository(
        version(),
        sections=[
            SearchSection(
                id="section-1",
                resource_id=RESOURCE_ID,
                version_id=VERSION_ID,
                section_key="project-1",
                section_type="project",
                section_order=1,
                title="Project One",
                canonical_entity_name=None,
                aliases=[],
                summary="A project",
                content="Details",
            )
        ],
    )

    citations = await LegacySearchService(repository).search_by_resource(
        WORKSPACE_ID, RESOURCE_ID, "项目有哪些", 5
    )

    assert citations[0].section_id == "section-1"
    assert repository.calls == [
        ("version", WORKSPACE_ID, RESOURCE_ID),
        ("sections", WORKSPACE_ID, VERSION_ID),
    ]
