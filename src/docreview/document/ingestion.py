"""Structured parser to hierarchical canonical AST ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from docreview.document.model import (
    Document,
    Node,
    NodeType,
    PageMapping,
    SourceLocation,
    rehash,
    stable_node_id,
    validate,
)
from docreview.document.normalize import NormalizedDocument, normalize
from docreview.document.parser import DocumentParser, ParsedDocument, ParsedElement


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
    extracted_text_length = sum(len(element.text.strip()) for element in parsed.elements)
    quality_flags = list(parsed.quality_flags)
    if 0 < extracted_text_length < 16 and "text_short" not in quality_flags:
        quality_flags.append("text_short")
    source_length = len(content.decode("utf-8", errors="replace"))
    root = Node(
        stable_node_id(document_id, "0", NodeType.DOCUMENT),
        NodeType.DOCUMENT,
        content="",
        source_location=SourceLocation(file_name, 0, source_length),
        metadata={
            "parser": parsed.parser_name,
            "quality_flags": quality_flags,
            "section_key": "",
            "section_type": "document",
            "structural_path": "0",
        },
    )
    root.metadata["section_key"] = root.node_id
    headings: list[tuple[int, Node]] = []
    for element in parsed.elements:
        if element.element_type == "page_break":
            continue
        if element.element_type == "heading":
            while headings and headings[-1][0] >= element.level:
                headings.pop()
            parent = headings[-1][1] if headings else root
            node = _node_for_element(
                document_id,
                parent,
                element,
                file_name,
                source_length,
                section_key="",
                section_type=str(element.attributes.get("section_type", "section")),
            )
            node.metadata["section_key"] = node.node_id
            parent.children.append(node)
            headings.append((element.level, node))
            continue
        parent = headings[-1][1] if headings else root
        section_key = str(parent.metadata.get("section_key") or root.node_id)
        section_type = str(parent.metadata.get("section_type") or "document")
        if _merge_paragraph(parent, element, file_name, source_length):
            continue
        node = _node_for_element(
            document_id,
            parent,
            element,
            file_name,
            source_length,
            section_key=section_key,
            section_type=section_type,
        )
        parent.children.append(node)
    document = Document(
        document_id,
        version_id,
        root,
        parsed.source_format,
        {"file_name": file_name, "quality_flags": quality_flags},
    )
    rehash(document)
    validate(document)
    return IngestedDocument(parsed, normalized, document)


def _merge_paragraph(
    parent: Node, element: ParsedElement, file_name: str, source_length: int
) -> bool:
    if (
        element.element_type != "paragraph"
        or element.attributes.get("block_type") == "code"
        or not parent.children
        or parent.children[-1].type is not NodeType.PARAGRAPH
        or parent.children[-1].attributes.get("block_type") == "code"
    ):
        return False
    previous = parent.children[-1]
    span = _source_location(element, file_name, source_length)
    previous.content = previous.content + "\n\n" + element.text
    previous.source_location.end_offset = max(previous.source_location.end_offset, span.end_offset)
    previous.source_location.end_line = max(previous.source_location.end_line, span.end_line)
    previous.metadata.setdefault("block_spans", []).append(_span(element, span))
    previous.metadata["quality_flags"] = sorted(
        set(_string_list(previous.metadata.get("quality_flags")) + list(element.quality_flags))
    )
    previous.page_mapping.extend(_page_mappings(element, span))
    return True


def _node_for_element(
    document_id: str,
    parent: Node,
    element: ParsedElement,
    file_name: str,
    source_length: int,
    *,
    section_key: str,
    section_type: str,
) -> Node:
    node_type = {
        "heading": NodeType.HEADING,
        "paragraph": NodeType.PARAGRAPH,
        "list": NodeType.LIST,
        "table": NodeType.TABLE,
    }.get(element.element_type)
    if node_type is None:
        raise ValueError(f"不支持的 已解析的 元素 类型{element.element_type!r}")
    structural_path = _child_path(parent)
    location = _source_location(element, file_name, source_length)
    node_id = stable_node_id(document_id, structural_path, node_type)
    metadata: dict[str, Any] = {
        "section_key": section_key,
        "section_type": section_type,
        "block_spans": [_span(element, location)],
        "quality_flags": sorted(set(element.quality_flags)),
        "structural_path": structural_path,
    }
    if node_type is NodeType.HEADING:
        metadata["heading_level"] = element.level
    if element.attributes:
        metadata.update(element.attributes)
    node = Node(
        node_id,
        node_type,
        attributes={"level": element.level} if node_type is NodeType.HEADING else {},
        content=element.text,
        source_location=location,
        page_mapping=_page_mappings(element, location),
        metadata=metadata,
    )
    if node_type is NodeType.LIST:
        for item in element.children:
            item_path = _child_path(node)
            item_location = _source_location(item, file_name, source_length)
            item_node = Node(
                stable_node_id(document_id, item_path, NodeType.LIST_ITEM),
                NodeType.LIST_ITEM,
                content=item.text,
                source_location=item_location,
                page_mapping=_page_mappings(item, item_location),
                metadata={
                    "section_key": section_key,
                    "section_type": section_type,
                    "item_order": item.attributes.get("item_order", len(node.children) + 1),
                    "marker": item.attributes.get("marker", ""),
                    "block_spans": [_span(item, item_location)],
                    "quality_flags": sorted(set(item.quality_flags)),
                },
            )
            node.children.append(item_node)
    return node


def _child_path(parent: Node) -> str:
    path = str(parent.metadata.get("structural_path", "0"))
    return f"{path}/{len(parent.children)}"


def _source_location(element: ParsedElement, file_name: str, source_length: int) -> SourceLocation:
    start = min(max(0, element.source_start_offset), source_length)
    end = min(max(start, element.source_end_offset), source_length)
    return SourceLocation(file_name, start, end, element.start_line, element.end_line)


def _page_mappings(element: ParsedElement, location: SourceLocation) -> list[PageMapping]:
    mappings: list[PageMapping] = []
    for item in element.page_mappings:
        if item.page < 1:
            continue
        start = min(max(location.start_offset, item.start_offset), location.end_offset)
        end = min(max(start, item.end_offset), location.end_offset)
        mappings.append(PageMapping(item.page, start, end))
    return mappings


def _span(element: ParsedElement, location: SourceLocation) -> dict[str, object]:
    return {
        "start_offset": location.start_offset,
        "end_offset": location.end_offset,
        "start_line": location.start_line,
        "end_line": location.end_line,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


__all__ = ["IngestedDocument", "ingest"]
