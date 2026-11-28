"""WordprocessingML (.docx / .docm / .dotx / .dotm) to :class:`Document`.

The package goes through :func:`open_zip`; the main part is located through
``_rels/.rels`` (officeDocument relationship) or ``[Content_Types].xml`` instead
of assuming ``word/document.xml``. Macros (``vbaProject.bin``) are never read.

Besides :func:`convert`, this module hosts the small private helpers the ODF and
HWPX converters share (guarded XML parsing, the inline markup writer, list
grouping and chapter assembly with per-chapter "Ghi chú" note sections).
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .base import (
    MAX_CHAPTERS, Chapter, Document, FormatError, decode_text, escape, open_zip, read_member, resolve_href,
    sanitize_fragment, sniff_image_type,
)

# ---------------------------------------------------------------- shared helpers (also used by odf / hwpx)

MAX_XML_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_FLAT_BYTES = 384 * 1024 * 1024
MAX_DEPTH = 40
NOTES_TITLE = "Ghi chú"
LEADING_TITLE = "Mở đầu"

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_HTTP_LINK = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
_NATIVE_ENCODINGS = {"utf-8", "utf8", "utf-16", "utf16", "us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1"}
_NOTE_REF = re.compile(r'id="nref(\d+)"')


_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, OSError, RuntimeError)


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """:func:`read_member` with corrupt-member errors (bad CRC, broken deflate stream) as :class:`FormatError`."""
    try:
        return read_member(archive, name, limit)
    except FormatError:
        raise
    except _ZIP_ERRORS as err:
        raise FormatError(f"corrupt archive member: {name}") from err


def _open_zip(path: Path) -> zipfile.ZipFile:
    """:func:`open_zip` with every container-level failure reported as :class:`FormatError`."""
    try:
        return open_zip(path)
    except FormatError:
        raise
    except (*_ZIP_ERRORS, ValueError) as err:
        raise FormatError("corrupt ZIP container") from err


def _parse_xml(data: bytes, what: str) -> ET.Element:
    """Parse untrusted XML; DOCTYPEs declaring entities (billion laughs) are refused."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff", b"<\x00", b"\x00<"):
        encoding = {b"<\x00": "utf-16-le", b"\x00<": "utf-16-be"}.get(data[:2], "utf-16")
        probe = data.decode(encoding, errors="replace")
        has_entities = "<!DOCTYPE" in probe and "<!ENTITY" in probe
    else:
        has_entities = b"<!DOCTYPE" in data and b"<!ENTITY" in data
    if has_entities:
        raise FormatError(f"{what}: XML entity declarations are not allowed")
    try:
        return ET.fromstring(data)
    except ValueError as err:  # ET.ParseError is a SyntaxError; multi-byte declared encodings raise ValueError
        match = _XML_ENCODING.match(data[:200])
        declared = match.group(1).decode("ascii").lower() if match else ""
        if not declared or declared in _NATIVE_ENCODINGS:
            raise FormatError(f"{what}: unreadable XML ({err})") from err
        text = decode_text(data, declared)
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
        try:
            return ET.fromstring(text.encode("utf-8"))
        except (ET.ParseError, ValueError) as again:
            raise FormatError(f"{what}: malformed XML") from again
    except ET.ParseError as err:
        raise FormatError(f"{what}: malformed XML") from err


@lru_cache(maxsize=4096)
def _split(tag: str) -> tuple[str, str]:
    if tag[:1] == "{":
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _local(tag: object) -> str:
    return _split(tag)[1] if isinstance(tag, str) else ""


def _walk(element: ET.Element, stop: frozenset[str] = frozenset(), limit: int = 200000):
    """Pre-order descendants of ``element`` without descending below nodes whose local name is in ``stop``."""
    stack = list(reversed(list(element)))
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        yield node
        if _local(node.tag) not in stop:
            stack.extend(reversed(list(node)))


def _safe_id(prefix: str, name: str) -> str:
    return prefix + _ID_UNSAFE.sub("_", name)[:100]


def _http_link(url: str | None) -> str:
    url = (url or "").strip()
    return url if _HTTP_LINK.match(url) else ""


def _clean_title(text: str) -> str:
    return " ".join(text.split())[:200]


def _fmt(props: dict) -> tuple[str, ...]:
    """Inline formatting tags (canonical order) from a {b,i,u,s,va} property dict."""
    tags = [key for key in ("b", "i", "u", "s") if props.get(key)]
    if props.get("va") in ("sup", "sub"):
        tags.append(props["va"])
    return tuple(tags)


def _add_image(document: Document, cache: dict[str, str], key: str, data: bytes, stem: str = "img") -> str:
    """Register raster image bytes once per ``key``; '' for EMF/WMF/SVG/unknown data."""
    if key in cache:
        return cache[key]
    media_type = sniff_image_type(data)
    name = ""
    if media_type and media_type != "image/svg+xml":
        name = document.add_resource(data, media_type, stem or "img")
    cache[key] = name
    return name


class _Inline:
    """Builds the inline XHTML of one paragraph from escaped text and simple tags."""

    __slots__ = ("parts", "plain", "fmt", "content", "links", "drop", "anchors")

    def __init__(self, drop: tuple[str, ...] = ()) -> None:
        self.parts: list[str] = []
        self.plain: list[str] = []
        self.fmt: tuple[str, ...] = ()
        self.content = False
        self.links = 0
        self.drop = drop
        self.anchors: list[str] = []

    def _set(self, fmt: tuple[str, ...]) -> None:
        if fmt == self.fmt:
            return
        for tag in reversed(self.fmt):
            self.parts.append(f"</{tag}>")
        for tag in fmt:
            self.parts.append(f"<{tag}>")
        self.fmt = fmt

    def text(self, value: str | None, fmt: tuple[str, ...] = ()) -> None:
        if not value:
            return
        value = _CONTROL_CHARS.sub("", value)
        if not value:
            return
        if self.drop or self.links:
            fmt = tuple(tag for tag in fmt if tag not in self.drop and not (self.links and tag == "u"))
        self._set(fmt)
        self.parts.append(escape(value))
        self.plain.append(value)
        if not self.content and not value.isspace():
            self.content = True

    def raw(self, markup: str, content: bool = False) -> None:
        self._set(())
        self.parts.append(markup)
        if content:
            self.content = True

    def br(self) -> None:
        self.raw("<br/>")
        self.plain.append(" ")

    def image(self, name: str, alt: str = "") -> None:
        self.raw(f'<img src="{escape(name)}" alt="{escape(_clean_title(alt))}"/>', True)

    def anchor(self, ident: str) -> None:
        self.raw(f'<span id="{escape(ident)}"></span>')
        self.anchors.append(ident)

    def note(self, number: int) -> None:
        if self.links:  # no nested <a>: the reference itself stays the back-link target
            self.raw(f'<sup id="nref{number}">{number}</sup>', True)
        else:
            self.raw(f'<sup><a class="noteref" id="nref{number}" href="#note{number}">{number}</a></sup>', True)

    def ruby(self, base_html: str, annotation: str) -> None:
        self.raw(f"<ruby>{base_html}<rt>{escape(annotation)}</rt></ruby>", True)
        self.plain.append(annotation)

    def open_link(self, href: str) -> bool:
        if self.links or not href:
            return False
        self.raw(f'<a href="{escape(href)}">')
        self.links = 1
        return True

    def close_link(self) -> None:
        if self.links:
            self.raw("</a>")
            self.links = 0

    def html(self) -> str:
        self.close_link()
        self._set(())
        return "".join(self.parts)

    def plain_text(self) -> str:
        return _clean_title("".join(self.plain))


@dataclass(slots=True)
class _Block:
    html: str
    level: int = 0
    """Outline level used for chapter splitting (0 = not a chapter heading)."""
    title: str = ""
    item: tuple | None = None
    """List item: (nesting level, "ul"/"ol", extra list attributes, list key); ``html`` is the item content."""


class _Notes:
    """Footnote / endnote bodies numbered in reference order."""

    def __init__(self) -> None:
        self.bodies: dict[int, str] = {}

    def add(self, body_html: str) -> int:
        number = len(self.bodies) + 1
        self.bodies[number] = body_html
        return number


def _sanitizer_keeps(markup: str) -> bool:
    try:
        return sanitize_fragment(markup) == markup
    except FormatError:
        return False


# The shared sanitizer implicitly closes an open <li>/<td> when it meets another one, even inside a
# nested list or table. Probe it once: nest properly when it copes, otherwise emit flat structures.
_NESTED_LISTS_OK = _sanitizer_keeps("<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>")
_NESTED_TABLES_OK = _sanitizer_keeps(
    "<table><tbody><tr><td><table><tbody><tr><td>x</td></tr></tbody></table></td></tr></tbody></table>")


class _ListCounter:
    """Item numbers per list and level, so split or interrupted lists keep counting (``start``)."""

    def __init__(self) -> None:
        self.counts: dict[object, list[int]] = {}

    def item(self, key: object, level: int, ordered: bool, list_type: str = "1", start: int | None = 1) -> tuple:
        level = max(0, min(int(level), 9))
        counts = self.counts.setdefault(key, [0] * 10)
        counts[level] += 1
        for deeper in range(level + 1, 10):
            counts[deeper] = 0
        if not ordered:
            return (level, "ul", "", key)
        number = (1 if start is None else start) + counts[level] - 1
        attrs = f' type="{list_type}"' if list_type and list_type != "1" else ""
        if number != 1:
            attrs += f' start="{number}"'
        return (level, "ol", attrs, key)


def _render_list(items: list[_Block]) -> str:
    if not _NESTED_LISTS_OK:
        return _render_flat_list(items)
    parts: list[str] = []
    stack: list[tuple[int, str, object]] = []
    for block in items:
        level, tag, attrs, key = block.item
        level = max(0, min(int(level), 9))
        while stack and stack[-1][0] > level:
            parts.append(f"</li></{stack.pop()[1]}>")
        if stack and stack[-1][0] == level:
            if (stack[-1][1], stack[-1][2]) != (tag, key):
                parts.append(f"</li></{stack.pop()[1]}>")
            else:
                parts.append("</li>")
        if not stack or stack[-1][0] < level:
            parts.append(f"<{tag}{attrs}>")
            stack.append((level, tag, key))
        parts.append(f"<li>{block.html}")
    while stack:
        parts.append(f"</li></{stack.pop()[1]}>")
    return "".join(parts)


def _render_flat_list(items: list[_Block]) -> str:
    """Nesting expressed as indented sibling lists (each keeps its numbering through ``start``)."""
    parts: list[str] = []
    current: tuple | None = None
    for block in items:
        level, tag, attrs, key = block.item
        level = max(0, min(int(level), 9))
        signature = (level, tag, key)
        if signature != current:
            if current is not None:
                parts.append(f"</{current[1]}>")
            indent = f' style="margin-left: {level * 2}em"' if level else ""
            parts.append(f"<{tag}{attrs}{indent}>")
            current = signature
        parts.append(f"<li>{block.html}</li>")
    if current is not None:
        parts.append(f"</{current[1]}>")
    return "".join(parts)


def _table_html(rows: list[tuple[bool, list[tuple[int, int, str]]]], nested: bool) -> str:
    """<table> markup from rows of (colspan, rowspan, inner html) cells; '' when every cell is empty.

    A table nested in another table's cell is unwrapped into its cell contents when the
    sanitizer cannot keep nested tables intact.
    """
    if not any(inner for _header, cells in rows for _span, _rowspan, inner in cells):
        return ""
    if nested and not _NESTED_TABLES_OK:
        return "".join(inner if inner.startswith("<") else f"<p>{inner}</p>"
                       for _header, cells in rows for _span, _rowspan, inner in cells if inner)
    rendered_rows: list[str] = []
    for header, cells in rows:
        tag = "th" if header else "td"
        rendered: list[str] = []
        for span, rowspan, inner in cells:
            attrs = (f' colspan="{span}"' if span > 1 else "") + (f' rowspan="{rowspan}"' if rowspan > 1 else "")
            rendered.append(f"<{tag}{attrs}>{inner}</{tag}>")
        if rendered:
            rendered_rows.append("<tr>" + "".join(rendered) + "</tr>")
    return "<table><tbody>" + "".join(rendered_rows) + "</tbody></table>"


def _group_lists(blocks: list[_Block]) -> list[_Block]:
    """Merge consecutive list-item blocks into (nested) <ul>/<ol> blocks."""
    grouped: list[_Block] = []
    run: list[_Block] = []
    for block in blocks:
        if block.item is not None:
            run.append(block)
            continue
        if run:
            grouped.append(_Block(_render_list(run)))
            run = []
        grouped.append(block)
    if run:
        grouped.append(_Block(_render_list(run)))
    return grouped


def _join_blocks(blocks: list[_Block]) -> str:
    return "".join(block.html for block in _group_lists(blocks))


def _notes_section(numbers: list[int], notes: _Notes) -> str:
    items: list[str] = []
    for number in numbers:
        body = notes.bodies.get(number)
        if not body:
            continue
        back = f'<a class="noteback" href="#nref{number}">{number}.</a> '
        if body.startswith("<p>"):
            body = "<p>" + back + body[3:]
        elif body.startswith("<p "):
            end = body.find(">") + 1
            body = body[:end] + back + body[end:]
        else:
            body = f"<p>{back}</p>{body}"
        items.append(f'<div class="note" id="note{number}">{body}</div>')
    if not items:
        return ""
    return f'<section class="notes"><h2>{escape(NOTES_TITLE)}</h2>{"".join(items)}</section>'


def _assemble(document: Document, blocks: list[_Block], notes: _Notes, fallback_title: str) -> Document:
    """Split blocks into chapters at the highest heading level used at least twice, then sanitize."""
    blocks = [block for block in _group_lists(blocks) if block.html.strip()]
    if not blocks:
        raise FormatError("document has no readable content")
    split = 0
    for level in (1, 2):
        if sum(1 for block in blocks if block.level == level) >= 2:
            split = level
            break
    segments: list[tuple[str | None, list[str]]] = []
    if split:
        title: str | None = None
        parts: list[str] = []
        for block in blocks:
            if block.level and block.level <= split:
                if parts:
                    segments.append((title, parts))
                title, parts = block.title, [block.html]
            else:
                parts.append(block.html)
        if parts:
            segments.append((title, parts))
        if len(segments) > MAX_CHAPTERS:
            segments = []
    if not segments:
        segments = [(None, [block.html for block in blocks])]
    names = {resource.name for resource in document.resources}

    def resolve_src(src: str) -> str:
        return src if src in names else ""

    for index, (title, parts) in enumerate(segments, 1):
        markup = "".join(parts)
        refs = list(dict.fromkeys(int(number) for number in _NOTE_REF.findall(markup)))
        if refs:
            markup += _notes_section(refs, notes)
        if title is None:
            name = (document.title or fallback_title) if len(segments) == 1 else (document.title or LEADING_TITLE)
        else:
            name = title
        name = _clean_title(name) or f"Phần {index}"
        document.chapters.append(Chapter(name, sanitize_fragment(markup, resolve_src=resolve_src)))
    return document


def _source_format(path: Path, allowed: tuple[str, ...], default: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    return ext if ext in allowed else default


# ---------------------------------------------------------------- WordprocessingML

_W_NS = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main", "http://purl.oclc.org/ooxml/wordprocessingml/main")
_R_NS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
         "http://purl.oclc.org/ooxml/officeDocument/relationships")
_A_NS = ("http://schemas.openxmlformats.org/drawingml/2006/main", "http://purl.oclc.org/ooxml/drawingml/main")
_M_NS = ("http://schemas.openxmlformats.org/officeDocument/2006/math", "http://purl.oclc.org/ooxml/officeDocument/math")
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_VML_NS = "urn:schemas-microsoft-com:vml"
_O_NS = "urn:schemas-microsoft-com:office:office"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_W_SET = frozenset(_W_NS)
_M_TEXT = frozenset((namespace, "t") for namespace in _M_NS)
_WA = tuple("{%s}" % ns for ns in _W_NS)
_RA = tuple("{%s}" % ns for ns in _R_NS)

_MAIN_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "application/vnd.ms-word.document.macroenabled.main+xml",
    "application/vnd.ms-word.template.macroenabledtemplate.main+xml",
})
_HEADING_STYLE = re.compile(r"heading\s*([1-9])")
_QUOTE_STYLES = frozenset({"quote", "intense quote"})
_OL_TYPES = {"decimal": "1", "lowerLetter": "a", "upperLetter": "A", "lowerRoman": "i", "upperRoman": "I"}
_DROP_FIELDS = frozenset({"TOC", "INDEX", "TOA"})
_FIELD_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
_INLINE_WRAPPERS = frozenset({"smartTag", "customXml", "ins", "moveTo", "dir", "bdo", "sdtContent"})
_BLOCK_WRAPPERS = frozenset({"customXml", "ins", "moveTo", "smartTag"})
_SYMBOL_GREEK = "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"
_MAX_FIELDS = 32


def _wl(element: ET.Element) -> str:
    """Local name of a WordprocessingML element ('' for other namespaces)."""
    namespace, local = _split(element.tag)
    return local if namespace in _W_SET else ""


def _wa(element: ET.Element, name: str) -> str | None:
    for prefix in _WA:
        value = element.get(prefix + name)
        if value is not None:
            return value
    return None


def _ra(element: ET.Element, name: str) -> str | None:
    for prefix in _RA:
        value = element.get(prefix + name)
        if value is not None:
            return value
    return None


def _wchild(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _wl(child) == name:
            return child
    return None


def _on(element: ET.Element) -> bool:
    """OOXML toggle property: present and not explicitly switched off."""
    value = _wa(element, "val")
    return value is None or value.strip().lower() not in ("0", "false", "off", "none")


def _int(value: str | None, default: int = 0, low: int = 0, high: int = 1 << 30) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class _Rel:
    type: str
    target: str
    external: bool


@dataclass(slots=True)
class _Field:
    instr: str = ""
    result: bool = False
    drop: bool = False
    link_open: bool = False


class _DocxConverter:
    def __init__(self, archive, path: Path) -> None:
        self.zip = archive
        self.path = path
        self.members = {name.lower(): name for name in archive.namelist()}
        self.doc = Document(source_format=_source_format(path, ("docx", "docm", "dotx", "dotm"), "docx"))
        self.notes = _Notes()
        self.images: dict[str, str] = {}
        self.rels: dict[str, _Rel] = {}
        self.main_rels: dict[str, _Rel] = {}
        self.pstyles: dict[str, dict] = {}
        self.cstyles: dict[str, dict] = {}
        self.nstyles: dict[str, dict] = {}
        self.para_cache: dict[str | None, tuple] = {}
        self.cstyle_cache: dict[str, dict] = {}
        self.abstracts: dict[str, dict] = {}
        self.nums: dict[str, tuple[str, dict]] = {}
        self.has_numbering = False
        self.counter = _ListCounter()
        self.cell_depth = 0
        self.fields: list[_Field] = []
        self.field_overflow = 0
        self.pending: list[_Block] = []
        self.pending_anchors: list[str] = []
        self.emitted_ids: set[str] = set()
        self.bookmarks: set[str] = set()
        self.anchor_ok: set[str] = set()
        self.note_parts: dict[str, tuple[dict[str, _Rel], dict[str, ET.Element]]] = {}
        self.in_note = False
        self.first_title = ""
        self.default_lang = ""

    # ------------------------------------------------------------ package

    def _member(self, name: str) -> str | None:
        return self.members.get(name.lower()) if name else None

    def _xml(self, name: str) -> ET.Element:
        member = self._member(name)
        if member is None:
            raise FormatError(f"missing package part: {name}")
        return _parse_xml(_read_member(self.zip, member, MAX_XML_BYTES), member)

    def _read_rels(self, part: str) -> dict[str, _Rel]:
        rels_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if self._member(rels_name) is None:
            return {}
        rels: dict[str, _Rel] = {}
        for rel in self._xml(rels_name):
            if _local(rel.tag) != "Relationship":
                continue
            rid, target = rel.get("Id"), rel.get("Target") or ""
            if not rid:
                continue
            external = (rel.get("TargetMode") or "").lower() == "external"
            if not external:
                target = resolve_href("", target.lstrip("/")) if target.startswith("/") else resolve_href(part, target)
            rels[rid] = _Rel(rel.get("Type") or "", target, external)
        return rels

    @staticmethod
    def _rel_target(rels: dict[str, _Rel], suffix: str) -> str:
        for rel in rels.values():
            if not rel.external and rel.target and rel.type.endswith(suffix):
                return rel.target
        return ""

    def _main_part(self) -> tuple[str, dict[str, _Rel]]:
        overrides: dict[str, str] = {}
        if self._member("[Content_Types].xml"):
            for item in self._xml("[Content_Types].xml"):
                if _local(item.tag) == "Override" and item.get("PartName"):
                    overrides[item.get("PartName").lstrip("/").lower()] = (item.get("ContentType") or "").lower()
        package_rels = self._read_rels("")
        main = ""
        for rel in package_rels.values():
            if rel.type.endswith("/officeDocument") and not rel.external and self._member(rel.target):
                main = rel.target
                break
        if not main:
            main = next((part for part, kind in overrides.items() if kind in _MAIN_TYPES and self._member(part)), "")
        if not main and self._member("word/document.xml"):
            main = "word/document.xml"
        if not main:
            raise FormatError("not a Word document (no main document part)")
        kind = overrides.get(main.lower(), "")
        if kind and kind not in _MAIN_TYPES and "wordprocessingml" not in kind and "ms-word" not in kind:
            raise FormatError("not a Word document")
        return self._member(main) or main, package_rels

    # ------------------------------------------------------------ styles / numbering / metadata

    def _rpr_props(self, rpr: ET.Element) -> dict:
        props: dict = {}
        for child in rpr:
            name = _wl(child)
            if name in ("b", "i", "vanish"):
                props[name] = _on(child)
            elif name in ("strike", "dstrike"):
                props["s"] = props.get("s") or _on(child)
            elif name == "u":
                props["u"] = (_wa(child, "val") or "single").lower() != "none"
            elif name == "vertAlign":
                props["va"] = {"superscript": "sup", "subscript": "sub"}.get(_wa(child, "val") or "", "")
            elif name == "position" and "va" not in props:
                shift = _int(_wa(child, "val"), 0, -10000, 10000)
                if shift:
                    props["position"] = "sup" if shift > 0 else "sub"
            elif name == "rStyle":
                props["style"] = _wa(child, "val")
        return props

    def _load_styles(self, part: str) -> None:
        try:
            root = self._xml(part)
        except FormatError:
            return
        for style in root:
            name = _wl(style)
            if name == "docDefaults":
                for node in _walk(style):
                    if _wl(node) == "lang" and _wa(node, "val"):
                        self.default_lang = _wa(node, "val") or ""
                        break
                continue
            if name != "style":
                continue
            style_id = _wa(style, "styleId")
            if not style_id:
                continue
            entry = {"name": "", "based": None, "outline": None, "num": None, "ilvl": None, "props": {}}
            for child in style:
                child_name = _wl(child)
                if child_name == "name":
                    entry["name"] = _wa(child, "val") or ""
                elif child_name == "basedOn":
                    entry["based"] = _wa(child, "val")
                elif child_name == "rPr":
                    entry["props"] = self._rpr_props(child)
                elif child_name == "pPr":
                    for prop in child:
                        prop_name = _wl(prop)
                        if prop_name == "outlineLvl":
                            entry["outline"] = _int(_wa(prop, "val"), 9, 0, 9)
                        elif prop_name == "numPr":
                            for item in prop:
                                if _wl(item) == "numId":
                                    entry["num"] = _wa(item, "val")
                                elif _wl(item) == "ilvl":
                                    entry["ilvl"] = _int(_wa(item, "val"), 0, 0, 9)
            kind = _wa(style, "type") or "paragraph"
            {"paragraph": self.pstyles, "character": self.cstyles, "numbering": self.nstyles}.get(kind, {})[style_id] = entry

    def _para_info(self, style_id: str | None) -> tuple:
        """(heading kind, level, numId, ilvl, is_quote) for a paragraph style, following basedOn."""
        if style_id in self.para_cache:
            return self.para_cache[style_id]
        heading: tuple[str, int] | None = None
        num = ilvl = None
        quote = False
        seen: set[str] = set()
        current = style_id
        while current and current not in seen and len(seen) < 16:
            seen.add(current)
            style = self.pstyles.get(current)
            if style is None:
                break
            label = (style["name"] or current).strip().lower()
            if heading is None:
                match = _HEADING_STYLE.fullmatch(label)
                if match:
                    heading = ("heading", int(match.group(1)))
                elif label == "title":
                    heading = ("title", 1)
                elif label == "subtitle":
                    heading = ("subtitle", 2)
                elif style["outline"] is not None:
                    heading = ("heading", style["outline"] + 1) if style["outline"] < 9 else ("body", 0)
            if label in _QUOTE_STYLES:
                quote = True
            if num is None and style["num"] is not None:
                num, ilvl = style["num"], style["ilvl"]
            current = style["based"]
        if heading is not None and heading[0] == "body":
            heading = None
        info = (heading[0] if heading else "", heading[1] if heading else 0, num, ilvl, quote)
        self.para_cache[style_id] = info
        return info

    def _cstyle_props(self, style_id: str) -> dict:
        if style_id in self.cstyle_cache:
            return self.cstyle_cache[style_id]
        chain: list[dict] = []
        seen: set[str] = set()
        current: str | None = style_id
        while current and current not in seen and len(seen) < 16:
            seen.add(current)
            style = self.cstyles.get(current)
            if style is None:
                break
            chain.append(style["props"])
            current = style["based"]
        props: dict = {}
        for item in reversed(chain):
            props.update({key: value for key, value in item.items() if key != "style"})
        self.cstyle_cache[style_id] = props
        return props

    def _load_numbering(self, part: str) -> None:
        try:
            root = self._xml(part)
        except FormatError:
            return
        self.has_numbering = True
        for element in root:
            name = _wl(element)
            if name == "abstractNum":
                levels: dict[int, tuple[str | None, int | None]] = {}
                link = None
                for child in element:
                    child_name = _wl(child)
                    if child_name == "lvl":
                        levels[_int(_wa(child, "ilvl"), 0, 0, 9)] = self._level_format(child)
                    elif child_name == "numStyleLink":
                        link = _wa(child, "val")
                self.abstracts[_wa(element, "abstractNumId") or ""] = {"levels": levels, "link": link}
            elif name == "num":
                abstract = ""
                overrides: dict[int, tuple[str | None, int | None]] = {}
                for child in element:
                    child_name = _wl(child)
                    if child_name == "abstractNumId":
                        abstract = _wa(child, "val") or ""
                    elif child_name == "lvlOverride":
                        fmt, start = None, None
                        for item in child:
                            if _wl(item) == "startOverride":
                                start = _int(_wa(item, "val"), 1, 0, 1 << 20)
                            elif _wl(item) == "lvl":
                                fmt, level_start = self._level_format(item)
                                start = start if start is not None else level_start
                        overrides[_int(_wa(child, "ilvl"), 0, 0, 9)] = (fmt, start)
                self.nums[_wa(element, "numId") or ""] = (abstract, overrides)

    @staticmethod
    def _level_format(level: ET.Element) -> tuple[str | None, int | None]:
        fmt, start, fallback = None, None, None
        for child in level:
            name = _wl(child)
            if name == "numFmt":
                fmt = _wa(child, "val")
            elif name == "start":
                start = _int(_wa(child, "val"), 1, 0, 1 << 20)
            elif _split(child.tag) == (_MC_NS, "AlternateContent"):
                for node in _walk(child):
                    if _wl(node) == "numFmt" and fallback is None:
                        fallback = _wa(node, "val")
        return fmt or fallback, start

    def _list_format(self, num_id: str, ilvl: int) -> tuple[str | None, int | None] | None:
        """(numFmt, start) for a numbering instance level; None when the instance does not exist."""
        for _ in range(4):
            entry = self.nums.get(num_id)
            if entry is None:
                return None
            abstract_id, overrides = entry
            abstract = self.abstracts.get(abstract_id) or {"levels": {}, "link": None}
            if not abstract["levels"] and abstract["link"]:
                linked = self.nstyles.get(abstract["link"])
                if linked and linked["num"] and linked["num"] != num_id:
                    num_id = linked["num"]
                    continue
            fmt, start = abstract["levels"].get(ilvl, (None, None))
            override = overrides.get(ilvl)
            if override:
                fmt = override[0] or fmt
                start = override[1] if override[1] is not None else start
            return fmt, start
        return None

    def _metadata(self, package_rels: dict[str, _Rel]) -> None:
        core = self._rel_target(package_rels, "/core-properties") or "docProps/core.xml"
        if not self._member(core):
            return
        try:
            root = self._xml(core)
        except FormatError:
            return
        values: dict[str, str] = {}
        for element in root:
            namespace, local = _split(element.tag)
            if namespace == _DC_NS and element.text and element.text.strip():
                values.setdefault(local, element.text.strip())
        self.doc.title = values.get("title", "")
        self.doc.authors = [name.strip() for name in values.get("creator", "").split(";") if name.strip()]
        self.doc.language = values.get("language", "")
        self.doc.description = values.get("description") or values.get("subject") or ""

    # ------------------------------------------------------------ main flow

    def run(self) -> Document:
        main, package_rels = self._main_part()
        root = self._xml(main)
        if _wl(root) != "document":
            raise FormatError("not a WordprocessingML document")
        body = _wchild(root, "body")
        if body is None:
            raise FormatError("Word document has no body")
        self.main_rels = self.rels = self._read_rels(main)
        styles = self._rel_target(self.main_rels, "/styles") or posixpath.join(posixpath.dirname(main), "styles.xml")
        if self._member(styles):
            self._load_styles(styles)
        numbering = self._rel_target(self.main_rels, "/numbering")
        if numbering and self._member(numbering):
            self._load_numbering(numbering)
        self._metadata(package_rels)
        self._collect_anchors(root)
        blocks = self._blocks(body, 0)
        if not self.doc.title and self.first_title:
            self.doc.title = self.first_title
        if not self.doc.language and self.default_lang:
            self.doc.language = self.default_lang
        return _assemble(self.doc, blocks, self.notes, self.doc.title or self.path.stem)

    def _collect_anchors(self, root: ET.Element) -> None:
        wanted: set[str] = set()
        for node in root.iter():
            name = _wl(node)
            if name == "bookmarkStart" and _wa(node, "name"):
                self.bookmarks.add(_wa(node, "name") or "")
            elif name == "hyperlink" and _wa(node, "anchor"):
                wanted.add(_wa(node, "anchor") or "")
            elif name in ("instrText", "fldSimple"):
                text = (node.text or "") if name == "instrText" else (_wa(node, "instr") or "")
                wanted.update(re.findall(r'\\l\s+"([^"]+)"', text))
        self.anchor_ok = wanted & self.bookmarks

    # ------------------------------------------------------------ blocks

    def _blocks(self, parent: ET.Element, depth: int) -> list[_Block]:
        blocks: list[_Block] = []
        if depth > MAX_DEPTH:
            return blocks
        for child in parent:
            name = _wl(child)
            if name == "p":
                blocks.extend(self._paragraph(child, depth))
            elif name == "tbl":
                table = self._table(child, depth)
                if table is not None:
                    blocks.append(table)
            elif name == "sdt":
                content = _wchild(child, "sdtContent")
                if content is not None and not self._is_toc_sdt(child):
                    blocks.extend(self._blocks(content, depth + 1))
            elif name in _BLOCK_WRAPPERS:
                blocks.extend(self._blocks(child, depth + 1))
            elif name == "bookmarkStart":
                self._bookmark(child, None)
            elif not name and _split(child.tag) == (_MC_NS, "AlternateContent"):
                choice, fallback = self._alternatives(child)
                produced = self._blocks(choice, depth + 1) if choice is not None else []
                if not produced and fallback is not None:
                    produced = self._blocks(fallback, depth + 1)
                blocks.extend(produced)
        return blocks

    @staticmethod
    def _alternatives(element: ET.Element) -> tuple[ET.Element | None, ET.Element | None]:
        choice = fallback = None
        for child in element:
            local = _local(child.tag)
            if local == "Choice" and choice is None:
                choice = child
            elif local == "Fallback" and fallback is None:
                fallback = child
        return choice, fallback

    @staticmethod
    def _is_toc_sdt(sdt: ET.Element) -> bool:
        properties = _wchild(sdt, "sdtPr")
        if properties is None:
            return False
        for node in _walk(properties, limit=200):
            if _wl(node) == "docPartGallery" and "table of contents" in (_wa(node, "val") or "").lower():
                return True
        return False

    def _bookmark(self, element: ET.Element, out: _Inline | None) -> None:
        name = _wa(element, "name") or ""
        if name not in self.anchor_ok:
            return
        ident = _safe_id("bm-", name)
        if ident in self.emitted_ids:
            return
        self.emitted_ids.add(ident)
        if out is None:
            self.pending_anchors.append(ident)
        else:
            out.anchor(ident)

    def _paragraph(self, paragraph: ET.Element, depth: int) -> list[_Block]:
        style_id = num_id = jc = None
        ilvl: int | None = None
        outline: int | None = None
        properties = _wchild(paragraph, "pPr")
        if properties is not None:
            for prop in properties:
                name = _wl(prop)
                if name == "pStyle":
                    style_id = _wa(prop, "val")
                elif name == "numPr":
                    for item in prop:
                        if _wl(item) == "numId":
                            num_id = _wa(item, "val")
                        elif _wl(item) == "ilvl":
                            ilvl = _int(_wa(item, "val"), 0, 0, 9)
                elif name == "outlineLvl":
                    outline = _int(_wa(prop, "val"), 9, 0, 9)
                elif name == "jc":
                    jc = _wa(prop, "val")
        kind, level, style_num, style_ilvl, quote = self._para_info(style_id)
        if outline is not None:
            kind, level = ("heading", outline + 1) if outline < 9 else ("", 0)
        heading = kind in ("heading", "title", "subtitle")
        saved_pending, self.pending = self.pending, []
        out = _Inline(drop=("b",) if heading else ())
        anchors, self.pending_anchors = self.pending_anchors, []
        for ident in anchors:
            out.anchor(ident)
        self._inline(paragraph, out, depth)
        for field in self.fields:
            field.link_open = False
        self.fields = [field for field in self.fields if field.result]
        markup = out.html()
        blocks: list[_Block] = []
        if out.content:
            text = out.plain_text()
            if heading:
                tag = f"h{min(level, 6) or 1}"
                split_level = 0 if kind == "subtitle" else level
                blocks.append(_Block(f"<{tag}>{markup}</{tag}>", split_level, text))
                if kind == "title" and not self.first_title:
                    self.first_title = text
            else:
                item = self._list_item(num_id if num_id is not None else style_num,
                                       ilvl if ilvl is not None else (style_ilvl or 0))
                align = {"center": "center", "right": "right", "end": "right"}.get(jc or "")
                if item is not None:
                    blocks.append(_Block(markup, item=item))
                elif quote:
                    blocks.append(_Block(f"<blockquote><p>{markup}</p></blockquote>"))
                elif align:
                    blocks.append(_Block(f'<p style="text-align: {align}">{markup}</p>'))
                else:
                    blocks.append(_Block(f"<p>{markup}</p>"))
        else:  # keep bookmark targets of an empty paragraph for the next one
            self.pending_anchors = out.anchors + self.pending_anchors
        blocks.extend(self.pending)
        self.pending = saved_pending
        return blocks

    def _list_item(self, num_id: str | None, ilvl: int) -> tuple | None:
        if not num_id or num_id == "0":
            return None
        fmt, start = None, None
        if self.has_numbering:
            found = self._list_format(num_id, ilvl)
            if found is None:
                return None
            fmt, start = found
        if fmt == "none":
            return None
        ordered = fmt not in (None, "", "bullet")
        return self.counter.item(num_id, ilvl, ordered, _OL_TYPES.get(fmt or "", "1"), start)

    # ------------------------------------------------------------ tables

    def _through(self, parent: ET.Element, wanted: str, depth: int):
        for child in parent:
            name = _wl(child)
            if name == wanted:
                yield child
            elif name in ("sdt", "sdtContent", "customXml", "smartTag") and depth < MAX_DEPTH:
                yield from self._through(child, wanted, depth + 1)

    def _table(self, table: ET.Element, depth: int) -> _Block | None:
        if depth > MAX_DEPTH:
            return None
        rows: list[tuple[bool, list[list]]] = []
        open_merges: dict[int, list] = {}
        for row in self._through(table, "tr", depth):
            header = deleted = False
            column = 0
            properties = _wchild(row, "trPr")
            if properties is not None:
                for prop in properties:
                    name = _wl(prop)
                    if name == "tblHeader":
                        header = _on(prop)
                    elif name == "del":
                        deleted = True
                    elif name == "gridBefore":
                        column = _int(_wa(prop, "val"), 0, 0, 1000)
            if deleted:
                continue
            cells: list[list] = []
            merges: dict[int, list] = {}
            for cell in self._through(row, "tc", depth):
                span, vmerge = 1, None
                cell_props = _wchild(cell, "tcPr")
                if cell_props is not None:
                    for prop in cell_props:
                        name = _wl(prop)
                        if name == "gridSpan":
                            span = _int(_wa(prop, "val"), 1, 1, 1000)
                        elif name == "vMerge":
                            vmerge = (_wa(prop, "val") or "continue").lower()
                # [column, colspan, rowspan, rendered?, inner html]
                entry = [column, span, 1, True, ""]
                if vmerge == "continue" and column in open_merges:
                    open_merges[column][2] += 1
                    merges[column] = open_merges[column]
                    entry[3] = False
                else:
                    self.cell_depth += 1
                    try:
                        entry[4] = _join_blocks(self._blocks(cell, depth + 1))
                    finally:
                        self.cell_depth -= 1
                    if vmerge == "restart":
                        merges[column] = entry
                cells.append(entry)
                column += span
            open_merges = merges
            rows.append((header, cells))
        markup = _table_html([(header, [(span, rowspan, inner) for _column, span, rowspan, visible, inner in cells
                                         if visible]) for header, cells in rows], self.cell_depth > 0)
        return _Block(markup) if markup else None

    # ------------------------------------------------------------ inline content

    def _visible(self) -> bool:
        return all(field.result and not field.drop for field in self.fields)

    def _inline(self, parent: ET.Element, out: _Inline, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for child in parent:
            name = _wl(child)
            if name == "r":
                self._run(child, out, depth)
            elif name == "hyperlink":
                self._hyperlink(child, out, depth)
            elif name == "fldSimple":
                self._fld_simple(child, out, depth)
            elif name in _INLINE_WRAPPERS:
                self._inline(child, out, depth + 1)
            elif name == "sdt":
                content = _wchild(child, "sdtContent")
                if content is not None:
                    self._inline(content, out, depth + 1)
            elif name == "bookmarkStart":
                self._bookmark(child, out)
            elif not name:
                namespace, local = _split(child.tag)
                if (namespace, local) == (_MC_NS, "AlternateContent"):
                    self._alternate(child, out, lambda node: self._inline(node, out, depth + 1))
                elif namespace in _M_NS and local in ("oMath", "oMathPara") and self._visible():
                    text = "".join(node.text or "" for node in _walk(child) if _split(node.tag) in _M_TEXT)
                    if text.strip():
                        out.raw('<span class="math">')
                        out.text(text)
                        out.raw("</span>")

    def _alternate(self, element: ET.Element, out: _Inline, handler) -> None:
        choice, fallback = self._alternatives(element)
        before = (len(out.parts), len(self.pending))
        if choice is not None:
            handler(choice)
        if fallback is not None and (len(out.parts), len(self.pending)) == before:
            handler(fallback)

    def _run(self, run: ET.Element, out: _Inline, depth: int) -> None:
        fmt: tuple[str, ...] = ()
        hidden = False
        properties = _wchild(run, "rPr")
        if properties is not None:
            direct = self._rpr_props(properties)
            props = dict(self._cstyle_props(direct["style"])) if direct.get("style") else {}
            props.update({key: value for key, value in direct.items() if key != "style"})
            if not props.get("va") and props.get("position"):
                props["va"] = props["position"]
            fmt, hidden = _fmt(props), bool(props.get("vanish"))
        for child in run:
            self._run_item(child, out, fmt, hidden, depth)

    def _run_item(self, child: ET.Element, out: _Inline, fmt: tuple[str, ...], hidden: bool, depth: int) -> None:
        name = _wl(child)
        if name == "fldChar":
            self._fld_char(child, out)
            return
        if name == "instrText":
            if self.fields and len(self.fields[-1].instr) < 4096:
                self.fields[-1].instr += child.text or ""
            return
        if hidden or not self._visible():
            return
        if name == "t":
            out.text(child.text, fmt)
        elif name in ("tab", "ptab"):
            out.text(" ", fmt)
        elif name == "br":
            kind = (_wa(child, "type") or "").lower()
            if kind == "column":
                out.text(" ", fmt)
            elif kind != "page":  # a page break is a soft boundary only
                out.br()
        elif name == "cr":
            out.br()
        elif name == "sym":
            out.text(self._symbol(child), fmt)
        elif name == "noBreakHyphen":
            out.text("\u2011", fmt)
        elif name == "softHyphen":
            out.text("\u00ad", fmt)
        elif name == "drawing":
            self._drawing(child, out, depth)
        elif name in ("pict", "object"):
            self._vml(child, out, depth)
        elif name in ("footnoteReference", "endnoteReference"):
            self._note_ref("footnotes" if name == "footnoteReference" else "endnotes", _wa(child, "id"), out, depth)
        elif name == "ruby":
            self._ruby(child, out, depth)
        elif not name and _split(child.tag) == (_MC_NS, "AlternateContent") and depth < MAX_DEPTH:
            self._alternate(child, out, lambda node: [self._run_item(item, out, fmt, hidden, depth + 1)
                                                      for item in node])

    @staticmethod
    def _symbol(element: ET.Element) -> str:
        raw = _wa(element, "char") or ""
        code = int(raw, 16) if re.fullmatch(r"[0-9A-Fa-f]{1,6}", raw) else 0
        font = (_wa(element, "font") or "").lower()
        if 0xF000 <= code <= 0xF0FF:
            code -= 0xF000
            if "symbol" in font:
                if 0x41 <= code <= 0x5A:
                    return _SYMBOL_GREEK[code - 0x41]
                if 0x61 <= code <= 0x7A:
                    return _SYMBOL_GREEK[code - 0x61].lower()
                return {0xB7: "•", 0xB4: "×", 0xB8: "÷", 0xAE: "→", 0xAC: "←", 0xA3: "≤", 0xB3: "≥"}.get(code, "")
            return ""
        if not code or 0xE000 <= code <= 0xF8FF or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            return ""
        return chr(code)

    def _ruby(self, ruby: ET.Element, out: _Inline, depth: int) -> None:
        base = _wchild(ruby, "rubyBase")
        annotation = _wchild(ruby, "rt")
        inner = _Inline()
        if base is not None:
            self._inline(base, inner, depth + 1)
        note = _Inline()
        if annotation is not None:
            self._inline(annotation, note, depth + 1)
        base_html = inner.html()
        if not inner.content:
            return
        out.plain.extend(inner.plain)
        if note.content:
            out.ruby(base_html, note.plain_text())
        else:
            out.raw(base_html, True)

    # ------------------------------------------------------------ fields and links

    def _fld_char(self, element: ET.Element, out: _Inline) -> None:
        kind = (_wa(element, "fldCharType") or "").lower()
        if kind == "begin":
            if len(self.fields) >= _MAX_FIELDS:
                self.field_overflow += 1
            else:
                self.fields.append(_Field())
        elif kind == "separate":
            if self.field_overflow or not self.fields:
                return
            field = self.fields[-1]
            if field.result:
                return
            field.result = True
            words = field.instr.split()
            keyword = words[0].upper() if words else ""
            if keyword in _DROP_FIELDS:
                field.drop = True
            elif keyword == "HYPERLINK" and self._visible():
                href = self._field_link(field.instr)
                if href:
                    field.link_open = out.open_link(href)
        elif kind == "end":
            if self.field_overflow:
                self.field_overflow -= 1
            elif self.fields:
                field = self.fields.pop()
                if field.link_open:
                    out.close_link()

    def _field_link(self, instr: str) -> str:
        tokens = [match.group(1) if match.group(1) is not None else match.group(2)
                  for match in _FIELD_TOKEN.finditer(instr)][1:40]
        url = anchor = ""
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lowered = token.lower()
            if lowered == "\\l" and index + 1 < len(tokens):
                anchor = tokens[index + 1]
                index += 2
                continue
            if lowered in ("\\o", "\\t"):
                index += 2
                continue
            if not token.startswith("\\") and not url:
                url = token
            index += 1
        href = _http_link(url)
        if href:
            return href
        if anchor and anchor in self.anchor_ok:
            return "#" + _safe_id("bm-", anchor)
        return ""

    def _fld_simple(self, element: ET.Element, out: _Inline, depth: int) -> None:
        instr = _wa(element, "instr") or ""
        words = instr.split()
        keyword = words[0].upper() if words else ""
        if keyword in _DROP_FIELDS:
            return
        opened = False
        if keyword == "HYPERLINK" and self._visible():
            opened = out.open_link(self._field_link(instr))
        self._inline(element, out, depth + 1)
        if opened:
            out.close_link()

    def _hyperlink(self, element: ET.Element, out: _Inline, depth: int) -> None:
        href = ""
        rid = _ra(element, "id")
        if rid:
            rel = self.rels.get(rid)
            if rel is not None and rel.external:
                href = _http_link(rel.target)
        anchor = _wa(element, "anchor")
        if not href and anchor and anchor in self.anchor_ok:
            href = "#" + _safe_id("bm-", anchor)
        opened = out.open_link(href) if href and self._visible() else False
        self._inline(element, out, depth + 1)
        if opened:
            out.close_link()

    # ------------------------------------------------------------ images, text boxes, notes

    def _rel_image(self, rid: str | None) -> str:
        rel = self.rels.get(rid or "")
        if rel is None or rel.external or not rel.target:
            return ""
        member = self._member(rel.target)
        if member is None:
            return ""
        if member in self.images:
            return self.images[member]
        try:
            data = _read_member(self.zip, member, MAX_IMAGE_BYTES)
        except FormatError:
            self.images[member] = ""
            return ""
        stem = posixpath.splitext(posixpath.basename(member))[0]
        return _add_image(self.doc, self.images, member, data, stem)

    def _drawing(self, drawing: ET.Element, out: _Inline, depth: int) -> None:
        alt = ""
        for node in _walk(drawing, frozenset({"txbxContent", "Fallback"})):
            namespace, local = _split(node.tag)
            if local == "docPr" and not alt:
                alt = node.get("descr") or node.get("title") or ""
            elif local == "blip" and namespace in _A_NS:
                name = self._rel_image(_ra(node, "embed"))
                if name:
                    out.image(name, alt)
            elif local == "txbxContent" and namespace in _W_SET:
                self.pending.extend(self._blocks(node, depth + 1))

    def _vml(self, element: ET.Element, out: _Inline, depth: int) -> None:
        for node in _walk(element, frozenset({"txbxContent"})):
            namespace, local = _split(node.tag)
            if local == "imagedata" and namespace == _VML_NS:
                name = self._rel_image(_ra(node, "id") or node.get(f"{{{_O_NS}}}relid"))
                if name:
                    out.image(name, node.get(f"{{{_O_NS}}}title") or "")
            elif local == "txbxContent" and namespace in _W_SET:
                self.pending.extend(self._blocks(node, depth + 1))

    def _note_part(self, kind: str) -> tuple[dict[str, _Rel], dict[str, ET.Element]]:
        if kind in self.note_parts:
            return self.note_parts[kind]
        rels: dict[str, _Rel] = {}
        notes: dict[str, ET.Element] = {}
        part = self._rel_target(self.main_rels, "/" + kind)
        if part and self._member(part):
            try:
                root = self._xml(part)
                rels = self._read_rels(self._member(part) or part)
            except FormatError:
                root = None
            if root is not None:
                for note in root:
                    if _wl(note) in ("footnote", "endnote") and (_wa(note, "type") or "normal") == "normal":
                        notes[_wa(note, "id") or ""] = note
        self.note_parts[kind] = (rels, notes)
        return rels, notes

    def _note_ref(self, kind: str, note_id: str | None, out: _Inline, depth: int) -> None:
        if self.in_note or note_id is None:
            return
        rels, notes = self._note_part(kind)
        element = notes.get(note_id)
        if element is None:
            return
        saved = (self.rels, self.fields, self.field_overflow, self.pending, self.pending_anchors)
        self.rels, self.fields, self.field_overflow, self.pending, self.pending_anchors = rels, [], 0, [], []
        self.in_note = True
        try:
            body = _join_blocks(self._blocks(element, depth + 1))
        finally:
            self.rels, self.fields, self.field_overflow, self.pending, self.pending_anchors = saved
            self.in_note = False
        if body:
            out.note(self.notes.add(body))


def _ole_error(path: Path) -> FormatError:
    with path.open("rb") as stream:
        head = stream.read(4 * 1024 * 1024)
    if "EncryptedPackage".encode("utf-16-le") in head or "EncryptionInfo".encode("utf-16-le") in head:
        return FormatError("document is password-protected (encrypted Office file)")
    return FormatError("not an OOXML package (legacy binary Office file)")


def convert(path: Path) -> Document:
    """Convert a .docx / .docm / .dotx / .dotm file into a :class:`Document`."""
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(8)
    if head.startswith(_OLE_MAGIC):
        raise _ole_error(path)
    if not head.startswith(b"PK"):
        raise FormatError("not a Word document (not a ZIP package)")
    archive = _open_zip(path)
    try:
        return _DocxConverter(archive, path).run()
    finally:
        archive.close()
