"""FictionBook 2 (.fb2) and zipped FictionBook (.fbz) -> :class:`Document`.

Elements are matched by local name (any or no namespace) so slightly broken
books still open. Metadata comes from ``description/title-info`` (falling back
to ``src-title-info``); the cover from ``coverpage/image``. The main body's
sections are flattened in reading order into chapters (section ``<title>`` ->
chapter title, else "Phần N"); text before the first section is a chapter of
its own. A body named ``notes`` / ``comments`` / ``footnotes`` becomes the final
"Ghi chú" chapter whose sections keep their ``id`` so note links resolve.
Images come from ``<binary>`` elements (base64) and only from there.

XML safety: documents declaring entities are refused (billion laughs) and
ElementTree never fetches external resources. Common breakage in the wild
(HTML named entities, bare ``&``, control characters) is repaired once before
giving up.
"""

from __future__ import annotations

import base64
import binascii
import html
import html.entities
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import (
    Chapter,
    Document,
    FormatError,
    decode_text,
    escape,
    open_zip,
    read_limited,
    read_member,
    sanitize_fragment,
    sniff_image_type,
)
from .htmldoc import clean_xml_text

MAX_FB2_BYTES = 256 * 1024 * 1024
MAX_BINARY_BYTES = 50 * 1024 * 1024
MAX_DEPTH = 64
NOTES_BODIES = {"notes", "comments", "footnotes"}

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_XML_DECL_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._:-]{1,40})[\"']")
_INLINE_TAGS = {"emphasis": "em", "strong": "strong", "strikethrough": "s", "sub": "sub", "sup": "sup", "code": "code"}


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _attr(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].lower() == name:
            return value
    return ""


def _children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in element if _local(child.tag) == name]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    found = _children(element, name)
    return found[0] if found else None


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _safe_id(value: str) -> str:
    return _SAFE_ID.sub("_", value.strip())[:120]


# ---------------------------------------------------------------- XML loading

def _decode(data: bytes) -> str:
    match = _XML_DECL_ENCODING.match(data[:1024])
    declared = match.group(1).decode("ascii", "ignore").lower() if match else ""
    if declared in {"utf-16", "utf-16le", "utf-16be"}:
        declared = ""  # BOM or the UTF-16 heuristic of decode_text decides the byte order
    return decode_text(data, declared)


def _repair(text: str) -> str:
    def named(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        return f"&#{codepoint};" if codepoint else f"&amp;{name};"

    def numeric(match: re.Match[str]) -> str:
        body = match.group(1)
        try:
            value = int(body[1:], 16) if body[:1] in "xX" else int(body)
        except ValueError:
            return ""
        valid = value in (0x9, 0xA, 0xD) or 0x20 <= value <= 0xD7FF or 0xE000 <= value <= 0xFFFD \
            or 0x10000 <= value <= 0x10FFFF
        return match.group(0) if valid else ""

    text = re.sub(r"&([A-Za-z][A-Za-z0-9]{0,31});", named, text)
    text = re.sub(r"&#([xX][0-9A-Fa-f]{1,8}|[0-9]{1,10});", numeric, text)
    return re.sub(r"&(?!(?:[A-Za-z][A-Za-z0-9]{0,31}|#[0-9]{1,10}|#[xX][0-9A-Fa-f]{1,8});)", "&amp;", text)


def parse_fb2(data: bytes) -> ET.Element:
    text = clean_xml_text(_decode(data))
    text = _XML_DECL.sub("", text, count=1)
    if re.search(r"<!ENTITY", text, re.IGNORECASE):
        raise FormatError("XML entity declarations are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair(text))
        except ET.ParseError as err:
            raise FormatError(f"corrupt FB2 XML ({err})") from err
    if _local(root.tag) != "fictionbook":
        raise FormatError("not a FictionBook document")
    return root


def _read_fbz(path: Path) -> bytes:
    with open_zip(path) as archive:
        names = sorted(info.filename for info in archive.infolist()
                       if not info.is_dir() and info.filename.lower().endswith(".fb2"))
        if not names:
            raise FormatError("FBZ archive contains no .fb2 file")
        return read_member(archive, names[0], MAX_FB2_BYTES)


# ---------------------------------------------------------------- rendering

class _Renderer:
    def __init__(self, document: Document, binaries: dict[str, ET.Element]) -> None:
        self.document = document
        self.binaries = binaries
        self.images: dict[str, str] = {}

    # -- resources
    def image_name(self, href: str) -> str:
        href = (href or "").strip()
        if not href.startswith("#"):
            return ""  # only embedded binaries; never external files or URLs
        binary_id = href[1:]
        if binary_id in self.images:
            return self.images[binary_id]
        name = ""
        element = self.binaries.get(binary_id)
        payload = (element.text or "") if element is not None else ""
        if payload and len(payload) <= MAX_BINARY_BYTES * 4 // 3 + 65536:
            data = b""
            for candidate in (payload, payload.strip() + "=="):
                try:
                    data = base64.b64decode(candidate, validate=False)
                    break
                except (binascii.Error, ValueError):
                    continue
            media_type = sniff_image_type(data) if data else ""
            if media_type and media_type != "image/svg+xml" and len(data) <= MAX_BINARY_BYTES:
                name = self.document.add_resource(data, media_type, binary_id or "image")
        self.images[binary_id] = name
        return name

    @staticmethod
    def id_attr(element: ET.Element) -> str:
        value = _safe_id(_attr(element, "id"))
        return f' id="{value}"' if value else ""

    # -- inline content
    def inline(self, element: ET.Element, depth: int = 0) -> str:
        if depth > MAX_DEPTH:
            return escape("".join(element.itertext()))
        parts = [escape(element.text or "")]
        for child in element:
            parts.append(self.inline_element(child, depth + 1))
            parts.append(escape(child.tail or ""))
        return "".join(parts)

    def inline_element(self, element: ET.Element, depth: int) -> str:
        name = _local(element.tag)
        if name in _INLINE_TAGS:
            tag = _INLINE_TAGS[name]
            return f"<{tag}>{self.inline(element, depth)}</{tag}>"
        if name == "a":
            inner = self.inline(element, depth)
            href = _attr(element, "href").strip()
            if href.startswith("#"):
                target = _safe_id(href[1:])
                link = f'<a href="#{target}" class="note">{inner}</a>' if target else inner
                if _attr(element, "type").lower() == "note" and "<sup>" not in inner:
                    return f"<sup>{link}</sup>"
                return link
            if re.match(r"^https?://", href, re.IGNORECASE):
                return f'<a href="{escape(href)}">{inner}</a>'
            return inner
        if name == "image":
            src = self.image_name(_attr(element, "href"))
            if not src:
                return ""
            return f'<img src="{src}" alt="{escape(_attr(element, "alt"))}"/>'
        if name in ("style", "span"):
            return f"<span>{self.inline(element, depth)}</span>"
        if name in ("p", "v", "subtitle", "text-author", "date"):
            return self.inline(element, depth) + " "
        return self.inline(element, depth)

    # -- block content
    def blocks(self, element: ET.Element, depth: int, heading: int = 3) -> str:
        """Render the children of a block container (stray text becomes paragraphs)."""
        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(f"<p>{escape(element.text.strip())}</p>")
        for child in element:
            parts.append(self.block(child, depth + 1, heading))
            if child.tail and child.tail.strip():
                parts.append(f"<p>{escape(child.tail.strip())}</p>")
        return "".join(parts)

    def title_block(self, element: ET.Element, level: int, extra_id: str = "") -> str:
        lines = [self.inline(p) for p in element if _local(p.tag) in ("p", "subtitle")]
        if not lines and _text(element):
            lines = [escape(_text(element))]
        lines = [line for line in lines if line.strip()]
        if not lines:
            return f'<div id="{extra_id}"></div>' if extra_id else ""
        id_attr = f' id="{extra_id}"' if extra_id else self.id_attr(element)
        level = max(1, min(level, 6))
        return f"<h{level}{id_attr}>{'<br/>'.join(lines)}</h{level}>"

    def block(self, element: ET.Element, depth: int, heading: int = 3) -> str:
        name = _local(element.tag)
        if depth > MAX_DEPTH:
            return f"<p>{escape(_text(element))}</p>"
        ident = self.id_attr(element)
        if name == "p":
            return f"<p{ident}>{self.inline(element)}</p>"
        if name == "empty-line":
            return '<p class="empty-line">\u00a0</p>'
        if name == "subtitle":
            return f'<p class="subtitle"{ident} style="text-align: center"><strong>{self.inline(element)}</strong></p>'
        if name == "image":
            src = self.image_name(_attr(element, "href"))
            if not src:
                return ""
            alt = escape(_attr(element, "alt") or _attr(element, "title"))
            return f'<div class="image"{ident}><img src="{src}" alt="{alt}"/></div>'
        if name == "title":
            return self.title_block(element, heading)
        if name == "epigraph":
            return f'<blockquote class="epigraph"{ident}>{self.blocks(element, depth, 5)}</blockquote>'
        if name == "cite":
            return f'<blockquote class="cite"{ident}>{self.blocks(element, depth, 5)}</blockquote>'
        if name == "text-author":
            return f'<p class="text-author" style="text-align: right"><em>{self.inline(element)}</em></p>'
        if name == "poem":
            return f'<div class="poem"{ident}>{self.blocks(element, depth, 4)}</div>'
        if name == "stanza":
            return f'<div class="stanza"{ident}>{self.blocks(element, depth, 5)}</div>'
        if name == "v":
            return f'<p class="verse"{ident}>{self.inline(element)}</p>'
        if name == "date":
            return f'<p class="date" style="text-align: right"><em>{self.inline(element)}</em></p>'
        if name == "table":
            return self.table(element)
        if name in ("section", "annotation", "history"):
            return f'<div class="{name}"{ident}>{self.blocks(element, depth, heading + 1)}</div>'
        if name in _INLINE_TAGS or name in ("a", "style"):
            return f"<p>{self.inline_element(element, depth)}</p>"
        if len(element):
            return self.blocks(element, depth, heading)
        text = (element.text or "").strip()
        return f"<p>{escape(text)}</p>" if text else ""

    def table(self, element: ET.Element) -> str:
        rows: list[str] = []
        for row in element:
            if _local(row.tag) != "tr":
                continue
            cells: list[str] = []
            for cell in row:
                tag = _local(cell.tag)
                if tag not in ("td", "th"):
                    continue
                attrs = ""
                for span in ("colspan", "rowspan"):
                    value = _attr(cell, span)
                    if value.isdigit() and 0 < int(value) < 1000:
                        attrs += f' {span}="{int(value)}"'
                align = _attr(cell, "align").lower()
                if align in ("left", "right", "center"):
                    attrs += f' style="text-align: {align}"'
                cells.append(f"<{tag}{attrs}>{self.inline(cell)}</{tag}>")
            if cells:
                rows.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows)}</table>" if rows else ""

    # -- chapters
    def body_chapters(self, body: ET.Element, chapters: list[tuple[str | None, str]], doc_title: str) -> None:
        preamble: list[str] = []
        body_title = ""
        trailing = False
        for child in body:
            name = _local(child.tag)
            if name == "section":
                if preamble:
                    chapters.append((body_title or doc_title or None, "".join(preamble)))
                    preamble = []
                self.section(child, 1, chapters)
                trailing = True
                continue
            if name == "title":
                body_title = body_title or self.plain_title(child)
                preamble.append(self.title_block(child, 1))
            else:
                preamble.append(self.block(child, 1, 2))
        if preamble and any(part for part in preamble):
            label = body_title or (None if trailing else doc_title or None)
            chapters.append((label, "".join(preamble)))

    @staticmethod
    def plain_title(element: ET.Element) -> str:
        parts = [_text(p) for p in element if _local(p.tag) in ("p", "subtitle")] or [_text(element)]
        joined = ""
        for part in (part for part in parts if part):
            if joined:
                joined += " " if joined[-1] in ".:!?…—-" else ". "
            joined += part
        return joined[:300]

    def section(self, section: ET.Element, depth: int, chapters: list[tuple[str | None, str]]) -> None:
        if depth > MAX_DEPTH:
            chapters.append((None, self.blocks(section, depth)))
            return
        title = ""
        own: list[str] = []
        section_id = _safe_id(_attr(section, "id"))
        anchor_used = False
        emitted = False
        for child in section:
            name = _local(child.tag)
            if name == "section":
                if not emitted and (own or title):
                    if section_id and not anchor_used:
                        own.insert(0, f'<div id="{section_id}"></div>')
                        anchor_used = True
                    chapters.append((title or None, "".join(own)))
                    own = []
                emitted = True
                self.section(child, depth + 1, chapters)
                continue
            if name == "title" and not title and not own:
                title = self.plain_title(child)
                own.append(self.title_block(child, depth, section_id))
                anchor_used = anchor_used or bool(section_id)
                continue
            own.append(self.block(child, depth, min(depth + 1, 6)))
        if section.text and section.text.strip():
            own.insert(0, f"<p>{escape(section.text.strip())}</p>")
        if own and any(own):
            if section_id and not anchor_used:
                own.insert(0, f'<div id="{section_id}"></div>')
            chapters.append((title or None, "".join(own)))

    def notes(self, body: ET.Element) -> str:
        parts: list[str] = []
        for child in body:
            name = _local(child.tag)
            if name == "title":
                parts.append(self.title_block(child, 2))
            elif name == "section":
                parts.append(self.note_section(child, 1))
            else:
                parts.append(self.block(child, 1, 3))
        return "".join(parts)

    def note_section(self, section: ET.Element, depth: int) -> str:
        parts: list[str] = []
        for child in section:
            name = _local(child.tag)
            if name == "title":
                label = "<br/>".join(self.inline(p) for p in child if _local(p.tag) in ("p", "subtitle")) \
                    or escape(_text(child))
                parts.append(f'<p class="note-title"><strong>{label}</strong></p>')
            elif name == "section" and depth < MAX_DEPTH:
                parts.append(self.note_section(child, depth + 1))
            else:
                parts.append(self.block(child, depth, 4))
        return f'<div class="note"{self.id_attr(section)}>{"".join(parts)}</div>'


# ---------------------------------------------------------------- converter

def _metadata(root: ET.Element, document: Document) -> str:
    """Fill title / authors / language / description; return the cover image href."""
    description = _child(root, "description")
    info = _child(description, "title-info")
    if info is None or not _text(_child(info, "book-title")):
        source_info = _child(description, "src-title-info")
        if source_info is not None:
            info = source_info
    document.title = _text(_child(info, "book-title"))[:300]
    for author in _children(info, "author")[:20]:
        name = " ".join(_text(_child(author, part)) for part in ("first-name", "middle-name", "last-name"))
        name = re.sub(r"\s+", " ", name).strip() or _text(_child(author, "nickname"))
        if name and name not in document.authors:
            document.authors.append(name[:200])
    language = _text(_child(info, "lang"))
    if re.fullmatch(r"[A-Za-z]{1,8}(?:[-_][A-Za-z0-9]{1,8})*", language):
        document.language = language
    annotation = _child(info, "annotation")
    if annotation is not None:
        lines = [_text(element) for element in annotation] or [_text(annotation)]
        if annotation.text and annotation.text.strip():
            lines.insert(0, annotation.text.strip())
        document.description = "\n".join(line for line in lines if line)[:5000]
    cover = _child(info, "coverpage")
    if cover is not None:
        for element in cover.iter():
            if _local(element.tag) == "image":
                return _attr(element, "href")
    return ""


def convert(path: Path) -> Document:
    path = Path(path)
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic in (b"PK\x03\x04", b"PK\x05\x06"):
        data = _read_fbz(path)
        source_format = "fbz"
    else:
        data = read_limited(path, MAX_FB2_BYTES)
        source_format = "fb2"
    root = parse_fb2(data)
    document = Document(source_format=source_format)
    cover_href = _metadata(root, document)
    binaries = {}
    for element in root:
        if _local(element.tag) == "binary":
            binary_id = _attr(element, "id")
            if binary_id and binary_id not in binaries:
                binaries[binary_id] = element
    renderer = _Renderer(document, binaries)
    if cover_href:
        name = renderer.image_name(cover_href)
        document.cover = next((r for r in document.resources if r.name == name), None) if name else None

    collected: list[tuple[str | None, str]] = []
    notes: list[str] = []
    for body in _children(root, "body"):
        if _attr(body, "name").strip().lower() in NOTES_BODIES:
            notes.append(renderer.notes(body))
        else:
            renderer.body_chapters(body, collected, document.title)
    names = {resource.name for resource in document.resources}
    keep = (lambda src: src if src in names else "")
    for index, (title, markup) in enumerate(collected, 1):
        fragment = sanitize_fragment(markup, resolve_src=keep)
        if fragment.strip():
            document.chapters.append(Chapter(title or f"Phần {index}", fragment))
    notes_html = sanitize_fragment("".join(notes), resolve_src=keep) if notes else ""
    if notes_html.strip():
        document.chapters.append(Chapter("Ghi chú", notes_html))
    if not document.chapters:
        raise FormatError("FB2 document has no readable content")
    return document
