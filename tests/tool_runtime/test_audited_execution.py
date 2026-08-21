from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from docreview.tool_runtime import (
    ApprovalGrant,
    ArtifactReference,
    ArtifactWriteRequest,
    AuditClaim,
    AuditClaimRequest,
    AuditFinishRequest,
    AuditStatus,
    BackendRequest,
    IdempotencyConflictError,
    PolicyDecision,
    Principal,
    Provenance,
    RateLimitDecision,
    RateLimitRequest,
    TokenCounter,
    ToolBackendFailure,
    ToolDefinition,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolIntent,
    ToolName,
    ToolRegistry,
    ToolResult,
    ToolRiskLevel,
    ToolRuntime,
    ToolVersion,
)

INPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "resource_id": {"type": "string"},
    "count": {"type": "integer"}
  },
  "required": ["resource_id", "count"],
  "additionalProperties": false
}"""

OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {"answer": {"type": "string"}},
  "required": ["answer"],
  "additionalProperties": false
}"""


class InMemoryAudit:
    def __init__(self, *, recovered: bool = False) -> None:
        self.identity: tuple[str, str, ToolName, ToolVersion, str] | None = None
        self.input_hash = ""
        self.finished: AuditFinishRequest | None = None
        self.claim_requests: list[AuditClaimRequest] = []
        self.recovered = recovered

    async def claim(self, request: AuditClaimRequest) -> AuditClaim:
        self.claim_requests.append(request)
        identity = (
            request.run_id,
            request.step_id,
            request.tool_name,
            request.tool_version,
            request.idempotency_key,
        )
        if self.identity is None:
            self.identity = identity
            self.input_hash = request.input_hash
            return AuditClaim(
                call_id="call-1",
                acquired=True,
                recovered=self.recovered,
                status=AuditStatus.RUNNING,
            )
        if self.identity != identity or self.input_hash != request.input_hash:
            raise IdempotencyConflictError("conflicting audit identity")
        if self.finished is None:
            return AuditClaim(
                call_id="call-1",
                acquired=False,
                recovered=False,
                status=AuditStatus.RUNNING,
            )
        return AuditClaim(
            call_id="call-1",
            acquired=False,
            recovered=False,
            status=self.finished.status,
            result=self.finished.result,
            error=self.finished.error,
            attempts=self.finished.backend_attempts,
            latency_ms=self.finished.latency_ms,
        )

    async def finish(self, request: AuditFinishRequest) -> None:
        self.finished = request


class Dependencies:
    def __init__(self) -> None:
        self.audit = InMemoryAudit()
        self.backend_requests: list[BackendRequest] = []
        self.artifact_calls = 0

    async def authorize(self, request: object) -> PolicyDecision:
        return PolicyDecision(allowed=True, reason_code="authorized")

    async def load_approval(self, approval_id: str) -> ApprovalGrant | None:
        raise AssertionError("approval was not expected")

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)

    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        return ToolResult(
            output={"answer": "bounded"},
            provenance=(
                Provenance(
                    source_type="document_node",
                    source_id="node-1",
                    resource_id="resource-1",
                    trust_level="untrusted",
                ),
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        raise AssertionError("recovery was not expected")

    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference:
        self.artifact_calls += 1
        raise AssertionError("small output must not become an artifact")


class RetryingDependencies(Dependencies):
    def __init__(self, *, permanent: bool = False) -> None:
        super().__init__()
        self.permanent = permanent

    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        if len(self.backend_requests) == 1:
            raise ToolBackendFailure(
                ToolErrorCategory.PERMANENT_FAILURE
                if self.permanent
                else ToolErrorCategory.RETRYABLE_UPSTREAM,
                "provider unavailable",
            )
        return ToolResult(
            output={"answer": "retried"},
            provenance=(
                Provenance(
                    source_type="document_node", source_id="node-1", trust_level="untrusted"
                ),
            ),
        )


def context() -> ToolExecutionContext:
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
    max_attempts: int = 1,
    max_inline_output_bytes: int = 1_024,
    timeout: timedelta = timedelta(seconds=1),
    side_effecting: bool = False,
    max_result_tokens: int | None = None,
    token_counter: TokenCounter | None = None,
) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=ToolName("document.read_nodes"),
            version=ToolVersion("1.0.0"),
            description="Read document nodes",
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            risk_level=ToolRiskLevel.LOW,
            timeout=timeout,
            requires_resource=True,
            requires_approval=False,
            max_inline_output_bytes=max_inline_output_bytes,
            backend=dependencies,
            max_attempts=max_attempts,
            retry_backoff=timedelta(milliseconds=1) if max_attempts > 1 else timedelta(0),
            side_effecting=side_effecting,
            max_result_tokens=max_result_tokens,
        )
    )
    registry.freeze()
    return ToolRuntime(
        registry=registry,
        policy=dependencies,
        approvals=dependencies,
        limiter=dependencies,
        audit=dependencies.audit,
        artifacts=dependencies,
        token_counter=token_counter,
    )


def intent(count: int = 1) -> ToolIntent:
    return ToolIntent(
        name=ToolName("document.read_nodes"),
        version=ToolVersion("1.0.0"),
        raw_input=f'{{"resource_id":"resource-1","count":{count}}}',
    )


@pytest.mark.asyncio
async def test_success_is_audited_and_identical_replay_does_not_repeat_backend() -> None:
    dependencies = Dependencies()
    runtime = runtime_with(dependencies)

    first = await runtime.execute(context(), intent())
    replay = await runtime.execute(context(), intent())

    assert first.status is AuditStatus.SUCCEEDED
    assert first.result is not None
    assert first.result.output == {"answer": "bounded"}
    assert replay == first.__class__(
        call_id="call-1",
        status=AuditStatus.SUCCEEDED,
        result=first.result,
        attempts=1,
        latency_ms=first.latency_ms,
        replayed=True,
    )
    assert len(dependencies.backend_requests) == 1
    backend_request = dependencies.backend_requests[0]
    assert backend_request.idempotency_key == "agent-step:step-1"
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.attempt == 2
    assert dependencies.audit.finished.backend_attempts == 1
    assert dependencies.audit.finished.result == first.result
    assert dependencies.audit.claim_requests[0].tool_input == {
        "resource_id": "resource-1",
        "count": 1,
    }


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_input_conflicts() -> None:
    dependencies = Dependencies()
    runtime = runtime_with(dependencies)
    await runtime.execute(context(), intent())

    conflict = await runtime.execute(context(), intent(count=2))

    assert conflict.error is not None
    assert conflict.error.category is ToolErrorCategory.IDEMPOTENCY_CONFLICT
    assert len(dependencies.backend_requests) == 1


@pytest.mark.asyncio
async def test_retryable_backend_failure_retries_with_the_same_business_identity() -> None:
    dependencies = RetryingDependencies()

    observation = await runtime_with(dependencies, max_attempts=2).execute(context(), intent())

    assert observation.status is AuditStatus.SUCCEEDED
    assert observation.result is not None
    assert observation.result.output == {"answer": "retried"}
    assert len(dependencies.backend_requests) == 2
    assert [request.idempotency_key for request in dependencies.backend_requests] == [
        "agent-step:step-1",
        "agent-step:step-1",
    ]
    assert [request.backend_attempt for request in dependencies.backend_requests] == [1, 2]
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.backend_attempts == 2


@pytest.mark.asyncio
async def test_permanent_backend_failure_is_not_retried() -> None:
    dependencies = RetryingDependencies(permanent=True)

    observation = await runtime_with(dependencies, max_attempts=3).execute(context(), intent())

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.PERMANENT_FAILURE
    assert len(dependencies.backend_requests) == 1
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.status is AuditStatus.FAILED


class InvalidOutputDependencies(Dependencies):
    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        return ToolResult(
            output={"wrong": "shape"},
            provenance=(
                Provenance(
                    source_type="document_node", source_id="node-1", trust_level="untrusted"
                ),
            ),
        )


class ArtifactDependencies(Dependencies):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.artifact_request: ArtifactWriteRequest | None = None

    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        return ToolResult(
            output={"answer": "x" * 128},
            provenance=(
                Provenance(
                    source_type="document_node", source_id="node-1", trust_level="untrusted"
                ),
            ),
        )

    async def persist(self, request: ArtifactWriteRequest) -> ArtifactReference:
        self.artifact_calls += 1
        self.artifact_request = request
        if self.fail:
            raise RuntimeError("artifact store unavailable")
        return ArtifactReference(
            artifact_id="artifact-1",
            uri="artifact://artifact-1",
            content_hash=request.content_hash,
            size_bytes=len(request.content),
            workspace_id="workspace-1",
            run_id="run-1",
            step_id="step-1",
            tool_name=ToolName("document.read_nodes"),
            tool_version=ToolVersion("1.0.0"),
        )


@pytest.mark.asyncio
async def test_output_schema_failure_is_audited_without_success() -> None:
    dependencies = InvalidOutputDependencies()

    observation = await runtime_with(dependencies).execute(context(), intent())

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.INVALID_OUTPUT
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.status is AuditStatus.FAILED


@pytest.mark.asyncio
async def test_oversized_output_is_bound_to_an_artifact_and_returns_a_bounded_summary() -> None:
    dependencies = ArtifactDependencies()

    observation = await runtime_with(dependencies, max_inline_output_bytes=32).execute(
        context(), intent()
    )

    assert observation.status is AuditStatus.SUCCEEDED
    assert observation.result is not None
    assert observation.result.artifact is not None
    assert observation.result.artifact.artifact_id == "artifact-1"
    assert observation.result.output["truncated"] is True
    assert dependencies.artifact_calls == 1
    assert dependencies.artifact_request is not None
    assert dependencies.artifact_request.idempotency_key == "tool-result:run-1:agent-step:step-1"
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.result == observation.result


@pytest.mark.asyncio
async def test_artifact_persistence_failure_is_not_reported_as_success() -> None:
    dependencies = ArtifactDependencies(fail=True)

    observation = await runtime_with(dependencies, max_inline_output_bytes=32).execute(
        context(), intent()
    )

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.PERMANENT_FAILURE
    assert observation.status is AuditStatus.FAILED


@pytest.mark.asyncio
async def test_token_budget_artifactizes_output_before_byte_safety_limit() -> None:
    class FixedTokenCounter:
        def count_json(self, content: bytes) -> int:
            return 1801

    dependencies = ArtifactDependencies()

    observation = await runtime_with(
        dependencies,
        max_inline_output_bytes=10_000,
        max_result_tokens=1800,
        token_counter=FixedTokenCounter(),
    ).execute(context(), intent())

    assert observation.result is not None
    assert observation.result.artifact is not None
    assert observation.result.output["truncated"] is True
    assert observation.result.output["full_result_tokens"] == 1801
    assert observation.result.output["summary"] == "tool result stored as artifact"


class IgnoringCancellationDependencies(Dependencies):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        return ToolResult(
            output={"answer": "late"},
            provenance=(Provenance(source_type="test", source_id="late", trust_level="trusted"),),
        )


@pytest.mark.asyncio
async def test_timeout_stops_waiting_and_keeps_cancellation_ignoring_backend_tracked() -> None:
    dependencies = IgnoringCancellationDependencies()
    runtime = runtime_with(dependencies, timeout=timedelta(milliseconds=10))

    observation = await runtime.execute(context(), intent())

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.TIMEOUT
    assert dependencies.cancelled.is_set()
    assert runtime.pending_backend_tasks == 1
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.status is AuditStatus.FAILED
    dependencies.release.set()
    await runtime.drain_backend_tasks()
    assert runtime.pending_backend_tasks == 0


@pytest.mark.asyncio
async def test_caller_cancellation_is_forwarded_and_audited_as_cancelled() -> None:
    dependencies = IgnoringCancellationDependencies()
    runtime = runtime_with(dependencies, timeout=timedelta(seconds=5))
    execution = asyncio.create_task(runtime.execute(context(), intent()))
    await dependencies.started.wait()

    execution.cancel()
    observation = await execution

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.CANCELLED
    assert dependencies.cancelled.is_set()
    assert dependencies.audit.finished is not None
    assert dependencies.audit.finished.status is AuditStatus.CANCELLED
    dependencies.release.set()
    await runtime.drain_backend_tasks()


class RecoveryDependencies(Dependencies):
    def __init__(self, result: ToolResult | None) -> None:
        super().__init__()
        self.audit = InMemoryAudit(recovered=True)
        self.recovery_result = result
        self.recovery_calls = 0

    async def execute(self, request: BackendRequest) -> ToolResult:
        self.backend_requests.append(request)
        raise AssertionError("a recovered side effect must not execute again")

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        self.recovery_calls += 1
        return self.recovery_result


@pytest.mark.asyncio
async def test_recovered_side_effect_uses_backend_receipt_without_reexecution() -> None:
    result = ToolResult(
        output={"answer": "recovered"},
        provenance=(
            Provenance(source_type="receipt", source_id="receipt-1", trust_level="trusted"),
        ),
    )
    dependencies = RecoveryDependencies(result)

    observation = await runtime_with(dependencies, side_effecting=True).execute(context(), intent())

    assert observation.status is AuditStatus.SUCCEEDED
    assert observation.result == result
    assert dependencies.recovery_calls == 1
    assert dependencies.backend_requests == []


@pytest.mark.asyncio
async def test_recovered_side_effect_without_receipt_fails_without_reexecution() -> None:
    dependencies = RecoveryDependencies(None)

    observation = await runtime_with(dependencies, side_effecting=True).execute(context(), intent())

    assert observation.error is not None
    assert observation.error.category is ToolErrorCategory.PERMANENT_FAILURE
    assert dependencies.recovery_calls == 1
    assert dependencies.backend_requests == []


@pytest.mark.asyncio
async def test_backend_exception_text_is_not_logged_or_exposed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SecretFailure(Dependencies):
        async def execute(self, request: BackendRequest) -> ToolResult:
            raise RuntimeError("token=production-secret")

    observation = await runtime_with(SecretFailure()).execute(context(), intent())

    assert observation.error is not None
    assert observation.error.message == "tool backend execution failed"
    assert "production-secret" not in caplog.text
    assert "production-secret" not in repr(observation)


@pytest.mark.asyncio
async def test_artifact_selector_is_authorized_without_comparing_it_to_document_resource() -> None:
    class ArtifactReadDependencies(Dependencies):
        async def execute(self, request: BackendRequest) -> ToolResult:
            self.backend_requests.append(request)
            return ToolResult(
                output={"artifact": {"id": "artifact-1"}},
                provenance=(
                    Provenance(
                        source_type="artifact",
                        source_id="artifact-1",
                        resource_id="resource-1",
                        trust_level="untrusted",
                    ),
                ),
            )

    dependencies = ArtifactReadDependencies()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=ToolName("artifact.read"),
            version=ToolVersion("1.0.0"),
            description="Read a bounded artifact by immutable ID",
            input_schema='{"type":"object","properties":{"artifact_id":{"type":"string"}},"required":["artifact_id"],"additionalProperties":false}',
            output_schema='{"type":"object","properties":{"artifact":{"type":"object"}},"required":["artifact"],"additionalProperties":false}',
            risk_level=ToolRiskLevel.LOW,
            timeout=timedelta(seconds=1),
            requires_resource=True,
            resource_input_field="artifact_id",
            resource_type="artifact",
            requires_approval=False,
            max_inline_output_bytes=1_024,
            backend=dependencies,
        )
    )
    registry.freeze()
    runtime = ToolRuntime(
        registry=registry,
        policy=dependencies,
        approvals=dependencies,
        limiter=dependencies,
        audit=dependencies.audit,
        artifacts=dependencies,
    )

    observation = await runtime.execute(
        context(),
        ToolIntent(
            name=ToolName("artifact.read"),
            version=ToolVersion("1.0.0"),
            raw_input='{"artifact_id":"artifact-1"}',
        ),
    )

    assert observation.status is AuditStatus.SUCCEEDED
    assert len(dependencies.backend_requests) == 1
