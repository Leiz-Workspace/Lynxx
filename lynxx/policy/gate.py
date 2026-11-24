"""Fail-closed source policy decisions for network operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from urllib.parse import urlparse


class PolicyState(StrEnum):
    DISCOVERED = "discovered"
    PERMITTED = "permitted"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"
    EXPIRED_REVIEW = "expired_review"


class Operation(StrEnum):
    SEARCH = "search"
    METADATA = "metadata"
    PAGE = "page"
    DOWNLOAD = "download"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    source_id: str
    hosts: tuple[str, ...]
    state: PolicyState = PolicyState.DISCOVERED
    allowed_operations: frozenset[Operation] = frozenset()
    policy_version: str = "unreviewed"
    reason: str = "Chưa có đánh giá chính sách cho nguồn này"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    decision: str
    reason: str
    policy_version: str


class PolicyGate:
    """Central gate; callers must check it before every source request."""

    def __init__(self, policies: tuple[SourcePolicy, ...] = ()) -> None:
        self._policies = {policy.source_id: policy for policy in policies}

    def register(self, policy: SourcePolicy) -> None:
        self._policies[policy.source_id] = policy

    def policy_for_url(self, url: str) -> SourcePolicy | None:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        for policy in self._policies.values():
            if any(host == pattern or fnmatch(host, pattern) for pattern in policy.hosts):
                return policy
        return None

    def check(self, source_id: str, url: str, operation: Operation) -> PolicyDecision:
        policy = self._policies.get(source_id)
        if policy is None:
            return PolicyDecision(False, "blocked", "Nguồn chưa được đăng ký", "none")
        if self.policy_for_url(url) is not policy:
            return PolicyDecision(False, "blocked", "URL không thuộc host đã khai báo của nguồn", policy.policy_version)
        if policy.state in {PolicyState.BLOCKED, PolicyState.DISCOVERED, PolicyState.EXPIRED_REVIEW}:
            return PolicyDecision(False, "blocked", policy.reason, policy.policy_version)
        if operation not in policy.allowed_operations:
            return PolicyDecision(False, "restricted", f"Nguồn không cho phép thao tác {operation.value}", policy.policy_version)
        return PolicyDecision(True, "allowed", "Được policy của nguồn cho phép", policy.policy_version)

    def require(self, source_id: str, url: str, operation: Operation) -> None:
        decision = self.check(source_id, url, operation)
        if not decision.allowed:
            raise PermissionError(f"{decision.decision}: {decision.reason}")
