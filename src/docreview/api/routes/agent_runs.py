"""Authenticated public Run query routes."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Request

from docreview.api.dependencies import AppDependencies, RunQueryReader
from docreview.api.errors import APIError
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
    WorkspaceScope,
)
from docreview.storage.models import PublicRunDetail
from docreview.storage.postgres.errors import RecordNotFoundError

router = APIRouter(prefix="/api/agent/runs")
RUN_STATUSES = {
    "queued",
    "running",
    "waiting_input",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
}


def deps(request: Request) -> AppDependencies:
    return request.app.state.dependencies


def agent_queries(request: Request) -> RunQueryReader:
    repository = deps(request).run_queries
    if repository is None:
        raise APIError(503, "Agent 运行查询服务未配置")

    return repository


def authenticate(request: Request) -> WorkspaceScope:
    dependencies = deps(request)
    if dependencies.identity_adapter is None:
        raise APIError(503, "Agent 运行查询服务未配置")
    if not request.headers.get(HEADER_SIGNATURE, "").strip():
        raise APIError(401, "Agent 查询身份不可信")
    workspace_id = request.headers.get(HEADER_WORKSPACE_ID, "").strip()
    try:
        scope = dependencies.identity_adapter.authenticate(
            IdentityRequest(
                method=request.method,
                path=request.url.path,
                request_id=str(request.state.request_id),
                headers=request.headers,
            ),
            workspace_id,
        )
    except UntrustedIdentityError as error:
        raise APIError(401, "Agent 查询身份不可信") from error
    if not scope.trusted or scope.principal.type != "user" or scope.workspace_id != workspace_id:
        raise APIError(401, "Agent 查询身份不可信")
    return scope


def parse_uuid(value: str, message: str) -> str:
    value = value.strip()
    try:
        UUID(value)
    except ValueError as error:
        raise APIError(400, message) from error
    return value


def parse_limit(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return 50
    try:
        value = int(raw)
    except ValueError as error:
        raise APIError(400, "limit 必须介于 1 和 100 之间") from error
    if value < 1 or value > 100:
        raise APIError(400, "limit 必须介于 1 和 100 之间")
    return value


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [json_value(item) for item in cast(list[Any], value)]
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, Any] = {}
        for item in fields(value):
            current = getattr(value, item.name)
            if current is None or current == "":
                continue
            result[item.name] = json_value(current)
        return result
    return value


@router.get("")
async def list_runs(request: Request) -> dict[str, Any]:
    repository = agent_queries(request)
    scope = authenticate(request)
    limit = parse_limit(request.query_params.get("limit", ""))
    status = request.query_params.get("status", "").strip()
    if status and status not in RUN_STATUSES:
        raise APIError(400, "运行状态无效")
    resource_id = request.query_params.get("resource_id", "").strip()
    if resource_id:
        resource_id = parse_uuid(resource_id, "资源 ID 非法")
    try:
        values = await repository.list_runs(scope.workspace_id, status, resource_id, limit)
    except Exception as error:
        raise APIError(500, "运行记录查询失败") from error
    return {"runs": json_value(values)}


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    repository = agent_queries(request)
    scope = authenticate(request)
    run_id = parse_uuid(run_id, "运行 ID 非法")
    try:
        detail: PublicRunDetail = await repository.get_run(scope.workspace_id, run_id)
    except RecordNotFoundError as error:
        raise APIError(404, "记录不存在") from error
    except Exception as error:
        raise APIError(500, "运行记录查询失败") from error
    return json_value(detail)


__all__ = [
    "RUN_STATUSES",
    "agent_queries",
    "authenticate",
    "deps",
    "json_value",
    "parse_limit",
    "parse_uuid",
    "router",
]
