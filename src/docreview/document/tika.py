"""Bounded Apache Tika transport and defensive XHTML structure reader."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx

from docreview.document.model import PageMapping
from docreview.document.parser import ParsedElement


class TikaError(RuntimeError):
    """Deterministic Tika boundary error."""


class TikaHTTPStatusError(TikaError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Tika 解析失败：状态码 {status_code}")  # noqa: RUF001
        self.status_code = status_code


class TikaTimeoutError(TikaError):
    pass


class TikaConnectionError(TikaError):
    pass


class TikaEmptyResponseError(TikaError):
    pass


class TikaResponseTooLargeError(TikaError):
    pass


class TikaInvalidResponseError(TikaError):
    pass


class XHTMLStructureError(TikaInvalidResponseError):
    pass


_DTD_OR_ENTITY = re.compile(rb"<!\s*(?:doctype|entity)\b", re.IGNORECASE)
_PAGE_ATTRIBUTE_NAMES = ("page", "page-number", "data-page", "data-page-number")


def _service_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Tika URL 端口 无效") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Tika URL 必须是 HTTP/HTTPS 服务 地址")
    return normalized


class HTTPXTikaClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        max_response_bytes: int,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("Tika 超时时间 必须为正数")
        if max_response_bytes <= 0:
            raise ValueError("Tika 响应 限制 必须为正数")
        self._client = client
        self._endpoint = _service_url(base_url) + "/tika"
        self._timeout = timeout_ms / 1000
        self._max_response_bytes = max_response_bytes

    async def parse(self, file_name: str, content: bytes) -> str:
        """Legacy compatibility method. Production structured ingestion does not use it."""
        del file_name
        return await self._request(content, "text/plain")

    async def parse_structured(self, file_name: str, content: bytes) -> str:
        """Fetch exactly one bounded XHTML representation for structured ingestion."""
        del file_name
        return await self._request(content, "text/xml")

    async def _request(self, content: bytes, accept: str) -> str:
        try:
            async with self._client.stream(
                "PUT",
                self._endpoint,
                headers={"Accept": accept},
                content=content,
                timeout=self._timeout,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise TikaHTTPStatusError(response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise TikaResponseTooLargeError("Tika 响应超过大小限制")
                    body.extend(chunk)
        except httpx.TimeoutException as error:
            raise TikaTimeoutError("Tika 解析超时") from error
        except httpx.NetworkError as error:
            raise TikaConnectionError("调用 Tika 失败") from error
        if not body:
            raise TikaEmptyResponseError("Tika 返回空响应")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TikaInvalidResponseError("Tika 响应不是合法 UTF-8") from error


def parse_tika_xhtml(
    value: bytes,
    *,
    max_depth: int = 64,
    max_elements: int = 20_000,
    max_text_chars: int = 20 * 1024 * 1024,
) -> list[ParsedElement]:
    """Convert safe Tika XHTML to deterministic source elements.

    Tika's XHTML has no reliable original-file character offsets. The elements
    deliberately mark that limitation rather than fabricating positions. Page
    numbers are kept only when explicit XHTML metadata provides them.
    """
    if (
        max_depth <= 0
        or max_elements <= 0
        or max_text_chars <= 0
        or len(value) > max_text_chars * 4
    ):
        raise XHTMLStructureError("XHTML 解析器 预算 无效 或 超出")
    if _DTD_OR_ENTITY.search(value):
        raise XHTMLStructureError("XHTML DTD 和 ENTITY 声明 为 禁止")
    try:
        root = ET.fromstring(value)
    except ET.ParseError as error:
        raise XHTMLStructureError("Tika XHTML 无效") from error
    count = 0
    text_count = 0
    output: list[ParsedElement] = []

    def visit(element: ET.Element, depth: int, page: int | None) -> None:
        nonlocal count, text_count
        count += 1
        if count > max_elements or depth > max_depth:
            raise XHTMLStructureError("XHTML 结构 超出 解析器 预算")
        tag = _tag(element.tag)
        page = _page_number(element.attrib, page)
        if tag in {"script", "style", "object", "embed", "iframe"}:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _text(element)
            append(
                "heading",
                text,
                level=int(tag[1]),
                attributes={"section_type": "section", "source": "tika_xhtml"},
                page=page,
            )
            return
        if tag == "p":
            append("paragraph", _text(element), attributes={"source": "tika_xhtml"}, page=page)
            return
        if tag in {"ul", "ol"}:
            items: list[ParsedElement] = []
            for child in element:
                if _tag(child.tag) != "li":
                    continue
                text = _text(child)
                if text:
                    items.append(
                        _element(
                            "list_item",
                            text,
                            attributes={"marker": "", "item_order": len(items) + 1},
                            page=page,
                        )
                    )
            if items:
                output.append(
                    _element(
                        "list",
                        "\n".join(item.text for item in items),
                        attributes={"ordered": tag == "ol", "source": "tika_xhtml"},
                        children=tuple(items),
                        page=page,
                    )
                )
            return
        if tag == "table":
            header: list[str] = []
            rows: list[list[str]] = []
            caption = ""
            for child in element.iter():
                child_tag = _tag(child.tag)
                if child_tag == "caption":
                    caption = _text(child)
                if child_tag == "tr":
                    cells = [_text(cell) for cell in child if _tag(cell.tag) in {"th", "td"}]
                    if not cells:
                        continue
                    if any(_tag(cell.tag) == "th" for cell in child) and not header:
                        header = cells
                    else:
                        rows.append(cells)
            rendered = "\n".join(" | ".join(row) for row in ([header] if header else []) + rows)
            if rendered:
                append(
                    "table",
                    rendered,
                    attributes={
                        "header": header,
                        "rows": rows,
                        "caption": caption,
                        "source": "tika_xhtml",
                    },
                    page=page,
                )
            return
        for child in element:
            visit(child, depth + 1, page)

    def append(
        element_type: str,
        text: str,
        *,
        level: int = 0,
        attributes: dict[str, Any],
        page: int | None,
    ) -> None:
        nonlocal text_count
        text_count += len(text)
        if text_count > max_text_chars:
            raise XHTMLStructureError("XHTML 文本 超出 解析器 预算")
        if text:
            output.append(
                _element(element_type, text, level=level, attributes=attributes, page=page)
            )

    visit(root, 0, None)
    return output


def _element(
    element_type: str,
    text: str,
    *,
    level: int = 0,
    attributes: dict[str, Any],
    children: tuple[ParsedElement, ...] = (),
    page: int | None,
) -> ParsedElement:
    mapping = () if page is None else (PageMapping(page, 0, max(0, len(text))),)
    return ParsedElement(
        element_type,
        text,
        level,
        attributes,
        children,
        quality_flags=("source_offsets_unavailable",),
        page_mappings=mapping,
    )


def _tag(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element) -> str:
    return " ".join(part.strip() for part in element.itertext() if part.strip())


def _page_number(attributes: dict[str, str], inherited: int | None) -> int | None:
    for key in _PAGE_ATTRIBUTE_NAMES:
        raw = attributes.get(key)
        if raw is not None and raw.strip().isdigit() and int(raw.strip()) > 0:
            return int(raw.strip())
    classes = attributes.get("class", "").split()
    if "page" in classes and inherited is None:
        return 1
    return inherited


__all__ = [
    "HTTPXTikaClient",
    "TikaConnectionError",
    "TikaEmptyResponseError",
    "TikaError",
    "TikaHTTPStatusError",
    "TikaInvalidResponseError",
    "TikaResponseTooLargeError",
    "TikaTimeoutError",
    "XHTMLStructureError",
    "parse_tika_xhtml",
]
