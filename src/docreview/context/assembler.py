"""供每次类型化模型调用使用的有界 ContextAssembler。"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from pydantic import JsonValue

from docreview.agent_graph.models import (
    ContextResult,
    ContextSnapshot,
    JSONObject,
    RuntimeRequest,
)


class ContextLayer(StrEnum):
    CONTROL = "control"
    TASK = "task"
    WORKING_MEMORY = "working_memory"
    EVIDENCE = "evidence"
    CONVERSATION = "conversation_memory"
    ARTIFACT_REFERENCE = "artifact_reference"


class TrustLevel(StrEnum):
    SYSTEM = "system"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class ContextItem:
    layer: ContextLayer
    item_type: str
    trust_level: TrustLevel
    source_id: str = ""
    resource_id: str = ""
    version_id: str = ""
    node_id: str = ""
    relevance_score: float = 0
    token_count: int = 0
    content_hash: str = ""
    selected_reason: str = ""
    truncated: bool = False
    content: str = ""
    reference: str = ""
    created_at: datetime | None = None
    window_group_id: str = ""
    order_in_window: int = 0
    retrieval_rank: int = 0
    source_spans: tuple[JSONObject, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextManifest:
    id: str
    run_id: str
    step_id: str
    token_budget: int
    reserved_output_tokens: int
    tokenizer: str
    items: tuple[ContextItem, ...]
    total_tokens: int
    content_hash: str
    created_at: datetime


class Tokenizer(Protocol):
    @property
    def name(self) -> str: ...

    def count(self, value: str) -> int: ...


class ManifestStore(Protocol):
    async def save(self, manifest: ContextManifest) -> str: ...
    async def load(self, manifest_id: str) -> ContextManifest | None: ...


class ContextCandidateSource(Protocol):
    async def candidates(self, request: RuntimeRequest) -> Sequence[ContextItem]: ...


@dataclass(frozen=True, slots=True)
class ContextConfig:
    tokenizer: Tokenizer
    token_budget: int
    reserved_output_tokens: int
    layer_budgets: Mapping[ContextLayer, int]

    def __post_init__(self) -> None:
        if (
            not self.tokenizer.name.strip()
            or self.token_budget <= 0
            or self.reserved_output_tokens < 0
            or self.reserved_output_tokens >= self.token_budget
        ):
            raise ValueError("无效的 上下文 分词器 或 令牌 预算")
        if any(budget < 0 for budget in self.layer_budgets.values()):
            raise ValueError("上下文层预算不能为负数")


class RequiredContextBudgetError(ValueError):
    pass


class ModelEstimator:
    def __init__(self, profile: str) -> None:
        self._name = profile.strip()
        if not self._name:
            raise ValueError("分词器 配置档 为必填项")

    @property
    def name(self) -> str:
        return self._name

    def count(self, value: str) -> int:
        tokens = 0
        run_bytes = 0

        def flush() -> None:
            nonlocal tokens, run_bytes
            if run_bytes:
                tokens += (run_bytes + 3) // 4
                run_bytes = 0

        for character in value:
            if character.isspace():
                flush()
            elif _individual_token(character):
                flush()
                tokens += 1
            else:
                run_bytes += len(character.encode())
        flush()
        return tokens


class JSONTokenCounter:
    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer

    def count_json(self, value: bytes) -> int:
        return self._tokenizer.count(value.decode("utf-8"))


class ContextAssembler:
    def __init__(
        self,
        config: ContextConfig,
        store: ManifestStore | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))

    async def assemble(
        self, run_id: str, step_id: str, candidates: Sequence[ContextItem]
    ) -> ContextManifest:
        run_id = _identity(run_id, "context run_id")
        step_id = _identity(step_id, "context step_id")
        now = _timestamp(self._now(), "context clock")
        grouped = {layer: list[ContextItem]() for layer in ContextLayer}
        for candidate in candidates:
            prepared = self._prepare(candidate, now)
            grouped[prepared.layer].append(prepared)

        available = self._config.token_budget - self._config.reserved_output_tokens
        selected: list[ContextItem] = []
        total = 0
        for layer in _LAYER_ORDER:
            items = grouped[layer]
            if layer in {ContextLayer.EVIDENCE, ContextLayer.CONVERSATION}:
                items.sort(
                    key=lambda item: (
                        -item.relevance_score,
                        item.retrieval_rank if item.retrieval_rank > 0 else 2**31 - 1,
                        item.window_group_id,
                        item.order_in_window,
                        item.source_id,
                    )
                )
            remaining = available - total
            configured = self._config.layer_budgets.get(layer)
            if configured is not None:
                remaining = min(remaining, configured)
            for item in items:
                if item.token_count > remaining or item.token_count > available - total:
                    if layer in {ContextLayer.CONTROL, ContextLayer.TASK}:
                        raise RequiredContextBudgetError(f"必需的 {layer.value} 上下文超出令牌预算")
                    continue
                selected.append(item)
                total += item.token_count
                remaining -= item.token_count

        encoded = manifest_items_bytes(selected)
        manifest = ContextManifest(
            id="",
            run_id=run_id,
            step_id=step_id,
            token_budget=self._config.token_budget,
            reserved_output_tokens=self._config.reserved_output_tokens,
            tokenizer=self._config.tokenizer.name,
            items=tuple(selected),
            total_tokens=total,
            content_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
            created_at=now,
        )
        if self._store is None:
            return manifest
        manifest_id = _identity(await self._store.save(manifest), "context manifest id")
        return replace(manifest, id=manifest_id)

    def _prepare(self, item: ContextItem, now: datetime) -> ContextItem:
        item_type = _identity(item.item_type, "context item_type")
        source_id = item.source_id.strip()
        resource_id = item.resource_id.strip()
        version_id = item.version_id.strip()
        node_id = item.node_id.strip()
        reference = item.reference.strip()
        window_group_id = item.window_group_id.strip()
        if item.layer is ContextLayer.CONTROL and item.trust_level is not TrustLevel.SYSTEM:
            raise ValueError("控制 上下文 需要 系统 信任")
        if (
            item.layer
            in {
                ContextLayer.EVIDENCE,
                ContextLayer.CONVERSATION,
                ContextLayer.ARTIFACT_REFERENCE,
            }
            and item.trust_level is TrustLevel.SYSTEM
        ):
            raise ValueError(f"{item.layer.value} context cannot claim system trust")
        if not math.isfinite(item.relevance_score) or not 0 <= item.relevance_score <= 1:
            raise ValueError("上下文 相关度 评分 必须介于 零 和 一个")
        if item.order_in_window < 0 or item.retrieval_rank < 0:
            raise ValueError("上下文 检索 顺序 无效")
        content = item.content
        counted = content
        selected_reason = item.selected_reason
        if item.layer is ContextLayer.ARTIFACT_REFERENCE:
            if not reference:
                raise ValueError("制品 上下文 需要 引用")
            content = ""
            counted = reference
            selected_reason = selected_reason or "大型对象以制品引用形式保留"
        elif not content.strip():
            raise ValueError("内联 上下文 内容 为必填项")
        tokens = self._config.tokenizer.count(counted)
        if isinstance(tokens, bool) or tokens < 0:
            raise ValueError("上下文 分词器 返回了无效的 令牌 数量")
        created_at = now if item.created_at is None else _timestamp(item.created_at, "context item")
        return ContextItem(
            layer=item.layer,
            item_type=item_type,
            source_id=source_id,
            resource_id=resource_id,
            version_id=version_id,
            node_id=node_id,
            trust_level=item.trust_level,
            relevance_score=item.relevance_score,
            token_count=tokens,
            content_hash="sha256:" + hashlib.sha256(counted.encode()).hexdigest(),
            selected_reason=selected_reason or "在分层预算内选中",
            truncated=item.truncated,
            content=content,
            reference=reference,
            created_at=created_at,
            window_group_id=window_group_id,
            order_in_window=item.order_in_window,
            retrieval_rank=item.retrieval_rank,
            source_spans=item.source_spans,
        )


class ManagedContextAssembler:
    def __init__(
        self,
        assembler: ContextAssembler,
        reader: ManifestStore,
        source: ContextCandidateSource,
    ) -> None:
        self._assembler = assembler
        self._reader = reader
        self._source = source

    async def assemble(self, request: RuntimeRequest) -> ContextResult:
        if request.step_id is None:
            raise ValueError("持久化 step_id 为必填项 用于 上下文 装配")
        candidates = await self._source.candidates(request)
        manifest = await self._assembler.assemble(request.run_id, request.step_id, candidates)
        if not manifest.id:
            raise RuntimeError("上下文 清单 尚未持久化")
        return ContextResult(context_manifest_id=manifest.id)

    async def load(self, manifest_id: str) -> ContextSnapshot:
        manifest_id = _identity(manifest_id, "context manifest id")
        manifest = await self._reader.load(manifest_id)
        if manifest is None:
            raise LookupError("上下文 清单 未找到")
        if manifest.id != manifest_id:
            raise RuntimeError("上下文 清单 身份 不匹配")
        return ContextSnapshot(
            context_manifest_id=manifest.id,
            run_id=manifest.run_id,
            step_id=manifest.step_id,
            items=tuple(context_item_json(item) for item in manifest.items),
            content_hash=manifest.content_hash,
        )


def manifest_items_bytes(items: Sequence[ContextItem]) -> bytes:
    encoded = json.dumps(
        [context_item_json(item) for item in items],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode()
    )


def context_item_from_json(value: Mapping[str, object]) -> ContextItem:
    created = value.get("created_at")
    if not isinstance(created, str):
        raise ValueError("已存储的 上下文 项 时间戳 无效")
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("已存储的 上下文 项 时间戳 无效") from error
    score = value.get("relevance_score", 0)
    tokens = value.get("token_count", 0)
    if isinstance(score, bool) or not isinstance(score, int | float):
        raise ValueError("已存储的 上下文 相关度 无效")
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise ValueError("已存储的 上下文 令牌 数量 无效")
    return ContextItem(
        layer=ContextLayer(_string(value, "layer")),
        item_type=_string(value, "item_type"),
        source_id=_string(value, "source_id", allow_missing=True),
        resource_id=_string(value, "resource_id", allow_missing=True),
        version_id=_string(value, "version_id", allow_missing=True),
        node_id=_string(value, "node_id", allow_missing=True),
        trust_level=TrustLevel(_string(value, "trust_level")),
        relevance_score=float(score),
        token_count=tokens,
        content_hash=_string(value, "content_hash"),
        selected_reason=_string(value, "selected_reason"),
        truncated=value.get("truncated") is True,
        content=_string(value, "content", allow_missing=True),
        reference=_string(value, "reference", allow_missing=True),
        created_at=_timestamp(created_at, "stored context item"),
        window_group_id=_string(value, "window_group_id", allow_missing=True),
        order_in_window=_optional_nonnegative_int(value.get("order_in_window", 0)),
        retrieval_rank=_optional_nonnegative_int(value.get("retrieval_rank", 0)),
        source_spans=_source_spans(value.get("source_spans", [])),
    )


def context_item_json(item: ContextItem) -> JSONObject:
    score_value = float(item.relevance_score)
    score: int | float = score_value
    if score_value.is_integer():
        score = int(score_value)
    result: JSONObject = {
        "layer": item.layer.value,
        "item_type": item.item_type,
    }
    for key, value in (
        ("source_id", item.source_id),
        ("resource_id", item.resource_id),
        ("version_id", item.version_id),
        ("node_id", item.node_id),
    ):
        if value:
            result[key] = value
    result.update(
        {
            "trust_level": item.trust_level.value,
            "relevance_score": score,
            "token_count": item.token_count,
            "content_hash": item.content_hash,
            "selected_reason": item.selected_reason,
            "truncated": item.truncated,
        }
    )
    if item.content:
        result["content"] = item.content
    if item.reference:
        result["reference"] = item.reference
    if item.window_group_id:
        result["window_group_id"] = item.window_group_id
    if item.order_in_window:
        result["order_in_window"] = item.order_in_window
    if item.retrieval_rank:
        result["retrieval_rank"] = item.retrieval_rank
    if item.source_spans:
        result["source_spans"] = cast(JsonValue, list(item.source_spans))
    if item.created_at is None:
        raise ValueError("已准备的 上下文 项 时间戳 为必填项")
    result["created_at"] = _timestamp_text(item.created_at)
    return result


def _individual_token(value: str) -> bool:
    codepoint = ord(value)
    cjk = (
        0x3400 <= codepoint <= 0x9FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )
    return cjk or unicodedata.category(value).startswith(("P", "S"))


def _timestamp_text(value: datetime) -> str:
    normalized = _timestamp(value, "context timestamp")
    base = normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if "." not in base:
        return base
    head, tail = base.split(".", 1)
    fraction = tail.removesuffix("Z").rstrip("0")
    return head + (("." + fraction) if fraction else "") + "Z"


def _timestamp(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}时间戳 必须包含时区信息")
    return value.astimezone(UTC)


def _identity(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > 500:
        raise ValueError(f"{field}无效")
    return normalized


def _string(value: Mapping[str, object], key: str, *, allow_missing: bool = False) -> str:
    item = value.get(key, "")
    if not isinstance(item, str) or (not allow_missing and not item):
        raise ValueError(f"已存储的 上下文{key}无效")
    return item


def _optional_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("已存储的 上下文 检索 顺序 无效")
    return value


def _source_spans(value: object) -> tuple[JSONObject, ...]:
    if not isinstance(value, list):
        raise ValueError("已存储的 上下文 来源 范围 无效")
    items = cast(list[object], value)
    if not all(isinstance(item, Mapping) for item in items):
        raise ValueError("已存储的 上下文 来源 范围 无效")
    return tuple(cast(JSONObject, dict(cast(Mapping[str, object], item))) for item in items)


_LAYER_ORDER = (
    ContextLayer.CONTROL,
    ContextLayer.TASK,
    ContextLayer.WORKING_MEMORY,
    ContextLayer.EVIDENCE,
    ContextLayer.CONVERSATION,
    ContextLayer.ARTIFACT_REFERENCE,
)


__all__ = [
    "ContextAssembler",
    "ContextCandidateSource",
    "ContextConfig",
    "ContextItem",
    "ContextLayer",
    "ContextManifest",
    "JSONTokenCounter",
    "ManagedContextAssembler",
    "ManifestStore",
    "ModelEstimator",
    "RequiredContextBudgetError",
    "Tokenizer",
    "TrustLevel",
    "context_item_from_json",
    "context_item_json",
    "manifest_items_bytes",
]
