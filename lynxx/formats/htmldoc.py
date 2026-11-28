"""HTML / XHTML files (.html, .htm, .xhtml) -> :class:`Document`.

The page is decoded (BOM, ``<?xml encoding?>``, ``<meta charset>`` or the
``http-equiv`` content type, then the shared fallbacks), its ``<body>`` is run
through the allowlist sanitizer and split into chapters at ``<h1>`` (or ``<h2>``
when there is at most one ``<h1>``) when there are at least two such headings.

This module also holds helpers shared by the other text converters of this batch
(Markdown, RTF, FB2): the local-image resolver, the heading splitter and the
XML character cleaner.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from pathlib import Path
from urllib.parse import unquote

from .base import (
    IMAGE_TYPES,
    MAX_CHAPTERS,
    MAX_INPUT_BYTES,
    Chapter,
    Document,
    FormatError,
    decode_text,
    extract_body,
    html_title,
    read_limited,
    sanitize_fragment,
    sniff_image_type,
    strip_tags,
)

MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024
"""Largest local or ``data:`` image accepted from an HTML-ish document."""
MAX_LOCAL_IMAGES_TOTAL = MAX_INPUT_BYTES
"""Budget for all images one document may pull in from its folder."""

# Characters XML 1.0 forbids (C0 controls except tab/newline/CR, lone surrogates, U+FFFE/U+FFFF).
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def clean_xml_text(text: str) -> str:
    """Drop characters that would make the generated XHTML ill-formed."""
    return _INVALID_XML_CHARS.sub("", text)


# ---------------------------------------------------------------- local images

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class LocalImageResolver:
    """``resolve_src`` for :func:`sanitize_fragment`: ``data:`` images and safe relative files.

    A relative reference is accepted only when it resolves (symlinks included)
    inside ``base_dir``, has an image extension, is at most 20 MB and its bytes
    sniff as a raster image. Remote (http, protocol-relative) and other schemes
    are dropped: converters never touch the network. SVG is dropped too.
    """

    def __init__(self, document: Document, base_dir: Path | None) -> None:
        self.document = document
        self.base_dir = base_dir.resolve() if base_dir is not None else None
        self.cache: dict[str, str] = {}
        self.total = 0

    def __call__(self, src: str) -> str:
        src = (src or "").strip()
        if not src:
            return ""
        cached = self.cache.get(src)
        if cached is not None:
            return cached
        if src[:5].lower() == "data:":
            name = self._data_uri(src)
        elif _SCHEME.match(src) or src.startswith("//") or src.startswith("\\\\"):
            name = ""
        else:
            name = self._local(src)
        if len(src) <= 4096:
            self.cache[src] = name
        return name

    def _data_uri(self, src: str) -> str:
        header, sep, payload = src.partition(",")
        if not sep or ";base64" not in header.lower():
            return ""
        if len(payload) > MAX_LOCAL_IMAGE_BYTES * 4 // 3 + 4096:
            return ""
        if "%" in payload:
            payload = unquote(payload)
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            try:
                data = base64.b64decode(payload + "==", validate=False)
            except (binascii.Error, ValueError):
                return ""
        return self._register(data, "image")

    def _local(self, src: str) -> str:
        if self.base_dir is None:
            return ""
        ref = unquote(src.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/").strip()
        if not ref or ref.startswith("/") or re.match(r"^[A-Za-z]:", ref) or "\x00" in ref:
            return ""
        try:
            candidate = (self.base_dir / ref).resolve()
        except (OSError, RuntimeError, ValueError):
            return ""
        if not candidate.is_relative_to(self.base_dir):
            return ""
        suffix = candidate.suffix.lower()
        if suffix not in IMAGE_TYPES or suffix == ".svg":
            return ""
        key = "file:" + str(candidate)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        name = ""
        try:
            if candidate.is_file():
                size = candidate.stat().st_size
                if 0 < size <= MAX_LOCAL_IMAGE_BYTES and self.total + size <= MAX_LOCAL_IMAGES_TOTAL:
                    with candidate.open("rb") as stream:
                        data = stream.read(MAX_LOCAL_IMAGE_BYTES + 1)
                    name = self._register(data, candidate.stem)
        except OSError:
            name = ""
        self.cache[key] = name
        return name

    def _register(self, data: bytes, stem: str) -> str:
        if not data or len(data) > MAX_LOCAL_IMAGE_BYTES or self.total + len(data) > MAX_LOCAL_IMAGES_TOTAL:
            return ""
        media_type = sniff_image_type(data)
        if not media_type or media_type == "image/svg+xml":
            return ""
        self.total += len(data)
        return self.document.add_resource(data, media_type, stem)


# ---------------------------------------------------------------- chapter splitting

_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)([^>]*)>|[^<]+|<")
_ID_ATTR = re.compile(r'\s(?:id|name)="[^"]*"')
_HAS_IMAGE = re.compile(r"<img\b")


def heading_split_level(fragment: str) -> int:
    """1 to split at <h1>, 2 at <h2>, 0 for a single chapter (the shared rule of this batch)."""
    h1 = len(re.findall(r"<h1[\s>/]", fragment))
    if h1 >= 2:
        return 1
    if len(re.findall(r"<h2[\s>/]", fragment)) >= 2:
        return 2
    return 0


def _has_content(markup: str) -> bool:
    return bool(_HAS_IMAGE.search(markup)) or bool(strip_tags(markup).replace("\u00a0", " ").strip())


def split_at_headings(fragment: str, default_title: str = "", level: int | None = None) -> list[Chapter]:
    """Split a *sanitized* fragment into chapters at ``<hN>`` (at any depth).

    Open ancestors are closed at each cut and reopened (without ``id``) in the
    next chapter so every chapter stays well-formed. Content before the first
    heading becomes its own chapter titled ``default_title``. Chapters without
    text or images are dropped.
    """
    level = heading_split_level(fragment) if level is None else level
    if not level:
        return [Chapter(default_title or "Phần 1", fragment)] if _has_content(fragment) else []
    target = f"h{level}"
    pieces: list[tuple[str | None, str]] = []
    stack: list[tuple[str, str]] = []
    current: list[str] = []
    title: str | None = None
    capturing = False
    capture_depth = 0
    heading_text: list[str] = []
    for match in _TOKEN.finditer(fragment):
        token = match.group(0)
        name = match.group(2)
        if name is None:
            current.append(token)
            if capturing:
                heading_text.append(token)
            continue
        name = name.lower()
        closing = match.group(1) == "/"
        if not closing and name == target and not capturing:
            current.append("".join(f"</{open_name}>" for open_name, _ in reversed(stack)))
            piece = "".join(current)
            if _has_content(piece):
                pieces.append((title, piece))
                current = [_ID_ATTR.sub("", start) for _, start in stack]
            else:  # nothing to keep before this heading: reopen the ancestors with their ids
                current = [start for _, start in stack]
            title, capturing, capture_depth, heading_text = "", True, len(stack), []
            current.append(token)
            stack.append((name, token))
            continue
        current.append(token)
        if capturing and name == "br":
            heading_text.append(" ")
        if closing:
            if any(open_name == name for open_name, _ in stack):
                while stack:
                    open_name, _ = stack.pop()
                    if open_name == name:
                        break
            if capturing and len(stack) <= capture_depth:
                capturing = False
                title = re.sub(r"\s+", " ", html.unescape("".join(heading_text))).strip()
        elif not match.group(3).endswith("/"):
            stack.append((name, token))
    current.append("".join(f"</{open_name}>" for open_name, _ in reversed(stack)))
    pieces.append((title, "".join(current)))
    chapters: list[Chapter] = []
    for piece_title, markup in pieces:
        if not _has_content(markup):
            continue
        if piece_title is None:
            label = default_title or f"Phần {len(chapters) + 1}"
        else:
            label = piece_title or f"Phần {len(chapters) + 1}"
        chapters.append(Chapter(label[:300], markup))
    if len(chapters) > MAX_CHAPTERS:
        return [Chapter(default_title or "Phần 1", fragment)]
    return chapters


# ---------------------------------------------------------------- decoding

_CHARSET_ALIASES = {
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252", "l1": "cp1252",
    "us-ascii": "cp1252", "ascii": "cp1252", "windows-1252": "cp1252",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "gb_2312-80": "gb18030", "cp936": "gb18030",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949", "tis-620": "cp874", "iso-8859-11": "cp874",
    "big5": "big5hkscs", "utf-16": "utf-8", "utf-16le": "utf-8", "utf-16be": "utf-8", "unicode": "utf-8",
}
_XML_DECL_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._:-]{1,40})[\"']")
_META_TAG = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_META_CHARSET = re.compile(rb"\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]{1,40})", re.IGNORECASE)


def detect_charset(data: bytes) -> str:
    """The charset declared by the XML declaration or a <meta> tag ('' when none)."""
    head = data[:8192]
    match = _XML_DECL_ENCODING.match(head)
    label = match.group(1) if match else b""
    if not label:
        for tag in _META_TAG.finditer(head):
            found = _META_CHARSET.search(tag.group(0))
            if found:
                label = found.group(1)
                break
    if not label:
        return ""
    name = label.decode("ascii", "ignore").strip().lower()
    return _CHARSET_ALIASES.get(name, name)


def decode_html(data: bytes) -> str:
    declared = detect_charset(data)
    if declared in {"cp1252", "latin-1"} and any(byte >= 0x80 for byte in data[:65536]):
        try:  # a page saved as UTF-8 that still claims Latin-1: valid multi-byte UTF-8 wins
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return decode_text(data, declared)


# ---------------------------------------------------------------- metadata

_ATTR = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")


def _attributes(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(m.group(2) or m.group(3) or m.group(4) or "") for m in _ATTR.finditer(tag)}


def _meta(markup: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    head = markup[:262144]
    body_at = head.lower().find("<body")
    if body_at >= 0:
        head = head[:body_at]
    for tag in re.finditer(r"<meta\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", head, re.IGNORECASE):
        attrs = _attributes(tag.group(0))
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        value = attrs.get("content", "").strip()
        if key and value:
            found.setdefault(key, []).append(value)
    return found


def _language(markup: str) -> str:
    match = re.search(r"<html\b[^>]*>", markup[:65536], re.IGNORECASE)
    if not match:
        return ""
    attrs = _attributes(match.group(0))
    value = attrs.get("lang") or attrs.get("xml:lang") or ""
    return value.strip()[:35] if re.fullmatch(r"[A-Za-z]{1,8}(?:[-_][A-Za-z0-9]{1,8})*", value.strip()) else ""


def _first_heading(fragment: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", fragment, re.DOTALL)
    return strip_tags(match.group(1))[:300] if match else ""


# ---------------------------------------------------------------- converter

def convert(path: Path) -> Document:
    path = Path(path)
    data = read_limited(path)
    markup = clean_xml_text(decode_html(data))
    document = Document(source_format=(path.suffix.lstrip(".").lower() or "html"))
    meta = _meta(markup)
    document.language = _language(markup)
    document.authors = [value[:200] for value in meta.get("author", [])[:10]]
    description = (meta.get("description") or meta.get("og:description") or [""])[0]
    document.description = strip_tags(description)[:5000]
    fragment = sanitize_fragment(extract_body(markup), resolve_src=LocalImageResolver(document, path.parent))
    document.title = html_title(markup)[:300] or _first_heading(fragment)
    chapters = split_at_headings(fragment, document.title or path.stem)
    if not chapters:
        raise FormatError("HTML document has no readable content")
    document.chapters = chapters
    return document
