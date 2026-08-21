"""纯 Patch validation ToolRuntime 适配器。

适配器不依赖 approval、commit、outbox、database 或 provider。它接收可信的
request factory，由调用方决定如何取得不可变快照；后端本身只能返回 Validator
结果。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, cast

from docreview.document.commit import commit
from docreview.document.patch import canonical_patch_bytes, parse_strict
from docreview.document.validation import ValidationRequest, validate_patch
from docreview.tool_runtime.models import (
    BackendRequest,
    Provenance,
    ToolBackendFailure,
    ToolErrorCategory,
    ToolResult,
)
from docreview.tool_runtime.schema import JSONObject, JSONValue


class ValidationRequestFactory(Protocol):
    def __call__(
        self, request: BackendRequest, patch: object
    ) -> ValidationRequest | Awaitable[ValidationRequest]: ...


@dataclass(frozen=True, slots=True)
class CommitScope:
    authorized_node_ids: frozenset[str]
    evidence_refs: frozenset[str]


class CommitScopeResolver(Protocol):
    async def resolve(self, request: BackendRequest, patch: object) -> CommitScope: ...


class PatchValidationBackend:
    """运行确定性的 Patch 校验，绝不创建副作用。"""

    def __init__(self, request_factory: ValidationRequestFactory) -> None:
        self._request_factory = request_factory

    async def execute(self, request: BackendRequest) -> ToolResult:
        raw_patch = request.tool_input.get("patch")
        if not isinstance(raw_patch, dict):
            raise ToolBackendFailure(
                ToolErrorCategory.INVALID_INPUT,
                "补丁校验输入必须包含补丁对象",
            )
        try:
            patch = parse_strict(json.dumps(raw_patch, ensure_ascii=False).encode("utf-8"))
            candidate = self._request_factory(request, patch)
            validation_request = await candidate if isinstance(candidate, Awaitable) else candidate
            result = validate_patch(validation_request)
        except (TypeError, ValueError) as error:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, str(error)) from error
        output: JSONObject = {
            "valid": result.valid,
            "errors": cast(
                JSONValue,
                [
                    {
                        "category": item.category.value,
                        "message": item.message,
                        "operation_index": item.operation_index,
                        "node_id": item.node_id,
                    }
                    for item in result.errors
                ],
            ),
        }
        if result.validated_patch is not None:
            validated = result.validated_patch
            output["validated_patch"] = cast(
                JSONValue, json.loads(canonical_patch_bytes(validated.patch).decode("utf-8"))
            )
            output.update(
                {
                    "canonical_patch_hash": validated.canonical_patch_hash,
                    "target_resource_id": validated.target_resource_id,
                    "target_version_id": validated.target_version_id,
                    "affected_node_ids": list(validated.affected_node_ids),
                    "evidence_references": list(validated.evidence_references),
                    **(
                        {}
                        if validated.required_approval is None
                        else {
                            "required_approval": {
                                "approval_id": validated.required_approval.approval_id,
                                "workspace_id": validated.required_approval.workspace_id,
                                "resource_id": validated.required_approval.resource_id,
                                "version_id": validated.required_approval.version_id,
                                "principal_type": validated.required_approval.principal_type,
                                "principal_id": validated.required_approval.principal_id,
                                "idempotency_key": validated.required_approval.idempotency_key,
                                "patch_hash": validated.required_approval.patch_hash,
                            }
                        }
                    ),
                    "summary": validated.summary,
                }
            )
        return ToolResult(
            output=output,
            provenance=(
                Provenance(
                    source_type="patch_validation",
                    source_id=request.context.request_id,
                    resource_id=request.context.resource_id,
                    version_id=validation_request.snapshot.current_version_id,
                    trust_level="trusted",
                ),
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        return None


class PatchCommitBackend:
    """规范 Serializable Commit 存储的写入 Tool 边界。

    后端只接受严格 PatchSet，并将所有变更委托给 ``document.commit.commit``。
    Approval 创建/决策有意位于本方法之外，必须在执行前由 ToolRuntime 满足。
    """

    def __init__(self, store: object, scope_resolver: CommitScopeResolver | None) -> None:
        if scope_resolver is None:
            raise ValueError("补丁提交范围解析器为必填项")
        self._store = store
        self._scope_resolver = scope_resolver

    async def execute(self, request: BackendRequest) -> ToolResult:
        raw_patch = request.tool_input.get("patch")
        if not isinstance(raw_patch, dict):
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "补丁提交输入无效")
        try:
            patch = parse_strict(json.dumps(raw_patch, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "补丁提交输入无效") from error
        try:
            scope = await self._scope_resolver.resolve(request, patch)
        except Exception as error:
            raise ToolBackendFailure(
                ToolErrorCategory.UNAUTHORIZED, "补丁提交范围未获授权"
            ) from error
        try:
            result = await commit(
                store=self._store,  # type: ignore[arg-type]
                workspace_id=request.context.workspace_id,
                resource_id=request.context.resource_id,
                idempotency_key=request.idempotency_key,
                actor_id=request.context.principal.id,
                patch=patch,
                authorized_node_ids=scope.authorized_node_ids,
                evidence_refs=scope.evidence_refs,
            )
        except RuntimeError as error:
            raise ToolBackendFailure(ToolErrorCategory.IDEMPOTENCY_CONFLICT, str(error)) from error
        except (TypeError, ValueError, LookupError) as error:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE, "规范 commit 失败"
            ) from error
        output: JSONObject = {
            "resource_id": result.resource_id,
            "version_id": result.version_id,
            "outbox_id": result.outbox_id,
            "created": result.created,
        }
        return ToolResult(
            output=output,
            provenance=(
                Provenance(
                    source_type="canonical_commit",
                    source_id=result.version_id,
                    resource_id=result.resource_id,
                    version_id=result.version_id,
                    trust_level="trusted",
                ),
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        del request
        return None


__all__ = [
    "CommitScope",
    "CommitScopeResolver",
    "PatchCommitBackend",
    "PatchValidationBackend",
    "ValidationRequestFactory",
]
