"""Strict, versioned node-ID PatchSet parsing, hashing and application."""

# Strict field validation happens at the JSON boundary before dynamic payloads enter the AST.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from docreview.document.model import Document, Node, rehash, validate

SCHEMA_VERSION = "1.0"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


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


@dataclass(frozen=True, slots=True)
class PatchSet:
    schema_version: str
    resource_id: str
    base_version_id: str
    operations: list[Operation]
    evidence_refs: list[str]
    reason: str


def _node_from(value: dict[str, Any]) -> Node:
    from docreview.document.model import NodeType, PageMapping, SourceLocation

    location = value.get("source_location", {})
    return Node(
        node_id=str(value["node_id"]),
        type=NodeType(value["type"]),
        attributes=dict(value.get("attributes") or {}),
        content=str(value.get("content") or ""),
        children=[_node_from(item) for item in value.get("children", [])],
        source_location=SourceLocation(
            str(location.get("file_name") or ""),
            int(location.get("start_offset", 0)),
            int(location.get("end_offset", 0)),
            int(location.get("start_line", 0)),
            int(location.get("end_line", 0)),
        ),
        page_mapping=[
            PageMapping(int(item["page"]), int(item["start_offset"]), int(item["end_offset"]))
            for item in value.get("page_mapping", [])
        ],
        metadata=dict(value.get("metadata") or {}),
        content_hash=str(value.get("content_hash") or ""),
    )


def parse_strict(
    data: bytes,
    *,
    max_bytes: int = 256 * 1024,
    max_operations: int = 100,
    max_depth: int = 24,
    max_evidence: int = 100,
) -> PatchSet:
    if len(data) > max_bytes:
        raise ValueError("PatchSet exceeds byte limit")

    def inspect(value: Any, depth: int = 0) -> None:
        if depth > max_depth:
            raise ValueError("PatchSet exceeds depth limit")
        if isinstance(value, dict):
            for item in value.values():
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = item
        return result

    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_int=int,
            parse_float=float,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid PatchSet JSON") from error
    inspect(value)
    if not isinstance(value, dict):
        raise ValueError("PatchSet must be an object")
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
    operations_value = value.get("operations")
    if (
        not isinstance(operations_value, list)
        or len(operations_value) == 0
        or len(operations_value) > max_operations
    ):
        raise ValueError("PatchSet operations limit or emptiness violated")
    evidence = value.get("evidence_refs", [])
    if not isinstance(evidence, list) or len(evidence) > max_evidence:
        raise ValueError("PatchSet evidence limit violated")
    operations: list[Operation] = []
    for item in operations_value:
        if not isinstance(item, dict):
            raise ValueError("operation must be an object")
        operation = Operation(
            op=str(item.get("op") or ""),
            node_id=str(item.get("node_id") or ""),
            expected_hash=str(item.get("expected_hash") or ""),
            content=item.get("content"),
            attributes=item.get("attributes"),
            expected_parent_id=str(item.get("expected_parent_id") or ""),
            expected_parent_hash=str(item.get("expected_parent_hash") or ""),
            node=_node_from(item["node"]) if isinstance(item.get("node"), dict) else None,
        )
        _validate_operation(operation)
        operations.append(operation)
    patch = PatchSet(
        str(value.get("schema_version") or ""),
        str(value.get("resource_id") or ""),
        str(value.get("base_version_id") or ""),
        operations,
        [str(item) for item in evidence],
        str(value.get("reason") or ""),
    )
    validate_set(patch)
    return patch


def validate_set(patch: PatchSet) -> None:
    if patch.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported PatchSet schema_version")
    if (
        not patch.resource_id.strip()
        or not patch.base_version_id.strip()
        or not patch.reason.strip()
    ):
        raise ValueError("resource_id, base_version_id and reason are required")
    if not patch.operations:
        raise ValueError("at least one operation is required")
    seen: set[str] = set()
    for ref in patch.evidence_refs:
        if not ref.strip() or ref in seen:
            raise ValueError("evidence_refs must be unique and non-empty")
        seen.add(ref)
    for operation in patch.operations:
        _validate_operation(operation)


def _validate_operation(operation: Operation) -> None:
    if not operation.node_id.strip() or not _HASH.fullmatch(operation.expected_hash):
        raise ValueError("node_id and lowercase sha256 expected_hash are required")
    if operation.op == "replace_node":
        if (
            operation.content is None
            or operation.node is not None
            or operation.attributes is not None
            or operation.expected_parent_id
            or operation.expected_parent_hash
        ):
            raise ValueError("replace_node accepts only content")
    elif operation.op in {"insert_before", "insert_after"}:
        if (
            operation.node is None
            or not operation.expected_parent_id
            or not _HASH.fullmatch(operation.expected_parent_hash)
            or operation.content is not None
            or operation.attributes is not None
        ):
            raise ValueError("insert requires node and parent identity/hash")
    elif operation.op == "delete_node":
        if (
            operation.content is not None
            or operation.node is not None
            or operation.attributes is not None
            or operation.expected_parent_id
            or operation.expected_parent_hash
        ):
            raise ValueError("delete_node accepts no payload")
    elif operation.op == "update_attributes":
        if (
            operation.attributes is None
            or operation.content is not None
            or operation.node is not None
            or operation.expected_parent_id
            or operation.expected_parent_hash
        ):
            raise ValueError("update_attributes accepts only attributes")
    else:
        raise ValueError(f"unsupported operation {operation.op!r}")


def _as_json(patch: PatchSet) -> dict[str, Any]:
    def node_value(node: Node) -> dict[str, Any]:
        from docreview.document.model import _json_value

        return _json_value(node)

    operations = []
    for operation in patch.operations:
        value: dict[str, Any] = {
            "op": operation.op,
            "node_id": operation.node_id,
            "expected_hash": operation.expected_hash,
        }
        if operation.content is not None:
            value["content"] = operation.content
        if operation.attributes is not None:
            value["attributes"] = operation.attributes
        if operation.expected_parent_id:
            value["expected_parent_id"] = operation.expected_parent_id
        if operation.expected_parent_hash:
            value["expected_parent_hash"] = operation.expected_parent_hash
        if operation.node is not None:
            value["node"] = node_value(operation.node)
        operations.append(value)
    return {
        "schema_version": patch.schema_version,
        "resource_id": patch.resource_id,
        "base_version_id": patch.base_version_id,
        "operations": operations,
        "evidence_refs": patch.evidence_refs,
        "reason": patch.reason,
    }


def patch_hash(patch: PatchSet) -> str:
    payload = json.dumps(_as_json(patch), ensure_ascii=False, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def apply_patch(document: Document, patch: PatchSet) -> Document:
    validate_set(patch)
    if document.document_id != patch.resource_id or document.version_id != patch.base_version_id:
        raise ValueError("PatchSet does not match document/base version")
    result = _clone_document(document)
    for operation in patch.operations:
        node, parent, index = _locate(result.root, operation.node_id)
        if node is None or node.content_hash != operation.expected_hash:
            raise ValueError(f"node {operation.node_id} expected_hash mismatch")
        if operation.op == "replace_node":
            node.content = str(operation.content)
        elif operation.op == "update_attributes":
            for key, value in (operation.attributes or {}).items():
                if value is None:
                    node.attributes.pop(key, None)
                else:
                    node.attributes[key] = value
        elif operation.op == "delete_node":
            if parent is None:
                raise ValueError("document root cannot be deleted")
            del parent.children[index]
        else:
            if (
                parent is None
                or parent.node_id != operation.expected_parent_id
                or parent.content_hash != operation.expected_parent_hash
            ):
                raise ValueError("expected parent mismatch")
            inserted = _clone_node(operation.node)
            parent.children.insert(index + (operation.op == "insert_after"), inserted)
    rehash(result)
    validate(result)
    return result


def _clone_node(node: Node | None) -> Node:
    if node is None:
        raise ValueError("inserted node is required")
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
    if root.node_id == node_id:
        return root, None, -1
    for index, child in enumerate(root.children):
        if child.node_id == node_id:
            return child, root, index
        found = _locate(child, node_id)
        if found[0] is not None:
            return found
    return None, None, -1


__all__ = ["Operation", "PatchSet", "apply_patch", "parse_strict", "patch_hash", "validate_set"]
