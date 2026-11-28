"""Export a library collection to EPUB 2 / EPUB 3 / KEPUB / CBZ / PDF and remember the export so it can be refreshed.

Refreshing an export (after new chapters were downloaded) rebuilds the same file with the same options: the book
then contains every chapter the library has, and chapters already downloaded are never fetched again.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..library.db import LibraryDB
from .book import ExportBook, build_book, default_filename
from .cbz import write_cbz
from .epub import normalize_language, write_epub
from .pdf import NeedsTranscode, write_image_pdf

FORMATS = ("epub2", "epub3", "kepub", "cbz", "pdf")


class NeedsPrint(ValueError):
    """A text book as PDF: the app renders the HTML bundle with Chromium (printToPDF) and records the result."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_options(options: dict[str, Any] | None) -> dict[str, Any]:
    o = options or {}
    scope = o.get("scope") if o.get("scope") in {"all", "downloaded", "range", "selected"} else "all"
    out = {
        "scope": scope, "missing": "link" if o.get("missing") == "link" else "skip",
        "includeIntro": o.get("includeIntro", True) is not False, "includeToc": o.get("includeToc", True) is not False,
        "showTitles": o.get("showTitles", True) is not False, "autoUpdate": bool(o.get("autoUpdate")),
    }
    if scope == "range":
        out["from"] = max(1, int(o.get("from") or 1))
        out["to"] = max(out["from"], int(o.get("to") or out["from"]))
    if scope == "selected":
        out["chapterIds"] = [str(c) for c in (o.get("chapterIds") or [])][:20000]
    return out


def suggested_name(db: LibraryDB, root: Path, collection_id: str, fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError("unsupported export format")
    row = db.collection(collection_id)
    if not row:
        raise ValueError("collection not found")
    meta = json.loads(row.get("metadata_json") or "{}")
    book = ExportBook(id=collection_id, title=row["title"], kind=row.get("kind") or "", source_name=str(meta.get("pluginName") or ""))
    return default_filename(book, fmt)


def record_export(db: LibraryDB, collection_id: str, fmt: str, path: Path, options: dict[str, Any], chapter_ids: list[str]) -> dict[str, Any]:
    stamp = _now()
    size = path.stat().st_size
    digest = _sha256(path)
    with db.connection:
        row = db.connection.execute("SELECT id, created_at FROM exports WHERE collection_id = ? AND path = ?", (collection_id, str(path))).fetchone()
        export_id = row["id"] if row else f"export-{uuid.uuid4().hex}"
        db.connection.execute(
            """INSERT INTO exports(id,collection_id,format,path,options_json,chapter_count,chapters_json,size,sha256,auto_update,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET format=excluded.format, options_json=excluded.options_json,
            chapter_count=excluded.chapter_count, chapters_json=excluded.chapters_json, size=excluded.size, sha256=excluded.sha256,
            auto_update=excluded.auto_update, updated_at=excluded.updated_at""",
            (export_id, collection_id, fmt, str(path), json.dumps(options, ensure_ascii=False), len(chapter_ids), json.dumps(chapter_ids),
             size, digest, 1 if options.get("autoUpdate") else 0, row["created_at"] if row else stamp, stamp),
        )
    return {"id": export_id, "path": str(path), "format": fmt, "size": size, "sha256": digest, "chapters": len(chapter_ids)}


def export_collection(db: LibraryDB, root: Path, collection_id: str, fmt: str, options: dict[str, Any] | None, output: str | Path,
                      replacements: dict[str, str] | None = None) -> dict[str, Any]:
    if fmt not in FORMATS:
        raise ValueError("unsupported export format")
    opts = clean_options(options)
    book = build_book(db, root, collection_id, opts)
    target = Path(output).expanduser()
    if fmt in {"epub2", "epub3", "kepub"}:
        write_epub(book, target, version=3 if fmt != "epub2" else 2, kepub=fmt == "kepub", include_intro=opts["includeIntro"],
                   include_toc=opts["includeToc"], show_titles=opts["showTitles"])
    elif fmt == "cbz":
        write_cbz(book, target)
    else:
        if not book.is_images:
            raise NeedsPrint("text book: print the HTML bundle")
        pages: list[Path] = []
        outline: list[tuple[int, str]] = []
        if book.cover is not None and opts["includeIntro"]:
            pages.append(book.cover)
            outline.append((0, "Bìa"))
        for chapter in book.chapters:
            if chapter.kind != "images" or not chapter.pages:
                continue
            outline.append((len(pages), chapter.title))
            pages.extend(chapter.pages)
        write_image_pdf(pages, target, title=book.title, author=", ".join(book.authors), outline=outline if opts["includeToc"] else [], replacements=replacements)
    return record_export(db, collection_id, fmt, target, opts, [c.id for c in book.chapters])


PRINT_CSS = """
@page { size: A5; margin: 14mm 12mm 16mm; }
html { -webkit-print-color-adjust: exact; }
body { font-family: "Noto Serif", "Source Serif 4", Georgia, "Times New Roman", serif; font-size: 11.5pt; line-height: 1.55; color: #111; }
h1 { text-align: center; font-size: 17pt; margin: 0 0 1.2em; break-before: page; }
section:first-of-type h1, .cover + section h1 { break-before: auto; }
p { text-indent: 1.4em; margin: 0 0 .45em; text-align: justify; }
p.meta { text-indent: 0; text-align: center; color: #444; }
.cover { break-after: page; text-align: center; }
.cover img { max-width: 100%; max-height: 180mm; }
.toc ol { list-style: none; padding: 0; }
.toc li { margin: .25em 0; }
.toc a { color: inherit; text-decoration: none; }
img { max-width: 100%; }
.page { text-align: center; break-inside: avoid; }
.page img { display: block; margin: 0 auto; max-height: 178mm; }
a { color: #3a5a8a; }
"""


def html_bundle(db: LibraryDB, root: Path, collection_id: str, options: dict[str, Any] | None, output: str | Path) -> dict[str, Any]:
    """A single HTML file (cover, introduction, linked table of contents, chapters) for Chromium's printToPDF."""
    opts = clean_options(options)
    book = build_book(db, root, collection_id, opts)
    esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731
    file_url = lambda p: Path(p).resolve().as_uri()  # noqa: E731
    parts = [f'<!doctype html><html lang="{esc(normalize_language(book.language))}"><head><meta charset="utf-8"><title>{esc(book.title)}</title>'
             f"<style>{PRINT_CSS}</style></head><body>"]
    if book.cover is not None and opts["includeIntro"]:
        parts.append(f'<div class="cover"><img src="{file_url(book.cover)}" alt=""></div>')
    if opts["includeIntro"]:
        intro = [f"<h1>{esc(book.title)}</h1>"]
        if book.authors:
            intro.append(f'<p class="meta">{esc(", ".join(book.authors))}</p>')
        if book.source_name:
            intro.append(f'<p class="meta">{esc(book.source_name)}</p>')
        intro += [f"<p>{esc(line.strip())}</p>" for line in book.description.splitlines() if line.strip()]
        parts.append(f'<section class="intro">{"".join(intro)}</section>')
    if opts["includeToc"]:
        items = "".join(f'<li><a href="#c{i}">{esc(c.title)}</a></li>' for i, c in enumerate(book.chapters, 1))
        parts.append(f'<section class="toc"><h1>Mục lục</h1><ol>{items}</ol></section>')
    for i, chapter in enumerate(book.chapters, 1):
        heading = f"<h1>{esc(chapter.title)}</h1>" if opts["showTitles"] else '<h1 style="font-size:0;margin:0">&nbsp;</h1>'
        if chapter.kind == "images":
            body = "".join(f'<div class="page"><img src="{file_url(p)}" alt=""></div>' for p in chapter.pages)
        elif chapter.kind == "text":
            body = re.sub(r'src="([^"]+)"', lambda m: f'src="{file_url(html.unescape(m.group(1)))}"' if Path(html.unescape(m.group(1))).is_file() else 'src=""', chapter.html)
        else:
            body = f'<p class="meta">Chương này chưa được tải. <a href="{esc(chapter.source_url)}">{esc(chapter.source_url)}</a></p>'
        parts.append(f'<section id="c{i}" class="chapter">{heading}{body}</section>')
    parts.append("</body></html>")
    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(parts), encoding="utf-8")
    return {"path": str(target), "chapters": [c.id for c in book.chapters], "options": opts, "title": book.title}


def auto_update_exports(db: LibraryDB, collection_id: str) -> list[dict[str, Any]]:
    rows = db.connection.execute("SELECT * FROM exports WHERE collection_id = ? AND auto_update = 1", (collection_id,)).fetchall()
    return [{"id": r["id"], "format": r["format"], "path": r["path"], "options": json.loads(r["options_json"] or "{}")} for r in rows]


def forget_export(db: LibraryDB, export_id: str) -> bool:
    with db.connection:
        return db.connection.execute("DELETE FROM exports WHERE id = ?", (export_id,)).rowcount > 0


__all__ = ["FORMATS", "NeedsPrint", "NeedsTranscode", "auto_update_exports", "clean_options", "export_collection", "forget_export",
           "html_bundle", "record_export", "suggested_name"]
