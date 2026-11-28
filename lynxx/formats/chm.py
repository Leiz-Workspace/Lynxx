"""Microsoft Compiled HTML Help (.chm) converter.

ITSF header -> ITSP directory -> PMGL listing chunks (name, section, offset, length).
Section 0 is stored; section 1 ("MSCompressed") is an LZX stream described by
ControlData (window size, reset interval) and the reset table, decompressed here
in pure Python one reset interval at a time (libmspack semantics: the stream is
re-aligned to 16 bits after every 32 KiB frame and fully reset at every reset
interval). Topics are read in .hhc table-of-contents order (all HTML files sorted
by path when there is no TOC) and sanitized with their images resolved inside
the CHM; links to other topics are dropped.
"""

from __future__ import annotations

import logging
import posixpath
import re
import struct
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from .base import (Chapter, Document, FormatError, decode_text, extract_body, html_title, read_limited,
                   resolve_href, sanitize_fragment, sniff_image_type, strip_tags)

log = logging.getLogger(__name__)

MAX_ENTRIES = 200000
MAX_TOPICS = 20000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_READ = 768 * 1024 * 1024      # all entries read together (entries may overlap)
MAX_SECTION_BYTES = 1024 * 1024 * 1024
MAX_LZX_OUTPUT = 64 * 1024 * 1024        # one reset interval
LZX_FRAME = 0x8000
CACHE_BYTES = 64 * 1024 * 1024

_CONTROL = "::DataSpace/Storage/MSCompressed/ControlData"
_CONTENT = "::DataSpace/Storage/MSCompressed/Content"
_RESET_TABLE = ("::DataSpace/Storage/MSCompressed/Transform/{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/"
                "InstanceData/ResetTable")
_NAMELIST = "::DataSpace/NameList"

class _BudgetError(FormatError):
    """The file expands beyond the total size limit: abort instead of skipping the entry."""


# ---------------------------------------------------------------- LZX

_POSITION_SLOTS = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}
_MAIN_MAX = 256 + 50 * 8
_LENGTH_SYMBOLS = 249
_EXTRA_BITS = [0] * 52
_value = 0
for _i in range(0, 51, 2):
    _EXTRA_BITS[_i] = _EXTRA_BITS[_i + 1] = _value
    if _i and _value < 17:
        _value += 1
_POSITION_BASE = [0] * 51
_value = 0
for _i in range(51):
    _POSITION_BASE[_i] = _value
    _value += 1 << _EXTRA_BITS[_i]
del _i, _value

BLOCK_VERBATIM, BLOCK_ALIGNED, BLOCK_UNCOMPRESSED = 1, 2, 3


def build_decode_table(lengths, count: int) -> tuple[list[int], int]:
    """Canonical MSB-first Huffman lookup: table[next `bits` bits] = (symbol << 5) | code length; 0 = invalid.

    An all-zero length list gives an empty table (allowed until a symbol is actually decoded).
    """
    by_length: list[list[int]] = [[] for _ in range(17)]
    max_length = 0
    for symbol in range(count):
        length = lengths[symbol]
        if length:
            if length > 16:
                raise FormatError("invalid LZX Huffman code length")
            by_length[length].append(symbol)
            if length > max_length:
                max_length = length
    if not max_length:
        return [], 0
    size = 1 << max_length
    table = [0] * size
    pos = 0
    for length in range(1, max_length + 1):
        span = 1 << (max_length - length)
        for symbol in by_length[length]:
            if pos + span > size:
                raise FormatError("invalid LZX Huffman table")
            table[pos:pos + span] = [(symbol << 5) | length] * span
            pos += span
    return table, max_length


class _Bits:
    """16-bit little-endian words read MSB first (the LZX bitstream)."""

    __slots__ = ("buf", "end", "pos", "start", "bitbuf", "bitsleft")

    def __init__(self, data: bytes) -> None:
        self.buf = bytes(data) + b"\0" * 64
        self.end = len(data)
        self.pos = 0
        self.start = 0
        self.bitbuf = 0
        self.bitsleft = 0

    def _fill(self) -> None:
        pos = self.pos
        if pos > self.end + 16:
            raise FormatError("truncated LZX data")
        self.bitbuf = ((self.bitbuf & ((1 << self.bitsleft) - 1)) << 16) | self.buf[pos] | (self.buf[pos + 1] << 8)
        self.pos = pos + 2
        self.bitsleft += 16

    def read(self, count: int) -> int:
        while self.bitsleft < count:
            self._fill()
        self.bitsleft -= count
        return (self.bitbuf >> self.bitsleft) & ((1 << count) - 1)

    def symbol(self, table: list[int], bits: int) -> int:
        if not bits:
            raise FormatError("invalid LZX Huffman code")
        while self.bitsleft < 16:
            self._fill()
        entry = table[(self.bitbuf >> (self.bitsleft - bits)) & ((1 << bits) - 1)]
        if not entry & 31:
            raise FormatError("invalid LZX Huffman code")
        self.bitsleft -= entry & 31
        return entry >> 5

    def restart(self, pos: int) -> None:
        self.pos = self.start = pos
        self.bitbuf = self.bitsleft = 0

    def consumed_bits(self) -> int:
        return (self.pos - self.start) * 8 - self.bitsleft


def _read_lengths(bits: _Bits, lengths: bytearray, first: int, last: int) -> None:
    pretree = bytearray(bits.read(4) for _ in range(20))
    table, table_bits = build_decode_table(pretree, 20)
    x = first
    cap = len(lengths)
    while x < last:
        code = bits.symbol(table, table_bits)
        if code <= 16:
            lengths[x] = (lengths[x] - code) % 17
            x += 1
            continue
        if code == 17:
            run, value = bits.read(4) + 4, 0
        elif code == 18:
            run, value = bits.read(5) + 20, 0
        else:
            run = bits.read(1) + 4
            code = bits.symbol(table, table_bits)
            if code > 16:
                raise FormatError("corrupt LZX length table")
            value = (lengths[x] - code) % 17
        if x + run > cap:
            raise FormatError("corrupt LZX length table")
        lengths[x:x + run] = bytes((value,)) * run
        x += run


def _decode_run(bits: _Bits, out: bytearray, target: int, aligned: bool, main: tuple, length: tuple,
                align: tuple, repeats: tuple[int, int, int]) -> tuple[int, int, int]:
    """Decode verbatim / aligned-offset symbols until ``out`` reaches ``target`` bytes (a match may overrun)."""
    buf = bits.buf
    limit = bits.end + 16
    pos, bb, bl = bits.pos, bits.bitbuf, bits.bitsleft
    main_table, main_bits = main
    length_table, length_bits = length
    align_table, align_bits = align
    if not main_bits:
        raise FormatError("empty LZX main tree")
    main_mask = (1 << main_bits) - 1
    length_mask = (1 << length_bits) - 1
    align_mask = (1 << align_bits) - 1
    extra_bits, position_base = _EXTRA_BITS, _POSITION_BASE
    r0, r1, r2 = repeats
    n = len(out)
    append = out.append
    while n < target:
        if bl < 16:
            if pos > limit:
                raise FormatError("truncated LZX data")
            bb = ((bb & ((1 << bl) - 1)) << 16) | buf[pos] | (buf[pos + 1] << 8)
            pos += 2
            bl += 16
        entry = main_table[(bb >> (bl - main_bits)) & main_mask]
        size = entry & 31
        if not size:
            raise FormatError("invalid LZX Huffman code")
        bl -= size
        symbol = entry >> 5
        if symbol < 256:
            append(symbol)
            n += 1
            continue
        symbol -= 256
        match_length = symbol & 7
        if match_length == 7:
            if not length_bits:
                raise FormatError("invalid LZX length code")
            if bl < 16:
                if pos > limit:
                    raise FormatError("truncated LZX data")
                bb = ((bb & ((1 << bl) - 1)) << 16) | buf[pos] | (buf[pos + 1] << 8)
                pos += 2
                bl += 16
            entry = length_table[(bb >> (bl - length_bits)) & length_mask]
            size = entry & 31
            if not size:
                raise FormatError("invalid LZX Huffman code")
            bl -= size
            match_length += entry >> 5
        match_length += 2
        slot = symbol >> 3
        if slot > 2:
            extra = extra_bits[slot]
            offset = position_base[slot] - 2
            if aligned and extra >= 3:
                if extra > 3:
                    verbatim = extra - 3
                    while bl < verbatim:
                        if pos > limit:
                            raise FormatError("truncated LZX data")
                        bb = ((bb & ((1 << bl) - 1)) << 16) | buf[pos] | (buf[pos + 1] << 8)
                        pos += 2
                        bl += 16
                    bl -= verbatim
                    offset += ((bb >> bl) & ((1 << verbatim) - 1)) << 3
                if not align_bits:
                    raise FormatError("invalid LZX aligned code")
                if bl < 16:
                    if pos > limit:
                        raise FormatError("truncated LZX data")
                    bb = ((bb & ((1 << bl) - 1)) << 16) | buf[pos] | (buf[pos + 1] << 8)
                    pos += 2
                    bl += 16
                entry = align_table[(bb >> (bl - align_bits)) & align_mask]
                size = entry & 31
                if not size:
                    raise FormatError("invalid LZX Huffman code")
                bl -= size
                offset += entry >> 5
            elif extra:
                while bl < extra:
                    if pos > limit:
                        raise FormatError("truncated LZX data")
                    bb = ((bb & ((1 << bl) - 1)) << 16) | buf[pos] | (buf[pos + 1] << 8)
                    pos += 2
                    bl += 16
                bl -= extra
                offset += (bb >> bl) & ((1 << extra) - 1)
            else:
                offset = 1
            r2, r1, r0 = r1, r0, offset
        elif slot == 0:
            offset = r0
        elif slot == 1:
            offset = r1
            r1, r0 = r0, offset
        else:
            offset = r2
            r2, r0 = r0, offset
        if not 0 < offset <= n:
            raise FormatError("invalid LZX match offset")
        src = n - offset
        if offset >= match_length:
            out += out[src:src + match_length]
        else:
            out += (out[src:n] * (match_length // offset + 1))[:match_length]
        n += match_length
    bits.pos, bits.bitbuf, bits.bitsleft = pos, bb, bl
    return r0, r1, r2


def _e8_translate(data: bytearray, file_size: int, first_frame: int, frame_size: int) -> bytes:
    out = bytearray(data)
    for frame in range(first_frame, min((len(out) + frame_size - 1) // frame_size, 32768)):
        start = frame * frame_size
        end = min(start + frame_size, len(out))
        if end - start <= 6:
            continue
        pos, stop = start, end - 10
        while pos < stop:
            pos = out.find(b"\xe8", pos, stop)
            if pos < 0:
                break
            absolute = int.from_bytes(out[pos + 1:pos + 5], "little", signed=True)
            if -pos <= absolute < file_size:
                relative = absolute - pos if absolute >= 0 else absolute + file_size
                out[pos + 1:pos + 5] = (relative & 0xFFFFFFFF).to_bytes(4, "little")
            pos += 5
    return bytes(out)


def lzx_decompress(data: bytes, window_bits: int, out_length: int, frame_size: int = LZX_FRAME) -> bytes:
    """Decompress one LZX stream that starts with a full reset and yields ``out_length`` bytes."""
    if window_bits not in _POSITION_SLOTS:
        raise FormatError("unsupported LZX window size")
    if out_length <= 0:
        return b""
    if out_length > MAX_LZX_OUTPUT:
        raise FormatError("LZX stream is too large")
    main_elements = 256 + (_POSITION_SLOTS[window_bits] << 3)
    bits = _Bits(data)
    out = bytearray()
    repeats = (1, 1, 1)
    main_lengths = bytearray(_MAIN_MAX + 64)
    length_lengths = bytearray(_LENGTH_SYMBOLS + 64)
    main: tuple = ([], 0)
    length: tuple = ([], 0)
    align: tuple = ([], 0)
    block_type = block_remaining = block_length = 0
    e8_size = 0
    e8_frame: int | None = None
    if bits.read(1):
        e8_size = (bits.read(16) << 16) | bits.read(16)
    frame_end = min(frame_size, out_length)
    while len(out) < out_length:
        if block_remaining == 0:
            if block_type == BLOCK_UNCOMPRESSED:
                bits.restart(bits.pos + (block_length & 1))
            block_type = bits.read(3)
            block_length = (bits.read(16) << 8) | bits.read(8)
            block_remaining = block_length
            if block_type == BLOCK_ALIGNED:
                align = build_decode_table(bytearray(bits.read(3) for _ in range(8)), 8)
            if block_type in (BLOCK_VERBATIM, BLOCK_ALIGNED):
                _read_lengths(bits, main_lengths, 0, 256)
                _read_lengths(bits, main_lengths, 256, main_elements)
                main = build_decode_table(main_lengths, main_elements)
                if main_lengths[0xE8] and e8_frame is None:
                    e8_frame = len(out) // frame_size
                _read_lengths(bits, length_lengths, 0, _LENGTH_SYMBOLS)
                length = build_decode_table(length_lengths, _LENGTH_SYMBOLS)
            elif block_type == BLOCK_UNCOMPRESSED:
                if e8_frame is None:
                    e8_frame = len(out) // frame_size
                # skip 1-16 bits of padding to the next 16-bit boundary, then R0..R2 stored little-endian
                bits.restart(bits.start + 2 * (bits.consumed_bits() // 16 + 1))
                if bits.pos + 12 > bits.end:
                    raise FormatError("truncated LZX data")
                repeats = struct.unpack_from("<3L", bits.buf, bits.pos)
                bits.restart(bits.pos + 12)
            else:
                raise FormatError("invalid LZX block type")
            if bits.pos > bits.end + 16:
                raise FormatError("truncated LZX data")
        run = min(block_remaining, frame_end - len(out))
        if block_type == BLOCK_UNCOMPRESSED:
            if bits.pos + run > bits.end:
                raise FormatError("truncated LZX data")
            out += bits.buf[bits.pos:bits.pos + run]
            bits.pos += run
            block_remaining -= run
        else:
            before = len(out)
            repeats = _decode_run(bits, out, before + run, block_type == BLOCK_ALIGNED, main, length, align, repeats)
            produced = len(out) - before
            if produced > block_remaining:
                raise FormatError("LZX match runs past the end of its block")
            block_remaining -= produced
        if len(out) >= frame_end:
            if block_type != BLOCK_UNCOMPRESSED:  # re-align the bitstream to 16 bits after every frame
                consumed = bits.consumed_bits()
                bits.restart(bits.start + 2 * ((consumed + 15) // 16))
            frame_end = min(frame_end + frame_size, out_length)
    del out[out_length:]
    if e8_size and e8_frame is not None:
        return _e8_translate(out, e8_size, e8_frame, frame_size)
    return bytes(out)


# ---------------------------------------------------------------- container

def _encint(data: bytes, pos: int, end: int) -> tuple[int, int]:
    value = 0
    for _ in range(10):
        if pos >= end:
            raise FormatError("truncated CHM directory entry")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise FormatError("corrupt CHM directory entry")


def _key(name: str) -> str:
    return name.replace("\\", "/").lstrip("/").lower()


class _LzxSection:
    def __init__(self, chm: "_ChmFile") -> None:
        control = chm.read(_CONTROL)
        reset = chm.read(_RESET_TABLE)
        content = chm.read(_CONTENT)
        if control is None or reset is None or content is None:
            raise FormatError("CHM compressed section is incomplete")
        if len(control) < 24 or control[4:8] != b"LZXC":
            raise FormatError("unsupported CHM compression")
        version, reset_interval, window_size = struct.unpack_from("<LLL", control, 8)
        if version == 2:
            reset_interval *= LZX_FRAME
            window_size *= LZX_FRAME
        elif version != 1:
            raise FormatError("unsupported CHM LZX control data")
        window_bits = {1 << bits: bits for bits in _POSITION_SLOTS}.get(window_size)
        if window_bits is None:
            raise FormatError("unsupported CHM LZX window size")
        if not reset_interval or reset_interval % LZX_FRAME or reset_interval > MAX_LZX_OUTPUT:
            raise FormatError("unsupported CHM LZX reset interval")
        if len(reset) < 0x28:
            raise FormatError("corrupt CHM reset table")
        _version, block_count, entry_size, table_offset = struct.unpack_from("<LLLL", reset, 0)
        uncompressed, compressed, block_length = struct.unpack_from("<QQQ", reset, 0x10)
        if block_length != LZX_FRAME or entry_size != 8 or block_count > 10_000_000 \
                or table_offset + 8 * block_count > len(reset):
            raise FormatError("corrupt CHM reset table")
        if uncompressed > MAX_SECTION_BYTES:
            raise FormatError("CHM compressed section is too large")
        self.entries = struct.unpack_from(f"<{block_count}Q", reset, table_offset)
        self.window_bits = window_bits
        self.reset_interval = reset_interval
        self.frames_per_reset = reset_interval // LZX_FRAME
        self.total = uncompressed
        self.content = content
        self.compressed = min(compressed or len(content), len(content))
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.cached_bytes = 0

    def _group(self, group: int) -> bytes:
        cached = self.cache.get(group)
        if cached is not None:
            self.cache.move_to_end(group)
            return cached
        first = group * self.frames_per_reset
        if first >= len(self.entries):
            raise FormatError("CHM reset table is too short")
        start = self.entries[first]
        following = first + self.frames_per_reset
        end = self.entries[following] if following < len(self.entries) else self.compressed
        if not start <= end <= len(self.content):
            raise FormatError("corrupt CHM reset table offsets")
        length = min(self.reset_interval, self.total - group * self.reset_interval)
        data = lzx_decompress(self.content[start:end], self.window_bits, length)
        self.cache[group] = data
        self.cached_bytes += len(data)
        while self.cached_bytes > CACHE_BYTES and len(self.cache) > 1:
            _, dropped = self.cache.popitem(last=False)
            self.cached_bytes -= len(dropped)
        return data

    def read(self, offset: int, length: int) -> bytes:
        if offset + length > self.total:
            raise FormatError("CHM entry lies outside the compressed section")
        out = bytearray()
        pos, end = offset, offset + length
        while pos < end:
            group = pos // self.reset_interval
            data = self._group(group)
            base = group * self.reset_interval
            piece = data[pos - base:min(end - base, len(data))]
            if not piece:
                raise FormatError("truncated CHM compressed data")
            out += piece
            pos += len(piece)
        return bytes(out)


class _ChmFile:
    def __init__(self, data: bytes) -> None:
        if data[:4] != b"ITSF" or len(data) < 0x58:
            raise FormatError("not a CHM file")
        version, header_length = struct.unpack_from("<LL", data, 4)
        if version not in (2, 3) or header_length < 0x58:
            raise FormatError("unsupported CHM version")
        self.lcid = struct.unpack_from("<L", data, 0x14)[0]
        dir_offset, dir_length = struct.unpack_from("<QQ", data, 0x48)
        if version >= 3 and header_length >= 0x60 and len(data) >= 0x60:
            content_offset = struct.unpack_from("<Q", data, 0x58)[0]
        else:
            content_offset = dir_offset + dir_length
        if dir_offset + dir_length > len(data) or content_offset > len(data):
            raise FormatError("corrupt CHM header")
        self.data = data
        self.content_offset = content_offset
        self.entries: dict[str, tuple[str, int, int, int]] = {}
        self._read_directory(data[dir_offset:dir_offset + dir_length])
        self._section1: _LzxSection | None = None
        self._section1_error: FormatError | None = None
        self.total_read = 0

    def _read_directory(self, directory: bytes) -> None:
        if directory[:4] != b"ITSP" or len(directory) < 0x54:
            raise FormatError("corrupt CHM directory")
        header_length, _unknown, chunk_size = struct.unpack_from("<LLL", directory, 8)
        chunk_count = struct.unpack_from("<L", directory, 0x2C)[0]
        if not 0x20 <= chunk_size <= 1024 * 1024 or header_length > len(directory):
            raise FormatError("corrupt CHM directory")
        chunk_count = min(chunk_count, (len(directory) - header_length) // chunk_size)
        for index in range(chunk_count):
            start = header_length + index * chunk_size
            chunk = directory[start:start + chunk_size]
            if chunk[:4] != b"PMGL":
                continue
            free = struct.unpack_from("<L", chunk, 4)[0]
            end = max(0x14, min(chunk_size, chunk_size - free if free < chunk_size else 0x14))
            pos = 0x14
            try:
                while pos < end:
                    name_length, pos = _encint(chunk, pos, end)
                    if name_length == 0 or pos + name_length > end:
                        break
                    name = chunk[pos:pos + name_length].decode("utf-8", "replace")
                    pos += name_length
                    section, pos = _encint(chunk, pos, end)
                    offset, pos = _encint(chunk, pos, end)
                    length, pos = _encint(chunk, pos, end)
                    self.entries.setdefault(_key(name), (name, section, offset, length))
                    if len(self.entries) > MAX_ENTRIES:
                        raise FormatError("CHM directory has too many entries")
            except FormatError as err:
                if "too many" in str(err):
                    raise
                log.info("chm: stopping at a corrupt directory chunk: %s", err)
        if not self.entries:
            raise FormatError("CHM directory is empty")

    def names(self) -> list[str]:
        return list(self.entries)

    def has(self, name: str) -> bool:
        return _key(name) in self.entries

    def read(self, name: str) -> bytes | None:
        entry = self.entries.get(_key(name))
        if entry is None:
            return None
        _, section, offset, length = entry
        if length > MAX_FILE_BYTES:
            raise FormatError("CHM entry is too large")
        self.total_read += length
        if self.total_read > MAX_TOTAL_READ:
            raise _BudgetError("CHM file expands beyond the size limit")
        if section == 0:
            start = self.content_offset + offset
            if start + length > len(self.data):
                raise FormatError("CHM entry lies outside the file")
            return self.data[start:start + length]
        if section != 1:
            return None
        if self._section1 is None:
            if self._section1_error is not None:
                raise self._section1_error
            try:
                self._check_namelist()
                self._section1 = _LzxSection(self)
            except FormatError as err:
                self._section1_error = err
                raise
        return self._section1.read(offset, length) if length else b""

    def _check_namelist(self) -> None:
        names = self.read(_NAMELIST)
        if not names or len(names) < 4:
            return
        count = struct.unpack_from("<H", names, 2)[0]
        pos = 4
        found: list[str] = []
        for _ in range(min(count, 16)):
            if pos + 2 > len(names):
                break
            length = struct.unpack_from("<H", names, pos)[0]
            found.append(names[pos + 2:pos + 2 + 2 * length].decode("utf-16-le", "replace"))
            pos += 2 + 2 * length + 2
        if len(found) > 1 and found[1] != "MSCompressed":
            raise FormatError(f"unsupported CHM section: {found[1][:40]}")


# ---------------------------------------------------------------- metadata, TOC, topics

_LCID_LANGUAGES = {
    0x01: "ar", 0x02: "bg", 0x03: "ca", 0x04: "zh", 0x05: "cs", 0x06: "da", 0x07: "de", 0x08: "el", 0x09: "en",
    0x0A: "es", 0x0B: "fi", 0x0C: "fr", 0x0D: "he", 0x0E: "hu", 0x10: "it", 0x11: "ja", 0x12: "ko", 0x13: "nl",
    0x14: "no", 0x15: "pl", 0x16: "pt", 0x18: "ro", 0x19: "ru", 0x1A: "hr", 0x1B: "sk", 0x1D: "sv", 0x1E: "th",
    0x1F: "tr", 0x21: "id", 0x22: "uk", 0x24: "sl", 0x25: "et", 0x26: "lv", 0x27: "lt", 0x29: "fa", 0x2A: "vi",
}
_PRIMARY_CODEPAGES = {
    0x01: "cp1256", 0x02: "cp1251", 0x05: "cp1250", 0x08: "cp1253", 0x0D: "cp1255", 0x0E: "cp1250", 0x11: "cp932",
    0x12: "cp949", 0x15: "cp1250", 0x18: "cp1250", 0x19: "cp1251", 0x1A: "cp1250", 0x1B: "cp1250", 0x1E: "cp874",
    0x1F: "cp1254", 0x22: "cp1251", 0x23: "cp1251", 0x24: "cp1250", 0x25: "cp1257", 0x26: "cp1257",
    0x27: "cp1257", 0x29: "cp1256", 0x2A: "cp1258",
}


def lcid_codepage(lcid: int) -> str:
    primary = lcid & 0x3FF
    if primary == 0x04:  # Chinese: Taiwan / Hong Kong / Macau use Big5, PRC / Singapore GBK
        return "cp950" if lcid in (0x0404, 0x0C04, 0x1404, 0x7C04) else "gb18030"
    return _PRIMARY_CODEPAGES.get(primary, "cp1252")


def _parse_system(data: bytes | None) -> dict[int, bytes | int]:
    info: dict[int, bytes | int] = {}
    if not data or len(data) < 4:
        return info
    pos = 4
    while pos + 4 <= len(data):
        code, length = struct.unpack_from("<HH", data, pos)
        pos += 4
        value = data[pos:pos + length]
        pos += length
        if code in (0, 2, 3, 6):
            info.setdefault(code, value.split(b"\0", 1)[0])
        elif code == 4 and len(value) >= 4:
            info.setdefault(4, struct.unpack_from("<L", value, 0)[0])
    return info


class _SitemapParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self.active = False
        self.params: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if tag == "object":
            self.active = values.get("type", "").lower() == "text/sitemap"
            self.params = {}
        elif tag == "param" and self.active:
            self.params.setdefault(values.get("name", "").lower(), values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "object" and self.active:
            self.active = False
            if len(self.entries) < MAX_TOPICS * 4:
                self.entries.append((self.params.get("name", ""), self.params.get("local", "")))


_CHARSET = re.compile(r"""<meta\b[^>]*?charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HTML_EXTENSIONS = (".htm", ".html", ".xhtml", ".shtml", ".hhc_htm")
_HEAD_OPEN = re.compile(r"<h([1-3])\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE = {level: re.compile(rf"</h{level}\s*>", re.IGNORECASE) for level in "123"}


def _decode(data: bytes, codepage: str) -> str:
    match = _CHARSET.search(data[:4096].decode("latin-1"))
    if match:
        return decode_text(data, match.group(1))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return decode_text(data, codepage)


def _internal_path(base: str, href: str) -> str:
    """Resolve a reference made from topic ``base`` to a CHM-internal key ('' when external or invalid)."""
    href = href.strip().replace("\\", "/")
    if "::" in href:
        href = "/" + href.split("::", 1)[1].lstrip("/")
    elif _SCHEME.match(href):
        return ""
    if href.startswith("/"):
        path = href.split("#", 1)[0].split("?", 1)[0]
        joined = posixpath.normpath(unquote(path)).lstrip("/")
        return "" if joined.startswith("..") or joined in ("", ".") else joined.lower()
    return resolve_href(base, href).lower()


def _first_heading(markup: str) -> str:
    pos = 0
    for _ in range(20):
        match = _HEAD_OPEN.search(markup, pos, min(len(markup), 400000))
        if not match:
            return ""
        close = _HEAD_CLOSE[match.group(1)].search(markup, match.end(), match.end() + 4000)
        if close:
            text = strip_tags(markup[match.end():close.start()])
            if text:
                return text[:200]
        pos = match.end()
    return ""


def _topic_order(chm: _ChmFile, toc_entries: list[tuple[str, str]], default_topic: str) -> list[tuple[str, str]]:
    topics: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, local in toc_entries:
        key = _internal_path("", local) if local else ""
        if not key or key in seen or not chm.has(key) \
                or key.rsplit(".", 1)[-1] in ("gif", "jpg", "jpeg", "png", "bmp", "css", "js"):
            continue
        seen.add(key)
        topics.append((key, name.strip()))
        if len(topics) >= MAX_TOPICS:
            return topics
    if topics:
        return topics
    names = sorted(name for name in chm.names()
                   if name.endswith(_HTML_EXTENSIONS) and not name.startswith(("#", "$", ":")))
    default_key = _internal_path("", default_topic) if default_topic else ""
    if default_key in names:
        names.remove(default_key)
        names.insert(0, default_key)
    return [(name, "") for name in names[:MAX_TOPICS]]


def convert(path: Path) -> Document:
    chm = _ChmFile(read_limited(path))
    try:
        system = _parse_system(chm.read("/#SYSTEM"))
    except FormatError as err:
        log.info("chm: #SYSTEM unreadable: %s", err)
        system = {}
    lcid = system.get(4) if isinstance(system.get(4), int) else chm.lcid
    codepage = lcid_codepage(int(lcid or 0))

    def text(code: int) -> str:
        value = system.get(code)
        return _decode(value, codepage).strip() if isinstance(value, bytes) and value else ""

    document = Document(title=text(3), language=_LCID_LANGUAGES.get(int(lcid or 0) & 0x3FF, ""), source_format="chm")
    toc_entries: list[tuple[str, str]] = []
    hhc = text(0)
    candidates = [hhc] if hhc else []
    compiled = text(6)
    if compiled:
        candidates.append(compiled + ".hhc")
    candidates += sorted(name for name in chm.names() if name.endswith(".hhc"))
    for candidate in candidates:
        try:
            raw = chm.read(candidate)
        except _BudgetError:
            raise
        except FormatError as err:
            log.info("chm: table of contents unreadable: %s", err)
            continue
        if raw:
            parser = _SitemapParser()
            try:
                parser.feed(_decode(raw, codepage))
                parser.close()
            except Exception as err:  # noqa: BLE001 - html.parser is lenient; a broken TOC just means no TOC
                log.info("chm: table of contents unparsable: %s", err)
            toc_entries = parser.entries
            break
    topics = _topic_order(chm, toc_entries, text(2))
    images: dict[str, str] = {}
    last_error: FormatError | None = None

    def image_resolver(base: str):
        def resolve(src: str) -> str:
            key = _internal_path(base, src)
            if not key:
                return ""
            if key not in images:
                name = ""
                try:
                    data = chm.read(key)
                except _BudgetError:
                    raise
                except FormatError:
                    data = None
                media_type = sniff_image_type(data) if data else ""
                if media_type and media_type != "image/svg+xml":
                    name = document.add_resource(data, media_type, posixpath.splitext(posixpath.basename(key))[0])
                images[key] = name
            return images[key]
        return resolve

    for key, toc_title in topics:
        try:
            raw = chm.read(key)
        except _BudgetError:
            raise
        except FormatError as err:
            last_error = err
            continue
        if not raw:
            continue
        markup = _decode(raw, codepage)
        fragment = sanitize_fragment(extract_body(markup), resolve_src=image_resolver(key))
        if "<img" not in fragment and not strip_tags(fragment):
            continue
        title = toc_title or html_title(markup) or _first_heading(markup) or f"Phần {len(document.chapters) + 1}"
        document.chapters.append(Chapter(re.sub(r"\s+", " ", title).strip()[:300], fragment))
    if not document.chapters:
        if last_error is not None:
            raise last_error
        raise FormatError("CHM file has no readable topics")
    return document
