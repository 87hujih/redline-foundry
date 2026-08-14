"""Stable section-aware chunks derived from canonical document nodes."""

from __future__ import annotations

from dataclasses import dataclass

from docreview.document.model import Document, NodeType, flatten

MAX_CHUNK_CHARS = 800


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_index: int
    node_id: str
    section_key: str
    section_title: str
    content: str
    content_hash: str
    chunk_role: str = "section_body"
    order_in_section: int = 1
    window_group_id: str = ""


def _paragraphs(content: str) -> list[str]:
    return [part.strip() for part in content.split("\n\n") if part.strip()]


def build_chunks(document: Document) -> list[Chunk]:
    chunks: list[Chunk] = []
    section_title = "全文"
    section_key = document.root.node_id
    order = 0
    for node in flatten(document.root)[1:]:
        if node.type is NodeType.HEADING:
            section_title = node.content.strip() or "全文"
            section_key = str(node.metadata.get("section_key") or node.node_id)
            order = 0
            continue
        content = node.content.strip()
        if not content:
            continue
        values = _paragraphs(content) if len(content) > MAX_CHUNK_CHARS else [content]
        for value in values:
            order += 1
            chunks.append(
                Chunk(
                    len(chunks),
                    node.node_id,
                    section_key,
                    section_title,
                    value,
                    node.content_hash,
                    order_in_section=order,
                    window_group_id=section_key,
                )
            )
    return chunks


__all__ = ["MAX_CHUNK_CHARS", "Chunk", "build_chunks"]
