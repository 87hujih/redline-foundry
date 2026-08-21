"""Explicitly scoped, dry-run-first historical document structure operations.

This module deliberately contains no pool construction, scheduler hook, or import
side effect. An operator must provide a scoped repository and explicit execute
authorization after the separate database-operation approval gate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from docreview.knowledge.chunking import REVIEW_STRUCTURE_PROFILE


class StructureDisposition(StrEnum):
    PROJECTION_ONLY = "projection_only"
    REINGEST_REQUIRED = "reingest_required"
    SOURCE_ARTIFACT_UNAVAILABLE = "source_artifact_unavailable"


@dataclass(frozen=True, slots=True)
class HistoricalVersion:
    workspace_id: str
    resource_id: str
    version_id: str
    source_artifact_hash: str | None
    parser_profile: str
    chunk_profile: str
    has_heading_tree: bool
    has_list_table_nodes: bool
    has_source_spans: bool


@dataclass(frozen=True, slots=True)
class StructureAudit:
    version: HistoricalVersion
    disposition: StructureDisposition
    reason: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class ReprojectionRequest:
    audit: StructureAudit
    execute: bool = False


@dataclass(frozen=True, slots=True)
class ReprojectionResult:
    operation_id: str
    disposition: StructureDisposition
    executed: bool
    new_version_id: str | None = None


class HistoricalDocumentRepository(Protocol):
    async def projection_only_rebuild(
        self, *, workspace_id: str, resource_id: str, version_id: str, chunk_profile: str
    ) -> None: ...

    async def source_backed_reingest(
        self,
        *,
        workspace_id: str,
        resource_id: str,
        prior_version_id: str,
        source_artifact_hash: str,
        parser_profile: str,
        chunk_profile: str,
        idempotency_key: str,
    ) -> str: ...


class HistoricalStructureOperations:
    def audit(self, version: HistoricalVersion) -> StructureAudit:
        _validate_version(version)
        operation_id = _operation_id(version)
        if not version.source_artifact_hash:
            return StructureAudit(
                version,
                StructureDisposition.SOURCE_ARTIFACT_UNAVAILABLE,
                "source_artifact_unavailable",
                operation_id,
            )
        if version.has_heading_tree and version.has_list_table_nodes and version.has_source_spans:
            return StructureAudit(
                version,
                StructureDisposition.PROJECTION_ONLY,
                "canonical_ast_retains_required_structure",
                operation_id,
            )
        return StructureAudit(
            version,
            StructureDisposition.REINGEST_REQUIRED,
            "canonical_ast_lost_required_structure",
            operation_id,
        )

    async def run(
        self, request: ReprojectionRequest, repository: HistoricalDocumentRepository
    ) -> ReprojectionResult:
        audit = request.audit
        _validate_version(audit.version)
        if not request.execute:
            return ReprojectionResult(audit.operation_id, audit.disposition, False)
        if audit.disposition is StructureDisposition.SOURCE_ARTIFACT_UNAVAILABLE:
            raise RuntimeError("source_artifact_unavailable")
        version = audit.version
        if audit.disposition is StructureDisposition.PROJECTION_ONLY:
            await repository.projection_only_rebuild(
                workspace_id=version.workspace_id,
                resource_id=version.resource_id,
                version_id=version.version_id,
                chunk_profile=REVIEW_STRUCTURE_PROFILE.profile_id,
            )
            return ReprojectionResult(audit.operation_id, audit.disposition, True)
        source_hash = version.source_artifact_hash
        if source_hash is None:
            raise RuntimeError("source_artifact_unavailable")
        new_version_id = await repository.source_backed_reingest(
            workspace_id=version.workspace_id,
            resource_id=version.resource_id,
            prior_version_id=version.version_id,
            source_artifact_hash=source_hash,
            parser_profile=version.parser_profile,
            chunk_profile=REVIEW_STRUCTURE_PROFILE.profile_id,
            idempotency_key=audit.operation_id,
        )
        return ReprojectionResult(audit.operation_id, audit.disposition, True, new_version_id)


def _operation_id(version: HistoricalVersion) -> str:
    artifact = version.source_artifact_hash or "source_artifact_unavailable"
    value = "\0".join(
        (
            version.workspace_id,
            version.resource_id,
            version.version_id,
            artifact,
            version.parser_profile,
            REVIEW_STRUCTURE_PROFILE.profile_id,
        )
    )
    return "doc-reprojection:" + hashlib.sha256(value.encode()).hexdigest()


def _validate_version(version: HistoricalVersion) -> None:
    if (
        not version.workspace_id.strip()
        or not version.resource_id.strip()
        or not version.version_id.strip()
        or not version.parser_profile.strip()
        or not version.chunk_profile.strip()
    ):
        raise ValueError("历史 文档 操作 范围 无效")
    if version.source_artifact_hash is not None and not version.source_artifact_hash.startswith(
        "sha256:"
    ):
        raise ValueError("来源 制品 哈希 无效")


__all__ = [
    "HistoricalDocumentRepository",
    "HistoricalStructureOperations",
    "HistoricalVersion",
    "ReprojectionRequest",
    "ReprojectionResult",
    "StructureAudit",
    "StructureDisposition",
]
