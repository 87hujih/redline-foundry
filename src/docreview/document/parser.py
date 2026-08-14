"""Parser boundary matching the active Go text/Tika closure."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class UnsupportedFileTypeError(ValueError):
    pass


class ParserUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Block:
    type: str
    text: str
    level: int = 0


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_format: str
    blocks: list[Block]
    file_name: str
    parser_name: str
    quality_flags: list[str]


class TikaClient(Protocol):
    async def parse(self, file_name: str, content: bytes) -> str: ...


TEXT_EXTENSIONS = (".md", ".txt")
TIKA_EXTENSIONS = (".doc", ".docx", ".pdf", ".rtf", ".odt")


class DocumentParser:
    def __init__(self, *, mode: str = "text", tika: TikaClient | None = None) -> None:
        resolved = mode.strip().lower() or "text"
        if resolved not in {"text", "tika"}:
            raise ValueError(f"unsupported parser mode: {resolved}")
        if resolved == "tika" and tika is None:
            raise ValueError("Tika client is required in tika mode")
        self.mode = resolved
        self.tika = tika

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return TEXT_EXTENSIONS + (TIKA_EXTENSIONS if self.tika is not None else ())

    def supports(self, file_name: str) -> bool:
        suffix = Path(file_name).suffix.lower()
        return suffix in TEXT_EXTENSIONS or (suffix in TIKA_EXTENSIONS and self.tika is not None)

    def unsupported_message(self, file_name: str) -> str:
        suffix = Path(file_name).suffix.lower() or "(no extension)"
        if self.tika is None and suffix in TIKA_EXTENSIONS:
            return "当前服务仅支持 md、txt；pdf/docx 等文件需要启用 Tika 解析。"  # noqa: RUF001
        return f"不支持的文件格式：{suffix}。当前支持：{','.join(self.supported_extensions)}。"  # noqa: RUF001

    async def parse(self, file_name: str, content: bytes) -> ParsedDocument:
        suffix = Path(file_name).suffix.lower()
        if suffix in TEXT_EXTENSIONS:
            text = content.decode("utf-8-sig", errors="replace")
            blocks = self._markdown_blocks(text) if suffix == ".md" else self._plain_blocks(text)
            flags = ["text_empty"] if not text.strip() else []
            return ParsedDocument(suffix[1:], blocks, file_name, "text", flags)
        if suffix not in TIKA_EXTENSIONS or self.tika is None:
            raise UnsupportedFileTypeError(self.unsupported_message(file_name))
        try:
            text = await asyncio.wait_for(self.tika.parse(file_name, content), timeout=30)
        except TimeoutError as error:
            raise ParserUnavailableError("Tika 解析超时") from error
        except Exception as error:
            raise ParserUnavailableError("Tika 解析失败") from error
        blocks = self._plain_blocks(text)
        flags: list[str] = []
        if not text.strip():
            flags.append("text_empty")
            if suffix == ".pdf":
                flags.append("requires_ocr")
        elif (
            suffix == ".pdf"
            and len(blocks) >= 4
            and sum(len(block.text) <= 8 for block in blocks) * 100 // len(blocks) >= 70
        ):
            flags.append("too_many_short_blocks")
        return ParsedDocument(suffix[1:], blocks, file_name, "tika", flags)

    @staticmethod
    def _plain_blocks(text: str) -> list[Block]:
        return [
            Block("paragraph", line.strip()) for line in re.split(r"\r?\n", text) if line.strip()
        ]

    @staticmethod
    def _markdown_blocks(text: str) -> list[Block]:
        blocks: list[Block] = []
        paragraph: list[str] = []

        def flush() -> None:
            value = "\n".join(paragraph).strip()
            if value:
                blocks.append(Block("paragraph", value))
            paragraph.clear()

        for raw_line in re.split(r"\r?\n", text):
            line = raw_line.strip()
            if not line:
                flush()
                continue
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match:
                flush()
                blocks.append(Block("heading", match.group(2), len(match.group(1))))
            else:
                paragraph.append(line)
        flush()
        return blocks


__all__ = [
    "Block",
    "DocumentParser",
    "ParsedDocument",
    "ParserUnavailableError",
    "TikaClient",
    "UnsupportedFileTypeError",
]
