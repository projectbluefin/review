"""Shared capability contract for review harnesses.

Adapters describe what they can do; orchestration selects no fallback when a
requested harness is unavailable.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence
from tui.review_evidence_manifest import ReviewRequest
from tui.review_result import ReviewResult


MAX_DRAFT_EVIDENCE_CHARS = 120_000


class Availability(str, Enum):
    READY = "READY"
    UNAVAILABLE_BINARY = "UNAVAILABLE_BINARY"
    UNAVAILABLE_AUTH = "UNAVAILABLE_AUTH"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    FAILED_CONFORMANCE = "FAILED_CONFORMANCE"


class DraftState(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class DraftRequest:
    binding: ReviewRequest
    verdict: str
    evidence: ReviewResult
    live_facts: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.verdict not in {"approve", "request-changes", "comment"}:
            raise ValueError("unsupported review verdict")
        if self.evidence.state not in {"complete", "findings"}:
            raise ValueError("drafting requires trusted review evidence")
        if self.verdict == "approve" and not self.evidence.is_clean:
            raise ValueError("approve requires clean review evidence")
        provenance = self.evidence.provenance
        expected = {
            "repository": f"{self.binding.owner}/{self.binding.repository}",
            "pull_request": self.binding.pull_request_number,
            "base_sha": self.binding.base_sha,
            "head_sha": self.binding.head_sha,
        }
        if any(provenance.get(key) != value for key, value in expected.items()):
            raise ValueError("review evidence does not match exact binding")
        if not isinstance(self.live_facts, Mapping) or len(self.live_facts) > 32:
            raise ValueError("live facts are unbounded or invalid")
        if any(not isinstance(key, str) or len(key) > 128 for key in self.live_facts):
            raise ValueError("live fact key is invalid")
        try:
            evidence = json.dumps(
                {"result": self.evidence.to_dict(), "live": self.live_facts},
                sort_keys=True, separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("draft evidence is not serializable") from exc
        if len(evidence) > MAX_DRAFT_EVIDENCE_CHARS:
            raise ValueError("draft evidence exceeds bound")


@dataclass(frozen=True)
class DraftResult:
    state: DraftState
    markdown: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)
    raw_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.markdown) > 4096 or len(self.raw_evidence) > 400:
            raise ValueError("draft result exceeds bound")


@dataclass(frozen=True)
class HarnessCapabilities:
    binary_readiness: bool = False
    auth_preflight: bool = False
    invocation: bool = False
    exact_binding: bool = False
    model_effort: bool = False
    steering: bool = False
    streaming: bool = False
    cancellation: bool = False
    result_conversion: bool = False
    provenance: bool = False
    body_drafting: bool = False
    # Re-entry capabilities. These are opt-in: a harness that does not
    # advertise them remains one-shot (#255).
    resumable: bool = False
    checkpoint: bool = False
    yieldable: bool = False


@dataclass(frozen=True)
class HarnessBranding:
    harness_id: str
    display_name: str
    terminal_badge: str
    accessible_label: str
    attribution: str
    asset_ref: str | None = None

    def __post_init__(self) -> None:
        if len(self.terminal_badge) != 2:
            raise ValueError("terminal badge must contain exactly two characters")
        if not self.accessible_label.strip() or self.accessible_label == self.terminal_badge:
            raise ValueError("accessible label must identify the full product")


class Harness(Protocol):
    name: str
    availability: Availability
    capabilities: HarnessCapabilities

    def invoke(self, binding: ReviewRequest, *, prompt: str, model: str, effort: str,
               steer: str | None = None) -> Any: ...

    def draft(self, request: DraftRequest) -> DraftResult: ...


@dataclass
class HarnessRegistry:
    _harnesses: dict[str, Harness] = field(default_factory=dict)

    def register(self, harness: Harness) -> None:
        if harness.name in self._harnesses:
            raise ValueError(f"harness already registered: {harness.name}")
        self._harnesses[harness.name] = harness

    def get(self, name: str) -> Harness:
        try:
            return self._harnesses[name]
        except KeyError as exc:
            raise KeyError(f"harness is not registered: {name}") from exc

    def names(self) -> Sequence[str]:
        return tuple(self._harnesses)

    def require_ready(self, name: str) -> Harness:
        harness = self.get(name)
        if harness.availability is not Availability.READY:
            raise RuntimeError(f"{name} unavailable: {harness.availability.value}")
        return harness
