"""Durable run state for exact-head pull request orchestration."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from tui.review_receipt import ReceiptIdentity as RunIdentity

from tui.model_profiles import is_high_assurance

RUN_STATE_VERSION = 1
DEFAULT_MAX_RECORDS = 500
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


class IllegalRunTransition(RuntimeError):
    """Raised when the run state machine rejects a transition."""


class RunState(str, Enum):
    PENDING = "pending"
    REVIEWING = "reviewing"
    REVIEW_CLEAN = "review_clean"
    REVIEW_FINDINGS = "review_findings"
    ESCALATION_REQUIRED = "escalation_required"
    RE_REVIEWING = "re_reviewing"
    MUTATING = "mutating"
    BLOCKED = "blocked"
    RETRY_AT = "retry_at"
    COMPLETED = "completed"
    REVIEW_MISSING = "review_missing"
    REVIEW_FAILED = "review_failed"
    REVIEW_INCOMPLETE = "review_incomplete"
    REVIEW_UNPARSABLE = "review_unparsable"
    MUTATION_FAILED = "mutation_failed"
    HEAD_CHANGED = "head_changed"
    HUMAN_REVIEW_MISSING = "human_review_missing"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_OUTCOMES


class TerminalOutcome(str, Enum):
    COMPLETED = "completed"
    REVIEW_MISSING = "review_missing"
    REVIEW_FAILED = "review_failed"
    REVIEW_INCOMPLETE = "review_incomplete"
    REVIEW_UNPARSABLE = "review_unparsable"
    MUTATION_FAILED = "mutation_failed"
    HEAD_CHANGED = "head_changed"
    HUMAN_REVIEW_MISSING = "human_review_missing"


_TERMINAL_OUTCOMES = {
    RunState.COMPLETED: TerminalOutcome.COMPLETED,
    RunState.REVIEW_MISSING: TerminalOutcome.REVIEW_MISSING,
    RunState.REVIEW_FAILED: TerminalOutcome.REVIEW_FAILED,
    RunState.REVIEW_INCOMPLETE: TerminalOutcome.REVIEW_INCOMPLETE,
    RunState.REVIEW_UNPARSABLE: TerminalOutcome.REVIEW_UNPARSABLE,
    RunState.MUTATION_FAILED: TerminalOutcome.MUTATION_FAILED,
    RunState.HEAD_CHANGED: TerminalOutcome.HEAD_CHANGED,
    RunState.HUMAN_REVIEW_MISSING: TerminalOutcome.HUMAN_REVIEW_MISSING,
}

_IN_FLIGHT = frozenset({RunState.REVIEWING, RunState.RE_REVIEWING, RunState.MUTATING})
_MAY_MUTATE = frozenset({RunState.REVIEW_CLEAN, RunState.REVIEW_FINDINGS})
_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({
        RunState.REVIEWING,
        RunState.RE_REVIEWING,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.REVIEWING: frozenset({
        RunState.REVIEW_CLEAN,
        RunState.REVIEW_FINDINGS,
        RunState.REVIEW_MISSING,
        RunState.REVIEW_FAILED,
        RunState.REVIEW_INCOMPLETE,
        RunState.REVIEW_UNPARSABLE,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.REVIEW_CLEAN: frozenset({
        RunState.MUTATING,
        RunState.ESCALATION_REQUIRED,
        RunState.RE_REVIEWING,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.REVIEW_FINDINGS: frozenset({
        RunState.MUTATING,
        RunState.ESCALATION_REQUIRED,
        RunState.RE_REVIEWING,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.ESCALATION_REQUIRED: frozenset({
        RunState.RE_REVIEWING,
        RunState.REVIEWING,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.RE_REVIEWING: frozenset({
        RunState.REVIEW_CLEAN,
        RunState.REVIEW_FINDINGS,
        RunState.REVIEW_MISSING,
        RunState.REVIEW_FAILED,
        RunState.REVIEW_INCOMPLETE,
        RunState.REVIEW_UNPARSABLE,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.MUTATING: frozenset({
        RunState.COMPLETED,
        RunState.MUTATION_FAILED,
        RunState.HUMAN_REVIEW_MISSING,
        RunState.HEAD_CHANGED,
        RunState.BLOCKED,
        RunState.RETRY_AT,
    }),
    RunState.BLOCKED: frozenset({
        RunState.RETRY_AT,
        RunState.HEAD_CHANGED,
    }),
    RunState.RETRY_AT: frozenset({
        RunState.BLOCKED,
        RunState.HEAD_CHANGED,
    }),
}

REVIEW_PROVIDER_TERMINALS = frozenset({
    RunState.REVIEW_FAILED,
    RunState.REVIEW_MISSING,
    RunState.REVIEW_INCOMPLETE,
    RunState.REVIEW_UNPARSABLE,
})

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class RunRecord:
    identity: RunIdentity
    state: RunState
    terminal_outcome: TerminalOutcome | None
    reason: str
    retry_at: str
    created_at: int
    updated_at: int
    sequence: int
    resume_state: RunState | None = None

    @property
    def run_id(self) -> str:
        return self.identity.cache_identity

    @property
    def in_flight(self) -> bool:
        return self.state in _IN_FLIGHT

    @property
    def repository(self) -> str:
        return self.identity.repository

    @property
    def number(self) -> int:
        return self.identity.pull_request

    @property
    def base_sha(self) -> str:
        return self.identity.base_sha

    @property
    def head_sha(self) -> str:
        return self.identity.head_sha

    @property
    def backend(self) -> str:
        return self.identity.backend

    @property
    def model(self) -> str:
        return self.identity.model

    @property
    def effort(self) -> str:
        return self.identity.effort

    @property
    def check_scope_version(self) -> str:
        return self.identity.check_scope_version

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def may_mutate(self) -> bool:
        return self.state in _MAY_MUTATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "state": self.state.value,
            "terminal_outcome": (
                self.terminal_outcome.value if self.terminal_outcome else None
            ),
            "reason": self.reason,
            "retry_at": self.retry_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sequence": self.sequence,
            "resume_state": self.resume_state.value if self.resume_state else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunRecord":
        state = RunState(data["state"])
        outcome_value = data.get("terminal_outcome")
        outcome = TerminalOutcome(outcome_value) if outcome_value else None
        expected = _TERMINAL_OUTCOMES.get(state)
        if outcome != expected:
            raise ValueError("run terminal outcome does not match state")
        resume_val = data.get("resume_state")
        resume_state = RunState(resume_val) if resume_val else None
        return cls(
            RunIdentity.from_dict(data["identity"]),
            state,
            outcome,
            str(data.get("reason") or ""),
            str(data.get("retry_at") or ""),
            int(data["created_at"]),
            int(data["updated_at"]),
            int(data["sequence"]),
            resume_state,
        )


def _thread_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


class RunStateStore:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        if isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be positive")
        if root is None:
            state_root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            root = os.path.join(state_root, "bluefin-review", "run-state")
        self.root = Path(root).expanduser()
        self.max_records = max_records
        self.path = self.root / "run-state.json"
        self.lock_path = self.root / ".run-state.lock"
        self._thread_lock = _thread_lock(self.lock_path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(descriptor) as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> tuple[dict[str, RunRecord], int]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, 1
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("run state store is unreadable") from error
        if not isinstance(payload, dict) or payload.get("version") != RUN_STATE_VERSION:
            raise ValueError("unsupported run state store")
        records = [
            RunRecord.from_dict(record)
            for record in payload.get("records", [])
            if isinstance(record, dict)
        ]
        next_sequence = int(payload.get("next_sequence") or 1)
        return {record.run_id: record for record in records}, next_sequence

    def _write(self, records: Mapping[str, RunRecord], next_sequence: int) -> None:
        kept = self._bounded(records.values())
        payload = {
            "version": RUN_STATE_VERSION,
            "next_sequence": next_sequence,
            "records": [record.to_dict() for record in kept],
        }
        descriptor, name = tempfile.mkstemp(
            dir=self.root, prefix=".run-state-", suffix=".tmp"
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._fsync_root()
        finally:
            temporary.unlink(missing_ok=True)

    def _fsync_root(self) -> None:
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _bounded(self, records: Any) -> list[RunRecord]:
        record_list = list(records)
        if len(record_list) <= self.max_records:
            return sorted(record_list, key=lambda record: record.sequence)
        in_flight = [r for r in record_list if r.in_flight]
        terminal_or_idle = [r for r in record_list if not r.in_flight]
        terminal_or_idle.sort(key=lambda r: r.sequence)
        remaining_slots = max(0, self.max_records - len(in_flight))
        kept_terminal = terminal_or_idle[-remaining_slots:] if remaining_slots > 0 else []
        return sorted(in_flight + kept_terminal, key=lambda r: r.sequence)

    def create(self, identity: RunIdentity) -> RunRecord:
        with self._locked():
            records, next_sequence = self._load()
            run_id = identity.cache_identity
            existing = records.get(run_id)
            if existing is not None:
                return existing
            now = time.time_ns()
            record = RunRecord(
                identity,
                RunState.PENDING,
                None,
                "",
                "",
                now,
                now,
                next_sequence,
            )
            records[run_id] = record
            self._write(records, next_sequence + 1)
            return record

    def get(self, identity: RunIdentity) -> RunRecord | None:
        with self._locked():
            records, _next_sequence = self._load()
            return records.get(identity.cache_identity)

    def get_by_pr(self, repository: str, pull_request: int) -> RunRecord | None:
        with self._locked():
            records, _ = self._load()
            for record in reversed(sorted(records.values(), key=lambda r: r.sequence)):
                if (
                    record.identity.repository == repository
                    and record.identity.pull_request == pull_request
                ):
                    return record
            return None

    def records(self) -> list[RunRecord]:
        with self._locked():
            records, _next_sequence = self._load()
            return sorted(records.values(), key=lambda record: record.sequence)

    def snapshot(self) -> dict[str, RunRecord]:
        """A single locked read returning all records indexed by cache_identity."""
        with self._locked():
            records, _next_sequence = self._load()
            return dict(records)

    def active_records(self) -> list[RunRecord]:
        with self._locked():
            records, _next_sequence = self._load()
            return [r for r in sorted(records.values(), key=lambda record: record.sequence) if r.in_flight]

    def may_mutate(self, identity: RunIdentity) -> bool:
        record = self.get(identity)
        return record.may_mutate() if record else False

    @contextlib.contextmanager
    def in_flight(
        self,
        identity: RunIdentity,
        target: RunState = RunState.REVIEWING,
        *,
        on_exit: RunState = RunState.REVIEW_INCOMPLETE,
        exit_reason: str = "exited before completing",
    ) -> Iterator[RunRecord]:
        record = self.get(identity)
        if record is None:
            record = self.create(identity)
        if record.state != target:
            record = self.transition(identity, target)
        try:
            yield record
        finally:
            current = self.get(identity)
            if current is not None and current.in_flight:
                allowed = set(_TRANSITIONS.get(current.state, frozenset()))
                if current.state in {RunState.BLOCKED, RunState.RETRY_AT} and current.resume_state:
                    allowed.add(current.resume_state)
                if on_exit in allowed:
                    self.transition(identity, on_exit, reason=exit_reason)

    def transition(
        self,
        identity: RunIdentity,
        target: RunState,
        *,
        reason: str = "",
        retry_at: str = "",
        low_risk: bool = False,
        retry: bool = False,
    ) -> RunRecord:
        """Transition a run to a target state.

        Terminal states cannot be transitioned out of in normal execution.
        The sole exception is explicit `retry=True`, which allows transitioning
        from review-provider terminal states (`REVIEW_PROVIDER_TERMINALS`:
        `REVIEW_FAILED`, `REVIEW_MISSING`, `REVIEW_INCOMPLETE`, `REVIEW_UNPARSABLE`)
        back to `REVIEWING` (via `retry_review()`) for durable provider failures
        at the same head. All other terminal state transitions remain strictly
        forbidden and raise `IllegalRunTransition`. Once a run reaches any other
        terminal state (such as COMPLETED, MUTATION_FAILED, or HEAD_CHANGED),
        a new run requires advancing to a new head SHA.

        A run in BLOCKED or RETRY_AT remembers the non-terminal state it paused
        from in `resume_state`. Leaving BLOCKED or RETRY_AT is permitted only
        back to that exact resume state, or onward to HEAD_CHANGED. Arbitrary
        transitions (e.g. jumping from BLOCKED to MUTATING without prior clean
        review) raise IllegalRunTransition.
        """
        with self._locked():
            records, next_sequence = self._load()
            run_id = identity.cache_identity
            record = records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run {run_id}")
            if record.is_terminal:
                if retry and record.state in REVIEW_PROVIDER_TERMINALS and target == RunState.REVIEWING:
                    allowed = {RunState.REVIEWING}
                else:
                    raise IllegalRunTransition(
                        f"cannot transition from terminal state {record.state.value} to {target.value}"
                    )
            else:
                allowed = set(_TRANSITIONS.get(record.state, frozenset()))
            if record.state in {RunState.BLOCKED, RunState.RETRY_AT} and record.resume_state:
                allowed.add(record.resume_state)
            if target not in allowed:
                raise IllegalRunTransition(
                    f"cannot transition from {record.state.value} to {target.value}"
                )
            if target == RunState.MUTATING and not low_risk and not is_high_assurance(identity):
                raise IllegalRunTransition(
                    f"cannot transition {identity.model} to mutating: high-assurance review required (not low-risk)"
                )
            if target == RunState.RETRY_AT and not retry_at:
                raise ValueError("retry_at state requires retry_at")
            outcome = _TERMINAL_OUTCOMES.get(target)

            if target in {RunState.BLOCKED, RunState.RETRY_AT}:
                new_resume = (
                    record.resume_state
                    if record.state in {RunState.BLOCKED, RunState.RETRY_AT}
                    else record.state
                )
            else:
                new_resume = None

            updated = replace(
                record,
                state=target,
                terminal_outcome=outcome,
                reason=reason,
                retry_at=retry_at if target == RunState.RETRY_AT else "",
                updated_at=time.time_ns(),
                sequence=next_sequence,
                resume_state=new_resume,
            )
            records[run_id] = updated
            self._write(records, next_sequence + 1)
            return updated

    def retry_review(self, identity: RunIdentity) -> RunRecord:
        """Permit same-head retry for durable review-provider failures."""
        return self.transition(identity, RunState.REVIEWING, retry=True)

    def revalidate_head(
        self, identity: RunIdentity, live_head_sha: str, *, fixer_advanced: bool = False
    ) -> RunRecord:
        if not FULL_SHA.fullmatch(live_head_sha):
            raise ValueError("live_head_sha must be a full lowercase SHA")
        record = self.get(identity)
        if record is None:
            raise KeyError(f"unknown run {identity.cache_identity}")
        if live_head_sha == identity.head_sha:
            return record
        if fixer_advanced:
            return self.transition(
                identity,
                RunState.ESCALATION_REQUIRED,
                reason=f"head advanced by fixer: reviewed {identity.head_sha[:12]}, live {live_head_sha[:12]}",
            )
        return self.transition(
            identity,
            RunState.HEAD_CHANGED,
            reason=f"reviewed {identity.head_sha}, live {live_head_sha}",
        )

    def prune(self) -> list[RunRecord]:
        with self._locked():
            records, next_sequence = self._load()
            kept = self._bounded(records.values())
            self._write({record.run_id: record for record in kept}, next_sequence)
            return kept


__all__ = [
    "DEFAULT_MAX_RECORDS",
    "IllegalRunTransition",
    "RunIdentity",
    "RunRecord",
    "RunState",
    "RunStateStore",
    "TerminalOutcome",
]
