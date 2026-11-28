"""Library collections from extension downloads, incremental chapter merge, reading data and exports."""

from __future__ import annotations

import base64
import re
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from lynxx.export import service as export_service
from lynxx.export.pdf import NeedsTranscode, write_image_pdf
from lynxx.library import LibraryDB
from lynxx.library import books
from lynxx.library.importer import import_document

JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")


def png(width: int = 2, height: int = 2, alpha: bool = False) -> bytes:
    channels = 4 if alpha else 3
    raw = b"".join(b"\x00" + bytes([200, 30, 30, 128][:channels]) * width for _ in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00\x2f\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"


class LibraryBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.db = LibraryDB(Path(self.tmp.name) / "library.sqlite")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def stage(self, collection_id: str, chapter_id: str, name: str, data: bytes) -> Path:
        path = self.root / "staging" / collection_id / chapter_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def comic(self) -> dict:
        detail = {"name": "Truyện Thử", "author": "Tác Giả", "description": "Dòng 1\nDòng 2", "genres": ["Hài"], "cover": "https://x.test/c.jpg"}
        toc = [{"name": "Chương 1", "url": "https://x.test/b/1/c1"}, {"name": "Chương 2", "url": "https://x.test/b/1/c2"}]
        return books.upsert_book(self.db, self.root, plugin_id="site-12345678", plugin_name="TruyenQQ", source="https://x.test",
                                 url="https://x.test/b/1/", kind="comic", detail=detail, chapters=toc)

    def test_incremental_chapter_merge_keeps_downloaded_chapters(self) -> None:
        book = self.comic()
        cid = book["collectionId"]
        first = book["chapters"][0]
        pages = [self.stage(cid, first["id"], "0001.jpg", JPEG), self.stage(cid, first["id"], "0002.png", png())]
        result = books.commit_images(self.db, self.root, first["id"], [{"path": str(p), "sourceUrl": "https://x.test/p"} for p in pages])
        self.assertEqual(result["pageCount"], 2)
        self.assertFalse((self.root / "staging" / cid / first["id"]).exists())
        # the site published chapter 3; the same book URL (with or without the slash) updates the same collection
        again = books.upsert_book(self.db, self.root, plugin_id="site-12345678", plugin_name="TruyenQQ", source="https://x.test",
                                  url="https://x.test/b/1", kind="comic", detail={"name": "Truyện Thử"},
                                  chapters=[{"name": "Chương 1", "url": "https://x.test/b/1/c1"}, {"name": "Chương 2", "url": "https://x.test/b/1/c2"},
                                            {"name": "Chương 3", "url": "https://x.test/b/1/c3"}])
        self.assertEqual(again["collectionId"], cid)
        self.assertEqual([c["state"] for c in again["chapters"]], ["ready", "pending", "pending"])
        detail = books.collection_detail(self.db, self.root, cid)
        self.assertEqual(detail["author"], "Tác Giả")  # kept from the first run
        read = books.read_chapter(self.db, self.root, first["id"])
        self.assertEqual(read["kind"], "images")
        self.assertTrue(read["pages"][0]["url"].startswith("lynxx-lib://lib/collections/"))

    def test_staged_paths_outside_staging_are_refused(self) -> None:
        book = self.comic()
        outside = Path(self.tmp.name) / "evil.jpg"
        outside.write_bytes(JPEG)
        with self.assertRaises(ValueError):
            books.commit_images(self.db, self.root, book["chapters"][0]["id"], [{"path": str(outside)}])
        fake = self.stage(book["collectionId"], book["chapters"][0]["id"], "0001.jpg", b"<html>not an image</html>")
        with self.assertRaises(ValueError):
            books.commit_images(self.db, self.root, book["chapters"][0]["id"], [{"path": str(fake)}])

    def novel(self) -> tuple[str, str]:
        book = books.upsert_book(self.db, self.root, plugin_id="novel-00000001", plugin_name="TruyenFull", source="https://n.test",
                                 url="https://n.test/b", kind="novel", detail={"name": "Tiểu Thuyết"},
                                 chapters=[{"name": "Chương 1", "url": "https://n.test/b/c1"}, {"name": "Chương 2", "url": "https://n.test/b/c2"}])
        cid = book["collectionId"]
        ch = book["chapters"][0]["id"]
        img = self.stage(cid, ch, "img-001", png())
        books.commit_text(self.db, self.root, ch, '<p onclick="x()">Xin chào<br>thế giới</p><script>alert(1)</script><img src="https://n.test/a.png"><img src="https://evil.test/x.png">',
                          title="Chương 1: Mở đầu", images=[{"src": "https://n.test/a.png", "path": str(img)}])
        return cid, ch

    def test_text_chapter_is_sanitized_and_images_are_local(self) -> None:
        cid, ch = self.novel()
        read = books.read_chapter(self.db, self.root, ch)
        self.assertEqual(read["kind"], "text")
        self.assertNotIn("script", read["html"])
        self.assertNotIn("onclick", read["html"])
        self.assertEqual(read["html"].count("<img"), 1)
        self.assertIn("lynxx-lib://lib/collections/", read["html"])
        self.assertEqual(books.collection_detail(self.db, self.root, cid)["chapters"][0]["title"], "Chương 1: Mở đầu")

    def test_epub_exports_follow_the_vbook_layout(self) -> None:
        cid, _ch = self.novel()
        for fmt in ("epub2", "epub3", "kepub"):
            out = Path(self.tmp.name) / f"out-{fmt}.epub"
            result = export_service.export_collection(self.db, self.root, cid, fmt, {"scope": "all", "missing": "link"}, out)
            self.assertEqual(result["chapters"], 2)
            with zipfile.ZipFile(out) as archive:
                infos = archive.infolist()
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                names = archive.namelist()
                for required in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/toc.ncx", "OEBPS/Styles/stylesheet.css",
                                 "OEBPS/Text/introduction.xhtml", "OEBPS/Text/toc.xhtml", "OEBPS/Text/chapter_1.xhtml", "OEBPS/Text/chapter_2.xhtml"):
                    self.assertIn(required, names)
                toc = archive.read("OEBPS/Text/toc.xhtml").decode()
                self.assertIn('href="../Text/chapter_1.xhtml"', toc)
                ncx = archive.read("OEBPS/toc.ncx").decode()
                self.assertEqual(ncx.count("<navPoint"), 4)  # introduction, table of content, 2 chapters
                chapter = archive.read("OEBPS/Text/chapter_1.xhtml").decode()
                self.assertIn('src="../Images/img_00001.png"', chapter)
                missing = archive.read("OEBPS/Text/chapter_2.xhtml").decode()
                self.assertIn("chưa được tải", missing)
                opf = archive.read("OEBPS/content.opf").decode()
                self.assertIn('version="3.0"' if fmt != "epub2" else 'version="2.0"', opf)
                if fmt != "epub2":
                    self.assertIn("OEBPS/nav.xhtml", names)
                if fmt == "kepub":
                    self.assertIn('class="koboSpan"', chapter)
        # downloaded-only scope leaves the missing chapter out; the export is remembered for refreshing
        out = Path(self.tmp.name) / "only.epub"
        self.assertEqual(export_service.export_collection(self.db, self.root, cid, "epub2", {"scope": "downloaded"}, out)["chapters"], 1)
        self.assertEqual(len(books.collection_detail(self.db, self.root, cid)["exports"]), 4)

    def test_comic_cbz_and_pdf(self) -> None:
        book = self.comic()
        cid = book["collectionId"]
        staged_cover = self.root / "staging" / cid / "cover.img"
        staged_cover.parent.mkdir(parents=True, exist_ok=True)
        staged_cover.write_bytes(JPEG)
        books.set_cover(self.db, self.root, cid, str(staged_cover))
        for chapter in book["chapters"]:
            files = [self.stage(cid, chapter["id"], "0001.jpg", JPEG), self.stage(cid, chapter["id"], "0002.png", png(alpha=True))]
            books.commit_images(self.db, self.root, chapter["id"], [{"path": str(f)} for f in files])
        cbz = Path(self.tmp.name) / "book.cbz"
        export_service.export_collection(self.db, self.root, cid, "cbz", {}, cbz)
        with zipfile.ZipFile(cbz) as archive:
            info = archive.read("ComicInfo.xml").decode()
            self.assertIn('Bookmark="Chương 2"', info)
            self.assertEqual(len([n for n in archive.namelist() if n != "ComicInfo.xml"]), 5)
        pdf = Path(self.tmp.name) / "book.pdf"
        export_service.export_collection(self.db, self.root, cid, "pdf", {}, pdf)
        data = pdf.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.5"))
        self.assertEqual(data.count(b"/Type /Page "), 5)
        self.assertIn(b"/SMask", data)
        self.assertEqual(len(re.findall(rb"/Dest \[\d+ 0 R /Fit\]", data)), 3)  # cover + 2 chapters
        self.assertEqual(export_service.suggested_name(self.db, self.root, cid, "epub2"), "Truyện Thử_TruyenQQ.epub")

    def test_webp_pages_need_transcoding(self) -> None:
        page = Path(self.tmp.name) / "p.webp"
        page.write_bytes(WEBP)
        with self.assertRaises(NeedsTranscode) as ctx:
            write_image_pdf([page], Path(self.tmp.name) / "x.pdf")
        self.assertEqual(ctx.exception.paths, [str(page)])

    def test_novel_pdf_goes_through_the_html_bundle(self) -> None:
        cid, _ch = self.novel()
        with self.assertRaises(export_service.NeedsPrint):
            export_service.export_collection(self.db, self.root, cid, "pdf", {}, Path(self.tmp.name) / "n.pdf")
        bundle = export_service.html_bundle(self.db, self.root, cid, {"missing": "link"}, Path(self.tmp.name) / "n.html")
        text = Path(bundle["path"]).read_text(encoding="utf-8")
        self.assertIn('href="#c1"', text)
        self.assertIn('id="c2"', text)
        self.assertIn("file://", text)

    def test_exported_epub_imports_back_as_chapters(self) -> None:
        cid, _ch = self.novel()
        out = Path(self.tmp.name) / "round.epub"
        export_service.export_collection(self.db, self.root, cid, "epub3", {"scope": "downloaded"}, out)
        result = import_document(out, self.root, self.db)
        detail = books.collection_detail(self.db, self.root, str(result["collectionId"]))
        self.assertEqual(detail["kind"], "document")
        titles = [c["title"] for c in detail["chapters"]]
        self.assertIn("Chương 1: Mở đầu", titles)
        text = next(c for c in detail["chapters"] if c["title"] == "Chương 1: Mở đầu")
        read = books.read_chapter(self.db, self.root, text["id"])
        self.assertIn("Xin chào", read["html"])
        self.assertIn("lynxx-lib://", read["html"])
        self.assertTrue(books.delete_collection(self.db, self.root, str(result["collectionId"])))


if __name__ == "__main__":
    unittest.main()


class SniffFormatTests(unittest.TestCase):
    """The registry trusts the bytes over the extension (".doc" files that are web pages, RTF or plain text)."""

    def test_disguised_files(self) -> None:
        from lynxx.formats import sniff_format
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "page.doc": (b"<!DOCTYPE html><html><body><p>x</p></body></html>", "html"),
                "letter.doc": (b"{\\rtf1\\ansi hello}", "rtf"),
                "notes.doc": ("Chương 1\nnội dung".encode(), "txt"),
                "book.pdf": (b"%PDF-1.4\n", "pdf"),
                "comic.zip": (b"", "zip"),
            }
            for name, (data, expected) in cases.items():
                path = root / name
                if name.endswith(".zip"):
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("001.jpg", JPEG)
                else:
                    path.write_bytes(data)
                self.assertEqual(sniff_format(path), expected, name)
