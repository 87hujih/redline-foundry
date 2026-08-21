"""严格的、带版本的规范文档 Patch。

JSON 契约由 ``document/patch`` 模块定义。:class:`PatchSet` 上的附加绑定字段
仅是内存上下文，既不计入 PatchSet hash，也不被严格 JSON 接受。
"""

# 动态 JSON 刻意只在严格边界校验。
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from docreview.document.model import (
    Document,
    Node,
    NodeType,
    PageMapping,
    SourceLocation,
    canonical_json_bytes,
    rehash,
    validate,
)

SCHEMA_VERSION = "1.0"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationType:
    REPLACE_NODE = "replace_node"
    INSERT_BEFORE = "insert_before"
    INSERT_AFTER = "insert_after"
    DELETE_NODE = "delete_node"
    UPDATE_ATTRIBUTES = "update_attributes"


SUPPORTED_OPERATIONS = frozenset(
    {
        OperationType.REPLACE_NODE,
        OperationType.INSERT_BEFORE,
        OperationType.INSERT_AFTER,
        OperationType.DELETE_NODE,
        OperationType.UPDATE_ATTRIBUTES,
    }
)


@dataclass(frozen=True, slots=True)
class Operation:
    op: str
    node_id: str
    expected_hash: str
    content: str | None = None
    attributes: dict[str, Any] | None = None
    expected_parent_id: str = ""
    expected_parent_hash: str = ""
    node: Node | None = None
    expected_parent_version_id: str = ""


@dataclass(frozen=True, slots=True)
class PatchSet:
    schema_version: str
    resource_id: str
    base_version_id: str
    operations: list[Operation]
    evidence_refs: list[str]
    reason: str
    # 可信上下文字段不属于 PatchSet JSON，也不属于其规范 hash。
    workspace_id: str = ""
    base_document_hash: str = ""
    author: str = ""
    principal_type: str = ""
    principal_id: str = ""
    idempotency_key: str = ""
    declared_patch_hash: str = ""
    citations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PatchLimits:
    max_bytes: int = 256 * 1024
    max_operations: int = 100
    max_depth: int = 24
    max_evidence: int = 100
    max_text_bytes: int = 50_000
    max_attribute_bytes: int = 64 * 1024


Limits = PatchLimits


def default_limits() -> PatchLimits:
    return PatchLimits()


def DefaultLimits() -> PatchLimits:
    """默认严格解析限制的公共别名。"""
    return default_limits()


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name}为必填项")
    return value


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _raise_constant(value: str) -> Any:
    raise ValueError(f"PatchSet 包含非有限数字{value}")


def _inspect(value: Any, *, depth: int, limits: PatchLimits) -> None:
    if depth > limits.max_depth:
        raise ValueError("PatchSet 超出深度限制")
    if isinstance(value, dict):
        for child in value.values():
            _inspect(child, depth=depth + 1, limits=limits)
    elif isinstance(value, list):
        for child in value:
            _inspect(child, depth=depth + 1, limits=limits)


def _node_from(value: dict[str, Any]) -> Node:
    allowed = {
        "node_id",
        "type",
        "attributes",
        "content",
        "children",
        "source_location",
        "page_mapping",
        "metadata",
        "content_hash",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"未知的 节点 字段:{sorted(unknown)}")
    missing = (allowed - {"content_hash"}) - set(value)
    if missing:
        raise ValueError(f"缺少 节点 字段:{sorted(missing)}")
    node_id = _required_string(value["node_id"], "node.node_id")
    try:
        node_type = NodeType(_required_string(value["type"], "node.type"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"unknown node type: {value.get('type')!r}") from error
    attributes = value["attributes"]
    children = value["children"]
    source = value["source_location"]
    pages = value["page_mapping"]
    metadata = value["metadata"]
    if (
        not isinstance(attributes, dict)
        or not isinstance(children, list)
        or not isinstance(source, dict)
    ):
        raise ValueError("节点属性、子节点和 source_location 的类型无效")
    if not isinstance(pages, list) or not isinstance(metadata, dict):
        raise ValueError("节点 page_mapping 和元数据的类型无效")
    source_allowed = {"file_name", "start_offset", "end_offset", "start_line", "end_line"}
    if set(source) - source_allowed or {"file_name", "start_offset", "end_offset"} - set(source):
        raise ValueError("节点 source_location 字段 无效")
    file_name = _required_string(source["file_name"], "source_location.file_name")
    offsets = (source["start_offset"], source["end_offset"])
    if any(isinstance(item, bool) or not isinstance(item, int) for item in offsets):
        raise ValueError("来源 偏移量 必须是整数")
    start_line = source.get("start_line", 0)
    end_line = source.get("end_line", 0)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (start_line, end_line)):
        raise ValueError("来源 行号 必须是整数")
    page_values: list[tuple[int, int, int]] = []
    for page in pages:
        if not isinstance(page, dict) or set(page) != {"page", "start_offset", "end_offset"}:
            raise ValueError("页面 映射 字段 无效")
        if any(isinstance(page[key], bool) or not isinstance(page[key], int) for key in page):
            raise ValueError("页面 映射 值 必须是整数")
        page_values.append((page["page"], page["start_offset"], page["end_offset"]))
    if "content" not in value or not isinstance(value["content"], str):
        raise ValueError("节点.内容 为必填项")
    content_hash = value.get("content_hash", "")
    if not isinstance(content_hash, str):
        raise ValueError("节点.content_hash 无效")
    if content_hash and _HASH.fullmatch(content_hash) is None:
        raise ValueError("节点.content_hash 无效")
    return Node(
        node_id=node_id,
        type=node_type,
        attributes=dict(attributes),
        content=value["content"],
        children=[
            _node_from(child) if isinstance(child, dict) else _raise("child must be object")
            for child in children
        ],
        source_location=SourceLocation(file_name, offsets[0], offsets[1], start_line, end_line),
        page_mapping=[PageMapping(*item) for item in page_values],
        metadata=dict(metadata),
        content_hash=content_hash,
    )


def _raise(message: str) -> Any:
    raise ValueError(message)


def _validate_node_budget(node: Node, limits: PatchLimits, *, depth: int = 0) -> None:
    if depth > limits.max_depth:
        raise ValueError("PatchSet 超出深度限制")
    if len(node.content.encode("utf-8")) > limits.max_text_bytes:
        raise ValueError("PatchSet text exceeds limit")
    if len(canonical_json_bytes(node.attributes)) > limits.max_attribute_bytes:
        raise ValueError("PatchSet 属性 超出限制")
    for child in node.children:
        _validate_node_budget(child, limits, depth=depth + 1)


def parse_strict(data: bytes, limits: PatchLimits | None = None, **legacy: int) -> PatchSet:
    """解析 PatchSet, 并拒绝重复键、未知字段和超出预算的输入。"""
    if limits is None:
        limits = PatchLimits(
            max_bytes=legacy.get("max_bytes", 256 * 1024),
            max_operations=legacy.get("max_operations", 100),
            max_depth=legacy.get("max_depth", 24),
            max_evidence=legacy.get("max_evidence", 100),
            max_text_bytes=legacy.get("max_text_bytes", 50_000),
            max_attribute_bytes=legacy.get("max_attribute_bytes", 64 * 1024),
        )
    if limits.max_bytes < 1 or len(data) > limits.max_bytes:
        raise ValueError("PatchSet 超出字节限制")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_raise_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("无效的 PatchSet JSON") from error
    if not isinstance(value, dict):
        raise ValueError("PatchSet 必须是对象")
    _inspect(value, depth=0, limits=limits)
    allowed = {
        "schema_version",
        "resource_id",
        "base_version_id",
        "operations",
        "evidence_refs",
        "reason",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown PatchSet fields: {sorted(unknown)}")
    for field_name in ("schema_version", "resource_id", "base_version_id", "reason"):
        if field_name not in value:
            raise ValueError(f"missing required PatchSet field: {field_name}")
    operations_value = value.get("operations")
    if not isinstance(operations_value, list) or not operations_value:
        raise ValueError("PatchSet 操作 必须是非空数组")
    if len(operations_value) > limits.max_operations:
        raise ValueError("PatchSet 超出操作数限制")
    evidence_value = value.get("evidence_refs", [])
    if not isinstance(evidence_value, list) or len(evidence_value) > limits.max_evidence:
        raise ValueError("PatchSet 超出证据数限制")
    operations: list[Operation] = []
    for item in operations_value:
        if not isinstance(item, dict):
            raise ValueError("操作 必须是对象")
        operation_allowed = {
            "op",
            "node_id",
            "expected_hash",
            "content",
            "attributes",
            "expected_parent_id",
            "expected_parent_hash",
            "node",
        }
        unknown = set(item) - operation_allowed
        if unknown:
            raise ValueError(f"未知的 操作 字段:{sorted(unknown)}")
        operation = Operation(
            op=_required_string(item.get("op"), "operation.op"),
            node_id=_required_string(item.get("node_id"), "operation.node_id"),
            expected_hash=_required_string(item.get("expected_hash"), "operation.expected_hash"),
            content=item.get("content"),
            attributes=item.get("attributes"),
            expected_parent_id=item.get("expected_parent_id", ""),
            expected_parent_hash=item.get("expected_parent_hash", ""),
            node=_node_from(item["node"])
            if "node" in item and isinstance(item["node"], dict)
            else None,
        )
        if "node" in item and not isinstance(item["node"], dict):
            raise ValueError("操作.节点 必须是对象")
        if operation.content is not None and not isinstance(operation.content, str):
            raise ValueError("操作.内容 必须是 文本")
        if operation.attributes is not None and not isinstance(operation.attributes, dict):
            raise ValueError("操作.属性 必须是对象")
        if not isinstance(operation.expected_parent_id, str) or not isinstance(
            operation.expected_parent_hash, str
        ):
            raise ValueError("预期 父节点 绑定 必须是字符串")
        if (
            operation.content is not None
            and len(operation.content.encode("utf-8")) > limits.max_text_bytes
        ):
            raise ValueError("PatchSet text exceeds limit")
        if (
            operation.attributes is not None
            and len(canonical_json_bytes(operation.attributes)) > limits.max_attribute_bytes
        ):
            raise ValueError("PatchSet 属性 超出限制")
        if operation.node is not None:
            _validate_node_budget(operation.node, limits)
        _validate_operation(operation)
        operations.append(operation)
    patch = PatchSet(
        schema_version=_required_string(value["schema_version"], "schema_version"),
        resource_id=_required_string(value["resource_id"], "resource_id"),
        base_version_id=_required_string(value["base_version_id"], "base_version_id"),
        operations=operations,
        evidence_refs=[_required_string(item, "evidence_ref") for item in evidence_value],
        reason=_required_string(value["reason"], "reason"),
    )
    validate_set(patch)
    return patch


def validate_set(patch: PatchSet, *, limits: PatchLimits | None = None) -> None:
    limits = limits or PatchLimits()
    if patch.schema_version != SCHEMA_VERSION:
        raise ValueError(f"不支持的 PatchSet schema_version{patch.schema_version!r}")
    if (
        not patch.resource_id.strip()
        or not patch.base_version_id.strip()
        or not patch.reason.strip()
    ):
        raise ValueError("resource_id, base_version_id 和 原因 为必填项")
    if not patch.operations or len(patch.operations) > limits.max_operations:
        raise ValueError("PatchSet 操作为空或超出数量限制")
    if len(patch.evidence_refs) > limits.max_evidence:
        raise ValueError("PatchSet 证据 超出限制")
    seen: set[str] = set()
    for reference in patch.evidence_refs:
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or reference != reference.strip()
            or reference in seen
        ):
            raise ValueError("evidence_refs 必须是 唯一 和 非空")
        seen.add(reference)
    for operation in patch.operations:
        _validate_operation(operation)
        if (
            operation.content is not None
            and len(operation.content.encode("utf-8")) > limits.max_text_bytes
        ):
            raise ValueError("PatchSet text exceeds limit")
        if (
            operation.attributes is not None
            and len(canonical_json_bytes(operation.attributes)) > limits.max_attribute_bytes
        ):
            raise ValueError("PatchSet 属性 超出限制")
        if operation.node is not None:
            _validate_node_budget(operation.node, limits)


def _validate_operation(operation: Operation) -> None:
    if operation.op not in SUPPORTED_OPERATIONS:
        raise ValueError(f"unsupported operation {operation.op!r}")
    if not operation.node_id.strip() or _HASH.fullmatch(operation.expected_hash) is None:
        raise ValueError("node_id 和 小写 sha256 expected_hash 为必填项")
    if operation.op == OperationType.REPLACE_NODE:
        if (
            operation.content is None
            or operation.node is not None
            or operation.attributes is not None
            or operation.expected_parent_id
            or operation.expected_parent_hash
            or operation.expected_parent_version_id
        ):
            raise ValueError("replace_node 接受 仅 内容")
    elif operation.op in {OperationType.INSERT_BEFORE, OperationType.INSERT_AFTER}:
        if (
            operation.node is None
            or not operation.expected_parent_id.strip()
            or _HASH.fullmatch(operation.expected_parent_hash) is None
        ):
            raise ValueError("写入 需要 节点 和 父节点 身份/哈希")
        if operation.content is not None or operation.attributes is not None:
            raise ValueError("插入操作不能携带目标节点以外的内容或属性")
    elif operation.op == OperationType.DELETE_NODE:
        if (
            operation.content is not None
            or operation.node is not None
            or operation.attributes is not None
            or operation.expected_parent_id
            or operation.expected_parent_hash
            or operation.expected_parent_version_id
        ):
            raise ValueError("delete_node 接受 不接受载荷")
    elif (
        operation.attributes is None
        or operation.content is not None
        or operation.node is not None
        or operation.expected_parent_id
        or operation.expected_parent_hash
        or operation.expected_parent_version_id
    ):
        raise ValueError("update_attributes 接受 仅接受属性")


def _node_json(node: Node) -> dict[str, Any]:
    from docreview.document.model import _json_value

    # 规范化 map 字段时保留固定字段顺序。
    return {
        "node_id": node.node_id,
        "type": node.type.value if isinstance(node.type, NodeType) else str(node.type),
        "attributes": _json_value(node.attributes),
        "content": node.content,
        "children": [_node_json(child) for child in node.children],
        "source_location": _json_value(node.source_location),
        "page_mapping": _json_value(node.page_mapping),
        "metadata": _json_value(node.metadata),
        "content_hash": node.content_hash,
    }


def _as_json(patch: PatchSet) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for operation in patch.operations:
        value: dict[str, Any] = {
            "op": operation.op,
            "node_id": operation.node_id,
            "expected_hash": operation.expected_hash,
        }
        if operation.content is not None:
            value["content"] = operation.content
        if operation.attributes is not None:
            from docreview.document.model import _json_value

            value["attributes"] = _json_value(operation.attributes)
        if operation.expected_parent_id:
            value["expected_parent_id"] = operation.expected_parent_id
        if operation.expected_parent_hash:
            value["expected_parent_hash"] = operation.expected_parent_hash
        if operation.node is not None:
            value["node"] = _node_json(operation.node)
        operations.append(value)
    return {
        "schema_version": patch.schema_version,
        "resource_id": patch.resource_id,
        "base_version_id": patch.base_version_id,
        "operations": operations,
        "evidence_refs": patch.evidence_refs,
        "reason": patch.reason,
    }


def canonical_patch_bytes(patch: PatchSet) -> bytes:
    validate_set(patch)
    import json

    encoded = json.dumps(
        _as_json(patch), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode("utf-8")
    )


def patch_hash(patch: PatchSet) -> str:
    return "sha256:" + hashlib.sha256(canonical_patch_bytes(patch)).hexdigest()


def apply_patch(document: Document, patch: PatchSet) -> Document:
    validate_set(patch)
    if document.document_id != patch.resource_id or document.version_id != patch.base_version_id:
        raise ValueError("PatchSet 与文档或基础版本不匹配")
    result = _clone_document(document)
    for operation in patch.operations:
        node, parent, index = _locate(result.root, operation.node_id)
        if node is None:
            raise ValueError(f"节点{operation.node_id}未找到")
        if node.content_hash != operation.expected_hash:
            raise ValueError(f"节点{operation.node_id}expected_hash 不匹配")
        if operation.op == OperationType.REPLACE_NODE:
            node.content = str(operation.content)
        elif operation.op == OperationType.UPDATE_ATTRIBUTES:
            for key, value in (operation.attributes or {}).items():
                if value is None:
                    node.attributes.pop(key, None)
                else:
                    node.attributes[key] = value
        elif operation.op == OperationType.DELETE_NODE:
            if parent is None:
                raise ValueError("文档 根节点 不能是 删除")
            del parent.children[index]
        else:
            if parent is None or parent.node_id != operation.expected_parent_id:
                raise ValueError("预期 父节点 不匹配")
            if parent.content_hash != operation.expected_parent_hash:
                raise ValueError("预期 父节点 哈希 不匹配")
            inserted = _clone_node(operation.node)
            parent.children.insert(index + (operation.op == OperationType.INSERT_AFTER), inserted)
    rehash(result)
    validate(result)
    return result


def _clone_node(node: Node | None) -> Node:
    if node is None:
        raise ValueError("必须提供待插入节点")
    return Node(
        node.node_id,
        node.type,
        dict(node.attributes),
        node.content,
        [_clone_node(child) for child in node.children],
        node.source_location,
        list(node.page_mapping),
        dict(node.metadata),
        node.content_hash,
    )


def _clone_document(document: Document) -> Document:
    return Document(
        document.document_id,
        document.version_id,
        _clone_node(document.root),
        document.source_format,
        dict(document.metadata),
        document.content_hash,
        document.schema_version,
    )


def _locate(root: Node, node_id: str) -> tuple[Node | None, Node | None, int]:
    seen: set[int] = set()

    def visit(
        node: Node, parent: Node | None = None, index: int = -1
    ) -> tuple[Node | None, Node | None, int]:
        if id(node) in seen:
            return None, None, -1
        seen.add(id(node))
        if node.node_id == node_id:
            return node, parent, index
        for child_index, child in enumerate(node.children):
            found = visit(child, node, child_index)
            if found[0] is not None:
                return found
        return None, None, -1

    return visit(root)


__all__ = [
    "SUPPORTED_OPERATIONS",
    "DefaultLimits",
    "Limits",
    "Operation",
    "OperationType",
    "PatchLimits",
    "PatchSet",
    "apply_patch",
    "canonical_patch_bytes",
    "default_limits",
    "parse_strict",
    "patch_hash",
    "validate_set",
]
