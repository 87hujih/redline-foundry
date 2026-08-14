"""Bounded JSON decoding for untrusted model and resume payloads."""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BaseModel

from docreview.agent_graph.models import JSONObject


def decode_unique_object(
    raw: str | bytes,
    *,
    max_bytes: int = 256 * 1024,
    max_depth: int = 32,
) -> JSONObject:
    encoded = raw.encode() if isinstance(raw, str) else raw
    if not encoded or len(encoded) > max_bytes:
        raise ValueError("JSON object is empty or exceeds byte limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        text = encoded.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=unique_pairs)
        value, end = decoder.raw_decode(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error
    if text[end:].strip():
        raise ValueError("JSON must contain exactly one value")
    if not isinstance(value, dict):
        raise ValueError("JSON value must be an object")

    def inspect(item: object, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("JSON exceeds depth limit")
        if isinstance(item, dict):
            for child in cast(dict[object, object], item).values():
                inspect(child, depth + 1)
        elif isinstance(item, list):
            for child in cast(list[object], item):
                inspect(child, depth + 1)

    inspect(cast(dict[object, object], value), 0)
    return cast(JSONObject, value)


def decode_model[ModelT: BaseModel](raw: str | bytes, model: type[ModelT]) -> ModelT:
    value = decode_unique_object(raw)
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return model.model_validate_json(canonical)


__all__ = ["decode_model", "decode_unique_object"]
