"""Goose's existing review surface represented by the shared contract."""

from dataclasses import dataclass

from .registry import Availability, Binding, HarnessBranding, HarnessCapabilities


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
        provenance=True,
    )

    def invoke(self, binding: Binding, *, prompt: str, model: str,
               effort: str, steer: str | None = None) -> None:
        """The launcher remains the owner of Goose invocation behavior."""
        raise NotImplementedError("use the existing bluefin-review Goose launcher")
