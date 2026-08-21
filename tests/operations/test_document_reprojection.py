from __future__ import annotations

import pytest

from docreview.operations.document_reprojection import (
    HistoricalStructureOperations,
    HistoricalVersion,
    ReprojectionRequest,
    StructureDisposition,
)


class Repository:
    def __init__(self) -> None:
        self.rebuilds: list[tuple[str, str, str, str]] = []
        self.reingests: list[dict[str, str]] = []

    async def projection_only_rebuild(self, **kwargs: str) -> None:
        self.rebuilds.append(
            (
                kwargs["workspace_id"],
                kwargs["resource_id"],
                kwargs["version_id"],
                kwargs["chunk_profile"],
            )
        )

    async def source_backed_reingest(self, **kwargs: str) -> str:
        self.reingests.append(kwargs)
        return "version-2"


def version(**changes: object) -> HistoricalVersion:
    fields: dict[str, object] = {
        "workspace_id": "workspace-1",
        "resource_id": "resource-1",
        "version_id": "version-1",
        "source_artifact_hash": "sha256:" + "1" * 64,
        "parser_profile": "legacy-text-v1",
        "chunk_profile": "chunk-v1",
        "has_heading_tree": True,
        "has_list_table_nodes": True,
        "has_source_spans": True,
    }
    fields.update(changes)
    return HistoricalVersion(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_historical_audit_is_dry_run_then_projection_or_reingestion() -> None:
    operations = HistoricalStructureOperations()
    repository = Repository()
    projection_audit = operations.audit(version())
    reingest_audit = operations.audit(version(has_list_table_nodes=False))

    assert projection_audit.disposition is StructureDisposition.PROJECTION_ONLY
    assert reingest_audit.disposition is StructureDisposition.REINGEST_REQUIRED
    assert (
        await operations.run(ReprojectionRequest(projection_audit), repository)
    ).executed is False
    result = await operations.run(ReprojectionRequest(reingest_audit, execute=True), repository)
    assert result.new_version_id == "version-2"
    assert repository.reingests[0]["prior_version_id"] == "version-1"
    assert repository.reingests[0]["idempotency_key"] == reingest_audit.operation_id


@pytest.mark.asyncio
async def test_historical_audit_fails_closed_when_source_artifact_is_unavailable() -> None:
    operations = HistoricalStructureOperations()
    audit = operations.audit(version(source_artifact_hash=None, has_heading_tree=False))

    assert audit.disposition is StructureDisposition.SOURCE_ARTIFACT_UNAVAILABLE
    with pytest.raises(RuntimeError, match="source_artifact_unavailable"):
        await operations.run(ReprojectionRequest(audit, execute=True), Repository())
