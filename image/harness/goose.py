"""Goose's existing review surface represented by the shared contract."""

from dataclasses import dataclass

from tui.review_evidence_manifest import ReviewRequest
from .registry import Availability, DraftRequest, DraftResult, DraftState, HarnessBranding, HarnessCapabilities


@dataclass
class GooseHarness:
    name: str = "goose"
    branding: HarnessBranding = HarnessBranding(
        "goose", "Goose", "GS", "Goose", "aaif-goose/goose", None
    )
    availability: Availability = Availability.READY
    capabilities: HarnessCapabilities = HarnessCapabilities(
        binary_readiness=True, auth_preflight=True, invocation=True,
        exact_binding=True, model_effort=True, steering=True,
        streaming=True, cancellation=True, result_conversion=True,
        provenance=True, body_drafting=False,
    )

    def invoke(self, binding: ReviewRequest, *, prompt: str, model: str,
               effort: str, steer: str | None = None) -> None:
        """The launcher remains the owner of Goose invocation behavior."""
        raise NotImplementedError("use the existing bluefin-review Goose launcher")

    def draft(self, request: DraftRequest) -> DraftResult:
        raise RuntimeError("goose unavailable: UNSUPPORTED_CAPABILITY")
