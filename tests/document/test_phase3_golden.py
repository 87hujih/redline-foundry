from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docreview.document.commit import CommitResult, CommitSnapshot, StoredCommit, commit
from docreview.document.ingestion import ingest
from docreview.document.model import flatten, hash_node
from docreview.document.parser import (
    DocumentParser,
    UnsupportedFileTypeError,
)
from docreview.document.patch import Operation, PatchSet, parse_strict, patch_hash
from docreview.knowledge.chunking import build_chunks
from docreview.knowledge.evidence import (
    Candidate,
    RerankResult,
    RetrievalConfig,
    ScoredCandidate,
    citations,
    retrieve,
)
from docreview.storage.filestore import LocalFileStore

FIXTURE = b"# Intro\n\nHello world.\n\n## Details\n\nPython pgvector."
GOLDEN = json.loads(
    Path(__file__).parents[1].joinpath("fixtures", "phase3_golden.json").read_text()
)


def test_golden_ast_node_ids_hashes_chunks() -> None:
    document = asyncio.run(
        ingest(
            DocumentParser(),
            document_id="resource-1",
            version_id="version-1",
            file_name="sample.md",
            content=FIXTURE,
        )
    ).document
    nodes = flatten(document.root)
    assert [node.node_id for node in nodes] == GOLDEN["document"]["node_ids"]
    assert [hash_node(node) for node in nodes] == GOLDEN["document"]["node_hashes"]
    assert document.content_hash == GOLDEN["document"]["content_hash"]
    assert [chunk.content for chunk in build_chunks(document)] == GOLDEN["document"]["chunks"]


def test_parser_boundaries_preserve_tika_error() -> None:
    parser = DocumentParser()
    with pytest.raises(UnsupportedFileTypeError):
        asyncio.run(parser.parse("contract.pdf", b"pdf"))

    class BrokenTika:
        async def parse(self, file_name: str, content: bytes) -> str:
            raise TimeoutError

    with pytest.raises(TimeoutError):
        asyncio.run(DocumentParser(mode="tika", tika=BrokenTika()).parse("contract.pdf", b"pdf"))


def test_content_addressed_store_reuses_bytes(tmp_path: Path) -> None:
    async def run() -> None:
        store = LocalFileStore(tmp_path)
        first = await store.save(b"same")
        second = await store.save(b"same")
        assert first.storage_key == second.storage_key
        assert first.sha256 == hashlib.sha256(b"same").hexdigest()
        stream = await store.open(first.storage_key)
        assert stream.read() == b"same"
        stream.close()

    asyncio.run(run())


def test_strict_patch_hash_and_conflict() -> None:
    raw = {
        "schema_version": "1.0",
        "resource_id": "resource-1",
        "base_version_id": "version-1",
        "operations": [
            {
                "op": "replace_node",
                "node_id": "node_1",
                "expected_hash": "sha256:" + "0" * 64,
                "content": "updated",
            }
        ],
        "evidence_refs": ["ev_1"],
        "reason": "golden",
    }
    patch = parse_strict(json.dumps(raw).encode())
    assert patch_hash(patch) == GOLDEN["patch_hash"]
    duplicate = json.dumps(raw).replace(
        '"reason": "golden"', '"reason": "golden", "reason": "duplicate"'
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_strict(duplicate.encode())
    with pytest.raises(ValueError):
        parse_strict((json.dumps(raw) + json.dumps(raw)).encode())


class FakeCommitStore:
    def __init__(self, snapshot: CommitSnapshot) -> None:
        self.snapshot = snapshot
        self.commits: dict[str, StoredCommit] = {}
        self.atomic_calls = 0

    async def get_commit(self, workspace_id: str, idempotency_key: str) -> StoredCommit | None:
        return self.commits.get(idempotency_key)

    async def load_snapshot(self, workspace_id: str, resource_id: str) -> CommitSnapshot:
        return self.snapshot

    async def commit_atomic(self, **kwargs: object) -> CommitResult:
        self.atomic_calls += 1
        result = CommitResult("resource-1", "version-2", "outbox-1", True)
        self.commits[str(kwargs["idempotency_key"])] = StoredCommit(
            str(kwargs["patch_hash"]), result
        )
        return result


def test_commit_same_key_replay_and_conflict() -> None:
    async def run() -> None:
        document = (
            await ingest(
                DocumentParser(),
                document_id="resource-1",
                version_id="version-1",
                file_name="sample.md",
                content=FIXTURE,
            )
        ).document
        operation = Operation(
            "replace_node",
            document.root.children[0].node_id,
            document.root.children[0].content_hash,
            content="Changed",
        )
        patch = PatchSet("1.0", "resource-1", "version-1", [operation], [], "edit")
        store = FakeCommitStore(
            CommitSnapshot(document, "version-1", frozenset({operation.node_id}), frozenset())
        )
        first = await commit(
            store=store,
            workspace_id="workspace-1",
            resource_id="resource-1",
            idempotency_key="key-1",
            actor_id="actor-1",
            patch=patch,
        )
        second = await commit(
            store=store,
            workspace_id="workspace-1",
            resource_id="resource-1",
            idempotency_key="key-1",
            actor_id="actor-1",
            patch=patch,
        )
        assert first.created is True and second.created is False and store.atomic_calls == 1
        with pytest.raises(RuntimeError, match="idempotency"):
            await commit(
                store=store,
                workspace_id="workspace-1",
                resource_id="resource-1",
                idempotency_key="key-1",
                actor_id="actor-1",
                patch=PatchSet(
                    "1.0",
                    "resource-1",
                    "version-1",
                    [
                        Operation(
                            "replace_node",
                            operation.node_id,
                            operation.expected_hash,
                            content="other",
                        )
                    ],
                    [],
                    "different",
                ),
            )

    asyncio.run(run())


class FakeRetrieval:
    async def lexical(
        self, workspace_id: str, resource_id: str, version_id: str, query: str, limit: int
    ) -> list[ScoredCandidate]:
        return [
            ScoredCandidate(
                Candidate(
                    "source-1",
                    resource_id,
                    version_id,
                    "node-1",
                    "upload",
                    "Hello world.",
                    datetime.now(UTC),
                ),
                0.8,
            )
        ]

    async def semantic(
        self, workspace_id: str, resource_id: str, version_id: str, vector: list[float], limit: int
    ) -> list[ScoredCandidate]:
        raise RuntimeError("provider unavailable")


class FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0]


class BrokenReranker:
    async def rerank(self, query: str, documents: list[str], limit: int) -> list[RerankResult]:
        raise RuntimeError("reranker unavailable")


def test_retrieval_degradation_and_citation_provenance() -> None:
    async def run() -> None:
        evidence = await retrieve(
            repository=FakeRetrieval(),
            embedder=FakeEmbedder(),
            reranker=BrokenReranker(),
            workspace_id="workspace-1",
            resource_id="resource-1",
            version_id="version-1",
            query="hello",
            config=RetrievalConfig(rerank_enabled=True, rerank_model="model"),
        )
        assert evidence.evidence[0].degraded_reason == "reranker_failed"
        assert citations(evidence)[0].provenance_id.startswith("sha256:")
        assert any(item.get("status") == "degraded" for item in evidence.process)

    asyncio.run(run())
