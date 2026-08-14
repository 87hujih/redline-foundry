"""Format-neutral canonical document AST and deterministic identity helpers."""

# JSON metadata is intentionally dynamic at this domain boundary.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"


class NodeType(StrEnum):
    DOCUMENT = "document"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    PAGE = "page"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"


@dataclass
class SourceLocation:
    file_name: str
    start_offset: int
    end_offset: int
    start_line: int = 0
    end_line: int = 0


@dataclass
class PageMapping:
    page: int
    start_offset: int
    end_offset: int


@dataclass
class Node:
    node_id: str
    type: NodeType
    attributes: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    content: str = ""
    children: list[Node] = field(default_factory=lambda: list[Node]())
    source_location: SourceLocation = field(default_factory=lambda: SourceLocation("", 0, 0))
    page_mapping: list[PageMapping] = field(default_factory=lambda: list[PageMapping]())
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    content_hash: str = ""


@dataclass
class Document:
    document_id: str
    version_id: str
    root: Node
    source_format: str
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    content_hash: str = ""
    schema_version: str = SCHEMA_VERSION


def stable_node_id(document_id: str, structural_path: str, node_type: NodeType) -> str:
    payload = f"{document_id.strip()}\0{structural_path}\0{node_type.value}"
    return "node_" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, SourceLocation):
        result: dict[str, Any] = {
            "file_name": value.file_name,
            "start_offset": value.start_offset,
            "end_offset": value.end_offset,
        }
        if value.start_line:
            result["start_line"] = value.start_line
        if value.end_line:
            result["end_line"] = value.end_line
        return result
    if isinstance(value, PageMapping):
        return {
            "page": value.page,
            "start_offset": value.start_offset,
            "end_offset": value.end_offset,
        }
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Node):
        return {
            "node_id": value.node_id,
            "type": value.type.value,
            "attributes": _json_value(value.attributes),
            "content": value.content,
            "children": [_json_value(child) for child in value.children],
            "source_location": _json_value(value.source_location),
            "page_mapping": _json_value(value.page_mapping),
            "metadata": _json_value(value.metadata),
            "content_hash": value.content_hash,
        }
    if isinstance(value, Document):
        return {
            "document_id": value.document_id,
            "version_id": value.version_id,
            "root": _json_value(value.root),
            "source_format": value.source_format,
            "metadata": _json_value(value.metadata),
            "content_hash": value.content_hash,
            "schema_version": value.schema_version,
        }
    return value


def _node_hash_shape(node: Node) -> dict[str, Any]:
    return {
        "type": node.type.value,
        "attributes": _json_value(node.attributes),
        "content": node.content,
        "child_ids": [child.node_id for child in node.children],
        "source_location": _json_value(node.source_location),
        "page_mapping": _json_value(node.page_mapping),
        "metadata": _json_value(node.metadata),
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(_json_value(value), ensure_ascii=False, separators=(",", ":")).encode()


def hash_node(node: Node) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(_node_hash_shape(node))).hexdigest()


def rehash(document: Document) -> None:
    seen: set[int] = set()

    def visit(node: Node) -> None:
        if id(node) in seen:
            raise ValueError(f"cycle or multiply-parented node {node.node_id}")
        seen.add(id(node))
        for child in node.children:
            visit(child)
        node.content_hash = hash_node(node)

    visit(document.root)
    copy = Document(
        document_id=document.document_id,
        version_id=document.version_id,
        root=document.root,
        source_format=document.source_format,
        metadata=document.metadata,
        schema_version=document.schema_version,
    )
    document.content_hash = "sha256:" + hashlib.sha256(_canonical_json(copy)).hexdigest()


def flatten(root: Node) -> list[Node]:
    result: list[Node] = []
    seen: set[int] = set()

    def visit(node: Node) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        result.append(node)
        for child in node.children:
            visit(child)

    visit(root)
    return result


def validate(document: Document) -> None:
    if not document.document_id.strip() or not document.version_id.strip():
        raise ValueError("document_id and version_id are required")
    if document.schema_version != SCHEMA_VERSION or document.root.type is not NodeType.DOCUMENT:
        raise ValueError("canonical schema version and document root are required")
    if not document.root.source_location.file_name.strip():
        raise ValueError("document source file is required")
    source_file = document.root.source_location.file_name
    source_end = document.root.source_location.end_offset
    seen_ids: set[str] = set()
    seen_ptrs: set[int] = set()

    def walk(node: Node) -> None:
        if not node.node_id.strip() or not node.type.value:
            raise ValueError("every node requires node_id and type")
        if node.node_id in seen_ids or id(node) in seen_ptrs:
            raise ValueError(f"duplicate or cyclic node {node.node_id}")
        seen_ids.add(node.node_id)
        seen_ptrs.add(id(node))
        location = node.source_location
        if (
            location.file_name != source_file
            or location.start_offset < 0
            or location.end_offset < location.start_offset
            or location.end_offset > source_end
            or location.start_line < 0
            or location.end_line < 0
            or (location.end_line and location.end_line < location.start_line)
        ):
            raise ValueError(f"invalid source location for {node.node_id}")
        if node.content_hash != hash_node(node):
            raise ValueError(f"stale content_hash for {node.node_id}")
        for child in node.children:
            walk(child)

    walk(document.root)
    copy = Document(
        document_id=document.document_id,
        version_id=document.version_id,
        root=document.root,
        source_format=document.source_format,
        metadata=document.metadata,
        schema_version=document.schema_version,
    )
    rehash(copy)
    if not document.content_hash or document.content_hash != copy.content_hash:
        raise ValueError("stale document content_hash")


__all__ = [
    "SCHEMA_VERSION",
    "Document",
    "Node",
    "NodeType",
    "PageMapping",
    "SourceLocation",
    "flatten",
    "hash_node",
    "rehash",
    "stable_node_id",
    "validate",
]
