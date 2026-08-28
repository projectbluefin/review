"""Backend-neutral re-entrant review run state machine.

A review run is the logical identity of one review, independent of how many
processes or agent turns it takes. The state machine preserves one-shot
adapters: a harness that does not advertise re-entry support produces a
terminal result on its first step and never yields.

States and their transitions::

    PENDING ──start()──▶ RUNNING
    RUNNING ──advance()─▶ RUNNING      (step completed, more steps remain)
    RUNNING ──complete()▶ COMPLETE     (terminal, result ready)
    RUNNING ──fail()────▶ FAILED       (terminal, error)
    RUNNING ──cancel()──▶ CANCELLED    (terminal, killed)
    RUNNING ──yield()───▶ YIELDED      (harness paused, no checkpoint yet)
    YIELDED ─checkpoint─▶ RESUMABLE    (checkpoint persisted, resume possible)
    YIELDED ──cancel()──▶ CANCELLED
    YIELDED ──fail()────▶ FAILED
    YIELDED ──stale()───▶ STALE        (head changed per #185, never resume)
    RESUMABLE ─resume()─▶ RUNNING
    RESUMABLE ─cancel()─▶ CANCELLED
    RESUMABLE ─stale()──▶ STALE
    WAITING_EXTERNAL ─▶ RESUMABLE      (external event received)
    WAITING_EXTERNAL ─▶ FAILED         (external event timed out)
    STALE ────────────── terminal (no valid transition)
    COMPLETE ─────────── terminal (result emitted at most once)
    FAILED ───────────── terminal
    CANCELLED ────────── terminal

Checkpoints are opaque to Review. A harness either supports checkpoints and
advertises ``HarnessCapabilities.checkpoint``, or it stays one-shot and its
run always reaches a terminal state on the first step.

An external wait receives and persists its checkpoint before the event can
make the run resumable.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

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
    YIELDED = "yielded"
    WAITING_EXTERNAL = "waiting-external"
    RESUMABLE = "resumable"
    STALE = "stale"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ReviewRunState.COMPLETE,
            ReviewRunState.FAILED,
            ReviewRunState.CANCELLED,
            ReviewRunState.STALE,
        }


# Valid transitions per source state. A missing key means invalid.
_TRANSITIONS: dict[ReviewRunState, set[ReviewRunState]] = {
    ReviewRunState.PENDING: {ReviewRunState.RUNNING},
    ReviewRunState.RUNNING: {
        ReviewRunState.RUNNING,  # advance — another step remains
        ReviewRunState.COMPLETE,
        ReviewRunState.FAILED,
        ReviewRunState.CANCELLED,
        ReviewRunState.YIELDED,
        ReviewRunState.WAITING_EXTERNAL,
    },
    ReviewRunState.YIELDED: {
        ReviewRunState.RESUMABLE,
        ReviewRunState.CANCELLED,
        ReviewRunState.FAILED,
        ReviewRunState.STALE,
    },
    ReviewRunState.WAITING_EXTERNAL: {
        ReviewRunState.RESUMABLE,
        ReviewRunState.FAILED,
    },
    ReviewRunState.RESUMABLE: {
        ReviewRunState.RUNNING,
        ReviewRunState.CANCELLED,
        ReviewRunState.STALE,
    },
}


@dataclass(frozen=True)
class ReviewRun:
    """Immutable identity for one logical review run.

    The identity is bound to the exact repository, pull request, and heads at
    the time the review was requested. A head change invalidates the run
    (``STALE`` per #185); the caller creates a new ``ReviewRun``.
    """

    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    evidence_id: str  # ReviewEvidenceManifest identity
    backend: str      # harness name (e.g. "goose", "codex")
    model: str
    effort: str

    @classmethod
    def from_request(
        cls,
        request: ReviewRequest,
        *,
        backend: str = "goose",
        model: str = "gpt-5.6-luna",
        effort: str = "high",
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


@dataclass(frozen=True)
class RunCheckpoint:
    """Opaque checkpoint that a harness produces and Review never inspects.

    A harness that returns a checkpoint claims it can resume from it. Review
    stores the checkpoint by its identity key and hands it back on resume.
    """

    data: str

    def encode(self) -> str:
        return self.data

    @classmethod
    def decode(cls, value: str) -> RunCheckpoint:
        return cls(value)


class ReviewRunError(Exception):
    """Raised when the run state machine rejects a transition."""


class CheckpointStore:
    """Small durable store for opaque checkpoints, keyed by run identity."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        if root is None:
            state_root = os.environ.get("XDG_STATE_HOME", "~/.local/state")
            root = os.path.join(state_root, "bluefin-review", "runs")
        self.root = Path(root).expanduser()

    def put(self, identity: str, checkpoint: RunCheckpoint) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{identity}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(checkpoint.encode())
            temporary.replace(self.root / f"{identity}.checkpoint")
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, identity: str) -> RunCheckpoint | None:
        try:
            return RunCheckpoint.decode(
                (self.root / f"{identity}.checkpoint").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError):
            return None


_STALE_HELP = (
    "The pull request head has changed since this review was started. "
    "Start a new review instead of resuming (#185)."
)


class StepAction(str, Enum):
    """What the caller should do after examining a StepResult."""

    WAIT = "wait"
    DONE = "done"
    RESUME_OR_CANCEL = "resume-or-cancel"
    RESUME = "resume"
    WAIT_FOR_CHECKPOINT = "wait-for-checkpoint"
    WAIT_FOR_EVENT = "wait-for-event"


@dataclass(frozen=True)
class StepResult:
    """Result of one state machine transition.

    A non-terminal step carries an output delta (lines appended since the
    previous step) and optionally a checkpoint and yield reason. A terminal
    step carries the final ``ReviewResult``.
    """

    state: ReviewRunState
    output_delta: tuple[str, ...] = ()
    checkpoint: RunCheckpoint | None = None
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
    checkpoint_store: CheckpointStore = field(default_factory=CheckpointStore)

    def can_resume(self) -> bool:
        return self.state is ReviewRunState.RESUMABLE

    def can_yield(self) -> bool:
        return self.state is ReviewRunState.RUNNING and self.harness.capabilities.yieldable

    def has_result(self) -> bool:
        return self._terminal_result is not None

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
        """Begin execution. The harness runs its first step.

        One-shot adapters reach COMPLETE/FAILED here; resumable adapters may
        return YIELDED with a checkpoint.
        """
        self._transition(ReviewRunState.RUNNING)
        return StepResult(
            state=self.state,
            next_action=StepAction.WAIT.value,
        )

    def advance(self) -> StepResult:
        """Continue after a non-terminal step."""
        if self.state not in (ReviewRunState.RUNNING, ReviewRunState.RESUMABLE):
            raise ReviewRunError(
                f"cannot advance from {self.state.value}: start or resume first"
            )
        return StepResult(
            state=self.state,
            next_action=StepAction.WAIT.value,
        )

    def checkpoint_ready(self, checkpoint: RunCheckpoint) -> StepResult:
        """The harness has saved a checkpoint; the run is now RESUMABLE."""
        if not self.harness.capabilities.checkpoint:
            raise ReviewRunError("checkpoint capability is not advertised by this harness")
        if self.state is not ReviewRunState.YIELDED:
            raise ReviewRunError(
                f"cannot checkpoint from {self.state.value}: only YIELDED runs can checkpoint"
            )
        self.checkpoint_store.put(self.run.identity, checkpoint)
        self._transition(ReviewRunState.RESUMABLE)
        return StepResult(
            state=ReviewRunState.RESUMABLE,
            checkpoint=checkpoint,
            next_action=StepAction.RESUME_OR_CANCEL.value,
        )

    def external_event_received(self) -> StepResult:
        """An external event arrived (e.g. CI finished, build completed)."""
        if self.state is not ReviewRunState.WAITING_EXTERNAL:
            raise ReviewRunError(
                f"cannot receive an external event from {self.state.value}"
            )
        if self.checkpoint_store.get(self.run.identity) is None:
            raise ReviewRunError(
                "no persisted checkpoint exists for this external wait"
            )
        self._transition(ReviewRunState.RESUMABLE)
        return StepResult(
            state=ReviewRunState.RESUMABLE,
            next_action=StepAction.RESUME.value,
        )

    def resume(self, checkpoint: RunCheckpoint | None = None) -> StepResult:
        """Resume from a checkpoint. The harness continues where it yielded."""
        if not self.harness.capabilities.resumable:
            raise ReviewRunError("resumable capability is not advertised by this harness")
        if self.state is ReviewRunState.STALE:
            raise ReviewRunError(_STALE_HELP)
        if self.state is not ReviewRunState.RESUMABLE:
            raise ReviewRunError(
                f"cannot resume from {self.state.value}: only RESUMABLE runs can resume"
            )
        stored = self.checkpoint_store.get(self.run.identity)
        if stored is None:
            raise ReviewRunError("no persisted checkpoint exists for this run")
        self._transition(ReviewRunState.RUNNING)
        return StepResult(
            state=ReviewRunState.RUNNING,
            checkpoint=stored,
            next_action=StepAction.WAIT.value,
        )

    def mark_yielded(self, reason: str = "") -> StepResult:
        """The harness voluntarily yielded."""
        if not self.harness.capabilities.yieldable:
            raise ReviewRunError("yieldable capability is not advertised by this harness")
        self._transition(ReviewRunState.YIELDED)
        return StepResult(
            state=ReviewRunState.YIELDED,
            yield_reason=reason,
            next_action=StepAction.WAIT_FOR_CHECKPOINT.value,
        )

    def mark_waiting_external(self, checkpoint: RunCheckpoint) -> StepResult:
        """Persist a continuation before waiting for an external event."""
        if not self.harness.capabilities.resumable:
            raise ReviewRunError("resumable capability is not advertised by this harness")
        if not self.harness.capabilities.checkpoint:
            raise ReviewRunError("checkpoint capability is not advertised by this harness")
        if self.state is not ReviewRunState.RUNNING:
            raise ReviewRunError(
                f"cannot wait for an external event from {self.state.value}"
            )
        self.checkpoint_store.put(self.run.identity, checkpoint)
        self._transition(ReviewRunState.WAITING_EXTERNAL)
        return StepResult(
            state=ReviewRunState.WAITING_EXTERNAL,
            checkpoint=checkpoint,
            next_action=StepAction.WAIT_FOR_EVENT.value,
        )

    def complete(self, result: ReviewResult) -> StepResult:
        """The review finished with a terminal result."""
        self._transition(ReviewRunState.COMPLETE)
        self._terminal_result = result
        return StepResult(
            state=ReviewRunState.COMPLETE,
            terminal_result=result,
            next_action=StepAction.DONE.value,
        )

    def fail(self, error: str = "") -> StepResult:
        """The review failed."""
        self._transition(ReviewRunState.FAILED)
        return StepResult(
            state=ReviewRunState.FAILED,
            yield_reason=error,
            next_action=StepAction.DONE.value,
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
            next_action=StepAction.DONE.value,
        )

    def stale(self) -> StepResult:
        """Mark the run stale because the pull request head changed."""
        allowed_targets = _TRANSITIONS.get(self.state)
        if allowed_targets is None or ReviewRunState.STALE not in allowed_targets:
            raise ReviewRunError(
                f"cannot become stale from {self.state.value}"
            )
        self._transition(ReviewRunState.STALE)
        return StepResult(
            state=ReviewRunState.STALE,
            next_action=StepAction.DONE.value,
        )


__all__ = [
    "ReviewRun",
    "ReviewRunController",
    "ReviewRunError",
    "ReviewRunState",
    "RunCheckpoint",
    "CheckpointStore",
    "StepAction",
    "StepResult",
]
