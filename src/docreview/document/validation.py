"""针对不可变文档快照的纯规范 PatchSet 校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum

from docreview.document.model import Document, Node, NodeType, hash_node, validate
from docreview.document.patch import (
    OperationType,
    PatchLimits,
    PatchSet,
    apply_patch,
    canonical_patch_bytes,
    patch_hash,
    validate_set,
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ErrorCategory(StrEnum):
    INVALID_PATCH = "invalid_patch"
    INVALID_NODE = "invalid_node"
    VERSION_CONFLICT = "version_conflict"
    HASH_CONFLICT = "hash_conflict"
    SCOPE_CONFLICT = "scope_conflict"
    STRUCTURE_CONFLICT = "structure_conflict"
    EVIDENCE_CONFLICT = "evidence_conflict"
    APPROVAL_CONFLICT = "approval_conflict"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNSUPPORTED_OPERATION = "unsupported_operation"


@dataclass(frozen=True, slots=True)
class ValidationError:
    category: ErrorCategory
    message: str
    operation_index: int | None = None
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    evidence_id: str
    workspace_id: str
    resource_id: str
    version_id: str
    node_id: str


CitationBinding = EvidenceBinding


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    approval_id: str
    workspace_id: str
    resource_id: str
    version_id: str
    principal_type: str
    principal_id: str
    idempotency_key: str
    patch_hash: str


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    workspace_id: str
    resource_id: str
    current_version_id: str
    document: Document | None
    authorized_node_ids: frozenset[str] = frozenset()
    evidence: tuple[EvidenceBinding | str, ...] = ()
    citations: tuple[EvidenceBinding | str, ...] = ()
    required_approval: bool = False
    base_document_hash: str = ""


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    workspace_id: str
    resource_id: str
    principal_type: str
    principal_id: str
    idempotency_key: str
    patch: PatchSet
    snapshot: ValidationSnapshot
    approval: ApprovalBinding | None = None
    expected_patch_hash: str = ""
    base_document_hash: str = ""
    limits: PatchLimits = dataclass_field(default_factory=PatchLimits)


@dataclass(frozen=True, slots=True)
class ValidatedPatch:
    patch: PatchSet
    canonical_patch_hash: str
    target_resource_id: str
    target_version_id: str
    affected_node_ids: tuple[str, ...]
    evidence_references: tuple[str, ...]
    required_approval: ApprovalBinding | None
    summary: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[ValidationError, ...] = ()
    validated_patch: ValidatedPatch | None = None

    @property
    def canonical_patch_hash(self) -> str | None:
        return self.validated_patch.canonical_patch_hash if self.validated_patch else None

    @property
    def target_resource_id(self) -> str | None:
        return self.validated_patch.target_resource_id if self.validated_patch else None

    @property
    def target_version_id(self) -> str | None:
        return self.validated_patch.target_version_id if self.validated_patch else None

    @property
    def affected_node_ids(self) -> tuple[str, ...]:
        return self.validated_patch.affected_node_ids if self.validated_patch else ()


def validate_patch(request: ValidationRequest) -> ValidationResult:
    """不执行 I/O 或变更; 所有失败均为确定性的。"""
    errors: list[ValidationError] = []

    def add(
        category: ErrorCategory, message: str, index: int | None = None, node_id: str = ""
    ) -> None:
        errors.append(ValidationError(category, message, index, node_id))

    try:
        validate_set(request.patch, limits=request.limits)
    except (TypeError, ValueError) as error:
        message = str(error)
        if "不支持的 操作" in message:
            add(ErrorCategory.UNSUPPORTED_OPERATION, message)
        elif any(
            token in message.casefold()
            for token in (
                "限制",
                "超出",
                "字节",
                "文本",
                "深度",
                "属性",
                "budget",
                "byte",
                "depth",
                "limit",
                "text",
            )
        ):
            add(ErrorCategory.BUDGET_EXCEEDED, message)
        else:
            add(ErrorCategory.INVALID_PATCH, message)
        return ValidationResult(False, tuple(errors))

    # 后续 scope、幂等与 Approval 都绑定 canonical bytes 的同一摘要。
    digest = patch_hash(request.patch)
    if len(canonical_patch_bytes(request.patch)) > request.limits.max_bytes:
        add(ErrorCategory.BUDGET_EXCEEDED, "PatchSet 超出字节限制")
    if request.expected_patch_hash and request.expected_patch_hash != digest:
        add(ErrorCategory.HASH_CONFLICT, "补丁哈希与规范 PatchSet 不匹配")
    if request.patch.declared_patch_hash and request.patch.declared_patch_hash != digest:
        add(ErrorCategory.HASH_CONFLICT, "声明的补丁哈希与规范 PatchSet 不匹配")
    if request.patch.idempotency_key and request.patch.idempotency_key != request.idempotency_key:
        add(ErrorCategory.SCOPE_CONFLICT, "补丁幂等键与可信请求不匹配")
    if not request.idempotency_key.strip():
        add(ErrorCategory.INVALID_PATCH, "必须提供幂等键")
    if request.patch.workspace_id and request.patch.workspace_id != request.workspace_id:
        add(ErrorCategory.SCOPE_CONFLICT, "补丁工作区与可信范围不匹配")
    if request.patch.principal_type and request.patch.principal_type != request.principal_type:
        add(ErrorCategory.SCOPE_CONFLICT, "补丁主体类型与可信范围不匹配")
    if request.patch.principal_id and request.patch.principal_id != request.principal_id:
        add(ErrorCategory.SCOPE_CONFLICT, "补丁主体与可信范围不匹配")
    if request.patch.author and request.patch.author != request.principal_id:
        add(ErrorCategory.SCOPE_CONFLICT, "补丁作者与可信主体不匹配")

    snapshot = request.snapshot
    if snapshot.document is None:
        add(ErrorCategory.INVALID_NODE, "缺少规范文档")
        return ValidationResult(False, tuple(errors))
    if (
        not request.workspace_id.strip()
        or request.workspace_id != snapshot.workspace_id
        or not request.resource_id.strip()
        or request.resource_id != snapshot.resource_id
        or request.patch.resource_id != request.resource_id
    ):
        add(
            ErrorCategory.SCOPE_CONFLICT,
            "工作区或资源与可信快照不匹配",
        )

    if (
        request.patch.base_version_id != snapshot.current_version_id
        or snapshot.document.version_id != snapshot.current_version_id
    ):
        add(ErrorCategory.VERSION_CONFLICT, "基础版本不是当前文档版本")

    expected_document_hash = (
        request.base_document_hash
        or request.patch.base_document_hash
        or snapshot.base_document_hash
    )
    if expected_document_hash and expected_document_hash != snapshot.document.content_hash:
        add(ErrorCategory.HASH_CONFLICT, "基础文档哈希与快照不匹配")
    if _HASH.fullmatch(snapshot.document.content_hash) is None:
        add(ErrorCategory.HASH_CONFLICT, "快照文档哈希无效")

    try:
        validate(snapshot.document)
    except (TypeError, ValueError) as error:
        category = (
            ErrorCategory.INVALID_NODE
            if "未知的节点类型" in str(error)
            else ErrorCategory.STRUCTURE_CONFLICT
        )
        add(category, f"存储的规范文档无效: {error}")
        return ValidationResult(False, tuple(errors))

    nodes, parents, graph_errors = _index(snapshot.document.root)
    for message, node_id in graph_errors:
        add(ErrorCategory.STRUCTURE_CONFLICT, message, node_id=node_id)

    # Evidence 必须来自同一 workspace/resource/current version，不能跨版本复用。
    evidence = {
        item.evidence_id: item for item in snapshot.evidence if isinstance(item, EvidenceBinding)
    }
    evidence_ids = {
        item if isinstance(item, str) else item.evidence_id for item in snapshot.evidence
    }
    citation_ids = {
        item if isinstance(item, str) else item.evidence_id for item in snapshot.citations
    }
    for reference in request.patch.evidence_refs:
        if reference not in evidence_ids:
            add(ErrorCategory.EVIDENCE_CONFLICT, f"缺少证据引用: {reference}")
            continue
        binding = evidence.get(reference)
        if binding is not None and (
            binding.workspace_id != request.workspace_id
            or binding.resource_id != request.resource_id
            or binding.version_id != snapshot.current_version_id
            or binding.node_id not in nodes
        ):
            add(
                ErrorCategory.EVIDENCE_CONFLICT,
                f"证据引用绑定到了错误的节点或版本: {reference}",
            )
    for citation in request.patch.citations:
        if citation not in citation_ids:
            add(ErrorCategory.EVIDENCE_CONFLICT, f"缺少引文引用: {citation}")

    deleted: set[str] = set()
    mutated: set[str] = set()
    new_ids: set[str] = set()
    affected: list[str] = []
    for index, operation in enumerate(request.patch.operations):
        node = nodes.get(operation.node_id)
        if node is None:
            add(
                ErrorCategory.INVALID_NODE,
                "目标版本中不存在该节点 ID",
                index,
                operation.node_id,
            )
            continue
        affected.append(operation.node_id)
        if operation.node_id not in snapshot.authorized_node_ids:
            add(
                ErrorCategory.SCOPE_CONFLICT,
                "节点超出授权范围",
                index,
                operation.node_id,
            )
        if node.content_hash != operation.expected_hash:
            add(
                ErrorCategory.HASH_CONFLICT,
                "预期节点哈希不匹配",
                index,
                operation.node_id,
            )
        if operation.node_id in deleted or operation.node_id in mutated:
            add(
                ErrorCategory.STRUCTURE_CONFLICT,
                "操作顺序重复或无效",
                index,
                operation.node_id,
            )
        mutated.add(operation.node_id)
        if operation.op == OperationType.DELETE_NODE:
            if operation.node_id == snapshot.document.root.node_id:
                add(
                    ErrorCategory.STRUCTURE_CONFLICT,
                    "不能删除根节点",
                    index,
                    operation.node_id,
                )
            deleted.add(operation.node_id)
        if operation.op in {OperationType.INSERT_BEFORE, OperationType.INSERT_AFTER}:
            parent = parents.get(operation.node_id)
            if parent is None or parent.node_id != operation.expected_parent_id:
                add(
                    ErrorCategory.STRUCTURE_CONFLICT,
                    "预期父节点引用无效",
                    index,
                    operation.node_id,
                )
            elif parent.content_hash != operation.expected_parent_hash:
                add(
                    ErrorCategory.HASH_CONFLICT,
                    "预期父节点哈希不匹配",
                    index,
                    parent.node_id,
                )
            if (
                operation.expected_parent_version_id
                and operation.expected_parent_version_id != snapshot.current_version_id
            ):
                add(
                    ErrorCategory.VERSION_CONFLICT,
                    "预期父节点版本与目标版本不匹配",
                    index,
                    operation.expected_parent_id,
                )
            if operation.expected_parent_id not in snapshot.authorized_node_ids:
                add(
                    ErrorCategory.SCOPE_CONFLICT,
                    "父节点超出授权范围",
                    index,
                    operation.expected_parent_id,
                )
            if operation.node is None:
                add(
                    ErrorCategory.INVALID_NODE,
                    "必须提供待插入节点",
                    index,
                    operation.node_id,
                )
            else:
                inserted_ids, inserted_errors = _validate_inserted(
                    operation.node,
                    snapshot.document,
                    nodes,
                    request.limits.max_depth,
                    request.limits.max_text_bytes,
                )
                for message, node_id in inserted_errors:
                    if message.startswith("PatchSet"):
                        category = ErrorCategory.BUDGET_EXCEEDED
                    elif any(
                        token in message for token in ("节点类型", "节点 ID 为空", "节点内容无效")
                    ):
                        category = ErrorCategory.INVALID_NODE
                    else:
                        category = ErrorCategory.STRUCTURE_CONFLICT
                    add(category, message, index, node_id)
                for inserted_id in inserted_ids:
                    if inserted_id in nodes or inserted_id in new_ids:
                        add(
                            ErrorCategory.STRUCTURE_CONFLICT,
                            "PatchSet 中的节点 ID 重复",
                            index,
                            inserted_id,
                        )
                    new_ids.add(inserted_id)

    if request.snapshot.required_approval:
        # Approval 同时绑定主体、scope、幂等键和 canonical patch hash，任一漂移即拒绝。
        if request.approval is None:
            add(ErrorCategory.APPROVAL_CONFLICT, "高影响 PatchSet 需要审批绑定")
        else:
            approval = request.approval
            if (
                approval.workspace_id != request.workspace_id
                or approval.resource_id != request.resource_id
                or approval.version_id != snapshot.current_version_id
                or approval.principal_type != request.principal_type
                or approval.principal_id != request.principal_id
                or approval.idempotency_key != request.idempotency_key
                or approval.patch_hash != digest
                or _HASH.fullmatch(approval.patch_hash) is None
            ):
                add(
                    ErrorCategory.APPROVAL_CONFLICT,
                    "审批绑定与校验通过的 PatchSet 不匹配",
                )

    if errors:
        return ValidationResult(False, tuple(errors))
    try:
        # 只作用于私有副本，绝不修改调用方的快照。
        apply_patch(snapshot.document, request.patch)
    except (TypeError, ValueError) as error:
        add(ErrorCategory.STRUCTURE_CONFLICT, f"应用 PatchSet 失败: {error}")
        return ValidationResult(False, tuple(errors))

    validated = ValidatedPatch(
        patch=request.patch,
        canonical_patch_hash=digest,
        target_resource_id=request.resource_id,
        target_version_id=snapshot.current_version_id,
        affected_node_ids=tuple(dict.fromkeys(affected)),
        evidence_references=tuple(request.patch.evidence_refs),
        required_approval=request.approval if request.snapshot.required_approval else None,
        summary=(
            f"validated {len(request.patch.operations)} operation(s) affecting "
            f"{len(set(affected))} node(s)"
        ),
    )
    return ValidationResult(True, (), validated)


def _index(root: Node) -> tuple[dict[str, Node], dict[str, Node], list[tuple[str, str]]]:
    nodes: dict[str, Node] = {}
    parents: dict[str, Node] = {}
    errors: list[tuple[str, str]] = []
    pointers: set[int] = set()

    def walk(node: Node, parent: Node | None = None) -> None:
        if id(node) in pointers:
            errors.append(("节点存在循环或多个父节点", node.node_id))
            return
        pointers.add(id(node))
        if node.node_id in nodes:
            errors.append(("节点 ID 重复", node.node_id))
        else:
            nodes[node.node_id] = node
        if parent is not None:
            parents.setdefault(node.node_id, parent)
        for child in node.children:
            walk(child, node)

    walk(root)
    return nodes, parents, errors


def _validate_inserted(
    node: Node,
    document: Document,
    existing: dict[str, Node],
    max_depth: int,
    max_text_bytes: int,
) -> tuple[set[str], list[tuple[str, str]]]:
    ids: set[str] = set()
    errors: list[tuple[str, str]] = []
    pointers: set[int] = set()
    source_file = document.root.source_location.file_name
    source_end = document.root.source_location.end_offset

    def walk(current: Node, depth: int) -> None:
        if depth > max_depth:
            errors.append(("PatchSet 超出深度限制", current.node_id))
            return
        if id(current) in pointers:
            errors.append(("插入的节点图包含循环", current.node_id))
            return
        pointers.add(id(current))
        try:
            node_type = NodeType(current.type)
        except (TypeError, ValueError):
            errors.append(("插入的节点类型未知", current.node_id))
            node_type = None
        if not current.node_id.strip():
            errors.append(("插入的节点 ID 为空", current.node_id))
        if len(current.content.encode("utf-8")) > max_text_bytes:
            errors.append(("PatchSet 超出文本限制", current.node_id))
        if current.node_id in ids:
            errors.append(("插入的节点 ID 重复", current.node_id))
        ids.add(current.node_id)
        location = current.source_location
        if (
            location.file_name != source_file
            or location.start_offset < 0
            or location.end_offset < location.start_offset
            or location.end_offset > source_end
            or location.start_line < 0
            or location.end_line < 0
            or (location.end_line and location.end_line < location.start_line)
        ):
            errors.append(("插入节点的来源元数据无效", current.node_id))
        for page in current.page_mapping:
            if (
                page.page < 1
                or page.start_offset < location.start_offset
                or page.end_offset < page.start_offset
                or page.end_offset > location.end_offset
            ):
                errors.append(("插入节点的页面元数据无效", current.node_id))
        if node_type is not None:
            try:
                expected = hash_node(current)
                if current.content_hash not in {"", expected}:
                    errors.append(("插入节点的哈希无效", current.node_id))
            except (TypeError, ValueError):
                errors.append(("插入节点的内容无效", current.node_id))
        for child in current.children:
            walk(child, depth + 1)

    walk(node, 0)
    return ids, errors


__all__ = [
    "ApprovalBinding",
    "CitationBinding",
    "ErrorCategory",
    "EvidenceBinding",
    "ValidatedPatch",
    "ValidationError",
    "ValidationRequest",
    "ValidationResult",
    "ValidationSnapshot",
    "validate_patch",
]
