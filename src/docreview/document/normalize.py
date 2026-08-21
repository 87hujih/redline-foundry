"""导入阶段使用的确定性 block 到 section 规范化。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docreview.document.parser import Block, ParsedDocument


@dataclass(frozen=True, slots=True)
class NormalizedSection:
    section_key: str
    type: str
    order: int
    title: str
    canonical_entity_name: str = ""
    aliases: list[str] = field(default_factory=lambda: list[str]())
    summary: str = ""
    content: str = ""
    tech_stack: list[str] = field(default_factory=lambda: list[str]())
    metadata: dict[str, object] = field(default_factory=lambda: dict[str, object]())


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    sections: list[NormalizedSection]


_PROJECT_DATE = re.compile(r"[\uff08(]\s*20\d{2}[./-]\d{1,2}.*?[)\uff09]")


def _first_line(content: str) -> str:
    return next((line.strip() for line in content.splitlines() if line.strip()), "")


def _tech_stack(text: str) -> list[str]:
    if ":" in text or "\uff1a" in text:
        return []
    values = [value.strip() for value in re.split(r"[\s、/,\uff0c]+", text) if value.strip()]
    if len(values) < 2:
        return []
    if any(
        any(not (char.isascii() and (char.isalnum() or char in "+-_#.")) for char in value)
        for value in values
    ):
        return []
    return values


def _generic(blocks: list[Block]) -> list[NormalizedSection]:
    sections: list[NormalizedSection] = []
    title = ""
    lines: list[str] = []

    def flush() -> None:
        nonlocal title, lines
        content = "\n".join(lines).strip()
        if not title.strip() and not content:
            return
        resolved_title = title.strip() or "全文"
        sections.append(
            NormalizedSection(
                f"section-{len(sections) + 1}",
                "section",
                len(sections) + 1,
                resolved_title,
                summary=_first_line(content),
                content=content,
            )
        )
        title, lines = "", []

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if block.type == "heading":
            if title or lines:
                flush()
            title = text
        else:
            lines.append(text)
    flush()
    if sections:
        return sections
    content = "\n".join(block.text.strip() for block in blocks if block.text.strip()).strip()
    return (
        [
            NormalizedSection(
                "document-1", "document", 1, "全文", summary=_first_line(content), content=content
            )
        ]
        if content
        else []
    )


def normalize(parsed: ParsedDocument) -> NormalizedDocument:
    sections = _generic(parsed.blocks)
    if any(_PROJECT_DATE.search(block.text.strip()) for block in parsed.blocks):
        projects: list[NormalizedSection] = []
        current_title = ""
        body: list[str] = []
        tech: list[str] = []

        def flush_project() -> None:
            nonlocal current_title, body, tech
            if not current_title:
                return
            content = "\n".join(body).strip()
            canonical = _PROJECT_DATE.sub("", current_title).strip()
            metadata: dict[str, object] = {}
            if tech:
                metadata["tech_stack"] = list(tech)
            if not content:
                metadata.update({"low_confidence": True, "quality_flag": "heading_only"})
            projects.append(
                NormalizedSection(
                    f"project-{len(projects) + 1}",
                    "project",
                    len(projects) + 1,
                    current_title,
                    canonical,
                    [current_title, canonical],
                    _first_line(content),
                    content,
                    list(tech),
                    metadata,
                )
            )
            current_title, body, tech = "", [], []

        for block in parsed.blocks:
            text = block.text.strip()
            if _PROJECT_DATE.search(text):
                flush_project()
                current_title = text
            elif current_title:
                parsed_tech = _tech_stack(text)
                if parsed_tech:
                    for item in parsed_tech:
                        if item not in tech:
                            tech.append(item)
                else:
                    body.append(text)
        flush_project()
        if projects:
            sections = projects
    return NormalizedDocument(sections)


__all__ = ["NormalizedDocument", "NormalizedSection", "normalize"]
