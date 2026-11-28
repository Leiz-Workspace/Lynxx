"""Tests for the text-family converters: TXT, Markdown, HTML, RTF, FB2 / FBZ.

Every input is generated in a temporary directory at test time.
"""

from __future__ import annotations

import base64
import os
import re
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from lynxx import formats
from lynxx.formats import fb2, htmldoc, markdown, rtf, text
from lynxx.formats.base import FormatError


def make_png(width: int = 2, height: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\x10\x80\xf0" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG = make_png()
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes | str, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode(encoding) if isinstance(data, str) else data)
        return path

    def assert_well_formed(self, markup: str) -> None:
        try:
            ET.fromstring(f"<root>{markup}</root>")
        except ET.ParseError as err:  # pragma: no cover - failure message
            self.fail(f"chapter XHTML is not well-formed ({err}): {markup[:500]!r}")

    def assert_document_ok(self, document) -> None:
        self.assertTrue(document.chapters)
        names = {resource.name for resource in document.resources}
        for chapter in document.chapters:
            self.assert_well_formed(chapter.html)
            lowered = chapter.html.lower()
            self.assertNotIn("<script", lowered)
            self.assertNotIn("javascript:", lowered)
            self.assertNotRegex(chapter.html, r"<[^>]*\son[a-z]+\s*=")
            for src in re.findall(r'<img[^>]*\ssrc="([^"]*)"', chapter.html):
                self.assertIn(src, names)


# ---------------------------------------------------------------- TXT

class TextConverterTests(_Case):
    def test_splits_vietnamese_chapters(self) -> None:
        path = self.write("truyen.txt", "Lời giới thiệu ngắn.\n\nChương 1: Khởi đầu\nDòng một.\nDòng hai.\n\n"
                                        "Chương 2: Tiếp theo\nNội dung <b>thô</b> & ký tự.\n")
        document = text.convert(path)
        self.assertEqual(document.title, "truyen")
        self.assertEqual([c.title for c in document.chapters], ["Phần 1", "Chương 1: Khởi đầu", "Chương 2: Tiếp theo"])
        self.assertIn("<p>Dòng một.</p><p>Dòng hai.</p>", document.chapters[1].html)
        self.assertIn("&lt;b&gt;thô&lt;/b&gt; &amp; ký tự.", document.chapters[2].html)
        self.assert_document_ok(document)

    def test_single_chapter_uses_file_stem(self) -> None:
        document = text.convert(self.write("ghi-chu.txt", "Chỉ một đoạn văn.\nThêm một dòng."))
        self.assertEqual([c.title for c in document.chapters], ["ghi-chu"])

    def test_gb18030_detection(self) -> None:
        body = "第1章 开始\n" + "他走进房间，环顾四周，然后坐在窗边。\n" * 50 + "第2章 继续\n天空很蓝。\n"
        document = text.convert(self.write("novel.txt", body.encode("gb18030")))
        self.assertEqual([c.title for c in document.chapters], ["第1章 开始", "第2章 继续"])
        self.assertIn("天空很蓝。", document.chapters[1].html)

    def test_utf16_with_and_without_bom(self) -> None:
        content = "Chapter 1\nHello wörld ✓\nChapter 2\nZweite Seite"
        for name, data in (("bom.txt", content.encode("utf-16")), ("le.txt", content.encode("utf-16-le"))):
            document = text.convert(self.write(name, data))
            self.assertEqual([c.title for c in document.chapters], ["Chapter 1", "Chapter 2"], name)
            self.assertIn("Hello wörld ✓", document.chapters[0].html)

    def test_control_characters_removed(self) -> None:
        document = text.convert(self.write("ctrl.txt", "a\x01b\x0cc\x1bd\n"))
        self.assertEqual(document.chapters[0].html, "<p>abcd</p>")
        self.assert_well_formed(document.chapters[0].html)

    def test_huge_chapter_is_cut_at_paragraphs(self) -> None:
        line = "Một câu văn khá dài để lấp đầy chương truyện không có tiêu đề nào cả.\n"
        document = text.convert(self.write("long.txt", line * 30000))
        self.assertGreater(len(document.chapters), 2)
        self.assertEqual(document.chapters[1].title, "long (2)")
        for chapter in document.chapters:
            self.assertTrue(chapter.html.startswith("<p>") and chapter.html.endswith("</p>"))
            self.assertLessEqual(len(chapter.html), text.MAX_CHAPTER_HTML)

    def test_empty_file_is_an_error(self) -> None:
        with self.assertRaises(FormatError):
            text.convert(self.write("empty.txt", b"\n\n  \n"))


# ---------------------------------------------------------------- Markdown

class MarkdownConverterTests(_Case):
    def render(self, source: str) -> str:
        return markdown.render_markdown(source)[0]

    def test_front_matter_and_level_one_chapters(self) -> None:
        source = ("---\ntitle: \"Sách Markdown\"\nauthor:\n  - An\n  - Bình\nlang: vi\n"
                  "description: Một cuốn thử\n---\n\nLời nói đầu.\n\n# Một\n\nĐoạn *một*.\n\n# Hai\n\nĐoạn **hai**.\n")
        document = markdown.convert(self.write("book.md", source))
        self.assertEqual(document.title, "Sách Markdown")
        self.assertEqual(document.authors, ["An", "Bình"])
        self.assertEqual(document.language, "vi")
        self.assertEqual(document.description, "Một cuốn thử")
        self.assertEqual([c.title for c in document.chapters], ["Sách Markdown", "Một", "Hai"])
        self.assertIn("<em>một</em>", document.chapters[1].html)
        self.assertNotIn("title:", "".join(c.html for c in document.chapters))
        self.assert_document_ok(document)

    def test_level_two_split_when_single_level_one(self) -> None:
        source = "Title\n=====\n\nIntro.\n\nFirst\n-----\n\nA.\n\n## Second\n\nB.\n"
        document = markdown.convert(self.write("notes.markdown", source))
        self.assertEqual(document.title, "Title")
        self.assertEqual([c.title for c in document.chapters], ["Title", "First", "Second"])
        self.assertIn("<h1", document.chapters[0].html)

    def test_inline_syntax(self) -> None:
        out = self.render("*em* **strong** ***both*** _u_ __uu__ snake_case `a <b>` \\*lit\\* ~~del~~ &copy;  \nnext")
        self.assertIn("<em>em</em>", out)
        self.assertIn("<strong>strong</strong>", out)
        self.assertIn("<em><strong>both</strong></em>", out)
        self.assertIn("<em>u</em>", out)
        self.assertIn("<strong>uu</strong>", out)
        self.assertIn("snake_case", out)
        self.assertIn("<code>a &lt;b&gt;</code>", out)
        self.assertIn("*lit*", out)
        self.assertIn("<del>del</del>", out)
        self.assertIn("©", out)
        self.assertIn("<br/>", out)

    def test_links_and_references(self) -> None:
        out = self.render('[a](http://x.org "T") <https://auto.example/p> [ref] [named][ref] [nope]\n\n'
                          "[ref]: https://ref.example 'R'\n")
        self.assertIn('<a href="http://x.org" title="T">a</a>', out)
        self.assertIn('<a href="https://auto.example/p">https://auto.example/p</a>', out)
        self.assertIn('<a href="https://ref.example" title="R">ref</a>', out)
        self.assertIn('<a href="https://ref.example" title="R">named</a>', out)
        self.assertIn("[nope]", out)
        self.assertNotIn("[ref]:", out)

    def test_blocks(self) -> None:
        source = ("> quoted\nlazy\n\n- a\n- b\n  1. x\n  2. y\n\n3) three\n\n    inside item\n\npara\n\n"
                  "    indented <code>\n\n"
                  "```python\nprint('<hi>')\n```\n\n***\n\n| h1 | h2 |\n|:---|---:|\n| c1 | *c2* |\n")
        out = self.render(source)
        self.assertIn("<blockquote><p>quoted\nlazy</p></blockquote>", out)
        self.assertIn("<ul><li>a</li><li>b<ol><li>x</li><li>y</li></ol></li></ul>", out)
        self.assertIn('<ol start="3"><li><p>three</p>\n<p>inside item</p></li></ol>', out)
        self.assertIn("<pre><code>indented &lt;code&gt;\n</code></pre>", out)
        self.assertIn('<pre><code class="language-python">print(&#x27;&lt;hi&gt;&#x27;)', out)
        self.assertIn("<hr/>", out)
        self.assertIn('<th style="text-align: left">h1</th>', out)
        self.assertIn('<td style="text-align: right"><em>c2</em></td>', out)

    def test_loose_list_and_setext(self) -> None:
        out = self.render("- one\n\n- two\n\nHeading\n---\n")
        self.assertIn("<li><p>one</p></li><li><p>two</p></li>", out)
        self.assertIn('<h2 id="heading">Heading</h2>', out)

    def test_raw_html_is_sanitized(self) -> None:
        source = ("# T\n\n<div onclick=\"steal()\">block <b>bold</b></div>\n\n"
                  "Text <span onmouseover=\"x()\">hover</span> <script>alert(1)</script> after "
                  "[js](javascript:alert(1)) <img src=\"x\" onerror=\"y()\">\n\n<script>\nevil()\n</script>\n\n"
                  "Still here <title>unclosed\n\nLast paragraph.\n")
        document = markdown.convert(self.write("raw.md", source))
        html = "".join(c.html for c in document.chapters)
        self.assertNotIn("onclick", html)
        self.assertNotIn("onmouseover", html)
        self.assertNotIn("evil()", html)
        self.assertNotIn("javascript", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)  # inline: shown as text
        self.assertIn("Last paragraph.", html)
        self.assert_document_ok(document)

    def test_images_local_data_remote_and_traversal(self) -> None:
        self.write("outside.png", PNG)
        book_dir = self.root / "book"
        self.write("book/img/pic.png", PNG)
        self.write("book/fake.png", b"not an image at all")
        self.write("book/notes.txt", PNG)
        data_uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        source = (f"# Images\n\n![local](img/pic.png) ![again](./img/pic.png) ![data]({data_uri})\n\n"
                  "![remote](https://example.com/a.png) ![up](../outside.png) ![abs](/etc/passwd.png) "
                  "![fake](fake.png) ![ext](notes.txt) ![missing](nope.png)\n")
        document = markdown.convert(self.write("book/doc.md", source))
        self.assertEqual(len(document.resources), 2)  # pic.png once + the data: image
        self.assertTrue(all(r.media_type == "image/png" for r in document.resources))
        html = document.chapters[0].html
        self.assertEqual(html.count("<img"), 3)
        self.assertNotIn("example.com", html)
        self.assertTrue(book_dir.is_dir())
        self.assert_document_ok(document)

    def test_symlink_escaping_folder_is_rejected(self) -> None:
        self.write("secret/real.png", PNG)
        (self.root / "book").mkdir()
        try:
            os.symlink(self.root / "secret" / "real.png", self.root / "book" / "link.png")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        document = markdown.convert(self.write("book/a.md", "Hi ![x](link.png)"))
        self.assertEqual(document.resources, [])

    def test_pathological_input_is_bounded(self) -> None:
        source = "*a " * 5000 + "[" * 3000 + "`" * 2000 + "\n\n" + "> " * 200 + "deep\n"
        out = self.render(source)
        self.assertIn("deep", out)


# ---------------------------------------------------------------- HTML

class HtmlConverterTests(_Case):
    def test_meta_charset_title_and_chapters(self) -> None:
        page = ('<html lang="ru"><head><meta charset="windows-1251"><title>Книга</title>'
                '<meta name="author" content="Автор"><meta name="description" content="Описание <b>книги</b>">'
                '<script>alert(1)</script></head><body onload="x()"><p>Вступление</p>'
                '<h1>Глава 1</h1><p onclick="bad()">Текст <a href="javascript:evil()">ссылка</a></p>'
                '<h1>Глава 2</h1><p style="color: red; background: url(http://x)">Ещё</p></body></html>')
        document = htmldoc.convert(self.write("book.html", page, "cp1251"))
        self.assertEqual(document.title, "Книга")
        self.assertEqual(document.authors, ["Автор"])
        self.assertEqual(document.language, "ru")
        self.assertEqual(document.description, "Описание книги")
        self.assertEqual([c.title for c in document.chapters], ["Книга", "Глава 1", "Глава 2"])
        self.assertIn("<p>Текст <a>ссылка</a></p>", document.chapters[1].html)
        self.assertIn('<p style="color: red">Ещё</p>', document.chapters[2].html)
        self.assert_document_ok(document)

    def test_http_equiv_and_h1_title_fallback(self) -> None:
        page = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1"></head>'
                '<body><h1>Caf\xe9</h1><p>Un seul chapitre.</p></body></html>')
        document = htmldoc.convert(self.write("one.htm", page, "latin-1"))
        self.assertEqual(document.title, "Café")
        self.assertEqual(len(document.chapters), 1)
        self.assertEqual(document.source_format, "htm")

    def test_utf16_bom_and_h2_split(self) -> None:
        page = "<html><body><h1>Book</h1><h2>One</h2><p>1</p><h2>Two</h2><p>2</p></body></html>"
        document = htmldoc.convert(self.write("u16.html", page.encode("utf-16")))
        self.assertEqual([c.title for c in document.chapters], ["Book", "One", "Two"])

    def test_nested_headings_stay_well_formed(self) -> None:
        page = ('<body><div class="book" id="b"><section><h1 id="c1">A</h1><p>x</p></section>'
                '<section><h1>B <em>bis</em></h1><p>y</p><ul><li>z</li></ul></section></div></body>')
        document = htmldoc.convert(self.write("nested.html", page))
        self.assertEqual([c.title for c in document.chapters], ["A", "B bis"])
        for chapter in document.chapters:
            self.assert_well_formed(chapter.html)
        self.assertTrue(document.chapters[1].html.startswith('<div class="book"><section><h1>'))
        self.assertEqual(sum(c.html.count('id="b"') for c in document.chapters), 1)

    def test_xhtml_declaration_encoding_and_images(self) -> None:
        self.write("pages/images/p.png", PNG)
        self.write("secret.png", PNG)
        data_uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        page = ('<?xml version="1.0" encoding="windows-1252"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
                '<head><title>Na\xefve</title></head><body><p>'
                '<img src="images/p.png" alt="p"/><img src="../secret.png"/><img src="http://e.com/x.png"/>'
                f'<img src="{data_uri}"/><img src="file:///etc/passwd"/></p></body></html>')
        document = htmldoc.convert(self.write("pages/doc.xhtml", page, "cp1252"))
        self.assertEqual(document.title, "Naïve")
        self.assertEqual(len(document.resources), 2)
        self.assertEqual(document.chapters[0].html.count("<img"), 2)
        self.assert_document_ok(document)

    def test_empty_body_is_an_error(self) -> None:
        with self.assertRaises(FormatError):
            htmldoc.convert(self.write("empty.html", "<html><body><script>x</script></body></html>"))


# ---------------------------------------------------------------- RTF

RTF_HEADER = (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\froman\fcharset0 Times New Roman;}"
              r"{\f1\fnil\fcharset134 SimSun;}{\f2\fnil\fcharset204 Arial Cyr;}{\f3\fnil\fcharset2 Symbol;}}"
              r"{\colortbl;\red255\green0\blue0;}"
              r"{\stylesheet{\s0 Normal;}{\s1\outlinelevel0 heading 1;}{\s2 Heading 2;}{\*\cs10 Default Font;}}"
              r"{\info{\title Th\'e9 Book}{\author Jane Doe}{\subject About things}{\operator op}}"
              r"\deflang1066{\header Running header}{\footer Page footer}")


class RtfConverterTests(_Case):
    def convert_rtf(self, body: str, header: str = RTF_HEADER, name: str = "doc.rtf"):
        return rtf.convert(self.write(name, (header + body + "}").encode("latin-1")))

    def test_metadata_headings_and_formatting(self) -> None:
        body = (r"\pard\s1 Chapter One\par\pard Plain {\b bold {\i both}} {\ul under}\ulnone  {\strike gone} "
                r"H{\sub 2}O x{\super 2} {\v hidden}\par"
                r"\pard\s1 Chapter Two\par\pard\qc Centered\line next\tab tab\par")
        document = self.convert_rtf(body)
        self.assertEqual(document.title, "Thé Book")
        self.assertEqual(document.authors, ["Jane Doe"])
        self.assertEqual(document.description, "About things")
        self.assertEqual(document.language, "vi")
        self.assertEqual([c.title for c in document.chapters], ["Chapter One", "Chapter Two"])
        first = document.chapters[0].html
        self.assertIn("<strong>bold </strong><strong><em>both</em></strong>", first)
        self.assertIn("<u>under</u>", first)
        self.assertIn("<s>gone</s>", first)
        self.assertIn("H<sub>2</sub>O x<sup>2</sup>", first)
        self.assertNotIn("hidden", first)
        joined = "".join(c.html for c in document.chapters)
        for skipped in ("Running header", "Page footer", "Times New Roman", "Normal", "op", "Default Font"):
            self.assertNotIn(skipped, joined)
        self.assertIn('<p style="text-align: center">Centered<br/>next\u2003tab</p>', document.chapters[1].html)
        self.assert_document_ok(document)

    def test_unicode_and_hex_escapes(self) -> None:
        body = (r"\pard Vi\u7879?t {\uc2\u20013\'3f\'3f}{\uc0\u25991} \u-10179?\u-8704? caf\'e9 "
                r"{\f2\'cf\'f0\'e8\'e2\'e5\'f2} {\f1\'d6\'d0\'ce\'c4} \emdash\~\ldblquote q\rdblquote {\f3\'b7}"
                r" \{braces\}\\\par")
        document = self.convert_rtf(body)
        html = document.chapters[0].html
        self.assertIn("Việt 中文", html)
        self.assertIn("\U0001F600", html)
        self.assertIn("café", html)
        self.assertIn("Привет", html)
        self.assertIn("中文", html)
        self.assertIn("—\u00a0“q”", html)
        self.assertIn("•", html)
        self.assertIn("{braces}\\", html)
        self.assertNotIn("?", html)

    def test_document_codepage_and_mac(self) -> None:
        header = r"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}"
        document = self.convert_rtf(r"\pard \'cf\'f0\'e8\'e2\'e5\'f2\par", header=header)
        self.assertIn("Привет", document.chapters[0].html)
        document = self.convert_rtf(r"\pard caf\'8e\par", header=r"{\rtf1\mac\deff0")
        self.assertIn("café", document.chapters[0].html)

    def test_fields_destinations_and_tables(self) -> None:
        body = (r'\pard Visit {\field{\*\fldinst {HYPERLINK "https://example.com/a" }}{\fldrslt {\ul site}}}'
                r" {\field{\*\fldinst PAGE}{\fldrslt 7}} {\*\bkmkstart b1}{\*\unknowndest secret}"
                r"{\*\generator gen;}\par"
                r"\trowd\cellx1000\cellx2000\pard\intbl A1\cell B1\cell\row"
                r"\trowd\cellx1000\cellx2000\pard\intbl A2\par more\cell {\b B2}\cell\row\pard After\par")
        document = self.convert_rtf(body)
        html = document.chapters[0].html
        self.assertIn('<a href="https://example.com/a"><u>site</u></a>', html)
        self.assertIn("</a> 7</p>", html)
        self.assertNotIn("HYPERLINK", html)
        self.assertNotIn("secret", html)
        self.assertNotIn("gen;", html)
        self.assertIn("<table><tr><td><p>A1</p></td><td><p>B1</p></td></tr>"
                      "<tr><td><p>A2</p><p>more</p></td><td><p><strong>B2</strong></p></td></tr></table>", html)
        self.assertTrue(html.rstrip().endswith("<p>After</p>"))

    def test_pictures_hex_and_bin(self) -> None:
        hex_png = PNG.hex()
        body = (r"\pard Before {\*\shppict{\pict\picw2\pich2\pngblip " + hex_png[:20] + "\n" + hex_png[20:] + "}}"
                r"{\nonshppict{\pict\wmetafile8 0102}}\par"
                r"\pard {\pict\jpegblip\bin" + str(len(JPEG)) + " ")
        tail = r"}\par\pard {\pict\emfblip 0102}{\pict\pngblip 00ff}End\par"
        data = (RTF_HEADER + body).encode("latin-1") + JPEG + (tail + "}").encode("latin-1")
        document = rtf.convert(self.write("pics.rtf", data))
        self.assertEqual([r.media_type for r in document.resources], ["image/png", "image/jpeg"])
        self.assertEqual(document.resources[0].data, PNG)
        self.assertEqual(document.resources[1].data, JPEG)
        html = document.chapters[0].html
        self.assertEqual(html.count("<img"), 2)
        self.assertIn("End", html)
        self.assert_document_ok(document)

    def test_shape_pictures_and_unicode_destinations(self) -> None:
        hex_png = PNG.hex()
        body = (r"\pard {\shp{\*\shpinst\shpleft0{\sp{\sn fillBlip}{\sv {\pict\pngblip " + hex_png + r"}}}"
                r"{\sp{\sn pib}{\sv {\pict\pngblip " + hex_png + r"}}}{\sp{\sn wzName}{\sv Picture 1}}}"
                r"{\shprslt {\pict\wmetafile8 0102}}}"
                r"{\upr{ansi text}{\*\ud{unicode text}}} done\par")
        document = self.convert_rtf(body)
        self.assertEqual(len(document.resources), 1)
        html = document.chapters[0].html
        self.assertEqual(html.count("<img"), 1)
        self.assertIn("unicode text done", html)
        self.assertNotIn("ansi text", html)
        self.assertNotIn("Picture 1", html)

    def test_footnotes_become_notes_chapter(self) -> None:
        body = (r"\pard\s1 Chapter\par\pard Text{\super\chftn}{\footnote\pard{\super\chftn} The note.} more"
                r"{\super\chftn}{\*\footnote\pard{\super\chftn} Second.}\par\pard\s1 Two\par\pard x\par")
        document = self.convert_rtf(body)
        self.assertEqual([c.title for c in document.chapters], ["Chapter", "Two", "Ghi chú"])
        self.assertIn('<a href="#rtf-fn-1"><sup>1</sup></a>', document.chapters[0].html)
        self.assertNotIn("The note.", document.chapters[0].html)
        notes = document.chapters[-1].html
        self.assertIn('id="rtf-fn-1"', notes)
        self.assertIn("The note.", notes)
        self.assertIn('id="rtf-fn-2"', notes)
        self.assert_document_ok(document)

    def test_chapter_detection_without_styles(self) -> None:
        body = r"\pard Chapter 1\par\pard Part of me wants this.\par\pard Some text.\par\pard Chapter 2\par\pard More.\par"
        document = self.convert_rtf(body, header=r"{\rtf1\ansi\deff0")
        self.assertEqual([c.title for c in document.chapters], ["Chapter 1", "Chapter 2"])
        self.assertIn("Part of me wants this.", document.chapters[0].html)

    def test_heading_three_only_is_promoted(self) -> None:
        header = r"{\rtf1\ansi{\stylesheet{\s3 heading 3;}}"
        document = self.convert_rtf(r"\pard\s3 A\par\pard a\par\pard\s3 B\par\pard b\par", header=header)
        self.assertEqual([c.title for c in document.chapters], ["A", "B"])

    def test_invalid_input(self) -> None:
        with self.assertRaises(FormatError):
            rtf.convert(self.write("bad.rtf", b"plain text, not rtf"))
        with self.assertRaises(FormatError):
            rtf.convert(self.write("deep.rtf", b"{\\rtf1 " + b"{" * 5000 + b"x" + b"}" * 5000 + b"}"))
        with self.assertRaises(FormatError):
            rtf.convert(self.write("empty.rtf", b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}}"))


# ---------------------------------------------------------------- FB2 / FBZ

def fb2_document(encoding: str = "utf-8") -> str:
    cover = base64.b64encode(PNG).decode()
    picture = base64.b64encode(JPEG).decode()
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
 <description>
  <title-info>
   <genre>sf</genre>
   <author><first-name>Лев</first-name><middle-name>Николаевич</middle-name><last-name>Толстой</last-name></author>
   <author><nickname>anon</nickname></author>
   <book-title>Война и мир</book-title>
   <annotation><p>Первая строка.</p><p>Вторая <emphasis>строка</emphasis>.</p></annotation>
   <coverpage><image l:href="#cover.png"/></coverpage>
   <lang>ru</lang>
  </title-info>
 </description>
 <body>
  <title><p>Война и мир</p></title>
  <epigraph><p>Эпиграф</p><text-author>Кто-то</text-author></epigraph>
  <section id="part1">
   <title><p>Часть первая</p></title>
   <section id="ch1">
    <title><p>Глава 1</p><p>Начало</p></title>
    <p>Текст с <strong>жирным</strong>, <emphasis>курсивом</emphasis>, <strikethrough>зачёркнутым</strikethrough>,
       H<sub>2</sub>O, x<sup>2</sup>, <code>код</code> и сноской<a l:href="#n1" type="note">[1]</a>.</p>
    <empty-line/>
    <subtitle>Подзаголовок</subtitle>
    <image l:href="#pic.jpg"/>
    <poem><title><p>Стих</p></title><stanza><v>Строка один</v><v>Строка два</v></stanza></poem>
    <cite><p>Цитата</p><text-author>Автор</text-author></cite>
    <table><tr><th>A</th><th colspan="2">B</th></tr><tr><td align="right">1</td><td>2</td></tr></table>
    <p><a l:href="https://example.com">внешняя</a> <a l:href="javascript:alert(1)">плохая</a></p>
   </section>
   <section>
    <p>Без заголовка.</p>
   </section>
  </section>
 </body>
 <body name="notes">
  <title><p>Примечания</p></title>
  <section id="n1"><title><p>1</p></title><p>Текст сноски.</p></section>
 </body>
 <binary id="cover.png" content-type="image/png">{cover}</binary>
 <binary id="pic.jpg" content-type="image/jpeg">
{picture}
 </binary>
</FictionBook>
"""


class Fb2ConverterTests(_Case):
    def test_metadata_cover_sections_and_notes(self) -> None:
        document = fb2.convert(self.write("book.fb2", fb2_document("windows-1251"), "cp1251"))
        self.assertEqual(document.title, "Война и мир")
        self.assertEqual(document.authors, ["Лев Николаевич Толстой", "anon"])
        self.assertEqual(document.language, "ru")
        self.assertEqual(document.description, "Первая строка.\nВторая строка.")
        self.assertIsNotNone(document.cover)
        self.assertEqual(document.cover.data, PNG)
        self.assertIn(document.cover, document.resources)
        titles = [c.title for c in document.chapters]
        self.assertEqual(titles, ["Война и мир", "Часть первая", "Глава 1. Начало", "Phần 4", "Ghi chú"])
        chapter = document.chapters[2].html
        self.assertIn("<strong>жирным</strong>", chapter)
        self.assertIn("<em>курсивом</em>", chapter)
        self.assertIn("<s>зачёркнутым</s>", chapter)
        self.assertIn("H<sub>2</sub>O", chapter)
        self.assertIn("<code>код</code>", chapter)
        self.assertIn('<sup><a href="#n1" class="note">[1]</a></sup>', chapter)
        self.assertIn('<div class="poem">', chapter)
        self.assertIn('<p class="verse">Строка один</p>', chapter)
        self.assertIn('<th colspan="2">B</th>', chapter)
        self.assertIn('<td style="text-align: right">1</td>', chapter)
        self.assertIn('<a href="https://example.com">внешняя</a>', chapter)
        self.assertIn(" плохая", chapter)
        self.assertNotIn("javascript", chapter)
        self.assertIn('<h2 id="ch1">Глава 1<br/>Начало</h2>', chapter)
        pic = next(r for r in document.resources if r.media_type == "image/jpeg")
        self.assertIn(f'<img src="{pic.name}"', chapter)
        self.assertIn("Эпиграф", document.chapters[0].html)
        notes = document.chapters[-1].html
        self.assertIn('<div class="note" id="n1">', notes)
        self.assertIn("Текст сноски.", notes)
        self.assert_document_ok(document)

    def test_fbz_archive(self) -> None:
        path = self.root / "book.fbz"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("book/book.fb2", fb2_document().encode("utf-8"))
        document = fb2.convert(path)
        self.assertEqual(document.source_format, "fbz")
        self.assertEqual(document.title, "Война и мир")
        self.assertEqual(len(document.resources), 2)

    def test_entity_declarations_are_refused(self) -> None:
        bomb = ('<?xml version="1.0"?><!DOCTYPE FictionBook [<!ENTITY a "aaaaaaaaaa">'
                '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><FictionBook><body><p>&b;</p></body></FictionBook>')
        with self.assertRaises(FormatError):
            fb2.convert(self.write("bomb.fb2", bomb))

    def test_lenient_repair_and_minimal_book(self) -> None:
        book = ('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body>'
                '<p>Tom &amp; Jerry&nbsp;&mdash; R&D \x01 &#0;</p><p>Second</p></body></FictionBook>')
        document = fb2.convert(self.write("loose.fb2", book))
        self.assertEqual(len(document.chapters), 1)
        self.assertIn("Tom &amp; Jerry\u00a0— R&amp;D", document.chapters[0].html)
        self.assert_document_ok(document)

    def test_not_fictionbook(self) -> None:
        with self.assertRaises(FormatError):
            fb2.convert(self.write("x.fb2", "<html><body>x</body></html>"))
        with self.assertRaises(FormatError):
            fb2.convert(self.write("broken.fb2", "<FictionBook><body><p>unclosed"))
        path = self.root / "empty.fbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("readme.txt", "nothing")
        with self.assertRaises(FormatError):
            fb2.convert(path)


# ---------------------------------------------------------------- public entry point

class PublicConvertTests(_Case):
    def test_every_extension_end_to_end(self) -> None:
        fbz = self.root / "book.fbz"
        with zipfile.ZipFile(fbz, "w") as archive:
            archive.writestr("book.fb2", fb2_document())
        page = "<html><head><title>Page</title></head><body><h1>A</h1><p>a</p><h1>B</h1><p>b</p></body></html>"
        xhtml = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                 "<head><title>X</title></head><body><p>x</p></body></html>")
        files = {
            "a.txt": ("Chương 1\nmột\nChương 2\nhai\n", "txt", 2),
            "b.md": ("# One\n\none\n\n# Two\n\ntwo\n", "md", 2),
            "c.markdown": ("Just *text*.\n", "markdown", 1),
            "d.html": (page, "html", 2),
            "e.htm": (page, "htm", 2),
            "f.xhtml": (xhtml, "xhtml", 1),
            "g.rtf": (RTF_HEADER + r"\pard\s1 One\par\pard x\par\pard\s1 Two\par\pard y\par}", "rtf", 2),
            "h.fb2": (fb2_document(), "fb2", 5),
        }
        for name, (content, fmt, chapters) in files.items():
            path = self.write(name, content)
            document = formats.convert(path)
            self.assertEqual(document.source_format, fmt, name)
            self.assertEqual(len(document.chapters), chapters, name)
            self.assertTrue(document.title, name)
            self.assert_document_ok(document)
        document = formats.convert(fbz)
        self.assertEqual(document.source_format, "fbz")
        self.assert_document_ok(document)

    def test_rtf_with_wrong_extension_is_sniffed(self) -> None:
        path = self.write("letter.txt", RTF_HEADER + r"\pard Hello\par}")
        document = formats.convert(path)
        self.assertEqual(document.source_format, "rtf")
        self.assertIn("Hello", document.chapters[0].html)


if __name__ == "__main__":
    unittest.main()
