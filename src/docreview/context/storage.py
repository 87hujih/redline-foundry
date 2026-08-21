"""不可变上下文 manifest 的严格持久化适配器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol, cast

from docreview.context.assembler import (
    ContextItem,
    ContextManifest,
    ManifestStore,
    context_item_from_json,
    manifest_items_bytes,
)
from docreview.runtime.models import ContextManifest as StoredContextManifest
from docreview.runtime.models import JSONObject


class ContextManifestRepository(Protocol):
    async def create_context_manifest(
        self,
        run_id: str,
        step_id: str,
        token_budget: int,
        reserved_output_tokens: int,
        tokenizer: str,
        items: list[JSONObject],
        total_tokens: int,
        content_hash: str,
    ) -> StoredContextManifest: ...

    async def get_context_manifest(self, manifest_id: str) -> StoredContextManifest | None: ...


class RepositoryManifestStore(ManifestStore):
    def __init__(self, repository: ContextManifestRepository) -> None:
        self._repository = repository

    async def save(self, manifest: ContextManifest) -> str:
        raw: object = json.loads(manifest_items_bytes(manifest.items))
        if not isinstance(raw, list):
            raise RuntimeError("上下文 清单 项目 未编码为 数组")
        raw_items = cast(list[object], raw)
        items = [cast(JSONObject, item) for item in raw_items if isinstance(item, dict)]
        if len(items) != len(raw_items):
            raise RuntimeError("上下文 清单 包含非对象项")
        stored = await self._repository.create_context_manifest(
            manifest.run_id,
            manifest.step_id,
            manifest.token_budget,
            manifest.reserved_output_tokens,
            manifest.tokenizer,
            items,
            manifest.total_tokens,
            manifest.content_hash,
        )
        _verify_binding(manifest, stored)
        return stored.id

    async def load(self, manifest_id: str) -> ContextManifest | None:
        stored = await self._repository.get_context_manifest(manifest_id)
        if stored is None:
            return None
        items = tuple(_decode_items(stored.items))
        manifest = ContextManifest(
            id=stored.id,
            run_id=stored.run_id,
            step_id=stored.step_id,
            token_budget=stored.token_budget,
            reserved_output_tokens=stored.reserved_output_tokens,
            tokenizer=stored.tokenizer,
            items=items,
            total_tokens=stored.total_tokens,
            content_hash=stored.content_hash,
            created_at=stored.created_at,
        )
        _verify_integrity(manifest)
        return manifest


def _decode_items(values: Sequence[JSONObject]) -> list[ContextItem]:
    return [context_item_from_json(value) for value in values]


def _verify_binding(expected: ContextManifest, stored: StoredContextManifest) -> None:
    if (
        not stored.id.strip()
        or stored.run_id != expected.run_id
        or stored.step_id != expected.step_id
        or stored.token_budget != expected.token_budget
        or stored.reserved_output_tokens != expected.reserved_output_tokens
        or stored.tokenizer != expected.tokenizer
        or stored.total_tokens != expected.total_tokens
        or stored.content_hash != expected.content_hash
        or manifest_items_bytes(_decode_items(stored.items)) != manifest_items_bytes(expected.items)
    ):
        raise RuntimeError("已持久化的 上下文 清单 绑定 无效")


def _verify_integrity(manifest: ContextManifest) -> None:
    digest = "sha256:" + hashlib.sha256(manifest_items_bytes(manifest.items)).hexdigest()
    if (
        manifest.content_hash != digest
        or manifest.total_tokens != sum(item.token_count for item in manifest.items)
        or manifest.token_budget <= 0
        or manifest.reserved_output_tokens < 0
        or manifest.total_tokens + manifest.reserved_output_tokens > manifest.token_budget
        or not manifest.tokenizer.strip()
    ):
        raise RuntimeError("persisted context manifest integrity check failed")


__all__ = ["ContextManifestRepository", "RepositoryManifestStore"]
