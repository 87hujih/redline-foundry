"""经过认证的公开 Approval 查询路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Request

from docreview.api.errors import APIError
from docreview.api.routes.agent_runs import (
    authenticate,
    deps,
    json_value,
    parse_limit,
    parse_uuid,
)
from docreview.identity.trusted_ingress import (
    HEADER_SIGNATURE,
    HEADER_WORKSPACE_ID,
    IdentityRequest,
    UntrustedIdentityError,
)
from docreview.runtime.contracts import ApprovalDecision
from docreview.runtime.errors import ApprovalConflictError
from docreview.storage.postgres.errors import RecordNotFoundError

router = APIRouter(prefix="/api/agent/approvals")
APPROVAL_STATUSES = {"pending", "approved", "rejected", "cancelled"}


@router.get("")
async def list_approvals(request: Request) -> dict[str, Any]:
    repository = deps(request).approval_queries
    if repository is None:
        raise APIError(503, "Agent 运行查询服务未配置")
    scope = authenticate(request)
    limit = parse_limit(request.query_params.get("limit", ""))
    status = request.query_params.get("status", "").strip()
    if status and status not in APPROVAL_STATUSES:
        raise APIError(400, "审批状态无效")
    try:
        values = await repository.list_approvals(scope.workspace_id, status, limit)
    except Exception as error:
        raise APIError(500, "审批记录查询失败") from error
    return {"approvals": json_value(values)}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, request: Request) -> dict[str, Any]:
    repository = deps(request).approval_queries
    if repository is None:
        raise APIError(503, "Agent 运行查询服务未配置")
    scope = authenticate(request)
    approval_id = parse_uuid(approval_id, "审批 ID 非法")
    try:
        value = await repository.get_approval(scope.workspace_id, approval_id)
    except RecordNotFoundError as error:
        raise APIError(404, "记录不存在") from error
    except Exception as error:
        raise APIError(500, "审批记录查询失败") from error
    return {"approval": json_value(value)}


async def _decision_body(request: Request) -> str:
    try:
        value = await request.json()
    except Exception as error:
        raise APIError(400, "审批理由不能为空") from error
    if not isinstance(value, dict):
        raise APIError(400, "审批理由不能为空")
    reason = cast(dict[str, Any], value).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise APIError(400, "审批理由不能为空")
    return reason.strip()


def _decision_scope(request: Request):
    dependencies = deps(request)
    if dependencies.identity_adapter is None:
        raise APIError(503, "持久化审批服务未配置")
    if not request.headers.get(HEADER_SIGNATURE, "").strip():
        raise APIError(401, "审批身份不可信")
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
        raise APIError(401, "审批身份不可信") from error
    if not scope.trusted or scope.principal.type != "user" or scope.workspace_id != workspace_id:
        raise APIError(401, "审批身份不可信")
    return scope


async def _decide(approval_id: str, request: Request, status: str) -> dict[str, object]:
    approval_id = parse_uuid(approval_id, "审批 ID 非法")
    reason = await _decision_body(request)
    dependencies = deps(request)
    if dependencies.approval_decider is None:
        raise APIError(503, "持久化审批服务未配置")
    scope = _decision_scope(request)
    try:
        approval = await dependencies.approval_decider.decide_approval(
            ApprovalDecision(
                approval_id=approval_id,
                workspace_id=scope.workspace_id,
                status=status,
                reason=reason,
                decided_by_type=scope.principal.type,
                decided_by_id=scope.principal.id,
                decided_at=datetime.now(UTC),
            )
        )
    except PermissionError as error:
        raise APIError(403, "审批权限不足") from error
    except LookupError as error:
        raise APIError(404, "审批不存在") from error
    except ApprovalConflictError as error:
        raise APIError(409, "审批状态冲突") from error
    except ValueError as error:
        raise APIError(400, "审批请求无效") from error
    except Exception as error:
        raise APIError(500, "审批决策失败") from error
    return {"approval": {"id": approval.id, "status": approval.status}}


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, request: Request) -> dict[str, object]:
    return await _decide(approval_id, request, "approved")


@router.post("/{approval_id}/reject")
async def reject(approval_id: str, request: Request) -> dict[str, object]:
    return await _decide(approval_id, request, "rejected")


__all__ = ["router"]
