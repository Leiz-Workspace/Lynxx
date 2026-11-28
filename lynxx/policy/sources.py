"""Built-in source registry for Lynxx's policy-gated library adapters.

These entries intentionally do not enable network downloading. A source must be
reviewed and explicitly promoted to PERMITTED for each operation first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .gate import Operation, PolicyDecision, PolicyGate, PolicyState, SourcePolicy


class SourceCapability(StrEnum):
    SEARCH = "search"
    METADATA = "metadata"
    PAGES = "pages"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    name: str
    homepage: str
    policy: SourcePolicy
    capabilities: frozenset[SourceCapability]


SOURCES = (
    SourceRecord(
        "nhentai",
        "nhentai.net",
        "https://nhentai.net/",
        SourcePolicy(
            "nhentai", ("nhentai.net", "*.nhentai.net"), PolicyState.EXPIRED_REVIEW,
            reason="ToS, robots và quyền lưu/phân phối cần được review trước khi bật adapter",
        ),
        frozenset({SourceCapability.SEARCH, SourceCapability.METADATA, SourceCapability.PAGES, SourceCapability.EXPORT}),
    ),
    SourceRecord(
        "cuutruyen",
        "cuutruyen.net",
        "https://cuutruyen.net/",
        SourcePolicy(
            "cuutruyen", ("cuutruyen.net", "*.cuutruyen.net"), PolicyState.BLOCKED,
            reason="Adapter Python không dùng: Cứu Truyện chạy qua extension đi kèm app (app/extensions/cuutruyen)",
        ),
        frozenset(),
    ),
)


def default_gate() -> PolicyGate:
    return PolicyGate(tuple(source.policy for source in SOURCES))


def source_status(source_id: str, url: str, operation: Operation) -> PolicyDecision:
    return default_gate().check(source_id, url, operation)


def list_sources() -> list[dict[str, object]]:
    return [
        {
            "id": source.source_id,
            "name": source.name,
            "homepage": source.homepage,
            "state": source.policy.state.value,
            "capabilities": sorted(capability.value for capability in source.capabilities),
            "reason": source.policy.reason,
        }
        for source in SOURCES
    ]
