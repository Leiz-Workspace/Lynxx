"""CBZ writer: page images in reading order + ComicInfo.xml (metadata and one bookmark per chapter)."""

from __future__ import annotations

import html
import os
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..formats.base import sniff_image_type
from .book import ExportBook
from .epub import EXT

esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731


def write_cbz(book: ExportBook, output: str | Path, *, include_cover: bool = True) -> Path:
    if not book.is_images:
        raise ValueError("CBZ chỉ dùng cho truyện tranh (chương ảnh)")
    entries: list[tuple[str, Path]] = []
    bookmarks: list[tuple[int, str]] = []
    if include_cover and book.cover is not None:
        with book.cover.open("rb") as stream:
            media = sniff_image_type(stream.read(64))
        if media in EXT:
            entries.append((f"0000_0000_cover.{EXT[media]}", book.cover))
    for index, chapter in enumerate(book.chapters, 1):
        if chapter.kind != "images" or not chapter.pages:
            continue
        bookmarks.append((len(entries), chapter.title))
        for number, page in enumerate(chapter.pages, 1):
            with page.open("rb") as stream:
                media = sniff_image_type(stream.read(64))
            if media not in EXT:
                continue
            entries.append((f"{index:04d}_{number:04d}.{EXT[media]}", page))
    if not entries:
        raise ValueError("không có trang ảnh nào để xuất")
    marks = dict(bookmarks)
    pages_xml = "".join(
        f'<Page Image="{i}"{" Type=" + chr(34) + "FrontCover" + chr(34) if name.startswith("0000_0000_cover") else ""}'
        f'{" Bookmark=" + chr(34) + esc(marks[i]) + chr(34) if i in marks else ""} />'
        for i, (name, _path) in enumerate(entries)
    )
    info = ['<?xml version="1.0" encoding="utf-8"?>',
            '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            f"  <Title>{esc(book.title)}</Title>", f"  <Series>{esc(book.title)}</Series>"]
    if book.description:
        info.append(f"  <Summary>{esc(book.description[:8000])}</Summary>")
    if book.authors:
        info.append(f"  <Writer>{esc(', '.join(book.authors))}</Writer>")
    if book.genres:
        info.append(f"  <Genre>{esc(', '.join(book.genres[:30]))}</Genre>")
    if book.source_url:
        info.append(f"  <Web>{esc(book.source_url)}</Web>")
    if book.language:
        info.append(f"  <LanguageISO>{esc(book.language.replace('_', '-').split('-')[0])}</LanguageISO>")
    info.append(f"  <PageCount>{len(entries)}</PageCount>")
    info.append(f"  <Pages>{pages_xml}</Pages>")
    info.append("</ComicInfo>")
    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in entries:
                archive.write(path, name)
            archive.writestr("ComicInfo.xml", "\n".join(info).encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output
