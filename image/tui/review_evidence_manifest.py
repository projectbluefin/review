"""Bounded, read-only review request contract shared by review harnesses."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_TEXT = 4096
_MAX_IDENTITY = 256
_MAX_GENERATED_AT = 128


def _require_text(value: str, limit: int, label: str) -> None:
    if not value or len(value) > limit:
        raise ValueError(f"{label} is empty or too long")


@dataclass(frozen=True)
class ReviewRequest:
    owner: str
    repository: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    actor: str
    tenant: str
    installation: str | None = None
    generated_at: str = ""
    focus: str = ""
    steering: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.owner, _MAX_IDENTITY, "owner")
        _require_text(self.repository, _MAX_IDENTITY, "repository")
        if self.pull_request_number < 1:
            raise ValueError("pull request number must be positive")
        if not _SHA.fullmatch(self.base_sha) or not _SHA.fullmatch(self.head_sha):
            raise ValueError("base_sha and head_sha must be full lowercase SHA-1 values")
        _require_text(self.actor, _MAX_IDENTITY, "actor")
        _require_text(self.tenant, _MAX_IDENTITY, "tenant")
        _require_text(self.generated_at, _MAX_GENERATED_AT, "generated_at")
        if self.installation is not None:
            _require_text(self.installation, _MAX_IDENTITY, "installation")
        if self.version != 1:
            raise ValueError("unsupported review request version")
        if len(self.focus) > _MAX_TEXT or len(self.steering) > _MAX_TEXT:
            raise ValueError("maintainer steering exceeds the bounded limit")
