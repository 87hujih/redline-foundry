"""Go-compatible read-only resource endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from docreview.api.dependencies import AppDependencies
from docreview.api.errors import APIError
from docreview.storage.models import Citation, Resource, ResourceVersion

router = APIRouter(prefix="/api/resources")


def _workspace(dependencies: AppDependencies) -> str:
    if dependencies.compatibility_scope is None:
        raise APIError(500, "资源存储未配置")
    return dependencies.compatibility_scope.workspace_id


def _uuid(value: str) -> str:
    value = value.strip()
    try:
        UUID(value)
    except ValueError as error:
        raise APIError(400, "资源 ID 非法") from error
    return value


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _resource(value: Resource) -> dict[str, object]:
    return {
        "id": value.id,
        "title": value.title,
        "source_type": value.source_type,
        "created_at": _time(value.created_at),
    }


def _version(value: ResourceVersion) -> dict[str, object]:
    return {
        "id": value.id,
        "version_number": value.version_number,
        "content": value.content,
        "source": value.source,
        "created_at": _time(value.created_at),
    }


def _citation(value: Citation) -> dict[str, object]:
    result: dict[str, object] = {
        "citation_id": value.citation_id,
        "resource_id": value.resource_id,
        "section_title": value.section_title,
        "snippet": value.snippet,
    }
    if value.section_id:
        result["section_id"] = value.section_id
    if value.section_type:
        result["section_type"] = value.section_type
    if value.window is not None:
        window = {key: item for key, item in value.window.items() if item not in (None, "", 0)}
        if window:
            result["window"] = window
    return result


def _deps(request: Request) -> AppDependencies:
    return request.app.state.dependencies


async def _find(dependencies: AppDependencies, workspace_id: str, resource_id: str) -> Resource:
    repository = dependencies.resources
    if repository is None:
        raise APIError(500, "资源存储未配置")
    try:
        value = await repository.get_by_id(workspace_id, resource_id)
    except Exception as error:
        raise APIError(500, "查询资源失败") from error
    if value is None:
        raise APIError(404, "资源不存在")
    return value


async def _current(
    dependencies: AppDependencies, workspace_id: str, resource_id: str
) -> ResourceVersion | None:
    repository = dependencies.resources
    if repository is None:
        raise APIError(500, "资源存储未配置")
    try:
        return await repository.get_current_version(workspace_id, resource_id)
    except Exception as error:
        raise APIError(500, "查询资源版本失败") from error


@router.get("")
async def list_resources(request: Request) -> dict[str, object]:
    dependencies = _deps(request)
    if dependencies.resources is None:
        raise APIError(500, "资源存储未配置")
    workspace_id = _workspace(dependencies)
    try:
        values = await dependencies.resources.list(workspace_id)
    except Exception as error:
        raise APIError(500, "查询资源列表失败") from error
    return {"resources": [_resource(value) for value in values]}


@router.get("/{resource_id}")
async def get_resource(resource_id: str, request: Request) -> dict[str, object]:
    dependencies = _deps(request)
    if dependencies.resources is None:
        raise APIError(500, "资源存储未配置")
    resource_id = _uuid(resource_id)
    workspace_id = _workspace(dependencies)
    value = await _find(dependencies, workspace_id, resource_id)
    current = await _current(dependencies, workspace_id, resource_id)
    return {
        "resource": _resource(value),
        "current_version": None if current is None else _version(current),
    }


def _export_filename(value: Resource) -> str:
    base = value.title.strip() or f"resource-{value.id}"
    for character in '\\/:*?"<>|':
        base = base.replace(character, "-")
    base = base.strip() or f"resource-{value.id}"
    return base + ".md"


@router.get("/{resource_id}/export")
async def export_resource(resource_id: str, request: Request) -> Response:
    dependencies = _deps(request)
    if dependencies.resources is None:
        raise APIError(500, "资源存储未配置")
    resource_id = _uuid(resource_id)
    workspace_id = _workspace(dependencies)
    value = await _find(dependencies, workspace_id, resource_id)
    current = await _current(dependencies, workspace_id, resource_id)
    if current is None:
        raise APIError(404, "资源没有可导出的当前版本")
    return Response(
        content=current.content.encode(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_export_filename(value)}"'},
    )


@router.get("/{resource_id}/search")
async def search_resource(resource_id: str, request: Request, q: str = "") -> dict[str, Any]:
    query = q.strip()
    if not query:
        raise APIError(400, "查询参数 q 不能为空")
    dependencies = _deps(request)
    if dependencies.resources is None:
        raise APIError(500, "资源存储未配置")
    resource_id = _uuid(resource_id)
    workspace_id = _workspace(dependencies)
    await _find(dependencies, workspace_id, resource_id)
    if await _current(dependencies, workspace_id, resource_id) is None:
        raise APIError(409, "资源当前版本不存在，无法检索")  # noqa: RUF001
    if dependencies.resource_search is None:
        raise APIError(500, "检索服务未配置")
    try:
        values = await dependencies.resource_search.search_by_resource(
            workspace_id, resource_id, query, 5
        )
    except Exception as error:
        raise APIError(500, "检索资源失败") from error
    return {"query": query, "citations": [_citation(value) for value in values[:5]]}


__all__ = ["router"]
