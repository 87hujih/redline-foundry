"""ToolRuntime 的显式 fail-closed 生产依赖装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from docreview.tool_runtime.executor import RuntimeToolExecutor, ScopeStore
from docreview.tool_runtime.models import ToolDefinition
from docreview.tool_runtime.rate_limit import (
    Clock,
    FixedWindowRateLimiter,
    RateLimitRepository,
    StaticRateLimitRules,
)
from docreview.tool_runtime.registry import ToolRegistry
from docreview.tool_runtime.runtime import (
    ApprovalBoundary,
    ArtifactBoundary,
    AuditBoundary,
    PolicyBoundary,
    TokenCounter,
    ToolRuntime,
)


@dataclass(frozen=True, slots=True)
class ProductionToolRuntimeDependencies:
    active_definitions: tuple[ToolDefinition, ...]
    policy: PolicyBoundary | None
    approvals: ApprovalBoundary | None
    audit: AuditBoundary | None
    rate_limit_repository: RateLimitRepository | None
    rate_limit_rules: StaticRateLimitRules | None
    artifacts: ArtifactBoundary | None
    scopes: ScopeStore | None
    token_counter: TokenCounter | None = None
    clock: Clock | None = None


@dataclass(frozen=True, slots=True)
class ProductionToolRuntimeAssembly:
    registry: ToolRegistry
    limiter: FixedWindowRateLimiter
    runtime: ToolRuntime
    executor: RuntimeToolExecutor


def build_production_tool_runtime(
    dependencies: ProductionToolRuntimeDependencies,
) -> ProductionToolRuntimeAssembly:
    if not dependencies.active_definitions:
        raise ValueError("at least one explicit active tool definition is required")
    required = {
        "policy": dependencies.policy,
        "approvals": dependencies.approvals,
        "audit": dependencies.audit,
        "rate_limit_repository": dependencies.rate_limit_repository,
        "rate_limit_rules": dependencies.rate_limit_rules,
        "artifacts": dependencies.artifacts,
        "scopes": dependencies.scopes,
        "token_counter": dependencies.token_counter,
    }
    missing = next((name for name, value in required.items() if value is None), None)
    if missing is not None:
        raise ValueError(f"生产环境 工具运行时 依赖{missing}为必填项")
    policy = cast(PolicyBoundary, dependencies.policy)
    approvals = cast(ApprovalBoundary, dependencies.approvals)
    audit = cast(AuditBoundary, dependencies.audit)
    rate_limit_repository = cast(RateLimitRepository, dependencies.rate_limit_repository)
    rate_limit_rules = cast(StaticRateLimitRules, dependencies.rate_limit_rules)
    artifacts = cast(ArtifactBoundary, dependencies.artifacts)
    scopes = cast(ScopeStore, dependencies.scopes)
    token_counter = cast(TokenCounter, dependencies.token_counter)

    registry = ToolRegistry()
    for definition in dependencies.active_definitions:
        registry.register(definition)
    registry.freeze()
    limiter = FixedWindowRateLimiter(
        repository=rate_limit_repository,
        rules=rate_limit_rules,
        clock=dependencies.clock,
    )
    runtime = ToolRuntime(
        registry=registry,
        policy=policy,
        approvals=approvals,
        limiter=limiter,
        audit=audit,
        artifacts=artifacts,
        token_counter=token_counter,
    )
    executor = RuntimeToolExecutor(runtime=runtime, scopes=scopes)
    return ProductionToolRuntimeAssembly(
        registry=registry,
        limiter=limiter,
        runtime=runtime,
        executor=executor,
    )


__all__ = [
    "ProductionToolRuntimeAssembly",
    "ProductionToolRuntimeDependencies",
    "build_production_tool_runtime",
]
