"""CBZ / ZIP → Document.

A comic archive (images, optionally in one folder per chapter, optional ComicInfo.xml) becomes an "images" document;
a ZIP holding a single supported book (one .epub / .fb2 / .txt …) is converted through that format instead.
"""

from __future__ import annotations

import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import Chapter, Document, FormatError, escape, open_zip, read_member, sniff_image_type

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
_BOOK_EXT = {".epub", ".fb2", ".txt", ".mobi", ".azw3", ".azw", ".docx", ".odt", ".rtf", ".html", ".htm", ".md", ".pdf"}


def _natural(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _comic_info(data: bytes) -> dict[str, str]:
    if b"<!ENTITY" in data[:4096].upper():
        return {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    return {child.tag: (child.text or "").strip() for child in root}


def convert(path: Path) -> Document:
    archive = open_zip(path)
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        images = [info for info in infos if posixpath.splitext(info.filename.lower())[1] in _IMAGE_EXT
                  and not posixpath.basename(info.filename).startswith(".") and "__MACOSX/" not in info.filename]
        if not images:
            books = [info for info in infos if posixpath.splitext(info.filename.lower())[1] in _BOOK_EXT]
            if len(books) == 1:
                inner = books[0]
                suffix = posixpath.splitext(inner.filename.lower())[1]
                if inner.file_size > 512 * 1024 * 1024:
                    raise FormatError("inner document is too large")
                from . import convert as convert_any  # late import: the registry imports this module

                with tempfile.TemporaryDirectory(prefix="lynxx-zip-") as tmp:
                    target = Path(tmp) / f"inner{suffix}"
                    target.write_bytes(read_member(archive, inner.filename, 512 * 1024 * 1024))
                    if suffix == ".pdf":
                        raise FormatError("ZIP contains a PDF: extract it and open the PDF directly")
                    document = convert_any(target)
                    if not document.title or document.title == "inner":
                        document.title = Path(inner.filename).stem
                    return document
            raise FormatError("archive has no images or supported document")
        info = {}
        comic = next((i for i in infos if posixpath.basename(i.filename).lower() == "comicinfo.xml"), None)
        if comic is not None:
            info = _comic_info(read_member(archive, comic.filename, 1024 * 1024))
        document = Document(title=info.get("Title") or info.get("Series") or path.stem, kind="images", source_format="cbz")
        if info.get("Writer"):
            document.authors = [info["Writer"]]
        document.description = info.get("Summary", "")[:20000]
        document.language = info.get("LanguageISO", "")[:20]
        groups: dict[str, list] = {}
        for item in sorted(images, key=lambda i: _natural(i.filename)):
            folder = posixpath.dirname(item.filename)
            groups.setdefault(folder, []).append(item)
        for folder in sorted(groups, key=_natural):
            body = []
            for number, item in enumerate(groups[folder], 1):
                data = read_member(archive, item.filename, 80 * 1024 * 1024)
                media = sniff_image_type(data)
                if not media or media == "image/svg+xml":
                    continue
                name = document.add_resource(data, media, "page")
                body.append(f'<div class="page"><img src="{name}" alt="{escape(f"Trang {number}")}"/></div>')
            if body:
                title = posixpath.basename(folder) if len(groups) > 1 and folder else document.title
                document.chapters.append(Chapter(title or "Trang", "".join(body)))
        if not document.resources:
            raise FormatError("archive has no supported images")
        document.cover = document.resources[0]
        return document
