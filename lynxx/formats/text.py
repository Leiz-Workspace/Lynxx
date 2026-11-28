"""Plain text (.txt) -> :class:`Document`.

The bytes are decoded with the shared detector (BOM, UTF-8, UTF-16 without BOM,
chardet when installed, legacy CJK / Vietnamese code pages) and split into
chapters at heading-like lines. A chapter that would still be huge (a novel
without recognizable headings) is cut at paragraph boundaries so the reader
never has to lay out tens of megabytes in one page.
"""

from __future__ import annotations

from pathlib import Path

from .base import Chapter, Document, FormatError, decode_text, read_limited, split_text_chapters
from .htmldoc import clean_xml_text

MAX_CHAPTER_HTML = 1024 * 1024
"""Chapters whose XHTML exceeds this are cut into parts of about ``PART_HTML`` characters."""
PART_HTML = 512 * 1024


def _split_large(chapter: Chapter) -> list[Chapter]:
    markup = chapter.html
    if len(markup) <= MAX_CHAPTER_HTML:
        return [chapter]
    parts: list[str] = []
    start, size = 0, len(markup)
    while start < size:
        cut = markup.find("</p>", start + PART_HTML)
        if cut < 0 or size - start <= MAX_CHAPTER_HTML:
            parts.append(markup[start:])
            break
        cut += len("</p>")
        parts.append(markup[start:cut])
        start = cut
    return [Chapter(chapter.title if index == 1 else f"{chapter.title} ({index})", part)
            for index, part in enumerate(parts, 1)]


def convert(path: Path) -> Document:
    path = Path(path)
    data = read_limited(path)
    text = clean_xml_text(decode_text(data))
    chapters = split_text_chapters(text, default_title=path.stem)
    if not chapters:
        raise FormatError("text file is empty")
    result: list[Chapter] = []
    for chapter in chapters:
        result.extend(_split_large(chapter))
    return Document(title=path.stem, chapters=result, source_format="txt")
