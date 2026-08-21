"""编排与 ToolRuntime 之间唯一的持久化 scope 适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from docreview.tool_runtime.models import (
    AuditStatus,
    ToolError,
    ToolErrorCategory,
    ToolExecutionContext,
    ToolIntent,
    ToolObservation,
)


@dataclass(frozen=True, slots=True)
class TrustedToolScope:
    context: ToolExecutionContext
    resource_workspace_id: str

    def __post_init__(self) -> None:
        if (
            not self.resource_workspace_id.strip()
            or self.resource_workspace_id != self.resource_workspace_id.strip()
        ):
            raise ValueError("可信 资源 工作区 为必填项")


class ScopeStore(Protocol):
    async def load_tool_scope(self, run_id: str, step_id: str) -> TrustedToolScope: ...


class RuntimeBoundary(Protocol):
    async def execute(
        self, context: ToolExecutionContext, intent: ToolIntent
    ) -> ToolObservation: ...


class RuntimeToolExecutor:
    def __init__(self, *, runtime: RuntimeBoundary, scopes: ScopeStore) -> None:
        self._runtime = runtime
        self._scopes = scopes

    async def execute(self, intent: ToolIntent, *, run_id: str, step_id: str) -> ToolObservation:
        if not run_id.strip() or not step_id.strip():
            return _executor_error("必须提供工具运行和步骤标识")
        try:
            scope = await self._scopes.load_tool_scope(run_id, step_id)
        except Exception:
            return _executor_error("无法加载可信工具范围")
        if (
            scope.context.run_id != run_id
            or scope.context.step_id != step_id
            or scope.resource_workspace_id != scope.context.workspace_id
        ):
            return _executor_error("可信工具范围与声明的执行范围不匹配")
        try:
            return await self._runtime.execute(scope.context, intent)
        except Exception:
            return ToolObservation(
                call_id=None,
                status=AuditStatus.FAILED,
                error=ToolError(
                    category=ToolErrorCategory.PERMANENT_FAILURE,
                    message="工具运行时基础设施失败",
                ),
            )


def _executor_error(message: str) -> ToolObservation:
    return ToolObservation(
        call_id=None,
        status=AuditStatus.FAILED,
        error=ToolError(category=ToolErrorCategory.UNAUTHORIZED, message=message),
    )


__all__ = ["RuntimeToolExecutor", "ScopeStore", "TrustedToolScope"]
