"""Minimal, defensive Compound File Binary reader ([MS-CFB], a.k.a. OLE2 / "structured storage").

Legacy Office (.doc/.dot/.wps), Hangul (.hwp 5.x) and a few other formats are
CFB containers: a small FAT file system inside one file. Everything read from
the file is untrusted, so every sector number, offset and length is validated,
sector chains are checked for loops, the directory tree walk is cycle-proof and
stream reads are capped.

API::

    cfb = CompoundFile(data)
    cfb.list_streams()            # ["WordDocument", "1Table", "\\x05SummaryInformation", ...]
    cfb.exists("BodyText/Section0")
    cfb.read("WordDocument", limit=64 * 1024 * 1024)
    read_property_set(cfb.read("\\x05SummaryInformation"))   # {2: "Title", 4: "Author", ...}
"""

from __future__ import annotations

import codecs
import datetime as _dt
import struct
import sys
from array import array
from collections import deque
from dataclasses import dataclass

from .base import FormatError

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAXREGSECT = 0xFFFFFFFA
DIFSECT = 0xFFFFFFFC
FATSECT = 0xFFFFFFFD
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF

TYPE_EMPTY = 0
TYPE_STORAGE = 1
TYPE_STREAM = 2
TYPE_ROOT = 5

DEFAULT_STREAM_LIMIT = 256 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1 << 18
MAX_TREE_DEPTH = 64
MINI_STREAM_CUTOFF = 4096


@dataclass(slots=True)
class DirEntry:
    sid: int
    name: str
    type: int
    left: int
    right: int
    child: int
    start: int
    size: int


def _u32_array(buffer: bytes) -> array:
    """Little-endian uint32 array (portable across host byte orders)."""
    code = "I" if array("I").itemsize == 4 else "L"
    values = array(code)
    values.frombytes(buffer[: len(buffer) - len(buffer) % 4])
    if sys.byteorder == "big":
        values.byteswap()
    return values


class CompoundFile:
    """Read-only view of a CFB container held in memory."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(MAGIC):
            raise FormatError("not a compound file (OLE2) container")
        self.data = data
        (minor, major, byte_order, sector_shift, mini_shift, _reserved, num_dir_sectors, num_fat_sectors,
         first_dir, _transaction, mini_cutoff, first_minifat, num_minifat, first_difat,
         num_difat) = struct.unpack_from("<HHHHH6sIIIIIIIII", data, 0x18)
        del minor
        if byte_order != 0xFFFE:
            raise FormatError("invalid compound file byte order")
        if major not in (3, 4):
            raise FormatError(f"unsupported compound file version {major}")
        if sector_shift not in (9, 12) or mini_shift != 6:
            raise FormatError("invalid compound file sector size")
        if mini_cutoff != MINI_STREAM_CUTOFF:
            raise FormatError("invalid compound file mini stream cutoff")
        self.major = major
        self.sector_shift = sector_shift
        self.sector_size = 1 << sector_shift
        if len(data) <= self.sector_size:
            raise FormatError("truncated compound file")
        # The last sector may be short (some writers trim trailing zeros): count it anyway.
        self.num_sectors = (len(data) - self.sector_size + self.sector_size - 1) // self.sector_size
        del num_fat_sectors, num_dir_sectors  # informative only: the DIFAT is authoritative
        self.fat = self._load_fat(data, first_difat, num_difat)
        self.entries = self._load_directory(first_dir)
        self._first_minifat = first_minifat
        self._num_minifat = num_minifat
        self._minifat: array | None = None
        self._ministream: bytes | None = None
        self._streams: dict[str, int] = {}
        self._storages: dict[str, int] = {}
        self._order: list[str] = []
        self._storage_order: list[str] = []
        self._walk()

    # ------------------------------------------------------------ sectors

    def _sector(self, sid: int) -> bytes:
        """One full sector (zero-padded when the file ends inside it)."""
        if sid >= self.num_sectors:
            raise FormatError("sector number out of range")
        start = (sid + 1) << self.sector_shift
        chunk = self.data[start:start + self.sector_size]
        if len(chunk) < self.sector_size:
            chunk += b"\0" * (self.sector_size - len(chunk))
        return chunk

    def _load_fat(self, data: bytes, first_difat: int, num_difat: int) -> array:
        fat_sids: list[int] = []
        seen_fat: set[int] = set()

        def add(value: int) -> None:
            if value > MAXREGSECT:
                return
            if value >= self.num_sectors:
                raise FormatError("FAT sector out of range (truncated file?)")
            if value in seen_fat:
                raise FormatError("duplicate FAT sector")
            seen_fat.add(value)
            fat_sids.append(value)

        for value in struct.unpack_from("<109I", data, 0x4C):
            add(value)
        if num_difat:
            per_sector = self.sector_size // 4 - 1
            sid = first_difat
            visited: set[int] = set()
            while sid <= MAXREGSECT and len(visited) < min(num_difat, self.num_sectors):
                if sid >= self.num_sectors or sid in visited:
                    raise FormatError("corrupt DIFAT chain")
                visited.add(sid)
                values = struct.unpack(f"<{per_sector + 1}I", self._sector(sid))
                for value in values[:per_sector]:
                    add(value)
                sid = values[per_sector]
        if not fat_sids:
            raise FormatError("compound file has no FAT")
        return _u32_array(b"".join(self._sector(sid) for sid in fat_sids))

    def _chain(self, start: int, fat: array, limit_count: int, max_sectors: int | None, what: str) -> list[int]:
        """Sector numbers of a chain; ``max_sectors`` stops early when the stream size is known."""
        chain: list[int] = []
        seen: set[int] = set()
        sid = start
        while sid != ENDOFCHAIN:
            if max_sectors is not None and len(chain) >= max_sectors:
                break
            if sid > MAXREGSECT or sid >= limit_count or sid >= len(fat):
                raise FormatError(f"corrupt {what} sector chain")
            if sid in seen:
                raise FormatError(f"loop in {what} sector chain")
            seen.add(sid)
            chain.append(sid)
            sid = fat[sid]
        if max_sectors is not None and len(chain) < max_sectors:
            raise FormatError(f"truncated {what} sector chain")
        return chain

    def _load_directory(self, first_dir: int) -> list[DirEntry]:
        chain = self._chain(first_dir, self.fat, self.num_sectors, None, "directory")
        per_sector = self.sector_size // 128
        if len(chain) * per_sector > MAX_DIRECTORY_ENTRIES:
            raise FormatError("compound file directory is too large")
        raw = b"".join(self._sector(sid) for sid in chain)
        entries: list[DirEntry] = []
        for index in range(len(raw) // 128):
            offset = index * 128
            name_len = struct.unpack_from("<H", raw, offset + 64)[0]
            entry_type = raw[offset + 66]
            left, right, child = struct.unpack_from("<III", raw, offset + 68)
            start, size_low, size_high = struct.unpack_from("<III", raw, offset + 116)
            size = size_low if self.major == 3 else size_low | (size_high << 32)
            name_bytes = raw[offset:offset + 64]
            if 2 <= name_len <= 64 and name_len % 2 == 0:
                name_bytes = name_bytes[:name_len - 2]
            name = name_bytes.decode("utf-16-le", errors="replace").split("\0", 1)[0]
            if entry_type not in (TYPE_STORAGE, TYPE_STREAM, TYPE_ROOT):
                entry_type = TYPE_EMPTY
            entries.append(DirEntry(index, name, entry_type, left, right, child, start, size))
        if not entries or entries[0].type != TYPE_ROOT:
            raise FormatError("compound file has no root entry")
        return entries

    # ------------------------------------------------------------ directory tree

    def _siblings(self, top: int, visited: set[int]) -> list[int]:
        """In-order walk of one storage's red-black sibling tree; revisits are dropped (cycle-proof)."""
        result: list[int] = []
        stack: list[int] = []
        node = top
        total = len(self.entries)
        while True:
            while node < total and node not in visited and self.entries[node].type != TYPE_EMPTY:
                visited.add(node)
                stack.append(node)
                node = self.entries[node].left
            if not stack:
                break
            node = stack.pop()
            result.append(node)
            node = self.entries[node].right
        return result

    def _walk(self) -> None:
        visited: set[int] = {0}
        pending: deque[tuple[int, str, int]] = deque([(self.entries[0].child, "", 0)])
        while pending:
            top, prefix, depth = pending.popleft()
            if depth > MAX_TREE_DEPTH:
                raise FormatError("compound file storages are nested too deeply")
            for sid in self._siblings(top, visited):
                entry = self.entries[sid]
                if entry.type == TYPE_ROOT:
                    continue
                path = prefix + entry.name
                key = self._key(path)
                if not key or key in self._streams or key in self._storages:
                    continue
                if entry.type == TYPE_STREAM:
                    self._streams[key] = sid
                    self._order.append(path)
                else:
                    self._storages[key] = sid
                    self._storage_order.append(path)
                    pending.append((entry.child, path + "/", depth + 1))

    # ------------------------------------------------------------ public API

    @staticmethod
    def _key(path: str) -> str:
        return path.replace("\\", "/").strip("/").lower()

    def list_streams(self) -> list[str]:
        """Every stream path ("Storage/Stream"), in directory order."""
        return list(self._order)

    def list_storages(self) -> list[str]:
        return list(self._storage_order)

    def exists(self, path: str) -> bool:
        key = self._key(path)
        return key in self._streams or key in self._storages

    def is_stream(self, path: str) -> bool:
        return self._key(path) in self._streams

    def stream_size(self, path: str) -> int:
        sid = self._streams.get(self._key(path))
        if sid is None:
            raise FormatError(f"missing stream: {path}")
        return self.entries[sid].size

    def read(self, path: str, limit: int = DEFAULT_STREAM_LIMIT) -> bytes:
        """The whole stream; :class:`FormatError` when missing, larger than ``limit`` or corrupt."""
        sid = self._streams.get(self._key(path))
        if sid is None:
            raise FormatError(f"missing stream: {path}")
        entry = self.entries[sid]
        if entry.size > limit:
            raise FormatError(f"stream is too large: {path}")
        if entry.size == 0:
            return b""
        if entry.size < MINI_STREAM_CUTOFF:
            return self._read_mini(entry)
        return self._read_regular(entry.start, entry.size)

    def _read_regular(self, start: int, size: int) -> bytes:
        needed = (size + self.sector_size - 1) // self.sector_size
        if needed > self.num_sectors:
            raise FormatError("stream is larger than the file")
        chain = self._chain(start, self.fat, self.num_sectors, needed, "stream")
        shift, data = self.sector_shift, self.data
        parts = [data[(sid + 1) << shift:(sid + 2) << shift] for sid in chain]
        return b"".join(parts)[:size]

    def _load_mini(self) -> tuple[array, bytes]:
        if self._minifat is None or self._ministream is None:
            minifat = array("I")
            if self._num_minifat and self._first_minifat <= MAXREGSECT:
                count = min(self._num_minifat, self.num_sectors)
                chain = self._chain(self._first_minifat, self.fat, self.num_sectors, count, "mini FAT")
                minifat = _u32_array(b"".join(self._sector(sid) for sid in chain))
            root = self.entries[0]
            ministream = b""
            if root.size and root.start <= MAXREGSECT:
                if root.size > DEFAULT_STREAM_LIMIT:
                    raise FormatError("mini stream is too large")
                ministream = self._read_regular(root.start, root.size)
            self._minifat, self._ministream = minifat, ministream
        return self._minifat, self._ministream

    def _read_mini(self, entry: DirEntry) -> bytes:
        minifat, ministream = self._load_mini()
        needed = (entry.size + 63) // 64
        mini_count = len(ministream) // 64 + (1 if len(ministream) % 64 else 0)
        chain = self._chain(entry.start, minifat, mini_count, needed, "mini stream")
        return b"".join(ministream[sid * 64:(sid + 1) * 64] for sid in chain)[:entry.size]


# ---------------------------------------------------------------- property sets ([MS-OLEPS])

PID_CODEPAGE = 1
PID_TITLE = 2
PID_SUBJECT = 3
PID_AUTHOR = 4
PID_KEYWORDS = 5
PID_COMMENTS = 6

_SPECIAL_CODEPAGES = {
    1200: "utf-16-le", 1201: "utf-16-be", 65001: "utf-8", 65000: "utf-7", 10000: "mac_roman",
    20127: "ascii", 28591: "latin-1", 20932: "euc_jp", 51932: "euc_jp", 51949: "euc_kr", 54936: "gb18030",
}


def codec_for_codepage(codepage: int, default: str = "cp1252") -> str:
    """Python codec name for a Windows code page number (``default`` when unknown)."""
    codepage &= 0xFFFF
    name = _SPECIAL_CODEPAGES.get(codepage) or f"cp{codepage}"
    try:
        codecs.lookup(name)
    except LookupError:
        return default
    return name


def _filetime(value: int) -> object:
    if not value:
        return None
    try:
        return _dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(microseconds=value // 10)
    except (OverflowError, ValueError):
        return value


def read_property_set(data: bytes, max_properties: int = 1024, max_string: int = 1 << 20) -> dict[int, object]:
    """Decode the first property set of a PropertySetStream (e.g. ``\\x05SummaryInformation``).

    Returns ``{pid: value}`` for simple scalar types and strings (VT_LPSTR honours the
    code page in PID 1, VT_LPWSTR is UTF-16). Unknown or malformed properties are skipped;
    a malformed stream header raises :class:`FormatError`.
    """
    if len(data) < 48:
        raise FormatError("property set stream is too short")
    byte_order, _version = struct.unpack_from("<HH", data, 0)
    if byte_order != 0xFFFE:
        raise FormatError("invalid property set byte order")
    num_sets = struct.unpack_from("<I", data, 24)[0]
    if num_sets < 1:
        return {}
    base = struct.unpack_from("<I", data, 44)[0]
    if base + 8 > len(data):
        raise FormatError("property set offset out of range")
    set_size, count = struct.unpack_from("<II", data, base)
    end = len(data) if set_size < 8 else min(len(data), base + set_size)
    count = min(count, max_properties, max(0, (end - base - 8) // 8))
    entries: list[tuple[int, int]] = []
    for index in range(count):
        pid, offset = struct.unpack_from("<II", data, base + 8 + index * 8)
        position = base + offset
        if offset >= 8 and position + 4 <= end:
            entries.append((pid, position))

    def value_at(position: int, codec: str) -> object:
        vt = struct.unpack_from("<H", data, position)[0]
        body = position + 4

        def need(size: int) -> bool:
            return body + size <= end

        if vt in (0, 1):
            return None
        if vt == 0x02 and need(2):
            return struct.unpack_from("<h", data, body)[0]
        if vt == 0x12 and need(2):
            return struct.unpack_from("<H", data, body)[0]
        if vt in (0x03, 0x16) and need(4):
            return struct.unpack_from("<i", data, body)[0]
        if vt in (0x13, 0x17) and need(4):
            return struct.unpack_from("<I", data, body)[0]
        if vt == 0x0B and need(2):
            return struct.unpack_from("<H", data, body)[0] != 0
        if vt == 0x10 and need(1):
            return struct.unpack_from("<b", data, body)[0]
        if vt == 0x11 and need(1):
            return data[body]
        if vt == 0x14 and need(8):
            return struct.unpack_from("<q", data, body)[0]
        if vt == 0x15 and need(8):
            return struct.unpack_from("<Q", data, body)[0]
        if vt == 0x04 and need(4):
            return struct.unpack_from("<f", data, body)[0]
        if vt == 0x05 and need(8):
            return struct.unpack_from("<d", data, body)[0]
        if vt == 0x40 and need(8):
            return _filetime(struct.unpack_from("<Q", data, body)[0])
        if vt in (0x1E, 0x08) and need(4):
            size = struct.unpack_from("<I", data, body)[0]
            if size > max_string or not need(4 + size):
                return None
            raw = data[body + 4:body + 4 + size]
            if codec.startswith("utf-16"):
                raw = raw[: len(raw) - len(raw) % 2]
            return raw.decode(codec, errors="replace").split("\0", 1)[0]
        if vt == 0x1F and need(4):
            chars = struct.unpack_from("<I", data, body)[0]
            if chars * 2 > max_string or not need(4 + chars * 2):
                return None
            return data[body + 4:body + 4 + chars * 2].decode("utf-16-le", errors="replace").split("\0", 1)[0]
        return None

    codec = "cp1252"
    for pid, position in entries:
        if pid == PID_CODEPAGE:
            codepage = value_at(position, codec)
            if isinstance(codepage, int):
                codec = codec_for_codepage(codepage)
            break
    result: dict[int, object] = {}
    for pid, position in entries:
        if pid == 0 or pid in result:  # PID 0 is the property-name dictionary
            continue
        value = value_at(position, codec)
        if value is not None:
            result[pid] = value
    return result


def summary_title_author(cfb: CompoundFile, stream: str = "\x05SummaryInformation") -> tuple[str, list[str]]:
    """(title, authors) from a summary property set stream; empty values when absent or unreadable."""
    try:
        if not cfb.is_stream(stream):
            return "", []
        props = read_property_set(cfb.read(stream, limit=4 * 1024 * 1024))
    except FormatError:
        return "", []
    title = props.get(PID_TITLE)
    author = props.get(PID_AUTHOR)
    title = " ".join(title.split()) if isinstance(title, str) else ""
    authors: list[str] = []
    if isinstance(author, str):
        for part in author.split(";"):
            part = " ".join(part.split())
            if part and part not in authors:
                authors.append(part)
    return title[:500], authors[:20]
