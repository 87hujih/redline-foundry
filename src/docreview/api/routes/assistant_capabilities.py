"""Assistant upload capability compatibility endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/assistant")


def _capabilities(extensions: list[str] | None) -> dict[str, object]:
    values = list(extensions or [])
    labels = [value.strip().lstrip(".") for value in values if value.strip().lstrip(".")]
    if not labels:
        return {
            "supported_extensions": [],
            "accept": "",
            "hint": "当前服务未开放文件上传",
        }
    return {
        "supported_extensions": values,
        "accept": ",".join(values),
        "hint": "支持 " + "、".join(labels),
    }


@router.get("/capabilities")
async def get_capabilities(request: Request) -> dict[str, object]:
    extensions = request.app.state.dependencies.upload_policy_extensions
    return {"upload": _capabilities(extensions)}


__all__ = ["router"]
