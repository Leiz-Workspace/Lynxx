"""Tests for the OLE2 (CFB) based converters: Word binary .doc/.dot/.wps, Works .wps and HWP 5.

The fixtures are synthesized by a tiny CFB writer below (version 3, 512-byte
sectors, mini stream for streams under 4096 bytes). A LibreOffice round trip
test runs only when ``soffice`` is on PATH.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from lynxx.formats import convert
from lynxx.formats.base import FormatError
from lynxx.formats import cfb as cfb_module
from lynxx.formats import hwp, msdoc
from lynxx.formats.cfb import CompoundFile, read_property_set

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF


# ================================================================ CFB writer

class CfbImage:
    """A synthesized compound file plus its layout, so tests can corrupt it precisely."""

    def __init__(self, data: bytes, starts: dict[str, int], fat_sectors: list[int], dir_start: int,
                 entry_ids: dict[str, int], sector_size: int = 512) -> None:
        self.data = bytearray(data)
        self.starts = starts
        self.fat_sectors = fat_sectors
        self.dir_start = dir_start
        self.entry_ids = entry_ids
        self.sector_size = sector_size

    def set_fat(self, index: int, value: int) -> None:
        per = self.sector_size // 4
        offset = (self.fat_sectors[index // per] + 1) * self.sector_size + (index % per) * 4
        struct.pack_into("<I", self.data, offset, value)

    def entry_offset(self, entry_id: int) -> int:
        per = self.sector_size // 128
        return (self.dir_start + entry_id // per + 1) * self.sector_size + (entry_id % per) * 128

    def bytes(self) -> bytes:
        return bytes(self.data)


def build_cfb(streams: dict[str, bytes], use_difat_sector: bool = False, version: int = 3) -> CfbImage:
    """Version 3 (512-byte sectors) or 4 (4096-byte sectors); streams under 4096 bytes use the mini stream."""
    ss = 512 if version == 3 else 4096
    per_fat = ss // 4
    root: dict = {"name": "Root Entry", "type": 5, "children": {}, "data": b""}
    for path, data in streams.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node["children"].setdefault(part, {"name": part, "type": 1, "children": {}, "data": b""})
        node["children"][parts[-1]] = {"name": parts[-1], "type": 2, "children": {}, "data": data, "path": path}

    entries: list[dict] = [root]
    queue = [root]
    while queue:
        node = queue.pop(0)
        kids = sorted(node["children"].values(), key=lambda n: (len(n["name"]), n["name"].upper()))
        for kid in kids:
            kid["id"] = len(entries)
            entries.append(kid)
            queue.append(kid)
        node["kids"] = kids
    root["id"] = 0
    for node in entries:
        node.setdefault("left", NOSTREAM)
        node.setdefault("right", NOSTREAM)

        def balanced(items: list[dict]) -> int:
            if not items:
                return NOSTREAM
            middle = len(items) // 2
            items[middle]["left"] = balanced(items[:middle])
            items[middle]["right"] = balanced(items[middle + 1:])
            return items[middle]["id"]

        node["child"] = balanced(node["kids"])

    sectors: list[bytes] = []
    fat: list[int] = []

    def alloc(data: bytes, fill: bytes = b"\0") -> int:
        if not data:
            return ENDOFCHAIN
        count = (len(data) + ss - 1) // ss
        start = len(sectors)
        for index in range(count):
            chunk = data[index * ss:(index + 1) * ss]
            sectors.append(chunk + fill * ((ss - len(chunk)) // len(fill)))
            fat.append(start + index + 1 if index + 1 < count else ENDOFCHAIN)
        return start

    ministream = bytearray()
    minifat: list[int] = []
    starts: dict[str, int] = {}
    for node in entries:
        if node["type"] != 2:
            continue
        data = node["data"]
        if len(data) >= 4096:
            node["start"] = alloc(data)
            starts[node["path"]] = node["start"]
        elif data:
            count = (len(data) + 63) // 64
            first = len(minifat)
            for index in range(count):
                minifat.append(first + index + 1 if index + 1 < count else ENDOFCHAIN)
            ministream += data + b"\0" * (count * 64 - len(data))
            node["start"] = first
        else:
            node["start"] = ENDOFCHAIN
    root["start"] = alloc(bytes(ministream))
    root["data"] = bytes(ministream)
    minifat_start = alloc(struct.pack(f"<{len(minifat)}I", *minifat), b"\xff\xff\xff\xff") if minifat else ENDOFCHAIN
    minifat_count = (len(minifat) * 4 + ss - 1) // ss

    raw_dir = bytearray()
    for node in entries:
        name = node["name"].encode("utf-16-le") + b"\0\0"
        entry = name.ljust(64, b"\0") + struct.pack("<HBB", len(name), node["type"], 1)
        entry += struct.pack("<III", node["left"], node["right"], node["child"])
        entry += b"\0" * 16 + b"\0" * 4 + b"\0" * 16
        size = len(node["data"])
        entry += struct.pack("<III", node.get("start", ENDOFCHAIN) if node["type"] != 1 else 0, size, 0)
        raw_dir += entry
    while len(raw_dir) % ss:
        raw_dir += b"\0" * 64 + struct.pack("<HBB", 0, 0, 0) + struct.pack("<III", NOSTREAM, NOSTREAM, NOSTREAM) + b"\0" * 52
    dir_start = alloc(bytes(raw_dir))

    difat_sector = None
    if use_difat_sector:
        difat_sector = len(sectors)
        sectors.append(b"")  # placeholder
        fat.append(DIFSECT)
    dir_count = len(raw_dir) // ss
    fat_count = 1
    while fat_count * per_fat < len(fat) + fat_count:
        fat_count += 1
    fat_sectors = list(range(len(sectors), len(sectors) + fat_count))
    for _ in range(fat_count):
        sectors.append(b"")
        fat.append(FATSECT)
    fat += [FREESECT] * (fat_count * per_fat - len(fat))
    for index, sid in enumerate(fat_sectors):
        sectors[sid] = struct.pack(f"<{per_fat}I", *fat[index * per_fat:(index + 1) * per_fat])
    header_difat = [FREESECT] * 109
    first_difat, num_difat = ENDOFCHAIN, 0
    if difat_sector is not None:
        values = fat_sectors + [FREESECT] * (per_fat - 1 - len(fat_sectors)) + [ENDOFCHAIN]
        sectors[difat_sector] = struct.pack(f"<{per_fat}I", *values)
        first_difat, num_difat = difat_sector, 1
    else:
        header_difat[:fat_count] = fat_sectors
    header = cfb_module.MAGIC + b"\0" * 16
    header += struct.pack("<HHHHH6sIIIIIIIII", 0x3E, version, 0xFFFE, 9 if version == 3 else 12, 6, b"\0" * 6,
                          0 if version == 3 else dir_count, fat_count, dir_start, 0, 4096,
                          minifat_start, minifat_count, first_difat, num_difat)
    header += struct.pack("<109I", *header_difat)
    header = header.ljust(ss, b"\0")
    entry_ids = {node.get("path", node["name"]): node["id"] for node in entries}
    return CfbImage(header + b"".join(sectors), starts, fat_sectors, dir_start, entry_ids, ss)


def property_set(props: dict[int, tuple[int, bytes]]) -> bytes:
    """A one-section PropertySetStream: pid -> (VT type, encoded value)."""
    values = b""
    offsets = []
    for pid, (vt, value) in props.items():
        offsets.append((pid, 8 + 8 * len(props) + len(values)))
        blob = struct.pack("<HH", vt, 0) + value
        blob += b"\0" * (-len(blob) % 4)
        values += blob
    section = b"".join(struct.pack("<II", pid, offset) for pid, offset in offsets) + values
    section = struct.pack("<II", 8 + len(section), len(props)) + section
    fmtid = bytes.fromhex("e0859ff2f94f6810ab9108002b27b3d9")
    head = struct.pack("<HHI", 0xFFFE, 0, 0x00020006) + b"\0" * 16 + struct.pack("<I", 1) + fmtid + struct.pack("<I", 48)
    return head + section


def lpstr(text: str, codec: str = "cp1252") -> bytes:
    raw = text.encode(codec) + b"\0"
    return struct.pack("<I", len(raw)) + raw


def lpwstr(text: str) -> bytes:
    raw = text.encode("utf-16-le") + b"\0\0"
    return struct.pack("<I", len(raw) // 2) + raw


def summary(title: str, author: str, wide: bool = False) -> bytes:
    if wide:
        return property_set({1: (2, struct.pack("<h", 1200)),
                             2: (0x1F, lpwstr(title)), 4: (0x1F, lpwstr(author))})
    return property_set({1: (2, struct.pack("<h", 1252)), 2: (0x1E, lpstr(title)), 4: (0x1E, lpstr(author))})


# ================================================================ Word binary synthesis

TEXT_FC = 0x600


def word_fib(ccp_text: int, pairs: dict[int, tuple[int, int]], flags: int = 0x0200, nfib: int = 0x00C1) -> bytes:
    fib = struct.pack("<HHHHHH", 0xA5EC, nfib, 0, 0x0409, 0, flags)
    fib += struct.pack("<HIBB", 0x00BF, 0, 0, 0) + b"\0" * 12
    fib += struct.pack("<H", 14) + b"\0" * 28
    rglw = [0] * 22
    rglw[3] = ccp_text
    fib += struct.pack("<H", 22) + struct.pack("<22I", *rglw)
    all_pairs = [(0, 0)] * 93
    for index, pair in pairs.items():
        all_pairs[index] = pair
    fib += struct.pack("<H", 93) + b"".join(struct.pack("<II", *pair) for pair in all_pairs)
    fib += struct.pack("<H", 0)
    return fib


def clx(pieces: list[tuple[int, int, int]], with_prc: bool = True) -> bytes:
    """pieces: (cp_start, cp_end, raw fc)."""
    cps = [piece[0] for piece in pieces] + [pieces[-1][1]]
    plc = struct.pack(f"<{len(cps)}I", *cps)
    plc += b"".join(struct.pack("<HIH", 0, fc, 0) for _, _, fc in pieces)
    out = b""
    if with_prc:
        out += b"\x01" + struct.pack("<h", 4) + b"\x16\x24\x01\x00"
    return out + b"\x02" + struct.pack("<I", len(plc)) + plc


PIECE_A = (
    'Intro paragraph \u201cquoted\u201d text.\r'
    'CHAPTER 1\r'
    'See \x13 HYPERLINK "http://x.example" \x14the link\x15 now.\x0bSecond line\r'
    'Cell A\x07Cell B\x07\x07'
)
PIECE_B = (
    'Chương 2: Tiếng Việt\r'
    'Ngày xửa ngày xưa có một nàng công chúa.\r'
    'opt\x1fional non\x1ebreaking\xa0space\x01\x08 \x13 PAGE \x15end\x0cAfter break\r'
)


def build_word_doc(flags: int = 0x0200, extra_streams: dict[str, bytes] | None = None,
                   title: str = "Sample Novel", author: str = "Nguyen Van A") -> bytes:
    raw_a = PIECE_A.encode("cp1252")
    raw_b = PIECE_B.encode("utf-16-le")
    offset_b = TEXT_FC + len(raw_a) + (len(raw_a) % 2)
    wd_body = bytearray(b"\0" * TEXT_FC)
    wd_body += raw_a + b"\0" * (len(raw_a) % 2) + raw_b
    wd_body += b"\0" * max(0, 5000 - len(wd_body))  # >= 4096: stored in regular sectors
    n_a, n_b = len(PIECE_A), len(PIECE_B)
    table_name = "1Table" if flags & 0x0200 else "0Table"
    table = clx([(0, n_a, (TEXT_FC * 2) | 0x40000000), (n_a, n_a + n_b, offset_b)])
    fib = word_fib(n_a + n_b, {33: (0, len(table))}, flags)
    wd_body[:len(fib)] = fib
    streams = {
        "WordDocument": bytes(wd_body),
        table_name: table,
        "\x05SummaryInformation": summary(title, author),
    }
    streams.update(extra_streams or {})
    return build_cfb(streams).bytes()


def officeart_picture(image: bytes) -> bytes:
    """PICF + OfficeArtInlineSpContainer (shape container + FBSE wrapping a PNG BLIP)."""
    blip = struct.pack("<HHI", 0x6E0 << 4, 0xF01E, 17 + len(image)) + b"\0" * 16 + b"\xff" + image
    fbse_body = bytes([6, 6]) + b"\0" * 16 + struct.pack("<HIIIBBBB", 0xFF, len(blip), 1, 0, 0, 0, 0, 0) + blip
    fbse = struct.pack("<HHI", (2 << 4) | 2, 0xF007, len(fbse_body)) + fbse_body
    shape = struct.pack("<HHI", 0x000F, 0xF004, 8) + struct.pack("<HHI", 0x0002, 0xF00A, 0)
    picf = struct.pack("<IHH", 0x44 + len(shape) + len(fbse), 0x44, 0x64) + b"\0" * (0x44 - 8)
    return picf + shape + fbse


def build_styled_doc() -> bytes:
    """A UTF-16 doc with a stylesheet, a PAPX FKP (Heading 1 style, table cells / row mark) and a
    CHPX FKP (tracked deletion, inline picture pointing into the Data stream)."""
    paragraphs = [
        ("Title Para\r", 0, b""),
        ("Heading One\r", 1, b""),
        ("Body one.DROPPED\r", 0, b""),
        ("Pic \x01 here\r", 0, b""),
        ("A1\x07", 0, b"\x16\x24\x01"),
        ("B1\x07", 0, b"\x16\x24\x01"),
        ("\x07", 0, b"\x16\x24\x01\x17\x24\x01"),
        ("Heading Two\r", 1, b""),
        ("Body two.\r", 0, b""),
    ]
    text = "".join(p[0] for p in paragraphs)
    raw = text.encode("utf-16-le")
    papx_pn = (TEXT_FC + len(raw) + 511) // 512 + 1
    chpx_pn = papx_pn + 1

    def fc_of(cp: int) -> int:
        return TEXT_FC + 2 * cp

    # PAPX FKP: one run per paragraph
    crun = len(paragraphs)
    page = bytearray(512)
    fcs, fc = [TEXT_FC], TEXT_FC
    for para_text, _, _ in paragraphs:
        fc += 2 * len(para_text)
        fcs.append(fc)
    struct.pack_into(f"<{crun + 1}I", page, 0, *fcs)
    cursor = 510
    for index, (_, istd, grpprl) in enumerate(paragraphs):
        blob = struct.pack("<H", istd) + grpprl
        if len(blob) % 2:
            papx = bytes([(len(blob) + 1) // 2]) + blob
        else:
            papx = b"\0" + bytes([len(blob) // 2]) + blob
        cursor -= len(papx)
        cursor -= cursor % 2
        page[cursor:cursor + len(papx)] = papx
        page[4 * (crun + 1) + 13 * index] = cursor // 2
    page[511] = crun

    # CHPX FKP: plain / deleted ("DROPPED") / plain / picture (\x01) / plain
    picture_offset = 16
    dropped, pic = text.index("DROPPED"), text.index("\x01")
    runs = [(0, b""), (dropped, b"\x00\x08\x01"), (dropped + 7, b""),
            (pic, b"\x03\x6a" + struct.pack("<I", picture_offset) + b"\x55\x08\x01"), (pic + 1, b"")]
    chpx = bytearray(512)
    chpx_fcs = [fc_of(cp) for cp, _ in runs] + [fc_of(len(text))]
    struct.pack_into(f"<{len(runs) + 1}I", chpx, 0, *chpx_fcs)
    cursor = 510
    for index, (_, grpprl) in enumerate(runs):
        if grpprl:
            blob = bytes([len(grpprl)]) + grpprl
            cursor -= len(blob)
            cursor -= cursor % 2
            chpx[cursor:cursor + len(blob)] = blob
            chpx[4 * (len(runs) + 1) + index] = cursor // 2
    chpx[511] = len(runs)

    wd = bytearray(b"\0" * (papx_pn * 512)) + page + chpx
    wd[TEXT_FC:TEXT_FC + len(raw)] = raw

    def std(sti: int, name: str) -> bytes:
        body = struct.pack("<H", sti) + b"\0" * 8 + struct.pack("<H", len(name)) + name.encode("utf-16-le") + b"\0\0"
        return struct.pack("<H", len(body)) + body

    stshi = struct.pack("<HHHHHH", 2, 10, 1, 100, 15, 0) + b"\0" * 6
    stsh = struct.pack("<H", len(stshi)) + stshi + std(0, "Normal") + std(1, "heading 1")
    piece_table = clx([(0, len(text), TEXT_FC)], with_prc=False)
    bte_papx = struct.pack("<III", TEXT_FC, TEXT_FC + len(raw), papx_pn)
    bte_chpx = struct.pack("<III", TEXT_FC, TEXT_FC + len(raw), chpx_pn)
    table = stsh + bte_papx + bte_chpx + piece_table
    offset = len(stsh)
    pairs = {1: (0, len(stsh)), 13: (offset, 12), 12: (offset + 12, 12), 33: (offset + 24, len(piece_table))}
    fib = word_fib(len(text), pairs)
    wd[:len(fib)] = fib
    data = b"\0" * picture_offset + officeart_picture(TINY_PNG)
    return build_cfb({"WordDocument": bytes(wd), "1Table": table, "Data": data}).bytes()


WORD95_TEXT = "Ch\u01b0\u01a1ng 1\r\u0110\u00e2y l\u00e0 n\u00f4\u0323i dung.\rCh\u01b0\u01a1ng 2\rH\u00ea\u0301t.\r"


def build_word95(complex_pieces: bool = False) -> bytes:
    """Word 6/95 (nFib 101, Vietnamese lid -> cp1258): contiguous text or an 8-bit piece table."""
    raw = WORD95_TEXT.encode("cp1258")
    wd = bytearray(0x400) + raw
    wd += b"\0" * max(0, 4600 - len(wd))
    flags = 0x0004 if complex_pieces else 0
    struct.pack_into("<HHHHHH", wd, 0, 0xA5EC, 0x65, 0, 0x042A, 0, flags)
    struct.pack_into("<II", wd, 0x18, 0x400, 0x400 + len(raw))
    struct.pack_into("<I", wd, 0x34, len(WORD95_TEXT))
    if complex_pieces:
        split = WORD95_TEXT.index("\r") + 1
        table = clx([(0, split, 0x400), (split, len(WORD95_TEXT), 0x400 + len(WORD95_TEXT[:split].encode("cp1258")))])
        clx_at = len(wd)
        wd += table
        struct.pack_into("<II", wd, 0x160, clx_at, len(table))
    return build_cfb({"WordDocument": bytes(wd)}).bytes()


# ================================================================ HWP synthesis

def hwp_record(tag: int, level: int, payload: bytes) -> bytes:
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), len(payload)) + payload
    return struct.pack("<I", tag | (level << 10) | (len(payload) << 20)) + payload


def hwp_units(*items) -> bytes:
    """Encode text and control characters: str -> UTF-16, ('ext', code) / ('inline', code) -> 8 units."""
    out = b""
    for item in items:
        if isinstance(item, str):
            out += item.encode("utf-16-le")
        elif isinstance(item, int):
            out += struct.pack("<H", item)
        else:
            _, code = item
            out += struct.pack("<H", code) + b"tbl \0\0\0\0\0\0\0\0" + struct.pack("<H", code)
    return out


def hwp_para(level: int, style: int, *items) -> bytes:
    header = struct.pack("<IIHBB", 0, 0, 0, style, 0) + b"\0" * 12
    return hwp_record(hwp.TAG_PARA_HEADER, level, header) + hwp_record(hwp.TAG_PARA_TEXT, level + 1, hwp_units(*items))


def hwp_ctrl(level: int, ctrl: str) -> bytes:
    return hwp_record(hwp.TAG_CTRL_HEADER, level, ctrl.encode("latin-1")[::-1] + b"\0" * 8)


def hwp_wstr(text: str) -> bytes:
    return struct.pack("<H", len(text)) + text.encode("utf-16-le")


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
LONG_KOREAN = "가나다라마바사 " * 700  # > 0xFFF bytes: extended record size


def raw_deflate(data: bytes) -> bytes:
    engine = zlib.compressobj(9, zlib.DEFLATED, -15)
    return engine.compress(data) + engine.flush()


def build_hwp(flags: int = 1, with_picture: bool = True) -> bytes:
    compressed = bool(flags & 1)
    section0 = b"".join([
        hwp_para(0, 2, "제1장 시작", 13),
        hwp_para(0, 0, "Hello", ("inline", 9), "World", 10, "next line", ("ext", 11), " tail", 30, "nb", 13),
        hwp_ctrl(1, "head"),
        hwp_record(hwp.TAG_LIST_HEADER, 2, b"\0" * 8),
        hwp_para(2, 0, "HEADER TEXT SHOULD BE DROPPED", 13),
        hwp_para(0, 0, "Field ", ("ext", 3), "result", ("inline", 4), " done", 24, "x", 31, "y", 13),
        hwp_para(0, 0, "Table follows", ("ext", 11), 13),
        hwp_ctrl(1, "tbl "),
        hwp_record(hwp.TAG_TABLE, 2, struct.pack("<IHHH", 0, 2, 2, 0) + b"\0" * 8 + struct.pack("<HH", 2, 2) + b"\0" * 4),
        *[hwp_record(hwp.TAG_LIST_HEADER, 2, b"\0" * 8) + hwp_para(2, 0, cell, 13) for cell in ("c11", "c12", "c21", "c22")],
        hwp_para(0, 0, "After table", 13),
        hwp_para(0, 0, LONG_KOREAN, 13),
    ])
    picture_records = b""
    if with_picture:
        picture_payload = b"\0" * 71 + struct.pack("<H", 1) + b"\0" * 10
        picture_records = (hwp_para(0, 0, "Picture:", ("ext", 11), 13) + hwp_ctrl(1, "gso ")
                           + hwp_record(0x4C, 2, b"\0" * 20)
                           + hwp_record(hwp.TAG_SHAPE_PICTURE, 3, picture_payload))
    section1 = hwp_para(0, 2, "제2장 끝", 13) + picture_records + hwp_para(0, 0, "Bye.", 13)
    docinfo = b"".join([
        hwp_record(hwp.TAG_STYLE, 0, hwp_wstr("바탕글") + hwp_wstr("Normal") + b"\0" * 10),
        hwp_record(hwp.TAG_STYLE, 0, hwp_wstr("본문") + hwp_wstr("Body") + b"\0" * 10),
        hwp_record(hwp.TAG_STYLE, 0, hwp_wstr("개요 1") + hwp_wstr("Outline 1") + b"\0" * 10),
        hwp_record(hwp.TAG_BIN_DATA, 0, struct.pack("<HH", 1, 1) + hwp_wstr("png")),
    ])

    def pack(data: bytes) -> bytes:
        return raw_deflate(data) if compressed else data

    header = hwp.SIGNATURE.ljust(32, b"\0") + struct.pack("<II", 0x05000300, flags) + b"\0" * 216
    streams = {
        "FileHeader": header,
        "DocInfo": pack(docinfo),
        "BodyText/Section0": pack(section0),
        "BodyText/Section1": pack(section1),
        "\x05HwpSummaryInformation": summary("한글 문서", "홍길동", wide=True),
        "PrvText": "preview".encode("utf-16-le"),
    }
    if with_picture:
        streams["BinData/BIN0001.png"] = pack(TINY_PNG)
    return build_cfb(streams).bytes()


# ================================================================ tests

class TempDirMixin:
    def setUp(self) -> None:  # noqa: D401 - unittest hook
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path


class CompoundFileTests(TempDirMixin, unittest.TestCase):
    def test_lists_reads_regular_and_mini_streams(self) -> None:
        big = bytes(range(256)) * 40  # 10240 bytes: regular sectors
        small = b"mini stream payload" * 10  # mini stream
        image = build_cfb({"Big": big, "Small": small, "Store/Inner": b"x" * 5000, "Store/Tiny": b"t"})
        cfb = CompoundFile(image.bytes())
        self.assertEqual(sorted(cfb.list_streams()), ["Big", "Small", "Store/Inner", "Store/Tiny"])
        self.assertEqual(cfb.list_storages(), ["Store"])
        self.assertEqual(cfb.read("Big"), big)
        self.assertEqual(cfb.read("small"), small)  # names are case-insensitive
        self.assertEqual(cfb.read("Store/Inner"), b"x" * 5000)
        self.assertEqual(cfb.read("Store/Tiny"), b"t")
        self.assertTrue(cfb.exists("Store") and cfb.exists("store/inner") and not cfb.exists("Nope"))
        with self.assertRaises(FormatError):
            cfb.read("Big", limit=100)
        with self.assertRaises(FormatError):
            cfb.read("Missing")

    def test_difat_sector(self) -> None:
        image = build_cfb({"A": b"a" * 6000, "B": b"b" * 10}, use_difat_sector=True)
        cfb = CompoundFile(image.bytes())
        self.assertEqual(cfb.read("A"), b"a" * 6000)
        self.assertEqual(cfb.read("B"), b"b" * 10)
        looped = bytearray(image.bytes())
        struct.pack_into("<I", looped, 0x48, 3)  # claims 3 DIFAT sectors: the chain ends early -> fine
        self.assertEqual(CompoundFile(bytes(looped)).read("A"), b"a" * 6000)
        difat_sid = struct.unpack_from("<I", looped, 0x44)[0]
        struct.pack_into("<I", looped, (difat_sid + 1) * 512 + 508, difat_sid)  # next DIFAT -> itself
        with self.assertRaises(FormatError):
            CompoundFile(bytes(looped))

    def test_version_4_sectors(self) -> None:
        big = os.urandom(20000)
        image = build_cfb({"Big": big, "Small": b"small", "S/T": b"t" * 4096}, version=4)
        cfb = CompoundFile(image.bytes())
        self.assertEqual(cfb.sector_size, 4096)
        self.assertEqual((cfb.read("Big"), cfb.read("Small"), cfb.read("S/T")), (big, b"small", b"t" * 4096))
        image.set_fat(image.starts["Big"] + 1, image.starts["Big"])
        with self.assertRaises(FormatError):
            CompoundFile(image.bytes()).read("Big")
        hwp_v4 = self.write("v4.hwp", build_cfb({path: CompoundFile(build_hwp()).read(path)
                                                for path in CompoundFile(build_hwp()).list_streams()}, version=4).bytes())
        self.assertEqual(convert(hwp_v4).chapters[0].title, "제1장 시작")

    def test_fat_self_loop_is_detected(self) -> None:
        image = build_cfb({"Loop": b"L" * 9000})
        start = image.starts["Loop"]
        image.set_fat(start, start)
        cfb = CompoundFile(image.bytes())
        began = time.monotonic()
        with self.assertRaises(FormatError):
            cfb.read("Loop")
        self.assertLess(time.monotonic() - began, 2)
        image2 = build_cfb({"Loop": b"L" * 9000})
        image2.set_fat(image2.starts["Loop"] + 2, image2.starts["Loop"])  # longer cycle
        with self.assertRaises(FormatError):
            CompoundFile(image2.bytes()).read("Loop")

    def test_directory_chain_loop_and_tree_cycle(self) -> None:
        image = build_cfb({"A": b"a" * 100})
        image.set_fat(image.dir_start, image.dir_start)
        with self.assertRaises(FormatError):
            CompoundFile(image.bytes())
        image = build_cfb({"A": b"a" * 100, "Store/B": b"b" * 100})
        store = image.entry_ids["Store"]
        # Store's child -> root and a sibling pointing at itself: the walk must terminate.
        offset = image.entry_offset(image.entry_ids["Store/B"])
        struct.pack_into("<III", image.data, offset + 68, image.entry_ids["Store/B"], store, 0)
        cfb = CompoundFile(image.bytes())
        self.assertIn("A", cfb.list_streams())
        self.assertIn("Store/B", cfb.list_streams())

    def test_corrupt_headers(self) -> None:
        good = build_cfb({"A": b"a" * 5000}).bytes()
        for data in (b"", b"garbage" * 100, good[:300], good[:600], cfb_module.MAGIC + b"\0" * 600):
            with self.assertRaises(FormatError):
                CompoundFile(data).read("A")
        bad_order = bytearray(good)
        struct.pack_into("<H", bad_order, 0x1C, 0xFEFF)
        with self.assertRaises(FormatError):
            CompoundFile(bytes(bad_order))
        bad_shift = bytearray(good)
        struct.pack_into("<H", bad_shift, 0x1E, 7)
        with self.assertRaises(FormatError):
            CompoundFile(bytes(bad_shift))
        bad_fat = bytearray(good)
        struct.pack_into("<I", bad_fat, 0x4C, 99999)
        with self.assertRaises(FormatError):
            CompoundFile(bytes(bad_fat))

    def test_property_sets(self) -> None:
        props = read_property_set(summary("Tiêu đề", "Tác giả", wide=True))
        self.assertEqual(props[2], "Tiêu đề")
        self.assertEqual(props[4], "Tác giả")
        props = read_property_set(property_set({1: (2, struct.pack("<h", 1258)), 2: (0x1E, lpstr("\u0110\u01b0\u01a1\u0300ng", "cp1258")),
                                                4: (0x1E, lpstr("x", "cp1258")), 7: (3, struct.pack("<i", -5))}))
        self.assertEqual(props[2], "\u0110\u01b0\u01a1\u0300ng")  # cp1258: base letters + combining tone marks
        self.assertEqual(props[7], -5)
        with self.assertRaises(FormatError):
            read_property_set(b"\0" * 10)
        # Offsets pointing outside the stream are skipped, not fatal.
        broken = bytearray(summary("T", "A"))
        struct.pack_into("<I", broken, 48 + 8 + 4, 0x7FFFFFF0)
        self.assertNotIn(1, read_property_set(bytes(broken)))


class WordBinaryTests(TempDirMixin, unittest.TestCase):
    def test_pieces_fields_special_characters(self) -> None:
        path = self.write("novel.doc", build_word_doc())
        document = convert(path)
        self.assertEqual(document.title, "Sample Novel")
        self.assertEqual(document.authors, ["Nguyen Van A"])
        self.assertEqual(document.source_format, "doc")
        titles = [chapter.title for chapter in document.chapters]
        self.assertEqual(titles, ["Sample Novel", "CHAPTER 1", "Chương 2: Tiếng Việt"])
        intro, first, second = (chapter.html for chapter in document.chapters)
        self.assertIn("Intro paragraph “quoted” text.", intro)
        self.assertIn("See the link now.<br/>Second line", first)
        self.assertNotIn("HYPERLINK", first)
        self.assertIn("<table><tbody><tr><td>Cell A</td><td>Cell B</td></tr></tbody></table>", first)
        self.assertIn("Ngày xửa ngày xưa có một nàng công chúa.", second)
        self.assertIn("optional non‑breaking space end</p><p>After break</p>", second)
        self.assertNotIn("PAGE", second)
        for chapter in document.chapters:
            self.assertNotRegex(chapter.html, "[\x00-\x08\x0b\x0c\x0e-\x1f]")

    def test_styles_table_marks_deletions_and_pictures(self) -> None:
        document = convert(self.write("styled.doc", build_styled_doc()))
        self.assertEqual([chapter.title for chapter in document.chapters], ["Phần 1", "Heading One", "Heading Two"])
        self.assertEqual(document.chapters[0].html, "<p>Title Para</p>")
        self.assertEqual(len(document.resources), 1)
        self.assertEqual(document.resources[0].data, TINY_PNG)
        name = document.resources[0].name
        self.assertEqual(document.chapters[1].html,
                         f'<p>Body one.</p><p>Pic <img src="{name}" alt=""/> here</p>'
                         "<table><tbody><tr><td>A1</td><td>B1</td></tr></tbody></table>")
        self.assertEqual(document.chapters[2].html, "<p>Body two.</p>")

    def test_word_95(self) -> None:
        for complex_pieces in (False, True):
            with self.subTest(complex_pieces=complex_pieces):
                document = convert(self.write(f"w95_{complex_pieces}.doc", build_word95(complex_pieces)))
                self.assertEqual([chapter.title for chapter in document.chapters], ["Chương 1", "Chương 2"])
                self.assertEqual(document.chapters[0].html, "<p>\u0110\u00e2y l\u00e0 n\u00f4\u0323i dung.</p>")
                self.assertEqual(document.chapters[1].html, "<p>H\u00ea\u0301t.</p>")

    def test_mutated_inputs_only_raise_format_error(self) -> None:
        import random

        rng = random.Random(20261128)
        seeds = [(msdoc, build_word_doc()), (msdoc, build_styled_doc()), (hwp, build_hwp(1)), (hwp, build_hwp(0))]
        for module, seed in seeds:
            streams = {path: CompoundFile(seed).read(path) for path in CompoundFile(seed).list_streams()}
            for case in range(60):
                mutated = dict(streams)
                key = rng.choice(sorted(mutated))
                blob = bytearray(mutated[key])
                for _ in range(rng.choice([1, 4, 16])):
                    if blob:
                        position = rng.randrange(len(blob))
                        blob[position:position + 4] = rng.choice(
                            [bytes([rng.randrange(256)]), b"\xff\xff\xff\xff", b"\0\0\0\0", b"\xff\xff\xff\x7f", b""])
                mutated[key] = bytes(blob)
                path = self.write(f"fuzz{case}.{'hwp' if module is hwp else 'doc'}", build_cfb(mutated).bytes())
                began = time.monotonic()
                try:
                    module.convert(path)
                except FormatError:
                    pass
                self.assertLess(time.monotonic() - began, 5)

    def test_dot_and_wps_and_table_stream_choice(self) -> None:
        dot = convert(self.write("template.dot", build_word_doc(flags=0x0001)))  # fDot, 0Table
        self.assertEqual(dot.source_format, "dot")
        self.assertEqual(dot.chapters[1].title, "CHAPTER 1")
        wps = convert(self.write("kingsoft.wps", build_word_doc()))
        self.assertEqual(wps.source_format, "wps")
        self.assertEqual(len(wps.chapters), 3)

    def test_encrypted_flag(self) -> None:
        with self.assertRaisesRegex(FormatError, "encrypted"):
            convert(self.write("locked.doc", build_word_doc(flags=0x0200 | 0x0100)))
        with self.assertRaisesRegex(FormatError, "encrypted"):
            convert(self.write("agile.doc", build_cfb({"EncryptionInfo": b"x" * 10, "EncryptedPackage": b"y" * 10}).bytes()))

    def test_wrong_real_types(self) -> None:
        cases = {
            "rtf.doc": (b"{\\rtf1\\ansi hello}", "RTF"),
            "page.doc": (b"<html><body><p>x</p></body></html>", "HTML"),
            "zip.doc": (b"PK\x03\x04" + b"\0" * 100, "ZIP"),
            "junk.doc": (b"\x00\x01\x02 not a document" * 10, "not a Word"),
            "notes.doc": ("Chương 1\nNội dung văn bản thuần.\n".encode("utf-8"), "plain text"),
            "mail.doc": (b"MIME-Version: 1.0\r\nContent-Type: multipart/related\r\n", "MHTML"),
            "word2.doc": (b"\xdb\xa5" + b"\0" * 200, "Word 2.0"),
        }
        for name, (data, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(FormatError, message):
                msdoc.convert(self.write(name, data))
        with self.assertRaisesRegex(FormatError, "Excel"):
            convert(self.write("book.doc", build_cfb({"Workbook": b"\0" * 100}).bytes()))

    def test_corrupt_word_streams(self) -> None:
        good = build_word_doc()
        with self.assertRaises(FormatError):
            convert(self.write("trunc.doc", good[:1500]))
        cfb_bytes = build_cfb({"WordDocument": b"\xec\xa5" + b"\0" * 100, "1Table": b""}).bytes()
        with self.assertRaises(FormatError):
            convert(self.write("tiny.doc", cfb_bytes))
        # piece table pointing far outside the streams
        raw_table = clx([(0, 10, 0x7FFFFFF0)])
        wd = bytearray(word_fib(10, {33: (0, len(raw_table))})) + b"\0" * 5000
        with self.assertRaisesRegex(FormatError, "no readable"):
            convert(self.write("outside.doc", build_cfb({"WordDocument": bytes(wd), "1Table": raw_table}).bytes()))
        with self.assertRaises(FormatError):
            convert(self.write("badclx.doc", build_cfb({"WordDocument": bytes(wd), "1Table": b"\x03garbage"}).bytes()))

    def test_works_wps(self) -> None:
        text = "Microsoft Works document text.\rSecond paragraph of the Works file.\r"
        contents = b"CHNKWKS \x00\x00\x00\x00" + b"\x01\x02" * 64 + text.encode("utf-16-le") + b"\0" * 64
        document = convert(self.write("works.wps", build_cfb({"CONTENTS": contents, "MatOST": b"\0" * 16}).bytes()))
        self.assertIn("Microsoft Works document text.", document.chapters[0].html)
        self.assertIn("Second paragraph of the Works file.", document.chapters[0].html)
        with self.assertRaisesRegex(FormatError, "Works"):
            convert(self.write("empty.wps", build_cfb({"CONTENTS": b"\x01\x02\x03" * 50, "SPELLING": b"x"}).bytes()))


class HwpTests(TempDirMixin, unittest.TestCase):
    def check_document(self, document) -> None:
        self.assertEqual(document.title, "한글 문서")
        self.assertEqual(document.authors, ["홍길동"])
        self.assertEqual(document.language, "ko")
        self.assertEqual([chapter.title for chapter in document.chapters], ["제1장 시작", "제2장 끝"])
        first, second = document.chapters[0].html, document.chapters[1].html
        self.assertIn("<p>Hello World<br/>next line tail nb</p>", first)
        self.assertIn("<p>Field result done-x y</p>", first)
        self.assertNotIn("HEADER TEXT", first)
        self.assertIn("<p>Table follows</p><table><tbody><tr><td>c11</td><td>c12</td></tr>"
                      "<tr><td>c21</td><td>c22</td></tr></tbody></table><p>After table</p>", first)
        self.assertIn(LONG_KOREAN.strip(), first)
        self.assertIn("<p>Bye.</p>", second)
        for chapter in document.chapters:
            self.assertNotRegex(chapter.html, "[\x00-\x08\x0b\x0c\x0e-\x1f]")

    def test_compressed_sections_with_picture(self) -> None:
        document = convert(self.write("doc.hwp", build_hwp(flags=1)))
        self.check_document(document)
        self.assertEqual(len(document.resources), 1)
        self.assertEqual(document.resources[0].media_type, "image/png")
        self.assertIn(f'<p>Picture:<img src="{document.resources[0].name}" alt=""/></p>', document.chapters[1].html)

    def test_uncompressed_sections(self) -> None:
        document = convert(self.write("plain.hwp", build_hwp(flags=0)))
        self.check_document(document)
        self.assertEqual(len(document.resources), 1)

    def test_protected_variants(self) -> None:
        for flags, message in ((0x3, "password"), (0x5, "distribution"), (0x11, "DRM")):
            with self.subTest(flags=flags), self.assertRaisesRegex(FormatError, message):
                convert(self.write(f"p{flags}.hwp", build_hwp(flags=flags)))
        with self.assertRaisesRegex(FormatError, "HWP 3.0"):
            convert(self.write("old.hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 200))

    def test_corrupt_hwp(self) -> None:
        header = hwp.SIGNATURE.ljust(32, b"\0") + struct.pack("<II", 0x05000300, 1) + b"\0" * 216
        bomb = raw_deflate(b"\0" * (1024 * 1024))
        cases = [
            build_cfb({"FileHeader": header, "BodyText/Section0": b"not deflate at all \xff\xfe" * 10}).bytes(),
            build_cfb({"FileHeader": header}).bytes(),
            build_cfb({"FileHeader": b"Something else".ljust(256, b"\0"), "BodyText/Section0": b""}).bytes(),
            build_cfb({"FileHeader": hwp.SIGNATURE.ljust(32, b"\0") + struct.pack("<II", 0x04000000, 0),
                       "BodyText/Section0": b""}).bytes(),
            build_hwp()[:2000],
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(FormatError):
                convert(self.write(f"bad{index}.hwp", data))
        # truncated records and garbage record sizes inside a valid container are tolerated
        records = hwp_para(0, 0, "Readable", 13) + struct.pack("<I", 0x43 | (0xFFF << 20)) + b"\xff\xff\xff\x7f"
        data = build_cfb({"FileHeader": header, "BodyText/Section0": raw_deflate(records)}).bytes()
        self.assertIn("Readable", convert(self.write("trunc.hwp", data)).chapters[0].html)
        old_limit = hwp.SECTION_LIMIT
        try:
            hwp.SECTION_LIMIT = 16384
            data = build_cfb({"FileHeader": header, "BodyText/Section0": bomb}).bytes()
            with self.assertRaisesRegex(FormatError, "size limit"):
                convert(self.write("bomb.hwp", data))
        finally:
            hwp.SECTION_LIMIT = old_limit

    def test_para_text_controls(self) -> None:
        payload = hwp_units("A", ("ext", 2), "B", ("inline", 9), "C", 10, "D", 0, "E", 13)
        self.assertEqual(hwp.para_text(payload), "AB\tC\nDE")
        self.assertEqual(hwp.para_text(b"\x41\x00\x42"), "A")  # odd trailing byte ignored
        self.assertEqual(hwp.para_text(hwp_units(("ext", 11))[:6]), "")  # truncated control


ODT_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/" office:version="1.2"><office:body><office:text>
<text:tracked-changes><text:changed-region text:id="ct1"><text:deletion><office:change-info><dc:creator>A</dc:creator>
<dc:date>2020-01-01T00:00:00</dc:date></office:change-info><text:p>REMOVED </text:p></text:deletion></text:changed-region>
</text:tracked-changes>
<text:p>Preface.</text:p>
<text:h text:outline-level="1">First part</text:h>
<text:p>Image <draw:frame draw:name="i1" text:anchor-type="as-char" svg:width="1cm" svg:height="1cm">
<draw:image xlink:href="Pictures/p.png" xlink:type="simple"/></draw:frame> inline, kept <text:change text:change-id="ct1"/>text.</text:p>
<text:h text:outline-level="2">Sub part</text:h><text:p>More.</text:p>
<text:h text:outline-level="1">Second part</text:h><text:p>End.</text:p>
</office:text></office:body></office:document-content>"""


@unittest.skipUnless(shutil.which("soffice"), "LibreOffice (soffice) is not installed")
class LibreOfficeRoundTripTests(TempDirMixin, unittest.TestCase):
    def soffice_to_doc(self, source: Path, *options: str) -> Path:
        env = dict(os.environ, HOME=str(self.root))
        command = ["soffice", f"-env:UserInstallation=file://{self.root / 'profile'}", "--headless", *options,
                   "--convert-to", "doc:MS Word 97", str(source), "--outdir", str(self.root)]
        try:
            subprocess.run(command, check=False, capture_output=True, timeout=180, env=env)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("soffice did not run")
        target = source.with_suffix(".doc")
        if not target.exists():
            self.skipTest("soffice produced no .doc")
        return target

    def test_real_document_with_picture_styles_and_tracked_deletion(self) -> None:
        import zipfile

        source = self.root / "parts.odt"
        manifest = (
            '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
            'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            '<manifest:file-entry manifest:full-path="Pictures/p.png" manifest:media-type="image/png"/>'
            "</manifest:manifest>")
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
            archive.writestr("META-INF/manifest.xml", manifest)
            archive.writestr("content.xml", ODT_CONTENT)
            archive.writestr("Pictures/p.png", TINY_PNG)
        document = convert(self.soffice_to_doc(source))
        self.assertEqual([chapter.title for chapter in document.chapters], ["Phần 1", "First part", "Second part"])
        self.assertEqual(len(document.resources), 1)
        self.assertEqual(document.resources[0].media_type, "image/png")
        first = document.chapters[1].html
        self.assertIn(f'Image <img src="{document.resources[0].name}" alt=""/> inline, kept text.', first)
        self.assertNotIn("REMOVED", first)
        self.assertIn("<h3>Sub part</h3><p>More.</p>", first)

    def test_real_word_97_document(self) -> None:
        source = self.write("story.html", (
            '<html><head><meta charset="utf-8"><title>Truyện</title></head><body>'
            "<p>Lời nói đầu.</p><h1>Chương 1: Mở đầu</h1><p>Tiếng Việt có dấu: ắ ằ ẳ ẵ ặ ơ ư đ.</p>"
            '<table border="1"><tr><td>A1</td><td>B1</td></tr><tr><td>A2</td><td>B2</td></tr></table>'
            "<h1>Chương 2: Kết</h1><p>中文 한국어</p></body></html>").encode("utf-8"))
        document = convert(self.soffice_to_doc(source, "--infilter=HTML (StarWriter)"))
        self.assertEqual(document.title, "Truyện")
        titles = [chapter.title for chapter in document.chapters]
        self.assertEqual(titles[-2:], ["Chương 1: Mở đầu", "Chương 2: Kết"])
        body = "".join(chapter.html for chapter in document.chapters)
        self.assertIn("Lời nói đầu.", body)
        self.assertIn("Tiếng Việt có dấu: ắ ằ ẳ ẵ ặ ơ ư đ.", body)
        self.assertIn("<td>A1</td><td>B1</td></tr><tr><td>A2</td><td>B2</td>", body)
        self.assertIn("中文 한국어", body)


if __name__ == "__main__":
    unittest.main()
