"""Parser, normalization and canonical AST ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from docreview.document.model import (
    Document,
    Node,
    NodeType,
    SourceLocation,
    rehash,
    stable_node_id,
    validate,
)
from docreview.document.normalize import NormalizedDocument, normalize
from docreview.document.parser import DocumentParser, ParsedDocument


@dataclass(frozen=True, slots=True)
class IngestedDocument:
    parsed: ParsedDocument
    normalized: NormalizedDocument
    document: Document


async def ingest(
    parser: DocumentParser, *, document_id: str, version_id: str, file_name: str, content: bytes
) -> IngestedDocument:
    parsed = await parser.parse(file_name, content)
    normalized = normalize(parsed)
    root = Node(
        stable_node_id(document_id, "0", NodeType.DOCUMENT),
        NodeType.DOCUMENT,
        content="",
        source_location=SourceLocation(
            file_name, 0, len(content.decode("utf-8", errors="replace"))
        ),
        metadata={"parser": parsed.parser_name, "quality_flags": parsed.quality_flags},
    )
    offset = 0
    for section_index, section in enumerate(normalized.sections):
        section_path = f"0/{section_index}"
        heading = Node(
            stable_node_id(document_id, section_path, NodeType.HEADING),
            NodeType.HEADING,
            content=section.title,
            source_location=SourceLocation(file_name, offset, offset + len(section.title)),
            metadata={
                "section_key": section.section_key,
                "section_type": section.type,
                **section.metadata,
            },
        )
        offset += len(section.title)
        root.children.append(heading)
        if section.content:
            paragraph_path = f"{section_path}/0"
            paragraph = Node(
                stable_node_id(document_id, paragraph_path, NodeType.PARAGRAPH),
                NodeType.PARAGRAPH,
                content=section.content,
                source_location=SourceLocation(file_name, offset, offset + len(section.content)),
                metadata={
                    "section_key": section.section_key,
                    "section_type": section.type,
                    **section.metadata,
                },
            )
            offset += len(section.content)
            heading.children.append(paragraph)
    document = Document(
        document_id,
        version_id,
        root,
        parsed.source_format,
        {"file_name": file_name, "quality_flags": parsed.quality_flags},
    )
    rehash(document)
    validate(document)
    return IngestedDocument(parsed, normalized, document)


__all__ = ["IngestedDocument", "ingest"]
