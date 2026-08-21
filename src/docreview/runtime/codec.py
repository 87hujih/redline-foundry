"""幂等 Runtime 边界共享的规范 JSON 辅助函数。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from docreview.runtime.models import JSONObject


def require_object(value: object, field: str) -> JSONObject:
    if not isinstance(value, dict):
        raise ValueError(f"{field}必须是 JSON 对象")
    typed = cast(dict[object, Any], value)
    return {str(key): item for key, item in typed.items()}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return f"sha256:{digest}"


def same_json(left: object, right: object) -> bool:
    return canonical_json(left) == canonical_json(right)


def as_object(value: Any) -> JSONObject:
    return require_object(value, "database JSON")


__all__ = ["as_object", "canonical_json", "require_object", "same_json", "sha256_json"]
