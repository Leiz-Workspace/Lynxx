"""Hangul Word Processor 5.x binary documents (.hwp).

An HWP 5 file is a CFB container: ``FileHeader`` (signature, version, flags),
``DocInfo`` (styles, binary item table) and ``BodyText/Section0..N`` record
streams, raw-deflate compressed when the header's "compressed" flag is set.
Each record has a 32-bit header (tag 10 bits, level 10 bits, size 12 bits with
0xFFF meaning "a 32-bit size follows"). Paragraph text (``PARA_TEXT``) is UTF-16LE
with inline / extended control characters that occupy 8 code units.

Supported: body paragraphs, tables (cells grouped into rows from the TABLE
record), outline / heading styles as chapter headings (else heading-like
paragraphs), embedded JPEG/PNG/GIF/BMP pictures in place, title / author from
``\\x05HwpSummaryInformation``. Headers, footers and hidden comments are dropped.
Password, distribution (encrypted) and DRM documents raise :class:`FormatError`,
and so do HWP 3.x files (not a CFB container).
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

from .base import Document, FormatError, read_limited, sniff_image_type
from .cfb import MAGIC, CompoundFile, summary_title_author
from .msdoc import Block, BlockBuilder, ImageRef, Parts, build_chapters

SIGNATURE = b"HWP Document File"
HWPTAG_BEGIN = 0x10
TAG_BIN_DATA = HWPTAG_BEGIN + 2
TAG_STYLE = HWPTAG_BEGIN + 10
TAG_PARA_HEADER = HWPTAG_BEGIN + 50
TAG_PARA_TEXT = HWPTAG_BEGIN + 51
TAG_CTRL_HEADER = HWPTAG_BEGIN + 55
TAG_LIST_HEADER = HWPTAG_BEGIN + 56
TAG_TABLE = HWPTAG_BEGIN + 61
TAG_SHAPE_PICTURE = HWPTAG_BEGIN + 69

FLAG_COMPRESSED = 1 << 0
FLAG_PASSWORD = 1 << 1
FLAG_DISTRIBUTION = 1 << 2
FLAG_DRM = (1 << 4) | (1 << 8) | (1 << 10)

SECTION_LIMIT = 256 * 1024 * 1024
TOTAL_EXPANDED_LIMIT = 512 * 1024 * 1024
DOCINFO_LIMIT = 64 * 1024 * 1024
IMAGE_LIMIT = 64 * 1024 * 1024
MAX_SECTIONS = 4096
MAX_TEXT_CHARS = 64 * 1024 * 1024
MAX_TABLE_NESTING = 8

_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}
_SKIPPED_CONTROLS = {"head", "foot", "tcmt"}
_CONTROL_UNIT = re.compile(rb"[\x00-\x1f]\x00", re.DOTALL)
_STYLE_HEADING = re.compile(r"^(?:outline|개요|heading|제목|見出し)\s*(\d{1,2})$", re.IGNORECASE)
_STYLE_TOC = re.compile(r"^(?:toc|차례|목차)\s*\d*$", re.IGNORECASE)


def _inflate(data: bytes, limit: int) -> bytes:
    """Raw deflate (HWP) with a zlib-wrapped fallback; output is capped at ``limit``."""
    for wbits in (-15, 15):
        engine = zlib.decompressobj(wbits)
        try:
            out = engine.decompress(data, limit)
        except zlib.error:
            continue
        if engine.unconsumed_tail:
            raise FormatError("compressed HWP stream expands beyond the size limit")
        return out
    raise FormatError("corrupt compressed HWP stream")


def _records(data: bytes):
    """(tag, level, payload) for every record; a truncated last record is cut to the data."""
    pos, total = 0, len(data)
    while pos + 4 <= total:
        header = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        size = header >> 20
        if size == 0xFFF:
            if pos + 4 > total:
                return
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        yield header & 0x3FF, (header >> 10) & 0x3FF, data[pos:pos + size]
        pos += size


def _wstring(payload: bytes, pos: int) -> tuple[str, int]:
    """A WORD-length-prefixed UTF-16 string at ``pos``: (text, position after it)."""
    if pos + 2 > len(payload):
        return "", len(payload)
    count = struct.unpack_from("<H", payload, pos)[0]
    start, end = pos + 2, pos + 2 + 2 * count
    return payload[start:min(end, len(payload))].decode("utf-16-le", errors="replace"), end


def para_text(payload: bytes) -> str:
    """Decode a PARA_TEXT record, dropping control characters (8 code units for inline/extended ones)."""
    out: list[str] = []
    units = len(payload) // 2
    index = 0
    while index < units:
        match = _CONTROL_UNIT.search(payload, index * 2, units * 2)
        while match and match.start() % 2:
            match = _CONTROL_UNIT.search(payload, match.start() + 1, units * 2)
        if not match:
            out.append(payload[index * 2:units * 2].decode("utf-16-le", errors="replace"))
            break
        if match.start() > index * 2:
            out.append(payload[index * 2:match.start()].decode("utf-16-le", errors="replace"))
        index = match.start() // 2
        code = payload[match.start()]
        if code in _CHAR_CONTROLS:
            if code == 10:
                out.append("\n")
            elif code == 24:
                out.append("-")
            elif code == 30:
                out.append(" ")
            elif code == 31:
                out.append(" ")
            index += 1
        else:
            if code == 9:
                out.append("\t")
            index += 8
    return "".join(out)


class _DocInfo:
    """Style heading levels and the binary item table from the DocInfo stream."""

    def __init__(self, data: bytes) -> None:
        self.style_levels: dict[int, int] = {}
        self.toc_styles: set[int] = set()
        self.bin_items: list[tuple[int, int]] = []  # (BinData id, compression mode) per BinItem id - 1
        style_index = 0
        for tag, _level, payload in _records(data):
            if tag == TAG_STYLE:
                local, pos = _wstring(payload, 0)
                english, _ = _wstring(payload, pos)
                for name in (english.strip(), local.strip()):
                    match = _STYLE_HEADING.match(name)
                    if match and 1 <= int(match.group(1)) <= 10:
                        self.style_levels[style_index] = int(match.group(1))
                        break
                    if name and _STYLE_TOC.match(name):
                        self.toc_styles.add(style_index)
                        break
                style_index += 1
            elif tag == TAG_BIN_DATA and len(payload) >= 2:
                attr = struct.unpack_from("<H", payload, 0)[0]
                kind, compression = attr & 0x0F, (attr >> 4) & 0x03
                bin_id = struct.unpack_from("<H", payload, 2)[0] if kind == 1 and len(payload) >= 4 else 0
                self.bin_items.append((bin_id, compression))  # 0: linked file / OLE storage, not a picture


class _Table:
    def __init__(self, level: int) -> None:
        self.level = level
        self.row_sizes: list[int] = []
        self.columns = 0
        self.cells: list[list[Parts]] = []

    def rows(self) -> list[list[list[Parts]]]:
        cells = self.cells
        sizes = self.row_sizes
        if not sizes or sum(sizes) != len(cells):  # unexpected layout: fall back to the column count
            width = self.columns if 0 < self.columns <= len(cells) else 1
            sizes = [width] * ((len(cells) + width - 1) // width)
        rows, pos = [], 0
        for size in sizes:
            rows.append(cells[pos:pos + size])
            pos += size
        return rows


class _BodyReader:
    def __init__(self, info: _DocInfo, picture) -> None:
        self.info = info
        self.picture = picture
        self.builder = BlockBuilder()
        self.parts: Parts | None = None
        self.para_level = 0
        self.para_style = -1
        self.tables: list[_Table] = []
        self.skip_level: int | None = None
        self.chars = 0

    def flush(self) -> None:
        parts, self.parts = self.parts, None
        if parts is None:
            return
        if self.tables and self.para_level > self.tables[-1].level:
            table = self.tables[-1]
            if not table.cells:
                table.cells.append([])
            table.cells[-1].append(parts)
            return
        level = self.info.style_levels.get(self.para_style)
        self.builder.body(parts, level, self.para_style in self.info.toc_styles)

    def close_tables(self, level: int) -> None:
        while self.tables and level <= self.tables[-1].level:
            self.flush()
            table = self.tables.pop()
            if self.tables:  # nested table: flatten its cells into the enclosing cell
                outer = self.tables[-1]
                if not outer.cells:
                    outer.cells.append([])
                for cell in table.cells:
                    outer.cells[-1].extend(cell)
            else:
                self.builder.add_table(table.rows())

    def feed(self, data: bytes) -> None:
        for tag, level, payload in _records(data):
            if self.skip_level is not None:
                if level > self.skip_level:
                    continue
                self.skip_level = None
            self.close_tables(level)
            if tag == TAG_PARA_HEADER:
                self.flush()
                self.parts = []
                self.para_level = level
                self.para_style = payload[10] if len(payload) > 10 else -1
            elif tag == TAG_PARA_TEXT:
                if self.parts is None:
                    self.parts, self.para_level, self.para_style = [], max(0, level - 1), -1
                text = para_text(payload)
                self.chars += len(text)
                if self.chars > MAX_TEXT_CHARS:
                    raise FormatError("HWP document has too much text")
                self.parts.append(text)
            elif tag == TAG_CTRL_HEADER and len(payload) >= 4:
                ctrl = payload[:4][::-1].decode("latin-1")
                if ctrl in _SKIPPED_CONTROLS:
                    self.skip_level = level
                elif ctrl == "tbl " and len(self.tables) < MAX_TABLE_NESTING:
                    self.flush()
                    self.tables.append(_Table(level))
            elif tag == TAG_TABLE and self.tables and level == self.tables[-1].level + 1 and len(payload) >= 18:
                rows, self.tables[-1].columns = struct.unpack_from("<HH", payload, 4)
                if 18 + 2 * rows <= len(payload):
                    self.tables[-1].row_sizes = list(struct.unpack_from(f"<{rows}H", payload, 18))
            elif tag == TAG_LIST_HEADER and self.tables and level == self.tables[-1].level + 1:
                self.flush()
                self.tables[-1].cells.append([])
            elif tag == TAG_SHAPE_PICTURE and len(payload) >= 73 and self.picture is not None:
                name = self.picture(struct.unpack_from("<H", payload, 71)[0])
                if name:
                    if self.parts is not None:
                        self.parts.append(ImageRef(name))
                    elif self.tables and self.tables[-1].cells:
                        self.tables[-1].cells[-1].append([ImageRef(name)])
                    else:
                        self.builder.image(name)

    def finish(self) -> list[Block]:
        self.close_tables(0)
        self.flush()
        return self.builder.finish()


def _read_header(cfb: CompoundFile) -> tuple[int, int]:
    if not cfb.is_stream("FileHeader"):
        raise FormatError("not an HWP document (no FileHeader stream)")
    header = cfb.read("FileHeader", limit=1024 * 1024)
    if len(header) < 40 or header[:32].rstrip(b"\0") != SIGNATURE:
        raise FormatError("not an HWP document (bad signature)")
    version, flags = struct.unpack_from("<II", header, 32)
    major = version >> 24
    if major != 5:
        raise FormatError(f"HWP {major}.x is not supported")
    if flags & FLAG_PASSWORD:
        raise FormatError("password-protected HWP is not supported")
    if flags & FLAG_DISTRIBUTION:
        raise FormatError("distribution HWP is encrypted")
    if flags & FLAG_DRM:
        raise FormatError("DRM-protected HWP is not supported")
    return version, flags


def _section_paths(cfb: CompoundFile) -> list[str]:
    numbered: list[tuple[int, str]] = []
    for path in cfb.list_streams():
        match = re.fullmatch(r"BodyText/Section(\d{1,6})", path, re.IGNORECASE)
        if match:
            numbered.append((int(match.group(1)), path))
    numbered.sort()
    if len(numbered) > MAX_SECTIONS:
        raise FormatError("HWP document has too many sections")
    return [path for _, path in numbered]


def _guess_language(blocks: list[Block]) -> str:
    hangul = letters = 0
    for block in blocks[:2000]:
        for part in block.parts:
            if isinstance(part, str):
                for char in part:
                    if char.isalpha():
                        letters += 1
                        hangul += 0xAC00 <= ord(char) <= 0xD7A3
    return "ko" if letters and hangul * 5 >= letters else ""


def convert(path: Path) -> Document:
    data = read_limited(path)
    if data.startswith(b"HWP Document File V"):
        raise FormatError("HWP 3.0 is not supported")
    if not data.startswith(MAGIC):
        if data.startswith(b"PK\x03\x04"):
            raise FormatError("file is HWPX (ZIP), not HWP 5 binary")
        raise FormatError("not an HWP 5 document")
    cfb = CompoundFile(data)
    _version, flags = _read_header(cfb)
    compressed = bool(flags & FLAG_COMPRESSED)
    sections = _section_paths(cfb)
    if not sections:
        if cfb.exists("ViewText"):
            raise FormatError("distribution HWP is encrypted")
        raise FormatError("HWP document has no body text")

    info = _DocInfo(b"")
    if cfb.is_stream("DocInfo"):
        try:
            raw = cfb.read("DocInfo", limit=DOCINFO_LIMIT)
            info = _DocInfo(_inflate(raw, DOCINFO_LIMIT) if compressed else raw)
        except (FormatError, struct.error):
            info = _DocInfo(b"")

    document = Document(source_format="hwp")
    title, authors = summary_title_author(cfb, "\x05HwpSummaryInformation")
    if not title and not authors:
        title, authors = summary_title_author(cfb)
    document.title, document.authors = title, authors

    bin_streams: dict[str, str] = {}
    for stream in cfb.list_streams():
        match = re.fullmatch(r"BinData/(BIN[0-9A-Fa-f]{4})(?:\.\w{1,8})?", stream)
        if match:
            bin_streams.setdefault(match.group(1).upper(), stream)
    cache: dict[int, str] = {}

    def picture(item_id: int) -> str:
        if item_id in cache:
            return cache[item_id]
        name = ""
        try:
            bin_id, mode = item_id, 0  # without a readable DocInfo, BinItem n is usually BIN000n
            if info.bin_items:
                bin_id, mode = info.bin_items[item_id - 1] if 1 <= item_id <= len(info.bin_items) else (0, 0)
            stream = bin_streams.get(f"BIN{bin_id:04X}") if bin_id else None
            if stream:
                raw = cfb.read(stream, limit=IMAGE_LIMIT)
                image = raw
                if mode == 1 or (mode == 0 and compressed):
                    try:
                        image = _inflate(raw, IMAGE_LIMIT)
                    except FormatError:
                        image = raw
                media_type = sniff_image_type(image)
                if media_type and media_type != "image/svg+xml":
                    name = document.add_resource(image, media_type, "image")
        except (FormatError, struct.error):
            name = ""
        cache[item_id] = name
        return name

    reader = _BodyReader(info, picture if bin_streams else None)
    expanded = 0
    for section in sections:
        raw = cfb.read(section, limit=SECTION_LIMIT)
        body = _inflate(raw, SECTION_LIMIT) if compressed else raw
        expanded += len(body)
        if expanded > TOTAL_EXPANDED_LIMIT:
            raise FormatError("HWP document expands beyond the size limit")
        reader.feed(body)
        reader.close_tables(0)
        reader.flush()
    blocks = reader.finish()
    document.language = _guess_language(blocks)
    document.chapters = build_chapters(blocks, document)
    if not document.chapters:
        raise FormatError("HWP document has no readable text")
    return document
