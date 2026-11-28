"""Tests for the legacy e-book converters (MOBI / PRC / AZW / AZW3, UMD, CHM) on synthetic fixtures.

Every fixture is built here at test time: a PalmDB / MOBI / KF8 writer (PalmDOC
compressor, HUFF/CDIC dictionary, INDX/TAGX/CNCX indexes), a UMD writer and a
CHM writer whose compressed section comes from a tiny test-only LZX encoder.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from lynxx import formats
from lynxx.formats import FormatError, chm, mobi, umd

NULL = 0xFFFFFFFF


def tiny_png(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b""))


FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32 + b"\xff\xd9"


# ---------------------------------------------------------------- MOBI writer

def palmdoc_compress(data: bytes) -> bytes:
    """Greedy PalmDOC LZ77 (back-references, space+char pairs, escaped high bytes)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        best = 0
        distance = 0
        for length in range(min(10, n - i), 2, -1):
            index = data.rfind(data[i:i + length], max(0, i - 2047), i)
            if index >= 0:
                best, distance = length, i - index
                break
        if best:
            out += struct.pack(">H", 0x8000 | (distance << 3) | (best - 3))
            i += best
            continue
        c = data[i]
        if c == 0x20 and i + 1 < n and 0x40 <= data[i + 1] <= 0x7F:
            out.append(data[i + 1] ^ 0x80)
            i += 2
        elif c == 0 or 0x09 <= c <= 0x7F:
            out.append(c)
            i += 1
        else:
            j = i
            while j < n and j - i < 8 and not (data[j] == 0 or 0x09 <= data[j] <= 0x7F):
                j += 1
            out.append(j - i)
            out += data[i:j]
            i = j
    return bytes(out)


def build_palmdb(records: list[bytes], ident: bytes = b"BOOKMOBI", name: bytes = b"Test_Book") -> bytes:
    header = bytearray(78)
    header[:len(name)] = name
    header[60:68] = ident
    struct.pack_into(">H", header, 76, len(records))
    offset = 78 + 8 * len(records) + 2
    table = bytearray()
    for index, record in enumerate(records):
        table += struct.pack(">LL", offset, 2 * index)
        offset += len(record)
    return bytes(header) + bytes(table) + b"\0\0" + b"".join(records)


def exth_block(items: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(struct.pack(">LL", kind, len(value) + 8) + value for kind, value in items)
    data = b"EXTH" + struct.pack(">LL", 12 + len(body), len(items)) + body
    return data + b"\0" * (-len(data) % 4)


def mobi_record0(*, compression: int, text_length: int, text_records: int, encryption: int = 0, version: int = 6,
                 first_image: int = NULL, huff: tuple[int, int] = (NULL, 0), extra_flags: int = 0,
                 exth: bytes = b"", title: bytes = b"Full Name", fdst: int = NULL, ncx: int = NULL,
                 frag: int = NULL, skel: int = NULL, locale: int = 0x09) -> bytes:
    length = 0x108
    rec = bytearray(16 + length)
    struct.pack_into(">HHLHHH", rec, 0, compression, 0, text_length, text_records, 4096, encryption)
    rec[16:20] = b"MOBI"

    def put(offset: int, value: int) -> None:
        struct.pack_into(">L", rec, offset, value)

    put(20, length)
    put(0x18, 2)
    put(0x1C, 65001)
    put(0x20, 0xC0FFEE)
    put(0x24, version)
    for offset in range(0x28, 0x54, 4):
        put(offset, NULL)
    put(0x5C, locale)
    put(0x68, version)
    put(0x6C, first_image)
    put(0x70, huff[0])
    put(0x74, huff[1])
    put(0x80, 0x50 if exth else 0)
    put(0xC0, fdst)
    put(0xC4, 1)
    struct.pack_into(">H", rec, 0xF2, extra_flags)
    put(0xF4, ncx)
    put(0xF8, frag)
    put(0xFC, skel)
    put(0x100, NULL)
    put(0x104, NULL)
    rec += exth
    put(0x54, len(rec))
    put(0x58, len(title))
    rec += title + b"\0\0"
    return bytes(rec)


def text_records(text: bytes, size: int, compress: bool, extra_flags: int) -> list[bytes]:
    """Calibre-style split: `size` bytes per record, multibyte overlap + TBS entry as trailing data."""
    records = []
    pos = 0
    while pos < len(text):
        chunk = text[pos:pos + size]
        end = pos + len(chunk)
        overlap = b""
        while end + len(overlap) < len(text) and 0x80 <= text[end + len(overlap)] < 0xC0 and len(overlap) < 3:
            overlap += text[end + len(overlap):end + len(overlap) + 1]
        record = palmdoc_compress(chunk) if compress else chunk
        if extra_flags & 1:
            record += overlap + bytes([len(overlap)])
        if extra_flags & 2:
            record += b"\x01\x02\x83"  # a 3-byte trailing entry whose size byte is 0x80 | 3
        records.append(record)
        pos = end
    return records


def vwi(value: int) -> bytes:
    groups = [value & 0x7F]
    value >>= 7
    while value:
        groups.append(value & 0x7F)
        value >>= 7
    groups.reverse()
    groups[-1] |= 0x80
    return bytes(groups)


INDX_HEADER = 0xC0


def indx_header(start: int, count: int, total: int = 0, nctoc: int = 0) -> bytearray:
    header = bytearray(INDX_HEADER)
    header[:4] = b"INDX"
    struct.pack_into(">13L", header, 4, INDX_HEADER, 0, 0, 0, start, count, 65001, NULL, total, 0, 0, 0, nctoc)
    return header


def index_records(tags: list[tuple[int, int, int, int]], entries: list[tuple[bytes, dict[int, list[int]]]],
                  strings: list[bytes] | None = None) -> list[bytes]:
    """[main INDX, one data INDX, optional CNCX]; tag values may reference CNCX by `("cncx", n)` markers."""
    cncx = bytearray()
    offsets = []
    for value in strings or []:
        offsets.append(len(cncx))
        cncx += vwi(len(value)) + value
    table = tags + [(0, 0, 0, 1)]
    main = indx_header(0, 1, len(entries), 1 if strings else 0)
    main += b"TAGX" + struct.pack(">LL", 12 + 4 * len(table), 1) + b"".join(bytes(t) for t in table)
    data = indx_header(0, len(entries))
    positions = []
    for key, values in entries:
        positions.append(len(data))
        control = 0
        payload = bytearray()
        for tag, per_entry, mask, _end in tags:
            if tag not in values:
                continue
            count = len(values[tag]) // per_entry
            shift = (mask & -mask).bit_length() - 1
            control |= (count << shift) & mask
            for value in values[tag]:
                payload += vwi(offsets[value[1]] if isinstance(value, tuple) else value)
        data += bytes([len(key)]) + key + bytes([control]) + payload
    idxt = len(data)
    data += b"IDXT" + struct.pack(f">{len(positions)}H", *positions)
    struct.pack_into(">L", data, 20, idxt)
    records = [bytes(main), bytes(data)]
    if strings:
        records.append(bytes(cncx))
    return records


SKELETON = (b'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            b'<head><title>Ignored Title</title></head><body aid="0"></body></html>')
FRAGMENT_ONE = ('<div aid="1"><h2>Heading A</h2><p>Phần một — tiếng Việt.</p>'
                '<p><img src="kindle:embed:0001?mime=image/png" aid="2"/></p>'
                '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                '<image width="10" height="10" xlink:href="kindle:embed:0002?mime=image/png"/></svg></div>').encode()
FRAGMENT_TWO = ('<p aid="3">Part two with a <a href="kindle:pos:fid:0000:off:0000000000">link</a> '
                'and 中文.</p>').encode()


def kf8_part(images: list[bytes] | None, exth_items: list[tuple[int, bytes]], first_image: int | None = None,
             record_size: int = 97) -> list[bytes]:
    """KF8 records [header, text..., (images), SKEL, FRAG, NCX, FDST]; indexes relative to the header."""
    skel_one = SKELETON
    skel_two = SKELETON.replace(b"Ignored Title", b"Second")
    pos_two = len(skel_one) + len(FRAGMENT_ONE)
    flow = skel_one + FRAGMENT_ONE + skel_two + FRAGMENT_TWO
    css = b"p { text-indent: 1em }"
    text = flow + css
    texts = text_records(text, record_size, True, 1)
    layout_start = 1 + len(texts)
    image_records = images or []
    skel = index_records([(1, 1, 0x03, 0), (6, 2, 0x0C, 0)], [
        (b"SKEL0000000000", {1: [1], 6: [0, len(skel_one)]}),
        (b"SKEL0000000001", {1: [1], 6: [pos_two, len(skel_two)]}),
    ])
    selector = b"P-//*[@aid='0']"
    frag = index_records([(2, 1, 0x01, 0), (3, 1, 0x02, 0), (4, 1, 0x04, 0), (6, 2, 0x08, 0)], [
        (b"%010d" % skel_one.index(b"</body>"), {2: [("cncx", 0)], 3: [0], 4: [0], 6: [0, len(FRAGMENT_ONE)]}),
        (b"%010d" % (pos_two + skel_two.index(b"</body>")),
         {2: [("cncx", 1)], 3: [1], 4: [1], 6: [0, len(FRAGMENT_TWO)]}),
    ], [selector, selector])
    ncx = index_records([(1, 1, 0x01, 0), (2, 1, 0x02, 0), (3, 1, 0x04, 0), (4, 1, 0x08, 0), (6, 2, 0x10, 0)], [
        (b"0", {1: [0], 2: [10], 3: [("cncx", 0)], 4: [0], 6: [0, 0]}),
        (b"1", {1: [pos_two], 2: [10], 3: [("cncx", 1)], 4: [0], 6: [1, 0]}),
    ], ["Chương 1".encode(), "Chương 2".encode()])
    skel_at = layout_start + len(image_records)
    frag_at = skel_at + len(skel)
    ncx_at = frag_at + len(frag)
    fdst_at = ncx_at + len(ncx)
    fdst = b"FDST" + struct.pack(">LL", 12, 2) + struct.pack(">4L", 0, len(flow), len(flow), len(text))
    if first_image is None:
        first_image = layout_start if image_records else fdst_at
    header = mobi_record0(compression=2, text_length=len(text), text_records=len(texts), version=8,
                          first_image=first_image, extra_flags=1, exth=exth_block(exth_items),
                          title=b"KF8 Book", fdst=fdst_at, ncx=ncx_at, frag=frag_at, skel=skel_at)
    return [header] + texts + image_records + skel + frag + ncx + [fdst]


def mobi6_html() -> bytes:
    template = ('<html><head><guide><reference type="toc" title="TOC" filepos=0000000000 /></guide></head><body>'
                '<h1>Chương Một</h1><p>Xin chào thế giới. 你好世界。Dấu: ắ ằ ẳ ẵ ặ.</p>'
                '<p><a filepos=@@@@@@@@@@>Tới đoạn đích</a> và <a filepos=##########>tới chương hai</a></p>'
                '<p><img recindex="00001" alt="Hình một"/></p>'
                '<p>Đoạn đích ở đây.</p>'
                '<mbp:pagebreak/>'
                '<h1>Chương Hai</h1><p>Nội dung chương hai. Lặp lại, lặp lại, lặp lại, lặp lại.</p>'
                '</body></html>').encode()
    target_one = template.index("<p>Đoạn đích".encode())
    target_two = template.index("<h1>Chương Hai".encode())
    return template.replace(b"@@@@@@@@@@", b"%010d" % target_one).replace(b"##########", b"%010d" % target_two)


def build_mobi6(*, compress: bool = True, extra_flags: int = 3, encryption: int = 0, record_size: int = 61,
                with_ncx: bool = False) -> bytes:
    text = mobi6_html()
    texts = text_records(text, record_size, compress, extra_flags)
    image_index = 1 + len(texts)
    exth = exth_block([(100, "Nguyễn Văn A".encode()), (503, "Sách Thử".encode()), (524, b"vi"),
                       (201, struct.pack(">L", 0)), (103, "<p>Mô tả <b>ngắn</b></p>".encode())])
    ncx: list[bytes] = []
    if with_ncx:  # MOBI 6 NCX: tag 1 = filepos, tag 3 = label offset in the CNCX record
        second = text.index("<h1>Chương Hai".encode())
        ncx = index_records([(1, 1, 0x01, 0), (3, 1, 0x02, 0)], [
            (b"0", {1: [0], 3: [("cncx", 0)]}), (b"1", {1: [second], 3: [("cncx", 1)]}),
        ], ["Mục lục một".encode(), "Mục lục hai".encode()])
    record0 = mobi_record0(compression=2 if compress else 1, text_length=len(text), text_records=len(texts),
                           encryption=encryption, first_image=image_index, extra_flags=extra_flags, exth=exth,
                           ncx=image_index + 1 if ncx else NULL)
    return build_palmdb([record0] + texts + [tiny_png()] + ncx + [b"FLIS" + b"\0" * 32, b"\xe9\x8e\r\n"])


def huff_records() -> tuple[bytes, bytes]:
    """HUFF with 8-bit terminal codes (code byte c -> phrase 255 - c) and one CDIC with three phrases."""
    dict1 = struct.pack(">256L", *([8 | 0x80 | (255 << 8)] * 256))
    huff = b"HUFF" + struct.pack(">LLL", 0x18, 24, 24 + 1024) + b"\0" * 8 + dict1 + b"\0" * 256
    phrases = [(b"<p>Hello ", True), (b"world</p>", True), (bytes([255, 254]), False)]
    table = bytearray()
    body = bytearray()
    for data, literal in phrases:
        table += struct.pack(">H", 2 * len(phrases) + len(body))
        body += struct.pack(">H", len(data) | (0x8000 if literal else 0)) + data
    cdic = b"CDIC" + struct.pack(">LLL", 0x10, len(phrases), 2) + bytes(table) + bytes(body)
    return huff, cdic


# ---------------------------------------------------------------- UMD writer

def umd_chunk(chunk_id: int, payload: bytes, flag: int = 0) -> bytes:
    return b"#" + struct.pack("<HBB", chunk_id, flag, 5 + len(payload)) + payload


def umd_block(check: int, payload: bytes) -> bytes:
    return b"$" + struct.pack("<LL", check, 9 + len(payload)) + payload


def u16(text: str) -> bytes:
    return text.encode("utf-16-le")


UMD_CHAPTERS = [
    ("第一章 开始", "第一章 开始\u2029这是第一章的内容。\u2029Xin chào, đây là chương một.\u2029"),
    ("Chương 2", "Chương 2\r\nNội dung chương hai.\u2029第二段。"),
]


def build_umd_text() -> bytes:
    raw = "".join(body for _, body in UMD_CHAPTERS).encode("utf-16-le")
    offsets, pos = [], 0
    for _, body in UMD_CHAPTERS:
        offsets.append(pos)
        pos += len(body.encode("utf-16-le"))
    pieces = [raw[i:i + 50] for i in range(0, len(raw), 50)]
    out = bytearray(umd.MAGIC)
    out += umd_chunk(0x01, b"\x01" + struct.pack("<H", 0x1234))
    out += umd_chunk(0x02, u16("Truyện UMD 测试"))
    out += umd_chunk(0x03, u16("Tác Giả"))
    out += umd_chunk(0x07, u16("Tiểu thuyết"))
    out += umd_chunk(0x0B, struct.pack("<L", len(raw)))
    checks = []
    for index, piece in enumerate(pieces):
        checks.append(0x5000 + index)
        out += umd_block(checks[-1], zlib.compress(piece))
        if index == 1:
            out += umd_chunk(0xF1, b"\x00" * 16)
            out += umd_chunk(0x0A, struct.pack("<L", 0x7777))
    out += umd_chunk(0x81, struct.pack("<L", 0x1001), 1) + umd_block(0x1001, struct.pack(f"<{len(checks)}L", *checks))
    out += umd_chunk(0x82, b"\x01" + struct.pack("<L", 0x1002), 1) + umd_block(0x1002, FAKE_JPEG)
    out += umd_chunk(0x87, b"\x10\xa0" + struct.pack("<L", 0x1005), 1) + umd_block(0x1005, struct.pack("<3L", 0, 9, 99))
    out += umd_chunk(0x83, struct.pack("<L", 0x1003), 1) + umd_block(0x1003, struct.pack("<2L", *offsets))
    titles = b"".join(bytes([len(u16(t))]) + u16(t) for t, _ in UMD_CHAPTERS)
    out += umd_chunk(0x84, struct.pack("<L", 0x1004), 1) + umd_block(0x1004, titles)
    out += umd_chunk(0x0C, struct.pack("<L", len(out) + 9))
    return bytes(out)


def build_umd_comic() -> bytes:
    out = bytearray(umd.MAGIC)
    out += umd_chunk(0x01, b"\x02" + struct.pack("<H", 0x4321))
    out += umd_chunk(0x02, u16("Truyện tranh"))
    out += umd_chunk(0x0E, b"\x01", 1)
    out += umd_block(0x2001, tiny_png((1, 2, 3)))
    out += umd_block(0x2002, tiny_png((4, 5, 6)))
    out += umd_chunk(0x83, struct.pack("<L", 0x2003), 1) + umd_block(0x2003, struct.pack("<L", 0))
    out += umd_chunk(0x0C, struct.pack("<L", len(out) + 9))
    return bytes(out)


# ---------------------------------------------------------------- LZX test encoder

POSITION_SLOTS = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}
PRETREE = [4] * 12 + [5] * 8  # a complete code over the 20 pretree symbols


class BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> shift) & 1)
            self.n += 1
            if self.n == 16:
                self.out += struct.pack("<H", self.acc)
                self.acc = self.n = 0

    def align(self) -> None:
        if self.n:
            self.write(0, 16 - self.n)

    def take(self) -> bytes:
        assert self.n == 0
        data, self.out = bytes(self.out), bytearray()
        return data


def canonical_codes(lengths: list[int]) -> dict[int, tuple[int, int]]:
    codes = {}
    code = 0
    for length in range(1, 17):
        for symbol, value in enumerate(lengths):
            if value == length:
                codes[symbol] = (code, length)
                code += 1
        code <<= 1
    return codes


def write_lengths(writer: BitWriter, new: list[int], previous: list[int]) -> None:
    codes = canonical_codes(PRETREE)
    for length in PRETREE:
        writer.write(length, 4)
    for x in range(len(new)):
        code, size = codes[(previous[x] - new[x]) % 17]
        writer.write(code, size)


def lzx_encode(blocks: list[dict], window_bits: int = 16) -> bytes:
    """Encode block specs: {'kind': 'uncompressed', 'data'} or {'kind': 'verbatim'|'aligned', 'ops', ...}."""
    main_elements = 256 + POSITION_SLOTS[window_bits] * 8
    out = bytearray()
    writer = BitWriter()
    writer.write(0, 1)  # no E8 translation
    prev_main, prev_length = [0] * main_elements, [0] * 249
    produced = 0
    for block in blocks:
        if block["kind"] == "uncompressed":
            data = block["data"]
            writer.write(3, 3)
            writer.write(len(data) >> 8, 16)
            writer.write(len(data) & 0xFF, 8)
            writer.write(0, 16 - writer.n if writer.n else 16)
            out += writer.take() + struct.pack("<3L", *block.get("r", (1, 1, 1))) + data
            if len(data) & 1:
                out += b"\0"
            produced += len(data)
            writer = BitWriter()
            continue
        ops = block["ops"]
        size = sum(len(op["data"]) if op["op"] == "lit" else op["length"] for op in ops)
        aligned = block["kind"] == "aligned"
        writer.write(2 if aligned else 1, 3)
        writer.write(size >> 8, 16)
        writer.write(size & 0xFF, 8)
        if aligned:
            for _ in range(8):
                writer.write(3, 3)
        elements = block.get("elements", [])
        main = [0] * main_elements
        for symbol in range(256):
            main[symbol] = 9 if elements else 8
        for element in elements:
            main[element] = 4
        lengths = [0] * 249
        for symbol in block.get("length_symbols", ()):
            lengths[symbol] = 1
        write_lengths(writer, main[:256], prev_main[:256])
        write_lengths(writer, main[256:], prev_main[256:])
        write_lengths(writer, lengths, prev_length)
        prev_main, prev_length = main, lengths
        main_codes, length_codes = canonical_codes(main), canonical_codes(lengths)
        for op in ops:
            if op["op"] == "lit":
                for byte in op["data"]:
                    writer.write(*main_codes[byte])
                    produced += 1
                    if produced % 0x8000 == 0:
                        writer.align()
                continue
            writer.write(*main_codes[op["element"]])
            if "footer" in op:
                writer.write(*length_codes[op["footer"]])
            if "verbatim" in op:
                writer.write(*op["verbatim"])
            if "aligned" in op:
                writer.write(op["aligned"], 3)
            produced += op["length"]
            if produced % 0x8000 == 0:
                writer.align()
    writer.align()
    return bytes(out + writer.take())


def apply_ops(ops: list[dict], out: bytearray | None = None) -> bytes:
    out = bytearray() if out is None else out
    for op in ops:
        if op["op"] == "lit":
            out += op["data"]
        else:
            for _ in range(op["length"]):
                out.append(out[-op["offset"]])
    return bytes(out)


MATCH_ELEMENTS = [257, 266, 274, 283, 294, 295, 297, 339]


def lit(data: bytes) -> dict:
    return {"op": "lit", "data": data}


def match(element: int, offset: int, length: int, **fields) -> dict:
    return {"op": "match", "element": element, "offset": offset, "length": length, **fields}


# ---------------------------------------------------------------- CHM writer

def encint(value: int) -> bytes:
    groups = [value & 0x7F]
    value >>= 7
    while value:
        groups.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(groups))


def namelist() -> bytes:
    body = bytearray(struct.pack("<H", 2))
    for name in ("Uncompressed", "MSCompressed"):
        body += struct.pack("<H", len(name)) + name.encode("utf-16-le") + b"\0\0"
    return struct.pack("<H", (len(body) + 2) // 2) + bytes(body)


def lzx_literal_stream(data: bytes) -> bytes:
    return lzx_encode([{"kind": "verbatim", "ops": [lit(data)]}])


# {7C01FD10-7BAA-11D0-9E0C-00A0C922E6EC} and {7C01FD11-...}, as stored in every ITSF header
ITSF_GUIDS = bytes.fromhex("10fd017caa7bd0119e0c00a0c922e6ec11fd017caa7bd0119e0c00a0c922e6ec")


def build_chm(files0: dict[str, bytes], files1: dict[str, bytes] | None = None, lcid: int = 0x0409) -> bytes:
    section0 = dict(files0)
    entries: list[tuple[str, int, int, int]] = [("/", 0, 0, 0), ("/html/", 0, 0, 0)]
    if files1:
        stream = bytearray()
        for name, data in files1.items():
            entries.append((name, 1, len(stream), len(data)))
            stream += data
        compressed = bytearray()
        frame_offsets = []
        true_length = len(stream)
        stream += b"\0" * (-len(stream) % 0x8000)  # encoders pad the last frame; the reset table keeps the true length
        for index in range(0, len(stream), 0x8000):
            frame_offsets.append(len(compressed))
            group = bytes(stream[index:index + 0x8000])
            if (index // 0x8000) % 2 == 0:
                compressed += lzx_literal_stream(group)
            else:
                compressed += lzx_encode([{"kind": "uncompressed", "data": group}])
        section0["::DataSpace/NameList"] = namelist()
        section0[chm._CONTROL] = struct.pack("<L", 6) + b"LZXC" + struct.pack("<LLLLL", 2, 1, 2, 1, 0)
        section0[chm._RESET_TABLE] = (struct.pack("<LLLL", 2, len(frame_offsets), 8, 0x28)
                                      + struct.pack("<QQQ", true_length, len(compressed), 0x8000)
                                      + b"".join(struct.pack("<Q", o) for o in frame_offsets))
        section0[chm._CONTENT] = bytes(compressed)
    content = bytearray()
    for name, data in section0.items():
        entries.append((name, 0, len(content), len(data)))
        content += data
    chunk_size = 4096
    body = b"".join(encint(len(n.encode())) + n.encode() + encint(s) + encint(o) + encint(l)
                    for n, s, o, l in sorted(entries, key=lambda e: e[0].lower()))
    assert 0x14 + len(body) + 2 <= chunk_size
    free = chunk_size - 0x14 - len(body)  # free space + quickref area, which ends with the entry count
    chunk = b"PMGL" + struct.pack("<LLll", free, 0, -1, -1) + body + b"\0" * (free - 2) + struct.pack("<H", len(entries))
    itsp = bytearray(0x54)
    itsp[:4] = b"ITSP"
    struct.pack_into("<LLLLLLllllL", itsp, 4, 1, 0x54, 0x0A, chunk_size, 2, 1, -1, 0, 0, -1, 1)
    struct.pack_into("<L", itsp, 0x30, lcid)
    struct.pack_into("<Llll", itsp, 0x44, 0x54, -1, -1, -1)
    directory = bytes(itsp) + chunk
    itsf = bytearray(0x60)
    itsf[:4] = b"ITSF"
    struct.pack_into("<LLLLL", itsf, 4, 3, 0x60, 1, 0x12345678, lcid)
    itsf[0x18:0x38] = ITSF_GUIDS
    dir_offset = 0x60 + 0x18
    content_offset = dir_offset + len(directory)
    struct.pack_into("<QQQQQ", itsf, 0x38, 0x60, 0x18, dir_offset, len(directory), content_offset)
    section_header = struct.pack("<LLQLL", 0x1FE, 0, content_offset + len(content), 0, 0)
    return bytes(itsf) + section_header + directory + bytes(content)


def system_file(entries: list[tuple[int, bytes]]) -> bytes:
    return struct.pack("<L", 3) + b"".join(struct.pack("<HH", code, len(value)) + value for code, value in entries)


HHC = ('<html><head><meta charset="utf-8"></head><body>'
       '<object type="text/site properties"><param name="ImageType" value="Folder"></object><ul>'
       '<li><object type="text/sitemap"><param name="Name" value="Mở đầu"><param name="Local" value="index.htm">'
       '</object><ul><li><object type="text/sitemap"><param name="Name" value="Chương hai">'
       '<param name="Local" value="html/two.htm#sec"></object></ul>'
       '<li><object type="text/sitemap"><param name="Name" value="Lặp lại"><param name="Local" value="index.htm">'
       '</object><li><object type="text/sitemap"><param name="Name" value="Thư mục"></object></ul></body></html>')
INDEX_HTM = ('<html><head><title>Index</title><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
             '</head><body><h1>Xin chào</h1><p>Trang đầu <img src="images/pic.png" alt="ảnh"></p>'
             '<a href="html/two.htm">tiếp</a> <a href="#top">đầu</a><script>alert(1)</script></body></html>').encode()
TWO_HTM = (b"<html><body><p id='sec'>Caf\xe9 au lait</p><img src='../images/pic.png'>"
           b"<img src='/images/missing.png'><a href='http://example.com/'>web</a></body></html>")


def chm_files() -> dict[str, bytes]:
    return {"/toc.hhc": HHC.encode(), "/index.htm": INDEX_HTM, "/html/two.htm": TWO_HTM, "/images/pic.png": tiny_png()}


def chm_system() -> bytes:
    return system_file([(0, b"toc.hhc\0"), (2, b"index.htm\0"), (3, b"CHM Test Book\0"),
                        (4, struct.pack("<LLL", 0x042A, 0, 0)), (9, b"HHA Version 4.74.8702\0")])


# ---------------------------------------------------------------- tests

class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path


class MobiPrimitiveTests(unittest.TestCase):
    def test_palmdoc_round_trip_and_overlapping_back_reference(self) -> None:
        text = "Lặp lại lặp lại lặp lại — the quick brown fox jumps over the lazy dog dog dog.".encode()
        self.assertEqual(mobi.palmdoc_decompress(palmdoc_compress(text)), text)
        # "a" then a back-reference of distance 1, length 5: "aaaaaa"; then " b" as one space+char byte
        self.assertEqual(mobi.palmdoc_decompress(b"a" + struct.pack(">H", 0x8000 | (1 << 3) | 2) + bytes([0x62 ^ 0x80])),
                         b"aaaaaa b")
        self.assertEqual(mobi.palmdoc_decompress(b"\x03\xe1\xba\xbfX"), "ế".encode() + b"X")

    def test_strip_trailing_entries(self) -> None:
        text = b"payload text"
        # multibyte only: 2 overlap bytes + their count byte
        self.assertEqual(mobi.strip_trailing_entries(text + b"\xa0\xa1\x02", 0b1), text)
        # one trailing entry of 202 bytes whose size is a 2-byte backward varint (0x81 0x4A)
        entry = b"x" * 200 + bytes([0x81, 0x4A])
        self.assertEqual(mobi.strip_trailing_entries(text + entry, 0b10), text)
        # both kinds, multibyte bytes sit before the other entries
        self.assertEqual(mobi.strip_trailing_entries(text + b"\xa0\x01" + entry + b"\x01\x02\x83", 0b111), text)
        self.assertEqual(mobi.strip_trailing_entries(b"\x85", 0b10), b"")

    def test_huff_cdic_recursive_dictionary(self) -> None:
        huff, cdic = huff_records()
        reader = mobi.HuffCdic(huff, [cdic])
        self.assertEqual(reader.decompress(bytes([253, 255, 254])), b"<p>Hello world</p><p>Hello world</p>")

    def test_huff_cdic_self_reference_is_rejected(self) -> None:
        huff, _ = huff_records()
        body = struct.pack(">H", 1) + bytes([255])  # phrase 0 (compressed) expands to phrase 0
        cdic = b"CDIC" + struct.pack(">LLL", 0x10, 1, 1) + struct.pack(">H", 2) + body
        with self.assertRaises(FormatError):
            mobi.HuffCdic(huff, [cdic]).decompress(bytes([255]))

    def test_kindle_embed_base32(self) -> None:
        self.assertEqual(mobi.kindle_embed_index("kindle:embed:0001?mime=image/jpeg"), 1)
        self.assertEqual(mobi.kindle_embed_index("kindle:embed:000A"), 10)
        self.assertEqual(mobi.kindle_embed_index("kindle:embed:00V0?mime=image/png"), 31 * 32)
        self.assertIsNone(mobi.kindle_embed_index("kindle:embed:0000"))
        self.assertIsNone(mobi.kindle_embed_index("images/a.jpg"))

    def test_index_parser_reads_tagx_entries_and_cncx(self) -> None:
        records = index_records([(1, 1, 0x03, 0), (3, 1, 0x04, 0), (6, 2, 0x18, 0)], [
            (b"KEY0", {1: [7], 3: [("cncx", 1)], 6: [300, 5]}),
            (b"KEY1", {1: [2], 6: [1, 2, 3, 4]}),
        ], [b"first", "thứ hai".encode()])
        db = mobi._PalmDB(build_palmdb([b"\0" * 16] + records))
        entries, cncx = mobi._read_index(db, 1)
        self.assertEqual([key for key, _ in entries], ["KEY0", "KEY1"])
        self.assertEqual(entries[0][1], {1: [7], 3: [6], 6: [300, 5]})
        self.assertEqual(entries[1][1], {1: [2], 6: [1, 2, 3, 4]})
        self.assertEqual(cncx[6], "thứ hai".encode())


class MobiConvertTests(_TempDir):
    def test_mobi6_palmdoc_with_multibyte_and_trailing_entries(self) -> None:
        document = formats.convert(self.write("book.mobi", build_mobi6()))
        self.assertEqual(document.source_format, "mobi")
        self.assertEqual(document.title, "Sách Thử")
        self.assertEqual(document.authors, ["Nguyễn Văn A"])
        self.assertEqual(document.language, "vi")
        self.assertEqual(document.description, "Mô tả ngắn")
        self.assertEqual([c.title for c in document.chapters], ["Chương Một", "Chương Hai"])
        first = document.chapters[0].html
        self.assertIn("你好世界", first)
        self.assertIn("ắ ằ ẳ ẵ ặ", first)
        self.assertEqual(len(document.resources), 1)
        image = document.resources[0]
        self.assertEqual(image.media_type, "image/png")
        self.assertIn(f'<img src="{image.name}" alt="Hình một"/>', first)
        self.assertIs(document.cover, image)
        target = mobi6_html().index("<p>Đoạn đích".encode())
        self.assertIn(f'<a href="#filepos{target}">Tới đoạn đích</a>', first)
        self.assertIn(f'<a id="filepos{target}"></a><p>Đoạn đích ở đây.</p>', first)
        self.assertIn("<a>tới chương hai</a>", first)  # cross-chapter link: href dropped
        self.assertNotIn("guide", first)
        self.assertIn("Lặp lại, lặp lại", document.chapters[1].html)

    def test_prc_uncompressed_multibyte_only(self) -> None:
        data = build_mobi6(compress=False, extra_flags=1, record_size=33)
        document = formats.convert(self.write("book.prc", data))
        self.assertEqual(document.source_format, "prc")
        self.assertIn("你好世界", document.chapters[0].html)
        self.assertIn("Nội dung chương hai", document.chapters[1].html)

    def test_mobi6_titles_from_ncx(self) -> None:
        document = formats.convert(self.write("ncx.mobi", build_mobi6(with_ncx=True)))
        self.assertEqual([c.title for c in document.chapters], ["Mục lục một", "Mục lục hai"])

    def test_drm_is_rejected(self) -> None:
        with self.assertRaisesRegex(FormatError, "DRM"):
            formats.convert(self.write("locked.azw", build_mobi6(encryption=2)))

    def test_azw_huff_cdic(self) -> None:
        huff, cdic = huff_records()
        text = bytes([253, 255, 254])
        record0 = mobi_record0(compression=17480, text_length=36, text_records=1, huff=(2, 2),
                               exth=exth_block([(100, b"Huff Author")]))
        document = formats.convert(self.write("huff.azw", build_palmdb([record0, text, huff, cdic])))
        self.assertEqual(document.source_format, "azw")
        self.assertEqual(document.authors, ["Huff Author"])
        self.assertEqual(document.title, "Full Name")
        self.assertEqual(document.chapters[0].html, "<p>Hello world</p><p>Hello world</p>")

    def test_azw3_kf8_reassembly(self) -> None:
        records = kf8_part([tiny_png((255, 0, 0)), tiny_png((0, 0, 255))],
                           [(100, b"KF8 Writer"), (201, struct.pack(">L", 1)), (524, b"vi")])
        document = formats.convert(self.write("book.azw3", build_palmdb(records + [b"\xe9\x8e\r\n"])))
        self.assertEqual(document.source_format, "azw3")
        self.assertEqual(document.title, "KF8 Book")
        self.assertEqual([c.title for c in document.chapters], ["Chương 1", "Chương 2"])
        first, second = document.chapters[0].html, document.chapters[1].html
        self.assertIn("<h2>Heading A</h2><p>Phần một — tiếng Việt.</p>", first)
        names = [r.name for r in document.resources]
        self.assertEqual(len(names), 2)
        self.assertIn(f'<img src="{names[0]}"/>', first)
        self.assertIn(f'<img src="{names[1]}" alt=""/>', first)  # from the inline <svg><image>
        self.assertNotIn("aid", first)
        self.assertIs(document.cover, document.resources[1])
        self.assertIn("Part two with a <a>link</a> and 中文.", second)
        self.assertNotIn("Ignored Title", first + second)

    def test_joint_mobi6_kf8_uses_kf8_half_and_shared_images(self) -> None:
        old_text = b"<html><body><p>OLD MOBI6 TEXT</p></body></html>"
        images = [tiny_png((9, 9, 9)), tiny_png((8, 8, 8))]
        mobi6_texts = text_records(old_text, 4096, True, 0)
        boundary = 1 + len(mobi6_texts) + len(images)
        kf8 = kf8_part(None, [(100, b"Joint Author")])
        record0 = mobi_record0(compression=2, text_length=len(old_text), text_records=len(mobi6_texts),
                               first_image=1 + len(mobi6_texts),
                               exth=exth_block([(121, struct.pack(">L", boundary + 1)), (100, b"Old Author")]))
        data = build_palmdb([record0] + mobi6_texts + images + [b"BOUNDARY"] + kf8 + [b"\xe9\x8e\r\n"])
        document = formats.convert(self.write("joint.mobi", data))
        self.assertEqual([c.title for c in document.chapters], ["Chương 1", "Chương 2"])
        self.assertNotIn("OLD MOBI6", "".join(c.html for c in document.chapters))
        self.assertEqual(document.authors, ["Joint Author"])
        self.assertEqual([r.data for r in document.resources], images)

    def test_kf8_without_indexes_falls_back_to_body_split(self) -> None:
        text = b"<html><body><p>One</p></body></html><html><body><h1>T2</h1><p>Two</p></body></html>"
        record0 = mobi_record0(compression=1, text_length=len(text), text_records=1, version=8)
        with self.assertLogs("lynxx.formats.mobi", "WARNING"):
            document = formats.convert(self.write("raw.azw3", build_palmdb([record0, text])))
        self.assertEqual([c.html for c in document.chapters], ["<p>One</p>", "<h1>T2</h1><p>Two</p>"])
        self.assertEqual(document.chapters[1].title, "T2")

    def test_palmdoc_textread(self) -> None:
        text = "Chapter 1\nHello caf\xe9\nChapter 2\nWorld".encode("cp1252")
        record0 = struct.pack(">HHLHHL", 2, 0, len(text), 1, 4096, 0)
        data = build_palmdb([record0, palmdoc_compress(text)], ident=b"TEXtREAd", name=b"Plain")
        document = formats.convert(self.write("plain.prc", data))
        self.assertEqual([c.title for c in document.chapters], ["Chapter 1", "Chapter 2"])
        self.assertIn("Hello café", document.chapters[0].html)

    def test_corrupt_files(self) -> None:
        with self.assertRaises(FormatError):
            formats.convert(self.write("bad.mobi", b"\0" * 60 + b"BOOKMOBI" + b"\xff" * 20))
        record0 = mobi_record0(compression=9, text_length=1, text_records=1)
        with self.assertRaisesRegex(FormatError, "compression"):
            formats.convert(self.write("bad2.mobi", build_palmdb([record0, b"x"])))


class UmdTests(_TempDir):
    def test_text_book_with_chapters_and_cover(self) -> None:
        document = formats.convert(self.write("book.umd", build_umd_text()))
        self.assertEqual(document.source_format, "umd")
        self.assertEqual(document.title, "Truyện UMD 测试")
        self.assertEqual(document.authors, ["Tác Giả"])
        self.assertEqual([c.title for c in document.chapters], ["第一章 开始", "Chương 2"])
        self.assertEqual(document.chapters[0].html,
                         "<p>这是第一章的内容。</p><p>Xin chào, đây là chương một.</p>")
        self.assertEqual(document.chapters[1].html, "<p>Nội dung chương hai.</p><p>第二段。</p>")
        self.assertIsNotNone(document.cover)
        self.assertEqual(document.cover.media_type, "image/jpeg")

    def test_comic_book(self) -> None:
        document = formats.convert(self.write("comic.umd", build_umd_comic()))
        self.assertEqual(document.kind, "images")
        self.assertEqual(document.title, "Truyện tranh")
        self.assertEqual(len(document.resources), 2)
        self.assertEqual(document.chapters[0].html.count("<img"), 2)

    def test_corrupt(self) -> None:
        with self.assertRaises(FormatError):
            formats.convert(self.write("bad.umd", umd.MAGIC + b"\x99garbage"))
        with self.assertRaises(FormatError):
            formats.convert(self.write("empty.umd", umd.MAGIC + umd_chunk(0x01, b"\x01\x00\x00")))


class LzxTests(unittest.TestCase):
    def test_uncompressed_blocks(self) -> None:
        data = lzx_encode([{"kind": "uncompressed", "data": b"Hello, odd!"},
                           {"kind": "uncompressed", "data": b"even"}])
        self.assertEqual(chm.lzx_decompress(data, 16, 15), b"Hello, odd!even")

    def test_verbatim_block_with_matches_and_repeated_offsets(self) -> None:
        ops = [
            lit(b"abc"),
            match(294, 3, 8, verbatim=(1, 1)),          # slot 4: offset 2 + 1 extra bit
            lit(b"X"),
            match(257, 3, 3),                           # R0
            match(283, 1, 5),                           # slot 3: offset 1
            match(266, 3, 4),                           # R1 (swapped with R0)
            match(274, 1, 4),                           # R2 (swapped with R0)
            match(295, 2, 14, footer=5, verbatim=(0, 1)),  # length 7 + footer 5 + 2
            match(297, 5, 3, verbatim=(1, 1)),          # slot 5: offset 4 + 1
            match(339, 35, 5, verbatim=(5, 4)),         # slot 10: offset 30 + 5
            lit("cuối".encode()),
        ]
        stream = lzx_encode([{"kind": "verbatim", "ops": ops, "elements": MATCH_ELEMENTS, "length_symbols": (0, 5)}])
        expected = apply_ops(ops)
        self.assertEqual(chm.lzx_decompress(stream, 16, len(expected)), expected)

    def test_aligned_block_and_tree_deltas_across_blocks(self) -> None:
        first = [lit(b"0123456789" * 4)]
        second = [match(339, 35, 5, verbatim=(0, 1), aligned=5),  # offset 30 + (0 << 3) + 5
                  match(297, 5, 3, verbatim=(1, 1)), lit(b"Z")]
        stream = lzx_encode([{"kind": "verbatim", "ops": first},
                             {"kind": "aligned", "ops": second, "elements": MATCH_ELEMENTS}])
        expected = apply_ops(second, bytearray(apply_ops(first)))
        self.assertEqual(chm.lzx_decompress(stream, 16, len(expected)), expected)

    def test_frames_are_realigned(self) -> None:
        data = bytes((i * 7 + i // 300) % 251 for i in range(40000))
        stream = lzx_literal_stream(data)
        self.assertEqual(chm.lzx_decompress(stream, 16, len(data)), data)
        mixed = lzx_encode([{"kind": "verbatim", "ops": [lit(data[:100])]},
                            {"kind": "uncompressed", "data": data[100:40000]}])
        self.assertEqual(chm.lzx_decompress(mixed, 16, 40000), data)

    def test_corrupt_streams(self) -> None:
        with self.assertRaises(FormatError):
            chm.lzx_decompress(b"\xff\xff" * 8, 16, 100)  # block type 7
        with self.assertRaises(FormatError):
            chm.lzx_decompress(lzx_literal_stream(b"short"), 16, 5000)  # truncated
        with self.assertRaises(FormatError):
            chm.lzx_decompress(b"", 14, 10)


class ChmTests(_TempDir):
    def check_document(self, document) -> None:
        self.assertEqual(document.source_format, "chm")
        self.assertEqual(document.title, "CHM Test Book")
        self.assertEqual(document.language, "vi")
        self.assertEqual([c.title for c in document.chapters], ["Mở đầu", "Chương hai"])
        first, second = document.chapters[0].html, document.chapters[1].html
        self.assertEqual(len(document.resources), 1)
        name = document.resources[0].name
        self.assertIn(f'<img src="{name}" alt="ảnh"/>', first)
        self.assertIn("<a>tiếp</a>", first)
        self.assertIn('<a href="#top">đầu</a>', first)
        self.assertNotIn("alert", first)
        self.assertIn('<p id="sec">Café au lait</p>', second)
        self.assertIn(f'<img src="{name}"/>', second)
        self.assertEqual(second.count("<img"), 1)
        self.assertIn('<a href="http://example.com/">web</a>', second)

    def test_section0_only(self) -> None:
        files = {"/#SYSTEM": chm_system(), **chm_files()}
        self.check_document(formats.convert(self.write("help.chm", build_chm(files))))

    def test_lzx_compressed_section(self) -> None:
        files1 = chm_files()
        big = ("<html><body><p>" + "Dòng dài lặp lại. " * 2500 + "KẾT THÚC</p></body></html>").encode()
        files1["/html/big.htm"] = big
        files1["/toc.hhc"] = HHC.replace(
            '<li><object type="text/sitemap"><param name="Name" value="Thư mục"></object>',
            '<li><object type="text/sitemap"><param name="Name" value="Lớn"><param name="Local" value="html/big.htm">'
            '</object>').encode()
        data = build_chm({"/#SYSTEM": chm_system()}, files1)
        document = formats.convert(self.write("packed.chm", data))
        self.assertEqual([c.title for c in document.chapters], ["Mở đầu", "Chương hai", "Lớn"])
        self.assertIn("KẾT THÚC", document.chapters[2].html)
        self.assertEqual(document.chapters[2].html.count("Dòng dài lặp lại."), 2500)
        document.chapters.pop()
        self.check_document(document)

    def test_without_toc_uses_sorted_html_files(self) -> None:
        files = chm_files()
        del files["/toc.hhc"]
        files["/#SYSTEM"] = system_file([(3, b"No TOC\0"), (2, b"index.htm\0")])
        document = formats.convert(self.write("notoc.chm", build_chm(files)))
        self.assertEqual([c.title for c in document.chapters], ["Index", "Phần 2"])

    def test_corrupt(self) -> None:
        with self.assertRaises(FormatError):
            formats.convert(self.write("bad.chm", b"ITSF" + b"\x03\x00\x00\x00" + b"\0" * 100))
        good = bytearray(build_chm({"/#SYSTEM": chm_system()}, chm_files()))
        content_at = good.find(b"MSCompressed") + 64
        broken = bytes(good[:content_at]) + b"\xff" * (len(good) - content_at)
        with self.assertRaises(FormatError):
            formats.convert(self.write("broken.chm", broken))


if __name__ == "__main__":
    unittest.main()
