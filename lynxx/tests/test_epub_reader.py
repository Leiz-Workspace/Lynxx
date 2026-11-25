"""Tests for safe EPUB page extraction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lynxx.artifacts import assemble_epub
from lynxx.library.epub_reader import epub_image_pages


class EpubReaderTests(unittest.TestCase):
    def test_returns_local_data_urls_in_archive_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "001.jpg", root / "002.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            book = assemble_epub([first, second], root / "book.epub", "Reader")
            pages = epub_image_pages(book)
            self.assertEqual([page["number"] for page in pages], [1, 2])
            self.assertTrue(str(pages[0]["src"]).startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
