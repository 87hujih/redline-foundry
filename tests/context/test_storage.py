from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from docreview.context.assembler import (
    ContextAssembler,
    ContextConfig,
    ContextItem,
    ContextLayer,
    ModelEstimator,
    TrustLevel,
)
from docreview.context.storage import RepositoryManifestStore
from docreview.runtime.models import ContextManifest as StoredContextManifest

NOW = datetime(2026, 8, 15, tzinfo=UTC)


class Repository:
    def __init__(self) -> None:
        self.stored: StoredContextManifest | None = None

    async def create_context_manifest(
        self,
        run_id: str,
        step_id: str,
        token_budget: int,
        reserved_output_tokens: int,
        tokenizer: str,
        items: list[dict[str, object]],
        total_tokens: int,
        content_hash: str,
    ) -> StoredContextManifest:
        self.stored = StoredContextManifest(
            "manifest-1",
            run_id,
            step_id,
            token_budget,
            reserved_output_tokens,
            tokenizer,
            items,  # type: ignore[arg-type]
            total_tokens,
            content_hash,
            NOW,
        )
        return self.stored

    async def get_context_manifest(self, manifest_id: str) -> StoredContextManifest | None:
        assert manifest_id == "manifest-1"
        return self.stored


async def test_repository_manifest_store_round_trips_exact_selected_items() -> None:
    repository = Repository()
    store = RepositoryManifestStore(repository)  # type: ignore[arg-type]
    assembler = ContextAssembler(
        ContextConfig(ModelEstimator("test-v1"), 128, 16, {}),
        store,
        now=lambda: NOW,
    )
    manifest = await assembler.assemble(
        "run-1",
        "step-1",
        (
            ContextItem(
                ContextLayer.CONTROL,
                "system",
                TrustLevel.SYSTEM,
                content="typed actions only",
            ),
        ),
    )

    loaded = await store.load(manifest.id)

    assert loaded == manifest


async def test_repository_manifest_store_rejects_tampered_hash() -> None:
    repository = Repository()
    store = RepositoryManifestStore(repository)  # type: ignore[arg-type]
    assembler = ContextAssembler(
        ContextConfig(ModelEstimator("test-v1"), 128, 16, {}),
        store,
        now=lambda: NOW,
    )
    manifest = await assembler.assemble(
        "run-1",
        "step-1",
        (
            ContextItem(
                ContextLayer.CONTROL,
                "system",
                TrustLevel.SYSTEM,
                content="typed actions only",
            ),
        ),
    )
    assert repository.stored is not None
    repository.stored = replace(repository.stored, content_hash="sha256:" + "0" * 64)

    with pytest.raises(RuntimeError, match="integrity"):
        await store.load(manifest.id)
