from __future__ import annotations

import httpx
import pytest

from docreview.api.dependencies import AppDependencies
from docreview.api.main import create_app
from docreview.config.settings import load_settings


def app(extensions: list[str] | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(upload_policy_extensions=extensions),
    )


@pytest.mark.anyio
async def test_capabilities_match_upload_policy_and_return_arrays() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app([".md", ".txt"])), base_url="http://test"
    ) as client:
        response = await client.get("/api/assistant/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "upload": {
            "supported_extensions": [".md", ".txt"],
            "accept": ".md,.txt",
            "hint": "支持 md、txt",
        }
    }


@pytest.mark.anyio
async def test_capabilities_empty_policy_is_explicit() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app([])), base_url="http://test"
    ) as client:
        response = await client.get("/api/assistant/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "upload": {
            "supported_extensions": [],
            "accept": "",
            "hint": "当前服务未开放文件上传",
        }
    }
