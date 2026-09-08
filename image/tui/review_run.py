"""Backend-neutral review run state machine.

A review run is the logical identity of one review, independent of how many
processes or agent turns it takes. Runs are one-shot: a run starts, then
reaches exactly one terminal state.

States and their transitions::

    PENDING ──start()───▶ RUNNING
    RUNNING ──complete()▶ COMPLETE     (terminal, result ready)
    RUNNING ──fail()────▶ FAILED       (terminal, error)
    RUNNING ──cancel()──▶ CANCELLED    (terminal, killed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256

try:
    from harness.registry import Harness, HarnessRegistry
    from tui.review_result import ReviewResult
    from tui.review_evidence_manifest import ReviewRequest
except ImportError:
    from image.harness.registry import Harness, HarnessRegistry  # type: ignore[no-redef]
    from image.tui.review_result import ReviewResult  # type: ignore[no-redef]
    from image.tui.review_evidence_manifest import ReviewRequest  # type: ignore[no-redef]


class ReviewRunState(str, Enum):
    """All possible states of a review run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ReviewRunState.COMPLETE,
            ReviewRunState.FAILED,
            ReviewRunState.CANCELLED,
        }


# Valid transitions per source state. A missing key means terminal.
_TRANSITIONS: dict[ReviewRunState, set[ReviewRunState]] = {
    ReviewRunState.PENDING: {ReviewRunState.RUNNING},
    ReviewRunState.RUNNING: {
        ReviewRunState.COMPLETE,
        ReviewRunState.FAILED,
        ReviewRunState.CANCELLED,
    },
}


@dataclass(frozen=True)
class ReviewRun:
    """Immutable identity for one logical review run.

    The identity is bound to the exact repository, pull request, and heads at
    the time the review was requested. A head change invalidates the run; the
    caller creates a new ``ReviewRun``.
    """

    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    evidence_id: str  # derived from the reviewed base and head
    backend: str      # harness name (e.g. "goose", "codex")
    model: str
    effort: str

    @classmethod
    def from_request(
        cls,
        request: ReviewRequest,
        *,
        backend: str = "goose",
        model: str = "gemini-3.8-flash",
        effort: str = "max",
    ) -> ReviewRun:
        return cls(
            repository=f"{request.owner}/{request.repository}",
            pull_request=request.pull_request_number,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            evidence_id=request.base_sha[:12] + request.head_sha[:12],
            backend=backend,
            model=model,
            effort=effort,
        )

    @property
    def identity(self) -> str:
        payload = {
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "evidence_id": self.evidence_id,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ReviewRunError(Exception):
    """Raised when the run state machine rejects a transition."""


@dataclass(frozen=True)
class StepResult:
    """Result of one state machine transition.

    A terminal step carries the final ``ReviewResult``.
    """

    state: ReviewRunState
    output_delta: tuple[str, ...] = ()
    yield_reason: str = ""
    terminal_result: ReviewResult | None = None
    next_action: str = "wait"


@dataclass
class ReviewRunController:
    """Controls the lifecycle of one review run.

    The controller owns the harness adapter and the run's current state. It
    does not own the process — the harness does — and it never blocks: step
    methods return a ``StepResult`` immediately, with the harness's execution
    happening off the UI thread through the harness's streaming protocol.
    """

    run: ReviewRun
    harness: Harness
    registry: HarnessRegistry
    state: ReviewRunState = ReviewRunState.PENDING
    _terminal_result: ReviewResult | None = field(default=None, repr=False)

    def terminal_result(self) -> ReviewResult | None:
        return self._terminal_result

    def _transition(self, target: ReviewRunState) -> None:
        allowed = _TRANSITIONS.get(self.state)
        if allowed is None or target not in allowed:
            raise ReviewRunError(
                f"cannot transition from {self.state.value} to {target.value}"
            )
        self.state = target

    def start(self, steer: str = "") -> StepResult:  # noqa: ARG002
        """Begin execution. The harness runs the review."""
        self._transition(ReviewRunState.RUNNING)
        return StepResult(state=self.state, next_action="wait")

    def complete(self, result: ReviewResult) -> StepResult:
        """The review finished with a terminal result."""
        self._transition(ReviewRunState.COMPLETE)
        self._terminal_result = result
        return StepResult(
            state=ReviewRunState.COMPLETE,
            terminal_result=result,
            next_action="done",
        )

    def fail(self, error: str = "") -> StepResult:
        """The review failed."""
        self._transition(ReviewRunState.FAILED)
        return StepResult(
            state=ReviewRunState.FAILED,
            yield_reason=error,
            next_action="done",
        )

    def cancel(self) -> StepResult:
        """Cancel the run. The harness must have already been signalled."""
        allowed_targets = _TRANSITIONS.get(self.state)
        if allowed_targets is None or ReviewRunState.CANCELLED not in allowed_targets:
            raise ReviewRunError(
                f"cannot cancel from terminal state {self.state.value}"
            )
        self._transition(ReviewRunState.CANCELLED)
        return StepResult(
            state=ReviewRunState.CANCELLED,
            next_action="done",
        )


__all__ = [
    "ReviewRun",
    "ReviewRunController",
    "ReviewRunError",
    "ReviewRunState",
    "StepResult",
]
