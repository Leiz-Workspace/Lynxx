"""UMD (Chinese "Universal Mobile Document") e-book converter.

Layout: magic 0x89 0x9B 0x9A 0xDE, then a sequence of
- ``#`` chunks: uint16 id, uint8 flag, uint8 total length (incl. the 5-byte head), payload;
- ``$`` data blocks: uint32 check, uint32 total length (incl. the 9-byte head), payload.
Index chunks (0x81 content index, 0x82 cover, 0x83 chapter offsets, 0x84 chapter
titles, 0x87 page offsets) own the ``$`` block that follows them (matched by its
check value when possible); every other ``$`` block is content: zlib-compressed
UTF-16LE text for text books, one image per block for comic books.
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path

from .base import (Chapter, Document, FormatError, images_document, read_limited, sniff_image_type,
                   split_text_chapters, text_to_paragraphs)

log = logging.getLogger(__name__)

MAGIC = b"\x89\x9b\x9a\xde"
MAX_TEXT_BYTES = 256 * 1024 * 1024
MAX_BLOCK_OUTPUT = 4 * 1024 * 1024  # a content block holds 32 KiB of text; allow generous slack
MAX_CHAPTERS = 20000

TYPE_TEXT, TYPE_COMIC = 1, 2
_STRING_CHUNKS = {0x02: "title", 0x03: "author", 0x04: "year", 0x05: "month", 0x06: "day", 0x07: "category",
                  0x08: "publisher", 0x09: "vendor"}
_OWNER_CHUNKS = {0x81, 0x82, 0x83, 0x84, 0x86, 0x87}


def _utf16(payload: bytes) -> str:
    if len(payload) & 1:
        payload = payload[:-1]
    return payload.decode("utf-16-le", "replace").replace("\x00", "").strip()


def _inflate(block: bytes) -> bytes:
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        engine = zlib.decompressobj(wbits)
        try:
            data = engine.decompress(block, MAX_BLOCK_OUTPUT)
        except zlib.error:
            continue
        if engine.unconsumed_tail:
            raise FormatError("UMD text block expands too much")
        return data
    raise FormatError("UMD text block is not zlib data")


class _UmdFile:
    def __init__(self, data: bytes) -> None:
        if not data.startswith(MAGIC):
            raise FormatError("not a UMD file")
        self.book_type = TYPE_TEXT
        self.meta: dict[str, str] = {}
        self.content_length = 0
        self.owned: dict[int, bytes] = {}
        self.loose: list[bytes] = []
        self._parse(data)

    def _parse(self, data: bytes) -> None:
        pos, size = 4, len(data)
        pending: int | None = None
        expected: dict[int, int] = {}
        while pos < size:
            marker = data[pos]
            if marker == 0x23:  # '#'
                if pos + 5 > size:
                    break
                chunk_id = struct.unpack_from("<H", data, pos + 1)[0]
                length = data[pos + 4]
                if length < 5:
                    raise FormatError("corrupt UMD chunk")
                if pos + length > size:
                    break
                payload = data[pos + 5:pos + length]
                pos += length
                pending = None
                if chunk_id == 0x01:
                    if payload:
                        self.book_type = payload[0]
                elif chunk_id in _STRING_CHUNKS:
                    self.meta[_STRING_CHUNKS[chunk_id]] = _utf16(payload)
                elif chunk_id == 0x0B and len(payload) >= 4:
                    self.content_length = struct.unpack_from("<L", payload, 0)[0]
                elif chunk_id == 0x0C:
                    break  # end of book
                elif chunk_id in _OWNER_CHUNKS:
                    pending = chunk_id
                    if len(payload) >= 4:
                        expected[struct.unpack_from("<L", payload, len(payload) - 4)[0]] = chunk_id
            elif marker == 0x24:  # '$'
                if pos + 9 > size:
                    break
                check, length = struct.unpack_from("<LL", data, pos + 1)
                if length < 9:
                    raise FormatError("corrupt UMD data block")
                if pos + length > size:
                    break
                payload = data[pos + 9:pos + length]
                pos += length
                owner = pending if pending is not None else expected.get(check)
                if owner is not None:
                    expected.pop(check, None)
                    self.owned.setdefault(owner, payload)
                else:
                    self.loose.append(payload)
                pending = None
            else:
                if self.loose:
                    log.warning("umd: unknown marker 0x%02X at %d; ignoring the rest of the file", marker, pos)
                    break
                raise FormatError("corrupt UMD structure")

    def chapter_offsets(self) -> list[int]:
        block = self.owned.get(0x83, b"")
        count = min(len(block) // 4, MAX_CHAPTERS)
        return list(struct.unpack_from(f"<{count}L", block, 0)) if count else []

    def chapter_titles(self) -> list[str]:
        block = self.owned.get(0x84, b"")
        titles: list[str] = []
        pos = 0
        while pos < len(block) and len(titles) < MAX_CHAPTERS:
            length = block[pos]
            pos += 1
            titles.append(_utf16(block[pos:pos + length]))
            pos += length
        return titles

    def text(self) -> bytes:
        parts: list[bytes] = []
        total = 0
        for block in self.loose:
            try:
                chunk = _inflate(block)
            except FormatError as err:
                log.info("umd: skipping a data block: %s", err)
                continue
            total += len(chunk)
            if total > MAX_TEXT_BYTES:
                raise FormatError("UMD text is too large")
            parts.append(chunk)
        raw = b"".join(parts)
        if 0 < self.content_length <= len(raw):
            raw = raw[:self.content_length]
        return raw[:len(raw) & ~1]


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-16-le", "replace")
    return text.replace("\r\n", "\n").replace("\u2029", "\n").replace("\r", "\n").replace("\x00", "")


def _body(text: str, title: str) -> str:
    lines = text.split("\n")
    first = next((k for k, line in enumerate(lines) if line.strip()), None)
    if first is not None and title and lines[first].strip() == title.strip():
        del lines[first]  # the title line repeated at the top of the chapter
    return text_to_paragraphs("\n".join(lines))


def _text_chapters(book: _UmdFile, title: str) -> list[Chapter]:
    raw = book.text()
    if not raw:
        raise FormatError("UMD book has no readable text")
    offsets = book.chapter_offsets()
    titles = book.chapter_titles()
    if not offsets:
        return split_text_chapters(_decode(raw), title)
    marks = sorted((min(offset, len(raw)) & ~1, index) for index, offset in enumerate(offsets))
    chapters: list[Chapter] = []
    if marks[0][0] > 0:
        preface = _decode(raw[:marks[0][0]])
        if preface.strip():
            chapters.append(Chapter(title or "Mở đầu", text_to_paragraphs(preface)))
    for position, (start, index) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(raw)
        if end <= start:
            continue
        name = (titles[index] if index < len(titles) else "") or f"Phần {index + 1}"
        body = _body(_decode(raw[start:end]), name)
        if body:
            chapters.append(Chapter(name[:300], body))
    return chapters


def convert(path: Path) -> Document:
    book = _UmdFile(read_limited(path))
    title = book.meta.get("title", "")
    cover_data = book.owned.get(0x82, b"")
    cover_type = sniff_image_type(cover_data)
    if book.book_type == TYPE_COMIC:
        pages: list[tuple[str, bytes]] = []
        for block in book.loose:
            if not sniff_image_type(block):
                try:
                    block = _inflate(block)
                except FormatError:
                    continue
            if sniff_image_type(block):
                pages.append((f"Trang {len(pages) + 1}", block))
        document = images_document(pages, title, "umd")
    else:
        document = Document(title=title, source_format="umd")
        document.chapters = _text_chapters(book, title)
        if not document.chapters:
            raise FormatError("UMD book has no readable text")
    if cover_type and cover_type != "image/svg+xml":
        document.add_resource(cover_data, cover_type, "cover")
        document.cover = document.resources[-1]
    author = book.meta.get("author", "")
    document.authors = [author] if author else []
    return document
