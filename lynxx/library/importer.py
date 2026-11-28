"""Import local files into the Lynxx-managed library.

PDF stays a PDF (shown by the PDF viewer); every other supported format (EPUB, TXT, MOBI/AZW3, FB2, DOCX, ODT,
RTF, HTML, Markdown, CHM, DOC/WPS, HWP/HWPX, UMD, CBZ/ZIP…) is converted into library chapters, and the original
file is kept next to them unchanged.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..artifacts import detect_document, sha256_file
from ..formats import SUPPORTED_EXTENSIONS, FormatError, convert, sniff_format
from .books import store_document
from .db import LibraryDB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_pdf(original: Path, library_root: Path, db: LibraryDB, title: str = "") -> dict[str, str | int]:
    detect_document(original)
    digest = sha256_file(original)
    collection_id = f"local-{uuid.uuid4().hex}"
    chapter_id = f"chapter-{uuid.uuid4().hex}"
    artifact_id = f"artifact-{uuid.uuid4().hex}"
    root = library_root.expanduser().resolve()
    destination = root / "collections" / collection_id / "chapters" / chapter_id / "original.pdf"
    destination.parent.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(original, destination)
    size = destination.stat().st_size
    if size != original.stat().st_size or sha256_file(destination) != digest:
        shutil.rmtree(destination.parent.parent.parent, ignore_errors=True)
        raise IOError("import checksum verification failed")
    now = _now()
    display_title = title.strip() or original.stem
    with db.connection:
        db.create_collection(collection_id, display_title, source_id="local", remote_id=collection_id, metadata_json='{"sourceFormat": "pdf"}',
                             created_at=now, updated_at=now, kind="document")
        db.create_chapter(chapter_id, collection_id, display_title, "pdf", "ready", created_at=now, updated_at=now, content_path=str(destination))
        db.create_artifact(artifact_id, chapter_id, "original", str(destination), "application/pdf", size, digest, "ready", now)
    return {"collectionId": collection_id, "chapterId": chapter_id, "artifactId": artifact_id, "path": str(destination), "kind": "pdf",
            "size": size, "sha256": digest, "chapters": 1, "title": display_title}


def import_document(source: str | Path, library_root: str | Path, db: LibraryDB, title: str = "") -> dict[str, str | int]:
    """Validate, convert and register a local file; nothing is written to the library when conversion fails."""
    original = Path(source).expanduser().resolve(strict=True)
    if not original.is_file():
        raise ValueError("source is not a regular file")
    ext = original.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise FormatError(f"unsupported file type: .{ext or '?'}")
    root = Path(library_root).expanduser().resolve()
    fmt = sniff_format(original, ext)
    if fmt == "pdf":
        return import_pdf(original, root, db, title)
    document = convert(original, ext)
    result = store_document(db, root, document, original=original, title=title.strip(), source_format=fmt)
    return {**result, "kind": result["kind"], "format": fmt}


def upgrade_legacy(db: LibraryDB, library_root: str | Path, chapter_id: str) -> dict[str, str | int] | None:
    """Collections imported by the first reader (an EPUB kept as one 'epub' chapter) are converted on first open."""
    chapter = db.chapter(chapter_id)
    if not chapter or chapter["content_kind"] != "epub":
        return None
    artifacts = [a for a in db.chapter_artifacts(chapter_id) if a["kind"] == "original"]
    if not artifacts or not Path(artifacts[0]["path"]).is_file():
        return None
    collection = db.collection(chapter["collection_id"])
    root = Path(library_root).expanduser().resolve()
    result = import_document(artifacts[0]["path"], root, db, collection["title"] if collection else "")
    from .books import delete_collection

    delete_collection(db, root, chapter["collection_id"])
    return result
