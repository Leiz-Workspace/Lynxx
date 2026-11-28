"""Mobipocket (.mobi / .prc / .azw) and Kindle KF8 (.azw3) converter.

PalmDB container -> record 0 (PalmDOC header, MOBI header, EXTH) -> text records
(trailing entries stripped, then stored / PalmDOC LZ77 / HUFF-CDIC decompressed).
MOBI 6 text is one HTML stream split at ``<mbp:pagebreak/>``; KF8 text is rebuilt
into its original XHTML parts from the FDST flows and the SKEL / FRAG indexes
(the KindleUnpack approach). Joint MOBI 6 + KF8 files use the KF8 half.
"""

from __future__ import annotations

import bisect
import codecs
import html
import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .base import (Chapter, Document, FormatError, escape, extract_body, html_title, read_limited,
                   sanitize_fragment, sniff_image_type, split_text_chapters, strip_tags)

log = logging.getLogger(__name__)

NULL_INDEX = 0xFFFFFFFF
MAX_TEXT_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024        # text records hold ~4 KiB; anything this large is corrupt
MAX_HUFF_OUTPUT = 4 * 1024 * 1024     # one decompressed record or dictionary phrase
MAX_HUFF_DEPTH = 32
MAX_HUFF_EXPANDED = 128 * 1024 * 1024  # all cached dictionary phrase expansions together
MAX_INDEX_RECORDS = 10000
MAX_INDEX_ENTRIES = 200000
MAX_ASSEMBLY_WORK = 512 * 1024 * 1024
MAX_ANCHORS = 100000
COMPRESSION_NONE, COMPRESSION_PALMDOC, COMPRESSION_HUFF = 1, 2, 17480

_LANGUAGES = {
    0x01: "ar", 0x02: "bg", 0x03: "ca", 0x04: "zh", 0x05: "cs", 0x06: "da", 0x07: "de", 0x08: "el", 0x09: "en",
    0x0A: "es", 0x0B: "fi", 0x0C: "fr", 0x0D: "he", 0x0E: "hu", 0x0F: "is", 0x10: "it", 0x11: "ja", 0x12: "ko",
    0x13: "nl", 0x14: "no", 0x15: "pl", 0x16: "pt", 0x18: "ro", 0x19: "ru", 0x1A: "hr", 0x1B: "sk", 0x1D: "sv",
    0x1E: "th", 0x1F: "tr", 0x21: "id", 0x22: "uk", 0x24: "sl", 0x25: "et", 0x26: "lv", 0x27: "lt", 0x29: "fa",
    0x2A: "vi", 0x39: "hi", 0x3E: "ms",
}


# ---------------------------------------------------------------- PalmDB container

class _PalmDB:
    def __init__(self, data: bytes) -> None:
        if len(data) < 78:
            raise FormatError("not a PalmDB file")
        self.data = data
        self.name = data[:32].split(b"\0", 1)[0]
        self.ident = data[60:68]
        count = struct.unpack_from(">H", data, 76)[0]
        if count == 0 or 78 + 8 * count > len(data):
            raise FormatError("corrupt PalmDB record list")
        starts = [entry[0] for entry in struct.iter_unpack(">LL", data[78:78 + 8 * count])]
        size = len(data)
        self.bounds: list[tuple[int, int]] = []
        for index, start in enumerate(starts):
            end = min(starts[index + 1], size) if index + 1 < count else size
            self.bounds.append((start, end) if start <= end else (0, 0))
        self.count = count

    def record(self, index: int) -> bytes:
        if not 0 <= index < self.count:
            return b""
        start, end = self.bounds[index]
        return self.data[start:end]

    def head(self, index: int, size: int) -> bytes:
        if not 0 <= index < self.count:
            return b""
        start, end = self.bounds[index]
        return self.data[start:min(end, start + size)]

    def size(self, index: int) -> int:
        if not 0 <= index < self.count:
            return 0
        start, end = self.bounds[index]
        return end - start


# ---------------------------------------------------------------- record 0 headers

@dataclass(slots=True)
class _Header:
    start: int
    compression: int = COMPRESSION_NONE
    text_length: int = 0
    text_records: int = 0
    encryption: int = 0
    has_mobi: bool = False
    header_length: int = 0
    codec: str = "cp1252"
    version: int = 0
    min_version: int = 0
    full_name: bytes = b""
    locale: int = 0
    first_image: int = NULL_INDEX
    huff_offset: int = NULL_INDEX
    huff_count: int = 0
    extra_flags: int = 0
    fdst_index: int = NULL_INDEX
    ncx_index: int = NULL_INDEX
    frag_index: int = NULL_INDEX
    skel_index: int = NULL_INDEX
    exth: dict[int, list[bytes]] = field(default_factory=dict)

    @property
    def is_kf8(self) -> bool:
        return self.has_mobi and 8 in (self.version, self.min_version)

    def absolute(self, value: int, count: int) -> int | None:
        """A record index stored in this header, made absolute (None when null or out of range)."""
        if value == NULL_INDEX:
            return None
        index = self.start + value
        return index if 0 <= index < count else None


def _codec_name(code: int) -> str:
    if code == 65001:
        return "utf-8"
    if code == 1252 or not code:
        return "cp1252"
    try:
        return codecs.lookup(f"cp{code}").name
    except LookupError:
        return "cp1252"


def _parse_header(rec0: bytes, start: int, palmdoc_only: bool) -> _Header:
    if len(rec0) < 16:
        raise FormatError("corrupt MOBI header")
    compression, _unused, text_length, text_records, _record_size, encryption = struct.unpack_from(">HHLHHH", rec0, 0)
    header = _Header(start, compression, text_length, text_records, encryption)
    if palmdoc_only:
        header.encryption = 0  # plain PalmDOC: bytes 12-15 are the reading position, not a DRM flag
        return header
    if rec0[16:20] != b"MOBI" or len(rec0) < 24:
        return header
    length = struct.unpack_from(">L", rec0, 20)[0]
    limit = min(len(rec0), 16 + length)

    def u32(offset: int, default: int = NULL_INDEX) -> int:
        return struct.unpack_from(">L", rec0, offset)[0] if offset + 4 <= limit else default

    header.has_mobi = True
    header.header_length = length
    header.codec = _codec_name(u32(0x1C, 1252))
    header.version = u32(0x24, 0)
    name_offset, name_length = u32(0x54, 0), u32(0x58, 0)
    if 0 < name_length <= 4096 and name_offset + name_length <= len(rec0):
        header.full_name = rec0[name_offset:name_offset + name_length]
    header.locale = u32(0x5C, 0)
    header.min_version = u32(0x68, 0)
    header.first_image = u32(0x6C)
    header.huff_offset = u32(0x70)
    header.huff_count = u32(0x74, 0)
    exth_flags = u32(0x80, 0)
    if length >= 0xE4 and len(rec0) >= 0xF4 and (5 <= header.version < 100 or 5 <= header.min_version < 100):
        header.extra_flags = struct.unpack_from(">H", rec0, 0xF2)[0]
    header.fdst_index = u32(0xC0)
    header.ncx_index = u32(0xF4)
    header.frag_index = u32(0xF8)
    header.skel_index = u32(0xFC)
    if exth_flags & 0x40:
        header.exth = _parse_exth(rec0, 16 + length)
    return header


def _parse_exth(rec0: bytes, offset: int) -> dict[int, list[bytes]]:
    result: dict[int, list[bytes]] = {}
    if rec0[offset:offset + 4] != b"EXTH" or offset + 12 > len(rec0):
        return result
    total, count = struct.unpack_from(">LL", rec0, offset + 4)
    end = min(len(rec0), offset + total) if total >= 12 else len(rec0)
    pos = offset + 12
    for _ in range(min(count, 10000)):
        if pos + 8 > end:
            break
        kind, size = struct.unpack_from(">LL", rec0, pos)
        if size < 8 or pos + size > end:
            break
        result.setdefault(kind, []).append(rec0[pos + 8:pos + size])
        pos += size
    return result


def _check_header(header: _Header) -> None:
    if header.encryption:
        raise FormatError("DRM-protected book")
    if header.compression not in (COMPRESSION_NONE, COMPRESSION_PALMDOC, COMPRESSION_HUFF):
        raise FormatError(f"unsupported MOBI compression ({header.compression})")
    if header.text_records == 0:
        raise FormatError("book has no text records")


# ---------------------------------------------------------------- text records

def _trailing_entry_size(record: bytes, end: int) -> int:
    """Size of one trailing entry whose length is a backward variable-width integer ending at ``end``."""
    result = shift = 0
    while end > 0:
        byte = record[end - 1]
        result |= (byte & 0x7F) << shift
        shift += 7
        end -= 1
        if byte & 0x80 or shift >= 28:
            break
    return result


def strip_trailing_entries(record: bytes, flags: int) -> bytes:
    """Remove the extra data the MOBI header's extra-record-data flags say each text record carries."""
    size = len(record)
    if not flags or not size:
        return record
    num = 0
    bits = flags >> 1
    while bits:
        if bits & 1:
            num += _trailing_entry_size(record, size - num)
            if num >= size:
                return b""
        bits >>= 1
    if flags & 1:
        num += (record[size - num - 1] & 0x3) + 1
    return record[:size - num] if num < size else b""


def palmdoc_decompress(data: bytes) -> bytes:
    """PalmDOC LZ77 decompression of one text record."""
    out = bytearray()
    append = out.append
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c >= 0xC0:
            append(32)
            append(c ^ 0x80)
        elif c >= 0x80:
            if i >= n:
                break
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x7FF
            length = (pair & 7) + 3
            size = len(out)
            if distance == 0 or distance > size:
                continue  # corrupt back-reference: skip it rather than inventing bytes
            start = size - distance
            if distance >= length:
                out += out[start:start + length]
            else:
                for k in range(start, start + length):
                    append(out[k])
        elif c >= 9 or c == 0:
            append(c)
        else:
            out += data[i:i + c]
            i += c
    return bytes(out)


class HuffCdic:
    """MOBI HUFF/CDIC decompressor: a HUFF code table plus CDIC phrase dictionaries."""

    def __init__(self, huff: bytes, cdics: list[bytes]) -> None:
        if huff[:8] != b"HUFF\x00\x00\x00\x18" or len(huff) < 24:
            raise FormatError("corrupt HUFF record")
        off1, off2 = struct.unpack_from(">LL", huff, 8)
        if off1 + 1024 > len(huff) or off2 + 256 > len(huff):
            raise FormatError("corrupt HUFF record")
        self.dict1: list[tuple[int, int, int]] = []
        for value in struct.unpack_from(">256L", huff, off1):
            codelen, term, maxcode = value & 0x1F, value & 0x80, value >> 8
            if codelen == 0:
                raise FormatError("corrupt HUFF code table")
            self.dict1.append((codelen, term, ((maxcode + 1) << (32 - codelen)) - 1))
        dict2 = struct.unpack_from(">64L", huff, off2)
        self.mincode = [0] + [dict2[2 * k] << (31 - k) for k in range(32)]
        self.maxcode = [0] + [((dict2[2 * k + 1] + 1) << (31 - k)) - 1 for k in range(32)]
        self.dictionary: list[tuple[bytes, int] | None] = []
        for cdic in cdics:
            if cdic[:8] != b"CDIC\x00\x00\x00\x10" or len(cdic) < 16:
                raise FormatError("corrupt CDIC record")
            phrases, bits = struct.unpack_from(">LL", cdic, 8)
            count = min(1 << min(bits, 20), phrases - len(self.dictionary), (len(cdic) - 16) // 2)
            if count <= 0:
                continue
            for offset in struct.unpack_from(f">{count}H", cdic, 16):
                pos = 16 + offset
                if pos + 2 > len(cdic):
                    raise FormatError("corrupt CDIC record")
                blen = struct.unpack_from(">H", cdic, pos)[0]
                self.dictionary.append((cdic[pos + 2:pos + 2 + (blen & 0x7FFF)], blen & 0x8000))
        if not self.dictionary:
            raise FormatError("empty HUFF/CDIC dictionary")
        self.expanded = 0

    def decompress(self, data: bytes, depth: int = 0) -> bytes:
        if depth > MAX_HUFF_DEPTH:
            raise FormatError("HUFF/CDIC dictionary is nested too deeply")
        dict1, mincode, maxcodes, dictionary = self.dict1, self.mincode, self.maxcode, self.dictionary
        size = len(dictionary)
        bitsleft = len(data) * 8
        padded = data + b"\0" * 16
        pos = 0
        x = int.from_bytes(padded[0:8], "big")
        n = 32
        out = bytearray()
        while True:
            if n <= 0:
                pos += 4
                x = int.from_bytes(padded[pos:pos + 8], "big")
                n += 32
            code = (x >> n) & 0xFFFFFFFF
            codelen, term, maxcode = dict1[code >> 24]
            if not term:
                while codelen < 32 and code < mincode[codelen]:
                    codelen += 1
                maxcode = maxcodes[codelen]
            n -= codelen
            bitsleft -= codelen
            if bitsleft < 0:
                break
            index = (maxcode - code) >> (32 - codelen)
            if not 0 <= index < size:
                raise FormatError("corrupt HUFF/CDIC data")
            entry = dictionary[index]
            if entry is None:
                raise FormatError("HUFF/CDIC dictionary refers to itself")
            phrase, flag = entry
            if not flag:
                dictionary[index] = None
                phrase = self.decompress(phrase, depth + 1)
                dictionary[index] = (phrase, 1)
                self.expanded += len(phrase)
                if self.expanded > MAX_HUFF_EXPANDED:
                    raise FormatError("HUFF/CDIC dictionary expands too much")
            out += phrase
            if len(out) > MAX_HUFF_OUTPUT:
                raise FormatError("HUFF/CDIC record expands too much")
        return bytes(out)


def _text_bytes(db: _PalmDB, header: _Header) -> bytes:
    if header.compression == COMPRESSION_PALMDOC:
        decompress = palmdoc_decompress
    elif header.compression == COMPRESSION_HUFF:
        first = header.absolute(header.huff_offset, db.count)
        if first is None or not 0 < header.huff_count <= 4096 or first + header.huff_count > db.count:
            raise FormatError("HUFF/CDIC book without a valid dictionary")
        reader = HuffCdic(db.record(first), [db.record(first + k) for k in range(1, header.huff_count)])
        decompress = reader.decompress
    else:
        decompress = None
    parts: list[bytes] = []
    total = 0
    for index in range(header.start + 1, min(header.start + 1 + header.text_records, db.count)):
        if db.size(index) > MAX_RECORD_BYTES:
            raise FormatError("corrupt text record")
        record = strip_trailing_entries(db.record(index), header.extra_flags)
        chunk = decompress(record) if decompress else record
        total += len(chunk)
        if total > MAX_TEXT_BYTES:
            raise FormatError("book text is too large")
        parts.append(chunk)
    text = b"".join(parts)
    if not text:
        raise FormatError("book has no text")
    return text


# ---------------------------------------------------------------- INDX / TAGX / CNCX

def _vwi(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """Forward variable-width integer (7 bits per byte, the last byte has bit 7 set)."""
    value = 0
    for _ in range(8):
        if pos >= end:
            raise FormatError("truncated index entry")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte & 0x80:
            return value, pos
    raise FormatError("corrupt index value")


_INDX_WORDS = ("len", "nul1", "type", "gen", "start", "count", "code", "lng", "total", "ordt", "ligt", "nligt", "nctoc")


def _indx_header(data: bytes) -> dict[str, int]:
    if data[:4] != b"INDX" or len(data) < 4 + 4 * len(_INDX_WORDS):
        raise FormatError("index record is not INDX")
    return dict(zip(_INDX_WORDS, struct.unpack_from(f">{len(_INDX_WORDS)}L", data, 4)))


def _indx_ordt(data: bytes, header: dict[str, int]) -> tuple[int, ...] | None:
    if len(data) < 0xA4 + 20:
        return None
    ocnt, oentries, _op1, op2, _otagx = struct.unpack_from(">LLLLL", data, 0xA4)
    if not (header["code"] == 0xFDEA or ocnt or oentries) or not 0 < oentries <= 65536:
        return None
    if data[op2:op2 + 4] != b"ORDT" or op2 + 4 + 2 * oentries > len(data):
        return None
    return struct.unpack_from(f">{oentries}H", data, op2 + 4)


def _read_tagx(data: bytes, start: int) -> tuple[int, list[tuple[int, int, int, int]]]:
    if data[start:start + 4] != b"TAGX" or start + 12 > len(data):
        raise FormatError("index without TAGX section")
    length, control_count = struct.unpack_from(">LL", data, start + 4)
    if length > 4096 or start + length > len(data) or not 0 < control_count <= 16:
        raise FormatError("corrupt TAGX section")
    tags = [tuple(data[start + k:start + k + 4]) for k in range(12, length - 3, 4)]
    return control_count, tags  # type: ignore[return-value]


def _tag_map(control_count: int, tags: list, data: bytes, start: int, end: int) -> dict[int, list[int]]:
    pending: list[tuple[int, int | None, int | None, int]] = []
    control_index = 0
    pos = start + control_count
    if pos > end:
        raise FormatError("truncated index entry")
    for tag, per_entry, mask, end_flag in tags:
        if end_flag == 1:
            control_index += 1
            continue
        if control_index >= control_count or not mask:
            continue
        value = data[start + control_index] & mask
        if not value:
            continue
        if value == mask:
            if bin(mask).count("1") > 1:
                byte_count, pos = _vwi(data, pos, end)
                pending.append((tag, None, byte_count, per_entry))
            else:
                pending.append((tag, 1, None, per_entry))
        else:
            while not mask & 1:
                mask >>= 1
                value >>= 1
            pending.append((tag, value, None, per_entry))
    result: dict[int, list[int]] = {}
    for tag, count, byte_count, per_entry in pending:
        values: list[int] = []
        if count is not None:
            if count * per_entry > 4096:
                raise FormatError("corrupt index entry")
            for _ in range(count * per_entry):
                value, pos = _vwi(data, pos, end)
                values.append(value)
        else:
            stop = pos + (byte_count or 0)
            if stop > end:
                raise FormatError("corrupt index entry")
            while pos < stop:
                value, pos = _vwi(data, pos, stop)
                values.append(value)
        result[tag] = values
    return result


def _read_cncx(data: bytes, base: int) -> dict[int, bytes]:
    result: dict[int, bytes] = {}
    pos, end = 0, len(data)
    try:
        while pos < end and data[pos] != 0:
            offset = pos
            length, pos = _vwi(data, pos, end)
            result[base + offset] = data[pos:pos + length]
            pos += length
    except FormatError:
        pass
    return result


def _read_index(db: _PalmDB, index: int) -> tuple[list[tuple[str, dict[int, list[int]]]], dict[int, bytes]]:
    main = db.record(index)
    header = _indx_header(main)
    count, nctoc = header["count"], header["nctoc"]
    if count > MAX_INDEX_RECORDS or nctoc > MAX_INDEX_RECORDS or index + 1 + count + nctoc > db.count:
        raise FormatError("corrupt index record count")
    ordt = _indx_ordt(main, header)
    cncx: dict[int, bytes] = {}
    for k in range(nctoc):
        cncx.update(_read_cncx(db.record(index + count + 1 + k), k * 0x10000))
    control_count, tags = _read_tagx(main, header["len"])
    entries: list[tuple[str, dict[int, list[int]]]] = []
    for record_index in range(index + 1, index + 1 + count):
        data = db.record(record_index)
        sub = _indx_header(data)
        idxt, total = sub["start"], sub["count"]
        if data[idxt:idxt + 4] != b"IDXT" or idxt + 4 + 2 * total > len(data):
            raise FormatError("corrupt IDXT table")
        positions = list(struct.unpack_from(f">{total}H", data, idxt + 4)) + [idxt]
        for k in range(total):
            start, end = positions[k], positions[k + 1]
            if not 0 < start < end <= idxt:
                raise FormatError("corrupt index entry offsets")
            key_length = data[start]
            raw_key = data[start + 1:start + 1 + key_length]
            if ordt:
                key = "".join(chr(ordt[b]) if b < len(ordt) else chr(b) for b in raw_key)
            else:
                key = raw_key.decode("latin-1")
            entries.append((key, _tag_map(control_count, tags, data, start + 1 + key_length, end)))
            if len(entries) > MAX_INDEX_ENTRIES:
                raise FormatError("index has too many entries")
    return entries, cncx


def _read_ncx(db: _PalmDB, header: _Header) -> list[tuple[int | None, int | None, int, str]]:
    """NCX entries as (position, fragment id, offset, label); [] when the book has none or it is unreadable."""
    index = header.absolute(header.ncx_index, db.count)
    if index is None:
        return []
    try:
        entries, cncx = _read_index(db, index)
    except FormatError as err:
        log.info("mobi: NCX index unreadable: %s", err)
        return []
    result = []
    for _key, tags in entries:
        label_offset = (tags.get(3) or [None])[0]
        label = cncx.get(label_offset, b"").decode(header.codec, "replace").strip() if label_offset is not None else ""
        position = (tags.get(1) or [None])[0]
        fid_off = tags.get(6) or []
        fid = fid_off[0] if len(fid_off) >= 2 else None
        off = fid_off[1] if len(fid_off) >= 2 else 0
        result.append((position, fid, off, label))
    return result


# ---------------------------------------------------------------- resources

_KINDLE_EMBED = re.compile(r"kindle:embed:([0-9A-Va-v]{1,6})")


def kindle_embed_index(src: str) -> int | None:
    """The 1-based resource number of a ``kindle:embed:XXXX`` reference (base 32, digits 0-9A-V)."""
    match = _KINDLE_EMBED.match(src.strip())
    if not match:
        return None
    value = int(match.group(1), 32)
    return value if value > 0 else None


class _Resources:
    """Registers image records once and resolves recindex / kindle:embed references to resource names."""

    def __init__(self, db: _PalmDB, document: Document, base: int | None) -> None:
        self.db = db
        self.document = document
        self.base = base
        self.names: dict[int, str] = {}

    def by_record(self, index: int) -> str:
        if index in self.names:
            return self.names[index]
        name = ""
        if 0 < index < self.db.count:
            data = self.db.record(index)
            media_type = sniff_image_type(data)
            if media_type and media_type != "image/svg+xml":
                name = self.document.add_resource(data, media_type, f"image{index:05d}")
        self.names[index] = name
        return name

    def by_number(self, number: int) -> str:
        """A 1-based image number relative to the first resource record."""
        if self.base is None or number < 1:
            return ""
        return self.by_record(self.base + number - 1)

    def src(self, value: str) -> str:
        value = value.strip()
        if value.startswith("lynxx-rec:"):
            digits = value[10:]
            return self.by_number(int(digits)) if digits.isdigit() else ""
        number = kindle_embed_index(value)
        return self.by_number(number) if number else ""

    def resource(self, name: str):
        return next((r for r in self.document.resources if r.name == name), None) if name else None


def _resource_base(db: _PalmDB, header: _Header, mobi6: _Header | None) -> int | None:
    own = header.absolute(header.first_image, db.count)
    if mobi6 is not None and mobi6 is not header:
        # joint file: the images live in the MOBI 6 half and are shared by the KF8 half
        if own is not None and own > header.start and sniff_image_type(db.head(own, 64)):
            return own
        shared = mobi6.absolute(mobi6.first_image, db.count)
        if shared is not None and shared < header.start:
            return shared
    if own is not None:
        return own
    for index in range(header.start + header.text_records + 1, db.count):
        if sniff_image_type(db.head(index, 64)):
            return index
    return None


# ---------------------------------------------------------------- shared HTML helpers

_HEAD_OPEN = re.compile(r"<h([1-3])\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE = {level: re.compile(rf"</h{level}\s*>", re.IGNORECASE) for level in "123"}
_SVG_OPEN = re.compile(r"<svg\b", re.IGNORECASE)
_SVG_CLOSE = re.compile(r"</svg\s*>", re.IGNORECASE)
_SVG_IMAGE = re.compile(r"""<image\b[^>]*?\b(?:xlink:)?href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _first_heading(markup: str) -> str:
    pos = 0
    limit = min(len(markup), 400000)
    for _ in range(20):
        match = _HEAD_OPEN.search(markup, pos, limit)
        if not match:
            return ""
        close = _HEAD_CLOSE[match.group(1)].search(markup, match.end(), match.end() + 4000)
        if close:
            text = strip_tags(markup[match.end():close.start()])
            if text:
                return text[:200]
        pos = match.end()
    return ""


def _svg_images(markup: str) -> str:
    """Replace inline <svg> wrappers (fixed-layout pages) by plain <img> tags for their <image> hrefs."""
    out: list[str] = []
    pos = 0
    while True:
        opening = _SVG_OPEN.search(markup, pos)
        if not opening:
            break
        closing = _SVG_CLOSE.search(markup, opening.end())
        if not closing:
            break
        block = markup[opening.start():closing.end()]
        images = "".join(f'<img src="{escape(html.unescape(href))}" alt=""/>' for href in _SVG_IMAGE.findall(block)[:50])
        out.append(markup[pos:opening.start()])
        out.append(images)
        pos = closing.end()
    out.append(markup[pos:])
    return "".join(out)


def _has_content(fragment: str) -> bool:
    return "<img" in fragment or bool(strip_tags(fragment))


def _clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:300]


# ---------------------------------------------------------------- MOBI 6

_HTML_HINT = re.compile(rb"<\s*(?:html|body|p|div|br|h[1-6]|mbp:|font|span|img|blockquote|center)\b", re.IGNORECASE)
_PAGEBREAK = re.compile(rb"<\s*mbp\s*:\s*pagebreak\b[^>]*>", re.IGNORECASE)
_FILEPOS = re.compile(rb"""\bfilepos\s*=\s*['"]?0*(\d{1,10})""", re.IGNORECASE)
_FILEPOS_LINK = re.compile(rb"""(<a\b[^>]*?)\bfilepos\s*=\s*['"]?0*(\d{1,10})['"]?""", re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""")


def _insert_anchors(raw: bytes, positions: list[int], base: int, utf8: bool) -> bytes:
    """Insert <a id="fileposN"></a> at byte offsets (relative to ``base``), never inside a tag or character."""
    if not positions:
        return raw
    out: list[bytes] = []
    prev = 0
    size = len(raw)
    for absolute in positions:
        pos = min(max(absolute - base, prev), size)
        lt = raw.rfind(b"<", max(0, pos - 4096), pos)
        gt = raw.rfind(b">", max(0, pos - 4096), pos)
        if lt > gt:  # inside a tag: move after it
            end = raw.find(b">", pos)
            pos = size if end < 0 else end + 1
        amp = raw.rfind(b"&", max(0, pos - 12), pos)
        if amp >= 0 and b";" not in raw[amp:pos] and b" " not in raw[amp:pos]:
            semi = raw.find(b";", pos, pos + 12)
            if semi >= 0:
                pos = semi + 1
        if utf8:
            while pos < size and 0x80 <= raw[pos] < 0xC0:
                pos += 1
        pos = max(pos, prev)
        out.append(raw[prev:pos])
        out.append(b'<a id="filepos%d"></a>' % absolute)
        prev = pos
    out.append(raw[prev:])
    return b"".join(out)


def _rewrite_mobi_images(markup: str) -> str:
    def replace(match: re.Match) -> str:
        attrs: dict[str, str] = {}
        for name, v1, v2, v3 in _ATTR.findall(match.group(0)):
            attrs.setdefault(name.lower(), v1 or v2 or v3)
        alt = html.unescape(attrs.get("alt", ""))
        for key in ("recindex", "hirecindex", "lowrecindex"):
            value = attrs.get(key, "").strip()
            if value.isdigit() and len(value) <= 9:
                return f'<img src="lynxx-rec:{int(value)}" alt="{escape(alt)}"/>'
        src = attrs.get("src", "")
        if src.lower().startswith("kindle:embed:"):
            return f'<img src="{escape(html.unescape(src))}" alt="{escape(alt)}"/>'
        return ""

    return _IMG_TAG.sub(replace, markup)


def _mobi6_chapters(db: _PalmDB, header: _Header, text: bytes, resources: _Resources,
                    book_title: str) -> list[Chapter]:
    if not _HTML_HINT.search(text[:262144]):
        return split_text_chapters(text.decode(header.codec, "replace"), book_title)
    utf8 = header.codec == "utf-8"
    breaks = [(m.start(), m.end()) for m in _PAGEBREAK.finditer(text)]
    segments: list[tuple[int, int]] = []
    prev = 0
    for start, end in breaks:
        segments.append((prev, start))
        prev = end
    segments.append((prev, len(text)))
    starts = [start for start, _ in segments]
    break_starts = [start for start, _ in breaks]

    def segment_of(position: int) -> tuple[int, int]:
        k = bisect.bisect_right(break_starts, position) - 1
        if k >= 0 and breaks[k][0] <= position < breaks[k][1]:
            position = breaks[k][1]  # inside a page-break tag: the next segment's start
        return max(0, bisect.bisect_right(starts, position) - 1), position

    positions = sorted({int(m.group(1)) for m in _FILEPOS.finditer(text)})[:MAX_ANCHORS]
    owner: dict[int, int] = {}
    per_segment: list[list[int]] = [[] for _ in segments]
    for position in positions:
        if 0 < position <= len(text):
            owner[position] = index = segment_of(position)[0]
            per_segment[index].append(position)
    titles: dict[int, str] = {}
    for position, _fid, _off, label in _read_ncx(db, header):
        if position is not None and label and 0 <= position <= len(text):
            titles.setdefault(segment_of(position)[0], label)

    def link_resolver(index: int):
        def resolve(href: str) -> str:
            href = href.strip()
            match = re.fullmatch(r"#filepos(\d+)", href)
            if match:
                return href if owner.get(int(match.group(1))) == index else ""
            if href.startswith("#"):
                return "#" + re.sub(r"[^A-Za-z0-9_.:-]", "_", href[1:])[:120]
            return href if re.match(r"^https?://", href, re.IGNORECASE) else ""
        return resolve

    chapters: list[Chapter] = []
    for index, (start, end) in enumerate(segments):
        raw = text[start:end]
        if per_segment[index]:
            raw = _insert_anchors(raw, per_segment[index], start, utf8)
        raw = _FILEPOS_LINK.sub(lambda m: m.group(1) + b'href="#filepos%d"' % int(m.group(2)), raw)
        markup = _rewrite_mobi_images(raw.decode(header.codec, "replace"))
        fragment = sanitize_fragment(markup, resolve_src=resources.src, resolve_link=link_resolver(index))
        if not _has_content(fragment):
            continue
        title = titles.get(index) or _first_heading(markup) or f"Phần {len(chapters) + 1}"
        chapters.append(Chapter(_clean_title(title), fragment))
    return chapters


# ---------------------------------------------------------------- KF8

def _fdst_flows(db: _PalmDB, header: _Header, text_length: int) -> list[tuple[int, int]]:
    index = header.absolute(header.fdst_index, db.count)
    if index is not None:
        record = db.record(index)
        if record[:4] == b"FDST" and len(record) >= 12:
            count = struct.unpack_from(">L", record, 8)[0]
            if 0 < count <= 10000 and 12 + 8 * count <= len(record):
                values = struct.unpack_from(f">{2 * count}L", record, 12)
                flows = []
                for start, end in zip(values[0::2], values[1::2]):
                    start = min(start, text_length)
                    flows.append((start, min(max(end, start), text_length)))
                return flows
    return [(0, text_length)]


_AID_SELECTOR = re.compile(rb"""@aid\s*=\s*['"]([^'"]{1,64})['"]""")


def _locate_aid(skeleton: bytes, aid: bytes) -> tuple[int, int]:
    pattern = re.compile(rb"""<[^>]*\said\s*=\s*['"]""" + re.escape(aid) + rb"""['"][^>]*>""", re.IGNORECASE)
    match = pattern.search(skeleton)
    if not match:
        return 0, 0
    return match.start(), skeleton.find(b">", match.start() + 1)


def _kf8_assemble(db: _PalmDB, header: _Header, text: bytes) -> tuple[list[bytes], list[tuple[int, int]], list]:
    """Rebuild the original XHTML parts: every skeleton with its fragments inserted (KindleUnpack)."""
    skel_index = header.absolute(header.skel_index, db.count)
    frag_index = header.absolute(header.frag_index, db.count)
    if skel_index is None or frag_index is None:
        raise FormatError("KF8 book without skeleton / fragment index")
    skel_entries, _ = _read_index(db, skel_index)
    frag_entries, frag_cncx = _read_index(db, frag_index)
    fragments = []
    for key, tags in frag_entries:
        if not re.fullmatch(r"[0-9]{1,12}", key):
            raise FormatError("corrupt KF8 fragment key")
        position = tags.get(6) or []
        if len(position) < 2:
            raise FormatError("corrupt KF8 fragment entry")
        selector = frag_cncx.get((tags.get(2) or [-1])[0], b"")
        fragments.append((int(key), selector, (tags.get(3) or [0])[0], position[0], position[1]))
    parts: list[bytes] = []
    ranges: list[tuple[int, int]] = []
    pointer = 0
    work = 0
    size = len(text)
    for _key, tags in skel_entries:
        count = (tags.get(1) or [0])[0]
        position = tags.get(6) or []
        if len(position) < 2:
            raise FormatError("corrupt KF8 skeleton entry")
        skel_pos, skel_len = position[0], position[1]
        base = skel_pos + skel_len
        if base > size:
            raise FormatError("KF8 skeleton outside the text")
        skeleton = text[skel_pos:base]
        for _ in range(count):
            if pointer >= len(fragments):
                raise FormatError("KF8 fragment table is too short")
            insert, selector, _filenum, start_pos, length = fragments[pointer]
            if base + length > size:
                raise FormatError("KF8 fragment outside the text")
            piece = text[base:base + length]
            rel = insert - skel_pos
            head, tail = skeleton[:max(rel, 0)], skeleton[max(rel, 0):]
            if not 0 <= rel <= len(skeleton) or tail.find(b">") < tail.find(b"<") \
                    or head.rfind(b">") < head.rfind(b"<"):
                aid = _AID_SELECTOR.search(selector)
                begin, finish = _locate_aid(skeleton, aid.group(1)) if aid else (0, 0)
                if begin != finish and finish > 0:
                    rel = finish + 1 + start_pos
                if not 0 <= rel <= len(skeleton):
                    raise FormatError("KF8 fragment insert position is invalid")
            skeleton = skeleton[:rel] + piece + skeleton[rel:]
            work += len(skeleton)
            if work > MAX_ASSEMBLY_WORK:
                raise FormatError("KF8 book is too fragmented")
            base += length
            pointer += 1
        parts.append(skeleton)
        ranges.append((skel_pos, base))
    if not parts:
        raise FormatError("KF8 skeleton index is empty")
    return parts, ranges, fragments


def _kf8_fallback_parts(text: bytes) -> list[bytes]:
    pieces = re.split(rb"<body\b[^>]*>", text, flags=re.IGNORECASE)[1:]
    parts = []
    for piece in pieces:
        end = re.search(rb"</body\s*>", piece, re.IGNORECASE)
        parts.append(b"<body>" + (piece[:end.start()] if end else piece) + b"</body>")
    return parts or [text]


def _kf8_chapters(db: _PalmDB, header: _Header, text: bytes, resources: _Resources) -> list[Chapter]:
    flows = _fdst_flows(db, header, len(text))
    main_start, main_end = flows[0]
    flow = text[main_start:main_end]
    titles: dict[int, str] = {}
    try:
        parts, ranges, fragments = _kf8_assemble(db, header, flow)
    except FormatError as err:
        log.warning("mobi: KF8 index unreadable (%s); splitting the raw text at <body> boundaries", err)
        parts = _kf8_fallback_parts(flow)
    else:
        starts = [start for start, _ in ranges]
        for position, fid, off, label in _read_ncx(db, header):
            if not label:
                continue
            if fid is not None and fid < len(fragments):
                position = fragments[fid][0] + off
            if position is None:
                continue
            index = bisect.bisect_right(starts, position) - 1
            if 0 <= index < len(parts) and position < ranges[index][1] + 1:
                titles.setdefault(index, label)
    chapters: list[Chapter] = []
    for index, part in enumerate(parts):
        markup = part.decode("utf-8", "replace")
        body = _svg_images(extract_body(markup))
        fragment = sanitize_fragment(body, resolve_src=resources.src)
        if not _has_content(fragment):
            continue
        title = titles.get(index) or _first_heading(body) or html_title(markup) or f"Phần {len(chapters) + 1}"
        chapters.append(Chapter(_clean_title(title), fragment))
    return chapters


# ---------------------------------------------------------------- entry point

def _find_kf8_start(db: _PalmDB, header: _Header) -> int | None:
    values = header.exth.get(121) or []
    if values and len(values[0]) >= 4:
        k8i = struct.unpack_from(">L", values[0], 0)[0]
        if 0 < k8i < db.count and db.record(k8i - 1) == b"BOUNDARY":
            return k8i
    for index in range(1, db.count - 1):
        if db.size(index) == 8 and db.record(index) == b"BOUNDARY":
            return index + 1
    return None


def _exth_text(exth: dict[int, list[bytes]], kind: int, codec: str) -> list[str]:
    return [value.decode(codec, "replace").strip("\0 \t\r\n") for value in exth.get(kind, [])]


def _exth_int(exth: dict[int, list[bytes]], kind: int) -> int | None:
    values = exth.get(kind) or []
    if values and len(values[0]) >= 4:
        value = struct.unpack_from(">L", values[0], 0)[0]
        return None if value == NULL_INDEX else value
    return None


def _fill_metadata(document: Document, db: _PalmDB, header: _Header, exth: dict[int, list[bytes]]) -> None:
    codec = header.codec
    title = next((t for t in _exth_text(exth, 503, codec) if t), "")
    if not title and header.full_name:
        title = header.full_name.decode(codec, "replace").strip("\0 ")
    if not title:
        title = db.name.decode("cp1252", "replace").replace("_", " ").strip()
    document.title = _clean_title(title)
    authors: list[str] = []
    for value in _exth_text(exth, 100, codec):
        for name in re.split(r"\s*[;&]\s*", value):
            name = name.strip()
            if name and name not in authors:
                authors.append(name)
    document.authors = authors[:20]
    description = next((d for d in _exth_text(exth, 103, codec) if d), "")
    document.description = strip_tags(description)[:20000] if description else ""
    language = next((v for v in _exth_text(exth, 524, codec) if v), "")
    document.language = language[:35] if language else _LANGUAGES.get(header.locale & 0xFF, "")


def _fill_cover(document: Document, exth: dict[int, list[bytes]], resources: _Resources) -> None:
    for kind in (201, 202):  # cover offset, then thumbnail offset (both relative to the first image record)
        offset = _exth_int(exth, kind)
        if offset is not None:
            name = resources.by_number(offset + 1)
            if name:
                document.cover = resources.resource(name)
                return


def _convert_header(db: _PalmDB, header: _Header, mobi6: _Header | None, source_format: str) -> Document:
    document = Document(source_format=source_format)
    text = _text_bytes(db, header)
    if text.startswith(b"%MOP"):
        raise FormatError("Print Replica (AZW4) books are not supported")
    resources = _Resources(db, document, _resource_base(db, header, mobi6) if header.has_mobi else None)
    exth = dict(mobi6.exth) if mobi6 is not None and mobi6 is not header else {}
    exth.update(header.exth)
    _fill_metadata(document, db, header, exth)
    if header.is_kf8:
        document.chapters = _kf8_chapters(db, header, text, resources)
    elif header.has_mobi:
        document.chapters = _mobi6_chapters(db, header, text, resources, document.title)
    else:
        document.chapters = split_text_chapters(text.decode(header.codec, "replace"), document.title)
    if not document.chapters:
        raise FormatError("book has no readable text")
    _fill_cover(document, exth, resources)
    return document


def convert(path: Path) -> Document:
    data = read_limited(path)
    db = _PalmDB(data)
    if db.ident not in (b"BOOKMOBI", b"TEXtREAd"):
        raise FormatError("unsupported PalmDB book type")
    header = _parse_header(db.record(0), 0, db.ident == b"TEXtREAd")
    _check_header(header)
    source_format = Path(path).suffix.lstrip(".").lower() or "mobi"
    if header.is_kf8 or not header.has_mobi:
        return _convert_header(db, header, None, source_format)
    start = _find_kf8_start(db, header)
    if start is not None:
        try:
            kf8 = _parse_header(db.record(start), start, False)
            if not kf8.has_mobi:
                raise FormatError("no MOBI header after the KF8 boundary")
            if not kf8.is_kf8:
                kf8.version = 8  # the header after BOUNDARY is the KF8 half by definition
            _check_header(kf8)
            return _convert_header(db, kf8, header, source_format)
        except FormatError as err:
            if "DRM" in str(err):
                raise
            log.warning("mobi: KF8 half unreadable (%s); using the MOBI 6 half", err)
    return _convert_header(db, header, None, source_format)
