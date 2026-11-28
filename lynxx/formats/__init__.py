"""Local document converters: every supported file becomes a :class:`Document`.

``convert(path)`` sniffs the real container (a ``.doc`` that is really RTF or
DOCX is common), dispatches to the converter module and validates the result.
PDF is not converted: the reader shows it with the PDF viewer.
"""

from __future__ import annotations

import importlib
import re
import zipfile
from pathlib import Path

from .base import Chapter, Document, FormatError, Resource

_MODULES = {
    "txt": "text",
    "md": "markdown", "markdown": "markdown",
    "html": "htmldoc", "htm": "htmldoc", "xhtml": "htmldoc",
    "rtf": "rtf",
    "fb2": "fb2", "fbz": "fb2",
    "docx": "ooxml", "docm": "ooxml", "dotx": "ooxml", "dotm": "ooxml",
    "odt": "odf", "ott": "odf", "fodt": "odf", "odf": "odf",
    "hwpx": "hwpx", "hwpml": "hwpx",
    "doc": "msdoc", "dot": "msdoc", "wps": "msdoc",
    "hwp": "hwp",
    "mobi": "mobi", "prc": "mobi", "azw": "mobi", "azw3": "mobi",
    "umd": "umd",
    "chm": "chm",
    "epub": "epub",
    "cbz": "comic", "zip": "comic",
}
SUPPORTED_EXTENSIONS = tuple(sorted(set(_MODULES) | {"pdf"}))
"""Extensions accepted by "Import from device" (the order the UI lists them in is its own)."""

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff_format(path: str | Path, ext: str = "") -> str:
    """The format to convert with: the extension unless the bytes clearly say otherwise."""
    candidate = Path(path)
    ext = (ext or candidate.suffix.lstrip(".")).lower()
    with candidate.open("rb") as stream:
        head = stream.read(4096)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"{\\rtf"):
        return "rtf"
    if head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        return ext if ext in {"mobi", "prc", "azw", "azw3"} else "mobi"
    if head.startswith(b"\x89\x9b\x9a\xde"):
        return "umd"
    if head.startswith(b"ITSF"):
        return "chm"
    if head.startswith(_OLE_MAGIC):
        if ext in {"doc", "dot", "wps", "hwp"}:
            return ext
        # an OLE file with another extension: HWP 5 stores "FileHeader" + "BodyText", Word "WordDocument" (UTF-16 names)
        with candidate.open("rb") as stream:
            blob = stream.read(2 * 1024 * 1024)
        return "hwp" if "FileHeader".encode("utf-16-le") in blob and "BodyText".encode("utf-16-le") in blob else "doc"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(candidate) as archive:
                names = set(archive.namelist())
                if "mimetype" in names:
                    mimetype = archive.read("mimetype")[:100].strip()
                    if mimetype == b"application/epub+zip":
                        return "epub"
                    if mimetype.startswith(b"application/vnd.oasis.opendocument.text"):
                        return "odf"
                    if mimetype.startswith(b"application/hwp+zip") or "Contents/section0.xml" in names:
                        return "hwpx"
                if "[Content_Types].xml" in names and any(n.startswith("word/") for n in names):
                    return ext if ext in {"docx", "docm", "dotx", "dotm"} else "docx"
                if "Contents/section0.xml" in names:
                    return "hwpx"
                if any(n.lower().endswith(".fb2") for n in names) and ext in {"fbz", "zip"}:
                    return "fbz"
        except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError, EOFError):
            pass
        return ext if ext in {"cbz", "zip", "fbz", "epub", "docx", "docm", "dotx", "dotm", "odt", "ott", "hwpx"} else "zip"
    text_head = head.lstrip(b"\xef\xbb\xbf \r\n\t")
    if text_head.startswith(b"<?xml") or text_head.startswith(b"<"):
        lowered = head.lower()
        if b"<fictionbook" in lowered:
            return "fb2"
        if b"<hwpml" in lowered:
            return "hwpml"
        if b"office:document" in lowered and ext in {"fodt", "odt", "ott", ""}:
            return "fodt"
        # a web page saved with a Word / text extension ("Save as .doc" of many sites)
        if ext in {"doc", "dot", "wps", "txt", "rtf"} and re.search(rb"<(html|body|head|!doctype html)\b", lowered):
            return "html"
    if ext in {"doc", "dot", "wps"} and head and not head.startswith(_OLE_MAGIC) and b"\x00" not in head[:512]:
        return "txt"  # plain text named .doc
    return ext


def convert(path: str | Path, ext: str = "") -> Document:
    """Convert a local file into a validated :class:`Document` (never touches the network)."""
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise FormatError("not a regular file")
    fmt = sniff_format(candidate, ext)
    if fmt == "pdf":
        raise FormatError("PDF is shown by the PDF viewer, not converted")
    module_name = _MODULES.get(fmt)
    if module_name is None:
        raise FormatError(f"unsupported format: .{fmt or '?'}")
    module = importlib.import_module(f".{module_name}", __name__)
    try:
        document: Document = module.convert(candidate)
    except FormatError:
        raise
    except (MemoryError, RecursionError) as err:
        raise FormatError(f"{fmt}: file is too complex to convert") from err
    except Exception as err:  # noqa: BLE001 - a converter bug must surface as a readable import error
        raise FormatError(f"{fmt}: cannot read file ({type(err).__name__}: {err})") from err
    if not document.title:
        document.title = candidate.stem
    if not document.source_format:
        document.source_format = fmt
    return document.validate()


__all__ = ["Chapter", "Document", "FormatError", "Resource", "SUPPORTED_EXTENSIONS", "convert", "sniff_format"]
