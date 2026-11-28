"""OpenDocument text (.odt / .ott packages and flat .fodt XML) to :class:`Document`.

Packages go through :func:`open_zip` (``content.xml``, ``styles.xml``, ``meta.xml``,
``Pictures/...``); flat files are one XML document whose images are inline
``office:binary-data``. Password-protected packages are refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

from .base import Document, FormatError, read_limited, resolve_href
from .ooxml import (
    MAX_DEPTH, MAX_FLAT_BYTES, MAX_IMAGE_BYTES, MAX_XML_BYTES, _add_image, _assemble, _Block, _fmt, _http_link,
    _Inline, _join_blocks, _ListCounter, _Notes, _open_zip, _parse_xml, _read_member, _safe_id, _source_format, _split, _table_html,
)

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
}


def _q(prefix: str, local: str) -> str:
    return f"{{{_NS[prefix]}}}{local}"


T_P, T_H, T_SPAN, T_A, T_S, T_TAB = (_q("text", n) for n in ("p", "h", "span", "a", "s", "tab"))
T_LINE_BREAK, T_LIST, T_LIST_ITEM, T_LIST_HEADER = (_q("text", n) for n in ("line-break", "list", "list-item",
                                                                            "list-header"))
T_SECTION, T_NOTE, T_NOTE_BODY, T_BOOKMARK, T_BOOKMARK_START = (
    _q("text", n) for n in ("section", "note", "note-body", "bookmark", "bookmark-start"))
T_RUBY, T_RUBY_BASE, T_RUBY_TEXT = (_q("text", n) for n in ("ruby", "ruby-base", "ruby-text"))
T_INDEX_BODY, T_INDEX_TITLE, T_NUMBERED_PARAGRAPH = (_q("text", n) for n in ("index-body", "index-title",
                                                                             "numbered-paragraph"))
T_LIST_STYLE = _q("text", "list-style")
T_STYLE_NAME, T_OUTLINE_LEVEL, T_C, T_NAME, T_LEVEL, T_START_VALUE = (
    _q("text", n) for n in ("style-name", "outline-level", "c", "name", "level", "start-value"))
TABLE_TABLE, TABLE_ROW, TABLE_CELL = (_q("table", n) for n in ("table", "table-row", "table-cell"))
TABLE_HEADER_ROWS = _q("table", "table-header-rows")
TABLE_ROW_CONTAINERS = frozenset(_q("table", n) for n in ("table-rows", "table-row-group"))
TABLE_COLS_SPANNED, TABLE_ROWS_SPANNED = _q("table", "number-columns-spanned"), _q("table", "number-rows-spanned")
TABLE_COLS_REPEATED, TABLE_ROWS_REPEATED = _q("table", "number-columns-repeated"), _q("table", "number-rows-repeated")
DRAW_FRAME, DRAW_IMAGE, DRAW_TEXT_BOX, DRAW_A = (_q("draw", n) for n in ("frame", "image", "text-box", "a"))
STYLE_STYLE, STYLE_NAME, STYLE_FAMILY, STYLE_PARENT = (_q("style", n) for n in ("style", "name", "family",
                                                                                 "parent-style-name"))
STYLE_DISPLAY, STYLE_LIST_STYLE = _q("style", "display-name"), _q("style", "list-style-name")
STYLE_TEXT_PROPS, STYLE_PARA_PROPS = _q("style", "text-properties"), _q("style", "paragraph-properties")
STYLE_NUM_FORMAT, STYLE_DEFAULT_STYLE = _q("style", "num-format"), _q("style", "default-style")
OFFICE_BINARY, OFFICE_BODY, OFFICE_TEXT, OFFICE_META = (_q("office", n) for n in ("binary-data", "body", "text", "meta"))
OFFICE_STYLES, OFFICE_AUTO_STYLES = _q("office", "styles"), _q("office", "automatic-styles")
XLINK_HREF = _q("xlink", "href")

_SKIP_BLOCKS = frozenset(
    [_q("text", n) for n in ("tracked-changes", "sequence-decls", "variable-decls", "user-field-decls",
                             "dde-connection-decls", "table-of-content", "illustration-index", "table-index",
                             "object-index", "alphabetical-index", "soft-page-break")]
    + [_q("office", n) for n in ("forms", "annotation", "annotation-end")]
    + [_q("table", n) for n in ("calculation-settings", "content-validations", "label-ranges", "named-expressions")])
_INDEXES_WITH_CONTENT = frozenset(_q("text", n) for n in ("bibliography", "user-index"))
_SKIP_INLINE = frozenset(
    [_q("office", n) for n in ("annotation", "annotation-end")]
    + [_q("text", n) for n in ("change", "change-start", "change-end", "bookmark-end", "soft-page-break",
                               "hidden-text", "hidden-paragraph", "tracked-changes", "toc-mark", "toc-mark-start",
                               "toc-mark-end", "alphabetical-index-mark", "user-index-mark", "note-citation")]
    + [_q("svg", n) for n in ("title", "desc")])
_LIST_TYPES = {"1": "1", "a": "a", "A": "A", "i": "i", "I": "I"}
_HEADING_STYLE = re.compile(r"heading\s*(10|[1-9])")
_TEXT_PROPS = {
    "b": (_q("fo", "font-weight"), _q("style", "font-weight-asian"), _q("style", "font-weight-complex")),
    "i": (_q("fo", "font-style"), _q("style", "font-style-asian"), _q("style", "font-style-complex")),
}


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _text_props(style: ET.Element) -> dict:
    props: dict = {}
    node = style.find(STYLE_TEXT_PROPS)
    if node is None:
        return props
    weight = next((node.get(attr) for attr in _TEXT_PROPS["b"] if node.get(attr)), None)
    if weight:
        props["b"] = weight == "bold" or (weight.isdigit() and int(weight) >= 600)
    slant = next((node.get(attr) for attr in _TEXT_PROPS["i"] if node.get(attr)), None)
    if slant:
        props["i"] = slant in ("italic", "oblique")
    underline = node.get(_q("style", "text-underline-style"))
    if underline:
        props["u"] = underline != "none"
    strike = node.get(_q("style", "text-line-through-style"))
    if strike:
        props["s"] = strike != "none"
    position = (node.get(_q("style", "text-position")) or "").split()
    if position:
        first = position[0]
        if first in ("super", "sub"):
            props["va"] = "sup" if first == "super" else "sub"
        elif first.endswith("%"):
            try:
                shift = float(first[:-1])
            except ValueError:
                shift = 0.0
            props["va"] = "sup" if shift > 0 else "sub" if shift < 0 else ""
    if node.get(_q("text", "display")) == "none":
        props["hidden"] = True
    return props


def _style_label(entry: dict) -> str:
    return (entry.get("display") or entry["name"]).replace("_20_", " ").strip().lower()


class _OdfConverter:
    def __init__(self, document: Document, archive=None, base: str = "content.xml") -> None:
        self.doc = document
        self.zip = archive
        self.base = base
        self.members = set(archive.namelist()) if archive is not None else set()
        self.para_styles: dict[str, dict] = {}
        self.text_styles: dict[str, dict] = {}
        self.list_styles: dict[str, dict[int, tuple[bool, str, int]]] = {}
        self.props_cache: dict[tuple[str, str], dict] = {}
        self.notes = _Notes()
        self.images: dict[str, str] = {}
        self.counter = _ListCounter()
        self.pending: list[_Block] = []
        self.bookmarks: set[str] = set()
        self.anchor_ok: set[str] = set()
        self.emitted_ids: set[str] = set()
        self.pending_anchors: list[str] = []
        self.in_note = False
        self.cell_depth = 0
        self.first_title = ""
        self.default_lang = ""

    # ------------------------------------------------------------ styles

    def load_styles(self, container: ET.Element | None, automatic: bool) -> None:
        if container is None:
            return
        for element in container:
            if element.tag == STYLE_STYLE:
                name = element.get(STYLE_NAME)
                if not name:
                    continue
                paragraph = element.find(STYLE_PARA_PROPS)
                entry = {
                    "name": name, "display": element.get(STYLE_DISPLAY) or "", "parent": element.get(STYLE_PARENT),
                    "props": _text_props(element), "auto": automatic, "list": element.get(STYLE_LIST_STYLE),
                    "align": paragraph.get(_q("fo", "text-align")) if paragraph is not None else None,
                    "outline": _int(element.get(_q("style", "default-outline-level")), 0, 0, 10),
                }
                family = element.get(STYLE_FAMILY)
                if family == "paragraph":
                    self.para_styles[name] = entry
                elif family == "text":
                    self.text_styles[name] = entry
            elif element.tag == T_LIST_STYLE and element.get(STYLE_NAME):
                levels: dict[int, tuple[bool, str, int]] = {}
                for level in element:
                    number = _int(level.get(T_LEVEL), 1, 1, 10)
                    if level.tag == _q("text", "list-level-style-number"):
                        fmt = level.get(STYLE_NUM_FORMAT) or ""
                        start = _int(level.get(T_START_VALUE), 1, 0, 1 << 20)
                        levels[number] = (fmt in _LIST_TYPES or len(fmt) == 1, _LIST_TYPES.get(fmt, "1"), start)
                    else:
                        levels[number] = (False, "", 1)
                self.list_styles[element.get(STYLE_NAME) or ""] = levels
            elif element.tag == STYLE_DEFAULT_STYLE and element.get(STYLE_FAMILY) == "paragraph":
                node = element.find(STYLE_TEXT_PROPS)
                if node is not None and node.get(_q("fo", "language")):
                    language = node.get(_q("fo", "language")) or ""
                    country = node.get(_q("fo", "country")) or ""
                    if language.lower() not in ("zxx", "none"):
                        self.default_lang = f"{language}-{country}" if country and country.lower() != "none" else language

    def _chain(self, table: dict[str, dict], name: str | None) -> list[dict]:
        chain: list[dict] = []
        seen: set[str] = set()
        while name and name not in seen and len(seen) < 16:
            seen.add(name)
            entry = table.get(name)
            if entry is None:
                break
            chain.append(entry)
            name = entry["parent"]
        return chain

    def _span_props(self, name: str | None) -> dict:
        key = ("text", name or "")
        if key not in self.props_cache:
            props: dict = {}
            for entry in reversed(self._chain(self.text_styles, name)):
                props.update(entry["props"])
            self.props_cache[key] = props
        return self.props_cache[key]

    def _para_style(self, name: str | None) -> tuple[str, int, dict, str | None, str | None]:
        """(heading kind, level, direct text props, alignment, list style name) of a paragraph style."""
        key = ("paragraph", name or "")
        if key in self.props_cache:
            return self.props_cache[key]["info"]
        chain = self._chain(self.para_styles, name)
        kind, level = "", 0
        # A text:p whose style carries an outline level was explicitly demoted to body text (LibreOffice
        # writes headings as text:h). Heading-named styles without outline levels (Writer/Web, HTML
        # imports) still mark headings on text:p.
        outline_style = any(entry["outline"] for entry in chain)
        for entry in chain:
            label = _style_label(entry)
            match = _HEADING_STYLE.fullmatch(label)
            if label == "title":
                kind, level = "title", 1
            elif label == "subtitle":
                kind, level = "subtitle", 2
            elif match and not outline_style:
                kind, level = "heading", int(match.group(1))
            else:
                continue
            break
        direct = dict(chain[0]["props"]) if chain and chain[0]["auto"] else {}
        align = next((entry["align"] for entry in chain if entry["align"]), None)
        list_style = next((entry["list"] for entry in chain if entry["list"]), None)
        info = (kind, level, direct, align, list_style)
        self.props_cache[key] = {"info": info}
        return info

    # ------------------------------------------------------------ package helpers

    def collect_anchors(self, body: ET.Element) -> None:
        wanted: set[str] = set()
        for node in body.iter():
            if node.tag in (T_BOOKMARK, T_BOOKMARK_START) and node.get(T_NAME):
                self.bookmarks.add(node.get(T_NAME) or "")
            elif node.tag in (T_A, DRAW_A):
                href = (node.get(XLINK_HREF) or "").strip()
                if href.startswith("#"):
                    wanted.add(unquote(href[1:]))
        self.anchor_ok = wanted & self.bookmarks

    def _link(self, href: str | None) -> str:
        href = (href or "").strip()
        if href.startswith("#"):
            name = unquote(href[1:])
            return "#" + _safe_id("bm-", name) if name in self.anchor_ok else ""
        return _http_link(href)

    def _image(self, element: ET.Element) -> str:
        href = (element.get(XLINK_HREF) or "").strip()
        if href:
            if self.zip is None:
                return ""
            target = resolve_href(self.base, href)
            if not target or target not in self.members:
                return ""
            if target in self.images:
                return self.images[target]
            try:
                data = _read_member(self.zip, target, MAX_IMAGE_BYTES)
            except FormatError:
                self.images[target] = ""
                return ""
            return _add_image(self.doc, self.images, target, data, posixpath.splitext(posixpath.basename(target))[0])
        binary = element.find(OFFICE_BINARY)
        if binary is None or not binary.text or len(binary.text) > MAX_IMAGE_BYTES * 4 // 3 + 65536:
            return ""
        try:
            data = base64.b64decode(binary.text, validate=False)
        except (binascii.Error, ValueError):
            return ""
        if len(data) > MAX_IMAGE_BYTES:
            return ""
        return _add_image(self.doc, self.images, "sha1:" + hashlib.sha1(data).hexdigest(), data, "image")

    # ------------------------------------------------------------ blocks

    def blocks(self, parent: ET.Element, depth: int) -> list[_Block]:
        blocks: list[_Block] = []
        if depth <= MAX_DEPTH:
            for child in parent:
                self._block(child, depth, blocks)
        return blocks

    def _block(self, child: ET.Element, depth: int, blocks: list[_Block]) -> None:
        tag = child.tag
        if tag in (T_P, T_H):
            blocks.extend(self._paragraph(child, depth, tag == T_H))
        elif tag == T_LIST:
            blocks.extend(self._list(child, depth, 0, None, None))
        elif tag == TABLE_TABLE:
            table = self._table(child, depth)
            if table is not None:
                blocks.append(table)
        elif tag in (T_SECTION, T_INDEX_BODY, T_INDEX_TITLE, T_NUMBERED_PARAGRAPH):
            blocks.extend(self.blocks(child, depth + 1))
        elif tag in _INDEXES_WITH_CONTENT:
            body = child.find(T_INDEX_BODY)
            if body is not None:
                blocks.extend(self.blocks(body, depth + 1))
        elif tag not in _SKIP_BLOCKS and _split(tag)[0] == _NS["draw"]:
            # a page- or paragraph-anchored frame outside any paragraph
            saved, self.pending = self.pending, []
            out = _Inline()
            self._inline_child(child, out, {}, depth + 1)
            markup = out.html()
            if out.content:
                blocks.append(_Block(f"<p>{markup}</p>"))
            blocks.extend(self.pending)
            self.pending = saved

    def _paragraph_inline(self, element: ET.Element, depth: int, drop_bold: bool,
                          props: dict) -> tuple[_Inline, list[_Block]]:
        saved, self.pending = self.pending, []
        out = _Inline(drop=("b",) if drop_bold else ())
        self._inline(element, out, props, depth)
        extra, self.pending = self.pending, saved
        return out, extra

    def _paragraph(self, element: ET.Element, depth: int, heading: bool) -> list[_Block]:
        kind, level, direct, align, _list_style = self._para_style(element.get(T_STYLE_NAME))
        if heading:
            kind, level = "heading", _int(element.get(T_OUTLINE_LEVEL), 1, 1, 10)
        is_heading = bool(kind)
        saved, self.pending = self.pending, []
        out = _Inline(drop=("b",) if is_heading else ())
        anchors, self.pending_anchors = self.pending_anchors, []
        for ident in anchors:
            out.anchor(ident)
        self._inline(element, out, {} if is_heading else direct, depth)
        extra, self.pending = self.pending, saved
        markup = out.html()
        blocks: list[_Block] = []
        if not out.content:  # keep bookmark targets of an empty paragraph for the next one
            self.pending_anchors = out.anchors + self.pending_anchors
        elif is_heading:
            text = out.plain_text()
            tag = f"h{min(level, 6)}"
            blocks.append(_Block(f"<{tag}>{markup}</{tag}>", 0 if kind == "subtitle" else level, text))
            if kind == "title" and not self.first_title:
                self.first_title = text
        elif align in ("center", "end", "right"):
            blocks.append(_Block(f'<p style="text-align: {"center" if align == "center" else "right"}">{markup}</p>'))
        else:
            blocks.append(_Block(f"<p>{markup}</p>"))
        blocks.extend(extra)
        return blocks

    def _list(self, element: ET.Element, depth: int, level: int, style: str | None, key: object) -> list[_Block]:
        """List items as item blocks (nesting by level) plus any tables they contain."""
        if depth > MAX_DEPTH:
            return []
        if element.find(f".//{T_H}") is not None and level == 0:
            return self._flatten(element, depth)  # outline-numbered headings wrapped in lists
        style = element.get(T_STYLE_NAME) or style
        if not style:
            first = element.find(f".//{T_P}")
            if first is not None:
                style = self._para_style(first.get(T_STYLE_NAME))[4]
        key = key if key is not None else ("list", id(element))
        levels = self.list_styles.get(style or "", {})
        spec = levels.get(level + 1) or (levels[max(levels)] if levels and max(levels) < level + 1 else None)
        ordered, list_type, start = spec if spec else (False, "", 1)
        blocks: list[_Block] = []
        for item in element:
            if item.tag not in (T_LIST_ITEM, T_LIST_HEADER):
                continue
            paragraphs: list[str] = []
            after: list[_Block] = []
            for child in item:
                if child.tag in (T_P, T_H):
                    out, extra = self._paragraph_inline(child, depth + 1, False,
                                                        self._para_style(child.get(T_STYLE_NAME))[2])
                    markup = out.html()
                    if out.content:
                        paragraphs.append(markup)
                    after.extend(extra)
                elif child.tag == T_LIST:
                    after.extend(self._list(child, depth + 1, level + 1, style, key))
                elif child.tag == TABLE_TABLE:
                    table = self._table(child, depth + 1)
                    if table is not None:
                        after.append(table)
            if paragraphs:
                inner = paragraphs[0] if len(paragraphs) == 1 else "".join(f"<p>{p}</p>" for p in paragraphs)
                restart = item.get(T_START_VALUE)
                if restart is not None and ordered:  # numbering restarts at this item
                    counts = self.counter.counts.setdefault(key, [0] * 10)
                    counts[min(level, 9)] = _int(restart, 1, 0, 1 << 20) - start
                blocks.append(_Block(inner, item=self.counter.item(key, level, ordered, list_type, start)))
            blocks.extend(after)
        return blocks

    def _flatten(self, element: ET.Element, depth: int) -> list[_Block]:
        blocks: list[_Block] = []
        for item in element:
            if item.tag in (T_LIST_ITEM, T_LIST_HEADER):
                blocks.extend(self.blocks(item, depth + 1))
        return blocks

    def _table(self, element: ET.Element, depth: int) -> _Block | None:
        if depth > MAX_DEPTH:
            return None
        rows: list[tuple[bool, list[tuple[int, int, str]]]] = []
        for row, header in self._rows(element, False, depth):
            cells: list[tuple[int, int, str]] = []
            for cell in row:
                if cell.tag != TABLE_CELL:
                    continue
                self.cell_depth += 1
                try:
                    inner = _join_blocks(self.blocks(cell, depth + 1))
                finally:
                    self.cell_depth -= 1
                span = _int(cell.get(TABLE_COLS_SPANNED), 1, 1, 1000)
                rowspan = _int(cell.get(TABLE_ROWS_SPANNED), 1, 1, 10000)
                repeat = _int(cell.get(TABLE_COLS_REPEATED), 1, 1, 64)
                cells.extend([(span, rowspan, inner)] * repeat)
            repeat = _int(row.get(TABLE_ROWS_REPEATED), 1, 1, 100)
            if not any(inner for _span, _rowspan, inner in cells):
                repeat = 1
            rows.extend([(header, cells)] * repeat)
        markup = _table_html(rows, self.cell_depth > 0)
        return _Block(markup) if markup else None

    def _rows(self, parent: ET.Element, header: bool, depth: int):
        for child in parent:
            if child.tag == TABLE_ROW:
                yield child, header
            elif child.tag == TABLE_HEADER_ROWS and depth < MAX_DEPTH:
                yield from self._rows(child, True, depth + 1)
            elif child.tag in TABLE_ROW_CONTAINERS and depth < MAX_DEPTH:
                yield from self._rows(child, header, depth + 1)

    # ------------------------------------------------------------ inline content

    def _emit(self, out: _Inline, text: str | None, props: dict) -> None:
        if text and not props.get("hidden"):
            out.text(text, _fmt(props))

    def _inline(self, element: ET.Element, out: _Inline, props: dict, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        self._emit(out, element.text, props)
        for child in element:
            self._inline_child(child, out, props, depth + 1)
            self._emit(out, child.tail, props)

    def _inline_child(self, child: ET.Element, out: _Inline, props: dict, depth: int) -> None:
        tag = child.tag
        if tag in _SKIP_INLINE:
            return
        if tag == T_SPAN:
            merged = dict(props)
            merged.update(self._span_props(child.get(T_STYLE_NAME)))
            self._inline(child, out, merged, depth)
        elif tag in (T_A, DRAW_A):
            opened = out.open_link(self._link(child.get(XLINK_HREF)))
            if tag == T_A:
                self._inline(child, out, props, depth)
            else:
                for node in child:
                    self._inline_child(node, out, props, depth + 1)
            if opened:
                out.close_link()
        elif tag == T_S:
            if not props.get("hidden"):
                out.text(" " * _int(child.get(T_C), 1, 1, 64), _fmt(props))
        elif tag == T_TAB:
            self._emit(out, " ", props)
        elif tag == T_LINE_BREAK:
            out.br()
        elif tag in (T_BOOKMARK, T_BOOKMARK_START):
            name = child.get(T_NAME) or ""
            ident = _safe_id("bm-", name)
            if name in self.anchor_ok and ident not in self.emitted_ids:
                self.emitted_ids.add(ident)
                out.anchor(ident)
        elif tag == T_NOTE:
            self._note(child, out, depth)
        elif tag == T_RUBY:
            base, annotation = child.find(T_RUBY_BASE), child.find(T_RUBY_TEXT)
            inner = _Inline()
            if base is not None:
                self._inline(base, inner, props, depth + 1)
            base_html = inner.html()
            if inner.content:
                text = "".join(annotation.itertext()).strip() if annotation is not None else ""
                if text:
                    out.ruby(base_html, text)
                else:
                    out.raw(base_html, True)
        elif tag == DRAW_FRAME:
            self._frame(child, out, depth)
        elif _split(tag)[0] == _NS["draw"]:
            self._shape(child, out, props, depth)
        elif tag in (T_LIST, TABLE_TABLE, T_P, T_H):  # block content inside a shape or a field
            self._block(child, depth, self.pending)
        else:  # fields (page number, date, references, ...) and unknown wrappers show their text
            self._inline(child, out, props, depth)

    def _frame(self, frame: ET.Element, out: _Inline, depth: int) -> None:
        alt = ""
        for child in frame:
            if child.tag in (_q("svg", "title"), _q("svg", "desc")) and child.text and not alt:
                alt = child.text.strip()
        image_done = False
        for child in frame:
            if child.tag == DRAW_IMAGE and not image_done:
                name = self._image(child)
                if name:
                    out.image(name, alt)
                    image_done = True
            elif child.tag == DRAW_TEXT_BOX:
                self.pending.extend(self.blocks(child, depth + 1))

    def _shape(self, shape: ET.Element, out: _Inline, props: dict, depth: int) -> None:
        """Groups and custom shapes: frames inside stay inline, shape text becomes following blocks."""
        if depth > MAX_DEPTH:
            return
        for child in shape:
            if child.tag in (DRAW_FRAME, DRAW_A) or _split(child.tag)[0] == _NS["draw"]:
                self._inline_child(child, out, props, depth + 1)
            elif child.tag in (T_P, T_H, T_LIST):
                self._block(child, depth + 1, self.pending)

    def _note(self, note: ET.Element, out: _Inline, depth: int) -> None:
        if self.in_note:
            return
        body = note.find(T_NOTE_BODY)
        if body is None:
            return
        saved, self.pending = self.pending, []
        self.in_note = True
        try:
            markup = _join_blocks(self.blocks(body, depth + 1) + self.pending)
        finally:
            self.in_note = False
            self.pending = saved
        if markup:
            out.note(self.notes.add(markup))


def _metadata(document: Document, meta: ET.Element | None) -> None:
    if meta is None:
        return
    values: dict[str, str] = {}
    for element in meta:
        namespace, local = _split(element.tag)
        text = "".join(element.itertext()).strip()
        if text and namespace in (_NS["dc"], _NS["meta"]):
            values.setdefault(local, text)
    document.title = values.get("title", "")
    author = values.get("initial-creator") or values.get("creator") or ""
    document.authors = [author] if author else []
    document.language = values.get("language", "")
    document.description = values.get("description") or values.get("subject") or ""


def _finish(converter: _OdfConverter, document: Document, office_text: ET.Element | None, path: Path) -> Document:
    if office_text is None:
        raise FormatError("not an OpenDocument text document")
    converter.collect_anchors(office_text)
    blocks = converter.blocks(office_text, 0)
    if not document.title and converter.first_title:
        document.title = converter.first_title
    if not document.language and converter.default_lang:
        document.language = converter.default_lang
    return _assemble(document, blocks, converter.notes, document.title or path.stem)


def _office_text(root: ET.Element) -> ET.Element | None:
    body = root.find(OFFICE_BODY)
    if body is None:
        return None
    return body.find(OFFICE_TEXT)


def _check_mimetype(value: bytes | str | None) -> None:
    if isinstance(value, str):
        value = value.encode("utf-8", "replace")
    value = (value or b"").strip()
    if value and not value.startswith(b"application/vnd.oasis.opendocument.text"):
        raise FormatError("not an OpenDocument text document")


def _convert_package(path: Path) -> Document:
    archive = _open_zip(path)
    try:
        members = set(archive.namelist())
        if "mimetype" in members:
            _check_mimetype(_read_member(archive, "mimetype", 1024))
        if "META-INF/manifest.xml" in members:
            manifest = _read_member(archive, "META-INF/manifest.xml", 16 * 1024 * 1024)
            if b"encryption-data" in manifest:
                raise FormatError("document is password-protected")
        if "content.xml" not in members:
            raise FormatError("OpenDocument package has no content.xml")
        content = _parse_xml(_read_member(archive, "content.xml", MAX_XML_BYTES), "content.xml")
        document = Document(source_format=_source_format(path, ("odt", "ott"), "odt"))
        converter = _OdfConverter(document, archive)
        if "styles.xml" in members:
            try:
                styles = _parse_xml(_read_member(archive, "styles.xml", MAX_XML_BYTES), "styles.xml")
                converter.load_styles(styles.find(OFFICE_STYLES), False)
            except FormatError:
                pass
        converter.load_styles(content.find(OFFICE_AUTO_STYLES), True)
        if "meta.xml" in members:
            try:
                meta = _parse_xml(_read_member(archive, "meta.xml", 16 * 1024 * 1024), "meta.xml")
                _metadata(document, meta.find(OFFICE_META))
            except FormatError:
                pass
        return _finish(converter, document, _office_text(content), path)
    finally:
        archive.close()


def _convert_flat(path: Path) -> Document:
    root = _parse_xml(read_limited(path, MAX_FLAT_BYTES), "flat OpenDocument")
    if root.tag != _q("office", "document"):
        raise FormatError("not a flat OpenDocument text file")
    _check_mimetype(root.get(_q("office", "mimetype")))
    document = Document(source_format=_source_format(path, ("fodt",), "fodt"))
    converter = _OdfConverter(document, None)
    converter.load_styles(root.find(OFFICE_STYLES), False)
    converter.load_styles(root.find(OFFICE_AUTO_STYLES), True)
    _metadata(document, root.find(OFFICE_META))
    return _finish(converter, document, _office_text(root), path)


def convert(path: Path) -> Document:
    """Convert an .odt / .ott package or a flat .fodt file into a :class:`Document`."""
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(8)
    if head.startswith(b"PK"):
        return _convert_package(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise FormatError("not an OpenDocument file (legacy binary office document)")
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"<") or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return _convert_flat(path)
    raise FormatError("not an OpenDocument text file")

