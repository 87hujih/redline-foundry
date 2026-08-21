"""Bounded, deterministic document parsing into structured source elements.

``Block`` remains the legacy compatibility view used by older upload tests. New
production ingestion consumes ``ParsedElement`` exclusively so structural facts
are fixed before canonical AST hashing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from docreview.document.model import PageMapping


class UnsupportedFileTypeError(ValueError):
    pass


class ParserUnavailableError(RuntimeError):
    pass


class EmptyDocumentError(ValueError):
    pass


class DocumentTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Block:
    """Legacy flat block projection. It is not a source of canonical structure."""

    type: str
    text: str
    level: int = 0


@dataclass(frozen=True, slots=True)
class ParsedElement:
    element_type: str
    text: str
    level: int = 0
    attributes: dict[str, object] = field(default_factory=lambda: {})
    children: tuple[ParsedElement, ...] = ()
    source_start_offset: int = 0
    source_end_offset: int = 0
    start_line: int = 0
    end_line: int = 0
    page_mappings: tuple[PageMapping, ...] = ()
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_format: str
    blocks: list[Block]
    file_name: str
    parser_name: str
    quality_flags: list[str]
    elements: list[ParsedElement] = field(default_factory=lambda: [])


class TikaClient(Protocol):
    async def parse(self, file_name: str, content: bytes) -> str: ...


class StructuredTikaClient(TikaClient, Protocol):
    async def parse_structured(self, file_name: str, content: bytes) -> str: ...


TEXT_EXTENSIONS = (".md", ".txt")
TIKA_EXTENSIONS = (".doc", ".docx", ".pdf", ".rtf", ".odt")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_LIST_ITEM = re.compile(r"^(?P<indent>\s*)(?P<marker>[-+*]|\d+[.)、])\s+(?P<text>.+?)\s*$")
_CN_ITEM = re.compile(
    r"^(?P<marker>[\u4e00-\u9fff]+\u3001|[\uff08(][\u4e00-\u9fff]+[\uff09)])\s*(?P<text>.+?)\s*$"
)
_CN_CLAUSE = re.compile(
    r"^\u7b2c[\u4e00-\u9fff\d]{1,12}(?P<kind>[\u7f16\u7ae0\u8282\u6761\u6b3e])(?:\s*[\uff1a:\u3001.\uff0e]?\s*\S.*)?$"
)
_NUMERIC_CLAUSE = re.compile(
    r"^(?P<number>[1-9]\d?(?:\.\d+){0,4})(?:[.、\uFF0E]|(?=\s|$))\s*(?P<title>\S.*)?$"
)


class DocumentParser:
    """Single parser boundary for the public upload formats.

    ``text`` and ``tika`` preserve legacy compatibility. ``structured`` is the
    only production writer mode; non-text files require bounded Tika XHTML and
    never fall back to flat plaintext.
    """

    def __init__(
        self,
        *,
        mode: str = "text",
        tika: TikaClient | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        resolved = mode.strip().lower() or "text"
        if resolved not in {"text", "tika", "structured"}:
            raise ValueError(f"不支持的 解析器 模式:{resolved}")
        if resolved == "tika" and tika is None:
            raise ValueError("Tika 客户端 为必填项 中 tika 模式")
        if max_bytes <= 0:
            raise ValueError("文档 大小 限制 必须为正数")
        self.mode = resolved
        self.tika = tika
        self.max_bytes = max_bytes

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return TEXT_EXTENSIONS + (TIKA_EXTENSIONS if self.tika is not None else ())

    def supports(self, file_name: str) -> bool:
        suffix = self._extension(file_name)
        return suffix in TEXT_EXTENSIONS or (suffix in TIKA_EXTENSIONS and self.tika is not None)

    def unsupported_message(self, file_name: str) -> str:
        suffix = self._extension(file_name) or "(无扩展名)"
        if self.tika is None and suffix in TIKA_EXTENSIONS:
            return "当前服务仅支持 md、txt；pdf/docx 等文件需要启用 Tika 解析。"  # noqa: RUF001
        supported = "、".join(
            extension.removeprefix(".") for extension in self.supported_extensions
        )
        return f"不支持的文件格式：{suffix}。当前支持：{supported}。"  # noqa: RUF001

    async def parse(self, file_name: str, content: bytes) -> ParsedDocument:
        if not content:
            raise EmptyDocumentError("文件内容不能为空")
        if len(content) > self.max_bytes:
            raise DocumentTooLargeError("上传文件过大")
        suffix = self._extension(file_name)
        if suffix in TEXT_EXTENSIONS:
            text = content.decode("utf-8", errors="replace")
            if self.mode == "structured":
                elements = _markdown_elements(text) if suffix == ".md" else _text_elements(text)
            else:
                elements = (
                    _legacy_markdown_elements(text)
                    if suffix == ".md"
                    else _legacy_plain_elements(text)
                )
            flags = ["text_empty"] if not text.strip() else []
            return ParsedDocument(
                "markdown" if suffix == ".md" else "text",
                _legacy_blocks(elements),
                file_name,
                "structured_text" if self.mode == "structured" else "text",
                flags,
                elements,
            )
        if suffix not in TIKA_EXTENSIONS or self.tika is None:
            raise UnsupportedFileTypeError(self.unsupported_message(file_name))
        if self.mode == "structured":
            if not hasattr(self.tika, "parse_structured"):
                raise ParserUnavailableError("结构化 Tika 解析器 为必填项 中 结构化 模式")
            structured_tika = cast(StructuredTikaClient, self.tika)
            xhtml = await structured_tika.parse_structured(file_name, content)
            from docreview.document.tika import parse_tika_xhtml

            elements = parse_tika_xhtml(xhtml.encode("utf-8"))
            flags = _structured_tika_flags(suffix, elements)
            return ParsedDocument(
                suffix[1:], _legacy_blocks(elements), file_name, "tika_xhtml", flags, elements
            )
        text = await self.tika.parse(file_name, content)
        elements = _legacy_plain_elements(text)
        flags = _legacy_tika_flags(suffix, elements)
        return ParsedDocument(
            suffix[1:], _legacy_blocks(elements), file_name, "tika", flags, elements
        )

    @staticmethod
    def _extension(file_name: str) -> str:
        return Path(file_name).suffix.strip().lower()

    @staticmethod
    def _plain_blocks(text: str) -> list[Block]:
        return _legacy_blocks(_text_elements(text))

    @staticmethod
    def _markdown_blocks(text: str) -> list[Block]:
        return _legacy_blocks(_markdown_elements(text))


def _legacy_tika_flags(suffix: str, elements: list[ParsedElement]) -> list[str]:
    flags: list[str] = []
    if not any(item.text.strip() for item in elements):
        flags.append("text_empty")
        if suffix == ".pdf":
            flags.append("requires_ocr")
    elif suffix == ".pdf" and len(elements) >= 4:
        short = sum(len(item.text) <= 8 for item in elements)
        if short * 100 // len(elements) >= 70:
            flags.append("too_many_short_blocks")
    return flags


def _structured_tika_flags(suffix: str, elements: list[ParsedElement]) -> list[str]:
    flags = _legacy_tika_flags(suffix, elements)
    if suffix == ".pdf" and not any(item.page_mappings for item in elements):
        flags.append("page_mapping_unavailable")
    return flags


def _legacy_blocks(elements: list[ParsedElement]) -> list[Block]:
    blocks: list[Block] = []
    for element in elements:
        if element.element_type == "heading":
            blocks.append(Block("heading", element.text, element.level))
        elif element.element_type == "list":
            blocks.extend(Block("paragraph", item.text) for item in element.children)
        elif element.element_type != "page_break" and element.text.strip():
            blocks.append(Block("paragraph", element.text))
    return blocks


def _legacy_plain_elements(text: str) -> list[ParsedElement]:
    return [
        ParsedElement(
            "paragraph",
            raw.strip(),
            source_start_offset=start,
            source_end_offset=end,
            start_line=line_no,
            end_line=line_no,
        )
        for raw, start, end, line_no in _source_lines(text)
        if raw.strip()
    ]


def _legacy_markdown_elements(text: str) -> list[ParsedElement]:
    elements: list[ParsedElement] = []
    paragraph: list[tuple[str, int, int, int]] = []

    def flush() -> None:
        if paragraph:
            elements.append(_paragraph_element(paragraph.copy()))
            paragraph.clear()

    for raw, start, end, line_no in _source_lines(text):
        line = raw.strip()
        if not line:
            flush()
            continue
        level = len(line) - len(line.lstrip("#"))
        if level > 0 and (len(line) == level or line[level] == " "):
            flush()
            heading = line.lstrip("#").strip()
            if heading:
                elements.append(
                    ParsedElement(
                        "heading",
                        heading,
                        level,
                        {"section_type": "section", "source": "legacy_markdown"},
                        source_start_offset=start,
                        source_end_offset=end,
                        start_line=line_no,
                        end_line=line_no,
                    )
                )
            continue
        paragraph.append((raw, start, end, line_no))
    flush()
    return elements


def _markdown_elements(text: str) -> list[ParsedElement]:
    lines = _source_lines(text)
    result: list[ParsedElement] = []
    index = 0
    while index < len(lines):
        raw, start, end, line_no = lines[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            selected = [(raw, start, end, line_no)]
            index += 1
            while index < len(lines):
                item = lines[index]
                selected.append(item)
                index += 1
                if item[0].strip().startswith(fence):
                    break
            result.append(_paragraph_element(selected, {"block_type": "code"}))
            continue
        match = _MARKDOWN_HEADING.match(raw)
        if match is not None:
            result.append(
                ParsedElement(
                    "heading",
                    match.group(2).strip().rstrip("#").rstrip(),
                    len(match.group(1)),
                    {"section_type": "section", "source": "markdown"},
                    source_start_offset=start,
                    source_end_offset=end,
                    start_line=line_no,
                    end_line=line_no,
                )
            )
            index += 1
            continue
        clause = _clause_heading(raw)
        if clause is not None:
            level, kind = clause
            result.append(
                ParsedElement(
                    "heading",
                    raw.strip(),
                    level,
                    {"section_type": "clause", "clause_kind": kind, "source": "rule"},
                    source_start_offset=start,
                    source_end_offset=end,
                    start_line=line_no,
                    end_line=line_no,
                )
            )
            index += 1
            continue
        if _is_table_start(lines, index):
            table, index = _table_element(lines, index)
            result.append(table)
            continue
        if _list_match(raw) is not None:
            listing, index = _list_element(lines, index)
            result.append(listing)
            continue
        selected: list[tuple[str, int, int, int]] = []
        while index < len(lines):
            candidate = lines[index]
            candidate_text = candidate[0].strip()
            if not candidate_text:
                break
            if selected and (
                _MARKDOWN_HEADING.match(candidate[0])
                or _is_table_start(lines, index)
                or _list_match(candidate[0]) is not None
                or candidate_text.startswith("```")
                or candidate_text.startswith("~~~")
            ):
                break
            selected.append(candidate)
            index += 1
        result.append(_paragraph_element(selected))
    return result


def _text_elements(text: str) -> list[ParsedElement]:
    lines = _source_lines(text)
    result: list[ParsedElement] = []
    index = 0
    paragraph: list[tuple[str, int, int, int]] = []

    def flush() -> None:
        if paragraph:
            result.append(_paragraph_element(paragraph.copy()))
            paragraph.clear()

    while index < len(lines):
        raw, start, end, line_no = lines[index]
        if not raw.strip():
            flush()
            index += 1
            continue
        clause = _clause_heading(raw)
        if clause is not None:
            flush()
            level, kind = clause
            result.append(
                ParsedElement(
                    "heading",
                    raw.strip(),
                    level,
                    {"section_type": "clause", "clause_kind": kind, "source": "rule"},
                    source_start_offset=start,
                    source_end_offset=end,
                    start_line=line_no,
                    end_line=line_no,
                )
            )
            index += 1
            continue
        if _list_match(raw) is not None:
            flush()
            listing, index = _list_element(lines, index)
            result.append(listing)
            continue
        paragraph.append((raw, start, end, line_no))
        index += 1
    flush()
    return result


def _source_lines(text: str) -> list[tuple[str, int, int, int]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[tuple[str, int, int, int]] = []
    offset = 0
    for line_no, value in enumerate(normalized.split("\n"), 1):
        end = offset + len(value)
        lines.append((value, offset, end, line_no))
        offset = end + 1
    return lines


def _paragraph_element(
    lines: list[tuple[str, int, int, int]], attributes: dict[str, object] | None = None
) -> ParsedElement:
    text = "\n".join(value.strip() for value, _, _, _ in lines).strip()
    return ParsedElement(
        "paragraph",
        text,
        attributes={} if attributes is None else attributes,
        source_start_offset=lines[0][1],
        source_end_offset=lines[-1][2],
        start_line=lines[0][3],
        end_line=lines[-1][3],
    )


def _list_match(value: str) -> re.Match[str] | None:
    return _LIST_ITEM.match(value) or _CN_ITEM.match(value)


def _list_element(lines: list[tuple[str, int, int, int]], index: int) -> tuple[ParsedElement, int]:
    items: list[ParsedElement] = []
    ordered = False
    first = lines[index]
    last = first
    while index < len(lines):
        raw, start, end, line_no = lines[index]
        match = _list_match(raw)
        if match is None:
            break
        marker = match.group("marker")
        body = match.group("text").strip()
        ordered = ordered or marker[0].isdigit()
        items.append(
            ParsedElement(
                "list_item",
                body,
                attributes={"marker": marker, "item_order": len(items) + 1},
                source_start_offset=start,
                source_end_offset=end,
                start_line=line_no,
                end_line=line_no,
            )
        )
        last = lines[index]
        index += 1
    return ParsedElement(
        "list",
        "\n".join(item.text for item in items),
        attributes={"ordered": ordered},
        children=tuple(items),
        source_start_offset=first[1],
        source_end_offset=last[2],
        start_line=first[3],
        end_line=last[3],
    ), index


def _is_table_start(lines: list[tuple[str, int, int, int]], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _is_table_line(lines[index][0])
        and _is_table_separator(lines[index + 1][0])
    )


def _is_table_line(value: str) -> bool:
    text = value.strip()
    return text.startswith("|") and text.count("|") >= 2


def _is_table_separator(value: str) -> bool:
    cells = _table_cells(value)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _table_cells(value: str) -> list[str]:
    return [cell.strip() for cell in value.strip().strip("|").split("|")]


def _table_element(lines: list[tuple[str, int, int, int]], index: int) -> tuple[ParsedElement, int]:
    selected: list[tuple[str, int, int, int]] = []
    while index < len(lines) and _is_table_line(lines[index][0]):
        selected.append(lines[index])
        index += 1
    header = _table_cells(selected[0][0])
    row_lines = [line for line in selected[2:] if not _is_table_separator(line[0])]
    rows = [_table_cells(line[0]) for line in row_lines]
    return ParsedElement(
        "table",
        "\n".join(line[0].strip() for line in selected),
        attributes={"header": header, "rows": rows, "caption": ""},
        source_start_offset=selected[0][1],
        source_end_offset=selected[-1][2],
        start_line=selected[0][3],
        end_line=selected[-1][3],
    ), index


def _clause_heading(value: str) -> tuple[int, str] | None:
    text = value.strip()
    if not text or len(text) > 120:
        return None
    match = _CN_CLAUSE.match(text)
    if match is not None:
        kind = match.group("kind")
        return ({"\u7f16": 1, "\u7ae0": 2, "\u8282": 3, "\u6761": 4, "\u6b3e": 5}[kind], kind)
    match = _NUMERIC_CLAUSE.match(text)
    if match is None:
        return None
    number = match.group("number")
    title = (match.group("title") or "").strip()
    if not title and "." not in number:
        return None
    return number.count(".") + 1, "numeric"


__all__ = [
    "TEXT_EXTENSIONS",
    "TIKA_EXTENSIONS",
    "Block",
    "DocumentParser",
    "DocumentTooLargeError",
    "EmptyDocumentError",
    "ParsedDocument",
    "ParsedElement",
    "ParserUnavailableError",
    "StructuredTikaClient",
    "TikaClient",
    "UnsupportedFileTypeError",
]
