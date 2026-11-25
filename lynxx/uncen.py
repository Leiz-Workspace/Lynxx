"""Non-destructive, local-only contract for future scan restoration workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class UncenState(StrEnum):
    DISABLED = "disabled"
    NEEDS_CONSENT = "needs_consent"
    READY = "ready"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UncenRequest:
    input_path: Path
    output_path: Path
    rights_confirmed: bool = False
    model_id: str = ""

    def validate(self) -> None:
        source = self.input_path.expanduser().resolve(strict=True)
        output = self.output_path.expanduser().resolve()
        if not source.is_file():
            raise ValueError("uncen input must be a regular file")
        if source == output:
            raise ValueError("uncen output must not overwrite the original")
        if not self.rights_confirmed:
            raise PermissionError("rights confirmation is required before local processing")
        if not self.model_id.strip():
            raise ValueError("model_id is required for provenance")


def validate_local_request(request: UncenRequest) -> dict[str, str]:
    """Validate a request without running inference or contacting a network."""
    request.validate()
    return {
        "input": str(request.input_path.expanduser().resolve()),
        "output": str(request.output_path.expanduser().resolve()),
        "model": request.model_id,
        "network": "disabled",
        "original": "preserved",
    }
