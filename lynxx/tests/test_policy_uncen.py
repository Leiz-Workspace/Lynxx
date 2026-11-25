"""Policy and local-only uncen contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lynxx.policy import Operation, PolicyState, list_sources, source_status
from lynxx.uncen import UncenRequest, validate_local_request


class SourcePolicyTests(unittest.TestCase):
    def test_initial_sources_fail_closed(self) -> None:
        statuses = {item["id"]: item["state"] for item in list_sources()}
        self.assertEqual(statuses, {"nhentai": "expired_review", "cuutruyen": "blocked"})
        self.assertFalse(source_status("nhentai", "https://nhentai.net/api/v2/galleries/1", Operation.METADATA).allowed)
        self.assertFalse(source_status("cuutruyen", "https://cuutruyen.net/", Operation.PAGE).allowed)
        self.assertEqual(PolicyState.BLOCKED.value, "blocked")


class UncenContractTests(unittest.TestCase):
    def test_requires_consent_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.png"
            source.write_bytes(b"fixture")
            request = UncenRequest(source, root / "derived.png", rights_confirmed=True, model_id="local-test-v1")
            result = validate_local_request(request)
            self.assertEqual(result["network"], "disabled")
            self.assertEqual(result["original"], "preserved")
            with self.assertRaises(PermissionError):
                validate_local_request(UncenRequest(source, root / "other.png", model_id="local-test-v1"))
            with self.assertRaises(ValueError):
                validate_local_request(UncenRequest(source, source, rights_confirmed=True, model_id="local-test-v1"))


if __name__ == "__main__":
    unittest.main()
