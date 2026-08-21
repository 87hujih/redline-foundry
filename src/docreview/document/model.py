"""与格式无关的规范文档 AST 与确定性身份辅助函数。"""

# JSON metadata 在此领域边界刻意保持动态。
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"


class _OrderedObject(dict[str, Any]):
    """规范编码时标记固定字段顺序。"""


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
        result: _OrderedObject = _OrderedObject(
            {
                "file_name": value.file_name,
                "start_offset": value.start_offset,
                "end_offset": value.end_offset,
            }
        )
        if value.start_line:
            result["start_line"] = value.start_line
        if value.end_line:
            result["end_line"] = value.end_line
        return result
    if isinstance(value, PageMapping):
        return _OrderedObject(
            {
                "page": value.page,
                "start_offset": value.start_offset,
                "end_offset": value.end_offset,
            }
        )
    if isinstance(value, _OrderedObject):
        return _OrderedObject((str(key), _json_value(item)) for key, item in value.items())
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Node):
        return _OrderedObject(
            {
                "node_id": value.node_id,
                "type": _node_type_value(value.type),
                "attributes": _json_value(value.attributes if value.attributes is not None else {}),
                "content": value.content,
                "children": [_json_value(child) for child in value.children],
                "source_location": _json_value(value.source_location),
                "page_mapping": _json_value(
                    value.page_mapping if value.page_mapping is not None else []
                ),
                "metadata": _json_value(value.metadata if value.metadata is not None else {}),
                "content_hash": value.content_hash,
            }
        )
    if isinstance(value, Document):
        return _OrderedObject(
            {
                "document_id": value.document_id,
                "version_id": value.version_id,
                "root": _json_value(value.root),
                "source_format": value.source_format,
                "metadata": _json_value(value.metadata),
                "content_hash": value.content_hash,
                "schema_version": value.schema_version,
            }
        )
    return value


def _node_hash_shape(node: Node) -> dict[str, Any]:
    return _OrderedObject(
        {
            "type": _node_type_value(node.type),
            "attributes": _json_value(node.attributes if node.attributes is not None else {}),
            "content": node.content,
            "child_ids": [child.node_id for child in node.children],
            "source_location": _json_value(node.source_location),
            "page_mapping": _json_value(node.page_mapping if node.page_mapping is not None else []),
            "metadata": _json_value(node.metadata if node.metadata is not None else {}),
        }
    )


def _canonical_json(value: Any) -> bytes:
    """编码服务使用的紧凑、确定性 JSON 形态。

    输出 UTF-8，并转义 HTML 敏感字符（以及 U+2028/U+2029）。将边界集中在
    一个 helper 中，可以让 document/node/patch hash 跨 Runtime 稳定，且不会
    把时间戳或进程状态带入输入。
    """
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode("utf-8")
    )


def canonical_json_bytes(value: Any) -> bytes:
    """返回确定性的规范 JSON 字节。"""
    return _canonical_json(value)


def hash_node(node: Node) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(_node_hash_shape(node))).hexdigest()


def _node_type_value(value: NodeType | str) -> str:
    return value.value if isinstance(value, NodeType) else str(value)


def rehash(document: Document) -> None:
    seen: set[int] = set()

    def visit(node: Node) -> None:
        if id(node) in seen:
            raise ValueError(f"循环 或 多父节点的 节点{node.node_id}")
        seen.add(id(node))
        for child in node.children:
            visit(child)
        node.content_hash = hash_node(node)

    visit(document.root)
    copy = Document(
        document_id=document.document_id,
        version_id=document.version_id,
        root=deepcopy(document.root),
        source_format=document.source_format,
        metadata=deepcopy(document.metadata),
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
        raise ValueError("document_id 和 version_id 为必填项")
    if document.schema_version != SCHEMA_VERSION or document.root.type is not NodeType.DOCUMENT:
        raise ValueError("规范 结构 版本 和 文档 根节点 为必填项")
    _validate_json_value(document.metadata, "document metadata")
    if not document.root.source_location.file_name.strip():
        raise ValueError("文档 来源 文件 为必填项")
    source_file = document.root.source_location.file_name
    source_end = document.root.source_location.end_offset
    seen_ids: set[str] = set()
    seen_ptrs: set[int] = set()

    def walk(node: Node) -> None:
        node_type = _node_type_value(node.type)
        if not node.node_id.strip() or not node_type:
            raise ValueError("每个 节点 需要 node_id 和 类型")
        try:
            NodeType(node_type)
        except (TypeError, ValueError) as error:
            raise ValueError(f"未知的 节点 类型{node.type!r}") from error
        if node.node_id in seen_ids or id(node) in seen_ptrs:
            raise ValueError(f"重复的 或 循环的 节点{node.node_id}")
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
            raise ValueError(f"无效的 来源 位置 用于{node.node_id}")
        for mapping in node.page_mapping:
            if (
                mapping.page < 1
                or mapping.start_offset < location.start_offset
                or mapping.end_offset < mapping.start_offset
                or mapping.end_offset > location.end_offset
            ):
                raise ValueError(f"无效的 页面 映射 用于{node.node_id}")
        _validate_json_value(node.attributes, "node attributes")
        _validate_json_value(node.metadata, "node metadata")
        if node.content_hash != hash_node(node):
            raise ValueError(f"过期的 content_hash 用于{node.node_id}")
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
        raise ValueError("过期的 文档 content_hash")


def _validate_json_value(value: Any, name: str) -> None:
    """拒绝 encoding/json 无法确定性表示的值。"""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name}包含非有限数字")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{name}包含空键")
            _validate_json_value(item, name)
        return
    raise ValueError(f"{name}包含不受支持的值{type(value).__name__}")


__all__ = [
    "SCHEMA_VERSION",
    "Document",
    "Node",
    "NodeType",
    "PageMapping",
    "SourceLocation",
    "canonical_json_bytes",
    "flatten",
    "hash_node",
    "rehash",
    "stable_node_id",
    "validate",
]
