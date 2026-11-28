"""Collection-centric library services: extension downloads, converted documents, reading data.

Layout under the managed library root::

    collections/<collection>/cover.<ext>
    collections/<collection>/res/<name>                     resources shared by converted documents
    collections/<collection>/chapters/<chapter>/pages/0001.jpg   image chapters
    collections/<collection>/chapters/<chapter>/chapter.xhtml    text chapters (sanitized XHTML fragment)
    collections/<collection>/chapters/<chapter>/res/<name>       images of one text chapter
    collections/<collection>/chapters/<chapter>/original.<ext>   imported originals
    staging/<collection>/<chapter>/...                      downloads in progress (written by the app)

Every path received from the app is checked to stay inside the expected folder; files are moved, hashed and
recorded in one transaction per chapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..formats.base import Document, FormatError, sanitize_fragment, sniff_image_type
from .db import LibraryDB

IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/avif": "avif", "image/bmp": "bmp"}
MAX_TITLE = 500
LIB_SCHEME = "lynxx-lib://lib/"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path is outside the managed library")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sniff_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sniff_image_type(stream.read(64))


def collection_dir(root: Path, collection_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", collection_id):
        raise ValueError("invalid collection id")
    return root / "collections" / collection_id


def chapter_dir(root: Path, collection_id: str, chapter_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", chapter_id):
        raise ValueError("invalid chapter id")
    return collection_dir(root, collection_id) / "chapters" / chapter_id


def lib_url(root: Path, path: str | Path) -> str:
    """URL of a library file for the app's lynxx-lib:// protocol ('' when the file is not inside the library)."""
    if not path:
        return ""
    try:
        rel = Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return LIB_SCHEME + "/".join(quote(part) for part in rel.parts)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(row.get("metadata_json") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


# ---------------------------------------------------------------- extension books

def upsert_book(db: LibraryDB, root: Path, *, plugin_id: str, plugin_name: str, source: str, url: str, kind: str,
                detail: dict[str, Any], chapters: list[dict[str, Any]]) -> dict[str, Any]:
    """Create or refresh a downloaded book and merge its chapter list (existing chapters keep their files)."""
    if not re.fullmatch(r"[a-z0-9-]{1,48}", plugin_id or ""):
        raise ValueError("invalid extension id")
    remote = str(url or "").strip().rstrip("/")
    if not re.match(r"^https?://", remote):
        raise ValueError("invalid book URL")
    kind = "comic" if kind == "comic" else "novel"
    source_id = f"plugin:{plugin_id}"
    stamp = now()
    meta = {
        "author": _text(detail.get("author"), 300), "description": _text(detail.get("description"), 20000),
        "detail": _text(detail.get("detail"), 5000), "genres": [_text(g, 80) for g in (detail.get("genres") or [])][:60],
        "coverUrl": _text(detail.get("cover"), 2000), "host": _text(detail.get("host"), 300), "ongoing": bool(detail.get("ongoing")),
        "nsfw": bool(detail.get("nsfw")), "language": _text(detail.get("language"), 20), "pluginId": plugin_id,
        "pluginName": _text(plugin_name, 120), "source": _text(source, 500), "bookUrl": remote,
    }
    title = _text(detail.get("name"), MAX_TITLE) or remote
    with db.connection:
        row = db.connection.execute("SELECT * FROM collections WHERE source_id = ? AND remote_id = ?", (source_id, remote)).fetchone()
        created = row is None
        if created:
            collection_id = f"book-{uuid.uuid4().hex}"
            db.create_collection(collection_id, title, source_id=source_id, remote_id=remote, metadata_json=json.dumps(meta, ensure_ascii=False),
                                 created_at=stamp, updated_at=stamp, kind=kind, source_url=remote)
            has_cover = False
        else:
            collection_id = row["id"]
            old = _metadata(dict(row))
            meta = {**old, **{k: v for k, v in meta.items() if v not in ("", [], None)}}
            db.connection.execute("UPDATE collections SET title=?, metadata_json=?, updated_at=?, kind=?, source_url=? WHERE id=?",
                                  (title, json.dumps(meta, ensure_ascii=False), stamp, kind, remote, collection_id))
            has_cover = bool(row["cover_path"]) and Path(row["cover_path"]).is_file()
        existing = {r["remote_id"]: dict(r) for r in db.connection.execute("SELECT * FROM chapters WHERE collection_id = ?", (collection_id,))}
        seen: set[str] = set()
        for position, entry in enumerate(chapters[:20000]):
            ch_url = str(entry.get("url") or "").strip()
            if not re.match(r"^https?://", ch_url) or ch_url in seen:
                continue
            seen.add(ch_url)
            ch_title = _text(entry.get("name"), 300) or f"Chương {position + 1}"
            locked = 1 if entry.get("locked") else 0
            if ch_url in existing:
                db.connection.execute("UPDATE chapters SET title=?, position=?, locked=?, updated_at=? WHERE id=?",
                                      (ch_title, position, locked, stamp, existing[ch_url]["id"]))
            else:
                db.create_chapter(f"ch-{uuid.uuid4().hex}", collection_id, ch_title, "image" if kind == "comic" else "text", "pending",
                                  remote_id=ch_url, created_at=stamp, updated_at=stamp, source_url=ch_url, position=position, locked=bool(locked))
        # chapters the site no longer lists keep their files and sort after the current list
        for ch_url, ch in existing.items():
            if ch_url not in seen and ch["position"] < 1_000_000:
                db.connection.execute("UPDATE chapters SET position=? WHERE id=?", (1_000_000 + ch["position"], ch["id"]))
    rows = db.chapters(collection_id)
    return {
        "collectionId": collection_id, "created": created, "hasCover": has_cover,
        "chapters": [{"id": r["id"], "url": r["source_url"], "title": r["title"], "state": r["state"], "position": r["position"], "locked": bool(r["locked"])} for r in rows],
    }


def set_cover(db: LibraryDB, root: Path, collection_id: str, staged: str, source_url: str = "") -> dict[str, str]:
    staging = _inside(Path(staged), root / "staging")
    kind = _sniff_file(staging)
    if not kind or kind == "image/svg+xml":
        raise FormatError("cover is not an image")
    target_dir = collection_dir(root, collection_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"cover.{IMAGE_EXT.get(kind, 'img')}"
    for old in target_dir.glob("cover.*"):
        old.unlink(missing_ok=True)
    shutil.move(str(staging), target)
    with db.connection:
        db.connection.execute("UPDATE collections SET cover_path=?, updated_at=? WHERE id=?", (str(target), now(), collection_id))
    return {"coverPath": str(target), "coverUrl": lib_url(root, target)}


def _chapter_row(db: LibraryDB, chapter_id: str) -> dict[str, Any]:
    row = db.chapter(chapter_id)
    if not row:
        raise ValueError("chapter not found")
    return row


def commit_images(db: LibraryDB, root: Path, chapter_id: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Move downloaded page files from staging into the chapter, record Page rows (sha256, size, type)."""
    chapter = _chapter_row(db, chapter_id)
    collection_id = chapter["collection_id"]
    staging_root = root / "staging" / collection_id / chapter_id
    if not pages:
        raise ValueError("no pages")
    checked: list[tuple[Path, str, str]] = []
    for page in pages[:5000]:
        source = _inside(Path(str(page.get("path") or "")), staging_root)
        if not source.is_file():
            raise FileNotFoundError(f"staged page missing: {source.name}")
        kind = _sniff_file(source)
        if not kind or kind == "image/svg+xml":
            raise FormatError(f"page is not an image: {source.name}")
        checked.append((source, kind, str(page.get("sourceUrl") or "")[:2000]))
    target = chapter_dir(root, collection_id, chapter_id)
    fresh = target / "pages.new"
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True)
    rows = []
    for number, (source, kind, url) in enumerate(checked, 1):
        dest = fresh / f"{number:04d}.{IMAGE_EXT.get(kind, 'img')}"
        shutil.move(str(source), dest)
        rows.append((number, dest.name, kind, _sha256(dest), dest.stat().st_size, url))
    final = target / "pages"
    old = target / "pages.old"
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        os.replace(final, old)
    os.replace(fresh, final)
    shutil.rmtree(old, ignore_errors=True)
    stamp = now()
    with db.connection:
        db.connection.execute("DELETE FROM pages WHERE chapter_id = ?", (chapter_id,))
        for number, name, kind, digest, size, url in rows:
            db.create_page(f"pg-{uuid.uuid4().hex}", chapter_id, number, str(final / name), kind, digest, size, "ready", source_url=url)
        db.connection.execute("UPDATE chapters SET state='ready', content_kind='image', content_path=?, page_count=?, error='', updated_at=? WHERE id=?",
                              (str(final), len(rows), stamp, chapter_id))
        db.connection.execute("UPDATE collections SET updated_at=? WHERE id=?", (stamp, collection_id))
    shutil.rmtree(staging_root, ignore_errors=True)
    return {"chapterId": chapter_id, "pageCount": len(rows), "bytes": sum(r[4] for r in rows)}


def commit_text(db: LibraryDB, root: Path, chapter_id: str, html: str, title: str = "", images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Store a text chapter: sanitized XHTML fragment + its downloaded images (src rewritten to local names)."""
    chapter = _chapter_row(db, chapter_id)
    collection_id = chapter["collection_id"]
    staging_root = root / "staging" / collection_id / chapter_id
    target = chapter_dir(root, collection_id, chapter_id)
    res_dir = target / "res"
    mapping: dict[str, str] = {}
    pending: list[tuple[Path, Path]] = []
    for index, image in enumerate((images or [])[:500], 1):
        try:
            source = _inside(Path(str(image.get("path") or "")), staging_root)
        except ValueError:
            continue
        if not source.is_file():
            continue
        kind = _sniff_file(source)
        if not kind or kind == "image/svg+xml":
            continue
        name = f"img-{index:03d}.{IMAGE_EXT.get(kind, 'img')}"
        mapping[str(image.get("src") or "")] = name
        pending.append((source, res_dir / name))
    body = sanitize_fragment(str(html or ""), resolve_src=lambda src: mapping.get(src, ""))
    if not body.strip():
        raise FormatError("chapter is empty after cleaning")
    target.mkdir(parents=True, exist_ok=True)
    if pending:
        shutil.rmtree(res_dir, ignore_errors=True)
        res_dir.mkdir(parents=True)
        for source, dest in pending:
            shutil.move(str(source), dest)
    content = target / "chapter.xhtml"
    tmp = target / "chapter.xhtml.tmp"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, content)
    stamp = now()
    new_title = _text(title, 300)
    with db.connection:
        db.connection.execute("UPDATE chapters SET state='ready', content_kind='text', content_path=?, page_count=?, error='', updated_at=?" +
                              (", title=?" if new_title else "") + " WHERE id=?",
                              (str(content), len(pending), stamp, *( [new_title] if new_title else []), chapter_id))
        db.connection.execute("UPDATE collections SET updated_at=? WHERE id=?", (stamp, collection_id))
    shutil.rmtree(staging_root, ignore_errors=True)
    return {"chapterId": chapter_id, "images": len(pending)}


def set_chapter_state(db: LibraryDB, chapter_id: str, state: str, error: str = "") -> None:
    if state not in {"pending", "failed", "ready", "partial"}:
        raise ValueError("invalid chapter state")
    with db.connection:
        db.connection.execute("UPDATE chapters SET state=?, error=?, updated_at=? WHERE id=?", (state, error[:300], now(), chapter_id))


# ---------------------------------------------------------------- converted documents

def store_document(db: LibraryDB, root: Path, document: Document, *, original: Path | None, title: str = "",
                   source_format: str = "") -> dict[str, Any]:
    """Write a converted Document into the library as a collection (images → page chapters, text → XHTML chapters)."""
    collection_id = f"local-{uuid.uuid4().hex}"
    base = collection_dir(root, collection_id)
    base.mkdir(parents=True, exist_ok=False)
    stamp = now()
    resources = {r.name: r for r in document.resources}
    kind = "comic" if document.kind == "images" else "document"
    try:
        chapter_rows = []
        res_dir = base / "res"
        if document.kind != "images" and resources:
            res_dir.mkdir()
            for res in document.resources:
                (res_dir / res.name).write_bytes(res.data)
        cover_path = ""
        if document.cover is not None:
            ext = IMAGE_EXT.get(document.cover.media_type, "")
            if ext:
                cover = base / f"cover.{ext}"
                cover.write_bytes(document.cover.data)
                cover_path = str(cover)
        for position, chapter in enumerate(document.chapters):
            chapter_id = f"ch-{uuid.uuid4().hex}"
            target = base / "chapters" / chapter_id
            target.mkdir(parents=True)
            if document.kind == "images":
                pages_dir = target / "pages"
                pages_dir.mkdir()
                names = re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', chapter.html)
                page_rows = []
                for number, name in enumerate(names, 1):
                    res = resources.get(name)
                    if res is None or res.media_type not in IMAGE_EXT:
                        continue
                    dest = pages_dir / f"{number:04d}.{IMAGE_EXT[res.media_type]}"
                    dest.write_bytes(res.data)
                    page_rows.append((len(page_rows) + 1, dest, res.media_type, hashlib.sha256(res.data).hexdigest(), len(res.data)))
                if not page_rows and chapter.remote_images:
                    # only linked, not embedded: kept as a chapter to download later (the links are its pages)
                    remote = [(n, "", "", "", 0, url) for n, url in enumerate(chapter.remote_images, 1) if re.match(r"^https?://", url)]
                    chapter_rows.append((chapter_id, chapter.title, "image", "", 0, remote))
                    continue
                if not page_rows:
                    continue
                chapter_rows.append((chapter_id, chapter.title, "image", str(pages_dir), len(page_rows), page_rows))
            else:
                content = target / "chapter.xhtml"
                content.write_text(chapter.html, encoding="utf-8")
                chapter_rows.append((chapter_id, chapter.title, "text", str(content), 0, []))
        if not chapter_rows:
            raise FormatError("document has no readable content")
        meta = {"author": ", ".join(document.authors)[:300], "description": document.description[:20000], "language": document.language[:20],
                "sourceFormat": source_format or document.source_format, "genres": []}
        display = (title or document.title or (original.stem if original else "") or "Tài liệu")[:MAX_TITLE]
        original_info: dict[str, Any] = {}
        with db.connection:
            db.create_collection(collection_id, display, metadata_json=json.dumps(meta, ensure_ascii=False), created_at=stamp, updated_at=stamp,
                                 kind=kind, cover_path=cover_path)
            for position, (chapter_id, chapter_title, content_kind, content_path, count, page_rows) in enumerate(chapter_rows):
                pending = content_kind == "image" and not content_path
                db.create_chapter(chapter_id, collection_id, (chapter_title or f"Phần {position + 1}")[:300], content_kind, "pending" if pending else "ready",
                                  remote_id=chapter_id, created_at=stamp, updated_at=stamp, position=position, content_path=content_path, page_count=count)
                for row in page_rows:
                    if pending:
                        number, _dest, _media, _digest, _size, url = row
                        db.create_page(f"pg-{uuid.uuid4().hex}", chapter_id, number, "", "", "", 0, "pending", source_url=url[:2000])
                    else:
                        number, dest, media, digest, size = row
                        db.create_page(f"pg-{uuid.uuid4().hex}", chapter_id, number, str(dest), media, digest, size, "ready")
            if original is not None:
                first = next((row[0] for row in chapter_rows if row[3]), chapter_rows[0][0])
                kept = base / "chapters" / first / f"original{original.suffix.lower()[:12]}"
                shutil.copyfile(original, kept)
                digest = _sha256(kept)
                db.create_artifact(f"artifact-{uuid.uuid4().hex}", first, "original", str(kept), "application/octet-stream", kept.stat().st_size, digest, "ready", stamp)
                original_info = {"path": str(kept), "sha256": digest, "size": kept.stat().st_size}
    except Exception:
        shutil.rmtree(base, ignore_errors=True)
        raise
    first_ready = next((row[0] for row in chapter_rows if row[3]), chapter_rows[0][0])
    return {"collectionId": collection_id, "chapterId": first_ready, "chapters": len(chapter_rows), "kind": kind, "title": display,
            "pendingChapters": sum(1 for row in chapter_rows if not row[3]), **original_info}


# ---------------------------------------------------------------- listing / reading

def _cover_url(root: Path, row: dict[str, Any]) -> str:
    path = row.get("cover_path") or ""
    return lib_url(root, path) if path and Path(path).is_file() else ""


def list_collections(db: LibraryDB, root: Path) -> list[dict[str, Any]]:
    rows = db.connection.execute(
        """SELECT c.*, COUNT(ch.id) AS chapter_total, SUM(CASE WHEN ch.state='ready' THEN 1 ELSE 0 END) AS chapter_ready,
           SUM(CASE WHEN ch.state='failed' THEN 1 ELSE 0 END) AS chapter_failed, p.chapter_id AS last_chapter, p.updated_at AS read_at
           FROM collections c LEFT JOIN chapters ch ON ch.collection_id = c.id LEFT JOIN collection_progress p ON p.collection_id = c.id
           GROUP BY c.id ORDER BY COALESCE(p.updated_at, c.updated_at) DESC""").fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        meta = _metadata(row)
        last_title = ""
        if row.get("last_chapter"):
            r = db.chapter(row["last_chapter"])
            last_title = r["title"] if r else ""
        out.append({
            "id": row["id"], "title": row["title"], "kind": row.get("kind") or "document", "author": meta.get("author", ""),
            "source": meta.get("pluginName") or meta.get("sourceFormat", "").upper(), "pluginId": meta.get("pluginId", ""),
            "bookUrl": meta.get("bookUrl", ""), "coverUrl": _cover_url(root, row), "chapters": row["chapter_total"] or 0,
            "ready": row["chapter_ready"] or 0, "failed": row["chapter_failed"] or 0, "updatedAt": row["updated_at"],
            "lastChapterId": row.get("last_chapter") or "", "lastChapterTitle": last_title, "readAt": row.get("read_at") or "",
        })
    return out


def collection_detail(db: LibraryDB, root: Path, collection_id: str) -> dict[str, Any]:
    row = db.collection(collection_id)
    if not row:
        raise ValueError("collection not found")
    meta = _metadata(row)
    chapters = db.chapters(collection_id)
    exports = [dict(r) for r in db.connection.execute("SELECT * FROM exports WHERE collection_id = ? ORDER BY updated_at DESC", (collection_id,))]
    progress = db.collection_progress(collection_id)
    return {
        "id": collection_id, "title": row["title"], "kind": row.get("kind") or "document", "coverUrl": _cover_url(root, row),
        "author": meta.get("author", ""), "description": meta.get("description", ""), "detail": meta.get("detail", ""),
        "genres": meta.get("genres", []), "ongoing": meta.get("ongoing", False), "source": meta.get("pluginName") or meta.get("sourceFormat", "").upper(),
        "pluginId": meta.get("pluginId", ""), "bookUrl": meta.get("bookUrl", ""), "language": meta.get("language", ""),
        "lastChapterId": progress["chapter_id"] if progress else "",
        "remoteChapters": len(remote_chapters(db, collection_id)) if not meta.get("pluginId") else 0,
        "chapters": [{
            "id": c["id"], "title": c["title"], "state": c["state"], "kind": c["content_kind"], "pages": c["page_count"],
            "url": c["source_url"], "locked": bool(c["locked"]), "error": c.get("error") or "", "position": c["position"],
        } for c in chapters],
        "exports": [{
            "id": e["id"], "format": e["format"], "path": e["path"], "chapters": e["chapter_count"], "size": e["size"],
            "updatedAt": e["updated_at"], "autoUpdate": bool(e["auto_update"]), "options": json.loads(e["options_json"] or "{}"),
            "exists": Path(e["path"]).is_file(),
        } for e in exports],
    }


def remote_chapters(db: LibraryDB, collection_id: str) -> list[dict[str, Any]]:
    """Chapters that are not downloaded but whose page images are known by address (imported exports with links)."""
    out = []
    for chapter in db.chapters(collection_id):
        if chapter["state"] == "ready" or chapter["content_kind"] != "image":
            continue
        urls = [p["source_url"] for p in db.pages(chapter["id"]) if p["state"] != "ready" and re.match(r"^https?://", p["source_url"] or "")]
        if urls:
            out.append({"id": chapter["id"], "title": chapter["title"], "pages": urls})
    return out


def delete_collection(db: LibraryDB, root: Path, collection_id: str) -> bool:
    if not db.collection(collection_id):
        return False
    with db.connection:
        db.connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    shutil.rmtree(collection_dir(root, collection_id), ignore_errors=True)
    shutil.rmtree(root / "staging" / collection_id, ignore_errors=True)
    return True


def resolve_resource(root: Path, chapter: dict[str, Any], name: str) -> Path | None:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    cdir = chapter_dir(root, chapter["collection_id"], chapter["id"])
    for candidate in (cdir / "res" / name, collection_dir(root, chapter["collection_id"]) / "res" / name):
        if candidate.is_file():
            return candidate
    return None


def chapter_html(root: Path, chapter: dict[str, Any], url_for) -> str:
    """The stored XHTML fragment with resource names mapped through url_for(path) ('' drops the image)."""
    path = Path(chapter.get("content_path") or "")
    if not path.is_file():
        return ""
    _inside(path, root)
    body = path.read_text(encoding="utf-8")

    def resolve(src: str) -> str:
        res = resolve_resource(root, chapter, src)
        return url_for(res) if res else ""

    return sanitize_fragment(body, resolve_src=resolve)


def read_chapter(db: LibraryDB, root: Path, chapter_id: str) -> dict[str, Any]:
    chapter = _chapter_row(db, chapter_id)
    siblings = [c for c in db.chapters(chapter["collection_id"])]
    index = next((i for i, c in enumerate(siblings) if c["id"] == chapter_id), 0)
    ready = [c for c in siblings if c["state"] == "ready"]
    ready_ids = [c["id"] for c in ready]
    pos = ready_ids.index(chapter_id) if chapter_id in ready_ids else -1
    result: dict[str, Any] = {
        "chapterId": chapter_id, "collectionId": chapter["collection_id"], "title": chapter["title"], "index": index, "total": len(siblings),
        "prevId": ready_ids[pos - 1] if pos > 0 else "", "nextId": ready_ids[pos + 1] if 0 <= pos < len(ready_ids) - 1 else "",
        "progress": db.reader_progress(chapter_id), "state": chapter["state"], "sourceUrl": chapter["source_url"],
    }
    if chapter["state"] != "ready":
        result["kind"] = "pending"
        return result
    kind = chapter["content_kind"]
    if kind == "image":
        result["kind"] = "images"
        result["pages"] = [{"number": p["page_number"], "url": lib_url(root, p["local_path"])} for p in db.pages(chapter_id) if Path(p["local_path"]).is_file()]
    elif kind == "text":
        result["kind"] = "text"
        result["html"] = chapter_html(root, chapter, lambda p: lib_url(root, p))
    elif kind == "pdf":
        artifacts = db.chapter_artifacts(chapter_id)
        result["kind"] = "pdf"
        result["path"] = artifacts[0]["path"] if artifacts else ""
    else:
        result["kind"] = kind
        artifacts = db.chapter_artifacts(chapter_id)
        result["path"] = artifacts[0]["path"] if artifacts else ""
    return result
