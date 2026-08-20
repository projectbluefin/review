"""Pure semantic view models for the maintainer review dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Mapping

from review_result import ReviewResult


_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEAD_EVIDENCE = _SHA


class ActionID(str, Enum):
    START_REVIEW = "start-review"
    STEER_REVIEW = "steer-review"
    STOP_REVIEW = "stop-review"
    CHOOSE_REVIEW_VERDICT = "choose-review-verdict"
    APPROVE_REVIEW = "approve-review"
    REQUEST_CHANGES = "request-changes"
    COMMENT_REVIEW = "comment-review"
    SUBMIT_REVIEW = "submit-review"
    APPROVE_AND_QUEUE = "approve-and-queue"
    MERGE_NOW = "merge-now"
    UPDATE_BRANCH = "update-branch"
    CLOSE_PULL_REQUEST = "close-pull-request"
    ADD_PULL_REQUEST_COMMENT = "add-pull-request-comment"
    RESOLVE_DUPLICATES = "resolve-duplicates"
    VIEW_DIFF = "view-diff"
    TOGGLE_EVIDENCE = "toggle-evidence"
    BATCH_SELECT = "batch-select"
    FILTER_QUEUE = "filter-queue"
    REFRESH = "refresh"
    COPY_REVIEW_CONTEXT = "copy-review-context"
    OPEN_BROWSER = "open-browser"
    ASK_HIVE = "ask-hive"
    SHOW_DOCS_UPDATE_TASK = "show-docs-update-task"
    SHOW_GHOST_BUILD_TASK = "show-ghost-build-task"
    NAVIGATE_UP = "navigate-up"
    NAVIGATE_DOWN = "navigate-down"
    NAVIGATE_FIRST = "navigate-first"
    NAVIGATE_LAST = "navigate-last"
    NAVIGATE_PAGE_UP = "navigate-page-up"
    NAVIGATE_PAGE_DOWN = "navigate-page-down"
    PANE_NEXT = "pane-next"
    PANE_PREVIOUS = "pane-previous"
    BACK = "back"
    HELP = "help"
    OPEN_COMMAND_PALETTE = "open-command-palette"
    SWITCH_HARNESS = "switch-harness"
    PREPARE_HARNESS = "prepare-harness"
    SIGN_IN_HARNESS = "sign-in-harness"
    INSTALL_HARNESS = "install-harness"
    RETRY_HARNESS_DETECTION = "retry-harness-detection"
    HARNESS_DIAGNOSTICS = "harness-diagnostics"
    GENERATE_BODY = "generate-body"
    EDIT_BODY = "edit-body"
    PREVIEW_BODY = "preview-body"
    CLOSE_LAYER = "close-layer"
    QUIT = "quit"


@dataclass(frozen=True)
class ActionSpec:
    label: str
    mutating: bool = False
    confirmation_required: bool = False
    ordinary_journey: bool = True
    suspended_in_editor: bool = False


_ACTION_SPECS = (
    (ActionID.START_REVIEW, ActionSpec("Start review")),
    (ActionID.STEER_REVIEW, ActionSpec("Steer review")),
    (ActionID.STOP_REVIEW, ActionSpec("Stop review")),
    (ActionID.CHOOSE_REVIEW_VERDICT, ActionSpec("Choose review verdict")),
    (ActionID.APPROVE_REVIEW, ActionSpec("Approve review")),
    (ActionID.REQUEST_CHANGES, ActionSpec("Request changes")),
    (ActionID.COMMENT_REVIEW, ActionSpec("Comment on review")),
    (ActionID.SUBMIT_REVIEW, ActionSpec("Submit review", True, True)),
    (ActionID.APPROVE_AND_QUEUE, ActionSpec("Approve and queue", True, True)),
    (ActionID.MERGE_NOW, ActionSpec("Merge now", True, True)),
    (ActionID.UPDATE_BRANCH, ActionSpec("Update branch", True, True)),
    (ActionID.CLOSE_PULL_REQUEST, ActionSpec("Close pull request", True, True)),
    (ActionID.ADD_PULL_REQUEST_COMMENT, ActionSpec("Add pull request comment", True, True)),
    (ActionID.RESOLVE_DUPLICATES, ActionSpec("Resolve duplicates", True, True)),
    (ActionID.VIEW_DIFF, ActionSpec("View diff")),
    (ActionID.TOGGLE_EVIDENCE, ActionSpec("Toggle evidence")),
    (ActionID.BATCH_SELECT, ActionSpec("Batch select")),
    (ActionID.FILTER_QUEUE, ActionSpec("Filter queue")),
    (ActionID.REFRESH, ActionSpec("Refresh")),
    (ActionID.COPY_REVIEW_CONTEXT, ActionSpec("Copy review context")),
    (ActionID.OPEN_BROWSER, ActionSpec("Open in browser", ordinary_journey=False)),
    (ActionID.ASK_HIVE, ActionSpec("Ask Hive")),
    (ActionID.SHOW_DOCS_UPDATE_TASK, ActionSpec("Show docs update task")),
    (ActionID.SHOW_GHOST_BUILD_TASK, ActionSpec("Show Ghost build task")),
    (ActionID.NAVIGATE_UP, ActionSpec("Navigate up", suspended_in_editor=True)),
    (ActionID.NAVIGATE_DOWN, ActionSpec("Navigate down", suspended_in_editor=True)),
    (ActionID.NAVIGATE_FIRST, ActionSpec("Navigate first", suspended_in_editor=True)),
    (ActionID.NAVIGATE_LAST, ActionSpec("Navigate last", suspended_in_editor=True)),
    (ActionID.NAVIGATE_PAGE_UP, ActionSpec("Navigate page up", suspended_in_editor=True)),
    (ActionID.NAVIGATE_PAGE_DOWN, ActionSpec("Navigate page down", suspended_in_editor=True)),
    (ActionID.PANE_NEXT, ActionSpec("Focus next pane", suspended_in_editor=True)),
    (ActionID.PANE_PREVIOUS, ActionSpec("Focus previous pane", suspended_in_editor=True)),
    (ActionID.BACK, ActionSpec("Back")),
    (ActionID.HELP, ActionSpec("Help")),
    (ActionID.OPEN_COMMAND_PALETTE, ActionSpec("Open command palette")),
    (ActionID.SWITCH_HARNESS, ActionSpec("Switch harness")),
    (ActionID.PREPARE_HARNESS, ActionSpec("Prepare harness")),
    (ActionID.SIGN_IN_HARNESS, ActionSpec("Sign in to harness", confirmation_required=True)),
    (ActionID.INSTALL_HARNESS, ActionSpec("Install harness", confirmation_required=True)),
    (ActionID.RETRY_HARNESS_DETECTION, ActionSpec("Retry harness detection")),
    (ActionID.HARNESS_DIAGNOSTICS, ActionSpec("Harness diagnostics")),
    (ActionID.GENERATE_BODY, ActionSpec("Generate review body")),
    (ActionID.EDIT_BODY, ActionSpec("Edit review body")),
    (ActionID.PREVIEW_BODY, ActionSpec("Preview review body")),
    (ActionID.CLOSE_LAYER, ActionSpec("Close")),
    (ActionID.QUIT, ActionSpec("Quit")),
)

ACTIONS: Mapping[ActionID, ActionSpec] = MappingProxyType(dict(_ACTION_SPECS))


class DecisionState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    CLEAN = "clean"
    FINDINGS = "findings"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    UNPARSABLE = "unparsable"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SemanticStatus:
    value: str
    label: str


_UNKNOWN_FRESHNESS = SemanticStatus("unknown", "FRESHNESS UNKNOWN")
_CURRENT_FRESHNESS = SemanticStatus("current", "CURRENT")
_STALE_FRESHNESS = SemanticStatus("stale", "STALE")
_UNKNOWN_MERGEABILITY = SemanticStatus("unknown", "MERGEABILITY UNKNOWN")
_UNKNOWN_CI = SemanticStatus("unknown", "CI UNKNOWN")
_UNKNOWN_REVIEW = SemanticStatus("unknown", "REVIEW UNKNOWN")


@dataclass(frozen=True)
class ReviewStateView:
    state: DecisionState
    label: str


@dataclass(frozen=True)
class QueueRow:
    repository: str
    number: int
    title: str
    author: str
    exact_head: str | None
    mergeability: SemanticStatus
    ci: SemanticStatus
    review: SemanticStatus
    primary_action: ActionID | None
    tldr: str = ""
    reviewed_head: str | None = None
    freshness: SemanticStatus = _UNKNOWN_FRESHNESS
    available_actions: tuple[ActionID, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def current_head(self) -> str | None:
        return self.exact_head


@dataclass(frozen=True)
class FindingView:
    severity: str
    title: str
    file: str
    line: int
    end_line: int | None = None


@dataclass(frozen=True)
class VerificationView:
    name: str
    state: str
    evidence: str


@dataclass(frozen=True)
class ProvenanceView:
    backend: str
    model: str
    provider: str = ""
    effort: str = ""


@dataclass(frozen=True)
class DecisionSummary:
    what_changed: str
    risk_impact: str
    ci_merge_state: str
    recommended_action: str


@dataclass(frozen=True)
class DecisionCard:
    state: DecisionState
    exact_head: str | None
    counts: Mapping[str, int]
    findings: tuple[FindingView, ...]
    verification: tuple[VerificationView, ...]
    provenance: ProvenanceView
    summary: DecisionSummary
    raw_evidence: tuple[str, ...]
    repository: str = ""
    number: int = 0
    title: str = ""
    tldr: str = ""
    reviewed_head: str | None = None
    freshness: SemanticStatus = _UNKNOWN_FRESHNESS
    ci: SemanticStatus = _UNKNOWN_CI
    mergeability: SemanticStatus = _UNKNOWN_MERGEABILITY
    review: SemanticStatus = _UNKNOWN_REVIEW
    available_actions: tuple[ActionID, ...] = ()
    duplicate_count: int = 0
    shared_file_count: int = 0
    merge_state: str = "?"

    @property
    def clean(self) -> bool:
        return self.state is DecisionState.CLEAN

    @property
    def current_head(self) -> str | None:
        return self.exact_head


_MERGEABILITY = {
    "clean": SemanticStatus("clean", "MERGEABLE"),
    "mergeable": SemanticStatus("clean", "MERGEABLE"),
    "dirty": SemanticStatus("dirty", "CONFLICTS"),
    "conflicting": SemanticStatus("dirty", "CONFLICTS"),
    "blocked": SemanticStatus("blocked", "BLOCKED"),
    "unstable": SemanticStatus("unstable", "UNSTABLE"),
}
_CI = {
    "success": SemanticStatus("success", "CI GREEN"),
    "failure": SemanticStatus("failure", "CI FAILED"),
    "pending": SemanticStatus("pending", "CI PENDING"),
}
_REVIEW = {
    "approved": SemanticStatus("approved", "APPROVED"),
    "changes_requested": SemanticStatus("changes_requested", "CHANGES REQUESTED"),
    "review_required": SemanticStatus("review_required", "REVIEW REQUIRED"),
}
_PRIMARY_ACTION = {
    "review": ActionID.START_REVIEW,
    "approve": ActionID.CHOOSE_REVIEW_VERDICT,
    "request-changes": ActionID.REQUEST_CHANGES,
    "comment": ActionID.COMMENT_REVIEW,
    "queue": ActionID.APPROVE_AND_QUEUE,
    "merge": ActionID.MERGE_NOW,
    "update": ActionID.UPDATE_BRANCH,
}
_DECISION_STATES = {
    "complete": DecisionState.CLEAN,
    "findings": DecisionState.FINDINGS,
    "incomplete": DecisionState.INCOMPLETE,
    "failed": DecisionState.FAILED,
    "unparsable": DecisionState.UNPARSABLE,
}

_STATE_LABELS = {state: state.value.upper() for state in DecisionState}


def _exact_head(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _reviewed_head(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEAD_EVIDENCE.fullmatch(value) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _positive_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _key(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _status(
    statuses: Mapping[str, SemanticStatus], value: Any, unknown: SemanticStatus
) -> SemanticStatus:
    return statuses.get(_key(value), unknown)


def _available_actions(value: Any) -> tuple[ActionID, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    actions: list[ActionID] = []
    for raw in value:
        try:
            action = raw if isinstance(raw, ActionID) else ActionID(raw)
        except (TypeError, ValueError):
            continue
        if action not in actions:
            actions.append(action)
    return tuple(actions)


def _heads_agree(first: str, second: str) -> bool:
    return first == second


def _result_head_evidence(result: ReviewResult) -> tuple[str | None, bool]:
    provenance = _mapping(result.provenance)
    live = _mapping(result.live)
    provenance_head = _reviewed_head(provenance.get("head_sha"))
    live_head = _reviewed_head(live.get("head_sha") or live.get("head"))
    if provenance_head and live_head and not _heads_agree(provenance_head, live_head):
        return None, True
    return provenance_head or live_head, False


def _head_freshness(
    reviewed_head: str | None,
    current_head: str | None,
    declared: Any = None,
    *,
    conflict: bool = False,
) -> SemanticStatus:
    if conflict or (reviewed_head and current_head and not _heads_agree(reviewed_head, current_head)):
        return _STALE_FRESHNESS
    if reviewed_head and current_head and _heads_agree(reviewed_head, current_head):
        return _CURRENT_FRESHNESS
    if _key(declared) == "stale":
        return _STALE_FRESHNESS
    return _UNKNOWN_FRESHNESS


def _bind_head(
    result: ReviewResult, exact_head: Any
) -> tuple[str | None, str | None, SemanticStatus]:
    current_head = _exact_head(exact_head)
    reviewed_head, conflict = _result_head_evidence(result)
    freshness = _head_freshness(reviewed_head, current_head, conflict=conflict)
    if freshness is _CURRENT_FRESHNESS:
        return current_head, reviewed_head, freshness
    return None, reviewed_head, freshness


def _bound_head(result: ReviewResult, exact_head: Any) -> str | None:
    return _bind_head(result, exact_head)[0]


def build_review_state(value: str | DecisionState) -> ReviewStateView:
    if isinstance(value, DecisionState):
        state = value
    elif isinstance(value, str):
        try:
            state = DecisionState(value.lower())
        except ValueError:
            state = DecisionState.UNPARSABLE
    else:
        state = DecisionState.UNPARSABLE
    return ReviewStateView(state, _STATE_LABELS[state])


def build_queue_row(snapshot: Mapping[str, Any]) -> QueueRow:
    """Build immutable queue meaning from an existing queue snapshot."""

    repository = snapshot.get("repository")
    number = snapshot.get("number")
    title = snapshot.get("title")
    author = snapshot.get("author")
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(title, str)
        or not isinstance(author, str)
    ):
        raise ValueError("queue identity fields are invalid")
    current_head = _exact_head(snapshot.get("head_sha"))
    reviewed_head = _reviewed_head(
        snapshot.get("reviewed_head_sha") or snapshot.get("reviewed_head")
    )
    mergeability = _status(_MERGEABILITY, snapshot.get("mergeable_state"), _UNKNOWN_MERGEABILITY)
    ci = _status(_CI, snapshot.get("check_state"), _UNKNOWN_CI)
    review = _status(_REVIEW, snapshot.get("review_state"), _UNKNOWN_REVIEW)
    return QueueRow(
        repository,
        number,
        title,
        author,
        current_head,
        mergeability,
        ci,
        review,
        _PRIMARY_ACTION.get(snapshot.get("recommended_action")),
        tldr=_text(snapshot.get("tldr") or snapshot.get("summary")),
        reviewed_head=reviewed_head,
        freshness=_head_freshness(
            reviewed_head, current_head, snapshot.get("freshness")
        ),
        available_actions=_available_actions(snapshot.get("available_actions")),
    )


def build_decision_card(result: ReviewResult, *, exact_head: str) -> DecisionCard:
    """Build terminal decision meaning without adding mutation authority."""

    state = _DECISION_STATES.get(result.state, DecisionState.UNPARSABLE)
    bound_head, reviewed_head, freshness = _bind_head(result, exact_head)
    if state in (DecisionState.CLEAN, DecisionState.FINDINGS) and freshness.value != "current":
        state = DecisionState.STALE
    live = _mapping(result.live)
    raw_provenance = _mapping(result.provenance)
    repository = _text(live.get("repository") or raw_provenance.get("repository"))
    number = _positive_int(live.get("number")) or _positive_int(
        raw_provenance.get("pull_request")
    )
    title = _text(live.get("title"))
    tldr = _text(live.get("tldr") or live.get("summary"))
    mergeability = _status(
        _MERGEABILITY,
        live.get("mergeable_state") or live.get("mergeable") or live.get("merge_state"),
        _UNKNOWN_MERGEABILITY,
    )
    ci = _status(_CI, live.get("check_state") or live.get("ci"), _UNKNOWN_CI)
    review = _status(
        _REVIEW,
        live.get("review_state") or live.get("reviewDecision"),
        _UNKNOWN_REVIEW,
    )
    findings = tuple(
        FindingView(
            item["severity"],
            item["title"],
            item["file"],
            item["line"],
            item.get("end_line"),
        )
        for item in result.findings
    )
    verification = tuple(
        VerificationView(item["name"], item["state"], item["evidence"])
        for item in result.verification
    )
    provenance = ProvenanceView(
        str(raw_provenance.get("backend", "")),
        str(raw_provenance.get("model", "")),
        str(raw_provenance.get("provider", "")),
        str(raw_provenance.get("effort") or raw_provenance.get("reasoning_effort") or ""),
    )
    what_changed = tldr or title or "No change summary was provided."
    finding_total = sum(result.counts.values())
    if state is DecisionState.FINDINGS:
        highest = next((
            severity for severity in ("critical", "high", "medium", "low")
            if result.counts.get(severity, 0)
        ), "unknown")
        risk_impact = (
            f"{highest.upper()} risk · {finding_total} actionable "
            f"finding{'s' if finding_total != 1 else ''}"
        )
        recommended_action = (
            "Request changes or comment on the cited "
            f"finding{'s' if finding_total != 1 else ''}."
        )
    elif state is DecisionState.CLEAN:
        risk_impact = "No evidenced review risk."
        if ci.value == "success" and mergeability.value == "clean":
            recommended_action = "Approve, queue, or merge after human judgment."
        else:
            recommended_action = "Review the evidence; wait for green CI before landing."
    elif state is DecisionState.STALE:
        risk_impact = "Risk unknown · the reviewed head is stale."
        recommended_action = "Rerun the review on the current head."
    else:
        risk_impact = "Risk unknown · the review did not complete."
        recommended_action = "Open diagnostics and rerun the review."
    summary = DecisionSummary(
        what_changed,
        risk_impact,
        f"{ci.label} · {mergeability.label}",
        recommended_action,
    )
    duplicates = result.overlap.get("duplicates") or []
    shared_files = result.overlap.get("shared_files") or result.overlap.get("overlaps") or []
    return DecisionCard(
        state,
        bound_head,
        MappingProxyType(dict(result.counts)),
        findings,
        verification,
        provenance,
        summary,
        tuple(result.raw_evidence),
        repository=repository,
        number=number,
        title=title,
        tldr=tldr,
        reviewed_head=reviewed_head,
        freshness=freshness,
        ci=ci,
        mergeability=mergeability,
        review=review,
        available_actions=_available_actions(live.get("available_actions")),
        duplicate_count=len(duplicates),
        shared_file_count=len(shared_files),
        merge_state=_text(live.get("merge_state") or live.get("mergeStateStatus")) or "?",
    )


__all__ = [
    "ACTIONS",
    "ActionID",
    "ActionSpec",
    "DecisionCard",
    "DecisionSummary",
    "DecisionState",
    "FindingView",
    "ProvenanceView",
    "QueueRow",
    "ReviewStateView",
    "SemanticStatus",
    "VerificationView",
    "build_decision_card",
    "build_queue_row",
    "build_review_state",
]
