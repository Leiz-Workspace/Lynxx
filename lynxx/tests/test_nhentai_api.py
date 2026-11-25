"""Offline tests for nhentai payload normalization only."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from lynxx.sources import parse_gallery_payload


class NhentaiApiTests(unittest.TestCase):
    def test_normalizes_explicit_page_urls_in_order(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "nhentai" / "gallery.json"
        collection, pages = parse_gallery_payload(json.loads(fixture.read_text(encoding="utf-8")))
        self.assertEqual(collection.remote_id, "123456")
        self.assertEqual(collection.title, "Fixture Gallery")
        self.assertEqual([page.number for page in pages], [1, 2])
        self.assertEqual(pages[1].mime_type, "image/png")

    def test_rejects_guessed_or_unapproved_page_hosts(self) -> None:
        payload = {"id": 1, "media_id": "m", "title": {"english": "x"}, "pages": [{"number": 1, "url": "https://evil.test/x.jpg"}]}
        with self.assertRaises(ValueError):
            parse_gallery_payload(payload)

    def test_rejects_nonconsecutive_pages(self) -> None:
        payload = {"id": 1, "media_id": "m", "title": {"english": "x"}, "pages": [{"number": 2, "url": "https://i1.nhentai.net/x.jpg"}]}
        with self.assertRaises(ValueError):
            parse_gallery_payload(payload)


if __name__ == "__main__":
    unittest.main()
