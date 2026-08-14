"""Workspace-scoped compatibility search used by resource search routes."""

from __future__ import annotations

import re
from typing import Protocol, cast

from docreview.storage.models import (
    Citation,
    CitationWindow,
    ResourceVersion,
    SearchChunk,
    SearchSection,
)


class SearchRepository(Protocol):
    async def get_current_version(
        self, workspace_id: str, resource_id: str
    ) -> ResourceVersion | None: ...

    async def search_chunks_by_version(
        self, workspace_id: str, version_id: str, vector: str, limit: int
    ) -> list[SearchChunk]: ...

    async def search_chunks_lexical_by_version(
        self, workspace_id: str, version_id: str, query: str, limit: int
    ) -> list[SearchChunk]: ...

    async def list_sections_by_version(
        self, workspace_id: str, version_id: str
    ) -> list[SearchSection]: ...

    async def list_chunks_by_version(
        self, workspace_id: str, version_id: str
    ) -> list[SearchChunk]: ...


class QueryEmbedder(Protocol):
    async def embed(self, query: str) -> str | None: ...


class QueryReranker(Protocol):
    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[int]: ...


class LegacySearchService:
    def __init__(
        self,
        repository: SearchRepository,
        *,
        embedder: QueryEmbedder | None = None,
        reranker: QueryReranker | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._reranker = reranker

    async def search_by_resource(
        self, workspace_id: str, resource_id: str, query: str, limit: int
    ) -> list[Citation]:
        if limit <= 0:
            return []
        version = await self._repository.get_current_version(workspace_id, resource_id)
        if version is None:
            return []
        analysis = _analyze(query)
        grounded = await self._grounded(workspace_id, version.id, analysis, limit)
        if grounded is not None:
            return grounded
        if self._embedder is None or self._reranker is None:
            raise RuntimeError("retrieval providers are not configured")
        vector = await self._embedder.embed(query.strip())
        if vector is None:
            return []
        semantic = await self._repository.search_chunks_by_version(
            workspace_id, version.id, vector, 8
        )
        lexical = await self._repository.search_chunks_lexical_by_version(
            workspace_id, version.id, query.strip(), 8
        )
        candidates = _unique_chunks(semantic, lexical)
        if not candidates:
            return []
        indexes = await self._reranker.rerank(
            query.strip(), [_document(chunk) for chunk in candidates], limit
        )
        ranked = [candidates[index] for index in indexes if 0 <= index < len(candidates)]
        return _chunk_citations(_unique_chunks(ranked), limit)

    async def _grounded(
        self, workspace_id: str, version_id: str, analysis: QueryAnalysis, limit: int
    ) -> list[Citation] | None:
        if analysis.intent == "general_search":
            return None
        sections = await self._repository.list_sections_by_version(workspace_id, version_id)
        if not sections:
            return None
        targets = [
            s
            for s in sections
            if not analysis.section_type or s.section_type == analysis.section_type
        ]
        if not targets:
            targets = sections
        if analysis.intent == "list_sections":
            return _section_citations(targets, limit)
        if analysis.intent == "aggregate_attribute":
            chunks = await self._repository.list_chunks_by_version(workspace_id, version_id)
            section_order = {section.id: section.section_order for section in targets}
            selected = [
                chunk
                for chunk in chunks
                if chunk.chunk_role == "tech_stack"
                and any(chunk.section_id == section.id for section in targets)
            ]
            selected.sort(key=lambda chunk: section_order.get(chunk.section_id or "", 0))
            return (
                _chunk_citations(selected, limit)
                if selected
                else _section_aggregate(targets, limit)
            )
        target: SearchSection | None = None
        if analysis.intent == "detail_by_ordinal":
            target = next((s for s in targets if s.section_order == analysis.ordinal), None)
        elif analysis.intent == "detail_by_entity":
            target = _resolve_entity(targets, analysis.entity_name)
        if target is None:
            return None
        chunks = await self._repository.list_chunks_by_version(workspace_id, version_id)
        selected = [chunk for chunk in chunks if chunk.section_id == target.id]
        if not selected:
            return _section_citations([target], limit)
        return _window_citations(selected, limit)


class QueryAnalysis:
    def __init__(
        self, intent: str, section_type: str = "", entity_name: str = "", ordinal: int = 0
    ) -> None:
        self.intent = intent
        self.section_type = section_type
        self.entity_name = entity_name
        self.ordinal = ordinal


def _analyze(query: str) -> QueryAnalysis:
    focus = query.strip()
    for line in focus.splitlines():
        if line.strip().startswith("当前问题："):  # noqa: RUF001
            focus = line.strip().removeprefix("当前问题：").strip()  # noqa: RUF001
            break
    project = any(marker in focus for marker in ("项目", "经历", "技术栈"))
    if "技术栈" in focus or ("用了哪些技术" in focus and "项目" not in focus):
        return QueryAnalysis("aggregate_attribute", "project")
    if project and any(
        marker in focus for marker in ("有哪些", "哪些", "列出", "都有什么", "分别是什么")
    ):
        return QueryAnalysis("list_sections", "project")
    ordinal = next(
        (
            number
            for text, number in (
                ("第一个", 1),
                ("第1个", 1),
                ("第二个", 2),
                ("第2个", 2),
                ("第三个", 3),
                ("第3个", 3),
            )
            if text in focus
        ),
        0,
    )
    if ordinal and project:
        return QueryAnalysis("detail_by_ordinal", "project", ordinal=ordinal)
    markers = ("做了什么", "负责什么", "讲讲", "介绍", "看下", "看看", "怎么做", "给出修改示例")
    for marker in markers:
        if marker in focus:
            entity = focus.split(marker, 1)[0].strip("，,。！？!?：:")  # noqa: RUF001
            for prefix in ("针对", "关于", "请", "帮我", "帮忙", "看下", "看看", "说说", "讲讲"):
                entity = entity.removeprefix(prefix).strip()
            entity = entity.removesuffix("项目").removesuffix("经历").strip()
            if entity:
                return QueryAnalysis("detail_by_entity", "project", entity_name=entity)
    return QueryAnalysis("general_search")


def _document(chunk: SearchChunk) -> str:
    title = chunk.section_title.strip()
    return f"{title}\n{chunk.content.strip()}" if title else chunk.content.strip()


def _unique_chunks(*groups: list[SearchChunk]) -> list[SearchChunk]:
    seen: set[str] = set()
    output: list[SearchChunk] = []
    for group in groups:
        for chunk in group:
            if chunk.id not in seen:
                seen.add(chunk.id)
                output.append(chunk)
    return output


def _window(chunk: SearchChunk) -> CitationWindow | None:
    result = CitationWindow()
    if chunk.window_group_id:
        result["group_id"] = chunk.window_group_id.strip()
    if chunk.order_in_section and chunk.order_in_section > 0:
        result["start_order"] = chunk.order_in_section
        result["end_order"] = chunk.order_in_section
    return result or None


def _citation(
    chunk: SearchChunk, index: int, snippet: str | None = None, *, trim_snippet: bool = False
) -> Citation:
    content = snippet if snippet is not None else chunk.content
    if trim_snippet:
        content = content.strip()
    return Citation(
        citation_id=f"cite_{index}",
        resource_id=chunk.resource_id,
        section_id=chunk.section_id.strip() if chunk.section_id else None,
        section_type=chunk.section_type.strip() if chunk.section_type else None,
        section_title=chunk.section_title,
        snippet=_truncate(content),
        window=_window(chunk),
    )


def _chunk_citations(chunks: list[SearchChunk], limit: int) -> list[Citation]:
    return [_citation(chunk, index) for index, chunk in enumerate(chunks[:limit], 1)]


def _window_citations(chunks: list[SearchChunk], limit: int) -> list[Citation]:
    groups: dict[str, list[SearchChunk]] = {}
    order: list[str] = []
    for chunk in chunks:
        key = chunk.window_group_id or chunk.id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(chunk)
    for group in groups.values():
        group.sort(key=lambda item: (item.order_in_section or 0, item.chunk_index))
    output: list[Citation] = []
    for index, key in enumerate(order[:limit], 1):
        group = groups[key]
        text = "\n".join(
            dict.fromkeys(chunk.content.strip() for chunk in group if chunk.content.strip())
        )
        first = group[0]
        citation = _citation(first, index, text)
        orders = [
            cast(int, item.order_in_section) for item in group if (item.order_in_section or 0) > 0
        ]
        if orders:
            window = citation.window or CitationWindow()
            window["start_order"] = min(orders)
            window["end_order"] = max(orders)
            citation = Citation(
                citation_id=citation.citation_id,
                resource_id=citation.resource_id,
                section_title=citation.section_title,
                snippet=citation.snippet,
                section_id=citation.section_id,
                section_type=citation.section_type,
                window=window,
            )
        output.append(citation)
    return output


def _section_citations(sections: list[SearchSection], limit: int) -> list[Citation]:
    output: list[Citation] = []
    for index, section in enumerate(sections[:limit], 1):
        snippet = section.summary.strip() or section.content.strip() or section.title.strip()
        chunk = SearchChunk(
            id=section.id,
            resource_id=section.resource_id,
            version_id=section.version_id,
            chunk_index=section.section_order,
            section_title=section.title,
            content=snippet,
            section_id=section.id,
            section_type=section.section_type,
            window_group_id=section.section_key,
        )
        output.append(_citation(chunk, index, trim_snippet=True))
    return output


def _section_aggregate(sections: list[SearchSection], limit: int) -> list[Citation]:
    output: list[Citation] = []
    for section in sections:
        values: object = (section.metadata or {}).get("tech_stack")
        if not isinstance(values, list):
            continue
        items = [item for item in cast(list[object], values) if isinstance(item, str)]
        snippet = " ".join(item.strip() for item in items if item.strip())
        if snippet:
            chunk = SearchChunk(
                id=section.id,
                resource_id=section.resource_id,
                version_id=section.version_id,
                chunk_index=section.section_order,
                section_title=section.title,
                content=snippet,
                section_id=section.id,
                section_type=section.section_type,
                window_group_id=section.section_key,
            )
            output.append(_citation(chunk, len(output) + 1, trim_snippet=True))
            if len(output) == limit:
                break
    return output


def _resolve_entity(sections: list[SearchSection], entity: str) -> SearchSection | None:
    normalized = re.sub(r"[\s项目经历：:（）()]", "", entity.lower())  # noqa: RUF001
    best: SearchSection | None = None
    best_score = 0
    for section in sections:
        candidates = [section.title, section.canonical_entity_name or "", *section.aliases]
        score = 0
        for item in candidates:
            candidate = re.sub(r"[\s项目经历：:（）()]", "", item.lower())  # noqa: RUF001
            if candidate == normalized:
                score = 3
                break
            if candidate and (candidate in normalized or normalized in candidate):
                score = max(score, 2)
        if score > best_score:
            best = section
            best_score = score
    return best


def _truncate(value: str) -> str:
    return value if len(value) <= 200 else value[:200] + "..."


__all__ = ["LegacySearchService", "QueryEmbedder", "QueryReranker", "SearchRepository"]
