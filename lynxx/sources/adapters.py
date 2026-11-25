"""Collection-centric source adapter contracts and policy-gated starters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from ..policy import Operation, source_status


@dataclass(frozen=True, slots=True)
class CollectionRef:
    source_id: str
    remote_id: str
    url: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class PageRef:
    remote_id: str
    number: int
    url: str
    mime_type: str = "image/jpeg"


class SourceAdapter(Protocol):
    source_id: str

    def search(self, query: str) -> list[CollectionRef]: ...

    def resolve_collection(self, url_or_id: str) -> CollectionRef: ...

    def list_pages(self, collection: CollectionRef) -> list[PageRef]: ...


class PolicyGatedAdapter:
    """Base for adapters: no method may perform a request before policy allows it."""

    source_id = ""

    def require(self, url: str, operation: Operation) -> None:
        decision = source_status(self.source_id, url, operation)
        if not decision.allowed:
            raise PermissionError(f"{self.source_id}: {decision.reason}")

    @staticmethod
    def host(url: str) -> str:
        return (urlparse(url).hostname or "").lower().rstrip(".")


class NhentaiAdapter(PolicyGatedAdapter):
    """API-v2 adapter placeholder; remains probe-only until policy review succeeds."""

    source_id = "nhentai"

    def search(self, query: str) -> list[CollectionRef]:
        raise PermissionError("nhentai is policy-gated; no network request is enabled")

    def resolve_collection(self, url_or_id: str) -> CollectionRef:
        raise PermissionError("nhentai is policy-gated; API and ToS review is required")

    def list_pages(self, collection: CollectionRef) -> list[PageRef]:
        self.require(collection.url, Operation.PAGE)
        raise PermissionError("nhentai page retrieval is not enabled before policy review")


class CuutruyenAdapter(PolicyGatedAdapter):
    """Explicitly blocked: this adapter never decodes DRM or calls image hosts."""

    source_id = "cuutruyen"

    def search(self, query: str) -> list[CollectionRef]:
        raise PermissionError("cuutruyen is blocked: DRM/robots policy prevents retrieval")

    def resolve_collection(self, url_or_id: str) -> CollectionRef:
        raise PermissionError("cuutruyen is blocked: DRM/robots policy prevents retrieval")

    def list_pages(self, collection: CollectionRef) -> list[PageRef]:
        raise PermissionError("cuutruyen is blocked: no DRM or scramble bypass")
