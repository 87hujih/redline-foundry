"""基于原子递增 repository 边界的固定窗口 rate limit。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from docreview.tool_runtime.models import (
    RateLimitDecision,
    RateLimitRequest,
    ToolName,
    ToolRiskLevel,
    ToolVersion,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    limit: int
    window: timedelta

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("速率 限制 必须为正数")
        if self.window < timedelta(seconds=1) or self.window > timedelta(hours=24):
            raise ValueError("速率限制窗口必须介于 1 秒和 24 小时之间")


@dataclass(frozen=True, slots=True)
class StaticRateLimitRules:
    default: RateLimitRule
    by_tool: dict[tuple[ToolName, ToolVersion], RateLimitRule] = field(
        default_factory=lambda: dict[tuple[ToolName, ToolVersion], RateLimitRule]()
    )
    by_risk: dict[ToolRiskLevel, RateLimitRule] = field(
        default_factory=lambda: dict[ToolRiskLevel, RateLimitRule]()
    )

    def resolve(self, request: RateLimitRequest) -> RateLimitRule:
        tool_rule = self.by_tool.get((request.definition.name, request.definition.version))
        if tool_rule is not None:
            return tool_rule
        return self.by_risk.get(request.definition.risk_level, self.default)


@dataclass(frozen=True, slots=True)
class RateLimitKey:
    workspace_id: str
    principal_type: str
    principal_id: str
    tool_name: ToolName
    tool_version: ToolVersion
    bucket_start: datetime

    def __post_init__(self) -> None:
        if any(
            not value.strip() or value != value.strip()
            for value in (self.workspace_id, self.principal_type, self.principal_id)
        ):
            raise ValueError("速率限制键的可信范围不完整")
        if self.bucket_start.tzinfo is None or self.bucket_start.utcoffset() is None:
            raise ValueError("速率 限制 桶 必须包含时区信息")


class RateLimitRepository(Protocol):
    async def increment(self, key: RateLimitKey, limit: int, now: datetime) -> int | None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        repository: RateLimitRepository,
        rules: StaticRateLimitRules,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._rules = rules
        self._clock = clock or SystemClock()

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        rule = self._rules.resolve(request)
        current = self._clock.now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("rate limit clock must return a timezone-aware datetime")
        now = current.astimezone(UTC)
        bucket_start = fixed_window_start(now, rule.window)
        key = RateLimitKey(
            workspace_id=request.context.workspace_id,
            principal_type=request.context.principal.type,
            principal_id=request.context.principal.id,
            tool_name=request.definition.name,
            tool_version=request.definition.version,
            bucket_start=bucket_start,
        )
        count = await self._repository.increment(key, rule.limit, now)
        if count is None:
            return RateLimitDecision(
                allowed=False,
                retry_after=bucket_start + rule.window - now,
            )
        if count < 1 or count > rule.limit:
            raise RuntimeError("速率 限制 仓库 返回了无效的 数量")
        return RateLimitDecision(allowed=True)


def fixed_window_start(now: datetime, window: timedelta) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("rate limit clock must return a timezone-aware datetime")
    window_microseconds = window // timedelta(microseconds=1)
    elapsed_microseconds = (now.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)
    bucket_microseconds = (elapsed_microseconds // window_microseconds) * window_microseconds
    return _EPOCH + timedelta(microseconds=bucket_microseconds)


__all__ = [
    "Clock",
    "FixedWindowRateLimiter",
    "RateLimitKey",
    "RateLimitRepository",
    "RateLimitRule",
    "StaticRateLimitRules",
    "SystemClock",
    "fixed_window_start",
]
