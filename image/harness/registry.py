"""Shared capability contract for review harnesses.

Adapters describe what they can do; orchestration selects no fallback when a
requested harness is unavailable.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence
from tui.review_evidence_manifest import ReviewRequest


class Availability(str, Enum):
    READY = "READY"
    UNAVAILABLE_BINARY = "UNAVAILABLE_BINARY"
    UNAVAILABLE_AUTH = "UNAVAILABLE_AUTH"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    FAILED_CONFORMANCE = "FAILED_CONFORMANCE"


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
