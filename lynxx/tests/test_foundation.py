"""Offline tests for Lynxx's first library primitives."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from lynxx.artifacts import assemble_epub, detect_document, validate_epub_members
from lynxx.library import LibraryDB
from lynxx.policy import Operation, PolicyGate, PolicyState, SourcePolicy


class FoundationTests(unittest.TestCase):
    def test_policy_is_fail_closed_and_scoped(self) -> None:
        gate = PolicyGate((SourcePolicy("nh", ("nhentai.net",), PolicyState.BLOCKED, reason="policy review required"),))
        denied = gate.check("nh", "https://nhentai.net/api/v2/galleries/1", Operation.METADATA)
        self.assertFalse(denied.allowed)
        self.assertIn("policy", denied.reason)
        self.assertFalse(gate.check("nh", "https://example.test/x", Operation.METADATA).allowed)

    def test_reader_progress_requires_existing_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = LibraryDB(Path(directory) / "library.sqlite")
            db.create_collection("c", "Book", created_at="now", updated_at="now")
            db.create_chapter("ch", "c", "Chapter", "image", "pending", created_at="now", updated_at="now")
            db.save_reader_progress("ch", 3, 12.5, "horizontal", 1.2, "now")
            self.assertEqual(db.reader_progress("ch")["page"], 3)
            with self.assertRaises(ValueError):
                db.save_reader_progress("ch", 0, 0, "diagonal", 1, "now")
            db.close()

    def test_epub_assembly_and_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "page.jpg"
            page.write_bytes(b"not-a-real-image-fixture")
            output = assemble_epub([page], root / "book.epub", "A <safe> title")
            self.assertEqual(detect_document(output), "epub")
            validate_epub_members(output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("OEBPS/content.opf", archive.namelist())
                self.assertIn("&lt;safe&gt;", archive.read("OEBPS/content.opf").decode())

    def test_epub_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr("../escape.txt", "blocked")
            with self.assertRaises(ValueError):
                validate_epub_members(path)


if __name__ == "__main__":
    unittest.main()
