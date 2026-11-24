"""Domain objects for Lynxx's collection/chapter/page library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class ContentKind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    EPUB = "epub"


class ArtifactKind(StrEnum):
    ORIGINAL = "original"
    PDF = "pdf"
    EPUB = "epub"
    DERIVED = "derived"


class DownloadState(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    remote_id: str
    url: str = ""


@dataclass(slots=True)
class Page:
    id: str
    number: int
    source_url: str = ""
    local_path: str = ""
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0
    sha256: str = ""
    byte_size: int = 0
    state: DownloadState = DownloadState.PENDING


@dataclass(slots=True)
class Chapter:
    id: str
    collection_id: str
    title: str
    source: SourceRef | None = None
    number: float | None = None
    pages: list[Page] = field(default_factory=list)
    content_kind: ContentKind = ContentKind.IMAGE
    state: DownloadState = DownloadState.PENDING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Collection:
    id: str
    title: str
    source: SourceRef | None = None
    language: str = ""
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Artifact:
    id: str
    chapter_id: str
    kind: ArtifactKind
    path: str
    mime_type: str
    size: int = 0
    sha256: str = ""
    state: DownloadState = DownloadState.PENDING
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ReaderPosition:
    chapter_id: str
    page: int = 0
    scroll_offset: float = 0.0
    mode: str = "vertical"
    zoom: float = 1.0
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.page < 0:
            raise ValueError("page must be non-negative")
        if self.scroll_offset < 0:
            raise ValueError("scroll_offset must be non-negative")
        if self.mode not in {"vertical", "horizontal"}:
            raise ValueError("mode must be vertical or horizontal")
        if self.zoom <= 0:
            raise ValueError("zoom must be positive")
