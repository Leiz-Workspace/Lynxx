"""Tests for managed local document import and reader metadata."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lynxx.artifacts import assemble_epub
from lynxx.library import LibraryDB
from lynxx.library.importer import import_document
from lynxx.tests.test_library_books import JPEG


class LibraryImportTests(unittest.TestCase):
    def test_import_epub_registers_artifact_and_preserves_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "001.jpg"
            page.write_bytes(JPEG)
            source = assemble_epub([page], root / "source.epub", "Fixture book")
            db = LibraryDB(root / "db.sqlite")
            result = import_document(source, root / "managed", db)
            self.assertEqual(result["kind"], "comic")  # an image-only EPUB is read as pages
            self.assertEqual(result["format"], "epub")
            artifact = db.chapter_artifacts(str(result["chapterId"]))[0]
            self.assertEqual(artifact["sha256"], result["sha256"])
            self.assertEqual(Path(artifact["path"]).read_bytes(), source.read_bytes())
            db.save_reader_progress(str(result["chapterId"]), 2, 18.0, "vertical", 1.1, "now")
            self.assertEqual(db.reader_progress(str(result["chapterId"]))["page"], 2)
            db.close()

    def test_import_rejects_invalid_document_without_creating_library_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.pdf"
            source.write_bytes(b"not a PDF")
            db = LibraryDB(root / "db.sqlite")
            with self.assertRaises(ValueError):
                import_document(source, root / "managed", db)
            self.assertEqual(db.connection.execute("SELECT count(*) FROM collections").fetchone()[0], 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
