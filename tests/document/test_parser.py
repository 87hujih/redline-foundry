from __future__ import annotations

from pathlib import Path

import pytest

from docreview.document.ingestion import ingest
from docreview.document.parser import (
    DocumentParser,
    DocumentTooLargeError,
    EmptyDocumentError,
    UnsupportedFileTypeError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "documents"


class FixtureTika:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def parse(self, file_name: str, content: bytes) -> str:
        self.calls.append((file_name, content))
        response = (
            "tika_pdf_response.txt"
            if file_name.lower().endswith(".pdf")
            else "tika_docx_response.txt"
        )
        return (FIXTURES / response).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_markdown_fixture_preserves_source_metadata() -> None:
    content = (FIXTURES / "markdown_fixture.md").read_bytes()
    parser = DocumentParser(max_bytes=1024)

    parsed = await parser.parse("markdown_fixture.md", content)
    ingested = await ingest(
        parser,
        document_id="resource-1",
        version_id="version-1",
        file_name="markdown_fixture.md",
        content=content,
    )

    assert parsed.source_format == "markdown"
    assert parsed.file_name == "markdown_fixture.md"
    assert parsed.parser_name == "text"
    assert parsed.quality_flags == []
    assert [(block.type, block.text, block.level) for block in parsed.blocks] == [
        ("heading", "学生守则", 1),
        ("paragraph", "这里是正文。", 0),
        ("paragraph", "第二段说明。", 0),
    ]
    assert ingested.document.root.source_location.file_name == "markdown_fixture.md"
    assert ingested.document.root.page_mapping == []
    assert ingested.document.metadata == {"file_name": "markdown_fixture.md", "quality_flags": []}
    assert [
        (section.section_key, section.type, section.title, section.content, section.metadata)
        for section in ingested.normalized.sections
    ] == [("section-1", "section", "学生守则", "这里是正文。\n第二段说明。", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "source_format", "expected_flags"),
    [
        ("document_fixture.docx", "docx", []),
        ("document_fixture.pdf", "pdf", ["too_many_short_blocks"]),
    ],
)
async def test_docx_and_pdf_fixtures_cross_tika_boundary(
    file_name: str, source_format: str, expected_flags: list[str]
) -> None:
    tika = FixtureTika()
    parser = DocumentParser(mode="tika", tika=tika, max_bytes=1024)
    content = (FIXTURES / file_name).read_bytes()

    parsed = await parser.parse(file_name, content)

    assert tika.calls == [(file_name, content)]
    assert parsed.source_format == source_format
    assert parsed.parser_name == "tika"
    assert parsed.file_name == file_name
    assert parsed.quality_flags == expected_flags
    assert all(block.type == "paragraph" for block in parsed.blocks)


def test_parser_modes_preserve_extension_policy() -> None:
    text = DocumentParser()
    tika = DocumentParser(mode="tika", tika=FixtureTika())

    assert text.supported_extensions == (".md", ".txt")
    assert tika.supported_extensions == (
        ".md",
        ".txt",
        ".doc",
        ".docx",
        ".pdf",
        ".rtf",
        ".odt",
    )
    assert text.supports("notes.MD")
    assert not text.supports("contract.pdf")
    assert tika.supports("contract.pdf")
    assert not tika.supports("archive.zip")
    assert text.supports(" notes.MD ")
    assert text.unsupported_message("contract.pdf") == (
        "当前服务仅支持 md、txt；pdf/docx 等文件需要启用 Tika 解析。"  # noqa: RUF001
    )
    assert tika.unsupported_message("archive") == (
        "不支持的文件格式：(无扩展名)。当前支持：md、txt、doc、docx、pdf、rtf、odt。"  # noqa: RUF001
    )


@pytest.mark.asyncio
async def test_plain_text_parser_normalizes_lines() -> None:
    parsed = await DocumentParser(max_bytes=1024).parse(
        "notes.txt", b" first line \r\n\r\n second line "
    )

    assert parsed.source_format == "text"
    assert [(block.type, block.text, block.level) for block in parsed.blocks] == [
        ("paragraph", "first line", 0),
        ("paragraph", "second line", 0),
    ]


@pytest.mark.asyncio
async def test_blank_tika_pdf_preserves_ocr_quality_flags() -> None:
    class BlankTika:
        async def parse(self, file_name: str, content: bytes) -> str:
            return "   \n\t"

    parsed = await DocumentParser(mode="tika", tika=BlankTika()).parse("scan.pdf", b"%PDF fixture")

    assert parsed.blocks == []
    assert parsed.quality_flags == ["text_empty", "requires_ocr"]


@pytest.mark.asyncio
async def test_parser_rejects_empty_oversize_and_unsupported_documents() -> None:
    parser = DocumentParser(max_bytes=4)

    with pytest.raises(EmptyDocumentError):
        await parser.parse("empty.md", b"")
    with pytest.raises(DocumentTooLargeError):
        await parser.parse("large.txt", b"12345")
    with pytest.raises(UnsupportedFileTypeError):
        await parser.parse("archive.zip", b"zip")


@pytest.mark.asyncio
async def test_text_parser_keeps_utf8_and_heading_rules_deterministic() -> None:
    parser = DocumentParser(max_bytes=1024)

    parsed = await parser.parse(
        "rules.md",
        b"\xef\xbb\xbf# BOM is content\n####### Seven\n#not-heading\n\nbody",
    )

    assert [(block.type, block.text, block.level) for block in parsed.blocks] == [
        ("paragraph", "\ufeff# BOM is content", 0),
        ("heading", "Seven", 7),
        ("paragraph", "#not-heading", 0),
        ("paragraph", "body", 0),
    ]
