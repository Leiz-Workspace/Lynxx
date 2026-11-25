"""Offline nhentai API-v2 payload normalization.

This module intentionally performs no network I/O. It accepts only URLs that a
fixture or an explicitly reviewed API response supplied; it never guesses CDN
paths or bypasses a source policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

from .adapters import CollectionRef, PageRef

_ALLOWED_PAGE_HOSTS = {"nhentai.net", "i1.nhentai.net", "i2.nhentai.net", "i3.nhentai.net", "i4.nhentai.net"}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"gallery field {field!r} must be a non-empty string")
    return value.strip()


def _gallery_id(value: object) -> str:
    if isinstance(value, int):
        result = str(value)
    else:
        result = _text(value, "id")
    if not result.isdigit() or len(result) > 32:
        raise ValueError("gallery id must be a decimal identifier")
    return result


def _page_url(value: object) -> str:
    url = _text(value, "url")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in _ALLOWED_PAGE_HOSTS or not parsed.path:
        raise ValueError("page URL is not an approved HTTPS nhentai host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("page URL must not contain credentials, query or fragment")
    return url


def parse_gallery_payload(payload: Mapping[str, object], api_url: str = "https://nhentai.net/api/v2/galleries") -> tuple[CollectionRef, list[PageRef]]:
    """Normalize a captured API response into a collection and ordered pages.

    The response must include explicit page URLs in each page object. This
    prevents the adapter from inventing CDN paths when a site changes format.
    """
    gallery_id = _gallery_id(payload.get("id"))
    title_data = payload.get("title")
    title = ""
    if isinstance(title_data, Mapping):
        title = _text(title_data.get("english") or title_data.get("pretty") or title_data.get("方"), "title")
    if not title:
        title = f"Gallery {gallery_id}"
    media_id = _text(payload.get("media_id"), "media_id")
    pages_data = payload.get("pages")
    if not isinstance(pages_data, list) or not pages_data:
        raise ValueError("gallery pages must be a non-empty array")
    pages: list[PageRef] = []
    for index, raw in enumerate(pages_data, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"page {index} must be an object")
        number = raw.get("number", index)
        if not isinstance(number, int) or number < 1:
            raise ValueError(f"page {index} has an invalid number")
        if number != index:
            raise ValueError("gallery pages must be ordered consecutively")
        url = _page_url(raw.get("url"))
        mime = _text(raw.get("mime_type", "image/jpeg"), "mime_type")
        pages.append(PageRef(f"{media_id}:{number}", number, url, mime))
    return CollectionRef("nhentai", gallery_id, f"https://nhentai.net/g/{gallery_id}/", title), pages
