from __future__ import annotations

from typing import Any

import pytest

from docreview.document.ingestion import ingest
from docreview.document.model import Document
from docreview.document.parser import DocumentParser
from docreview.document.patch import Operation, PatchSet
from docreview.knowledge.chunking import build_projection
from docreview.storage.postgres.document_commit import (
    ADVISORY_LOCK_SQL,
    GET_IDEMPOTENCY_SQL,
    INSERT_CHUNK_SQL,
    INSERT_COMMIT_SQL,
    INSERT_DOCUMENT_SQL,
    INSERT_NODE_SQL,
    INSERT_OUTBOX_SQL,
    INSERT_SECTION_SQL,
    INSERT_VERSION_SQL,
    LOCK_NODE_HASH_SQL,
    LOCK_RESOURCE_SQL,
    PostgresCommitStore,
    insert_canonical_projection,
)


def test_canonical_commit_sql_has_lock_bundle_and_transactional_outbox() -> None:
    assert "pg_advisory_xact_lock" in ADVISORY_LOCK_SQL
    assert "document_patch_commits" in GET_IDEMPOTENCY_SQL
    assert "FOR UPDATE OF resource, version" in LOCK_RESOURCE_SQL
    assert "FOR UPDATE" in LOCK_NODE_HASH_SQL
    assert "canonical_documents" in INSERT_DOCUMENT_SQL
    assert "document_nodes" in INSERT_NODE_SQL
    assert "resource_sections" in INSERT_SECTION_SQL
    assert "resource_chunks" in INSERT_CHUNK_SQL
    assert "document.version.committed" in INSERT_OUTBOX_SQL
    assert "document_patch_commits" in INSERT_COMMIT_SQL


class Cursor:
    def __init__(self, *, node_hash: str) -> None:
        self.node_hash = node_hash
        self.last_query = ""
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.section = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.last_query = query
        self.executions.append((query, params))

    async def fetchone(self) -> tuple[object, ...] | None:
        if self.last_query == GET_IDEMPOTENCY_SQL:
            return None
        if self.last_query == LOCK_RESOURCE_SQL:
            return ("resource-1", "version-1", 1)
        if self.last_query == LOCK_NODE_HASH_SQL:
            return (self.node_hash,)
        if self.last_query == INSERT_SECTION_SQL:
            self.section += 1
            return (f"section-{self.section}",)
        if self.last_query == INSERT_OUTBOX_SQL:
            return ("outbox-1",)
        return None


class Transaction:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: object, *args: object) -> None:
        self.rolled_back = exc_type is not None


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self.cursor_value = cursor
        self.transaction_value = Transaction()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def cursor(self) -> Cursor:
        return self.cursor_value

    def transaction(self, *args: object, **kwargs: object) -> Transaction:
        return self.transaction_value


class Pool:
    def __init__(self, node_hash: str) -> None:
        self.connection_value = Connection(Cursor(node_hash=node_hash))

    def connection(self) -> Connection:
        return self.connection_value


async def document() -> Document:
    return (
        await ingest(
            DocumentParser(),
            document_id="resource-1",
            version_id="version-2",
            file_name="sample.md",
            content=b"# Heading\n\nBody",
        )
    ).document


def patch(node_id: str, node_hash: str) -> PatchSet:
    return PatchSet(
        "1.0",
        "resource-1",
        "version-1",
        [Operation("replace_node", node_id, node_hash, content="updated")],
        [],
        "test",
    )


@pytest.mark.anyio
async def test_canonical_commit_rechecks_hash_and_writes_complete_atomic_bundle() -> None:
    value = await document()
    node = value.root.children[0]
    pool = Pool(node.content_hash)
    result = await PostgresCommitStore(AnyPool(pool)).commit_atomic(
        workspace_id="workspace-1",
        resource_id="resource-1",
        base_version_id="version-1",
        idempotency_key="commit-1",
        patch_hash="sha256:" + "1" * 64,
        patch=patch(node.node_id, node.content_hash),
        expected_hashes={node.node_id: node.content_hash},
        document=value,
        actor_id="actor-1",
    )

    assert result.created is True and result.outbox_id == "outbox-1"
    queries = [query for query, _params in pool.connection_value.cursor_value.executions]
    assert queries[0] == "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    assert ADVISORY_LOCK_SQL in queries
    assert LOCK_NODE_HASH_SQL in queries
    assert INSERT_DOCUMENT_SQL in queries
    assert INSERT_NODE_SQL in queries
    assert INSERT_SECTION_SQL in queries
    assert INSERT_CHUNK_SQL in queries
    assert queries.index(INSERT_OUTBOX_SQL) < queries.index(INSERT_COMMIT_SQL)


@pytest.mark.anyio
async def test_canonical_commit_hash_conflict_rolls_back_before_bundle_write() -> None:
    value = await document()
    node = value.root.children[0]
    pool = Pool("sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="hash conflict"):
        await PostgresCommitStore(AnyPool(pool)).commit_atomic(
            workspace_id="workspace-1",
            resource_id="resource-1",
            base_version_id="version-1",
            idempotency_key="commit-1",
            patch_hash="sha256:" + "1" * 64,
            patch=patch(node.node_id, node.content_hash),
            expected_hashes={node.node_id: node.content_hash},
            document=value,
            actor_id="actor-1",
        )

    queries = [query for query, _params in pool.connection_value.cursor_value.executions]
    assert INSERT_VERSION_SQL not in queries
    assert pool.connection_value.transaction_value.rolled_back is True


@pytest.mark.anyio
async def test_canonical_projection_uses_zero_page_range_for_unpaged_chunks() -> None:
    value = await document()
    cursor = Cursor(node_hash="unused")

    await insert_canonical_projection(
        cursor,
        workspace_id="workspace-1",
        resource_id=value.document_id,
        version_id=value.version_id,
        document=value,
        projection=build_projection(value),
        renderer_profile="canonical-v1",
        chunk_profile="docreview-review-structure-2026-08-17",
        embedding_profile="embedding-v1",
    )

    chunk_params = [params for query, params in cursor.executions if query == INSERT_CHUNK_SQL]
    assert chunk_params
    assert all(params[10:12] == (0, 0) for params in chunk_params)


class AnyPool:
    def __init__(self, value: Pool) -> None:
        self.value = value

    def connection(self) -> Any:
        return self.value.connection()
