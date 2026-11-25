"""Source adapter tests never access live sites."""

from __future__ import annotations

import unittest

from lynxx.sources import CollectionRef, CuutruyenAdapter, NhentaiAdapter


class AdapterContractTests(unittest.TestCase):
    def test_nhentai_is_probe_only(self) -> None:
        with self.assertRaises(PermissionError):
            NhentaiAdapter().search("sample")

    def test_cuutruyen_never_retrieves_pages(self) -> None:
        collection = CollectionRef("cuutruyen", "1", "https://cuutruyen.net/chapters/1")
        with self.assertRaises(PermissionError):
            CuutruyenAdapter().list_pages(collection)


if __name__ == "__main__":
    unittest.main()
