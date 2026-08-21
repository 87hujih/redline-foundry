from __future__ import annotations

import asyncio

from docreview.document.ingestion import ingest
from docreview.document.model import NodeType, flatten
from docreview.document.parser import DocumentParser
from docreview.knowledge.chunking import (
    REVIEW_STRUCTURE_PROFILE,
    DeterministicTokenizer,
    build_chunks,
)


def test_structured_ast_preserves_heading_tree_clause_list_and_table() -> None:
    content = (
        "# \u5408\u540c\n\n\u7b2c\u4e00\u7ae0 \u603b\u5219\n\n"
        "\u7b2c\u4e00\u6761 \u9002\u7528\u8303\u56f4\n"
        "\u672c\u6761\u6b3e\u9002\u7528\u4e8e\u5168\u90e8\u670d\u52a1\u3002\n\n"
        "- \u7532\u65b9\u5e94\u4ed8\u6b3e\n- \u4e59\u65b9\u5e94\u4ea4\u4ed8\n\n"
        "| \u9879\u76ee | \u91d1\u989d |\n| --- | --- |\n| \u670d\u52a1\u8d39 | 100 \u5143 |\n"
    )
    ingested = asyncio.run(
        ingest(
            DocumentParser(mode="structured"),
            document_id="resource-1",
            version_id="version-1",
            file_name="contract.md",
            content=content.encode(),
        )
    )

    nodes = flatten(ingested.document.root)
    assert [node.type for node in nodes].count(NodeType.HEADING) == 3
    assert any(node.type is NodeType.LIST for node in nodes)
    assert any(node.type is NodeType.LIST_ITEM for node in nodes)
    assert any(node.type is NodeType.TABLE for node in nodes)
    clause = next(
        node for node in nodes if node.content == "\u7b2c\u4e00\u6761 \u9002\u7528\u8303\u56f4"
    )
    assert clause.metadata["section_type"] == "clause"
    assert (
        clause.children[0].content
        == "\u672c\u6761\u6b3e\u9002\u7528\u4e8e\u5168\u90e8\u670d\u52a1\u3002"
    )


def test_token_aware_chunks_have_stable_profile_metadata_and_parent_windows() -> None:
    words = " ".join(f"token-{index}" for index in range(1400))
    content = f"# Review\n\n## Long section\n\n{words}"
    document = asyncio.run(
        ingest(
            DocumentParser(mode="structured"),
            document_id="resource-1",
            version_id="version-1",
            file_name="review.md",
            content=content.encode(),
        )
    ).document
    tokenizer = DeterministicTokenizer.for_testing()

    first = build_chunks(document, tokenizer=tokenizer)
    second = build_chunks(document, tokenizer=tokenizer)

    assert first == second
    assert len({chunk.window_group_id for chunk in first}) > 1
    assert all(
        tokenizer.count(chunk.embedding_text) <= REVIEW_STRUCTURE_PROFILE.child_hard_max_tokens
        for chunk in first
    )
    assert all(
        chunk.content_hash in {node.content_hash for node in flatten(document.root)}
        for chunk in first
    )
    assert all(
        chunk.metadata["profile_id"] == REVIEW_STRUCTURE_PROFILE.profile_id for chunk in first
    )
    assert all(chunk.metadata["fragment_hash"].startswith("sha256:") for chunk in first)
    assert all(chunk.metadata["embedding_text_hash"].startswith("sha256:") for chunk in first)
    assert all(chunk.metadata["source_spans"] for chunk in first)
    assert all("heading_path" in chunk.metadata for chunk in first)


def test_overlap_is_restricted_to_forced_atom_splits_and_never_changes_citation_content() -> None:
    content = "# Review\n\n" + "x" * 4000
    document = asyncio.run(
        ingest(
            DocumentParser(mode="structured"),
            document_id="resource-1",
            version_id="version-1",
            file_name="review.md",
            content=content.encode(),
        )
    ).document
    chunks = build_chunks(document, tokenizer=DeterministicTokenizer.for_testing())

    assert len(chunks) > 1
    assert all("overlap_prefix" not in chunk.content for chunk in chunks)
    assert all(chunk.metadata["overlap_prefix_tokens"] <= 48 for chunk in chunks)
    assert any(chunk.metadata["boundary_reason"] == "forced_token_split" for chunk in chunks)


def test_table_rows_and_list_items_are_not_crossed_by_children() -> None:
    rows = "\n".join(f"| row-{index} | value-{index} |" for index in range(180))
    content = "# Review\n\n- first item\n- second item\n\n| key | value |\n| --- | --- |\n" + rows
    document = asyncio.run(
        ingest(
            DocumentParser(mode="structured"),
            document_id="resource-1",
            version_id="version-1",
            file_name="review.md",
            content=content.encode(),
        )
    ).document
    chunks = build_chunks(document, tokenizer=DeterministicTokenizer.for_testing())

    table_chunks = [chunk for chunk in chunks if chunk.chunk_role == "table"]
    list_chunks = [chunk for chunk in chunks if chunk.chunk_role == "list"]
    assert table_chunks and list_chunks
    assert all("key | value" in chunk.embedding_text for chunk in table_chunks)
    assert all(chunk.metadata["chunk_role"] == "table" for chunk in table_chunks)
    assert all(chunk.metadata["chunk_role"] == "list" for chunk in list_chunks)
