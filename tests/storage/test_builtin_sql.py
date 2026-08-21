from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from docreview.storage.postgres.builtin import (
    AUTHORIZED_EVIDENCE_SQL,
    AUTHORIZED_NODES_SQL,
    COMMIT_SCOPE_SQL,
    DOCUMENT_NODES_SQL,
    DOCUMENT_SEARCH_SQL,
    DOCUMENT_VERSION_SQL,
    EVIDENCE_LEXICAL_SQL,
    EVIDENCE_SCOPE_SQL,
    EVIDENCE_SEMANTIC_SQL,
    WINDOW_SIBLINGS_SQL,
    PostgresCanonicalDocumentRepository,
)


class Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.query = ""
        self.params: tuple[object, ...] = ()

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.query = query
        self.params = params

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []

    async def __aenter__(self) -> Cursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> Cursor:
        return self._cursor

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Pool:
    def __init__(self, cursor: Cursor) -> None:
        self._connection = Connection(cursor)

    def connection(self) -> Connection:
        return self._connection


@pytest.mark.asyncio
async def test_canonical_version_query_is_workspace_scoped_and_version_bound() -> None:
    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    cursor = Cursor(("version-1", "resource-1", 2, "upload", created_at))
    repository = PostgresCanonicalDocumentRepository(cast(Any, Pool(cursor)))

    value = await repository.resolve_version("workspace-1", "resource-1", "version-1")

    assert value is not None
    assert value.workspace_id == "workspace-1"
    assert value.id == "version-1"
    assert cursor.query == DOCUMENT_VERSION_SQL
    assert cursor.params == ("workspace-1", "resource-1", "version-1", "version-1")
    assert "resource.workspace_id = %s" in DOCUMENT_VERSION_SQL


def test_builtin_sql_keeps_scope_predicates_and_profile_filters_in_database() -> None:
    assert "%s::text IS NULL OR version.id::text = %s::text" in DOCUMENT_VERSION_SQL
    assert "node.workspace_id = %s" in DOCUMENT_NODES_SQL
    assert "node.workspace_id = %s" in DOCUMENT_SEARCH_SQL
    assert "node.page_mapping_json" in DOCUMENT_NODES_SQL
    assert "resource.workspace_id = %s" in EVIDENCE_SCOPE_SQL
    assert "%s::text IS NULL OR version.id::text = %s::text" in EVIDENCE_SCOPE_SQL
    assert "resource.workspace_id = %s" in EVIDENCE_LEXICAL_SQL
    for query in (EVIDENCE_LEXICAL_SQL, EVIDENCE_SEMANTIC_SQL, WINDOW_SIBLINGS_SQL):
        assert "COALESCE(chunk.canonical_node_id, chunk.id::text)" in query
    assert "embedding_status = 'ready'" in EVIDENCE_SEMANTIC_SQL
    assert "chunk.embedding_profile = %s" in EVIDENCE_SEMANTIC_SQL
    assert "run.id = %s AND step.id = %s" in COMMIT_SCOPE_SQL
    assert "node.workspace_id = %s" in AUTHORIZED_NODES_SQL
    assert "observation.run_id = %s" in AUTHORIZED_EVIDENCE_SQL


def test_evidence_lexical_sql_falls_back_to_bounded_individual_query_terms() -> None:
    normalized = " ".join(EVIDENCE_LEXICAL_SQL.lower().split())

    assert "regexp_split_to_table" in normalized
    assert "length(term.value) >= 2" in normalized
    assert "like '%%' || term.value || '%%'" in normalized
    assert "greatest(" in normalized
    assert "then 0.25" in normalized
