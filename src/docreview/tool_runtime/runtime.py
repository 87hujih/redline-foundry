"""已注册生产 Tool 的 fail-closed 执行 pipeline。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, cast

from docreview.approval import (
    Approval,
    ApprovalBinding,
    ApprovalCreateCommand,
)
from docreview.approval import (
    Principal as ApprovalPrincipal,
)
from docreview.tool_runtime.models import (
    ApprovalGrant,
    ApprovalRequirement,
    ArtifactReference,
    ArtifactWriteRequest,
    AuditClaim,
    AuditClaimRequest,
    AuditFinishRequest,
    AuditStatus,
    BackendRequest,
    IdempotencyConflictError,
    PolicyDecision,
    PolicyRequest,
    RateLimitDecision,
    RateLimitRequest,
    ToolBackendFailure,
    ToolDefinition,
    ToolError,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolIntent,
    ToolName,
    ToolObservation,
    ToolResult,
    ToolVersion,
)
from docreview.tool_runtime.registry import ToolRegistry
from docreview.tool_runtime.schema import (
    JSONObject,
    canonical_json_bytes,
    canonical_json_hash,
    decode_json_object,
)


class PolicyBoundary(Protocol):
    async def authorize(self, request: PolicyRequest) -> PolicyDecision: ...


class ApprovalBoundary(Protocol):
    async def load_approval(self, approval_id: str) -> ApprovalGrant | None: ...


class ApprovalCreationBoundary(Protocol):
    async def create(self, command: ApprovalCreateCommand) -> Approval: ...


class RateLimiterBoundary(Protocol):
    async def check(self, request: RateLimitRequest) -> RateLimitDecision: ...


class AuditBoundary(Protocol):
    async def claim(self, request: AuditClaimRequest) -> AuditClaim: ...
    async def finish(self, request: AuditFinishRequest) -> None: ...


class ArtifactBoundary(Protocol):
    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference: ...


class TokenCounter(Protocol):
    def count_json(self, content: bytes) -> int: ...


class BackendBoundary(Protocol):
    async def execute(self, request: BackendRequest) -> ToolResult: ...
    async def recover(self, request: BackendRequest) -> ToolResult | None: ...


class ToolRuntime:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyBoundary,
        approvals: ApprovalBoundary,
        limiter: RateLimiterBoundary,
        audit: AuditBoundary,
        artifacts: ArtifactBoundary,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if not registry.frozen:
            raise ValueError("工具运行时 需要 已冻结 注册表")
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._limiter = limiter
        self._audit = audit
        self._artifacts = artifacts
        self._token_counter = token_counter
        self._backend_tasks: set[asyncio.Future[ToolResult | None]] = set()

    @property
    def pending_backend_tasks(self) -> int:
        return len(self._backend_tasks)

    async def drain_backend_tasks(self) -> None:
        while self._backend_tasks:
            await asyncio.gather(*tuple(self._backend_tasks), return_exceptions=True)

    async def request_pending_approval(
        self,
        context: ToolExecutionContext,
        binding: ApprovalBinding,
        *,
        reason: str,
        payload: JSONObject,
    ) -> ToolObservation:
        """通过唯一的 ToolRuntime 创建边界生成待处理 Approval。"""
        if (
            binding.workspace_id != context.workspace_id
            or binding.run_id != context.run_id
            or binding.step_id != context.step_id
            or binding.resource_id != context.resource_id
        ):
            return _preflight_error(
                ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                "approval binding conflicts with the durable tool scope",
            )
        try:
            registered = self._registry.resolve_registered(
                ToolName(binding.tool_name), ToolVersion(binding.tool_version)
            )
            definition = registered.definition
            if not definition.requires_approval:
                raise ValueError("审批 目标 不是 高风险 工具")
            decision = await self._policy.authorize(
                PolicyRequest(
                    definition=definition,
                    context=context,
                    tool_input={definition.resource_input_field: binding.resource_id},
                    input_hash=binding.input_hash,
                )
            )
        except Exception:
            return _preflight_error(
                ToolErrorCategory.UNAUTHORIZED,
                "approval target policy evaluation failed",
            )
        if not decision.allowed:
            return _preflight_error(
                ToolErrorCategory.UNAUTHORIZED,
                "approval target policy denied creation",
                details={"reason_code": decision.reason_code},
            )
        creator = cast(ApprovalCreationBoundary, self._approvals)
        try:
            approval = await creator.create(
                ApprovalCreateCommand(
                    binding=binding,
                    reason=reason,
                    payload=payload,
                    requested_by=ApprovalPrincipal(context.principal.type, context.principal.id),
                    source="tool_runtime",
                )
            )
        except Exception:
            return _preflight_error(
                ToolErrorCategory.PERMANENT_FAILURE,
                "approval creation failed",
            )
        if approval.status != "pending":
            return _preflight_error(
                ToolErrorCategory.PERMANENT_FAILURE,
                "approval creation did not return a pending approval",
            )
        return ToolObservation(
            call_id=None,
            status=AuditStatus.PENDING,
            approval_id=approval.approval_id,
        )

    async def execute(self, context: ToolExecutionContext, intent: ToolIntent) -> ToolObservation:
        # 后端调用严格晚于 schema/resource、Policy、Approval、限流与持久化 audit claim。
        try:
            registered = self._registry.resolve_registered(intent.name, intent.version)
            parsed = decode_json_object(intent.raw_input)
            registered.input_schema.validate(parsed)
        except Exception:
            return ToolObservation(
                call_id=None,
                status=AuditStatus.FAILED,
                error=ToolError(
                    category=ToolErrorCategory.INVALID_INPUT,
                    message="工具 输入 无效",
                ),
            )
        definition = registered.definition
        if definition.requires_resource:
            resource_id = parsed.get(definition.resource_input_field)
            if not isinstance(resource_id, str) or (
                definition.resource_type in {"", "document"} and resource_id != context.resource_id
            ):
                return ToolObservation(
                    call_id=None,
                    status=AuditStatus.FAILED,
                    error=ToolError(
                        category=ToolErrorCategory.UNAUTHORIZED,
                        message="工具 资源 与预期不匹配 该 持久化 运行 资源",
                    ),
                )
        input_hash = canonical_json_hash(parsed)
        idempotency_key = stable_tool_idempotency_key(context.step_id, intent.idempotency_key)
        try:
            decision = await self._policy.authorize(
                PolicyRequest(
                    definition=definition,
                    context=context,
                    tool_input=parsed,
                    input_hash=input_hash,
                )
            )
        except Exception:
            return ToolObservation(
                call_id=None,
                status=AuditStatus.FAILED,
                error=ToolError(
                    category=ToolErrorCategory.UNAUTHORIZED,
                    message="工具策略评估失败",
                ),
            )
        if not decision.allowed:
            return ToolObservation(
                call_id=None,
                status=AuditStatus.FAILED,
                error=ToolError(
                    category=ToolErrorCategory.UNAUTHORIZED,
                    message="工具策略拒绝执行",
                    details={"reason_code": decision.reason_code},
                ),
            )
        if definition.requires_approval:
            if intent.approval_id is None or intent.patch_hash is None:
                return _preflight_error(
                    ToolErrorCategory.APPROVAL_REQUIRED,
                    "tool execution requires an external approval",
                )
            requirement = ApprovalRequirement(
                workspace_id=context.workspace_id,
                run_id=context.run_id,
                step_id=context.step_id,
                resource_id=context.resource_id,
                tool_name=definition.name,
                tool_version=definition.version,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                patch_hash=intent.patch_hash,
            )
            try:
                approval = await self._approvals.load_approval(intent.approval_id)
            except Exception:
                return _preflight_error(
                    ToolErrorCategory.UNAUTHORIZED,
                    "tool approval verification failed",
                )
            if approval is None or approval.status != "approved":
                return _preflight_error(
                    ToolErrorCategory.APPROVAL_REQUIRED,
                    "tool execution requires an approved external decision",
                )
            if approval.approval_id != intent.approval_id or not _same_approval_authority(
                approval.requirement, requirement
            ):
                return _preflight_error(
                    ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                    "tool approval binding conflicts with the execution",
                )
        try:
            rate_limit = await self._limiter.check(
                RateLimitRequest(
                    definition=definition,
                    context=context,
                    idempotency_key=idempotency_key,
                )
            )
        except Exception:
            return _preflight_error(
                ToolErrorCategory.PERMANENT_FAILURE,
                "tool rate limit check failed",
            )
        if not rate_limit.allowed:
            return _preflight_error(
                ToolErrorCategory.RATE_LIMITED,
                "tool rate limit is exhausted",
                details={"retry_after_ms": int(rate_limit.retry_after.total_seconds() * 1000)},
            )
        started_at = datetime.now(UTC)
        started_tick = time.monotonic()
        try:
            claim = await self._audit.claim(
                AuditClaimRequest(
                    run_id=context.run_id,
                    step_id=context.step_id,
                    tool_name=definition.name,
                    tool_version=definition.version,
                    idempotency_key=idempotency_key,
                    tool_input=parsed,
                    input_hash=input_hash,
                    attempt=context.attempt,
                    started_at=started_at,
                )
            )
        except IdempotencyConflictError:
            return _preflight_error(
                ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                "tool idempotency identity conflicts with an existing call",
            )
        except Exception:
            return _preflight_error(
                ToolErrorCategory.PERMANENT_FAILURE,
                "tool audit claim failed",
            )
        if not claim.acquired:
            if claim.status is AuditStatus.RUNNING:
                return ToolObservation(
                    call_id=claim.call_id,
                    status=claim.status,
                    error=ToolError(
                        category=ToolErrorCategory.RETRYABLE_UPSTREAM,
                        message="工具调用已在有效领取记录下运行",
                    ),
                    replayed=True,
                )
            return ToolObservation(
                call_id=claim.call_id,
                status=claim.status,
                result=claim.result,
                error=claim.error,
                attempts=claim.attempts,
                latency_ms=claim.latency_ms,
                replayed=True,
            )

        backend = cast(BackendBoundary, definition.backend)
        effective_deadline = min(
            datetime.now(UTC) + definition.timeout,
            context.deadline.astimezone(UTC),
        )
        backend_attempts = 0
        result: ToolResult | None = None
        if claim.recovered:
            # 恢复时先查后端回执；有副作用且无法证明结果时禁止盲目重放。
            backend_request = BackendRequest(
                definition=definition,
                context=context,
                tool_input=parsed,
                input_hash=input_hash,
                idempotency_key=idempotency_key,
                backend_attempt=1,
                recovering=True,
            )
            try:
                result = await self._await_backend(
                    backend.recover(backend_request),
                    effective_deadline,
                )
            except _BackendTimedOut:
                return await self._finish(
                    claim,
                    context,
                    started_tick,
                    result=None,
                    failure=ToolError(
                        category=ToolErrorCategory.TIMEOUT,
                        message="工具后端恢复超时",
                    ),
                    backend_attempts=0,
                )
            except _BackendCancelled:
                return await self._finish(
                    claim,
                    context,
                    started_tick,
                    result=None,
                    failure=ToolError(
                        category=ToolErrorCategory.CANCELLED,
                        message="工具后端恢复已取消",
                    ),
                    backend_attempts=0,
                )
            except Exception:
                result = None
            if result is None and definition.side_effecting:
                failure = ToolError(
                    category=ToolErrorCategory.PERMANENT_FAILURE,
                    message="工具恢复无法证明重放安全",
                )
                return await self._finish(
                    claim,
                    context,
                    started_tick,
                    result=None,
                    failure=failure,
                    backend_attempts=0,
                )
        if result is None:
            # 重试沿用稳定幂等键，并同时受错误分类、次数与截止时间约束。
            while result is None:
                backend_attempts += 1
                backend_request = BackendRequest(
                    definition=definition,
                    context=context,
                    tool_input=parsed,
                    input_hash=input_hash,
                    idempotency_key=idempotency_key,
                    backend_attempt=backend_attempts,
                    recovering=False,
                )
                try:
                    candidate = await self._await_backend(
                        backend.execute(backend_request),
                        effective_deadline,
                    )
                    if candidate is None:
                        return await self._finish(
                            claim,
                            context,
                            started_tick,
                            result=None,
                            failure=ToolError(
                                category=ToolErrorCategory.PERMANENT_FAILURE,
                                message="工具后端未返回结果",
                            ),
                            backend_attempts=backend_attempts,
                        )
                    result = candidate
                except _BackendTimedOut:
                    return await self._finish(
                        claim,
                        context,
                        started_tick,
                        result=None,
                        failure=ToolError(
                            category=ToolErrorCategory.TIMEOUT,
                            message="工具 后端 执行 超时",
                        ),
                        backend_attempts=backend_attempts,
                    )
                except _BackendCancelled:
                    return await self._finish(
                        claim,
                        context,
                        started_tick,
                        result=None,
                        failure=ToolError(
                            category=ToolErrorCategory.CANCELLED,
                            message="工具后端执行已取消",
                        ),
                        backend_attempts=backend_attempts,
                    )
                except ToolBackendFailure as error:
                    if (
                        error.category is ToolErrorCategory.RETRYABLE_UPSTREAM
                        and backend_attempts < definition.max_attempts
                    ):
                        try:
                            await asyncio.sleep(definition.retry_backoff.total_seconds())
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if current is not None:
                                current.uncancel()
                            return await self._finish(
                                claim,
                                context,
                                started_tick,
                                result=None,
                                failure=ToolError(
                                    category=ToolErrorCategory.CANCELLED,
                                    message="工具后端重试已取消",
                                ),
                                backend_attempts=backend_attempts,
                            )
                        continue
                    failure = ToolError(
                        category=error.category,
                        message=error.safe_message,
                    )
                    return await self._finish(
                        claim,
                        context,
                        started_tick,
                        result=None,
                        failure=failure,
                        backend_attempts=backend_attempts,
                    )
                except Exception:
                    failure = ToolError(
                        category=ToolErrorCategory.PERMANENT_FAILURE,
                        message="tool backend execution failed",
                    )
                    return await self._finish(
                        claim,
                        context,
                        started_tick,
                        result=None,
                        failure=failure,
                        backend_attempts=backend_attempts,
                    )
        backend_attempts = max(backend_attempts, 1)
        try:
            registered.output_schema.validate(result.output)
        except ValueError:
            failure = ToolError(
                category=ToolErrorCategory.INVALID_OUTPUT,
                message="工具 后端 输出 无效",
            )
            return await self._finish(
                claim,
                context,
                started_tick,
                result=None,
                failure=failure,
                backend_attempts=backend_attempts,
            )
        bounded_result, bound_error = await self._bound_result(
            definition,
            context,
            idempotency_key,
            result,
        )
        if bound_error is not None:
            return await self._finish(
                claim,
                context,
                started_tick,
                result=None,
                failure=bound_error,
                backend_attempts=backend_attempts,
            )
        return await self._finish(
            claim,
            context,
            started_tick,
            result=bounded_result,
            failure=None,
            backend_attempts=backend_attempts,
        )

    async def _bound_result(
        self,
        definition: ToolDefinition,
        context: ToolExecutionContext,
        idempotency_key: str,
        result: ToolResult,
    ) -> tuple[ToolResult, ToolError | None]:
        content = canonical_json_bytes(result.output)
        token_count: int | None = None
        # 字节数与令牌预算分别约束；超限正文只以哈希绑定的制品保存。
        if definition.max_result_tokens is not None:
            if self._token_counter is None:
                return result, ToolError(
                    category=ToolErrorCategory.PERMANENT_FAILURE,
                    message="工具 令牌 计数器 不可用",
                )
            token_count = self._token_counter.count_json(content)
            if token_count < 0:
                return result, ToolError(
                    category=ToolErrorCategory.PERMANENT_FAILURE,
                    message="工具 令牌 计数器 返回了无效的 结果",
                )
        within_token_limit = definition.max_result_tokens is None or (
            token_count is not None and token_count <= definition.max_result_tokens
        )
        if len(content) <= definition.max_inline_output_bytes and within_token_limit:
            return replace(result, output=decode_json_object(content)), None
        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        try:
            reference = await self._artifacts.persist(
                ArtifactWriteRequest(
                    workspace_id=context.workspace_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    resource_id=context.resource_id,
                    tool_name=definition.name,
                    tool_version=definition.version,
                    idempotency_key=(f"tool-result:{context.run_id}:{idempotency_key}"),
                    content=content,
                    content_hash=content_hash,
                    provenance=result.provenance,
                )
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            return result, ToolError(
                category=ToolErrorCategory.CANCELLED,
                message="工具制品持久化已取消",
            )
        except Exception:
            return result, ToolError(
                category=ToolErrorCategory.PERMANENT_FAILURE,
                message="工具 制品 持久化 失败",
            )
        if (
            reference.workspace_id != context.workspace_id
            or reference.run_id != context.run_id
            or reference.step_id != context.step_id
            or reference.tool_name != definition.name
            or reference.tool_version != definition.version
            or reference.content_hash != content_hash
            or reference.size_bytes != len(content)
        ):
            return result, ToolError(
                category=ToolErrorCategory.PERMANENT_FAILURE,
                message="工具 制品 绑定 无效",
            )
        summary = cast(
            JSONObject,
            {
                "truncated": True,
                "summary": (
                    result.oversize_summary
                    if result.oversize_summary is not None
                    else "tool result stored as artifact"
                ),
                "artifact_id": reference.artifact_id,
                "artifact_uri": reference.uri,
                "content_hash": reference.content_hash,
                "size_bytes": reference.size_bytes,
            },
        )
        if token_count is not None:
            summary["full_result_tokens"] = token_count
        if len(canonical_json_bytes(summary)) > definition.max_summary_bytes:
            return result, ToolError(
                category=ToolErrorCategory.PERMANENT_FAILURE,
                message="工具 制品 摘要 超出范围",
            )
        return (
            ToolResult(output=summary, provenance=result.provenance, artifact=reference),
            None,
        )

    async def _await_backend(
        self,
        awaitable: Awaitable[ToolResult | None],
        deadline: datetime,
    ) -> ToolResult | None:
        future = asyncio.ensure_future(awaitable)
        self._backend_tasks.add(future)
        future.add_done_callback(self._backend_done)
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            future.cancel()
            await asyncio.sleep(0)
            raise _BackendTimedOut
        try:
            done, _ = await asyncio.wait((future,), timeout=remaining)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            future.cancel()
            await asyncio.sleep(0)
            raise _BackendCancelled from error
        if not done:
            future.cancel()
            await asyncio.sleep(0)
            raise _BackendTimedOut
        return future.result()

    def _backend_done(self, future: asyncio.Future[ToolResult | None]) -> None:
        self._backend_tasks.discard(future)
        if future.cancelled():
            return
        _ = future.exception()

    async def _finish(
        self,
        claim: AuditClaim,
        context: ToolExecutionContext,
        started_tick: float,
        *,
        result: ToolResult | None,
        failure: ToolError | None,
        backend_attempts: int,
    ) -> ToolObservation:
        completed_at = datetime.now(UTC)
        latency_ms = max(0, int((time.monotonic() - started_tick) * 1000))
        status = AuditStatus.SUCCEEDED
        if failure is not None:
            status = (
                AuditStatus.CANCELLED
                if failure.category is ToolErrorCategory.CANCELLED
                else AuditStatus.FAILED
            )
        finish = AuditFinishRequest(
            call_id=claim.call_id,
            status=status,
            result=result,
            error=failure,
            attempt=context.attempt,
            backend_attempts=backend_attempts,
            latency_ms=latency_ms,
            completed_at=completed_at,
        )
        try:
            await self._audit.finish(finish)
        except Exception:
            return ToolObservation(
                call_id=claim.call_id,
                status=AuditStatus.FAILED,
                error=ToolError(
                    category=ToolErrorCategory.PERMANENT_FAILURE,
                    message="工具 审计 结果 持久化 失败",
                ),
                attempts=backend_attempts,
                latency_ms=latency_ms,
            )
        return ToolObservation(
            call_id=claim.call_id,
            status=status,
            result=result,
            error=failure,
            attempts=backend_attempts,
            latency_ms=latency_ms,
        )


def stable_tool_idempotency_key(step_id: str, supplied: str) -> str:
    supplied = supplied.strip()
    return supplied if supplied else f"agent-step:{step_id}"


def _same_approval_authority(approved: ApprovalRequirement, execution: ApprovalRequirement) -> bool:
    """request Step 只记录 provenance；后续 CommitPatch Step 才行使权限。"""

    return (
        approved.workspace_id == execution.workspace_id
        and approved.run_id == execution.run_id
        and approved.resource_id == execution.resource_id
        and approved.tool_name == execution.tool_name
        and approved.tool_version == execution.tool_version
        and approved.idempotency_key == execution.idempotency_key
        and approved.input_hash == execution.input_hash
        and approved.patch_hash == execution.patch_hash
    )


class _BackendTimedOut(RuntimeError):
    pass


class _BackendCancelled(RuntimeError):
    pass


def _preflight_error(
    category: ToolErrorCategory,
    message: str,
    *,
    details: dict[str, str | int] | None = None,
) -> ToolObservation:
    return ToolObservation(
        call_id=None,
        status=AuditStatus.FAILED,
        error=ToolError(category=category, message=message, details=details),
    )


__all__ = ["TokenCounter", "ToolRuntime", "stable_tool_idempotency_key"]
