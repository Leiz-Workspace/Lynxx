"""Hancom documents: .hwpx (OWPML ZIP package) and .hwpml (older single-file HWPML XML).

``convert()`` dispatches on the first bytes (ZIP vs XML). Both dialects share one
walker: their element names map onto the same canonical vocabulary (paragraph,
run, text, table, picture, ...). Elements are matched by local name.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import re
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .base import Document, FormatError, read_limited, resolve_href
from .ooxml import (
    MAX_DEPTH, MAX_FLAT_BYTES, MAX_IMAGE_BYTES, MAX_XML_BYTES, _add_image, _assemble, _Block, _fmt, _http_link,
    _Inline, _join_blocks, _ListCounter, _local, _Notes, _open_zip, _parse_xml, _read_member, _source_format, _table_html, _walk,
)

_HWPX_NAMES = {
    "p": "p", "run": "run", "t": "t", "tab": "tab", "lineBreak": "br", "hyphen": "hyphen", "nbSpace": "nbsp",
    "fwSpace": "fwsp", "tbl": "table", "tr": "row", "tc": "cell", "subList": "sublist", "pic": "picture",
    "img": "image", "footNote": "footnote", "endNote": "endnote", "ctrl": "ctrl", "fieldBegin": "fieldbegin",
    "fieldEnd": "fieldend", "rect": "shape", "ellipse": "shape", "arc": "shape", "polygon": "shape",
    "curve": "shape", "connectLine": "shape", "container": "shape", "drawText": "drawtext", "dutmal": "dutmal",
    "mainText": "maintext", "subText": "subtext", "deleteBegin": "delbegin", "deleteEnd": "delend",
    "shapeComment": "comment", "switch": "switch",
}
_HWPML_NAMES = {
    "P": "p", "TEXT": "run", "CHAR": "t", "TAB": "tab", "LINEBREAK": "br", "HYPHEN": "hyphen", "NBSPACE": "nbsp",
    "FWSPACE": "fwsp", "TABLE": "table", "ROW": "row", "CELL": "cell", "PARALIST": "sublist", "PICTURE": "picture",
    "IMAGE": "image", "FOOTNOTE": "footnote", "ENDNOTE": "endnote", "FIELDBEGIN": "fieldbegin",
    "FIELDEND": "fieldend", "RECTANGLE": "shape", "ELLIPSE": "shape", "ARC": "shape", "POLYGON": "shape",
    "CURVE": "shape", "CONTAINER": "shape", "DRAWTEXT": "drawtext", "DUTMAL": "dutmal", "MAINTEXT": "maintext",
    "SUBTEXT": "subtext", "SHAPECOMMENT": "comment",
}
_HWPX_ATTRS = {"p_style": "styleIDRef", "p_shape": "paraPrIDRef", "run_char": "charPrIDRef",
               "image_ref": "binaryItemIDRef"}
_HWPML_ATTRS = {"p_style": "Style", "p_shape": "ParaShape", "run_char": "CharShape", "image_ref": "BinItem"}
_HEADING_NAME = re.compile(r"(?:개요|제목|outline|heading)\s*(10|[1-9])")
_TITLE_NAMES = frozenset({"제목", "title"})
_SUBTITLE_NAMES = frozenset({"부제목", "subtitle"})
_SECTION_NAME = re.compile(r"(?:^|/)section(\d+)\.xml$", re.IGNORECASE)
_HWPX_MIMETYPES = (b"application/hwp+zip", b"application/haansofthwpx", b"application/vnd.hancom.hwpx")


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _attr(element: ET.Element, *names: str) -> str:
    for name in names:
        value = element.get(name)
        if value is not None:
            return value
    return ""


def _char_props(element: ET.Element) -> dict:
    props: dict = {}
    for child in element:
        local = _local(child.tag).lower()
        if local == "bold":
            props["b"] = True
        elif local == "italic":
            props["i"] = True
        elif local == "underline":
            props["u"] = _attr(child, "type", "Type").lower() not in ("none",)
        elif local == "strikeout":
            props["s"] = _attr(child, "shape", "Shape", "type", "Type").lower() not in ("none", "3d")
        elif local in ("supscript", "superscript"):
            props["va"] = "sup"
        elif local == "subscript":
            props["va"] = "sub"
    return props


class _HwpConverter:
    def __init__(self, document: Document, names: dict[str, str], attrs: dict[str, str], image_loader) -> None:
        self.doc = document
        self.names = names
        self.attrs = attrs
        self.load_image = image_loader
        self.char_props: dict[str, dict] = {}
        self.para_heads: dict[str, tuple[str, int, str]] = {}
        self.styles: dict[str, dict] = {}
        self.para_cache: dict[tuple[str, str], tuple] = {}
        self.images: dict[str, str] = {}
        self.notes = _Notes()
        self.counter = _ListCounter()
        self.pending: list[_Block] = []
        self.in_note = False
        self.cell_depth = 0
        self.deleting = False
        self.first_title = ""

    def canon(self, element: ET.Element) -> str:
        return self.names.get(_local(element.tag), "")

    # ------------------------------------------------------------ styles

    def load_hwpx_header(self, root: ET.Element) -> None:
        for element in root.iter():
            local = _local(element.tag)
            if local == "charPr" and element.get("id") is not None:
                self.char_props[element.get("id") or ""] = _char_props(element)
            elif local == "paraPr" and element.get("id") is not None:
                for node in _walk(element, limit=200):
                    if _local(node.tag) == "heading":
                        self.para_heads[element.get("id") or ""] = (
                            (node.get("type") or "NONE").upper(), _int(node.get("level"), 0, 0, 9),
                            node.get("idRef") or "")
                        break
            elif local == "style" and element.get("id") is not None:
                self.styles[element.get("id") or ""] = {
                    "name": element.get("name") or "", "eng": element.get("engName") or "",
                    "para": element.get("paraPrIDRef") or "",
                }

    def load_hwpml_mapping(self, mapping: ET.Element | None) -> None:
        if mapping is None:
            return
        for element in mapping.iter():
            tag = _local(element.tag)
            if tag == "CHARSHAPE" and element.get("Id") is not None:
                self.char_props[element.get("Id") or ""] = _char_props(element)
            elif tag == "PARASHAPE" and element.get("Id") is not None:
                self.para_heads[element.get("Id") or ""] = (
                    (element.get("HeadingType") or "NONE").upper(), _int(element.get("Level"), 0, 0, 9),
                    element.get("Heading") or "")
            elif tag == "STYLE" and element.get("Id") is not None:
                self.styles[element.get("Id") or ""] = {
                    "name": element.get("Name") or "", "eng": element.get("EngName") or "",
                    "para": element.get("ParaShape") or "",
                }

    def _para_info(self, paragraph: ET.Element) -> tuple[str, int, tuple | None]:
        style_id = paragraph.get(self.attrs["p_style"]) or ""
        shape_id = paragraph.get(self.attrs["p_shape"]) or ""
        key = (style_id, shape_id)
        if key in self.para_cache:
            return self.para_cache[key]
        style = self.styles.get(style_id) or {}
        kind, level = "", 0
        for label in (style.get("name", ""), style.get("eng", "")):
            label = " ".join(label.split()).lower()
            match = _HEADING_NAME.fullmatch(label)
            if match:
                kind, level = "heading", int(match.group(1))
            elif label in _TITLE_NAMES:
                kind, level = "title", 1
            elif label in _SUBTITLE_NAMES:
                kind, level = "subtitle", 2
            if kind:
                break
        head = self.para_heads.get(shape_id) or self.para_heads.get(style.get("para", ""))
        list_spec = None
        if head and not kind:
            head_type, head_level, head_ref = head
            if head_type == "OUTLINE":
                kind, level = "heading", head_level + 1
            elif head_type in ("NUMBER", "BULLET"):
                list_spec = (head_type, head_level, head_ref)
        info = (kind, level, list_spec)
        self.para_cache[key] = info
        return info

    # ------------------------------------------------------------ blocks

    def blocks(self, parent: ET.Element, depth: int) -> list[_Block]:
        blocks: list[_Block] = []
        if depth > MAX_DEPTH:
            return blocks
        for child in parent:
            kind = self.canon(child)
            if kind == "p":
                blocks.extend(self._paragraph(child, depth))
            elif kind == "sublist":
                blocks.extend(self.blocks(child, depth + 1))
            elif kind == "table":
                table = self._table(child, depth + 1)
                if table is not None:
                    blocks.append(table)
        return blocks

    def _paragraph(self, paragraph: ET.Element, depth: int) -> list[_Block]:
        kind, level, list_spec = self._para_info(paragraph)
        saved, self.pending = self.pending, []
        saved_deleting, self.deleting = self.deleting, False
        out = _Inline(drop=("b",) if kind else ())
        for child in paragraph:
            if self.canon(child) == "run":
                self._run(child, out, depth)
            else:
                self._run_item(child, out, (), depth)
        markup = out.html()
        blocks: list[_Block] = []
        if out.content:
            if kind:
                text = out.plain_text()
                tag = f"h{min(level, 6)}"
                blocks.append(_Block(f"<{tag}>{markup}</{tag}>", 0 if kind == "subtitle" else level, text))
                if kind == "title" and not self.first_title:
                    self.first_title = text
            elif list_spec is not None:
                head_type, head_level, head_ref = list_spec
                item = self.counter.item((head_type, head_ref), head_level, head_type == "NUMBER")
                blocks.append(_Block(markup, item=item))
            else:
                blocks.append(_Block(f"<p>{markup}</p>"))
        blocks.extend(self.pending)
        self.pending, self.deleting = saved, saved_deleting
        return blocks

    def _table(self, table: ET.Element, depth: int) -> _Block | None:
        if depth > MAX_DEPTH:
            return None
        rows: list[tuple[bool, list[tuple[int, int, str]]]] = []
        for row in table:
            if self.canon(row) != "row":
                continue
            header = False
            cells: list[tuple[int, int, str]] = []
            for cell in row:
                if self.canon(cell) != "cell":
                    continue
                span = _int(_attr(cell, "ColSpan", "colSpan"), 1, 1, 1000)
                rowspan = _int(_attr(cell, "RowSpan", "rowSpan"), 1, 1, 10000)
                for child in cell:
                    if _local(child.tag) == "cellSpan":
                        span = _int(child.get("colSpan"), span, 1, 1000)
                        rowspan = _int(child.get("rowSpan"), rowspan, 1, 10000)
                header = header or _attr(cell, "header", "Header").lower() in ("1", "true")
                blocks: list[_Block] = []
                self.cell_depth += 1
                try:
                    for child in cell:
                        if self.canon(child) == "sublist":
                            blocks.extend(self.blocks(child, depth + 1))
                finally:
                    self.cell_depth -= 1
                cells.append((span, rowspan, _join_blocks(blocks)))
            rows.append((header, cells))
        markup = _table_html(rows, self.cell_depth > 0)
        return _Block(markup) if markup else None

    # ------------------------------------------------------------ inline content

    def _run(self, run: ET.Element, out: _Inline, depth: int) -> None:
        fmt = _fmt(self.char_props.get(run.get(self.attrs["run_char"]) or "", {}))
        for child in run:
            self._run_item(child, out, fmt, depth)

    def _run_item(self, child: ET.Element, out: _Inline, fmt: tuple[str, ...], depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        kind = self.canon(child)
        if kind == "t":
            self._text(child, out, fmt)
        elif kind in ("tab", "br", "hyphen", "nbsp", "fwsp", "delbegin", "delend"):
            self._special(kind, out, fmt)
        elif kind == "table":
            table = self._table(child, depth + 1)
            if table is not None:
                self.pending.append(table)
        elif kind == "picture":
            self._picture(child, out)
        elif kind == "ctrl":
            for node in child:
                self._run_item(node, out, fmt, depth + 1)
        elif kind in ("footnote", "endnote"):
            self._note(child, out, depth)
        elif kind == "fieldbegin":
            self._field_begin(child, out)
        elif kind == "fieldend":
            out.close_link()
        elif kind == "shape":
            self._shape(child, out, depth + 1)
        elif kind == "dutmal":
            self._dutmal(child, out, fmt)
        elif kind == "switch":  # hp:switch: the first hp:case, else hp:default
            before = (len(out.parts), len(self.pending))
            for branch in ("case", "default"):
                node = next((item for item in child if _local(item.tag) == branch), None)
                if node is not None:
                    for item in node:
                        self._run_item(item, out, fmt, depth + 1)
                if (len(out.parts), len(self.pending)) != before:
                    break

    def _special(self, kind: str, out: _Inline, fmt: tuple[str, ...]) -> None:
        if kind == "delbegin":
            self.deleting = True
        elif kind == "delend":
            self.deleting = False
        elif self.deleting:
            return
        elif kind == "br":
            out.br()
        else:
            out.text({"tab": " ", "hyphen": "-", "nbsp": " ", "fwsp": "　"}[kind], fmt)

    def _text(self, text: ET.Element, out: _Inline, fmt: tuple[str, ...]) -> None:
        if not self.deleting:
            out.text(text.text, fmt)
        for child in text:
            kind = self.canon(child)
            if kind in ("tab", "br", "hyphen", "nbsp", "fwsp", "delbegin", "delend"):
                self._special(kind, out, fmt)
            if child.tail and not self.deleting:
                out.text(child.tail, fmt)

    def _dutmal(self, element: ET.Element, out: _Inline, fmt: tuple[str, ...]) -> None:
        main = sub = ""
        for child in element:
            kind = self.canon(child)
            if kind == "maintext":
                main = "".join(child.itertext())
            elif kind == "subtext":
                sub = "".join(child.itertext())
        main = main or element.get("MainText") or ""
        sub = sub or element.get("SubText") or ""
        if not main.strip():
            return
        if sub.strip():
            inner = _Inline()
            inner.text(main, fmt)
            out.ruby(inner.html(), sub)
        else:
            out.text(main, fmt)

    def _picture(self, picture: ET.Element, out: _Inline) -> None:
        alt = ""
        image = None
        for node in _walk(picture, limit=2000):
            kind = self.canon(node)
            if kind == "comment" and node.text and not alt:
                alt = node.text.strip()
            elif kind == "image" and image is None:
                image = node
        if image is None:
            return
        reference = image.get(self.attrs["image_ref"]) or ""
        if reference not in self.images:
            loaded = self.load_image(reference) if reference else None
            if loaded:
                _add_image(self.doc, self.images, reference, loaded[1], loaded[0])
            else:
                self.images[reference] = ""
        name = self.images[reference]
        if name:
            out.image(name, alt)

    def _shape(self, shape: ET.Element, out: _Inline, depth: int) -> None:
        """Drawing objects: text boxes become following blocks, grouped pictures stay inline."""
        if depth > MAX_DEPTH:
            return
        for child in shape:
            kind = self.canon(child)
            if kind == "drawtext":
                for sub in child:
                    if self.canon(sub) == "sublist":
                        self.pending.extend(self.blocks(sub, depth + 1))
            elif kind == "sublist":
                self.pending.extend(self.blocks(child, depth + 1))
            elif kind == "shape":
                self._shape(child, out, depth + 1)
            elif kind == "picture":
                self._picture(child, out)

    def _note(self, note: ET.Element, out: _Inline, depth: int) -> None:
        if self.in_note:
            return
        saved = (self.pending, self.deleting)
        self.pending, self.in_note = [], True
        try:
            blocks: list[_Block] = []
            for child in note:
                if self.canon(child) == "sublist":
                    blocks.extend(self.blocks(child, depth + 1))
            markup = _join_blocks(blocks + self.pending)
        finally:
            self.pending, self.deleting = saved
            self.in_note = False
        if markup:
            out.note(self.notes.add(markup))

    def _field_begin(self, field: ET.Element, out: _Inline) -> None:
        if _attr(field, "type", "Type").upper() != "HYPERLINK":
            return
        url = ""
        command = _attr(field, "Command", "command")
        for node in _walk(field, limit=200):
            if _local(node.tag).lower() == "stringparam" and node.text:
                name = (node.get("name") or "").lower()
                if name == "path":
                    url = node.text
                elif name == "command" and not command:
                    command = node.text
        if not _http_link(url) and command:
            url = command.replace("\\:", ":").split(";", 1)[0]
        out.open_link(_http_link(url))


# ---------------------------------------------------------------- HWPX package

def _convert_hwpx(path: Path) -> Document:
    archive = _open_zip(path)
    try:
        members = {name.lower(): name for name in archive.namelist()}

        def member(name: str) -> str | None:
            return members.get(name.lower()) if name else None

        if member("mimetype"):
            mimetype = _read_member(archive, member("mimetype") or "", 1024).strip()
            if mimetype and not mimetype.startswith(_HWPX_MIMETYPES):
                raise FormatError("not an HWPX document")
        manifest = member("META-INF/manifest.xml")
        if manifest and b"encryption-data" in _read_member(archive, manifest, 16 * 1024 * 1024):
            raise FormatError("document is password-protected")

        hpf = ""
        container = member("META-INF/container.xml")
        if container:
            try:
                for node in _parse_xml(_read_member(archive, container, 1024 * 1024), "container.xml").iter():
                    full_path = node.get("full-path") or ""
                    if _local(node.tag) == "rootfile" and full_path.lower().endswith(".hpf") and member(full_path):
                        hpf = member(full_path) or ""
                        break
            except FormatError:
                pass
        hpf = hpf or member("Contents/content.hpf") or next(
            (name for name in members.values() if name.lower().endswith(".hpf")), "")

        def resolve(href: str) -> str | None:
            for candidate in (resolve_href("", href.lstrip("/")), resolve_href(hpf, href) if hpf else ""):
                found = member(candidate)
                if found:
                    return found
            return None

        items: dict[str, tuple[str, str]] = {}
        spine: list[str] = []
        document = Document(source_format="hwpx")
        if hpf:
            package = _parse_xml(_read_member(archive, hpf, MAX_XML_BYTES), "content.hpf")
            for node in package.iter():
                local = _local(node.tag)
                if local == "item" and node.get("id") and node.get("href"):
                    items[node.get("id") or ""] = (node.get("href") or "", (node.get("media-type") or "").lower())
                elif local == "itemref" and node.get("idref"):
                    spine.append(node.get("idref") or "")
                elif local == "metadata":
                    _hpf_metadata(document, node)

        sections: list[str] = []
        for idref in spine:
            href = items.get(idref, ("", ""))[0]
            found = resolve(href) if href else None
            if found and _SECTION_NAME.search(found) and found not in sections:
                sections.append(found)
        if not sections:
            numbered = []
            for name in members.values():
                match = _SECTION_NAME.search(name)
                if match and name.lower().startswith("contents/"):
                    numbered.append((int(match.group(1)), name))
            sections = [name for _number, name in sorted(numbered)]
        if not sections:
            raise FormatError("HWPX package has no sections")

        def load_image(reference: str) -> tuple[str, bytes] | None:
            target = None
            if reference in items:
                target = resolve(items[reference][0])
            if target is None:
                prefix = f"bindata/{reference.lower()}."
                target = next((name for low, name in members.items() if low.startswith(prefix)), None)
            if target is None:
                return None
            try:
                data = _read_member(archive, target, MAX_IMAGE_BYTES)
            except FormatError:
                return None
            return posixpath.splitext(posixpath.basename(target))[0], data

        converter = _HwpConverter(document, _HWPX_NAMES, _HWPX_ATTRS, load_image)
        header = None
        for item_id, (href, _media) in items.items():
            if item_id.lower() == "header" or href.lower().endswith("header.xml"):
                header = resolve(href)
                if header:
                    break
        header = header or member("Contents/header.xml")
        if header:
            try:
                converter.load_hwpx_header(_parse_xml(_read_member(archive, header, MAX_XML_BYTES), "header.xml"))
            except FormatError:
                pass
        blocks: list[_Block] = []
        for section in sections:
            root = _parse_xml(_read_member(archive, section, MAX_XML_BYTES), section)
            blocks.extend(converter.blocks(root, 0))
        if not document.title and converter.first_title:
            document.title = converter.first_title
        return _assemble(document, blocks, converter.notes, document.title or path.stem)
    finally:
        archive.close()


def _hpf_metadata(document: Document, metadata: ET.Element) -> None:
    description = subject = ""
    for node in metadata:
        local = _local(node.tag).lower()
        text = (node.text or "").strip()
        if local == "meta":
            content = node.get("content") or ""
            text = text or (content if content.lower() not in ("text", "date") else "")
            local = (node.get("name") or "").lower()
        if not text:
            continue
        if local == "title" and not document.title:
            document.title = text
        elif local == "language" and not document.language:
            document.language = text
        elif local in ("creator", "author") and text not in document.authors:
            document.authors.append(text)
        elif local == "description":
            description = description or text
        elif local == "subject":
            subject = subject or text
    document.description = description or subject


# ---------------------------------------------------------------- HWPML single file

def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _decode_bindata(element: ET.Element) -> bytes | None:
    text = element.text or ""
    if not text.strip() or len(text) > MAX_IMAGE_BYTES * 4 // 3 + 65536:
        return None
    if (element.get("Encoding") or "Base64").lower() != "base64":
        return None
    try:
        data = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return None
    if (element.get("Compress") or "").lower() == "true":
        for wbits in (-15, 47):
            inflater = zlib.decompressobj(wbits)
            try:
                inflated = inflater.decompress(data, MAX_IMAGE_BYTES + 1)
            except zlib.error:
                continue
            if len(inflated) > MAX_IMAGE_BYTES or inflater.unconsumed_tail:
                return None
            return inflated
    return data if len(data) <= MAX_IMAGE_BYTES else None


def _convert_hwpml(path: Path) -> Document:
    root = _parse_xml(read_limited(path, MAX_FLAT_BYTES), "HWPML")
    if _local(root.tag).upper() != "HWPML":
        raise FormatError("not an HWPML document")
    head, body, tail = _child(root, "HEAD"), _child(root, "BODY"), _child(root, "TAIL")
    if body is None:
        raise FormatError("HWPML document has no BODY")
    document = Document(source_format=_source_format(path, ("hwpml",), "hwpml"))
    summary = _child(head, "DOCSUMMARY")
    if summary is not None:
        values = {_local(node.tag): " ".join("".join(node.itertext()).split()) for node in summary}
        document.title = values.get("TITLE", "")
        if values.get("AUTHOR"):
            document.authors = [values["AUTHOR"]]
        document.description = values.get("SUBJECT") or values.get("COMMENTS") or ""
    mapping = _child(head, "MAPPINGTABLE")
    bin_list, bin_storage = _child(mapping, "BINDATALIST"), _child(tail, "BINDATASTORAGE")
    bin_items = [node for node in (bin_list if bin_list is not None else []) if _local(node.tag) == "BINITEM"]
    storage = {node.get("Id") or "": node for node in (bin_storage if bin_storage is not None else [])
               if _local(node.tag) == "BINDATA"}

    def load_image(reference: str) -> tuple[str, bytes] | None:
        candidates: list[str] = []
        index = _int(reference, -1, -1, 1 << 20)
        if 1 <= index <= len(bin_items):
            candidates.append(bin_items[index - 1].get("BinData") or "")
        candidates.append(reference)
        if 0 <= index < len(bin_items):
            candidates.append(bin_items[index].get("BinData") or "")
        for candidate in candidates:
            element = storage.get(candidate)
            if element is not None:
                data = _decode_bindata(element)
                return (f"bindata{candidate}", data) if data else None
        return None

    converter = _HwpConverter(document, _HWPML_NAMES, _HWPML_ATTRS, load_image)
    converter.load_hwpml_mapping(mapping)
    blocks: list[_Block] = []
    for section in body:
        if _local(section.tag) == "SECTION":
            blocks.extend(converter.blocks(section, 0))
    if not document.title and converter.first_title:
        document.title = converter.first_title
    return _assemble(document, blocks, converter.notes, document.title or path.stem)


def convert(path: Path) -> Document:
    """Convert an .hwpx package or an .hwpml XML file (chosen by the first bytes)."""
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(8)
    if head.startswith(b"PK"):
        return _convert_hwpx(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise FormatError("binary HWP 5 document, not HWPX/HWPML")
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"<") or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return _convert_hwpml(path)
    raise FormatError("not an HWPX or HWPML document")
