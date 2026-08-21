from __future__ import annotations

import pytest

from docreview.document.ingestion import ingest
from docreview.document.parser import DocumentParser
from docreview.document.tika import XHTMLStructureError, parse_tika_xhtml


@pytest.mark.asyncio
async def test_markdown_and_text_structure_do_not_promote_invalid_or_ordinary_numbers() -> None:
    parser = DocumentParser(mode="structured")
    markdown = await parser.parse("rules.md", b"# One\n####### seven\n\n- item\n- second")
    text = await parser.parse(
        "rules.txt",
        "\u7b2c\u4e00\u7ae0 \u603b\u5219\n\u7b2c\u4e00\u6761 \u8303\u56f4\n"
        "\u5185\u5bb9\u3002\n\n2026 \u5e74\u9884\u7b97\u4e3a 100\u3002".encode(),
    )

    assert [item.element_type for item in markdown.elements] == ["heading", "paragraph", "list"]
    assert markdown.elements[0].level == 1
    assert markdown.elements[1].text == "####### seven"
    assert [item.element_type for item in text.elements] == [
        "heading",
        "heading",
        "paragraph",
        "paragraph",
    ]
    assert text.elements[-1].text == "2026 \u5e74\u9884\u7b97\u4e3a 100\u3002"


def test_xhtml_structure_is_bounded_and_rejects_entity_expansion_inputs() -> None:
    elements = parse_tika_xhtml(
        b"<html><body><h1>Title</h1><p>Body</p><ul><li>Item</li></ul></body></html>"
    )
    assert [item.element_type for item in elements] == ["heading", "paragraph", "list"]

    with pytest.raises(XHTMLStructureError):
        parse_tika_xhtml(b"<!DOCTYPE doc [<!ENTITY x 'boom'>]><p>&x;</p>")


@pytest.mark.asyncio
async def test_ingestion_marks_nonempty_but_abnormally_short_extracted_text() -> None:
    result = await ingest(
        DocumentParser(mode="structured"),
        document_id="document-1",
        version_id="version-1",
        file_name="short.md",
        content="这里是正文。第二段说明。".encode(),
    )

    assert "text_short" in result.document.metadata["quality_flags"]
    assert "text_short" in result.document.root.metadata["quality_flags"]
