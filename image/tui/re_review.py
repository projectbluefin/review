"""Read-only exact-head re-review delta contract.

This module classifies evidence for a human's next review.  It does not make
or preserve a review decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Any

from review_evidence_manifest import ReviewEvidenceManifest


_SHA = re.compile(r"^[0-9a-f]{40}$")


class FindingDisposition(str, Enum):
    CHANGED_REGION = "changed-region"
    UNCHANGED_REGION = "unchanged-region"
    STALE_REEVALUATE = "stale-re-evaluate"
    INVALIDATED_UNMAPPABLE = "invalidated-unmappable"


class FallbackReason(str, Enum):
    MAPPING_UNCERTAIN = "mapping-uncertain"
    MERGE_BASE_CHANGED = "merge-base-changed"
    SENSITIVE_SURFACE = "sensitive-surface-changed"
    PRIOR_REVIEW_INCOMPLETE = "prior-review-incomplete"
    RISK_BOUND_EXCEEDED = "bounded-risk-exceeded"
    CAPABILITY_ABSENT = "capability-absent"


@dataclass(frozen=True)
class Region:
    path: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not self.path or self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("review regions must have a path and ordered positive lines")

    def overlaps(self, other: "Region") -> bool:
        return self.path == other.path and self.start_line <= other.end_line and other.start_line <= self.end_line


@dataclass(frozen=True)
class FindingEvidence:
    path: str
    start_line: int
    end_line: int
    stale: bool = False

    def region(self) -> Region:
        return Region(self.path, self.start_line, self.end_line)


@dataclass(frozen=True)
class PriorFinding:
    finding_id: str
    evidence: FindingEvidence | None

    def __post_init__(self) -> None:
        if not self.finding_id:
            raise ValueError("prior finding id is required")


@dataclass(frozen=True)
class H1Evidence:
    finding_id: str
    path: str
    line: int

    def __post_init__(self) -> None:
        if not self.finding_id:
            raise ValueError("new evidence finding id is required")
        Region(self.path, self.line, self.line)


@dataclass(frozen=True)
class DeltaInput:
    reviewed_head_sha: str
    current_head_sha: str
    reviewed_merge_base_sha: str
    current_merge_base_sha: str
    current_h1_manifest: ReviewEvidenceManifest
    changed_regions: tuple[Region, ...] = ()
    prior_findings: tuple[PriorFinding, ...] = ()
    evidence: tuple[FindingEvidence, ...] = ()
    newly_supported: tuple[H1Evidence, ...] = ()
    mapping_uncertain: bool = False
    sensitive_surfaces_changed: bool = False
    prior_review_complete: bool = True
    bounded_risk_exceeded: bool = False
    capability_available: bool = True
    # Accepted only as input context; it is deliberately never copied out.
    prior_authority: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name in ("reviewed_head_sha", "current_head_sha", "reviewed_merge_base_sha", "current_merge_base_sha"):
            if not _SHA.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a full lowercase SHA-1 value")
        request = self.current_h1_manifest.request
        if request.head_sha != self.current_head_sha:
            raise ValueError("current H1 manifest head does not match current_head_sha")
        if request.base_sha != self.current_merge_base_sha:
            raise ValueError("current H1 manifest base does not match current_merge_base_sha")


@dataclass(frozen=True)
class ClassifiedFinding:
    finding_id: str
    disposition: FindingDisposition


@dataclass(frozen=True)
class ReReviewResult:
    reviewed_head_sha: str
    current_head_sha: str
    current_h1_manifest: ReviewEvidenceManifest
    findings: tuple[ClassifiedFinding, ...] = ()
    newly_supported: tuple[H1Evidence, ...] = ()
    fallback_reasons: tuple[FallbackReason, ...] = ()
    authority: None = field(default=None, init=False, repr=False)

    @property
    def full_review_required(self) -> bool:
        return bool(self.fallback_reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed_head_sha": self.reviewed_head_sha,
            "current_head_sha": self.current_head_sha,
            "findings": [
                {"finding_id": item.finding_id, "disposition": item.disposition.value}
                for item in self.findings
            ],
            "newly_supported": [
                {"finding_id": item.finding_id, "path": item.path, "line": item.line}
                for item in self.newly_supported
            ],
            "fallback_reasons": [reason.value for reason in self.fallback_reasons],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _evidence_for(finding: PriorFinding, evidence: tuple[FindingEvidence, ...]) -> FindingEvidence | None:
    if finding.evidence is None:
        return None
    target = finding.evidence.region()
    return next((item for item in evidence if item.region() == target), None)


def classify_head_delta(value: DeltaInput) -> ReReviewResult:
    reasons: list[FallbackReason] = []
    if value.mapping_uncertain:
        reasons.append(FallbackReason.MAPPING_UNCERTAIN)
    if value.reviewed_merge_base_sha != value.current_merge_base_sha:
        reasons.append(FallbackReason.MERGE_BASE_CHANGED)
    if value.sensitive_surfaces_changed:
        reasons.append(FallbackReason.SENSITIVE_SURFACE)
    if not value.prior_review_complete:
        reasons.append(FallbackReason.PRIOR_REVIEW_INCOMPLETE)
    if value.bounded_risk_exceeded:
        reasons.append(FallbackReason.RISK_BOUND_EXCEEDED)
    if not value.capability_available:
        reasons.append(FallbackReason.CAPABILITY_ABSENT)

    classified: list[ClassifiedFinding] = []
    for finding in value.prior_findings:
        evidence = _evidence_for(finding, value.evidence)
        if evidence is None:
            disposition = FindingDisposition.INVALIDATED_UNMAPPABLE
        elif evidence.stale:
            disposition = FindingDisposition.STALE_REEVALUATE
        elif any(evidence.region().overlaps(changed) for changed in value.changed_regions):
            disposition = FindingDisposition.CHANGED_REGION
        else:
            disposition = FindingDisposition.UNCHANGED_REGION
        classified.append(ClassifiedFinding(finding.finding_id, disposition))

    return ReReviewResult(
        value.reviewed_head_sha,
        value.current_head_sha,
        value.current_h1_manifest,
        tuple(classified),
        value.newly_supported,
        tuple(reasons),
    )


__all__ = [
    "ClassifiedFinding",
    "DeltaInput",
    "FallbackReason",
    "FindingDisposition",
    "FindingEvidence",
    "H1Evidence",
    "PriorFinding",
    "Region",
    "ReReviewResult",
    "classify_head_delta",
]
