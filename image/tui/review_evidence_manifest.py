"""Bounded, read-only review input contracts shared by review harnesses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Any, Protocol


_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_SUMMARY = 4096
_MAX_TEXT = 4096
_MAX_HANDLE = 2048
_MAX_IDENTITY = 256
_MAX_GENERATED_AT = 128
_MAX_KIND = 256
_MAX_PROVENANCE = 256
_MAX_MANIFEST_ENTRIES = 128
_MAX_ENTRY_HANDLES = 32


def _require_text(value: str, limit: int, label: str) -> None:
    if not value or len(value) > limit:
        raise ValueError(f"{label} is empty or too long")


class TrustClass(str, Enum):
    VERIFIED = "verified"
    REPOSITORY = "repository"
    UNTRUSTED = "untrusted"


class Availability(str, Enum):
    AVAILABLE = "available"
    INVALID = "invalid"
    TRUNCATED = "truncated"
    STALE = "stale"
    OMITTED = "omitted"


class EvidencePhase(str, Enum):
    SNAPSHOT = "exact-head-snapshot"
    LIVE = "live-revalidate"


@dataclass(frozen=True)
class ReviewScope:
    actor: str
    tenant: str
    installation: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.actor, _MAX_IDENTITY, "actor")
        _require_text(self.tenant, _MAX_IDENTITY, "tenant")
        if self.installation is not None:
            _require_text(self.installation, _MAX_IDENTITY, "installation")


@dataclass(frozen=True)
class EvidenceHandle:
    """A bounded pointer to evidence; it never embeds the evidence itself."""

    uri: str
    label: str
    max_bytes: int = 4096

    def __post_init__(self) -> None:
        if not self.uri or len(self.uri) > _MAX_HANDLE:
            raise ValueError("evidence handle URI is empty or too long")
        if not self.label or len(self.label) > 256:
            raise ValueError("evidence handle label is empty or too long")
        if not 1 <= self.max_bytes <= 1024 * 1024:
            raise ValueError("evidence handle bound is invalid")


@dataclass(frozen=True)
class EvidenceEntry:
    kind: str
    provenance: str
    trust: TrustClass
    availability: Availability
    phase: EvidencePhase
    summary: str = ""
    handles: tuple[EvidenceHandle, ...] = ()
    untrusted_text: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "trust", TrustClass(self.trust))
            object.__setattr__(self, "availability", Availability(self.availability))
            object.__setattr__(self, "phase", EvidencePhase(self.phase))
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence enum value is unsupported") from exc
        _require_text(self.kind, _MAX_KIND, "evidence kind")
        _require_text(self.provenance, _MAX_PROVENANCE, "evidence provenance")
        if len(self.summary) > _MAX_SUMMARY:
            raise ValueError("evidence summary exceeds the bounded limit")
        if len(self.handles) > _MAX_ENTRY_HANDLES:
            raise ValueError("evidence handle count exceeds the bounded limit")
        if self.untrusted_text is not None and len(self.untrusted_text) > _MAX_TEXT:
            raise ValueError("untrusted text exceeds the bounded limit")
        if self.untrusted_text is not None and self.trust is not TrustClass.UNTRUSTED:
            raise ValueError("text supplied by a review object must be untrusted")


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

    @property
    def scope(self) -> ReviewScope:
        return ReviewScope(self.actor, self.tenant, self.installation)


class ManifestHarness(Protocol):
    def receive(self, manifest: "ReviewEvidenceManifest") -> None: ...


@dataclass(frozen=True)
class ReviewEvidenceManifest:
    request: ReviewRequest
    entries: tuple[EvidenceEntry, ...] = ()
    organization_policy: EvidenceEntry | None = None
    version: int = 1
    mutation_authority: None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported evidence manifest version")
        if len(self.entries) + (self.organization_policy is not None) > _MAX_MANIFEST_ENTRIES:
            raise ValueError("manifest entry count exceeds the bounded limit")
        if self.organization_policy is not None and self.organization_policy.kind != "organization-policy":
            raise ValueError("organization policy must use its declared evidence kind")

    def require_scope(self, scope: ReviewScope) -> None:
        if self.request.scope != scope:
            raise PermissionError("manifest scope does not match the requesting harness")

    def deliver_to(self, harness: ManifestHarness, scope: ReviewScope) -> None:
        self.require_scope(scope)
        harness.receive(self)

    def semantic_dict(self) -> dict[str, Any]:
        return _encode(self)

    def semantic_json(self) -> str:
        return json.dumps(self.semantic_dict(), sort_keys=True, separators=(",", ":"))


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int)) or value is None:
        return value
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, EvidenceEntry):
        omitted = {"untrusted_text", "mutation_authority"}
        if value.trust is TrustClass.UNTRUSTED:
            omitted.add("summary")
        return {
            name: _encode(getattr(value, name))
            for name in value.__dataclass_fields__
            if name not in omitted
        }
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _encode(getattr(value, name))
            for name in value.__dataclass_fields__
            if name not in {"mutation_authority", "untrusted_text"}
        }
    raise TypeError(f"unsupported manifest value: {type(value).__name__}")
