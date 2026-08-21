"""严格 JSON 解码与刻意保持精简的受支持 Schema 子集。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeGuard, cast

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]

_SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "default",
        "examples",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }
)
_SUPPORTED_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def _reject_constant(value: str) -> None:
    raise ValueError(f"无效的 JSON 数字 令牌{value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(raw: str | bytes) -> JSONValue:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON 无效") from error
    return cast(JSONValue, value)


def decode_json_object(raw: str | bytes) -> JSONObject:
    value = decode_json(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点 必须是对象")
    return value


def canonical_json_bytes(value: JSONValue) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return encoded.encode("utf-8")


def canonical_json_hash(value: JSONValue) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SchemaNode:
    type_name: str
    properties: Mapping[str, SchemaNode] | None = None
    required: frozenset[str] = frozenset()
    additional_properties: bool = False
    items: SchemaNode | None = None
    enum: tuple[JSONValue, ...] = ()
    constant: JSONValue = None
    has_constant: bool = False
    minimum: float | int | None = None
    maximum: float | int | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    pattern: re.Pattern[str] | None = None

    def validate(self, value: JSONValue, path: str = "$", depth: int = 0) -> None:
        if depth > 64:
            raise ValueError(f"JSON 超出最大结构深度 位于{path}")
        if not _matches_type(self.type_name, value):
            raise ValueError(f"{path}必须是{self.type_name}")
        if self.enum and not any(_json_equal(value, allowed) for allowed in self.enum):
            raise ValueError(f"{path}不是允许的枚举值")
        if self.has_constant and not _json_equal(value, self.constant):
            raise ValueError(f"{path}与预期不匹配 const")

        if isinstance(value, dict):
            properties = self.properties or {}
            for name in sorted(self.required):
                if name not in value:
                    raise ValueError(f"{path}.{name}为必填项")
            for name in sorted(value):
                child = properties.get(name)
                if child is None:
                    if not self.additional_properties:
                        raise ValueError(f"{path}.{name}不允许")
                    continue
                child.validate(value[name], f"{path}.{name}", depth + 1)
            return
        if isinstance(value, list):
            if self.min_items is not None and len(value) < self.min_items:
                raise ValueError(f"{path}项目过少")
            if self.max_items is not None and len(value) > self.max_items:
                raise ValueError(f"{path}项目过多")
            if self.items is not None:
                for index, item in enumerate(value):
                    self.items.validate(item, f"{path}[{index}]", depth + 1)
            return
        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise ValueError(f"{path}短于 minLength")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"{path}长于 maxLength")
            if self.pattern is not None and self.pattern.search(value) is None:
                raise ValueError(f"{path}与预期不匹配 模式")
            return
        if _is_number(value):
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{path}低于 最小值")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{path}高于 最大值")


def compile_schema(raw: str | bytes) -> SchemaNode:
    document = decode_json_object(raw)
    node = _compile_schema_object(document, "$schema", root=True)
    if node.type_name != "object":
        raise ValueError("工具 结构 根节点 必须是对象")
    return node


def _compile_schema_object(document: JSONObject, path: str, *, root: bool = False) -> SchemaNode:
    unsupported = sorted(set(document) - _SUPPORTED_KEYWORDS)
    if unsupported:
        raise ValueError(f"unsupported JSON Schema keyword {unsupported[0]} at {path}")
    type_name = document.get("type")
    if not isinstance(type_name, str) or type_name not in _SUPPORTED_TYPES:
        raise ValueError(f"结构 类型 位于{path}必须是受支持的字符串")

    properties: dict[str, SchemaNode] | None = None
    required: frozenset[str] = frozenset()
    additional_properties = False
    if type_name == "object":
        raw_additional = document.get("additionalProperties", True)
        if not isinstance(raw_additional, bool):
            raise ValueError(f"additionalProperties 必须是 布尔值 位于{path}")
        if root and raw_additional:
            raise ValueError(f"additionalProperties must be false at {path}")
        additional_properties = raw_additional
        raw_properties = document.get("properties", {})
        if not isinstance(raw_properties, dict):
            raise ValueError(f"属性 位于{path}必须是对象")
        properties = {}
        for name, child in raw_properties.items():
            if not isinstance(child, dict):
                raise ValueError(f"属性 结构{path}.{name}必须是对象")
            properties[name] = _compile_schema_object(child, f"{path}.properties.{name}")
        raw_required = document.get("required", [])
        if not isinstance(raw_required, list) or not all(
            isinstance(item, str) and bool(item) for item in raw_required
        ):
            raise ValueError(f"{path} 中的 required 必须是字符串数组")
        names = cast(list[str], raw_required)
        if len(names) != len(set(names)):
            raise ValueError(f"{path} 中的 required 包含重复项")
        if any(name not in properties for name in names):
            raise ValueError(f"{path} 中的 required 指定了未知属性")
        required = frozenset(names)
    elif "properties" in document or "required" in document or "additionalProperties" in document:
        raise ValueError(f"对象 关键字 位于{path}需要 对象 类型")

    items: SchemaNode | None = None
    if type_name == "array":
        raw_items = document.get("items")
        if raw_items is not None and not isinstance(raw_items, dict):
            raise ValueError(f"项目 位于{path}必须是 结构 对象")
        if isinstance(raw_items, dict):
            items = _compile_schema_object(raw_items, f"{path}.items")
    elif "items" in document:
        raise ValueError(f"项目 位于{path}需要rray 类型")

    enum = _enum(document, path)
    has_constant = "const" in document
    constant = document.get("const")
    minimum = _optional_number(document, "minimum", path)
    maximum = _optional_number(document, "maximum", path)
    if (minimum is not None or maximum is not None) and type_name not in {"number", "integer"}:
        raise ValueError(f"最小值/最大值 位于{path}需要 数字 类型")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"最小值 超过最大值 位于{path}")

    min_length = _optional_nonnegative_int(document, "minLength", path)
    max_length = _optional_nonnegative_int(document, "maxLength", path)
    if (min_length is not None or max_length is not None) and type_name != "string":
        raise ValueError(f"minLength/maxLength 位于{path}需要 字符串 类型")
    if min_length is not None and max_length is not None and min_length > max_length:
        raise ValueError(f"minLength 超出 maxLength 位于{path}")

    min_items = _optional_nonnegative_int(document, "minItems", path)
    max_items = _optional_nonnegative_int(document, "maxItems", path)
    if (min_items is not None or max_items is not None) and type_name != "array":
        raise ValueError(f"minItems/maxItems 位于{path}需要 数组 类型")
    if min_items is not None and max_items is not None and min_items > max_items:
        raise ValueError(f"minItems 超出 maxItems 位于{path}")

    pattern: re.Pattern[str] | None = None
    if "pattern" in document:
        raw_pattern = document["pattern"]
        if not isinstance(raw_pattern, str) or type_name != "string":
            raise ValueError(f"模式 位于{path}需要 字符串 类型")
        try:
            pattern = re.compile(raw_pattern)
        except re.error as error:
            raise ValueError(f"无效的 模式 位于{path}") from error

    return SchemaNode(
        type_name=type_name,
        properties=MappingProxyType(properties) if properties is not None else None,
        required=required,
        additional_properties=additional_properties,
        items=items,
        enum=enum,
        constant=constant,
        has_constant=has_constant,
        minimum=minimum,
        maximum=maximum,
        min_length=min_length,
        max_length=max_length,
        min_items=min_items,
        max_items=max_items,
        pattern=pattern,
    )


def _enum(document: JSONObject, path: str) -> tuple[JSONValue, ...]:
    if "enum" not in document:
        return ()
    value = document["enum"]
    if not isinstance(value, list) or not value:
        raise ValueError(f"enum 位于{path}必须是非空数组")
    return tuple(value)


def _optional_number(document: JSONObject, name: str, path: str) -> int | float | None:
    if name not in document:
        return None
    value = document[name]
    if not _is_number(value):
        raise ValueError(f"{name}位于{path}必须是数字")
    return value


def _optional_nonnegative_int(document: JSONObject, name: str, path: str) -> int | None:
    if name not in document:
        return None
    value = document[name]
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}位于{path}必须是 非负 整数")
    return value


def _matches_type(expected: str, value: JSONValue) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return _is_number(value)
    if expected == "integer":
        return type(value) is int
    if expected == "boolean":
        return type(value) is bool
    return value is None


def _is_number(value: object) -> TypeGuard[int | float]:
    return type(value) in {int, float} and (not isinstance(value, float) or math.isfinite(value))


def _json_equal(left: JSONValue, right: JSONValue) -> bool:
    if type(left) is not type(right):
        return False
    return left == right


__all__ = [
    "JSONObject",
    "JSONValue",
    "SchemaNode",
    "canonical_json_bytes",
    "canonical_json_hash",
    "compile_schema",
    "decode_json",
    "decode_json_object",
]
