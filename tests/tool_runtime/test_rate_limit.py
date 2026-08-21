from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from docreview.tool_runtime import (
    FixedWindowRateLimiter,
    Principal,
    RateLimitKey,
    RateLimitRequest,
    RateLimitRule,
    StaticRateLimitRules,
    ToolDefinition,
    ToolExecutionContext,
    ToolName,
    ToolRiskLevel,
    ToolVersion,
)
from docreview.tool_runtime.postgres import (
    FIXED_WINDOW_INCREMENT_SQL,
    PostgresRateLimitRepository,
)


class Backend:
    async def execute(self, request: object) -> object:
        raise AssertionError("unused")

    async def recover(self, request: object) -> object:
        raise AssertionError("unused")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class Repository:
    def __init__(self) -> None:
        self.counts: dict[RateLimitKey, int] = {}

    async def increment(self, key: RateLimitKey, limit: int, now: datetime) -> int | None:
        current = self.counts.get(key, 0)
        if current >= limit:
            return None
        current += 1
        self.counts[key] = current
        return current


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
        attempt=1,
        deadline=datetime(2026, 8, 15, 13, 0, tzinfo=UTC),
    )


def definition() -> ToolDefinition:
    return ToolDefinition(
        name=ToolName("retrieval.search"),
        version=ToolVersion("2.0.0"),
        description="Search durable evidence",
        input_schema='{"type":"object","additionalProperties":false}',
        output_schema='{"type":"object","additionalProperties":false}',
        risk_level=ToolRiskLevel.MEDIUM,
        timeout=timedelta(seconds=1),
        requires_resource=False,
        requires_approval=False,
        max_inline_output_bytes=1_024,
        backend=Backend(),
    )


@pytest.mark.asyncio
async def test_fixed_window_uses_trusted_scope_and_risk_limit_for_every_retry() -> None:
    repository = Repository()
    limiter = FixedWindowRateLimiter(
        repository=repository,
        rules=StaticRateLimitRules(
            by_risk={ToolRiskLevel.MEDIUM: RateLimitRule(2, timedelta(minutes=1))},
            default=RateLimitRule(60, timedelta(minutes=1)),
        ),
        clock=Clock(datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)),
    )
    request = RateLimitRequest(
        definition=definition(),
        context=context(),
        idempotency_key="agent-step:step-1",
    )

    assert (await limiter.check(request)).allowed is True
    assert (await limiter.check(request)).allowed is True
    exhausted = await limiter.check(request)

    assert exhausted.allowed is False
    assert exhausted.retry_after == timedelta(seconds=4)
    assert tuple(repository.counts) == (
        RateLimitKey(
            workspace_id="workspace-1",
            principal_type="user",
            principal_id="user-1",
            tool_name=ToolName("retrieval.search"),
            tool_version=ToolVersion("2.0.0"),
            bucket_start=datetime(2026, 8, 15, 12, 34, tzinfo=UTC),
        ),
    )


@pytest.mark.asyncio
async def test_fixed_window_rejects_a_naive_injected_clock() -> None:
    limiter = FixedWindowRateLimiter(
        repository=Repository(),
        rules=StaticRateLimitRules(default=RateLimitRule(1, timedelta(minutes=1))),
        clock=Clock(datetime(2026, 8, 15, 12, 34, 56)),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await limiter.check(
            RateLimitRequest(
                definition=definition(),
                context=context(),
                idempotency_key="agent-step:step-1",
            )
        )


class Cursor:
    async def fetchone(self) -> tuple[object, ...] | None:
        return (1,)


class Connection:
    def __init__(self) -> None:
        self.query = ""
        self.params: tuple[object, ...] = ()

    async def execute(self, query: str, params: tuple[object, ...]) -> Cursor:
        self.query = query
        self.params = params
        return Cursor()


@pytest.mark.asyncio
async def test_postgres_rate_limit_repository_uses_one_parameterized_atomic_statement() -> None:
    connection = Connection()
    repository = PostgresRateLimitRepository(connection)
    key = RateLimitKey(
        workspace_id="workspace-1",
        principal_type="user",
        principal_id="user-1",
        tool_name=ToolName("retrieval.search"),
        tool_version=ToolVersion("2.0.0"),
        bucket_start=datetime(2026, 8, 15, 12, 34, tzinfo=UTC),
    )

    observed_at = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)
    assert await repository.increment(key, 2, observed_at) == 1
    assert connection.query == FIXED_WINDOW_INCREMENT_SQL
    assert connection.params == (
        "workspace-1",
        "user",
        "user-1",
        "retrieval.search",
        "2.0.0",
        datetime(2026, 8, 15, 12, 34, tzinfo=UTC),
        observed_at,
        2,
    )
    assert "%s" in FIXED_WINDOW_INCREMENT_SQL
    assert "%s::uuid, %s::text, %s::uuid" in FIXED_WINDOW_INCREMENT_SQL
    assert "%s::timestamptz, %s::timestamptz, %s::integer" in FIXED_WINDOW_INCREMENT_SQL
    assert "ON CONFLICT" in FIXED_WINDOW_INCREMENT_SQL
    assert "call_count < (SELECT limit_value FROM rate_input)" in FIXED_WINDOW_INCREMENT_SQL
