"""Local PDF/EPUB import into a Lynxx-managed library."""

from __future__ import annotations

import mimetypes
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..artifacts import detect_document, sha256_file, validate_epub_members
from .db import LibraryDB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_document(source: str | Path, library_root: str | Path, db: LibraryDB, title: str = "") -> dict[str, str | int]:
    """Copy a validated document into the managed library and register it atomically."""
    original = Path(source).expanduser().resolve(strict=True)
    if not original.is_file():
        raise ValueError("source is not a regular file")
    kind = detect_document(original)
    if kind == "epub":
        validate_epub_members(original)
    digest = sha256_file(original)
    collection_id = f"local-{uuid.uuid4().hex}"
    chapter_id = f"chapter-{uuid.uuid4().hex}"
    artifact_id = f"artifact-{uuid.uuid4().hex}"
    root = Path(library_root).expanduser().resolve()
    destination = root / "collections" / collection_id / "chapters" / chapter_id / f"original.{kind}"
    destination.parent.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(original, destination)
    size = destination.stat().st_size
    if size != original.stat().st_size or sha256_file(destination) != digest:
        shutil.rmtree(destination.parent.parent.parent, ignore_errors=True)
        raise IOError("import checksum verification failed")
    now = _now()
    display_title = title.strip() or original.stem
    mime = "application/pdf" if kind == "pdf" else "application/epub+zip"
    with db.connection:
        db.create_collection(collection_id, display_title, metadata_json="{}", created_at=now, updated_at=now)
        db.create_chapter(chapter_id, collection_id, display_title, kind, "ready", created_at=now, updated_at=now)
        db.create_artifact(artifact_id, chapter_id, kind, str(destination), mime, size, digest, "ready", now)
    return {"collectionId": collection_id, "chapterId": chapter_id, "artifactId": artifact_id, "path": str(destination), "kind": kind, "size": size, "sha256": digest}
