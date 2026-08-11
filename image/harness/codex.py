"""Codex CLI adapter contract; it never substitutes another harness."""

from dataclasses import dataclass

from .registry import Availability, Binding, HarnessCapabilities


@dataclass
class CodexHarness:
    name: str = "codex"
    model: str = "gpt-5.6-luna"
    effort: str = "max"
    availability: Availability = Availability.UNAVAILABLE_BINARY
    capabilities: HarnessCapabilities = HarnessCapabilities(
        binary_readiness=True, auth_preflight=True, invocation=True,
        exact_binding=True, model_effort=True, steering=True,
        streaming=True, cancellation=True, result_conversion=True,
        provenance=True,
    )

    def invoke(self, binding: Binding, *, prompt: str, model: str | None = None,
               effort: str | None = None, steer: str | None = None) -> None:
        if self.availability is not Availability.READY:
            raise RuntimeError(f"codex unavailable: {self.availability.value}")
        raise NotImplementedError("Codex CLI process integration follows this seam")
