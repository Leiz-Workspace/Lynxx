"""Safe PDF/EPUB format detection and artifact assembly helpers."""

from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import posixpath
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

PDF_MAGIC = b"%PDF-"
EPUB_MIMETYPE = b"application/epub+zip"
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_document(path: str | Path) -> str:
    """Return pdf/epub, rejecting files whose extension is not enough evidence."""
    candidate = Path(path)
    with candidate.open("rb") as stream:
        prefix = stream.read(5)
    if prefix == PDF_MAGIC:
        return "pdf"
    if zipfile.is_zipfile(candidate):
        with zipfile.ZipFile(candidate) as archive:
            try:
                mimetype = archive.read("mimetype")
            except KeyError:
                mimetype = b""
            if mimetype == EPUB_MIMETYPE:
                return "epub"
    raise ValueError(f"unsupported or invalid document: {candidate.name}")


def validate_epub_members(path: str | Path) -> None:
    """Reject absolute paths, traversal and symlink entries before extraction."""
    with zipfile.ZipFile(path) as archive:
        total = 0
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or posixpath.normpath(name) != name or ".." in name.split("/"):
                raise ValueError(f"unsafe EPUB member: {info.filename}")
            if info.is_dir():
                continue
            # Unix mode 0o120000 denotes a symlink in a ZIP external attribute.
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(f"symlink is not allowed in EPUB: {info.filename}")
            total += info.file_size
            if total > MAX_ARTIFACT_BYTES:
                raise ValueError("EPUB exceeds the artifact size limit")


def assemble_epub(image_paths: Iterable[str | Path], output: str | Path, title: str) -> Path:
    """Create a minimal, deterministic image EPUB from local page files."""
    pages = [Path(page) for page in image_paths]
    if not pages:
        raise ValueError("at least one page is required")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            archive.writestr("mimetype", EPUB_MIMETYPE, compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", "<?xml version='1.0'?><container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/></rootfiles></container>")
            manifest = []
            spine = []
            for index, page in enumerate(pages, 1):
                if not page.is_file() or page.stat().st_size > MAX_ARTIFACT_BYTES:
                    raise ValueError(f"invalid page: {page}")
                media_type = mimetypes.guess_type(page.name)[0] or "image/jpeg"
                item_id = f"page-{index:04d}"
                archive.write(page, f"OEBPS/images/{item_id}{page.suffix.lower()}")
                manifest.append(f"<item id='{item_id}' href='images/{item_id}{page.suffix.lower()}' media-type='{media_type}'/>")
                spine.append(f"<itemref idref='{item_id}'/>")
            opf = f"<?xml version='1.0' encoding='utf-8'?><package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='book-id'><metadata xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:identifier id='book-id'>lynxx:{hashlib.sha256(title.encode()).hexdigest()[:16]}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>und</dc:language></metadata><manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine></package>"
            archive.writestr("OEBPS/content.opf", opf)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
