"""Deterministic, structure-aware parent/child document projection.

The module owns the one production chunk profile. Character limits are retained
only as a compatibility constant and are never consulted by the builder.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from docreview.document.model import Document, Node, NodeType

MAX_CHUNK_CHARS = 800  # 为兼容旧 API 保留；生产切块流程不会使用该值。


@dataclass(frozen=True, slots=True)
class ChunkProfile:
    profile_id: str
    child_target_tokens: int = 384
    child_hard_max_tokens: int = 512
    child_min_tokens: int = 96
    parent_target_tokens: int = 960
    parent_hard_max_tokens: int = 1440
    overflow_overlap_tokens: int = 48
    heading_context_max_tokens: int = 96
    metadata_max_bytes: int = 16_384
    serializer_name: str = "docreview-embedding-text"
    serializer_version: str = "1"

    def __post_init__(self) -> None:
        if (
            not self.profile_id.strip()
            or self.child_min_tokens <= 0
            or self.child_target_tokens < self.child_min_tokens
            or self.child_hard_max_tokens < self.child_target_tokens
            or self.parent_target_tokens <= 0
            or self.parent_hard_max_tokens < self.parent_target_tokens
            or not 0 <= self.overflow_overlap_tokens <= self.child_hard_max_tokens
            or self.heading_context_max_tokens <= 0
            or self.metadata_max_bytes <= 0
            or not self.serializer_name.strip()
            or not self.serializer_version.strip()
        ):
            raise ValueError("文档审查切块配置档无效")


REVIEW_STRUCTURE_PROFILE = ChunkProfile("docreview-review-structure-2026-08-17")


class ChunkTokenizer(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def vocabulary_hash(self) -> str: ...

    @property
    def exact(self) -> bool: ...

    def count(self, value: str) -> int: ...


class TokenizerUnavailableError(RuntimeError):
    pass


class DeterministicTokenizer:
    """A versioned local tokenizer used in offline tests and approved deployments.

    Its vocabulary is intentionally defined in code and hashed, so a caller must
    bind the exact tokenizer identity to an embedding profile instead of silently
    estimating an unknown model's token count.
    """

    _VOCABULARY = "unicode-word-cjk-punctuation-v1"

    def __init__(
        self,
        *,
        name: str = "docreview-deterministic",
        version: str = "1",
        vocabulary_hash: str | None = None,
        exact: bool = True,
    ) -> None:
        self._name = name.strip()
        self._version = version.strip()
        self._vocabulary_hash = (
            vocabulary_hash or "sha256:" + hashlib.sha256(self._VOCABULARY.encode()).hexdigest()
        )
        self._exact = exact
        if not self._name or not self._version or not _valid_hash(self._vocabulary_hash):
            raise ValueError("无效的 分词器 身份")

    @classmethod
    def for_testing(cls) -> DeterministicTokenizer:
        return cls(name="docreview-test-tokenizer", version="1")

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def vocabulary_hash(self) -> str:
        return self._vocabulary_hash

    @property
    def exact(self) -> bool:
        return self._exact

    def count(self, value: str) -> int:
        return len(self.spans(value))

    def spans(self, value: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start: int | None = None

        def append_run(run_start: int, run_end: int) -> None:
            for offset in range(run_start, run_end, 4):
                spans.append((offset, min(run_end, offset + 4)))

        for index, character in enumerate(value):
            if character.isspace() or _single_token(character):
                if start is not None:
                    append_run(start, index)
                    start = None
                if not character.isspace():
                    spans.append((index, index + 1))
                continue
            if start is None:
                start = index
        if start is not None:
            append_run(start, len(value))
        return spans


class ModelTokenEstimator(DeterministicTokenizer):
    """Explicitly compatibility-only approximate tokenizer for database-free tests."""

    def __init__(self, profile: str) -> None:
        super().__init__(name=profile, version="compat", exact=False)


@dataclass(frozen=True, slots=True)
class HeadingPathItem:
    node_id: str
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_index: int
    node_id: str
    section_key: str
    section_title: str
    content: str
    content_hash: str
    chunk_role: str = "section_body"
    order_in_section: int = 1
    window_group_id: str = ""
    section_type: str = "section"
    page_start: int | None = None
    page_end: int | None = None
    embedding_text: str = ""
    metadata: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class SectionProjection:
    section_key: str
    section_type: str
    section_order: int
    title: str
    content: str
    summary: str
    metadata: dict[str, Any]
    canonical_node_id: str
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class ChunkProjection:
    profile: ChunkProfile
    tokenizer_profile: str
    sections: tuple[SectionProjection, ...]
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True, slots=True)
class _Section:
    key: str
    node: Node
    title: str
    section_type: str
    heading_path: tuple[HeadingPathItem, ...]
    order: int


@dataclass(frozen=True, slots=True)
class _Atom:
    text: str
    start: int
    end: int
    reason: str
    detail: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class _Draft:
    node: Node
    section: _Section
    role: str
    raw: str
    start: int
    end: int
    reason: str
    overlap: str
    overlap_tokens: int
    detail: dict[str, Any]
    heading_path: tuple[HeadingPathItem, ...]
    heading_truncated: bool


def build_chunks(
    document: Document,
    *,
    tokenizer: ChunkTokenizer | None = None,
    profile: ChunkProfile = REVIEW_STRUCTURE_PROFILE,
    embedding_profile: str = "embedding-v1",
    require_exact_tokenizer: bool = False,
) -> list[Chunk]:
    return list(
        build_projection(
            document,
            tokenizer=tokenizer,
            profile=profile,
            embedding_profile=embedding_profile,
            require_exact_tokenizer=require_exact_tokenizer,
        ).chunks
    )


def build_projection(
    document: Document,
    *,
    tokenizer: ChunkTokenizer | None = None,
    profile: ChunkProfile = REVIEW_STRUCTURE_PROFILE,
    embedding_profile: str = "embedding-v1",
    require_exact_tokenizer: bool = False,
) -> ChunkProjection:
    """Build the immutable AST-derived facts once for section and chunk writes."""
    resolved_tokenizer: ChunkTokenizer = tokenizer or ModelTokenEstimator("compat-estimator-v1")
    if require_exact_tokenizer and not resolved_tokenizer.exact:
        raise TokenizerUnavailableError("生产环境 切块 投影 需要 精确 分词器")
    tokenizer_profile = _tokenizer_profile(resolved_tokenizer)
    if not embedding_profile.strip():
        raise ValueError("嵌入 配置档 为必填项")
    sections, retrieval_nodes = _collect_structure(document)
    drafts: list[_Draft] = []
    for node, section, role, heading_path in retrieval_nodes:
        drafts.extend(
            _node_drafts(node, section, role, heading_path, document, resolved_tokenizer, profile)
        )
    windowed = _assign_windows(drafts, document, resolved_tokenizer, profile)
    chunks: list[Chunk] = []
    orders: dict[str, int] = {}
    for draft, window_order, group_id in windowed:
        orders[draft.section.key] = orders.get(draft.section.key, 0) + 1
        raw_page_start, raw_page_end = _fragment_pages(draft.node, draft.start, draft.end)
        embedding_text = _embedding_text(
            document,
            draft.heading_path,
            draft.role,
            draft.raw,
            draft.overlap,
            draft.detail,
        )
        token_count = _count(resolved_tokenizer, embedding_text)
        if token_count > profile.child_hard_max_tokens:
            raise TokenizerUnavailableError("切块 序列化 超出 其 硬性 令牌 限制")
        metadata: dict[str, Any] = {
            "schema_version": "1.0",
            "profile_id": profile.profile_id,
            "tokenizer_profile": tokenizer_profile,
            "heading_path": [
                {"node_id": item.node_id, "level": item.level, "text": item.text}
                for item in draft.heading_path
            ],
            "heading_path_truncated": draft.heading_truncated,
            "parent_window_order": window_order,
            "child_order": orders[draft.section.key],
            "chunk_role": draft.role,
            "raw_token_count": _count(resolved_tokenizer, draft.raw),
            "embedding_token_count": token_count,
            "fragment_hash": _hash(draft.raw),
            "embedding_text_hash": _hash(embedding_text),
            "boundary_reason": draft.reason,
            "overlap_prefix_tokens": draft.overlap_tokens,
            "overlap_prefix": draft.overlap,
            "source_spans": [
                {
                    "node_id": draft.node.node_id,
                    "start_offset": draft.node.source_location.start_offset + draft.start,
                    "end_offset": draft.node.source_location.start_offset + draft.end,
                    "page_start": raw_page_start,
                    "page_end": raw_page_end,
                }
            ],
            "quality_flags": sorted(_string_values(draft.node.metadata.get("quality_flags"))),
        }
        metadata.update(draft.detail)
        _validate_metadata(metadata, profile)
        chunks.append(
            Chunk(
                len(chunks),
                draft.node.node_id,
                draft.section.key,
                draft.section.title,
                draft.raw,
                draft.node.content_hash,
                draft.role,
                orders[draft.section.key],
                group_id,
                draft.section.section_type,
                raw_page_start,
                raw_page_end,
                embedding_text,
                metadata,
            )
        )
    projections = tuple(_section_projection(section, document) for section in sections)
    return ChunkProjection(profile, tokenizer_profile, projections, tuple(chunks))


def embedding_text_for_chunk(chunk: Chunk) -> str:
    """The persisted child serialization used by embedding and reranking workers."""
    return chunk.embedding_text


def embedding_text_from_metadata(content: str, metadata: dict[str, Any]) -> str:
    """Rebuild the exact deterministic model text without consulting current AST facts."""
    raw_path = metadata.get("heading_path")
    path_items = cast(list[object], raw_path) if isinstance(raw_path, list) else []
    path = tuple(
        HeadingPathItem(
            str(cast(Mapping[str, object], item).get("node_id", "")),
            _int_value(cast(Mapping[str, object], item).get("level"), 1),
            str(cast(Mapping[str, object], item).get("text", "")),
        )
        for item in path_items
        if isinstance(item, Mapping)
        and str(cast(Mapping[str, object], item).get("node_id", "")).strip()
    )
    role = str(metadata.get("chunk_role", "section_body"))
    overlap = str(metadata.get("overlap_prefix", ""))
    return _embedding_text(
        Document("", "", Node("", NodeType.DOCUMENT), ""), path, role, content, overlap, metadata
    )


def _collect_structure(
    document: Document,
) -> tuple[list[_Section], list[tuple[Node, _Section, str, tuple[HeadingPathItem, ...]]]]:
    sections: list[_Section] = []
    retrieval: list[tuple[Node, _Section, str, tuple[HeadingPathItem, ...]]] = []
    root_section = _Section(document.root.node_id, document.root, "全文", "document", (), 1)
    sections.append(root_section)

    def walk(node: Node, active: _Section, path: tuple[HeadingPathItem, ...]) -> None:
        for child in node.children:
            if child.type is NodeType.HEADING:
                level = _heading_level(child)
                heading = HeadingPathItem(child.node_id, level, child.content.strip())
                section = _Section(
                    str(child.metadata.get("section_key") or child.node_id),
                    child,
                    child.content.strip() or "全文",
                    str(child.metadata.get("section_type") or "section"),
                    (*path, heading),
                    len(sections) + 1,
                )
                sections.append(section)
                if not child.children:
                    retrieval.append((child, section, "heading_only", section.heading_path))
                walk(child, section, section.heading_path)
                continue
            if child.type is NodeType.LIST:
                for item in child.children:
                    if item.type is NodeType.LIST_ITEM and item.content.strip():
                        retrieval.append((item, active, "list", path))
                continue
            if child.type is NodeType.TABLE and child.content.strip():
                retrieval.append((child, active, "table", path))
                continue
            if child.type is NodeType.PARAGRAPH and child.content.strip():
                role = str(child.metadata.get("chunk_role") or "")
                if not role:
                    role = "clause_body" if active.section_type == "clause" else "section_body"
                retrieval.append((child, active, role, path))

    walk(document.root, root_section, ())
    return sections, retrieval


def _node_drafts(
    node: Node,
    section: _Section,
    role: str,
    heading_path: tuple[HeadingPathItem, ...],
    document: Document,
    tokenizer: ChunkTokenizer,
    profile: ChunkProfile,
) -> list[_Draft]:
    compact_path, truncated = _compact_heading_path(heading_path, tokenizer, profile)
    if role == "heading_only":
        return [
            _Draft(
                node,
                section,
                role,
                node.content.strip(),
                0,
                len(node.content.strip()),
                "heading_only",
                "",
                0,
                {},
                compact_path,
                truncated,
            )
        ]
    if role == "table":
        atoms = _table_atoms(node)
    elif role == "list":
        atoms = [
            _Atom(
                node.content.strip(),
                0,
                len(node.content.strip()),
                "list_item",
                {
                    "item_start_order": _int_value(node.metadata.get("item_order"), 1),
                    "item_end_order": _int_value(node.metadata.get("item_order"), 1),
                },
            )
        ]
    else:
        atoms = _paragraph_atoms(node.content)
    output: list[_Draft] = []
    pending: list[_Atom] = []
    for atom in atoms:
        if _fits(document, compact_path, role, atom.text, "", atom.detail, tokenizer, profile):
            pending.append(atom)
            continue
        output.extend(
            _pack_atoms(
                node, section, role, pending, compact_path, truncated, document, tokenizer, profile
            )
        )
        pending.clear()
        split = _force_split_atom(document, compact_path, role, atom, tokenizer, profile)
        previous = ""
        for number, part in enumerate(split):
            overlap = (
                _tail_tokens(previous, tokenizer, profile.overflow_overlap_tokens) if number else ""
            )
            output.append(
                _Draft(
                    node,
                    section,
                    role,
                    part.text,
                    part.start,
                    part.end,
                    "forced_token_split",
                    overlap,
                    _count(tokenizer, overlap),
                    {**part.detail, "fragment_order": number + 1},
                    compact_path,
                    truncated,
                )
            )
            previous = part.text
    output.extend(
        _pack_atoms(
            node, section, role, pending, compact_path, truncated, document, tokenizer, profile
        )
    )
    return output


def _pack_atoms(
    node: Node,
    section: _Section,
    role: str,
    atoms: list[_Atom],
    path: tuple[HeadingPathItem, ...],
    truncated: bool,
    document: Document,
    tokenizer: ChunkTokenizer,
    profile: ChunkProfile,
) -> list[_Draft]:
    output: list[_Draft] = []
    current: list[_Atom] = []
    for atom in atoms:
        candidate = _join_atoms([*current, atom])
        detail = _combined_detail([*current, atom], role)
        candidate_tokens = _count(
            tokenizer, _embedding_text(document, path, role, candidate, "", detail)
        )
        current_tokens = (
            _count(
                tokenizer,
                _embedding_text(
                    document, path, role, _join_atoms(current), "", _combined_detail(current, role)
                ),
            )
            if current
            else 0
        )
        if (
            not current
            or candidate_tokens <= profile.child_target_tokens
            or (
                current_tokens < profile.child_min_tokens
                and candidate_tokens <= profile.child_hard_max_tokens
            )
        ):
            current.append(atom)
            continue
        output.append(_draft_from_atoms(node, section, role, current, path, truncated))
        current = [atom]
    if current:
        output.append(_draft_from_atoms(node, section, role, current, path, truncated))
    return output


def _draft_from_atoms(
    node: Node,
    section: _Section,
    role: str,
    atoms: list[_Atom],
    path: tuple[HeadingPathItem, ...],
    truncated: bool,
) -> _Draft:
    return _Draft(
        node,
        section,
        role,
        _join_atoms(atoms),
        atoms[0].start,
        atoms[-1].end,
        atoms[0].reason,
        "",
        0,
        _combined_detail(atoms, role),
        path,
        truncated,
    )


def _paragraph_atoms(content: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    cursor = 0
    for value in re.split(r"(\n\s*\n)", content):
        if not value or value.isspace():
            cursor += len(value)
            continue
        start = cursor
        end = cursor + len(value)
        atoms.append(_Atom(value.strip(), start, end, "paragraph"))
        cursor = end
    return atoms or [_Atom(content.strip(), 0, len(content.strip()), "paragraph")]


def _table_atoms(node: Node) -> list[_Atom]:
    header = _string_values(node.metadata.get("header"))
    rows_value = node.metadata.get("rows")
    rows: list[object] = cast(list[object], rows_value) if isinstance(rows_value, list) else []
    atoms: list[_Atom] = []
    cursor = 0
    for order, raw in enumerate(rows, 1):
        cells = _string_values(raw)
        text = "| " + " | ".join(cells) + " |"
        start = node.content.find(text, cursor)
        if start < 0:
            start = cursor
        end = start + len(text)
        cursor = end
        atoms.append(
            _Atom(
                text,
                start,
                end,
                "table_row",
                {
                    "row_start": order,
                    "row_end": order,
                    "table_header": header,
                    "header_hash": _hash("|".join(header)),
                },
            )
        )
    if not atoms:
        atoms.append(
            _Atom(
                node.content.strip(),
                0,
                len(node.content.strip()),
                "table_row",
                {
                    "row_start": 1,
                    "row_end": 1,
                    "table_header": header,
                    "header_hash": _hash("|".join(header)),
                },
            )
        )
    return atoms


def _force_split_atom(
    document: Document,
    path: tuple[HeadingPathItem, ...],
    role: str,
    atom: _Atom,
    tokenizer: ChunkTokenizer,
    profile: ChunkProfile,
) -> list[_Atom]:
    units = _split_units(atom.text)
    fragments: list[_Atom] = []
    current = ""
    current_start = atom.start
    consumed = 0
    for unit in units:
        candidate = current + unit
        if current and not _fits(
            document, path, role, candidate, "", atom.detail, tokenizer, profile
        ):
            fragments.append(
                _Atom(
                    current, current_start, atom.start + consumed, "forced_token_split", atom.detail
                )
            )
            current_start = atom.start + consumed
            current = ""
        if not current and not _fits(
            document, path, role, unit, "", atom.detail, tokenizer, profile
        ):
            forced = _force_token_units(
                document, path, role, unit, atom.start + consumed, atom.detail, tokenizer, profile
            )
            fragments.extend(forced)
            consumed += len(unit)
            current_start = atom.start + consumed
            continue
        current += unit
        consumed += len(unit)
    if current:
        fragments.append(
            _Atom(current, current_start, atom.start + consumed, "forced_token_split", atom.detail)
        )
    bounded: list[_Atom] = []
    previous = ""
    for fragment in fragments:
        overlap = _tail_tokens(previous, tokenizer, profile.overflow_overlap_tokens)
        if _fits(document, path, role, fragment.text, overlap, fragment.detail, tokenizer, profile):
            bounded.append(fragment)
        else:
            bounded.extend(
                _force_token_units(
                    document,
                    path,
                    role,
                    fragment.text,
                    fragment.start,
                    fragment.detail,
                    tokenizer,
                    profile,
                    overlap,
                )
            )
        previous = bounded[-1].text
    return bounded


def _split_units(value: str) -> list[str]:
    first = [part for part in re.split(r"(?<=[\u3002\uFF01\uFF1F\uFF1B.!?;])\s*", value) if part]
    if len(first) > 1:
        return first
    second = [part for part in re.split(r"(?<=[\uFF0C,])\s*", value) if part]
    if len(second) > 1:
        return second
    third = re.findall(r"\S+\s*", value)
    return third or [value]


def _force_token_units(
    document: Document,
    path: tuple[HeadingPathItem, ...],
    role: str,
    value: str,
    offset: int,
    detail: dict[str, Any],
    tokenizer: ChunkTokenizer,
    profile: ChunkProfile,
    overlap: str = "",
) -> list[_Atom]:
    output: list[_Atom] = []
    start = 0
    while start < len(value):
        low, high, best = start + 1, len(value), start
        while low <= high:
            middle = (low + high) // 2
            if _fits(
                document, path, role, value[start:middle], overlap, detail, tokenizer, profile
            ):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == start:
            raise TokenizerUnavailableError("分词器 上下文 单独 超出 切块 硬性 限制")
        output.append(
            _Atom(value[start:best], offset + start, offset + best, "forced_token_split", detail)
        )
        start = best
    return output


def _assign_windows(
    drafts: list[_Draft], document: Document, tokenizer: ChunkTokenizer, profile: ChunkProfile
) -> list[tuple[_Draft, int, str]]:
    output: list[tuple[_Draft, int, str]] = []
    by_section: dict[str, list[_Draft]] = {}
    for draft in drafts:
        by_section.setdefault(draft.section.key, []).append(draft)
    for section_drafts in by_section.values():
        window_order = 1
        current_tokens = 0
        for draft in section_drafts:
            tokens = _count(tokenizer, draft.raw)
            if current_tokens and current_tokens + tokens > profile.parent_target_tokens:
                window_order += 1
                current_tokens = 0
            if tokens > profile.parent_hard_max_tokens:
                raise TokenizerUnavailableError("子节点 原始 内容 超出 父节点 硬性 令牌 限制")
            current_tokens += tokens
            group = _window_id(
                document.document_id, draft.section.node.node_id, window_order, profile.profile_id
            )
            output.append((draft, window_order, group))
    return output


def _section_projection(section: _Section, document: Document) -> SectionProjection:
    content = _section_content(section.node)
    pages = [item.page for node in _iter_source_nodes(section.node) for item in node.page_mapping]
    metadata = {
        "heading_path": [
            {"node_id": item.node_id, "level": item.level, "text": item.text}
            for item in section.heading_path
        ],
        "quality_flags": sorted(_string_values(section.node.metadata.get("quality_flags"))),
    }
    return SectionProjection(
        section.key,
        section.section_type,
        section.order,
        section.title,
        content,
        _first_line(content),
        metadata,
        section.node.node_id,
        min(pages) if pages else None,
        max(pages) if pages else None,
    )


def _section_content(node: Node) -> str:
    values: list[str] = []
    for child in node.children:
        if child.type is NodeType.HEADING:
            continue
        if child.type is NodeType.LIST:
            values.extend(item.content.strip() for item in child.children if item.content.strip())
        elif child.content.strip():
            values.append(child.content.strip())
    return "\n\n".join(values)


def _iter_source_nodes(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        yield from _iter_source_nodes(child)


def _compact_heading_path(
    path: tuple[HeadingPathItem, ...], tokenizer: ChunkTokenizer, profile: ChunkProfile
) -> tuple[tuple[HeadingPathItem, ...], bool]:
    if not path:
        return (), False
    values = list(path)

    def encoded(items: list[HeadingPathItem]) -> str:
        return "\n".join(item.text for item in items)

    truncated = False
    while (
        len(values) > 2 and _count(tokenizer, encoded(values)) > profile.heading_context_max_tokens
    ):
        values.pop(1)
        truncated = True
    if _count(tokenizer, encoded(values)) > profile.heading_context_max_tokens:
        first, last = values[0], values[-1]
        allowed = max(1, profile.heading_context_max_tokens // (2 if len(values) > 1 else 1))
        values = [_replace_heading(first, _fit_tokens(first.text, tokenizer, allowed))]
        if first.node_id != last.node_id:
            values.append(_replace_heading(last, _fit_tokens(last.text, tokenizer, allowed)))
        truncated = True
    return tuple(values), truncated


def _replace_heading(item: HeadingPathItem, text: str) -> HeadingPathItem:
    return HeadingPathItem(item.node_id, item.level, text)


def _fit_tokens(value: str, tokenizer: ChunkTokenizer, limit: int) -> str:
    if _count(tokenizer, value) <= limit:
        return value
    end = len(value)
    while end and _count(tokenizer, value[:end]) > limit:
        end -= 1
    return value[:end].rstrip()


def _embedding_text(
    document: Document,
    path: tuple[HeadingPathItem, ...],
    role: str,
    raw: str,
    overlap: str,
    detail: dict[str, Any],
) -> str:
    parts: list[str] = []
    if path:
        parts.append(path[0].text)
        parts.extend(item.text for item in path)
    if role == "table":
        caption = detail.get("caption")
        header = detail.get("table_header")
        if isinstance(caption, str) and caption.strip():
            parts.append(caption.strip())
        if isinstance(header, list):
            parts.append(" | ".join(_string_values(cast(list[object], header))))
    if overlap:
        parts.append(overlap)
    parts.append(raw)
    return "\n\n".join(part for part in parts if part.strip())


def _fits(
    document: Document,
    path: tuple[HeadingPathItem, ...],
    role: str,
    raw: str,
    overlap: str,
    detail: dict[str, Any],
    tokenizer: ChunkTokenizer,
    profile: ChunkProfile,
) -> bool:
    return (
        _count(tokenizer, _embedding_text(document, path, role, raw, overlap, detail))
        <= profile.child_hard_max_tokens
    )


def _join_atoms(atoms: list[_Atom]) -> str:
    return "\n\n".join(atom.text for atom in atoms)


def _combined_detail(atoms: list[_Atom], role: str) -> dict[str, Any]:
    if not atoms:
        return {}
    if role == "table":
        first = atoms[0].detail
        return {
            **first,
            "row_start": atoms[0].detail.get("row_start", 1),
            "row_end": atoms[-1].detail.get("row_end", 1),
        }
    if role == "list":
        return {
            "item_start_order": atoms[0].detail.get("item_start_order", 1),
            "item_end_order": atoms[-1].detail.get("item_end_order", 1),
        }
    return {}


def _tail_tokens(value: str, tokenizer: ChunkTokenizer, limit: int) -> str:
    if not value or limit <= 0:
        return ""
    spans = tokenizer.spans(value) if isinstance(tokenizer, DeterministicTokenizer) else []
    if spans:
        return value[spans[max(0, len(spans) - limit)][0] :]
    start = max(0, len(value) - limit * 4)
    return value[start:]


def _fragment_pages(node: Node, start: int, end: int) -> tuple[int | None, int | None]:
    absolute_start = node.source_location.start_offset + start
    absolute_end = node.source_location.start_offset + end
    pages = [
        mapping.page
        for mapping in node.page_mapping
        if mapping.end_offset >= absolute_start and mapping.start_offset <= absolute_end
    ]
    return (min(pages), max(pages)) if pages else (None, None)


def _heading_level(node: Node) -> int:
    value = node.attributes.get("level", node.metadata.get("heading_level", 1))
    return _int_value(value, 1)


def _window_id(document_id: str, section_node_id: str, order: int, profile_id: str) -> str:
    value = "\0".join((document_id, section_node_id, str(order), profile_id))
    return "win_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _tokenizer_profile(tokenizer: ChunkTokenizer) -> str:
    values = (tokenizer.name.strip(), tokenizer.version.strip(), tokenizer.vocabulary_hash.strip())
    if not all(values) or not _valid_hash(values[2]):
        raise TokenizerUnavailableError("分词器 身份 无效")
    return "/".join(values)


def _count(tokenizer: ChunkTokenizer, value: str) -> int:
    result = tokenizer.count(value)
    if result < 0:
        raise TokenizerUnavailableError("分词器 返回了无效的 令牌 数量")
    return result


def _validate_metadata(value: dict[str, Any], profile: ChunkProfile) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > profile.metadata_max_bytes:
        raise ValueError("切块 元数据 超出 已配置的 大小 限制")
    forbidden = ("api_key", "password", "secret", "provider_response", "base64")
    if any(marker in key.lower() for key in value for marker in forbidden):
        raise ValueError("切块元数据包含禁止字段")


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _valid_hash(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(item in "0123456789abcdef" for item in value[7:])
    )


def _single_token(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x9FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
        or unicodedata.category(character).startswith(("P", "S"))
    )


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def _int_value(value: object, default: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
    )


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


__all__ = [
    "MAX_CHUNK_CHARS",
    "REVIEW_STRUCTURE_PROFILE",
    "Chunk",
    "ChunkProfile",
    "ChunkProjection",
    "DeterministicTokenizer",
    "ModelTokenEstimator",
    "SectionProjection",
    "TokenizerUnavailableError",
    "build_chunks",
    "build_projection",
    "embedding_text_for_chunk",
    "embedding_text_from_metadata",
]
