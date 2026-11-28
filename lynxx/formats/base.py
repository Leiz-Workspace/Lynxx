"""Shared model and helpers for Lynxx document converters.

Every converter turns one local file into a :class:`Document`: an ordered list of
chapters whose bodies are *sanitized XHTML fragments* plus the binary resources
(images) they reference by bare file name. The library then writes that document
as a normalized EPUB for the reader and for exports, so converters never write
files themselves and never touch the network.

Untrusted input rules (all converters):
- read at most ``MAX_INPUT_BYTES``; never follow paths found inside the file;
- archives go through :func:`open_zip` (traversal, symlink and zip-bomb checks);
- every HTML-ish body goes through :func:`sanitize_fragment` (allowlist of tags
  and attributes, no scripts, no event handlers, no remote resources);
- raise :class:`FormatError` with a short English message for corrupt,
  encrypted (DRM) or unsupported files.
"""

from __future__ import annotations

import codecs
import html
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20000
MAX_RESOURCES = 20000
MAX_CHAPTERS = 20000

IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml", ".avif": "image/avif",
}


class FormatError(ValueError):
    """The file is corrupt, encrypted or uses an unsupported variant."""


@dataclass(slots=True)
class Resource:
    """A binary resource referenced from chapter XHTML by ``name`` (no folders)."""

    name: str
    data: bytes
    media_type: str


@dataclass(slots=True)
class Chapter:
    title: str
    html: str
    """Sanitized XHTML body fragment (no <html>/<head>/<body>)."""
    remote_images: list[str] = field(default_factory=list)
    """Pages of an image chapter that the file only links to (http(s) URLs, not embedded): the library keeps the chapter
    as "not downloaded" with these addresses so the images can be fetched later (exports of vBook keep such links)."""


@dataclass(slots=True)
class Document:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    language: str = ""
    description: str = ""
    """Plain text."""
    cover: Resource | None = None
    chapters: list[Chapter] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    kind: str = "text"
    """"text" (reflowable) or "images" (every chapter is a sequence of page images)."""
    source_format: str = ""

    def add_resource(self, data: bytes, media_type: str, stem: str = "res") -> str:
        """Register a resource under a unique safe name and return that name."""
        if len(self.resources) >= MAX_RESOURCES:
            raise FormatError("too many embedded resources")
        ext = next((e for e, t in IMAGE_TYPES.items() if t == media_type), "") or ".bin"
        if ext == ".jpeg":
            ext = ".jpg"
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:40] or "res"
        name = f"{safe_stem}_{len(self.resources) + 1:05d}{ext}"
        self.resources.append(Resource(name, data, media_type))
        return name

    def validate(self) -> "Document":
        if not self.chapters:
            raise FormatError("document has no readable content")
        if len(self.chapters) > MAX_CHAPTERS:
            raise FormatError("document has too many chapters")
        names = {r.name for r in self.resources}
        if len(names) != len(self.resources):
            raise FormatError("duplicate resource names")
        for name in names:
            if "/" in name or "\\" in name or name.startswith("."):
                raise FormatError(f"unsafe resource name: {name}")
        self.title = (self.title or "").strip()
        return self


# ---------------------------------------------------------------- bytes / text

def read_limited(path: str | Path, limit: int = MAX_INPUT_BYTES) -> bytes:
    candidate = Path(path)
    size = candidate.stat().st_size
    if size > limit:
        raise FormatError("file is too large")
    return candidate.read_bytes()


_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_FALLBACK_ENCODINGS = ("gb18030", "big5", "shift_jis", "euc-kr", "cp1258", "cp1252", "latin-1")


def decode_text(data: bytes, declared: str = "") -> str:
    """Decode text bytes: BOM, declared charset, UTF-8, chardet (if installed), common fallbacks."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, errors="replace")
    if len(data) >= 4 and data[1:4:2] == b"\x00\x00" and data[0] and data[2]:
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    for encoding in ([declared] if declared else []) + ["utf-8"]:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    candidates: list[str] = []
    try:  # optional dependency: a confident guess for legacy Chinese / Vietnamese text files wins
        import chardet  # type: ignore

        guess = chardet.detect(data[:200000])
        if guess.get("encoding") and (guess.get("confidence") or 0) >= 0.9:
            candidates.append(guess["encoding"])
    except Exception:  # noqa: BLE001 - chardet is optional
        pass
    sample = data[:200000]
    best, best_score = "", -1.0
    for encoding in candidates + list(_FALLBACK_ENCODINGS):
        try:
            decoded = sample.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        score = _plausibility(decoded) + (0.05 if encoding in candidates else 0)
        if score > best_score:
            best, best_score = encoding, score
    if best:
        return data.decode(best, errors="replace")
    return data.decode("utf-8", errors="replace")


def _plausibility(text: str) -> float:
    """Share of characters typical of book text (letters, CJK, Hangul, Vietnamese, punctuation)."""
    if not text:
        return 0.0
    good = 0
    for char in text:
        code = ord(char)
        if (char.isascii() and (char.isprintable() or char in "\r\n\t")) or 0x4E00 <= code <= 0x9FFF \
                or 0x3000 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF \
                or 0x00C0 <= code <= 0x01B0 or 0x1EA0 <= code <= 0x1EF9 or 0x2010 <= code <= 0x2027:
            good += 1
    return good / len(text)


def sniff_image_type(data: bytes) -> str:
    """Media type from magic bytes, '' when the bytes are not a known image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return ""


_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def clean_xml_text(text: str) -> str:
    """Drop characters XML 1.0 does not allow (control characters, U+FFFE/U+FFFF, lone surrogates)."""
    return _XML_ILLEGAL.sub("", text)


def escape(text: str) -> str:
    """Escape text for XHTML content and attribute values."""
    return html.escape(clean_xml_text(text), quote=True)


def text_to_paragraphs(text: str) -> str:
    """Plain text to <p> paragraphs (one per non-empty line)."""
    lines = (line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "".join(f"<p>{escape(line)}</p>" for line in lines if line)


# Headings typical of web novels / light novels (Vietnamese, English, Chinese, Japanese, Korean).
CHAPTER_HEADING = re.compile(
    r"^\s*(?:"
    # a keyword followed by a number (arabic, roman or Chinese numerals): "Chương 12", "Chapter IV: …", "Quyển 2 - …"
    r"(?:chương|chuong|chapter|chap|hồi|hoi|quyển|quyen|tập|tap|phần|phan|volume|vol\.?|book|part|episode|ep\.?)"
    r"(?![^\W\d_])\s*(?:\d+(?:[.,]\d+)?|[IVXLCDM]+|[ivxlcdm]+|[零一二三四五六七八九十百千]+)(?![^\W\d_])(?:\s*[.:：\-–—]\s*.*|\s+.*)?"
    # stand-alone section names
    r"|(?:prologue|epilogue|lời mở đầu|lời nói đầu|ngoại truyện|phiên ngoại|vĩ thanh)(?![^\W\d_])(?:\s*[.:：\-–—]?\s*.*)?"
    r"|第\s*[\d零一二三四五六七八九十百千万〇两]+\s*[章节回卷集部篇话].*"
    r"|제\s*\d+\s*[장화권].*"
    r")$",
    re.IGNORECASE,
)


def split_text_chapters(text: str, default_title: str = "", max_heading_len: int = 80) -> list[Chapter]:
    """Split plain text into chapters at heading-like lines; one chapter if none are found."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chapters: list[tuple[str, list[str]]] = []
    current_title = ""
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and len(stripped) <= max_heading_len and CHAPTER_HEADING.match(stripped):
            if current_title or any(item.strip() for item in current):
                chapters.append((current_title, current))
            current_title, current = stripped, []
        else:
            current.append(line)
    if current_title or any(item.strip() for item in current):
        chapters.append((current_title, current))
    if not chapters:
        return []
    result: list[Chapter] = []
    for index, (title, body) in enumerate(chapters, 1):
        name = title or (default_title if len(chapters) == 1 else f"Phần {index}")
        result.append(Chapter(name or f"Phần {index}", text_to_paragraphs("\n".join(body))))
    return result


# ---------------------------------------------------------------- archives

def open_zip(path: str | Path) -> zipfile.ZipFile:
    """Open a ZIP after checking member names, symlinks, member count and expanded size."""
    candidate = Path(path)
    if candidate.stat().st_size > MAX_INPUT_BYTES:
        raise FormatError("file is too large")
    try:
        archive = zipfile.ZipFile(candidate)
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError, EOFError) as err:
        raise FormatError("corrupt ZIP container") from err
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        archive.close()
        raise FormatError("archive has too many members")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or (len(name) > 1 and name[1] == ":"):
            archive.close()
            raise FormatError(f"unsafe archive member: {info.filename}")
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            archive.close()
            raise FormatError(f"symlink in archive: {info.filename}")
        if info.flag_bits & 0x1:
            archive.close()
            raise FormatError("encrypted archive members are not supported")
        total += info.file_size
        if total > MAX_EXPANDED_BYTES:
            archive.close()
            raise FormatError("archive expands beyond the size limit")
    return archive


def read_member(archive: zipfile.ZipFile, name: str, limit: int = 256 * 1024 * 1024) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise FormatError(f"archive member is too large: {name}")
    try:
        with archive.open(info) as stream:
            data = stream.read(limit + 1)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError, OSError) as err:
        raise FormatError(f"damaged archive member {name}: {err}") from err
    if len(data) > limit:
        raise FormatError(f"archive member is too large: {name}")
    return data


def resolve_href(base: str, href: str) -> str:
    """Resolve an archive-relative reference (no scheme) against the referencing member's path."""
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href or re.match(r"^[a-z][a-z0-9+.-]*:", href, re.IGNORECASE):
        return ""
    from urllib.parse import unquote

    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))
    return "" if joined.startswith("../") or joined == ".." else joined.lstrip("/")


# ---------------------------------------------------------------- HTML sanitizer

ALLOWED_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "big", "blockquote", "br", "caption", "center",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
    "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main",
    "mark", "nav", "ol", "p", "pre", "q", "rp", "rt", "ruby", "s", "samp", "section", "small", "span", "strike",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul",
    "var", "wbr",
}
VOID_TAGS = {"br", "col", "hr", "img", "wbr"}
DROP_WITH_CONTENT = {"script", "style", "noscript", "template", "iframe", "object", "embed", "applet", "frame",
                     "frameset", "svg", "math", "canvas", "audio", "video", "head", "title", "select", "textarea",
                     "button", "form", "input"}
GLOBAL_ATTRS = {"id", "class", "title", "lang", "dir", "align"}
TAG_ATTRS = {
    "a": {"href", "name"}, "img": {"src", "alt", "width", "height"}, "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"}, "col": {"span"}, "colgroup": {"span"}, "ol": {"start", "type", "reversed"},
    "li": {"value"}, "time": {"datetime"}, "font": {"color", "size"}, "q": {"cite"}, "blockquote": {"cite"},
    "bdo": {"dir"},
}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]")
_STYLE_ALLOWED = re.compile(
    r"^\s*(text-align|font-weight|font-style|text-decoration|text-indent|margin-left|margin-right|font-size|"
    r"vertical-align|color|background-color)\s*:\s*[#%()\w\s.,-]{1,40}\s*$",
    re.IGNORECASE,
)


def _clean_style(value: str) -> str:
    parts = [part for part in value.split(";") if part.strip() and _STYLE_ALLOWED.match(part)]
    return "; ".join(part.strip() for part in parts if "url" not in part.lower() and "expression" not in part.lower())


_IMPLICIT_SCOPE = {
    "li": {"ul", "ol", "table", "blockquote", "div", "section", "article"},
    "dt": {"dl", "table", "div"}, "dd": {"dl", "table", "div"},
    "tr": {"table", "tbody", "thead", "tfoot"},
    "td": {"tr", "table"}, "th": {"tr", "table"},
    "p": {"div", "li", "td", "th", "blockquote", "section", "article", "table", "ul", "ol", "dd", "dt", "figure", "details", "aside", "header", "footer", "main", "nav"},
}


_CLOSES_P = {"address", "article", "aside", "blockquote", "center", "details", "div", "dl", "figcaption", "figure", "footer", "h1", "h2",
             "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "pre", "section", "summary", "table", "ul"}


class _Sanitizer(HTMLParser):
    def __init__(self, resolve_src, resolve_link) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip_depth = 0
        self.skip_tag = ""
        self.resolve_src = resolve_src
        self.resolve_link = resolve_link

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        kept: list[str] = []
        if tag == "img" and not any((name or "").lower() == "src" and value for name, value in attrs):
            return None  # an image without a source is dropped
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = raw_value or ""
            if name == "style":
                cleaned = _clean_style(value)
                if cleaned:
                    kept.append(f'style="{escape(cleaned)}"')
                continue
            if name not in allowed:
                continue
            if name in {"id", "name"}:
                value = _SAFE_ID.sub("_", value)[:120]
                if not value:
                    continue
            if tag == "img" and name == "src":
                value = self.resolve_src(value) if self.resolve_src else ""
                if not value:
                    return None
            if tag == "a" and name == "href":
                value = self.resolve_link(value) if self.resolve_link else _default_link(value)
                if not value:
                    continue
            kept.append(f'{name}="{escape(value)}"')
        return " ".join(kept)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag and tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            if tag not in VOID_TAGS and tag != "input":
                self.skip_tag, self.skip_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS:
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:  # an <img> whose source cannot be resolved locally
            return
        attr_text = f" {rendered}" if rendered else ""
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{attr_text}/>")
            return
        if tag in _CLOSES_P and "p" in self.stack:
            # a block inside a paragraph ends the paragraph (as HTML parsers do)
            index = len(self.stack) - 1 - self.stack[::-1].index("p")
            if not any(name in _IMPLICIT_SCOPE["p"] for name in self.stack[index + 1:]):
                while len(self.stack) > index:
                    self.out.append(f"</{self.stack.pop()}>")
        boundary = _IMPLICIT_SCOPE.get(tag)
        if boundary is not None:
            # implicit end of an open paragraph / list item / cell, but never across a nested list or table
            for index in range(len(self.stack) - 1, -1, -1):
                name = self.stack[index]
                if name == tag:
                    while len(self.stack) > index:
                        self.out.append(f"</{self.stack.pop()}>")
                    break
                if name in boundary:
                    break
        self.stack.append(tag)
        self.out.append(f"<{tag}{attr_text}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            closing = self.stack.pop()
            self.out.append(f"</{closing}>")
            if closing == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.out.append(escape(data).replace("&#x27;", "'"))

    def close_all(self) -> str:
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def _default_link(value: str) -> str:
    value = value.strip()
    if value.startswith("#"):
        return "#" + _SAFE_ID.sub("_", value[1:])[:120]
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    return ""


def sanitize_fragment(markup: str, resolve_src=None, resolve_link=None) -> str:
    """Return a well-formed, allowlisted XHTML fragment.

    ``resolve_src(src) -> str`` maps an <img src> to a resource name ('' drops the image);
    without it every image is dropped. ``resolve_link(href) -> str`` maps <a href>
    ('' drops the attribute); the default keeps in-document anchors and http(s) links.
    """
    parser = _Sanitizer(resolve_src, resolve_link)
    markup = clean_xml_text(markup)
    lowered = markup.lower()
    for raw in ("script", "style", "title", "textarea", "xmp", "noembed", "noframes", "iframe"):
        # html.parser reads everything after an unclosed raw-text tag as its content: drop such opening tags
        if lowered.count(f"<{raw}") > lowered.count(f"</{raw}"):
            markup = re.sub(rf"<{raw}\b[^>]*>", "", markup, flags=re.IGNORECASE)
            lowered = markup.lower()
    try:
        parser.feed(markup)
    except Exception as err:  # noqa: BLE001 - html.parser is lenient; anything else is a corrupt input
        raise FormatError(f"unreadable HTML: {err}") from err
    return parser.close_all()


def extract_body(markup: str) -> str:
    """The inner markup of <body> (or the whole input when it has none)."""
    match = re.search(r"<body\b[^>]*>(.*)</body\s*>", markup, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r"<body\b[^>]*>(.*)$", markup, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else markup


def html_title(markup: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", markup, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()


def strip_tags(markup: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))).strip()


def images_document(pages: Iterable[tuple[str, bytes]], title: str, source_format: str) -> Document:
    """A comic-style document: one chapter with every page image in order."""
    document = Document(title=title, kind="images", source_format=source_format)
    body: list[str] = []
    for index, (label, data) in enumerate(pages, 1):
        media_type = sniff_image_type(data)
        if not media_type or media_type == "image/svg+xml":
            continue
        name = document.add_resource(data, media_type, "page")
        body.append(f'<div class="page"><img src="{name}" alt="{escape(label or f"Trang {index}")}"/></div>')
    if not body:
        raise FormatError("archive has no supported images")
    document.chapters.append(Chapter(title or "Trang", "".join(body)))
    document.cover = document.resources[0]
    return document
