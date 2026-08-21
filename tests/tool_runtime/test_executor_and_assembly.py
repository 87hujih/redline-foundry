from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docreview.tool_runtime import (
    ArtifactReference,
    ArtifactWriteRequest,
    AuditClaim,
    AuditClaimRequest,
    AuditFinishRequest,
    AuditStatus,
    BackendRequest,
    PolicyDecision,
    Principal,
    ProductionToolRuntimeDependencies,
    Provenance,
    RateLimitKey,
    RateLimitRule,
    RuntimeToolExecutor,
    StaticRateLimitRules,
    ToolDefinition,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolIntent,
    ToolName,
    ToolObservation,
    ToolResult,
    ToolRiskLevel,
    ToolVersion,
    TrustedToolScope,
    build_production_tool_runtime,
)
from docreview.tool_runtime.token_counter import JSONTokenCounter


class CapturingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolExecutionContext, ToolIntent]] = []

    async def execute(self, context: ToolExecutionContext, intent: ToolIntent) -> ToolObservation:
        self.calls.append((context, intent))
        return ToolObservation(call_id="call-1", status=AuditStatus.SUCCEEDED)


class ScopeStore:
    def __init__(self, scope: TrustedToolScope) -> None:
        self.scope = scope
        self.requests: list[tuple[str, str]] = []

    async def load_tool_scope(self, run_id: str, step_id: str) -> TrustedToolScope:
        self.requests.append((run_id, step_id))
        return self.scope


def context(*, run_id: str = "run-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id=run_id,
        step_id="step-1",
        workspace_id="workspace-1",
        resource_id="resource-1",
        principal=Principal(type="user", id="user-1"),
        roles=("owner",),
        trace_id="trace-1",
        attempt=1,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def intent() -> ToolIntent:
    return ToolIntent(
        name=ToolName("document.read_nodes"),
        version=ToolVersion("1.0.0"),
        raw_input='{"resource_id":"resource-1"}',
    )


@pytest.mark.asyncio
async def test_runtime_tool_executor_uses_only_the_durable_scope() -> None:
    runtime = CapturingRuntime()
    trusted_context = context()
    scopes = ScopeStore(
        TrustedToolScope(context=trusted_context, resource_workspace_id="workspace-1")
    )
    executor = RuntimeToolExecutor(runtime=runtime, scopes=scopes)

    observation = await executor.execute(intent(), run_id="run-1", step_id="step-1")

    assert observation.status is AuditStatus.SUCCEEDED
    assert scopes.requests == [("run-1", "step-1")]
    assert runtime.calls == [(trusted_context, intent())]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        TrustedToolScope(context=context(run_id="other-run"), resource_workspace_id="workspace-1"),
        TrustedToolScope(context=context(), resource_workspace_id="workspace-2"),
    ],
)
async def test_runtime_tool_executor_rejects_run_or_workspace_mismatch(
    scope: TrustedToolScope,
) -> None:
    runtime = CapturingRuntime()
    executor = RuntimeToolExecutor(runtime=runtime, scopes=ScopeStore(scope))

    observation = await executor.execute(intent(), run_id="run-1", step_id="step-1")

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.UNAUTHORIZED
    assert runtime.calls == []


class ProductionFakes:
    async def execute(self, request: BackendRequest) -> ToolResult:
        return ToolResult(
            output={"answer": "ok"},
            provenance=(Provenance(source_type="test", source_id="fake", trust_level="trusted"),),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        return None

    async def authorize(self, request: object) -> PolicyDecision:
        return PolicyDecision(allowed=True, reason_code="test")

    async def load_approval(self, approval_id: str) -> None:
        return None

    async def claim(self, request: AuditClaimRequest) -> AuditClaim:
        return AuditClaim(
            call_id="call-1",
            acquired=True,
            recovered=False,
            status=AuditStatus.RUNNING,
        )

    async def finish(self, request: AuditFinishRequest) -> None:
        return None

    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference:
        raise AssertionError("artifact persistence was not expected")

    async def increment(self, key: RateLimitKey, limit: int, now: datetime) -> int | None:
        return 1

    async def load_tool_scope(self, run_id: str, step_id: str) -> TrustedToolScope:
        return TrustedToolScope(context=context(), resource_workspace_id="workspace-1")


def definition(backend: object) -> ToolDefinition:
    return ToolDefinition(
        name=ToolName("document.read_nodes"),
        version=ToolVersion("1.0.0"),
        description="Read document nodes",
        input_schema="""{
          "type":"object",
          "properties":{"resource_id":{"type":"string"}},
          "required":["resource_id"],
          "additionalProperties":false
        }""",
        output_schema="""{
          "type":"object",
          "properties":{"answer":{"type":"string"}},
          "required":["answer"],
          "additionalProperties":false
        }""",
        risk_level=ToolRiskLevel.LOW,
        timeout=timedelta(seconds=1),
        requires_resource=True,
        requires_approval=False,
        max_inline_output_bytes=1_024,
        backend=backend,
    )


def production_dependencies() -> ProductionToolRuntimeDependencies:
    fakes = ProductionFakes()
    return ProductionToolRuntimeDependencies(
        active_definitions=(definition(fakes),),
        policy=fakes,
        approvals=fakes,
        audit=fakes,
        rate_limit_repository=fakes,
        rate_limit_rules=StaticRateLimitRules(
            default=RateLimitRule(limit=60, window=timedelta(minutes=1))
        ),
        artifacts=fakes,
        scopes=fakes,
        token_counter=JSONTokenCounter(),
    )


def test_production_assembly_registers_and_freezes_explicit_active_tools() -> None:
    assembly = build_production_tool_runtime(production_dependencies())

    assert (
        assembly.registry.resolve(ToolName("document.read_nodes"), ToolVersion("1.0.0")).description
        == "Read document nodes"
    )
    with pytest.raises(RuntimeError, match="frozen"):
        assembly.registry.register(definition(ProductionFakes()))


def test_production_assembly_fails_closed_without_active_tools_or_required_dependency() -> None:
    dependencies = production_dependencies()
    with pytest.raises(ValueError, match="active tool"):
        build_production_tool_runtime(
            ProductionToolRuntimeDependencies(
                active_definitions=(),
                policy=dependencies.policy,
                approvals=dependencies.approvals,
                audit=dependencies.audit,
                rate_limit_repository=dependencies.rate_limit_repository,
                rate_limit_rules=dependencies.rate_limit_rules,
                artifacts=dependencies.artifacts,
                scopes=dependencies.scopes,
                token_counter=dependencies.token_counter,
            )
        )

    object.__setattr__(dependencies, "audit", None)
    with pytest.raises(ValueError, match="audit"):
        build_production_tool_runtime(dependencies)


def test_production_assembly_rejects_a_backend_without_the_execution_contract() -> None:
    dependencies = production_dependencies()
    object.__setattr__(dependencies, "active_definitions", (definition(object()),))

    with pytest.raises(ValueError, match="backend"):
        build_production_tool_runtime(dependencies)
