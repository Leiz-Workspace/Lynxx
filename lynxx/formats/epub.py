"""EPUB 2/3 (and KEPUB) → Document: spine order, NCX / nav titles, sanitized XHTML, embedded images.

A fixed-layout or image-only EPUB (every spine item is just images, e.g. a comic export) becomes an "images" document
so the reader shows pages; anything with text stays reflowable.
"""

from __future__ import annotations

import html
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import (
    Chapter, Document, FormatError, extract_body, html_title, open_zip, read_member, resolve_href, sanitize_fragment,
    sniff_image_type, strip_tags,
)

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container", "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/", "ncx": "http://www.daisy.org/z3986/2005/ncx/", "x": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}
_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)


def _xml(data: bytes) -> ET.Element:
    if _ENTITY_DECL.search(data[:4096]):
        raise FormatError("XML entity declarations are not allowed")
    try:
        return ET.fromstring(data)
    except ET.ParseError as err:
        raise FormatError(f"invalid XML: {err}") from err


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _nav_titles(archive, nav_path: str) -> dict[str, str]:
    titles: dict[str, str] = {}
    try:
        markup = read_member(archive, nav_path, 8 * 1024 * 1024).decode("utf-8", "replace")
    except KeyError:
        return titles
    for href, label in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup, re.IGNORECASE | re.DOTALL):
        target = resolve_href(nav_path, href)
        if target and target not in titles:
            titles[target] = strip_tags(label)[:300]
    return titles


def _ncx_titles(archive, ncx_path: str) -> dict[str, str]:
    titles: dict[str, str] = {}
    try:
        root = _xml(read_member(archive, ncx_path, 16 * 1024 * 1024))
    except (KeyError, FormatError):
        return titles
    for point in root.iter():
        if _local(point.tag) != "navPoint":
            continue
        label = ""
        src = ""
        for child in point:
            if _local(child.tag) == "navLabel":
                label = "".join(child.itertext()).strip()
            elif _local(child.tag) == "content":
                src = child.get("src", "")
        target = resolve_href(ncx_path, src)
        if target and target not in titles:
            titles[target] = label[:300]
    return titles


def convert(path: Path) -> Document:
    archive = open_zip(path)
    with archive:
        try:
            container = _xml(read_member(archive, "META-INF/container.xml", 1024 * 1024))
        except KeyError as err:
            raise FormatError("EPUB has no META-INF/container.xml") from err
        rootfile = next((el.get("full-path", "") for el in container.iter() if _local(el.tag) == "rootfile"), "")
        if not rootfile:
            raise FormatError("EPUB container lists no package document")
        if any(n.startswith("META-INF/encryption.xml") for n in archive.namelist()):
            enc = read_member(archive, "META-INF/encryption.xml", 1024 * 1024)
            if b"EncryptedData" in enc and b"http://ns.adobe.com/pdf/enc#RC" not in enc and b"fonts" not in enc.lower():
                raise FormatError("DRM-protected EPUB")
        try:
            opf = _xml(read_member(archive, rootfile, 16 * 1024 * 1024))
        except KeyError as err:
            raise FormatError("EPUB package document is missing") from err
        document = Document(source_format="epub")
        metadata = next((el for el in opf if _local(el.tag) == "metadata"), None)
        cover_id = ""
        if metadata is not None:
            for el in metadata:
                name = _local(el.tag)
                text = "".join(el.itertext()).strip()
                if name == "title" and not document.title:
                    document.title = text[:500]
                elif name == "creator" and text:
                    document.authors.append(text[:200])
                elif name == "language" and not document.language:
                    document.language = text[:20]
                elif name == "description" and not document.description:
                    document.description = strip_tags(text)[:20000]
                elif name == "meta" and el.get("name") == "cover":
                    cover_id = el.get("content", "")
        manifest: dict[str, dict[str, str]] = {}
        manifest_el = next((el for el in opf if _local(el.tag) == "manifest"), None)
        if manifest_el is not None:
            for item in manifest_el:
                if _local(item.tag) != "item":
                    continue
                href = resolve_href(rootfile, item.get("href", ""))
                if href:
                    manifest[item.get("id", "")] = {"href": href, "type": item.get("media-type", ""), "props": item.get("properties", "")}
        spine_el = next((el for el in opf if _local(el.tag) == "spine"), None)
        spine = [manifest[ref.get("idref", "")] for ref in (spine_el if spine_el is not None else []) if ref.get("idref", "") in manifest and ref.get("linear", "yes") != "no"]
        if not spine:
            raise FormatError("EPUB spine is empty")
        titles: dict[str, str] = {}
        nav = next((m["href"] for m in manifest.values() if "nav" in m["props"].split()), "")
        if nav:
            titles = _nav_titles(archive, nav)
        ncx_id = spine_el.get("toc", "") if spine_el is not None else ""
        ncx = manifest.get(ncx_id, {}).get("href") or next((m["href"] for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), "")
        if ncx:
            for k, v in _ncx_titles(archive, ncx).items():
                titles.setdefault(k, v)
        cover_href = manifest.get(cover_id, {}).get("href") or next((m["href"] for m in manifest.values() if "cover-image" in m["props"].split()), "")
        names = set(archive.namelist())
        registered: dict[str, str] = {}

        def resource(member: str) -> str:
            if member in registered:
                return registered[member]
            if member not in names:
                return ""
            data = read_member(archive, member, 60 * 1024 * 1024)
            media = sniff_image_type(data)
            if not media or media == "image/svg+xml":
                return ""
            registered[member] = document.add_resource(data, media, posixpath.splitext(posixpath.basename(member))[0])
            return registered[member]

        spine_hrefs = {item["href"] for item in spine}
        bodies: list[dict] = []
        for item in spine:
            href = item["href"]
            if href not in names:
                continue
            title = titles.get(href, "")
            if item["type"].startswith("image/"):
                name = resource(href)
                if name:
                    bodies.append({"href": href, "title": title, "html": f'<div class="page"><img src="{name}" alt=""/></div>', "text": "", "remote": [], "images": 1})
                continue
            raw = read_member(archive, href, 32 * 1024 * 1024).decode("utf-8", "replace")
            body = extract_body(raw)
            # SVG wrapper pages (<svg><image xlink:href=...>) are common for covers / comic pages
            for src in re.findall(r'<image\b[^>]*(?:xlink:)?href="([^"]+)"', body, re.IGNORECASE):
                body = body + f'<img src="{src}"/>'
            title = title or html_title(raw)
            # a generated table of contents page (mostly links to other spine documents) is not a chapter
            links = [resolve_href(href, h) for h in re.findall(r'<a\b[^>]*href="([^"#][^"]*)"', body, re.IGNORECASE)]
            if sum(1 for target in links if target in spine_hrefs) >= 3 and len(strip_tags(re.sub(r"<a\b.*?</a>", "", body, flags=re.IGNORECASE | re.DOTALL))) < 200:
                continue
            remote = [html.unescape(u) for u in re.findall(r'<img\b[^>]*\bsrc="(https?://[^"]+)"', body, re.IGNORECASE)]
            clean = sanitize_fragment(body, resolve_src=lambda src, base=href: resource(resolve_href(base, src)),
                                      resolve_link=lambda link: link if re.match(r"^https?://", link, re.IGNORECASE) else "")
            # the title is shown by the reader / exports: drop a leading heading that repeats it
            lead = re.match(r"\s*<h([1-3])\b[^>]*>(.*?)</h\1>", clean, re.DOTALL)
            if lead and title and strip_tags(lead.group(2)).strip().lower() == title.strip().lower():
                clean = clean[lead.end():]
            elif lead and not title:
                title = strip_tags(lead.group(2))[:300]
                clean = clean[lead.end():]
            bodies.append({"href": href, "title": title, "html": clean, "text": strip_tags(clean), "remote": remote, "images": clean.count("<img")})
        # an "Introduction" page (vBook / Lynxx exports) becomes the description, not a chapter
        for entry in list(bodies[:3]):
            base_name = posixpath.basename(entry["href"]).lower()
            if base_name.startswith("introduction.") or entry["title"].strip().lower() in {"introduction", "giới thiệu", "gioi thieu"}:
                if not document.description:
                    document.description = "\n".join(p for p in (strip_tags(x) for x in re.split(r"</p>|<br\s*/?>", entry["html"])) if p)[:20000]
                bodies.remove(entry)
        if cover_href:
            name = resource(cover_href)
            if name:
                document.cover = next(r for r in document.resources if r.name == name)
        content = [b for b in bodies if b["html"].strip() or b["remote"]]
        # a leading cover-only page is the cover already
        if content and document.cover is not None and not content[0]["text"] and content[0]["images"] == 1 and not content[0]["remote"] and len(content) > 1:
            content = content[1:]
        image_only = bool(content) and all(not b["text"] and (b["images"] or b["remote"]) for b in content)
        for b in content:
            chapter = Chapter(b["title"] or f"Phần {len(document.chapters) + 1}", b["html"])
            if image_only and b["remote"] and not b["images"]:
                chapter.remote_images = b["remote"][:3000]
            document.chapters.append(chapter)
        if image_only:
            document.kind = "images"
            if len(document.chapters) > 50 and all(not c.remote_images for c in document.chapters) and all(not b["title"] for b in content):
                document.chapters = [Chapter(document.title or "Trang", "".join(c.html for c in document.chapters))]
        if document.cover is None and document.resources:
            document.cover = document.resources[0]
        return document
