"""The export view of a library collection: metadata + the chapters an export should contain."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..library.books import chapter_html, resolve_resource
from ..library.db import LibraryDB


@dataclass(slots=True)
class ExportChapter:
    id: str
    title: str
    kind: str
    """"images", "text" or "missing" (not downloaded)."""
    number: int
    pages: list[Path] = field(default_factory=list)
    html: str = ""
    """Sanitized fragment whose <img src> are absolute local paths (resources of this chapter)."""
    source_url: str = ""
    remote_images: list[str] = field(default_factory=list)
    """Image links of a chapter that is not downloaded (written as links when options.missing == "link")."""


@dataclass(slots=True)
class ExportBook:
    id: str
    title: str
    kind: str
    authors: list[str] = field(default_factory=list)
    description: str = ""
    detail: str = ""
    language: str = ""
    genres: list[str] = field(default_factory=list)
    cover: Path | None = None
    source_name: str = ""
    source_url: str = ""
    chapters: list[ExportChapter] = field(default_factory=list)

    @property
    def is_images(self) -> bool:
        return all(c.kind in {"images", "missing"} for c in self.chapters) and any(c.kind == "images" for c in self.chapters)


SCOPES = {"all", "downloaded", "range", "selected"}


def build_book(db: LibraryDB, root: Path, collection_id: str, options: dict[str, Any] | None = None) -> ExportBook:
    """Collect the chapters for an export.

    options.scope: "all" (every chapter; missing ones follow options.missing: "skip" | "link"), "downloaded",
    "range" (options.from / options.to, 1-based positions), "selected" (options.chapterIds).
    """
    options = options or {}
    row = db.collection(collection_id)
    if not row:
        raise ValueError("collection not found")
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except ValueError:
        meta = {}
    scope = options.get("scope", "all")
    if scope not in SCOPES:
        raise ValueError("invalid export scope")
    missing_mode = options.get("missing", "skip")
    chapters = db.chapters(collection_id)
    selected = set(options.get("chapterIds") or [])
    first = max(1, int(options.get("from") or 1))
    last = int(options.get("to") or len(chapters))
    book = ExportBook(
        id=collection_id, title=row["title"], kind=row.get("kind") or "document",
        authors=[a.strip() for a in re.split(r"[,;]", str(meta.get("author") or "")) if a.strip()],
        description=str(meta.get("description") or ""), detail=str(meta.get("detail") or ""), language=str(meta.get("language") or "")[:20],
        genres=[str(g) for g in meta.get("genres") or []], source_name=str(meta.get("pluginName") or ""),
        source_url=str(meta.get("bookUrl") or ""),
    )
    cover = row.get("cover_path")
    if cover and Path(cover).is_file():
        book.cover = Path(cover)
    for number, chapter in enumerate(chapters, 1):
        if scope == "range" and not first <= number <= last:
            continue
        if scope == "selected" and chapter["id"] not in selected:
            continue
        ready = chapter["state"] == "ready"
        if not ready:
            if scope == "downloaded" or missing_mode != "link":
                continue
            remote = [p["source_url"] for p in db.pages(chapter["id"]) if re.match(r"^https?://", p["source_url"] or "")] if chapter["content_kind"] == "image" else []
            book.chapters.append(ExportChapter(chapter["id"], chapter["title"], "missing", number, source_url=chapter["source_url"], remote_images=remote))
            continue
        if chapter["content_kind"] == "image":
            pages = [Path(p["local_path"]) for p in db.pages(chapter["id"]) if Path(p["local_path"]).is_file()]
            if pages:
                book.chapters.append(ExportChapter(chapter["id"], chapter["title"], "images", number, pages=pages, source_url=chapter["source_url"]))
        elif chapter["content_kind"] == "text":
            html = chapter_html(root, chapter, lambda p: str(p))
            book.chapters.append(ExportChapter(chapter["id"], chapter["title"], "text", number, html=html, source_url=chapter["source_url"]))
        # pdf / legacy chapters have no reflowable content to export
    if not book.chapters:
        raise ValueError("no downloaded chapter matches the export scope")
    return book


def default_filename(book: ExportBook, fmt: str) -> str:
    """'<title>_<source>.<ext>' like vBook ('Chào Mừng Cậu Đến Trường Iruma-Kun_TruyenQQ.epub')."""
    stem = book.title
    if book.source_name:
        stem = f"{stem}_{book.source_name}"
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", stem).strip().rstrip(".")[:150] or "book"
    ext = {"epub2": "epub", "epub3": "epub", "kepub": "kepub.epub", "cbz": "cbz", "pdf": "pdf"}[fmt]
    return f"{stem}.{ext}"


__all__ = ["ExportBook", "ExportChapter", "build_book", "default_filename", "resolve_resource"]
