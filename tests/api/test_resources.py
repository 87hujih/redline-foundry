from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest

from docreview.api.dependencies import AppDependencies, CompatibilityScope
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.storage.models import Citation, Resource, ResourceVersion

WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
RESOURCE_ID = "55555555-5555-4555-8555-555555555555"
VERSION_ID = "66666666-6666-4666-8666-666666666666"
CREATED_AT = datetime(2026, 8, 12, 10, 30, tzinfo=UTC)


@dataclass
class FakeResources:
    resources: list[Resource] = field(default_factory=lambda: list[Resource]())
    resource: Resource | None = None
    version: ResourceVersion | None = None
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def list(self, workspace_id: str) -> list[Resource]:
        self.calls.append(("list", workspace_id))
        return self.resources

    async def get_by_id(self, workspace_id: str, resource_id: str) -> Resource | None:
        self.calls.append(("get", workspace_id, resource_id))
        return self.resource

    async def get_current_version(
        self, workspace_id: str, resource_id: str
    ) -> ResourceVersion | None:
        self.calls.append(("version", workspace_id, resource_id))
        return self.version


@dataclass
class FakeSearch:
    citations: list[Citation] = field(default_factory=lambda: list[Citation]())
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def search_by_resource(
        self, workspace_id: str, resource_id: str, query: str, limit: int
    ) -> list[Citation]:
        self.calls.append((workspace_id, resource_id, query, limit))
        return self.citations


def resource(title: str = "Review/Notes") -> Resource:
    return Resource(id=RESOURCE_ID, title=title, source_type="upload", created_at=CREATED_AT)


def version() -> ResourceVersion:
    return ResourceVersion(
        id=VERSION_ID,
        resource_id=RESOURCE_ID,
        version_number=2,
        content="# Current\n",
        source="assistant_upload",
        created_at=CREATED_AT,
    )


def app(resources: FakeResources | None, search: FakeSearch | None = None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(
            compatibility_scope=CompatibilityScope(WORKSPACE_ID),
            resources=resources,
            resource_search=search,
        ),
    )


@pytest.mark.anyio
async def test_list_resources_exact_dto_empty_array_and_workspace_scope() -> None:
    repository = FakeResources(resources=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get("/api/resources")

    assert response.status_code == 200
    assert response.json() == {"resources": []}
    assert repository.calls == [("list", WORKSPACE_ID)]


@pytest.mark.anyio
async def test_resource_detail_exact_dto_and_current_version() -> None:
    repository = FakeResources(resource=resource(), version=version())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/resources/{RESOURCE_ID}")

    assert response.status_code == 200
    assert response.json() == {
        "resource": {
            "id": RESOURCE_ID,
            "title": "Review/Notes",
            "source_type": "upload",
            "created_at": "2026-08-12T10:30:00Z",
        },
        "current_version": {
            "id": VERSION_ID,
            "version_number": 2,
            "content": "# Current\n",
            "source": "assistant_upload",
            "created_at": "2026-08-12T10:30:00Z",
        },
    }
    assert repository.calls == [
        ("get", WORKSPACE_ID, RESOURCE_ID),
        ("version", WORKSPACE_ID, RESOURCE_ID),
    ]


@pytest.mark.anyio
async def test_resource_detail_validates_uuid_and_maps_missing() -> None:
    repository = FakeResources()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        invalid = await client.get("/api/resources/not-a-uuid")
        missing = await client.get(f"/api/resources/{RESOURCE_ID}")

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "资源 ID 非法"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "资源不存在"}
    assert repository.calls == [("get", WORKSPACE_ID, RESOURCE_ID)]


@pytest.mark.anyio
async def test_export_current_markdown_with_sanitized_filename() -> None:
    repository = FakeResources(resource=resource('Review/Notes:*?"<>|'), version=version())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/resources/{RESOURCE_ID}/export")

    assert response.status_code == 200
    assert response.content == b"# Current\n"
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="Review-Notes-------.md"'
    )


@pytest.mark.anyio
async def test_search_trims_query_caps_limit_and_keeps_citation_shape() -> None:
    repository = FakeResources(resource=resource(), version=version())
    search = FakeSearch(
        citations=[
            Citation(
                citation_id="cite_1",
                resource_id=RESOURCE_ID,
                section_title="Summary",
                snippet="Evidence",
                section_id="section-1",
                section_type="summary",
                window={"group_id": "group-1", "start_order": 1, "end_order": 2},
            )
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository, search)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/resources/{RESOURCE_ID}/search?q=%20policy%20")

    assert response.status_code == 200
    assert response.json() == {
        "query": "policy",
        "citations": [
            {
                "citation_id": "cite_1",
                "resource_id": RESOURCE_ID,
                "section_id": "section-1",
                "section_type": "summary",
                "section_title": "Summary",
                "snippet": "Evidence",
                "window": {"group_id": "group-1", "start_order": 1, "end_order": 2},
            }
        ],
    }
    assert search.calls == [(WORKSPACE_ID, RESOURCE_ID, "policy", 5)]


@pytest.mark.anyio
async def test_search_failure_branches_match_go() -> None:
    repository = FakeResources(resource=resource(), version=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        blank = await client.get(f"/api/resources/{RESOURCE_ID}/search?q=%20")
        no_version = await client.get(f"/api/resources/{RESOURCE_ID}/search?q=policy")

    assert blank.status_code == 400
    assert blank.json() == {"error": "查询参数 q 不能为空"}
    assert no_version.status_code == 409
    assert no_version.json() == {"error": "资源当前版本不存在，无法检索"}  # noqa: RUF001


@pytest.mark.anyio
async def test_unconfigured_resource_storage_fails_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(None)), base_url="http://test"
    ) as client:
        response = await client.get("/api/resources")

    assert response.status_code == 500
    assert response.json() == {"error": "资源存储未配置"}
