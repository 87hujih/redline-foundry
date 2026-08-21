from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docreview.tool_runtime import (
    ApprovalGrant,
    ApprovalRequirement,
    ArtifactReference,
    ArtifactWriteRequest,
    AuditClaim,
    AuditClaimRequest,
    AuditFinishRequest,
    PolicyDecision,
    Principal,
    RateLimitDecision,
    RateLimitRequest,
    ToolDefinition,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolIntent,
    ToolName,
    ToolRegistry,
    ToolRiskLevel,
    ToolRuntime,
    ToolVersion,
)

INPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "resource_id": {"type": "string", "minLength": 1},
    "mode": {"type": "string", "enum": ["brief", "full"]},
    "count": {"type": "integer", "minimum": 1}
  },
  "required": ["resource_id", "mode", "count"],
  "additionalProperties": false
}"""

OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {"answer": {"type": "string"}},
  "required": ["answer"],
  "additionalProperties": false
}"""


class Dependencies:
    def __init__(self) -> None:
        self.policy_calls = 0
        self.approval_calls = 0
        self.limiter_calls = 0
        self.audit_claims = 0
        self.audit_finishes = 0
        self.backend_calls = 0
        self.artifact_calls = 0

    async def authorize(self, request: object) -> PolicyDecision:
        self.policy_calls += 1
        raise AssertionError("policy was not expected")

    async def load_approval(self, approval_id: str) -> ApprovalGrant | None:
        self.approval_calls += 1
        raise AssertionError("approval was not expected")

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        self.limiter_calls += 1
        raise AssertionError("limiter was not expected")

    async def claim(self, request: AuditClaimRequest) -> AuditClaim:
        self.audit_claims += 1
        raise AssertionError("audit claim was not expected")

    async def finish(self, request: AuditFinishRequest) -> None:
        self.audit_finishes += 1
        raise AssertionError("audit finish was not expected")

    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference:
        self.artifact_calls += 1
        raise AssertionError("artifact persistence was not expected")

    async def execute(self, request: object) -> object:
        self.backend_calls += 1
        raise AssertionError("backend was not expected")

    async def recover(self, request: object) -> object:
        raise AssertionError("backend recovery was not expected")


def execution_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="request-1",
        run_id="run-1",
        step_id="step-1",
        workspace_id="workspace-1",
        resource_id="resource-1",
        principal=Principal(type="user", id="user-1"),
        roles=("owner",),
        trace_id="trace-1",
        attempt=2,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def runtime_with(
    dependencies: Dependencies,
    *,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
    requires_approval: bool = False,
) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=ToolName("document.read_nodes"),
            version=ToolVersion("1.0.0"),
            description="Read immutable document nodes",
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            risk_level=risk_level,
            timeout=timedelta(seconds=1),
            requires_resource=True,
            requires_approval=requires_approval,
            max_inline_output_bytes=1_024,
            backend=dependencies,
        )
    )
    registry.freeze()
    return ToolRuntime(
        registry=registry,
        policy=dependencies,
        approvals=dependencies,
        limiter=dependencies,
        audit=dependencies,
        artifacts=dependencies,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input",
    [
        '{"resource_id":"resource-1","resource_id":"resource-2","mode":"brief","count":1}',
        "[]",
        '{"resource_id":"resource-1","mode":"brief"}',
        '{"resource_id":"resource-1","mode":"brief","count":"1"}',
        '{"resource_id":"resource-1","mode":"other","count":1}',
        '{"resource_id":"resource-1","mode":"brief","count":1,"extra":true}',
    ],
)
async def test_invalid_json_or_schema_input_stops_before_every_side_effect(
    raw_input: str,
) -> None:
    dependencies = Dependencies()
    runtime = runtime_with(dependencies)

    observation = await runtime.execute(
        execution_context(),
        ToolIntent(
            name=ToolName("document.read_nodes"),
            version=ToolVersion("1.0.0"),
            raw_input=raw_input,
        ),
    )

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.INVALID_INPUT
    assert dependencies.policy_calls == 0
    assert dependencies.approval_calls == 0
    assert dependencies.limiter_calls == 0
    assert dependencies.audit_claims == 0
    assert dependencies.backend_calls == 0
    assert dependencies.artifact_calls == 0


class DenyingDependencies(Dependencies):
    async def authorize(self, request: object) -> PolicyDecision:
        self.policy_calls += 1
        return PolicyDecision(allowed=False, reason_code="permission_denied")


@pytest.mark.asyncio
async def test_resource_mismatch_fails_before_policy() -> None:
    dependencies = DenyingDependencies()

    observation = await runtime_with(dependencies).execute(
        execution_context(),
        ToolIntent(
            name=ToolName("document.read_nodes"),
            version=ToolVersion("1.0.0"),
            raw_input='{"resource_id":"resource-2","mode":"brief","count":1}',
        ),
    )

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.UNAUTHORIZED
    assert dependencies.policy_calls == 0
    assert dependencies.limiter_calls == 0
    assert dependencies.audit_claims == 0
    assert dependencies.backend_calls == 0


@pytest.mark.asyncio
async def test_policy_denial_stops_before_approval_rate_limit_audit_and_backend() -> None:
    dependencies = DenyingDependencies()

    observation = await runtime_with(dependencies).execute(
        execution_context(),
        ToolIntent(
            name=ToolName("document.read_nodes"),
            version=ToolVersion("1.0.0"),
            raw_input='{"resource_id":"resource-1","mode":"brief","count":1}',
        ),
    )

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.UNAUTHORIZED
    assert dependencies.policy_calls == 1
    assert dependencies.approval_calls == 0
    assert dependencies.limiter_calls == 0
    assert dependencies.audit_claims == 0
    assert dependencies.backend_calls == 0


class ApprovalDependencies(Dependencies):
    def __init__(self, grant: ApprovalGrant | None = None) -> None:
        super().__init__()
        self.grant = grant

    async def authorize(self, request: object) -> PolicyDecision:
        self.policy_calls += 1
        return PolicyDecision(allowed=True, reason_code="authorized")

    async def load_approval(self, approval_id: str) -> ApprovalGrant | None:
        self.approval_calls += 1
        return self.grant

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        self.limiter_calls += 1
        return RateLimitDecision(allowed=False, retry_after=timedelta(seconds=30))


def approved_requirement(
    *, resource_id: str = "resource-1", step_id: str = "step-1"
) -> ApprovalRequirement:
    return ApprovalRequirement(
        workspace_id="workspace-1",
        run_id="run-1",
        step_id=step_id,
        resource_id=resource_id,
        tool_name=ToolName("document.read_nodes"),
        tool_version=ToolVersion("1.0.0"),
        idempotency_key="agent-step:step-1",
        input_hash="sha256:5669dbb41a27400eae39ddd5095ee96b82ba098c0779ed7ef9377c70b79d11cd",
        patch_hash="sha256:" + "a" * 64,
    )


def high_risk_intent(*, approval_id: str | None = None) -> ToolIntent:
    return ToolIntent(
        name=ToolName("document.read_nodes"),
        version=ToolVersion("1.0.0"),
        raw_input='{"resource_id":"resource-1","mode":"brief","count":1}',
        approval_id=approval_id,
        patch_hash="sha256:" + "a" * 64,
    )


@pytest.mark.asyncio
async def test_high_risk_tool_requires_external_approval() -> None:
    dependencies = ApprovalDependencies()

    observation = await runtime_with(
        dependencies,
        risk_level=ToolRiskLevel.HIGH,
        requires_approval=True,
    ).execute(execution_context(), high_risk_intent())

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.APPROVAL_REQUIRED
    assert dependencies.approval_calls == 0
    assert dependencies.limiter_calls == 0
    assert dependencies.audit_claims == 0


@pytest.mark.asyncio
async def test_matching_approval_reaches_rate_limiter_but_exhaustion_stops_execution() -> None:
    grant = ApprovalGrant(
        approval_id="approval-1",
        status="approved",
        requirement=approved_requirement(),
    )
    dependencies = ApprovalDependencies(grant)

    observation = await runtime_with(
        dependencies,
        risk_level=ToolRiskLevel.HIGH,
        requires_approval=True,
    ).execute(execution_context(), high_risk_intent(approval_id="approval-1"))

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.RATE_LIMITED
    assert observation.error.details == {"retry_after_ms": 30_000}
    assert dependencies.approval_calls == 1
    assert dependencies.limiter_calls == 1
    assert dependencies.audit_claims == 0
    assert dependencies.backend_calls == 0


@pytest.mark.asyncio
async def test_approval_request_step_is_provenance_for_later_commit_step() -> None:
    grant = ApprovalGrant(
        approval_id="approval-1",
        status="approved",
        requirement=approved_requirement(step_id="request-approval-step"),
    )
    dependencies = ApprovalDependencies(grant)

    observation = await runtime_with(
        dependencies,
        risk_level=ToolRiskLevel.HIGH,
        requires_approval=True,
    ).execute(execution_context(), high_risk_intent(approval_id="approval-1"))

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.RATE_LIMITED
    assert dependencies.limiter_calls == 1


@pytest.mark.asyncio
async def test_conflicting_approval_binding_fails_closed() -> None:
    grant = ApprovalGrant(
        approval_id="approval-1",
        status="approved",
        requirement=approved_requirement(resource_id="resource-2"),
    )
    dependencies = ApprovalDependencies(grant)

    observation = await runtime_with(
        dependencies,
        risk_level=ToolRiskLevel.HIGH,
        requires_approval=True,
    ).execute(execution_context(), high_risk_intent(approval_id="approval-1"))

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.IDEMPOTENCY_CONFLICT
    assert dependencies.limiter_calls == 0
    assert dependencies.audit_claims == 0
    assert dependencies.backend_calls == 0
