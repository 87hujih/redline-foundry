"""带幂等校验的规范文档原子提交边界。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from docreview.document.model import Document, rehash, validate
from docreview.document.patch import PatchSet, apply_patch, patch_hash, validate_set


@dataclass(frozen=True, slots=True)
class CommitSnapshot:
    document: Document
    current_version_id: str
    authorized_node_ids: frozenset[str]
    evidence_refs: frozenset[str]


@dataclass(frozen=True, slots=True)
class CommitResult:
    resource_id: str
    version_id: str
    outbox_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class StoredCommit:
    patch_hash: str
    result: CommitResult


class CommitStore(Protocol):
    async def get_commit(self, workspace_id: str, idempotency_key: str) -> StoredCommit | None: ...

    async def load_snapshot(self, workspace_id: str, resource_id: str) -> CommitSnapshot: ...

    async def commit_atomic(
        self,
        *,
        workspace_id: str,
        resource_id: str,
        base_version_id: str,
        idempotency_key: str,
        patch_hash: str,
        patch: PatchSet,
        expected_hashes: dict[str, str],
        document: Document,
        actor_id: str,
    ) -> CommitResult: ...


class CommitValidationError(ValueError):
    pass


async def commit(
    *,
    store: CommitStore,
    workspace_id: str,
    resource_id: str,
    idempotency_key: str,
    actor_id: str,
    patch: PatchSet,
    authorized_node_ids: frozenset[str] | None = None,
    evidence_refs: frozenset[str] | None = None,
) -> CommitResult:
    if (
        not workspace_id.strip()
        or not resource_id.strip()
        or not idempotency_key.strip()
        or not actor_id.strip()
    ):
        raise ValueError("workspace_id, resource_id, idempotency_key 和 actor_id 为必填项")
    validate_set(patch)
    if patch.resource_id != resource_id:
        raise CommitValidationError("PatchSet 资源 超出 可信 范围")
    digest = patch_hash(patch)
    existing = await store.get_commit(workspace_id, idempotency_key)
    if existing is not None:
        if existing.patch_hash != digest:
            raise RuntimeError("document commit idempotency conflict")
        return CommitResult(
            existing.result.resource_id,
            existing.result.version_id,
            existing.result.outbox_id,
            False,
        )
    snapshot = await store.load_snapshot(workspace_id, resource_id)
    if authorized_node_ids is not None or evidence_refs is not None:
        snapshot = replace(
            snapshot,
            authorized_node_ids=(
                snapshot.authorized_node_ids if authorized_node_ids is None else authorized_node_ids
            ),
            evidence_refs=snapshot.evidence_refs if evidence_refs is None else evidence_refs,
        )
    if (
        snapshot.current_version_id != patch.base_version_id
        or snapshot.document.version_id != patch.base_version_id
    ):
        raise CommitValidationError("基础版本冲突")
    for operation in patch.operations:
        if operation.node_id not in snapshot.authorized_node_ids:
            raise CommitValidationError(f"节点{operation.node_id}超出 已授权 范围")
        if (
            operation.expected_parent_id
            and operation.expected_parent_id not in snapshot.authorized_node_ids
        ):
            raise CommitValidationError(f"父节点{operation.expected_parent_id}超出 已授权 范围")
    if any(ref not in snapshot.evidence_refs for ref in patch.evidence_refs):
        raise CommitValidationError("证据 引用 缺失")
    try:
        document = apply_patch(snapshot.document, patch)
    except ValueError as error:
        raise CommitValidationError(str(error)) from error
    document.version_id = await _new_version_id(store, workspace_id, resource_id, digest)
    rehash(document)
    validate(document)
    return await store.commit_atomic(
        workspace_id=workspace_id,
        resource_id=resource_id,
        base_version_id=patch.base_version_id,
        idempotency_key=idempotency_key,
        patch_hash=digest,
        patch=patch,
        expected_hashes={
            operation.node_id: operation.expected_hash for operation in patch.operations
        },
        document=document,
        actor_id=actor_id,
    )


async def _new_version_id(
    store: CommitStore, workspace_id: str, resource_id: str, digest: str
) -> str:
    allocator = getattr(store, "allocate_version_id", None)
    if allocator is not None:
        return str(await allocator(workspace_id, resource_id, digest))
    return "version_" + digest.removeprefix("sha256:")[:32]


__all__ = [
    "CommitResult",
    "CommitSnapshot",
    "CommitStore",
    "CommitValidationError",
    "StoredCommit",
    "commit",
]
