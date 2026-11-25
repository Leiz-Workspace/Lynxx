"""Safe EPUB image-page extraction for the local reader."""

from __future__ import annotations

import base64
import mimetypes
import posixpath
import zipfile
from pathlib import Path

from ..artifacts import validate_epub_members


def epub_image_pages(path: str | Path, max_pages: int = 2000, max_page_bytes: int = 25 * 1024 * 1024) -> list[dict[str, str | int]]:
    """Return image resources as data URLs after validating the EPUB archive."""
    candidate = Path(path).expanduser().resolve(strict=True)
    validate_epub_members(candidate)
    pages: list[dict[str, str | int]] = []
    with zipfile.ZipFile(candidate) as archive:
        names = [info for info in archive.infolist() if not info.is_dir() and Path(info.filename).suffix.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}]
        for number, info in enumerate(names[:max_pages], 1):
            if info.file_size > max_page_bytes:
                raise ValueError(f"EPUB page is too large: {info.filename}")
            payload = archive.read(info)
            mime = mimetypes.guess_type(info.filename)[0] or 'image/jpeg'
            pages.append({'number': number, 'name': posixpath.basename(info.filename), 'src': f'data:{mime};base64,{base64.b64encode(payload).decode("ascii")}'})
    if not pages:
        raise ValueError('EPUB has no supported image pages')
    return pages
