"""Dependency-free image PDF writer: one page per image at its own size, chapter outline (bookmarks), metadata.

JPEG (gray / RGB / CMYK) is embedded as-is (DCTDecode). Non-interlaced PNG without alpha is embedded as-is too
(FlateDecode + PNG predictors); PNG with alpha or interlacing is decoded here (alpha → SMask). Formats PDF cannot
hold (WebP, GIF, AVIF, BMP) raise :class:`NeedsTranscode` listing the files: the app converts those to JPEG with
Chromium and calls again with ``replacements``.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from ..formats.base import sniff_image_type


class NeedsTranscode(ValueError):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(f"{len(paths)} ảnh cần chuyển sang JPEG trước khi tạo PDF")
        self.paths = paths


@dataclass(slots=True)
class PdfImage:
    width: int
    height: int
    color_space: bytes
    bits: int
    filter: bytes
    data: bytes
    decode_parms: bytes = b""
    smask: "PdfImage | None" = None
    decode: bytes = b""


def _jpeg_info(data: bytes) -> tuple[int, int, int, bool]:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG")
    i = 2
    adobe = False
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xEE and data[i + 4:i + 9] == b"Adobe":
            adobe = True
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 10 > n:
                break
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            components = data[i + 9]
            return width, height, components, adobe
        i += 2 + length
    raise ValueError("JPEG has no frame header")


def jpeg_image(data: bytes) -> PdfImage:
    width, height, components, adobe = _jpeg_info(data)
    if components == 1:
        space = b"/DeviceGray"
    elif components == 4:
        space = b"/DeviceCMYK"
    else:
        space = b"/DeviceRGB"
    decode = b"/Decode [1 0 1 0 1 0 1 0]" if components == 4 and adobe else b""
    return PdfImage(width, height, space, 8, b"/DCTDecode", data, decode=decode)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, bpp: int, row_bytes: int) -> bytearray:
    out = bytearray(row_bytes * height)
    prev = bytearray(row_bytes)
    pos = 0
    for y in range(height):
        if pos >= len(raw):
            raise ValueError("truncated PNG data")
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + row_bytes])
        pos += 1 + row_bytes
        if len(line) < row_bytes:
            raise ValueError("truncated PNG row")
        if ftype == 1:
            for x in range(bpp, row_bytes):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ftype == 2:
            for x in range(row_bytes):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(row_bytes):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(row_bytes):
                left = line[x - bpp] if x >= bpp else 0
                upleft = prev[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + _paeth(left, prev[x], upleft)) & 0xFF
        elif ftype != 0:
            raise ValueError("bad PNG filter")
        out[y * row_bytes:(y + 1) * row_bytes] = line
        prev = line
    return out


_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def png_image(data: bytes) -> PdfImage:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG")
    pos = 8
    ihdr = None
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk[:13])
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if not ihdr:
        raise ValueError("PNG has no header")
    width, height, depth, ctype, _comp, _filt, interlace = ihdr
    if width <= 0 or height <= 0 or width * height > 200_000_000:
        raise ValueError("PNG dimensions out of range")
    colors = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if colors is None or depth not in (1, 2, 4, 8, 16):
        raise ValueError("unsupported PNG variant")
    if ctype in (0, 2, 3) and not interlace:  # tRNS transparency is painted opaque
        space = b"/DeviceGray" if ctype == 0 else b"/DeviceRGB"
        if ctype == 3:
            if not palette:
                raise ValueError("PNG palette missing")
            space = b"[/Indexed /DeviceRGB " + str(len(palette) // 3 - 1).encode() + b" <" + palette.hex().encode() + b">]"
        parms = f"/DecodeParms << /Predictor 15 /Colors {1 if ctype in (0, 3) else 3} /BitsPerComponent {depth} /Columns {width} >>".encode()
        return PdfImage(width, height, space, depth, b"/FlateDecode", bytes(idat), parms)
    # alpha and/or interlaced: decode to raw samples (8-bit), split alpha into an SMask
    raw = zlib.decompressobj().decompress(bytes(idat), 1_200_000_000)
    if depth != 8:
        raise ValueError("PNG với alpha / interlace cần độ sâu màu 8 bit")
    bpp = colors
    if not interlace:
        pixels = _unfilter(raw, width, height, bpp, width * bpp)
    else:
        pixels = bytearray(width * height * bpp)
        offset = 0
        for x0, y0, dx, dy in _ADAM7:
            pw = (width - x0 + dx - 1) // dx
            ph = (height - y0 + dy - 1) // dy
            if pw <= 0 or ph <= 0:
                continue
            row = pw * bpp
            size = (row + 1) * ph
            sub = _unfilter(raw[offset:offset + size], pw, ph, bpp, row)
            offset += size
            for j in range(ph):
                for i in range(pw):
                    src = (j * pw + i) * bpp
                    dst = ((y0 + j * dy) * width + x0 + i * dx) * bpp
                    pixels[dst:dst + bpp] = sub[src:src + bpp]
    if ctype in (4, 6):
        n = colors - 1
        color = bytearray(width * height * n)
        alpha = bytearray(width * height)
        mv = memoryview(pixels)
        for c in range(n):
            color[c::n] = mv[c::colors]
        alpha[:] = mv[n::colors]
        space = b"/DeviceGray" if n == 1 else b"/DeviceRGB"
        smask = PdfImage(width, height, b"/DeviceGray", 8, b"/FlateDecode", zlib.compress(bytes(alpha), 6))
        return PdfImage(width, height, space, 8, b"/FlateDecode", zlib.compress(bytes(color), 6), smask=smask)
    if ctype == 3:
        space = b"[/Indexed /DeviceRGB " + str(len(palette) // 3 - 1).encode() + b" <" + palette.hex().encode() + b">]"
    else:
        space = b"/DeviceGray" if ctype == 0 else b"/DeviceRGB"
    return PdfImage(width, height, space, 8, b"/FlateDecode", zlib.compress(bytes(pixels), 6))


def load_image(path: Path) -> PdfImage:
    data = path.read_bytes()
    kind = sniff_image_type(data[:64])
    if kind == "image/jpeg":
        return jpeg_image(data)
    if kind == "image/png":
        return png_image(data)
    raise NeedsTranscode([str(path)])


def pdf_text(value: str) -> bytes:
    """A PDF text string (UTF-16BE with BOM) for titles and bookmarks."""
    return b"<FEFF" + str(value).encode("utf-16-be").hex().upper().encode() + b">"


def write_image_pdf(pages: Iterable[Path], output: str | Path, *, title: str = "", author: str = "",
                    outline: list[tuple[int, str]] | None = None, replacements: dict[str, str] | None = None) -> Path:
    """pages: image files in order; outline: (page index, title) bookmarks; replacements: path -> transcoded JPEG."""
    replacements = replacements or {}
    page_list = [Path(replacements.get(str(p), str(p))) for p in pages]
    if not page_list:
        raise ValueError("at least one page is required")
    pending = []
    for p in page_list:
        with p.open("rb") as stream:
            kind = sniff_image_type(stream.read(64))
        if kind not in {"image/jpeg", "image/png"}:
            pending.append(str(p))
    if pending:
        raise NeedsTranscode(pending)
    output_path = Path(output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False) as temp:
        temporary = Path(temp.name)
    offsets: dict[int, int] = {}
    try:
        with temporary.open("wb") as stream:
            stream.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
            next_id = [5]  # 1 catalog, 2 pages, 3 info, 4 outlines

            def new_id() -> int:
                value = next_id[0]
                next_id[0] += 1
                return value

            def write_obj(num: int, body: bytes, stream_data: bytes | None = None) -> None:
                offsets[num] = stream.tell()
                stream.write(f"{num} 0 obj\n".encode())
                stream.write(body)
                if stream_data is not None:
                    stream.write(b"\nstream\n")
                    stream.write(stream_data)
                    stream.write(b"\nendstream")
                stream.write(b"\nendobj\n")

            def write_image(img: PdfImage) -> int:
                smask_ref = write_image(img.smask) if img.smask else 0
                num = new_id()
                extra = (b" /SMask " + f"{smask_ref} 0 R".encode() if smask_ref else b"") + (b" " + img.decode if img.decode else b"") + \
                        (b" " + img.decode_parms if img.decode_parms else b"")
                body = (f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} /ColorSpace ".encode() + img.color_space +
                        f" /BitsPerComponent {img.bits} /Filter ".encode() + img.filter + extra + f" /Length {len(img.data)} >>".encode())
                write_obj(num, body, img.data)
                return num

            page_refs: list[int] = []
            for page in page_list:
                img = load_image(page)
                image_ref = write_image(img)
                content = f"q\n{img.width} 0 0 {img.height} 0 0 cm\n/Im0 Do\nQ\n".encode()
                content_ref = new_id()
                write_obj(content_ref, f"<< /Length {len(content)} >>".encode(), content)
                page_ref = new_id()
                write_obj(page_ref, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {img.width} {img.height}] /Resources << /XObject << /Im0 {image_ref} 0 R >> >> /Contents {content_ref} 0 R >>".encode())
                page_refs.append(page_ref)
            marks = [(i, t) for i, t in (outline or []) if 0 <= i < len(page_refs)]
            mark_refs = [new_id() for _ in marks]
            for k, ((index, label), ref) in enumerate(zip(marks, mark_refs)):
                links = f" /Prev {mark_refs[k - 1]} 0 R" if k else ""
                links += f" /Next {mark_refs[k + 1]} 0 R" if k + 1 < len(mark_refs) else ""
                write_obj(ref, b"<< /Title " + pdf_text(label) + f" /Parent 4 0 R{links} /Dest [{page_refs[index]} 0 R /Fit] >>".encode())
            if mark_refs:
                write_obj(4, f"<< /Type /Outlines /First {mark_refs[0]} 0 R /Last {mark_refs[-1]} 0 R /Count {len(mark_refs)} >>".encode())
            else:
                write_obj(4, b"<< /Type /Outlines /Count 0 >>")
            write_obj(2, f"<< /Type /Pages /Count {len(page_refs)} /Kids [{' '.join(f'{r} 0 R' for r in page_refs)}] >>".encode())
            info = b"<< /Producer " + pdf_text("Lynxx") + (b" /Title " + pdf_text(title) if title else b"") + (b" /Author " + pdf_text(author) if author else b"") + b" >>"
            write_obj(3, info)
            write_obj(1, b"<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R" + (b" /PageMode /UseOutlines" if mark_refs else b"") + b" >>")
            xref = stream.tell()
            total = next_id[0]
            stream.write(f"xref\n0 {total}\n0000000000 65535 f \n".encode())
            for num in range(1, total):
                stream.write(f"{offsets.get(num, 0):010d} 00000 {'n' if num in offsets else 'f'} \n".encode())
            stream.write(f"trailer\n<< /Size {total} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path
