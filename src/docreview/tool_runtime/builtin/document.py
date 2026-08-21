"""规范且 Workspace-scoped 的文档读取后端。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast

from docreview.tool_runtime.models import (
    BackendRequest,
    Provenance,
    ToolBackendFailure,
    ToolErrorCategory,
    ToolResult,
)
from docreview.tool_runtime.schema import JSONObject, JSONValue

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CanonicalDocumentVersion:
    id: str
    workspace_id: str
    resource_id: str
    version_number: int
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalDocumentNode:
    node_id: str
    workspace_id: str
    resource_id: str
    version_id: str
    node_type: str
    content: str
    content_hash: str
    sibling_order: int
    page_start: int | None = None
    page_end: int | None = None
    attributes: JSONObject = field(default_factory=lambda: dict[str, JSONValue]())

    def __post_init__(self) -> None:
        values = (
            self.node_id,
            self.workspace_id,
            self.resource_id,
            self.version_id,
            self.node_type,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("规范 文档 节点 身份 无效")
        if _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("规范 文档 节点 哈希 无效")
        if self.sibling_order < 0:
            raise ValueError("规范 文档 节点 顺序 无效")
        if self.page_start is not None and self.page_start < 1:
            raise ValueError("规范 文档 节点 页面 无效")
        if self.page_end is not None and (
            self.page_end < 1 or (self.page_start is not None and self.page_end < self.page_start)
        ):
            raise ValueError("规范 文档 节点 页面 范围 无效")


class CanonicalDocumentRepository(Protocol):
    async def resolve_version(
        self, workspace_id: str, resource_id: str, version_id: str | None
    ) -> CanonicalDocumentVersion | None: ...

    async def read_nodes(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str,
        node_ids: tuple[str, ...],
    ) -> list[CanonicalDocumentNode]: ...

    async def search_nodes(
        self,
        workspace_id: str,
        resource_id: str,
        version_id: str,
        query: str,
        limit: int,
    ) -> list[CanonicalDocumentNode]: ...


class DocumentReadBackend:
    def __init__(self, repository: CanonicalDocumentRepository) -> None:
        self._repository = repository

    async def execute(self, request: BackendRequest) -> ToolResult:
        resource_id = _required_string(request.tool_input, "resource_id")
        if resource_id != request.context.resource_id:
            raise ToolBackendFailure(
                ToolErrorCategory.UNAUTHORIZED,
                "文档资源与可信执行范围不匹配",
            )
        raw_version = request.tool_input.get("version_id")
        version_id = (
            None if raw_version is None or raw_version == "" else _string(raw_version, "version_id")
        )
        version = await self._repository.resolve_version(
            request.context.workspace_id, resource_id, version_id
        )
        if version is None:
            raise ToolBackendFailure(ToolErrorCategory.NOT_FOUND, "文档 版本 未找到")
        if (
            version.workspace_id != request.context.workspace_id
            or version.resource_id != resource_id
            or (version_id is not None and version.id != version_id)
        ):
            raise ToolBackendFailure(
                ToolErrorCategory.UNAUTHORIZED,
                "文档版本与可信执行范围不匹配",
            )
        if request.definition.name.value == "document.get_current_version":
            return ToolResult(
                output={
                    "version": {
                        "id": version.id,
                        "resource_id": version.resource_id,
                        "version_number": version.version_number,
                        "source": version.source,
                        "created_at": _timestamp(version.created_at),
                    }
                },
                provenance=(
                    Provenance(
                        source_type="document",
                        source_id=resource_id,
                        resource_id=resource_id,
                        version_id=version.id,
                        trust_level="untrusted",
                    ),
                ),
            )
        if request.definition.name.value == "document.read_nodes":
            raw_node_ids = request.tool_input.get("node_ids")
            if not isinstance(raw_node_ids, list):
                raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "文档 节点 IDs 无效")
            node_ids = tuple(_string(value, "node_id") for value in raw_node_ids)
            nodes = await self._repository.read_nodes(
                request.context.workspace_id, resource_id, version.id, node_ids
            )
            if {node.node_id for node in nodes} != set(node_ids):
                raise ToolBackendFailure(ToolErrorCategory.NOT_FOUND, "文档 节点 未找到")
        elif request.definition.name.value == "document.search_nodes":
            query = _required_string(request.tool_input, "query")
            limit = request.tool_input.get("limit")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, "文档搜索限制无效")
            nodes = await self._repository.search_nodes(
                request.context.workspace_id, resource_id, version.id, query, limit
            )
        else:
            raise ToolBackendFailure(ToolErrorCategory.PERMANENT_FAILURE, "不支持该文档工具")
        for node in nodes:
            if (
                node.workspace_id != request.context.workspace_id
                or node.resource_id != resource_id
                or node.version_id != version.id
            ):
                raise ToolBackendFailure(
                    ToolErrorCategory.UNAUTHORIZED,
                    "文档节点与可信执行范围不匹配",
                )
        ordered = sorted(nodes, key=lambda node: (node.sibling_order, node.node_id))
        return ToolResult(
            output={"nodes": cast(list[JSONValue], [_node_json(node) for node in ordered])},
            provenance=tuple(
                Provenance(
                    source_type="document",
                    source_id=node.node_id,
                    resource_id=node.resource_id,
                    version_id=node.version_id,
                    content_hash=node.content_hash,
                    trust_level="untrusted",
                )
                for node in ordered
            ),
        )

    async def recover(self, request: BackendRequest) -> ToolResult | None:
        return None


def _node_json(node: CanonicalDocumentNode) -> JSONObject:
    value: JSONObject = {
        "node_id": node.node_id,
        "resource_id": node.resource_id,
        "version_id": node.version_id,
        "type": node.node_type,
        "content": node.content,
        "content_hash": node.content_hash,
    }
    if node.page_start is not None:
        value["page_start"] = node.page_start
    if node.page_end is not None:
        value["page_end"] = node.page_end
    if node.attributes:
        value["attributes"] = dict(node.attributes)
    return value


def _required_string(value: JSONObject, key: str) -> str:
    return _string(value.get(key), key)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ToolBackendFailure(ToolErrorCategory.INVALID_INPUT, f"{label} 无效")
    return value


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "CanonicalDocumentNode",
    "CanonicalDocumentRepository",
    "CanonicalDocumentVersion",
    "DocumentReadBackend",
]
