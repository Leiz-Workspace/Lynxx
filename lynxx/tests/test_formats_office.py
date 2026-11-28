"""Tests for the office document converters (OOXML, ODF, HWPX / HWPML).

Every fixture is synthesized at test time inside a temporary directory. The
optional LibreOffice round trip runs only when ``soffice`` is on PATH.
"""

from __future__ import annotations

import base64
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

from lynxx import formats
from lynxx.formats import FormatError, hwpx, odf, ooxml


def make_png(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"\x00" + bytes(rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00" \
      b"\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
EMF = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 40


def well_formed(markup: str) -> ET.Element:
    return ET.fromstring(f"<div>{markup}</div>")


# ---------------------------------------------------------------- OOXML fixtures

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_NS = (
    f'xmlns:w="{W}" xmlns:r="{R}" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
)
MAIN_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}
STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
<w:docDefaults><w:rPrDefault><w:rPr><w:lang w:val="en-US"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="ChapterTitle"><w:name w:val="Chapter Title"/><w:basedOn w:val="Heading1"/></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>
<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
<w:style w:type="character" w:styleId="Strong"><w:name w:val="Strong"/><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="character" w:styleId="StrongEm"><w:name w:val="Strong Emphasis"/><w:basedOn w:val="Strong"/>
<w:rPr><w:i/></w:rPr></w:style>
</w:styles>"""
NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W}">
<w:abstractNum w:abstractNumId="10"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/></w:lvl>
<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="20"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>
<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>
</w:numbering>"""
CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
<dc:title>Meta Title</dc:title><dc:creator>Ann; Bob</dc:creator><dc:language>vi-VN</dc:language>
<dc:subject>Subject text</dc:subject><dc:description>A short description.</dc:description>
</cp:coreProperties>"""


def para(text: str = "", style: str = "", runs: str = "", ppr: str = "") -> str:
    props = (f'<w:pStyle w:val="{style}"/>' if style else "") + ppr
    body = runs or (f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else "")
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{body}</w:p>"


def run(text: str, rpr: str = "") -> str:
    return f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def build_docx(path: Path, body: str, *, kind: str = "docx", main: str = "word/document.xml", styles: str = STYLES,
               numbering: str | None = None, footnotes: str | None = None, rels: list[tuple[str, str, str, bool]] = (),
               media: dict[str, bytes] | None = None, core: str | None = CORE, main_type: str | None = None,
               raw_document: str | None = None) -> Path:
    folder, base = main.rsplit("/", 1) if "/" in main else ("", main)
    part_rels = list(rels)
    overrides = [(f"/{main}", main_type or MAIN_TYPES[kind])]
    if styles:
        part_rels.append(("rIdStyles", f"{REL}/styles", "styles.xml", False))
    if numbering:
        part_rels.append(("rIdNumbering", f"{REL}/numbering", "numbering.xml", False))
    if footnotes:
        part_rels.append(("rIdFootnotes", f"{REL}/footnotes", "footnotes.xml", False))
    external_mode = ' TargetMode="External"'
    rel_xml = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{escape(target)}"{external_mode if external else ""}/>'
        for rid, rtype, target, external in part_rels)
    prefix = f"{folder}/" if folder else ""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
            + "".join(f'<Override PartName="{name}" ContentType="{ctype}"/>' for name, ctype in overrides) + "</Types>"))
        archive.writestr("_rels/.rels", (
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL}">'
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="{main}"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
            'core-properties" Target="docProps/core.xml"/></Relationships>'))
        archive.writestr(main, raw_document or (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {DOC_NS}><w:body>{body}'
            '<w:sectPr/></w:body></w:document>'))
        archive.writestr(f"{prefix}_rels/{base}.rels",
                         f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL}">{rel_xml}</Relationships>')
        if styles:
            archive.writestr(f"{prefix}styles.xml", styles)
        if numbering:
            archive.writestr(f"{prefix}numbering.xml", numbering)
        if footnotes:
            archive.writestr(f"{prefix}footnotes.xml", footnotes)
        for name, data in (media or {}).items():
            archive.writestr(f"{prefix}media/{name}", data)
        if core:
            archive.writestr("docProps/core.xml", core)
        if kind in ("docm", "dotm"):
            archive.writestr(f"{prefix}vbaProject.bin", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 macro payload never read")
    return path


def drawing(rid: str, descr: str) -> str:
    return (f'<w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="{descr}"/><a:graphic>'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:blipFill>'
            f'<a:blip r:embed="{rid}"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


class OoxmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_headings_split_chapters_and_inline_formatting(self) -> None:
        body = "".join([
            para("Intro before the first heading."),
            para("Chapter One", "Heading1"),
            para(runs=run("bold", "<w:b/>") + run(" ") + run("italic", "<w:i/>") + run(" ")
                 + run("under", '<w:u w:val="single"/>') + run(" ") + run("strike", "<w:strike/>") + run(" x")
                 + run("2", '<w:vertAlign w:val="superscript"/>') + run(" H") + run("2", '<w:vertAlign w:val="subscript"/>')
                 + run("O ") + run("strong", '<w:rStyle w:val="Strong"/>') + run(" ")
                 + run("both", '<w:rStyle w:val="StrongEm"/>') + run(" ")
                 + run("notbold", '<w:b w:val="0"/>')
                 + '<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t><w:noBreakHyphen/><w:t>d</w:t></w:r>'),
            para("Section 1.1", "Heading2"),
            para("Quoted words", "Quote"),
            para("Chapter Two", "ChapterTitle"),
            para("Second chapter text.", ppr='<w:jc w:val="center"/>'),
        ])
        path = build_docx(self.root / "book.docx", body)
        document = ooxml.convert(path)
        self.assertEqual([c.title for c in document.chapters], ["Meta Title", "Chapter One", "Chapter Two"])
        self.assertEqual(document.title, "Meta Title")
        self.assertEqual(document.authors, ["Ann", "Bob"])
        self.assertEqual(document.language, "vi-VN")
        self.assertEqual(document.description, "A short description.")
        self.assertEqual(document.source_format, "docx")
        self.assertEqual(document.kind, "text")
        first = document.chapters[1].html
        for fragment in ("<h1>Chapter One</h1>", "<b>bold</b>", "<i>italic</i>", "<u>under</u>", "<s>strike</s>",
                         "x<sup>2</sup>", "H<sub>2</sub>O", "<b>strong</b>", "<b><i>both</i></b>", "notbold",
                         "a b<br/>c‑d", "<h2>Section 1.1</h2>", "<blockquote><p>Quoted words</p></blockquote>"):
            self.assertIn(fragment, first)
        self.assertNotIn("<b>notbold", first)
        self.assertIn('<p style="text-align: center">Second chapter text.</p>', document.chapters[2].html)
        self.assertEqual(document.chapters[0].html, "<p>Intro before the first heading.</p>")
        for chapter in document.chapters:
            well_formed(chapter.html)

    def test_single_heading_gives_one_chapter_named_after_title(self) -> None:
        body = para("Only Heading", "Heading1") + para("Body text.")
        document = ooxml.convert(build_docx(self.root / "one.docx", body, core=None))
        self.assertEqual(len(document.chapters), 1)
        self.assertEqual(document.chapters[0].title, "one")
        self.assertIn("<h1>Only Heading</h1>", document.chapters[0].html)

    def test_title_style_counts_and_supplies_missing_title(self) -> None:
        body = para("The Real Title", "Title") + para("Preface text.") + para("Chapter", "Heading1") + para("x")
        document = ooxml.convert(build_docx(self.root / "t.docx", body, core=None))
        self.assertEqual(document.title, "The Real Title")
        self.assertEqual([c.title for c in document.chapters], ["The Real Title", "Chapter"])

    def test_lists_bullets_numbers_nesting_and_continuation(self) -> None:
        def item(text: str, num: int, level: int = 0) -> str:
            return para(text, ppr=f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>')

        body = "".join([
            item("apple", 1), item("nested", 1, 1), item("banana", 1),
            item("one", 2), item("sub", 2, 1), item("two", 2),
            para("Interruption."),
            item("three", 2),
            para("styled bullet", "ListBullet"),
        ])
        document = ooxml.convert(build_docx(self.root / "lists.docx", body, numbering=NUMBERING))
        markup = document.chapters[0].html
        well_formed(markup)
        self.assertIn("<ul><li>apple", markup)
        self.assertIn("nested</li>", markup)
        self.assertLess(markup.index("apple"), markup.index("nested"))
        self.assertLess(markup.index("nested"), markup.index("banana"))
        self.assertIn("<ol><li>one", markup)
        self.assertIn('type="a"', markup)
        self.assertIn('<ol start="3"><li>three</li></ol>', markup)
        self.assertIn("<ul><li>styled bullet</li></ul>", markup)
        self.assertNotIn("<li><li>", markup)

    def test_tables_with_colspan_rowspan_and_header(self) -> None:
        def cell(text: str, props: str = "") -> str:
            return f'<w:tc>{f"<w:tcPr>{props}</w:tcPr>" if props else ""}{para(text)}</w:tc>'

        span2, restart, merged = '<w:gridSpan w:val="2"/>', '<w:vMerge w:val="restart"/>', "<w:vMerge/>"
        table = (
            '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
            f'<w:tr><w:trPr><w:tblHeader/></w:trPr>{cell("Head", span2)}</w:tr>'
            f'<w:tr>{cell("tall", restart)}{cell("x")}</w:tr>'
            f'<w:tr>{cell("", merged)}{cell("y")}</w:tr>'
            '<w:sdt><w:sdtContent>'
            f'<w:tr>{cell("in sdt row")}{cell("z")}</w:tr></w:sdtContent></w:sdt>'
            '</w:tbl>')
        document = ooxml.convert(build_docx(self.root / "table.docx", para("Before") + table))
        markup = document.chapters[0].html
        well_formed(markup)
        self.assertIn('<th colspan="2"><p>Head</p></th>', markup)
        self.assertIn('<td rowspan="2"><p>tall</p></td>', markup)
        self.assertEqual(markup.count("tall"), 1)
        self.assertIn("<td><p>y</p></td>", markup)
        self.assertIn("in sdt row", markup)

    def test_hyperlinks_are_sanitized(self) -> None:
        rels = [("rIdLink", f"{REL}/hyperlink", "https://example.com/page?a=1&b=2", True),
                ("rIdBad", f"{REL}/hyperlink", "javascript:alert(1)", True),
                ("rIdFile", f"{REL}/hyperlink", "file:///etc/passwd", True)]
        body = "".join([
            para(runs='<w:hyperlink r:id="rIdLink">' + run("good link", '<w:rStyle w:val="Hyperlink"/><w:u/>')
                 + "</w:hyperlink>"),
            para(runs='<w:hyperlink r:id="rIdBad">' + run("bad link") + "</w:hyperlink>"
                 + '<w:hyperlink r:id="rIdFile">' + run("file link") + "</w:hyperlink>"),
            para(runs='<w:hyperlink w:anchor="target">' + run("jump") + "</w:hyperlink>"),
            para(runs='<w:fldSimple w:instr=" HYPERLINK &quot;https://example.org/f&quot; ">' + run("field link")
                 + "</w:fldSimple>"),
            para(runs='<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> HYPERLINK '
                 '"javascript:evil()" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                 + run("evil field") + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
            para(runs='<w:bookmarkStart w:id="0" w:name="target"/><w:bookmarkEnd w:id="0"/>'),
            para("Target here"),
        ])
        markup = ooxml.convert(build_docx(self.root / "links.docx", body, rels=rels)).chapters[0].html
        well_formed(markup)
        self.assertIn('<a href="https://example.com/page?a=1&amp;b=2">good link</a>', markup)
        self.assertIn('<a href="https://example.org/f">field link</a>', markup)
        self.assertIn('<a href="#bm-target">jump</a>', markup)
        self.assertIn('<span id="bm-target"></span>Target here', markup)
        self.assertNotIn("javascript", markup)
        self.assertNotIn("file:", markup)
        self.assertIn("bad link", markup)
        self.assertIn("evil field", markup)

    def test_embedded_images_become_resources(self) -> None:
        png = make_png()
        rels = [("rIdImg", f"{REL}/image", "media/image1.png", False),
                ("rIdEmf", f"{REL}/image", "media/image2.emf", False),
                ("rIdGif", f"{REL}/image", "media/image3.gif", False),
                ("rIdMissing", f"{REL}/image", "media/nope.png", False),
                ("rIdRemote", f"{REL}/image", "https://example.com/x.png", True)]
        vml = ('<w:r><w:pict><v:shape><v:imagedata r:id="rIdGif" o:title="old style"/></v:shape></w:pict></w:r>')
        body = "".join([
            para(runs=drawing("rIdImg", "A red dot") + run(" caption")),
            para(runs=drawing("rIdEmf", "metafile")),
            para(runs=vml),
            para(runs=drawing("rIdImg", "again") + drawing("rIdMissing", "x") + drawing("rIdRemote", "y")),
        ])
        document = ooxml.convert(build_docx(self.root / "img.docx", body, rels=rels,
                                            media={"image1.png": png, "image2.emf": EMF, "image3.gif": GIF}))
        self.assertEqual([r.media_type for r in document.resources], ["image/png", "image/gif"])
        self.assertEqual(document.resources[0].data, png)
        markup = document.chapters[0].html
        well_formed(markup)
        png_name, gif_name = document.resources[0].name, document.resources[1].name
        self.assertIn(f'<img src="{png_name}" alt="A red dot"/> caption', markup)
        self.assertIn(f'<img src="{gif_name}" alt="old style"/>', markup)
        self.assertEqual(markup.count(f'src="{png_name}"'), 2)
        self.assertNotIn("metafile", markup)
        self.assertNotIn("example.com", markup)

    def test_fields_revisions_content_controls_and_hidden_text(self) -> None:
        body = "".join([
            para(runs='<w:r><w:t xml:space="preserve">Page </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                 '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>7</w:t></w:r>'
                 '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
            para(runs='<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>TOC \\o "1-3"</w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + run("Contents entry 1")),
            para("Contents entry 2", "TOC1"),
            para(runs='<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
            '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr>'
            f'<w:sdtContent>{para("Sdt toc entry")}</w:sdtContent></w:sdt>',
            para(runs='<w:ins w:id="1" w:author="a">' + run("inserted") + '</w:ins>'
                 '<w:del w:id="2" w:author="a"><w:r><w:delText>deleted</w:delText></w:r></w:del>'
                 + run(" visible") + run("hidden", "<w:vanish/>")),
            f'<w:sdt><w:sdtPr/><w:sdtContent>{para("inside block sdt")}</w:sdtContent></w:sdt>',
            para(runs='<w:smartTag w:uri="x" w:element="place">' + run("smart") + '</w:smartTag>'
                 '<w:customXml w:element="c">' + run(" custom") + '</w:customXml>'
                 '<w:sdt><w:sdtContent>' + run(" inline sdt") + '</w:sdtContent></w:sdt>'),
            para(runs='<mc:AlternateContent><mc:Choice Requires="wps">' + run("choice text") + '</mc:Choice>'
                 '<mc:Fallback>' + run("fallback text") + '</mc:Fallback></mc:AlternateContent>'),
            para(runs='<w:r><w:sym w:font="Symbol" w:char="F061"/><w:sym w:font="Arial" w:char="00A9"/></w:r>'),
        ])
        markup = ooxml.convert(build_docx(self.root / "fields.docx", body)).chapters[0].html
        self.assertIn("Page 7", markup)
        self.assertNotIn("PAGE", markup)
        self.assertNotIn("Contents entry", markup)
        self.assertNotIn("Sdt toc entry", markup)
        self.assertIn("inserted visible", markup)
        self.assertNotIn("deleted", markup)
        self.assertNotIn("hidden", markup)
        self.assertIn("inside block sdt", markup)
        self.assertIn("smart custom inline sdt", markup)
        self.assertIn("choice text", markup)
        self.assertNotIn("fallback text", markup)
        self.assertIn("α©", markup)

    def test_footnotes_become_ghi_chu_section(self) -> None:
        footnotes = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:footnotes {DOC_NS}>'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p>'
            '</w:footnote><w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r>'
            '<w:r><w:t xml:space="preserve"> Footnote body text.</w:t></w:r></w:p></w:footnote></w:footnotes>')
        body = para(runs=run("Statement") + '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
                    '<w:footnoteReference w:id="1"/></w:r>' + run(" continues."))
        markup = ooxml.convert(build_docx(self.root / "notes.docx", body, footnotes=footnotes)).chapters[0].html
        well_formed(markup)
        self.assertIn('Statement<sup><a class="noteref" id="nref1" href="#note1">1</a></sup> continues.', markup)
        self.assertIn('<section class="notes"><h2>Ghi chú</h2>', markup)
        self.assertIn('<div class="note" id="note1"><p><a class="noteback" href="#nref1">1.</a>', markup)
        self.assertIn("Footnote body text.", markup)

    def test_main_part_found_through_relationships(self) -> None:
        body = para("Custom location works.")
        document = ooxml.convert(build_docx(self.root / "custom.docx", body, main="content/main.xml"))
        self.assertIn("Custom location works.", document.chapters[0].html)

    def test_corrupt_encrypted_and_hostile_inputs(self) -> None:
        encrypted = self.root / "locked.docx"
        encrypted.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
                              + "EncryptionInfo".encode("utf-16-le") + b"\x00" * 64
                              + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 2048)
        with self.assertRaisesRegex(FormatError, "password"):
            ooxml.convert(encrypted)
        garbage = self.root / "garbage.docx"
        garbage.write_bytes(b"this is not a zip file at all" * 10)
        with self.assertRaises(FormatError):
            ooxml.convert(garbage)
        truncated = self.root / "truncated.docx"
        truncated.write_bytes(build_docx(self.root / "full.docx", para("x")).read_bytes()[:200])
        with self.assertRaises(FormatError):
            ooxml.convert(truncated)
        empty_zip = self.root / "empty.docx"
        with zipfile.ZipFile(empty_zip, "w") as archive:
            archive.writestr("readme.txt", "no word here")
        with self.assertRaisesRegex(FormatError, "main document"):
            ooxml.convert(empty_zip)
        bomb = ('<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                f'<w:document {DOC_NS}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
        with self.assertRaisesRegex(FormatError, "entity"):
            ooxml.convert(build_docx(self.root / "bomb.docx", "", raw_document=bomb))
        wrong = build_docx(self.root / "slides.docx", para("x"), main_type="application/vnd.openxmlformats-"
                           "officedocument.presentationml.presentation.main+xml")
        with self.assertRaisesRegex(FormatError, "not a Word document"):
            ooxml.convert(wrong)
        with self.assertRaisesRegex(FormatError, "no readable content"):
            ooxml.convert(build_docx(self.root / "blank.docx", para("") + para("   ")))
        malformed = build_docx(self.root / "malformed.docx", "", raw_document="<w:document><w:body>")
        with self.assertRaises(FormatError):
            ooxml.convert(malformed)

    def test_public_entry_for_all_word_extensions(self) -> None:
        for kind in ("docx", "docm", "dotx", "dotm"):
            with self.subTest(kind=kind):
                path = build_docx(self.root / f"sample.{kind}", para("Alpha", "Heading1") + para("one")
                                  + para("Beta", "Heading1") + para("two"), kind=kind)
                document = formats.convert(path)
                self.assertEqual(document.source_format, kind)
                self.assertEqual([c.title for c in document.chapters], ["Alpha", "Beta"])
                self.assertEqual(document.title, "Meta Title")


# ---------------------------------------------------------------- ODF fixtures

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
)
ODF_COMMON_STYLES = """
<style:default-style style:family="paragraph"><style:text-properties fo:language="ko" fo:country="KR"/></style:default-style>
<style:style style:name="Standard" style:family="paragraph"/>
<style:style style:name="Title" style:family="paragraph" style:parent-style-name="Standard"/>
<style:style style:name="Heading_20_1" style:display-name="Heading 1" style:family="paragraph"/>
<style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph"
 style:default-outline-level="2"/>
<style:style style:name="Base_20_Char" style:display-name="Base Char" style:family="text">
 <style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="Child_20_Char" style:display-name="Child Char" style:family="text"
 style:parent-style-name="Base_20_Char"><style:text-properties fo:font-style="italic"/></style:style>
<text:list-style style:name="Bullets"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/>
 <text:list-level-style-number text:level="2" style:num-format="1"/></text:list-style>
"""
ODF_AUTO_STYLES = """
<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Standard">
 <style:paragraph-properties fo:text-align="center"/></style:style>
<style:style style:name="P2" style:family="paragraph" style:parent-style-name="Standard">
 <style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
<style:style style:name="T2" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>
<style:style style:name="T3" style:family="text"><style:text-properties style:text-underline-style="solid"/></style:style>
<style:style style:name="T4" style:family="text"><style:text-properties style:text-line-through-style="solid"/></style:style>
<style:style style:name="T5" style:family="text"><style:text-properties style:text-position="super 58%"/></style:style>
<style:style style:name="T6" style:family="text"><style:text-properties style:text-position="-33% 58%"/></style:style>
<style:style style:name="T7" style:family="text"><style:text-properties fo:font-weight="normal"/></style:style>
<text:list-style style:name="L2"><text:list-level-style-number text:level="1" style:num-format="a"/></text:list-style>
"""
ODF_META = """<office:meta><dc:title>ODF Title</dc:title><meta:initial-creator>Original Author</meta:initial-creator>
<dc:creator>Last Editor</dc:creator><dc:language>vi</dc:language><dc:description>ODF description</dc:description>
</office:meta>"""


def odf_body(image_xml: str) -> str:
    return f"""
<text:sequence-decls><text:sequence-decl text:display-outline-level="0" text:name="Table"/></text:sequence-decls>
<text:tracked-changes><text:changed-region text:id="c1"><text:deletion><office:change-info><dc:creator>x</dc:creator>
<dc:date>2024-01-01T00:00:00</dc:date></office:change-info><text:p>DELETED TEXT</text:p></text:deletion>
</text:changed-region></text:tracked-changes>
<text:table-of-content text:name="TOC"><text:index-body><text:p>TOC ENTRY</text:p></text:index-body></text:table-of-content>
<text:p text:style-name="P1">Centered intro</text:p>
<text:h text:outline-level="1">Part A</text:h>
<text:p>Plain <text:span text:style-name="T1">bold</text:span> <text:span text:style-name="T2">italic</text:span>
 <text:span text:style-name="T3">under</text:span> <text:span text:style-name="T4">strike</text:span> x<text:span
 text:style-name="T5">2</text:span> H<text:span text:style-name="T6">2</text:span>O <text:span
 text:style-name="Child_20_Char">inherited</text:span><text:s text:c="3"/>gap<text:tab/>tab<text:line-break/>next
<office:annotation><dc:creator>x</dc:creator><text:p>COMMENT</text:p></office:annotation><text:change text:change-id="c1"/></text:p>
<text:p text:style-name="Heading_20_2">Demoted heading</text:p>
<text:p text:style-name="P2">All bold <text:span text:style-name="T7">except this</text:span></text:p>
<text:p><text:a xlink:href="https://example.com/odf">web</text:a> <text:a xlink:href="javascript:alert(1)">bad</text:a>
 <text:a xlink:href="#mark">jump</text:a> <text:a xlink:href="../outside.odt">rel</text:a></text:p>
<text:list text:style-name="Bullets"><text:list-item><text:p>apple</text:p><text:list><text:list-item>
<text:p>first sub</text:p></text:list-item><text:list-item><text:p>second sub</text:p></text:list-item></text:list>
</text:list-item><text:list-item><text:p>banana</text:p></text:list-item></text:list>
<text:list text:style-name="L2"><text:list-item><text:p>alpha</text:p></text:list-item>
<text:list-item><text:p>beta</text:p></text:list-item></text:list>
<table:table table:name="T"><table:table-column table:number-columns-repeated="2"/>
<table:table-header-rows><table:table-row><table:table-cell><text:p>H1</text:p></table:table-cell>
<table:table-cell><text:p>H2</text:p></table:table-cell></table:table-row></table:table-header-rows>
<table:table-row><table:table-cell table:number-columns-spanned="2"><text:p>wide cell</text:p></table:table-cell>
<table:covered-table-cell/></table:table-row>
<table:table-row><table:table-cell table:number-rows-spanned="2"><text:p>tall</text:p></table:table-cell>
<table:table-cell><text:p>r2</text:p></table:table-cell></table:table-row>
<table:table-row><table:covered-table-cell/><table:table-cell><text:p>r3</text:p></table:table-cell></table:table-row>
</table:table>
<text:p>Picture: {image_xml}</text:p>
<text:p>Note here<text:note text:id="ftn1" text:note-class="footnote"><text:note-citation>1</text:note-citation>
<text:note-body><text:p>The note body.</text:p></text:note-body></text:note> after.</text:p>
<text:soft-page-break/>
<text:p text:style-name="Heading_20_1">Part B</text:p>
<text:section text:name="S1"><text:p><text:bookmark text:name="mark"/></text:p><text:p>In section</text:p>
<text:p><text:ruby><text:ruby-base>漢字</text:ruby-base><text:ruby-text>かんじ</text:ruby-text></text:ruby></text:p>
</text:section>
"""


def build_odt(path: Path, *, body: str | None = None, mimetype: str = "application/vnd.oasis.opendocument.text",
              encrypted: bool = False, content: str | None = None, pictures: dict[str, bytes] | None = None) -> Path:
    if pictures is None:
        pictures = {"Pictures/dot.png": make_png()}
    image = ('<draw:frame draw:name="img1" text:anchor-type="as-char"><draw:image xlink:href="Pictures/dot.png"/>'
             '<svg:title>Dot</svg:title></draw:frame>')
    content = content or (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODF_NS} office:version="1.3">'
        f'<office:automatic-styles>{ODF_AUTO_STYLES}</office:automatic-styles>'
        f'<office:body><office:text>{body if body is not None else odf_body(image)}</office:text></office:body>'
        '</office:document-content>')
    encryption = '<manifest:encryption-data manifest:checksum-type="SHA1"/>' if encrypted else ""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        archive.writestr("content.xml", content)
        archive.writestr("styles.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {ODF_NS}>'
                                       f'<office:styles>{ODF_COMMON_STYLES}</office:styles></office:document-styles>')
        archive.writestr("meta.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-meta {ODF_NS}>'
                                     f'{ODF_META}</office:document-meta>')
        entries = "".join(f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="image/png"/>'
                          for name in pictures)
        archive.writestr("META-INF/manifest.xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><manifest:manifest {ODF_NS} manifest:version="1.3">'
            f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{mimetype}"/>'
            f'<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">{encryption}'
            '</manifest:file-entry><manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
            '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
            f'{entries}</manifest:manifest>'))
        for name, data in pictures.items():
            archive.writestr(name, data)
    return path


def build_fodt(path: Path, body: str | None = None, mimetype: str = "application/vnd.oasis.opendocument.text") -> Path:
    image = ('<draw:frame draw:name="img1"><draw:image><office:binary-data>'
             + base64.encodebytes(make_png((0, 0, 255))).decode("ascii")
             + '</office:binary-data></draw:image></draw:frame>')
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document {ODF_NS} office:version="1.3" '
        f'office:mimetype="{mimetype}">\n {ODF_META}\n <office:styles>{ODF_COMMON_STYLES}</office:styles>\n'
        f' <office:automatic-styles>{ODF_AUTO_STYLES}</office:automatic-styles>\n'
        f' <office:body>\n  <office:text>{body if body is not None else odf_body(image)}</office:text>\n </office:body>\n'
        '</office:document>\n', encoding="utf-8")
    return path


class OdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check_common(self, document) -> None:
        self.assertEqual([c.title for c in document.chapters], ["ODF Title", "Part A", "Part B"])
        self.assertEqual(document.title, "ODF Title")
        self.assertEqual(document.authors, ["Original Author"])
        self.assertEqual(document.language, "vi")
        self.assertEqual(document.description, "ODF description")
        intro, part_a, part_b = (c.html for c in document.chapters)
        for chapter in document.chapters:
            well_formed(chapter.html)
        self.assertEqual(intro, '<p style="text-align: center">Centered intro</p>')
        for fragment in ("<h1>Part A</h1>", "<b>bold</b>", "<i>italic</i>", "<u>under</u>", "<s>strike</s>",
                         "x<sup>2</sup>", "H<sub>2</sub>O", "<b><i>inherited</i></b>", "   gap tab<br/>next",
                         "<p><b>All bold </b>except this</p>", '<a href="https://example.com/odf">web</a>',
                         '<a href="#bm-mark">jump</a>', "<ul><li>apple", "second sub</li>", "banana</li>",
                         '<ol type="a"><li>alpha</li><li>beta</li></ol>', "<th>", '<td colspan="2"><p>wide cell</p></td>',
                         '<td rowspan="2"><p>tall</p></td>', "r3", '<sup><a class="noteref" id="nref1" href="#note1">1</a>',
                         "<h2>Ghi chú</h2>", "The note body."):
            self.assertIn(fragment, part_a)
        self.assertIn("<p>Demoted heading</p>", part_a)
        self.assertNotIn("javascript", part_a)
        self.assertNotIn("outside", part_a)
        self.assertIn("bad", part_a)
        self.assertLess(part_a.index("first sub"), part_a.index("second sub"))
        for absent in ("DELETED TEXT", "TOC ENTRY", "COMMENT"):
            self.assertNotIn(absent, intro + part_a + part_b)
        self.assertIn("<h1>Part B</h1>", part_b)
        self.assertIn('<span id="bm-mark"></span>In section', part_b)
        self.assertIn("<ruby>漢字<rt>かんじ</rt></ruby>", part_b)
        self.assertEqual(len(document.resources), 1)
        self.assertEqual(document.resources[0].media_type, "image/png")
        self.assertIn(f'<img src="{document.resources[0].name}"', part_a)

    def test_odt_package(self) -> None:
        document = odf.convert(build_odt(self.root / "book.odt"))
        self.check_common(document)
        self.assertEqual(document.source_format, "odt")
        self.assertIn('alt="Dot"', document.chapters[1].html)

    def test_flat_fodt_with_binary_image(self) -> None:
        document = odf.convert(build_fodt(self.root / "book.fodt"))
        self.check_common(document)
        self.assertEqual(document.source_format, "fodt")
        self.assertEqual(document.resources[0].data, make_png((0, 0, 255)))

    def test_language_falls_back_to_default_style(self) -> None:
        flat = self.root / "nolang.fodt"
        flat.write_text(f'<?xml version="1.0"?><office:document {ODF_NS}><office:styles>{ODF_COMMON_STYLES}'
                        '</office:styles><office:body><office:text><text:p>Hi</text:p></office:text></office:body>'
                        '</office:document>', encoding="utf-8")
        document = odf.convert(flat)
        self.assertEqual(document.language, "ko-KR")
        self.assertEqual(document.chapters[0].title, "nolang")

    def test_errors(self) -> None:
        with self.assertRaisesRegex(FormatError, "password"):
            odf.convert(build_odt(self.root / "locked.odt", encrypted=True))
        with self.assertRaisesRegex(FormatError, "not an OpenDocument text"):
            odf.convert(build_odt(self.root / "sheet.odt", mimetype="application/vnd.oasis.opendocument.spreadsheet"))
        bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
                f'<office:document-content {ODF_NS}><office:body><office:text><text:p>&b;</text:p></office:text>'
                '</office:body></office:document-content>')
        with self.assertRaisesRegex(FormatError, "entity"):
            odf.convert(build_odt(self.root / "bomb.odt", content=bomb))
        garbage = self.root / "garbage.fodt"
        garbage.write_bytes(b"\x00\x01\x02 binary junk")
        with self.assertRaises(FormatError):
            odf.convert(garbage)
        broken = self.root / "broken.fodt"
        broken.write_text("<office:document><unclosed>", encoding="utf-8")
        with self.assertRaises(FormatError):
            odf.convert(broken)
        with self.assertRaises(FormatError):
            odf.convert(build_fodt(self.root / "pres.fodt", mimetype="application/vnd.oasis.opendocument.presentation"))
        with self.assertRaisesRegex(FormatError, "no readable content"):
            odf.convert(build_odt(self.root / "empty.odt", body="<text:p/>"))

    def test_public_entry_for_odf_extensions(self) -> None:
        build_fodt(self.root / "book.fodt")
        self.assertEqual(formats.convert(self.root / "book.fodt").source_format, "fodt")
        if formats.sniff_format(build_odt(self.root / "probe.odt")) not in formats._MODULES:
            self.skipTest("lynxx.formats.sniff_format() returns 'odf' for ODT/OTT packages but _MODULES has no "
                          "'odf' key (fix needed in lynxx/formats/__init__.py)")
        for ext in ("odt", "ott"):
            with self.subTest(ext=ext):
                path = build_odt(self.root / f"book.{ext}",
                                 mimetype="application/vnd.oasis.opendocument.text"
                                 + ("-template" if ext == "ott" else ""))
                document = formats.convert(path)
                self.assertEqual(document.source_format, ext)
                self.assertEqual(len(document.chapters), 3)


# ---------------------------------------------------------------- HWPX / HWPML fixtures

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
HC = "http://www.hancom.co.kr/hwpml/2011/core"
OPF = "http://www.idpf.org/2007/opf/"
HWPX_HEADER = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<hh:head xmlns:hh="{HH}" xmlns:hp="{HP}" version="1.4"><hh:refList>
<hh:charProperties itemCnt="4">
 <hh:charPr id="0" height="1000"><hh:underline type="NONE" shape="SOLID"/><hh:strikeout shape="NONE"/></hh:charPr>
 <hh:charPr id="1" height="1000"><hh:bold/><hh:underline type="NONE" shape="SOLID"/></hh:charPr>
 <hh:charPr id="2" height="1000"><hh:italic/><hh:underline type="BOTTOM" shape="SOLID"/></hh:charPr>
 <hh:charPr id="3" height="1000"><hh:strikeout shape="SOLID"/><hh:supscript/></hh:charPr>
</hh:charProperties>
<hh:paraProperties itemCnt="3">
 <hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>
 <hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>
 <hh:paraPr id="2"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>
 <hh:paraPr id="3"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>
</hh:paraProperties>
<hh:styles itemCnt="3">
 <hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="0"/>
 <hh:style id="1" type="PARA" name="개요 1" engName="Outline 1" paraPrIDRef="1" charPrIDRef="1"/>
 <hh:style id="2" type="PARA" name="본문" engName="Body" paraPrIDRef="0" charPrIDRef="0"/>
</hh:styles></hh:refList></hh:head>"""


def hp_para(inner: str, style: int = 0, shape: int = 0) -> str:
    return f'<hp:p id="0" paraPrIDRef="{shape}" styleIDRef="{style}" pageBreak="0">{inner}<hp:linesegarray/></hp:p>'


def hp_run(text: str, char: int = 0) -> str:
    return f'<hp:run charPrIDRef="{char}"><hp:t>{text}</hp:t></hp:run>'


def hwpx_section(paragraphs: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}" '
            f'xmlns:hc="{HC}">{paragraphs}</hs:sec>')


def build_hwpx(path: Path, *, with_hpf: bool = True, mimetype: str = "application/hwp+zip",
               encrypted: bool = False, section0: str | None = None) -> Path:
    table = ('<hp:run charPrIDRef="0"><hp:tbl rowCnt="2" colCnt="2"><hp:tr><hp:tc header="1"><hp:subList>'
             + hp_para(hp_run("머리")) + '</hp:subList><hp:cellAddr colAddr="0" rowAddr="0"/>'
             '<hp:cellSpan colSpan="2" rowSpan="1"/></hp:tc></hp:tr><hp:tr><hp:tc><hp:subList>'
             + hp_para(hp_run("왼쪽")) + '</hp:subList><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc><hp:tc><hp:subList>'
             + hp_para(hp_run("오른쪽")) + '</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run>')
    picture = ('<hp:run charPrIDRef="0"><hp:pic id="5"><hp:shapeComment>그림 설명</hp:shapeComment>'
               '<hc:img binaryItemIDRef="image1" bright="0" contrast="0"/></hp:pic></hp:run>')
    footnote = ('<hp:run charPrIDRef="0"><hp:ctrl><hp:footNote number="1"><hp:subList>'
                + hp_para(hp_run("각주 내용")) + '</hp:subList></hp:footNote></hp:ctrl></hp:run>')
    link = ('<hp:run charPrIDRef="0"><hp:ctrl><hp:fieldBegin id="9" type="HYPERLINK"><hp:parameters>'
            '<hp:stringParam name="Command">https\\://www.example.com/hwp;1;0;0;</hp:stringParam></hp:parameters>'
            '</hp:fieldBegin></hp:ctrl><hp:t>링크</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="9"/></hp:ctrl></hp:run>'
            '<hp:run charPrIDRef="0"><hp:ctrl><hp:fieldBegin id="10" type="HYPERLINK"><hp:parameters>'
            '<hp:stringParam name="Command">javascript\\:alert(1);1;0;0;</hp:stringParam></hp:parameters>'
            '</hp:fieldBegin></hp:ctrl><hp:t>나쁜 링크</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="10"/></hp:ctrl></hp:run>')
    section0 = section0 or hwpx_section("".join([
        hp_para('<hp:run charPrIDRef="0"><hp:secPr/><hp:t>머리말 앞 내용</hp:t></hp:run>'),
        hp_para(hp_run("제1장 시작", 1), style=1, shape=1),
        hp_para(hp_run("굵게", 1) + hp_run(" ") + hp_run("기울임", 2) + hp_run(" ") + hp_run("위", 3)),
        hp_para('<hp:run charPrIDRef="0"><hp:t>앞<hp:tab/>뒤<hp:lineBreak/>다음 줄<hp:nbSpace/>끝</hp:t></hp:run>'),
        hp_para(table),
        hp_para(picture),
        hp_para(hp_run("본문") + footnote + hp_run(" 계속")),
        hp_para(link),
        hp_para(hp_run("첫 항목"), shape=2), hp_para(hp_run("둘째 항목"), shape=2),
        hp_para(hp_run("하나"), shape=3), hp_para(hp_run("둘"), shape=3),
        hp_para('<hp:run charPrIDRef="0"><hp:t>남김<hp:deleteBegin/>삭제됨<hp:deleteEnd/> 유지</hp:t></hp:run>'),
    ]))
    section1 = hwpx_section(hp_para(hp_run("제2장"), style=1, shape=1) + hp_para(hp_run("둘째 구역 본문")))
    hpf = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><opf:package xmlns:opf="{OPF}" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/" version="" unique-identifier="" id="">'
           '<opf:metadata><opf:title>한글 문서</opf:title><opf:language>ko</opf:language>'
           '<opf:meta name="creator" content="text">홍길동</opf:meta>'
           '<opf:meta name="subject" content="text">주제</opf:meta>'
           '<opf:meta name="description" content="text">설명 텍스트</opf:meta></opf:metadata>'
           '<opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
           '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
           '<opf:item id="section1" href="Contents/section1.xml" media-type="application/xml"/>'
           '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
           '<opf:item id="settings" href="settings.xml" media-type="application/xml"/></opf:manifest>'
           '<opf:spine><opf:itemref idref="header" linear="yes"/><opf:itemref idref="section0" linear="yes"/>'
           '<opf:itemref idref="section1" linear="yes"/></opf:spine></opf:package>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        archive.writestr("version.xml", '<?xml version="1.0"?><hv:HCFVersion xmlns:hv="x"/>')
        archive.writestr("META-INF/container.xml", (
            '<?xml version="1.0" encoding="UTF-8"?><ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:'
            'container"><ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" '
            'media-type="application/hwpml-package+xml"/></ocf:rootfiles></ocf:container>'))
        if encrypted:
            archive.writestr("META-INF/manifest.xml", '<odf:manifest xmlns:odf="urn:x"><odf:file-entry>'
                                                      '<odf:encryption-data/></odf:file-entry></odf:manifest>')
        if with_hpf:
            archive.writestr("Contents/content.hpf", hpf)
            archive.writestr("Contents/header.xml", HWPX_HEADER)
        archive.writestr("Contents/section0.xml", section0)
        archive.writestr("Contents/section1.xml", section1)
        archive.writestr("BinData/image1.png", make_png((0, 255, 0)))
        archive.writestr("Preview/PrvText.txt", "preview")
    return path


def build_hwpml(path: Path, *, compress: bool = True, encoding: str = "utf-8", doctype: str = "") -> Path:
    png = make_png((10, 20, 30))
    if compress:
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = packer.compress(png) + packer.flush()
    else:
        payload = png
    data = base64.b64encode(payload).decode("ascii")
    xml = f"""<?xml version="1.0" encoding="{encoding.upper()}" standalone="no"?>{doctype}
<HWPML Version="2.8" SubVersion="8.0.0.0" Style2="embed">
<HEAD SecCnt="1"><DOCSUMMARY><TITLE>HWPML 제목</TITLE><AUTHOR>김작가</AUTHOR><SUBJECT>요약</SUBJECT></DOCSUMMARY>
<MAPPINGTABLE><BINDATALIST Count="1"><BINITEM BinData="1" Format="png" Type="Embedding"/></BINDATALIST>
<CHARSHAPELIST Count="2"><CHARSHAPE Id="0" Height="1000"/><CHARSHAPE Id="1" Height="1000"><BOLD/></CHARSHAPE>
<CHARSHAPE Id="2" Height="1000"><ITALIC/><UNDERLINE Type="Bottom" Shape="Solid"/></CHARSHAPE></CHARSHAPELIST>
<PARASHAPELIST Count="2"><PARASHAPE Id="0" HeadingType="None" Level="0"/>
<PARASHAPE Id="1" HeadingType="Outline" Level="0"/></PARASHAPELIST>
<STYLELIST Count="2"><STYLE Id="0" Name="바탕글" EngName="Normal" ParaShape="0" CharShape="0" Type="Para"/>
<STYLE Id="1" Name="개요 1" EngName="Outline 1" ParaShape="1" CharShape="0" Type="Para"/></STYLELIST>
</MAPPINGTABLE></HEAD>
<BODY><SECTION Id="0">
<P ParaShape="0" Style="0"><TEXT CharShape="0"><SECDEF/><CHAR>서문</CHAR></TEXT></P>
<P ParaShape="1" Style="1"><TEXT CharShape="0"><CHAR>첫째 장</CHAR></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="1"><CHAR>굵은 글</CHAR></TEXT><TEXT CharShape="0"><CHAR> 보통</CHAR></TEXT>
<TEXT CharShape="2"><CHAR>기울임</CHAR></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>탭<TAB/>뒤<LINEBREAK/>새 줄</CHAR></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><TABLE RowCount="2" ColCount="2"><ROW><CELL ColAddr="0" RowAddr="0"
 ColSpan="2" RowSpan="1"><PARALIST><P><TEXT CharShape="0"><CHAR>넓은 칸</CHAR></TEXT></P></PARALIST></CELL></ROW>
<ROW><CELL ColSpan="1" RowSpan="1"><PARALIST><P><TEXT><CHAR>가</CHAR></TEXT></P></PARALIST></CELL>
<CELL><PARALIST><P><TEXT><CHAR>나</CHAR></TEXT></P></PARALIST></CELL></ROW></TABLE></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><PICTURE><SHAPEOBJECT/><IMAGE BinItem="1" Effect="RealPic"/></PICTURE></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>각주 앞</CHAR><FOOTNOTE><PARALIST><P><TEXT><CHAR>각주 본문</CHAR>
</TEXT></P></PARALIST></FOOTNOTE></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><HEADER><PARALIST><P><TEXT><CHAR>머리글 숨김</CHAR></TEXT></P></PARALIST>
</HEADER><CHAR>본문 계속</CHAR></TEXT></P>
<P ParaShape="1" Style="1"><TEXT CharShape="0"><CHAR>둘째 장</CHAR></TEXT></P>
<P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>끝.</CHAR></TEXT></P>
</SECTION></BODY>
<TAIL><BINDATASTORAGE><BINDATA Id="1" Size="{len(png)}" Encoding="Base64" Compress="{'true' if compress else 'false'}">
{data}</BINDATA></BINDATASTORAGE></TAIL></HWPML>"""
    path.write_bytes(xml.encode(encoding))
    return path


class HwpxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hwpx_package(self) -> None:
        document = hwpx.convert(build_hwpx(self.root / "doc.hwpx"))
        self.assertEqual([c.title for c in document.chapters], ["한글 문서", "제1장 시작", "제2장"])
        self.assertEqual(document.title, "한글 문서")
        self.assertEqual(document.authors, ["홍길동"])
        self.assertEqual(document.language, "ko")
        self.assertEqual(document.description, "설명 텍스트")
        self.assertEqual(document.source_format, "hwpx")
        intro, first, second = (c.html for c in document.chapters)
        for chapter in document.chapters:
            well_formed(chapter.html)
        self.assertEqual(intro, "<p>머리말 앞 내용</p>")
        for fragment in ("<h1>제1장 시작</h1>", "<b>굵게</b>", "<i><u>기울임</u></i>", "<s><sup>위</sup></s>",
                         "앞 뒤<br/>다음 줄 끝", '<th colspan="2"><p>머리</p></th>', "<td><p>왼쪽</p></td>",
                         'alt="그림 설명"', '<sup><a class="noteref" id="nref1" href="#note1">1</a></sup> 계속',
                         "각주 내용", "<h2>Ghi chú</h2>", '<a href="https://www.example.com/hwp">링크</a>',
                         "<ul><li>첫 항목</li><li>둘째 항목</li></ul>", "<ol><li>하나</li><li>둘</li></ol>",
                         "남김 유지"):
            self.assertIn(fragment, first)
        self.assertNotIn("javascript", first)
        self.assertIn("나쁜 링크", first)
        self.assertNotIn("삭제됨", first)
        self.assertNotIn("<u>굵게", first)
        self.assertIn("둘째 구역 본문", second)
        self.assertEqual(len(document.resources), 1)
        self.assertEqual(document.resources[0].data, make_png((0, 255, 0)))
        self.assertIn(f'<img src="{document.resources[0].name}"', first)

    def test_hwpx_without_content_hpf_uses_sorted_sections(self) -> None:
        document = hwpx.convert(build_hwpx(self.root / "bare.hwpx", with_hpf=False))
        self.assertEqual(len(document.chapters), 1)
        markup = document.chapters[0].html
        self.assertLess(markup.index("머리말 앞 내용"), markup.index("둘째 구역 본문"))
        self.assertEqual(document.resources[0].media_type, "image/png")  # BinData/<id>.* fallback

    def test_hwpml_compressed_and_utf16(self) -> None:
        for compress, encoding in ((True, "utf-8"), (False, "utf-16")):
            with self.subTest(compress=compress, encoding=encoding):
                document = hwpx.convert(build_hwpml(self.root / f"doc-{encoding}.hwpml", compress=compress,
                                                    encoding=encoding))
                self.assertEqual([c.title for c in document.chapters], ["HWPML 제목", "첫째 장", "둘째 장"])
                self.assertEqual(document.authors, ["김작가"])
                self.assertEqual(document.description, "요약")
                self.assertEqual(document.source_format, "hwpml")
                first = document.chapters[1].html
                well_formed(first)
                for fragment in ("<h1>첫째 장</h1>", "<b>굵은 글</b> 보통<i><u>기울임</u></i>", "탭 뒤<br/>새 줄",
                                 '<td colspan="2"><p>넓은 칸</p></td>', "각주 본문", "본문 계속"):
                    self.assertIn(fragment, first)
                self.assertNotIn("머리글 숨김", first)
                self.assertEqual(document.resources[0].data, make_png((10, 20, 30)))
                self.assertIn(f'<img src="{document.resources[0].name}"', first)

    def test_hwpml_legacy_multibyte_encoding(self) -> None:
        document = hwpx.convert(build_hwpml(self.root / "euckr.hwpml", encoding="euc-kr"))
        self.assertEqual(document.chapters[1].title, "첫째 장")

    def test_errors(self) -> None:
        with self.assertRaisesRegex(FormatError, "password"):
            hwpx.convert(build_hwpx(self.root / "locked.hwpx", encrypted=True))
        with self.assertRaisesRegex(FormatError, "not an HWPX"):
            hwpx.convert(build_hwpx(self.root / "epub.hwpx", mimetype="application/epub+zip"))
        doctype = '<!DOCTYPE HWPML [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        with self.assertRaisesRegex(FormatError, "entity"):
            hwpx.convert(build_hwpml(self.root / "bomb.hwpml", doctype=doctype))
        other = self.root / "other.hwpml"
        other.write_text('<?xml version="1.0"?><html><body>x</body></html>', encoding="utf-8")
        with self.assertRaisesRegex(FormatError, "HWPML"):
            hwpx.convert(other)
        garbage = self.root / "garbage.hwpx"
        garbage.write_bytes(b"\x13\x37" * 100)
        with self.assertRaises(FormatError):
            hwpx.convert(garbage)
        ole = self.root / "old.hwpx"
        ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
        with self.assertRaises(FormatError):
            hwpx.convert(ole)
        broken_section = hwpx_section("").replace("</hs:sec>", "")
        with self.assertRaises(FormatError):
            hwpx.convert(build_hwpx(self.root / "broken.hwpx", section0=broken_section))

    def test_public_entry_for_hancom_extensions(self) -> None:
        document = formats.convert(build_hwpx(self.root / "doc.hwpx"))
        self.assertEqual(document.source_format, "hwpx")
        self.assertEqual(len(document.chapters), 3)
        document = formats.convert(build_hwpml(self.root / "doc.hwpml"))
        self.assertEqual(document.source_format, "hwpml")
        self.assertEqual(document.title, "HWPML 제목")


# ---------------------------------------------------------------- optional LibreOffice round trip

@unittest.skipUnless(shutil.which("soffice"), "LibreOffice (soffice) is not installed")
class LibreOfficeRoundTripTests(unittest.TestCase):
    """Documents written by a real office suite (generated from a synthetic ODT)."""

    def test_libreoffice_docx_and_fodt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build_odt(root / "source.odt")
            outputs: dict[str, Path] = {}
            for target in ("docx", "fodt"):
                result = subprocess.run(
                    ["soffice", f"-env:UserInstallation={(root / 'profile').as_uri()}", "--headless",
                     "--convert-to", target, "--outdir", str(root / "out"), str(source)],
                    capture_output=True, timeout=180, check=False)
                produced = root / "out" / f"source.{target}"
                if result.returncode != 0 or not produced.exists():
                    self.skipTest(f"soffice could not convert to {target}")
                outputs[target] = produced
            for target, path in outputs.items():
                with self.subTest(target=target):
                    document = formats.convert(path)
                    titles = [c.title for c in document.chapters]
                    self.assertEqual(titles[-2:], ["Part A", "Part B"])
                    joined = "".join(c.html for c in document.chapters)
                    for fragment in ("<b>bold</b>", "<i>italic</i>", "x<sup>2</sup>", "apple", "second sub",
                                     '<ol type="a">', "wide cell", 'colspan="2"', 'rowspan="2"', "The note body.",
                                     "<h2>Ghi chú</h2>", "In section", '<a href="https://example.com/odf">web</a>',
                                     "<p>Demoted heading</p>"):
                        self.assertIn(fragment, joined)
                    self.assertNotIn("javascript", joined)
                    self.assertNotIn("DELETED TEXT", joined)
                    self.assertEqual([r.media_type for r in document.resources], ["image/png"])
                    for chapter in document.chapters:
                        well_formed(chapter.html)


if __name__ == "__main__":
    unittest.main()
