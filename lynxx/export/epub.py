"""EPUB 2 / EPUB 3 / KEPUB writer for library collections and converted documents.

Layout (the same as vBook's exports, so other readers and converters treat Lynxx books alike)::

    mimetype                      (first, stored)
    META-INF/container.xml
    OEBPS/content.opf  OEBPS/toc.ncx  [OEBPS/nav.xhtml for EPUB 3]
    OEBPS/Styles/stylesheet.css
    OEBPS/Images/cover.<ext>  OEBPS/Images/img_00001.<ext> …
    OEBPS/Text/cover.xhtml  introduction.xhtml  toc.xhtml  chapter_1.xhtml …

The table of contents page links every chapter; the NCX (and the EPUB 3 nav) list cover, introduction, the TOC page
and all chapters. KEPUB adds Kobo's koboSpan markers (reading position / statistics on Kobo devices).
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..formats.base import sniff_image_type
from .book import ExportBook

EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp", "image/avif": "avif"}
XHTML_NS = "http://www.w3.org/1999/xhtml"
CSS = """h1 {
  text-align: center;
}

p {
  text-indent: 20pt;
  line-height: 140%;
  font-size: 100%;
  text-align: justify;
}

a {
  color: #666633;
}

img {
  width: 100%;
}

div.page {
  margin: 0;
  padding: 0;
  text-align: center;
  page-break-inside: avoid;
}

div.page img {
  display: block;
  max-width: 100%;
  margin: 0 auto;
}

p.meta, p.toc {
  text-indent: 0;
}
"""

esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731


class _Writer:
    def __init__(self, book: ExportBook, version: int, kepub: bool, include_intro: bool, include_toc: bool, show_titles: bool) -> None:
        self.book = book
        self.version = 3 if version == 3 or kepub else 2
        self.kepub = kepub
        self.include_intro = include_intro
        self.include_toc = include_toc
        self.show_titles = show_titles
        self.images: dict[str, str] = {}  # local path -> OEBPS/Images/<name>
        self.manifest: list[tuple[str, str, str, str]] = []  # id, href, media type, properties
        self.spine: list[str] = []
        self.nav: list[tuple[str, str, str]] = []  # id, label, href
        self.files: list[tuple[str, bytes | Path]] = []
        self.identifier = hashlib.md5(f"lynxx:{book.id}".encode()).hexdigest()

    # ------------------------------------------------------------ helpers

    def image(self, path: Path, name: str = "") -> str:
        key = str(path)
        if key in self.images:
            return self.images[key]
        with path.open("rb") as stream:
            media = sniff_image_type(stream.read(64))
        if media not in EXT:
            return ""
        name = name or f"img_{len([i for i in self.images.values() if i.startswith('img_')]) + 1:05d}"
        href = f"{name}.{EXT[media]}"
        self.images[key] = href
        item_id = "cover-image" if name == "cover" else f"i{len(self.images)}"
        self.manifest.append((item_id, f"Images/{href}", media, "cover-image" if name == "cover" and self.version == 3 else ""))
        self.files.append((f"OEBPS/Images/{href}", path))
        return href

    def page(self, title: str, body: str) -> bytes:
        if self.kepub:
            body = koboize(body)
            body = f'<div id="book-columns"><div id="book-inner">{body}</div></div>'
        if self.version == 3:
            head = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        else:
            head = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml">')
        doc = (f'{head}\n<head>\n  <title>{esc(title)}</title>\n  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>\n'
               f'  <link rel="stylesheet" type="text/css" href="../Styles/stylesheet.css"/>\n</head>\n<body>{body}</body>\n</html>\n')
        return doc.encode("utf-8")

    def add_page(self, item_id: str, name: str, title: str, body: str, nav_label: str | None = None, props: str = "") -> None:
        href = f"Text/{name}.xhtml"
        self.files.append((f"OEBPS/{href}", self.page(title, body)))
        self.manifest.append((item_id, href, "application/xhtml+xml", props))
        self.spine.append(item_id)
        if nav_label is not None:
            self.nav.append((item_id, nav_label, href))

    def heading(self, title: str) -> str:
        return f'<h1 style="text-align:center">{esc(title)}</h1>' if self.show_titles else ""

    # ------------------------------------------------------------ parts

    def cover(self) -> None:
        if self.book.cover is None:
            return
        href = self.image(self.book.cover, "cover")
        if href:
            self.add_page("cover", "cover", "Cover", f'<div><img alt="cover" src="../Images/{href}"/></div>', "Cover")

    def introduction(self) -> None:
        b = self.book
        parts = [f'<h1 style="text-align:center">{esc(b.title)}</h1>']
        if b.authors:
            parts.append(f'<p class="meta">{esc(", ".join(b.authors))}</p>')
        if b.source_name:
            parts.append(f'<p class="meta">{esc(b.source_name)}</p>')
        if b.genres:
            parts.append(f'<p class="meta">{esc(", ".join(b.genres))}</p>')
        for line in (b.detail or "").splitlines():
            if line.strip():
                parts.append(f'<p class="meta">{esc(line.strip())}</p>')
        for line in (b.description or "").splitlines():
            if line.strip():
                parts.append(f"<p>{esc(line.strip())}</p>")
        if b.source_url:
            parts.append(f'<p class="meta"><a href="{esc(b.source_url)}">{esc(b.source_url)}</a></p>')
        self.add_page("introduction", "introduction", b.title, "".join(parts), "Introduction")

    def chapter_body(self, chapter) -> str:
        parts = [self.heading(chapter.title)]
        if chapter.kind == "images":
            for page in chapter.pages:
                href = self.image(page)
                if href:
                    parts.append(f'<div class="page"><img src="../Images/{href}" alt=""/></div>')
        elif chapter.kind == "text":
            def swap(match: re.Match) -> str:
                path = Path(html.unescape(match.group(1)))
                href = self.image(path) if path.is_file() else ""
                return f'src="../Images/{href}"' if href else 'src=""'
            body = re.sub(r'src="([^"]*)"', swap, chapter.html)
            body = re.sub(r'<img\b[^>]*src=""[^>]*/>', "", body)
            parts.append(body)
        elif chapter.remote_images:
            # not downloaded, images known by address: linked like vBook's exports (a reader online shows them)
            parts.extend(f'<div class="page"><img src="{esc(url)}" alt=""/></div>' for url in chapter.remote_images)
        else:
            link = f' <a href="{esc(chapter.source_url)}">{esc(chapter.source_url)}</a>' if re.match(r"^https?://", chapter.source_url or "") else ""
            parts.append(f'<p class="meta">Chương này chưa được tải.{link}</p>')
        return "".join(parts)

    def write(self, output: Path) -> Path:
        self.cover()
        if self.include_intro:
            self.introduction()
        toc_index = len(self.spine)
        chapter_links: list[tuple[str, str]] = []
        for index, chapter in enumerate(self.book.chapters, 1):
            name = f"chapter_{index}"
            self.add_page(f"c{index}", name, chapter.title, self.chapter_body(chapter), chapter.title)
            chapter_links.append((chapter.title, f"../Text/{name}.xhtml"))
        if self.include_toc:
            body = '<h1 style="text-align:center">Table of content</h1>' + "".join(f'<p class="toc"><a href="{href}">{esc(title)}</a></p>' for title, href in chapter_links)
            href = "Text/toc.xhtml"
            self.files.append((f"OEBPS/{href}", self.page("Table of content", body)))
            self.manifest.append(("toc", href, "application/xhtml+xml", ""))
            self.spine.insert(toc_index, "toc")
            nav_at = sum(1 for n in self.nav if n[0] in {"cover", "introduction"})
            self.nav.insert(nav_at, ("toc", "Table of content", href))
        return self.package(output)

    def ncx(self) -> bytes:
        points = "".join(
            f'\n    <navPoint id="{esc(item_id)}" playOrder="{order}">\n      <navLabel>\n        <text>{esc(label)}</text>\n      </navLabel>\n      <content src="{esc(href)}" />\n    </navPoint>'
            for order, (item_id, label, href) in enumerate(self.nav, 1)
        )
        return (f'<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n  <head>\n'
                f'    <meta name="dtb:uid" content="{self.identifier}" />\n    <meta name="dtb:depth" content="1" />\n'
                f'    <meta name="dtb:totalPageCount" content="0" />\n    <meta name="dtb:maxPageNumber" content="0" />\n  </head>\n'
                f'  <docTitle>\n    <text>{esc(self.book.title)}</text>\n  </docTitle>\n  <navMap>{points}\n  </navMap>\n</ncx>\n').encode("utf-8")

    def nav_doc(self) -> bytes:
        items = "".join(f'<li><a href="{esc(href[5:] if href.startswith("Text/") else href)}">{esc(label)}</a></li>' for _id, label, href in self.nav)
        body = f'<nav epub:type="toc" id="toc"><h1>Table of content</h1><ol>{items}</ol></nav>'
        doc = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
               f'<head><title>{esc(self.book.title)}</title><meta charset="utf-8"/></head>\n<body>{body}</body>\n</html>\n')
        return doc.encode("utf-8")

    def opf(self) -> bytes:
        b = self.book
        meta = [f'<dc:identifier id="BookId">{self.identifier}</dc:identifier>', f"<dc:title>{esc(b.title)}</dc:title>"]
        meta += [f"<dc:creator>{esc(a)}</dc:creator>" for a in (b.authors or ([b.source_name] if b.source_name else []))]
        meta.append(f"<dc:language>{esc(normalize_language(b.language))}</dc:language>")
        if b.description:
            meta.append(f"<dc:description>{esc(b.description[:4000])}</dc:description>")
        meta += [f"<dc:subject>{esc(g)}</dc:subject>" for g in b.genres[:20]]
        if b.source_url:
            meta.append(f"<dc:source>{esc(b.source_url)}</dc:source>")
        meta.append("<dc:publisher>Lynxx</dc:publisher>")
        if any(m[0] == "cover-image" for m in self.manifest):
            meta.append('<meta name="cover" content="cover-image" />')
        if self.version == 3:
            meta.append(f'<meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>')
        items = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />',
                 '<item id="stylesheet" href="Styles/stylesheet.css" media-type="text/css" />']
        if self.version == 3:
            items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />')
        for item_id, href, media, props in self.manifest:
            items.append(f'<item id="{esc(item_id)}" href="{esc(href)}" media-type="{media}"{f" properties={chr(34)}{props}{chr(34)}" if props else ""} />')
        spine = " ".join(f'<itemref idref="{esc(i)}" />' for i in self.spine)
        guide = ""
        if self.version == 2:
            refs = []
            if "cover" in self.spine:
                refs.append('<reference type="cover" title="Cover" href="Text/cover.xhtml" />')
            if "toc" in self.spine:
                refs.append('<reference type="toc" title="Table of content" href="Text/toc.xhtml" />')
            guide = f"\n  <guide>\n    {' '.join(refs)}\n  </guide>" if refs else ""
        version = "3.0" if self.version == 3 else "2.0"
        return (f'<?xml version="1.0" encoding="utf-8"?>\n<package version="{version}" unique-identifier="BookId" xmlns="http://www.idpf.org/2007/opf">\n'
                f'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n    {" ".join(meta)}\n  </metadata>\n'
                f'  <manifest>\n    {" ".join(items)}\n  </manifest>\n  <spine toc="ncx">\n    {spine}\n  </spine>{guide}\n</package>\n').encode("utf-8")

    def package(self, output: Path) -> Path:
        output = Path(output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
                archive.writestr("META-INF/container.xml", '<?xml version="1.0"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n  <rootfiles>\n    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n  </rootfiles>\n</container>\n')
                archive.writestr("OEBPS/content.opf", self.opf())
                archive.writestr("OEBPS/toc.ncx", self.ncx())
                if self.version == 3:
                    archive.writestr("OEBPS/nav.xhtml", self.nav_doc())
                archive.writestr("OEBPS/Styles/stylesheet.css", CSS)
                for name, data in self.files:
                    if isinstance(data, Path):
                        archive.write(data, name, compress_type=zipfile.ZIP_STORED if name.startswith("OEBPS/Images/") else zipfile.ZIP_DEFLATED)
                    else:
                        archive.writestr(name, data)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output


def normalize_language(value: str) -> str:
    v = (value or "").replace("_", "-").strip()
    if not v or v.lower() in {"locale", "global", "und"}:
        return "und"
    return v.split("-")[0].lower() if len(v) > 2 and v[2] == "-" and v[:2].isalpha() else v.lower()[:12]


_KOBO_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "blockquote", "dd", "dt", "figcaption", "pre", "div"}
_SENTENCE = re.compile(r"(?<=[.!?…。！？])\s+")


def koboize(fragment: str) -> str:
    """Wrap text runs (split into sentences) and images in <span class="koboSpan" id="kobo.P.S"> like kepubify."""
    try:
        root = ET.fromstring(f'<div xmlns="{XHTML_NS}">{fragment}</div>')
    except ET.ParseError:
        return fragment
    counter = {"p": 0}
    ns = f"{{{XHTML_NS}}}"

    def span(text: str, para: int, seg: int) -> ET.Element:
        el = ET.Element(f"{ns}span", {"class": "koboSpan", "id": f"kobo.{para}.{seg}"})
        el.text = text
        return el

    def process(block: ET.Element) -> None:
        counter["p"] += 1
        para = counter["p"]
        seg = [0]

        def wrap_text(text: str) -> list[ET.Element]:
            out = []
            for piece in [p for p in _SENTENCE.split(text) if p]:
                seg[0] += 1
                out.append(span(piece, para, seg[0]))
            return out

        def walk(el: ET.Element) -> None:
            children = list(el)
            if el.text and el.text.strip():
                spans = wrap_text(el.text)
                el.text = None
                for i, s in enumerate(spans):
                    el.insert(i, s)
            for child in children:
                tag = child.tag.replace(ns, "")
                if tag == "img":
                    seg[0] += 1
                    idx = list(el).index(child)
                    wrapper = ET.Element(f"{ns}span", {"class": "koboSpan", "id": f"kobo.{para}.{seg[0]}"})
                    el.remove(child)
                    tail = child.tail
                    child.tail = None
                    wrapper.append(child)
                    wrapper.tail = tail
                    el.insert(idx, wrapper)
                    child = wrapper
                elif tag in _KOBO_BLOCKS:
                    process(child)
                elif tag != "span" or "koboSpan" not in (child.get("class") or ""):
                    walk(child)
                if child.tail and child.tail.strip():
                    spans = wrap_text(child.tail)
                    child.tail = None
                    idx = list(el).index(child) + 1
                    for i, s in enumerate(spans):
                        el.insert(idx + i, s)

        walk(block)

    process(root)
    ET.register_namespace("", XHTML_NS)
    text = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    text = re.sub(r'^<div xmlns="http://www.w3.org/1999/xhtml">', "", text)
    text = re.sub(r"</div>$", "", text)
    return text.replace(f' xmlns="{XHTML_NS}"', "")


def write_epub(book: ExportBook, output: str | Path, *, version: int = 2, kepub: bool = False, include_intro: bool = True,
               include_toc: bool = True, show_titles: bool = True) -> Path:
    return _Writer(book, version, kepub, include_intro, include_toc, show_titles).write(Path(output))


def new_identifier() -> str:
    return uuid.uuid4().hex
