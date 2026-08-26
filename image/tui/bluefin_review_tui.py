"""Bluefin Review Dashboard — the maintainer surface for the PR queue.

The static queue snapshot orders the work, GitHub supplies the live evidence,
Goose supplies the review, and every state-changing command runs through
exactly one confirmation gate that makes the maintainer type the pull request
number. GitHub stays authoritative for pull-request state; Hive is never asked
for work here.

This is the only maintainer surface. Runs inside the review image:
``just review-queue``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich.syntax import Syntax
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Button,
    Static,
    Select,
    TextArea,
)
from review_result import ReviewResult, adapt_current_engine
from semantic_view import DecisionState, build_decision_card
import landing
import hive_api
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from harness.codex import CodexHarness
from harness.goose import GooseHarness
from harness.autopilot import (HarnessOption, Preference, can_remember,
                               choose_option, discover_all, load_preferences,
                               remember_success)
from review_evidence_manifest import ReviewRequest, ReviewEvidenceManifest
from re_review import (DeltaInput, FindingEvidence, H1Evidence, PriorFinding,
                       Region, classify_head_delta)
from harness.registry import Availability, DraftRequest, DraftState, HarnessRegistry

QUEUE_URL = os.environ.get(
    "BLUEFIN_REVIEW_QUEUE_URL",
    "https://projectbluefin.github.io/review/queue.json",
)
PULL_FETCH_LIMIT = os.environ.get("BLUEFIN_REVIEW_PULL_LIMIT", "200")
TRACE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "bluefin-review",
    "trace.jsonl",
)
MUTATION_TIMEOUT = 60
HIVE_TIMEOUT = 15
HIVE_API_HELPER = os.path.join(os.path.dirname(__file__), "hive_api.py")
MAX_REVIEW_BODY_CHARS = 4096
# The label Hive's governor sweep scans for. It is not defined in most
# repositories; Hive's queue endpoint owns creating and applying it.
QUEUE_LABEL = "lgtm"

# The semantic registry is the source for bindings, help, and the command
# palette. IDs are stable so clickable surfaces can consume the same contract.
@dataclass(frozen=True)
class CommandSpec:
    id: str
    key: str
    action: str
    label: str
    mutating: bool = False
    suspended_in_editor: bool = True
    terminal_dispatched: bool = False


COMMANDS = (
    CommandSpec("navigate_down", "j", "navigate_down", "next item"),
    CommandSpec("navigate_up", "k", "navigate_up", "previous item"),
    CommandSpec("navigate_first", "g", "navigate_first", "first item"),
    CommandSpec("navigate_last", "G", "navigate_last", "last item"),
    CommandSpec("navigate_page_down", "ctrl+d", "navigate_page_down", "page down"),
    CommandSpec("navigate_page_up", "ctrl+u", "navigate_page_up", "page up"),
    CommandSpec("pane_previous", "h", "pane_previous", "previous pane"),
    CommandSpec("pane_next", "l", "pane_next", "next pane", terminal_dispatched=True),
    CommandSpec("activate", "enter", "activate", "inspect highlighted item"),
    CommandSpec("back", "escape", "back", "back"),
    CommandSpec("back_alias", "q", "back", "back", terminal_dispatched=True),
    CommandSpec("quit", "ctrl+c", "quit", "quit"),
    CommandSpec("quit_alias", "ctrl+q", "quit", "quit"),
    CommandSpec("steer", "slash", "steer", "steer review"),
    CommandSpec("review", "r", "review", "start a review"),
    CommandSpec("copy_review_context", "y", "handoff", "copy review context"),
    CommandSpec("open_command_palette", "ctrl+p", "command_palette", "command palette"),
    CommandSpec("open_command_palette_alias", ":", "command_palette", "command palette"),
    CommandSpec("help", "?", "help", "key help"),
    CommandSpec("leave_review", "L", "leave_review", "leave a review"),
    CommandSpec("batch", "b", "batch", "batch select"),
    CommandSpec("docs", "d", "docs", "update docs"),
    CommandSpec("open_browser", "o", "open_browser", "open"),
    CommandSpec("view_diff", "v", "view_diff", "diff"),
    CommandSpec("comment", "c", "comment", "comment", mutating=True),
    CommandSpec("approve_or_land", "a", "merge", "approve+queue", mutating=True),
    CommandSpec("land_batch", "A", "land_batch", "land batch", mutating=True),
    CommandSpec("agents", "w", "agents", "watch batches"),
    CommandSpec("merge_now", "m", "merge_now", "merge now", mutating=True),
    CommandSpec("reject", "x", "reject", "reject", mutating=True),
    CommandSpec("update_branch", "u", "update_branch", "update clean branch", mutating=True),
    CommandSpec("select_mechanical", "U", "select_mechanical", "select mechanical"),
    CommandSpec("resolve_duplicates", "M", "resolve_cluster", "resolve dupes", mutating=True),
    CommandSpec("filter", "f", "filter", "filter"),
    CommandSpec("hive", "H", "hive", "ask hive"),
    CommandSpec("refresh", "R", "refresh", "refresh"),
)


def command_registry() -> tuple[CommandSpec, ...]:
    return COMMANDS


def bindings_for(_owner) -> list[Binding]:
    return [Binding(command.key, command.action, command.label)
            for command in COMMANDS if command.key and not command.terminal_dispatched]


def back_bindings(dismiss_action: str) -> list[Binding]:
    """Project the semantic back keys onto a pushed screen's dismiss action."""
    return [Binding(command.key, dismiss_action, command.label)
            for command in COMMANDS if command.action == "back"]


# The key map, split by what a key costs you. Nothing on the first line
# changes anything on GitHub; everything on the second goes through the
# typed-number gate.
KEYS_READING = (
    " [b]r[/b] review [b]v[/b] diff [b]o[/b] open [b]h[/b] handoff"
    " [b]/[/b] steer [b]f[/b] filter [b]b[/b] batch [b]w[/b] watch batches [b]H[/b] hive"
    " [b]R[/b] refresh [b]q[/b]/Esc back"
)
KEYS_ACTING = (
    " [b]L[/b] leave review [b]a[/b] approve+queue [b]A[/b] land batch [b]m[/b] merge"
    " [b]u[/b] update clean branch [b]U[/b] select mechanical [b]x[/b] reject [b]M[/b] dupes"
)

# The bot whose pull requests can be classified as mechanical. The login is
# configurable because the Renovate installation differs per deployment: this
# organisation runs it as `app/mergeraptor`, and hard-coding one name is how a
# correct classifier silently matches nothing somewhere else.
RENOVATE_BOTS = frozenset(
    login.strip().lower()
    for login in os.environ.get(
        "BLUEFIN_REVIEW_RENOVATE_BOTS",
        "app/mergeraptor,app/renovate,renovate[bot],renovate-bot",
    ).split(",")
    if login.strip()
)

# The update types current policy already covers. A major update is a semantic
# decision about the dependency, so it never qualifies for a mechanical branch
# update no matter how green the branch is.
MECHANICAL_UPDATE_TYPES = frozenset({"digest", "pin", "patch", "minor"})

# A check that says anything else — running, queued, failed, absent — is not
# evidence that the branch is currently green.
MECHANICAL_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

# The live evidence the mechanical classifier consumes. `body` is Renovate's
# own update-type metadata; every other field is GitHub's own account of the
# pull request's state.
MECHANICAL_FIELDS = (
    "author,state,isDraft,mergeable,mergeStateStatus,body,statusCheckRollup"
)


def hive_api_base() -> str:
    """The hub's HTTP root, derived from the WebSocket URL the image owns.

    The hub URL is defined once, in the image's Hive entrypoint hook, which
    exports `HIVE_HUB` before this runs. It is never written down here: a
    second copy is how a deployment ends up consulting someone else's hub.
    """
    hub = os.environ.get("HIVE_HUB", "")
    if not hub.startswith(("wss://", "ws://", "https://", "http://")):
        return ""
    http = hub.replace("wss://", "https://").replace("ws://", "http://")
    return http[: -len("/contribute")] if http.endswith("/contribute") else http


# The order a maintainer wants, which is not the order the snapshot is written
# in. The generator ranks by how stuck a pull request is; a reviewer opening
# this dashboard wants the ones they can act on now — the merge-ready and the
# reviewable — above the ones waiting on their author or on better evidence.
# A queue that buries what you can land under sixty things you cannot is a
# queue you stop reading.
MAINTAINER_ORDER = [
    "ready-for-human-merge",
    "review",
    "resolve-conflicts",
    "fix-ci",
    "investigate",
]


def action_rank(action: str) -> int:
    try:
        return MAINTAINER_ORDER.index(action)
    except ValueError:
        return len(MAINTAINER_ORDER)


# The review engine. It produces a Review Draft and has no approve, merge,
# comment, or close path of its own, so running it can never mutate GitHub.
REVIEW_COMMAND = os.environ.get("BLUEFIN_REVIEW_COMMAND", "bluefin-review")
ACTIVE_BACKEND = os.environ.get("BLUEFIN_REVIEW_BACKEND", "goose")
if ACTIVE_BACKEND not in {"goose", "codex"}:
    raise RuntimeError(f"unsupported review backend: {ACTIVE_BACKEND}")

# bluefin-review's exit status for a review whose checks did not all return a
# verdict. 'goose review' exits 0 in that case and still prints a finding
# count, so the count would otherwise read as a clean review.
REVIEW_INCOMPLETE = 65

# How long a stopped review has to die politely before it is killed.
STOP_GRACE_SECONDS = 5.0

# The docs-update agent task is tracked work, not a silent stub; the
# handler below names the issue.
DOCS_UPDATE_ISSUE = "projectbluefin/review#134"


MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def reviewer_standing(association: str) -> str:
    """Maintainer or community, from GitHub's own author association.

    GitHub already decides this per review: OWNER, MEMBER and COLLABORATOR
    carry write access to the repository, everything else does not. Reading
    it off the review costs nothing, where asking the permissions API costs
    one round trip per reviewer per stop.
    """
    return "maintainer" if association in MAINTAINER_ASSOCIATIONS else "community"


def escape(text: str) -> str:
    """Make arbitrary text safe for the markup parser.

    Neither `rich.markup.escape` nor `textual.markup.escape` escapes a tag
    that starts with an uppercase letter -- their tag patterns only match
    lowercase -- but Textual's renderer consumes `[H]` and `[WIP]` all the
    same. So a pull request titled "[WIP] fix the thing" silently lost its
    prefix, and so did the "[H]" in this dashboard's own hints. Escape every
    opening bracket instead of trying to predict which ones the parser will
    claim.
    """
    return str(text).replace("\\", "\\\\").replace("[", "\\[")


def pr_url(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/pull/{number}"


def issue_url(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/issues/{number}"


def link(text: str, url: str) -> str:
    """Markup for a terminal hyperlink (OSC 8), with the text escaped.

    Everything shown here is somebody else's text — pull request titles carry
    `[skip ci]`, label names carry brackets — and the markup parser reads a
    bracket as a tag. Unescaped, `[review]` and `[skip ci]` were being
    silently eaten from the queue rows, so the action tag never appeared and
    titles quietly lost words. Escape at the point of display, once, in the
    helper that also makes the link.

    The URL is quoted because Textual's markup value parser stops at the
    colon in `https:` otherwise.
    """
    return f'[link="{url}"]{escape(text)}[/link]'


def gh(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout
    )


def bounded_detail(detail: str) -> str:
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", str(detail))
    return " ".join(detail.split())[:240]


def hive_token() -> str:
    """The hub bearer token: GH_TOKEN when exported, else the host's own gh
    login. The dashboard runs where the maintainer is already authed with
    gh; requiring a second, separately exported token is how a connected
    hub reads as unreachable. Read-only either way."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    try:
        result = gh("auth", "token", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def hive_get(path: str) -> hive_api.Result:
    """Read one hub endpoint. Read-only, and never fatal.

    Consulting Hive must not be able to break the dashboard. The result keeps
    routing, authentication, authorization, network, malformed-response, and
    server failures distinct without exposing credentials.
    """
    base = hive_api_base()
    token = hive_token()
    if not base:
        return hive_api.Result(False, "configuration", "not configured", {})
    return hive_api.request(f"{base}{path}", token, timeout=HIVE_TIMEOUT)


def trace(record: dict) -> None:
    """Append a JSON trace of a maintainer action for the feedback loop."""
    os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    with open(TRACE_PATH, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, separators=(",", ":")) + "\n")


def dependency_subject(title: str) -> str | None:
    """Normalise a title down to the dependency it updates (walker parity)."""
    s = title.lower()
    s = re.sub(r"^\w+(\([^)]*\))?:\s*", "", s)
    for pattern in (
        r"update module\s+(\S+)",
        r"update dependency\s+(\S+)",
        r"update\s+(\S+)\s+docker\s+(?:tag|digest)",
        r"update\s+(\S+)\s+action",
        r"update\s+(\S+)\s+digest",
        r"update\s+(\S+)\s+to\s+v?[\d.]",
    ):
        found = re.search(pattern, s)
        if found:
            return re.sub(r":[^:/]*$", "", found.group(1).strip())
    return None


def renovate_update_types(body: str) -> set[str]:
    """The update types Renovate declares in its own pull request body.

    Renovate writes one row per updated package into a `| Package | Update |
    Change |` table, and the Update cell carries the type it decided on.
    Reading that cell is not an inference from the title: it is the bot's own
    metadata about what it changed.
    """
    types: set[str] = set()
    columns: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            columns = []
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        lowered = [cell.lower() for cell in cells]
        if "update" in lowered and "package" in lowered:
            columns = lowered
            continue
        if not columns or set(line) <= set("|-: "):
            continue
        index = columns.index("update")
        if index < len(cells) and cells[index]:
            types.add(cells[index].lower())
    return types


def mechanical_reason(author: str, live: dict) -> str | None:
    """Why this branch is safe to *update*, or None when it is not.

    MECHANICAL describes exactly one operation — merging the base branch into
    a green, mergeable branch that is merely behind — and says nothing about
    whether the dependency change itself should be approved or merged. Every
    signal below is live GitHub evidence or Renovate's own metadata. A
    dependency-shaped title proves nothing and is deliberately not consulted:
    that heuristic is duplicate evidence, not a safety boundary.
    """
    if not live:
        return None
    login = (author or (live.get("author") or {}).get("login") or "").lower()
    if login not in RENOVATE_BOTS:
        return None
    if (live.get("state") or "OPEN").upper() != "OPEN":
        return None
    if live.get("isDraft"):
        return None
    if (live.get("mergeable") or "").upper() != "MERGEABLE":
        return None
    if (live.get("mergeStateStatus") or "").upper() != "BEHIND":
        return None
    checks = authoritative_checks(live)
    if not checks:
        return None
    for check in checks:
        outcome = str(check.get("conclusion") or check.get("state") or "").upper()
        if outcome not in MECHANICAL_CHECK_OK:
            return None
    types = renovate_update_types(live.get("body") or "")
    if not types or not types <= MECHANICAL_UPDATE_TYPES:
        return None
    kinds = "/".join(sorted(types))
    return f"{kinds} update by {login}, every check green, mergeable but behind"


def stop_style(action: str, mergeable: str, checks: str, review: str) -> str:
    """The colour a row is worth, from the snapshot's own state fields.

    A hundred rows of identical grey is a queue you read linearly. These
    states are already in every snapshot item, so colour costs nothing and
    turns the list into something scannable: what is ready, what is merely
    stuck behind its own branch, and what nobody can act on yet.
    """
    if mergeable == "dirty":
        return "red"
    if checks == "failure":
        return "yellow"
    if action == "ready-for-human-merge":
        return "bold green"
    if action == "review" or review == "approved":
        return "cyan"
    if action == "investigate" or checks == "unknown":
        return "grey62"
    return ""


def ci_marker(checks: str) -> str:
    """Carry the snapshot's CI state as text, not colour alone."""
    return {
        "success": "✓ CI GREEN",
        "failure": "✗ CI FAILED",
        "pending": "… CI PENDING",
        "unknown": "? CI UNKNOWN",
    }.get(checks, "? CI UNKNOWN")


def authoritative_checks(live: dict) -> list[dict]:
    """Return the latest run for each stable current-head check context.

    GitHub's pull-request ``statusCheckRollup`` is fetched together with
    ``headRefOid``, so every entry belongs to that exact current head. Reruns
    may leave older entries in the rollup; a check-run context is its workflow
    plus job name, while a commit status context is its context string.
    """
    latest: dict[tuple[str, ...], tuple[tuple[str, str, int], dict]] = {}
    ungrouped: list[dict] = []
    for index, check in enumerate(live.get("statusCheckRollup") or []):
        typename = str(check.get("__typename") or "")
        name = str(check.get("name") or "")
        context = str(check.get("context") or "")
        if typename == "CheckRun" or name:
            key = ("check-run", str(check.get("workflowName") or ""), name)
        elif typename == "StatusContext" or context:
            key = ("status-context", context)
        else:
            ungrouped.append(check)
            continue
        rank = (
            str(check.get("startedAt") or ""),
            str(check.get("completedAt") or ""),
            index,
        )
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, check)
    return [item[1] for item in sorted(latest.values(), key=lambda item: item[0])] + ungrouped


def effective_check_state(snapshot: str, live: dict) -> str:
    """Prefer fetched check evidence, retaining the snapshot when absent."""
    checks = authoritative_checks(live)
    if not checks:
        return snapshot or "unknown"
    outcomes = [check.get("conclusion") or check.get("state") or "PENDING" for check in checks]
    if any(outcome in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for outcome in outcomes):
        return "failure"
    if any(outcome not in ("SUCCESS", "NEUTRAL", "SKIPPED") for outcome in outcomes):
        return "pending"
    return "success"


# The merge queue's segments, in the order a maintainer drains them, with the
# same colours the rows use so the bar and the list agree.
QUEUE_SEGMENTS = [
    ("queued", "queued", "green"),
    ("ready", "ready", "bold green"),
    ("review", "review", "cyan"),
    ("ci", "CI", "yellow"),
    ("conflicts", "conflicts", "red"),
    ("unclear", "unclear", "grey62"),
]


def classify_queue_item(item: dict) -> str:
    """Which segment of a repository's merge queue this pull request sits in.

    First match wins, and the order is the maintainer's: something already
    handed to the sweep is queued no matter what else is true of it, and a
    conflict outranks a failing check because it blocks the check from
    meaning anything.
    """
    if item.get("mergeable_state") == "dirty":
        return "conflicts"
    if item.get("check_state") == "failure":
        return "ci"
    if "lgtm" in (item.get("labels") or []):
        return "queued"
    if item.get("recommended_action") == "ready-for-human-merge":
        return "ready"
    if item.get("recommended_action") == "review":
        return "review"
    return "unclear"


def meter_bar(counts: dict[str, int], width: int = 24) -> str:
    """A stacked bar of one repository's merge queue.

    Every non-empty segment gets at least one cell: a single pull request
    waiting on the sweep is exactly the thing a maintainer needs to see, and
    proportional rounding is what would hide it.
    """
    total = sum(counts.values())
    if not total:
        return ""
    cells: list[str] = []
    for key, _, colour in QUEUE_SEGMENTS:
        count = counts.get(key, 0)
        if not count:
            continue
        size = max(1, round(count / total * width))
        cells.append(f"[{colour}]{'█' * size}[/{colour}]")
    return "".join(cells)


@dataclass
class QueueFilters:
    """Which of the snapshot's items reach the dashboard.

    The launcher passes these straight through, so 'just review-queue --repo
    bluefin' narrows the queue without a second surface to learn.
    """

    action: str = ""
    repository: str = ""
    live_repository: str = ""
    url: str = QUEUE_URL

    @property
    def live(self) -> bool:
        return bool(self.live_repository)

    def wants(self, item: dict) -> bool:
        if self.action and item.get("recommended_action", "") != self.action:
            return False
        if self.repository:
            full = item.get("repository", "")
            if full != self.repository and full.split("/")[-1] != self.repository:
                return False
        return True


@dataclass
class Stop:
    repository: str
    number: int
    action: str
    title: str
    author: str = ""
    mergeable_state: str = ""
    check_state: str = ""
    review_state: str = ""
    selected: bool = False
    failure: str = ""
    failure_command: str = ""
    failure_argv: list[str] = field(default_factory=list)
    failure_checks: str = ""
    failure_branch: str = ""
    live: dict = field(default_factory=dict)
    overlap: dict = field(default_factory=dict)
    review_result: ReviewResult | None = None

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def batchable(self) -> bool:
        return dependency_subject(self.title) is not None

    @property
    def mechanical(self) -> str | None:
        """The branch-update reason, from live evidence only."""
        return mechanical_reason(self.author, self.live)


def live_review_context(live: dict, *, title: str = "") -> dict:
    checks = authoritative_checks(live)
    outcomes = [
        str(item.get("conclusion") or item.get("state") or "PENDING").upper()
        for item in checks
    ]
    failed = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    passed = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    if any(outcome in failed for outcome in outcomes):
        ci = "failure"
    elif outcomes and all(outcome in passed for outcome in outcomes):
        ci = "success"
    elif outcomes:
        ci = "pending"
    else:
        ci = "unknown"
    head_sha = str(live.get("headRefOid") or "")
    return {
        "ci": ci,
        "mergeable": live.get("mergeable") or "?",
        "merge_state": live.get("mergeStateStatus") or "?",
        "head": (head_sha or "?")[:12],
        "head_sha": head_sha,
        "title": title or live.get("title") or "",
        "draft": live.get("isDraft", "?"),
    }


def live_review_verification(live: dict) -> list[dict]:
    records = []
    passed = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    failed = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    for index, item in enumerate(authoritative_checks(live), start=1):
        outcome = str(item.get("conclusion") or item.get("state") or "PENDING").upper()
        state = (
            "verified"
            if outcome in passed
            else "unverified"
            if outcome in failed
            else "pending"
        )
        records.append(
            {
                "name": item.get("name")
                or item.get("context")
                or f"CI check {index}",
                "state": state,
                "evidence": outcome,
                "source": "github",
            }
        )
    return records


class BatchPlanScreen(ModalScreen[bool]):
    """The batch gate: the whole plan on one screen, one Enter to dispatch.

    The typed-number gate earns its ceremony on a single irreversible
    command. On a batch the maintainer has already reviewed row by row —
    the selection was the review — typing the count back teaches nothing
    and only slows the loop. This gate is proportionate: every pull request
    and the exact agent command are shown, Enter dispatches, Esc aborts.
    There is no default and no timer; dispatch is still a decision, not a
    typing exercise.
    """

    BINDINGS = [
        Binding("enter", "dispatch", "dispatch the batch"),
        *back_bindings("dismiss(False)"),
    ]

    def __init__(self, task: "landing.LandingTask") -> None:
        super().__init__()
        self.plan = task

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(
                f"one agent will land {len(self.plan.stops)} pull requests:",
                id="confirm-heading",
            )
            for stop in self.plan.stops:
                yield Static(
                    f"  {stop.key} — {stop.title}", classes="confirm-command"
                )
            yield Static(" ".join(self.plan.command), classes="confirm-command")
            yield Label("[enter] dispatch · [esc] abort")

    def action_dispatch(self) -> None:
        self.dismiss(True)


# Per-state presentation for the batch queue. The printed state word stays
# the primary carrier of the fact; the glyph adds a distinct *shape* and the
# style adds colour on top, so no fact exists only as colour (the design
# rule in docs/skills/review-dashboard.md). Deuteranopia merges red and
# green, so shapes differ between states and the terminal states also read
# bold on a muted fill rather than relying on hue.
# Verified against the pinned Textual (8.2.8): markup spans resolve $-theme
# variables through the active app's stylesheet (Style.parse falls back to
# app.stylesheet.parse_style), and padding spaces inside a span keep its
# background — which is what turns a batch header into a full-width bar.
LANDING_STATE_STYLES: dict[str, tuple[str, str]] = {
    "waiting": ("◌", "dim"),
    "diagnosing": ("◐", "cyan"),
    "fixing": ("◐", "cyan"),
    "waiting-ci": ("◔", "$text-warning"),
    "merging": ("▶", "bold $text-primary"),
    "awaiting-stable": ("◆", "$text-accent"),
    "merged": ("✓", "bold $text-success on $success-muted"),
    "blocked": ("■", "$text-warning on $warning-muted"),
    "failed": ("✗", "bold $text-error on $error-muted"),
}


def batch_bar_style(state: str) -> str:
    """The header bar's style for a batch-level state. Every header is a
    filled bar, its fill naming the state with the theme's own
    text-on-muted pairing so the text stays legible on it."""
    if state == "running":
        return "bold $text-primary on $primary-muted"
    if state == "queued":
        return "bold $text-warning on $warning-muted"
    if state == "exited 0":
        return "bold $text-success on $success-muted"
    return "bold $text-error on $error-muted"


class LandingScreen(Screen):
    """The live batch queue: every dispatched batch, its agent, the per-PR
    state the agent reports, and what Hive is doing alongside.

    Status comes from the agent's JSONL report file, polled on a timer —
    never scraped from its prose. The screen is read-only except [x], which
    stops the running agent's process group the same way a review stop does.

    The presentation is a cabinet of framed panels in the Midnight Commander
    mold: a title bar, the BATCHES panel where each header is a state-filled
    bar and each pull request carries its state as word, glyph, and colour
    together, the HIVE line, and the AGENT LOG.
    """

    CSS = """
    #landing-status {
        height: 1; background: $secondary; color: $text; text-style: bold;
    }
    #landing-rows {
        border: round $secondary; height: auto; padding: 0 1;
    }
    #landing-hive {
        border: round $secondary; height: 3; padding: 0 1;
        color: $text-secondary;
    }
    #landing-log { border: round $secondary; }
    """

    BINDINGS = [
        *back_bindings("dismiss(None)"),
        Binding("x", "stop_agent", "stop the running agent"),
    ]

    def __init__(self, dashboard: "ReviewDashboard") -> None:
        super().__init__()
        self.dashboard = dashboard

    def compose(self) -> ComposeResult:
        yield Static("batch queue", id="landing-status")
        yield Static("", id="landing-rows")
        yield Static("", id="landing-hive")
        yield RichLog(highlight=False, markup=False, wrap=True, id="landing-log")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#landing-rows", Static).border_title = "BATCHES"
        self.query_one("#landing-hive", Static).border_title = "HIVE"
        self.query_one("#landing-log", RichLog).border_title = "AGENT LOG"
        self.poll()
        self.set_interval(2.0, self.poll)

    def poll(self) -> None:
        rows = self.query_one("#landing-rows", Static)
        width = rows.content_region.width
        lines: list[str] = []
        for task in self.dashboard.landing_queue:
            if task.returncode is None:
                state = "running" if task.running else "queued"
            else:
                state = f"exited {task.returncode}"
            header = f" batch {task.task_id} — {state}"
            if task.running:
                # A wait that names its target is still invisible if the
                # row cannot say how long the agent has been silent: the
                # report file's mtime is the heartbeat (#291).
                header += f" · {landing.report_age(task.status_path)}"
            # ljust(0) is a no-op, so the first pre-layout poll renders a
            # text-wide bar and the next tick paints it to the panel's edge.
            lines.append(f"[{batch_bar_style(state)}]{header.ljust(width)}[/]")
            events = landing.parse_status(task.status_path)
            done = events.get("", {})
            for stop in task.stops:
                event = events.get(stop.key, {})
                note = event.get("note", "")
                # The state string is agent-sourced JSONL: coerce it (a
                # non-string would raise on the dict lookup) and escape it
                # before it meets the markup parser. The styled branch only
                # fires on this module's own fixed literal keys, so the
                # escape belongs on the fallback alone.
                mark = str(event.get("state", "waiting"))
                glyph, style = LANDING_STATE_STYLES.get(mark, ("?", ""))
                if style:
                    badge = f"[{style}]{glyph} {mark}[/]"
                else:
                    badge = f"{glyph} {escape(mark)}"
                lines.append(
                    f"  {link(stop.key, pr_url(stop.repository, stop.number))}"
                    f"  {badge}"
                    + (f" — {escape(str(note))}" if note else "")
                )
            if done:
                note = escape(str(done.get("note", "")))
                lines.append(
                    "  [bold $text-success]✔ done[/]"
                    + (f" — {note}" if note else "")
                )
        rows.update("\n".join(lines))
        self.query_one("#landing-hive", Static).update(
            f" Hive: {escape(self.dashboard.hive_state or 'asking…')}"
        )
        task = self.dashboard.landing_queue[-1]
        try:
            with open(task.log_path, encoding="utf-8") as handle:
                tail = handle.readlines()[-200:]
        except OSError:
            tail = []
        log = self.query_one("#landing-log", RichLog)
        log.clear()
        for line in tail:
            log.write(line.rstrip("\n"))
        running = sum(1 for t in self.dashboard.landing_queue if t.running)
        self.query_one("#landing-status", Static).update(
            f" batch queue: {len(self.dashboard.landing_queue)} batches, "
            f"{running} running · [x] stop · [esc] back"
        )

    def action_stop_agent(self) -> None:
        task = next(
            (t for t in self.dashboard.landing_queue if t.running), None
        )
        if task is None or task.process is None:
            self.app.notify("no agent is running.", severity="warning")
            return
        try:
            os.killpg(os.getpgid(task.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError) as error:
            self.app.notify(f"stop failed: {error}", severity="error")


class ConfirmMutation(ModalScreen[bool]):
    """The single mutation gate: show the exact operations, require the typed
    pull request number. Empty, wrong, or Esc aborts; there is no y/yes and
    no timeout.

    One decision gates one sequence. Queueing is one authenticated Hive request,
    and reject is a comment plus a close: splitting either into two gates asks
    a maintainer to confirm the same decision twice, which trains them to type
    the number without reading it. Every command that will run is shown here,
    before the one gate.
    """

    BINDINGS = back_bindings("dismiss(False)")

    def __init__(self, commands: list[list[str]], expected: str) -> None:
        super().__init__()
        self.commands = [list(command) for command in commands]
        self.expected = expected

    @property
    def command(self) -> list[str]:
        return self.commands[0]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("will run:", id="confirm-heading")
            for index, command in enumerate(self.commands):
                yield Static(" ".join(command), classes="confirm-command",
                             id=f"confirm-command-{index}")
            yield Label(
                f"type the pull request number ({self.expected}) to run it; "
                "empty or Esc aborts"
            )
            yield Input(placeholder=self.expected, id="confirm-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() == self.expected)


class MergeRecovery(ModalScreen[str | None]):
    """A merge that failed, and the ways out of it.

    GitHub refuses a merge for reasons that are mostly fixable — the branch is
    behind, a required approval is missing, checks have not finished — and the
    old behaviour was to print the refusal and drop it. In a batch that is
    worse than useless: the maintainer is several confirmations further on by
    the time they read it. The failure is offered as a choice instead, and
    whatever is not fixed now stays selected so it comes back with the batch.
    """

    BINDINGS = back_bindings("dismiss(None)")

    def __init__(self, stop: Stop, message: str) -> None:
        super().__init__()
        self.stop_record = stop
        self.message = message
        self.choices = self.offers(stop, message)

    @staticmethod
    def offers(stop: Stop, message: str) -> list[tuple[str, str]]:
        """What is worth offering, given why GitHub said no."""
        state = str(stop.live.get("mergeStateStatus", "")).upper()
        text = message.upper()
        choices: list[tuple[str, str]] = []
        if state == "BEHIND" or "NOT UP TO DATE" in text or "BEHIND" in text:
            choices.append(("update", "update the branch, then merge again"))
        if state == "BLOCKED" or "REVIEW" in text or "REQUIRED" in text:
            choices.append(("queue", "approve and queue it for the sweep instead"))
        if state == "DIRTY" or "CONFLICT" in text:
            choices.append(("handoff", "exceptional manual handoff — conflict; no bypass"))
        choices.append(("retry", "try the merge again"))
        choices.append(("skip", "leave it queued and move on"))
        return choices

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"{self.stop_record.key} did not merge:")
            yield Static(
                "\n".join(
                    (
                        f"command  {self.stop_record.failure_command or 'gh pr merge'}",
                        f"error    {self.message}",
                        f"checks   {self.stop_record.failure_checks or 'unknown'}",
                        f"branch   {self.stop_record.failure_branch or 'unknown'}",
                    )
                ),
                classes="confirm-command",
            )
            yield Label("")
            for index, (_, description) in enumerate(self.choices, start=1):
                yield Label(f"  [{index}] {description}")
            yield Label("")
            yield Label("esc keeps it in the queue")

    def on_key(self, event) -> None:
        if event.key.isdigit():
            index = int(event.key) - 1
            if 0 <= index < len(self.choices):
                self.dismiss(self.choices[index][0])


class ReviewVerdict(ModalScreen[str | None]):
    """Pick what kind of review to leave. One keystroke, Esc aborts."""

    BINDINGS = back_bindings("dismiss(None)")

    CHOICES = [
        ("approve", "approve"),
        ("request-changes", "request changes"),
        ("comment", "comment (no verdict)"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="label-box"):
            yield Label("leave a review:")
            for index, (_, description) in enumerate(self.CHOICES, start=1):
                yield Label(f"  [{index}] {description}")

    def on_key(self, event) -> None:
        if event.key.isdigit():
            index = int(event.key) - 1
            if 0 <= index < len(self.CHOICES):
                self.dismiss(self.CHOICES[index][0])


class ReviewBodyPreview(ModalScreen[bool | None]):
    BINDINGS = [
        Binding("ctrl+s", "submit", "submit review", priority=True),
        *back_bindings("dismiss(None)"),
    ]

    def __init__(self, body: str, command: list[str]) -> None:
        super().__init__()
        self.body = body
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("exact GitHub Markdown:")
            yield Static(self.body, markup=False, id="review-body-preview")
            yield Label("exact command:")
            yield Static(" ".join(self.command), id="review-command-preview")
            yield Button("Submit review", id="review-preview-submit", variant="primary")
            yield Static("[ctrl-s] submit · [esc] edit", markup=False)

    def action_submit(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "review-preview-submit":
            self.action_submit()


class CommentPreview(ModalScreen[bool | None]):
    BINDINGS = [
        Binding("ctrl+s", "submit", "submit comment", priority=True),
        *back_bindings("dismiss(None)"),
    ]

    def __init__(self, body: str, command: list[str]) -> None:
        super().__init__()
        self.body = body
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("exact GitHub Markdown:")
            yield Static(self.body, markup=False, id="comment-preview-body")
            yield Label("exact command:")
            yield Static(" ".join(self.command), id="comment-preview-command")
            yield Button("Submit comment", id="comment-preview-submit", variant="primary")
            yield Static("[ctrl-s] submit · [esc] edit", markup=False)

    def action_submit(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "comment-preview-submit":
            self.action_submit()


class ReviewBody(ModalScreen[str | None]):
    """Editable review body; generation is an optional maintainer action."""

    BINDINGS = [
        Binding("ctrl+g", "generate", "generate", priority=True),
        Binding("ctrl+e", "edit", "edit", priority=True),
        Binding("ctrl+p", "preview", "preview", priority=True),
        Binding("ctrl+shift+k", "clear", "clear", priority=True),
        Binding("ctrl+s", "submit", "submit", priority=True),
        *back_bindings("cancel"),
    ]

    def __init__(self, stop: Stop, verdict: str) -> None:
        super().__init__()
        self.stop_record = stop
        self.verdict = verdict
        self.draft_provenance: dict = {}
        self.body_file: str | None = None
        self.previewed_body: str | None = None

    def compose(self) -> ComposeResult:
        optional = " (empty is allowed for an approval)" if self.verdict == "approve" else ""
        with Vertical(id="confirm-box"):
            yield Label(f"{self.verdict} — say why{optional}:")
            yield TextArea(id="review-body-editor")
            yield Static(
                "[ctrl-g] generate · [ctrl-e] edit · [ctrl-p] preview · "
                "[ctrl-shift-k] clear · [ctrl-s] submit",
                markup=False,
                id="review-body-shortcuts",
            )
            with Horizontal(id="review-body-actions"):
                yield Button("Generate", id="review-body-generate")
                yield Button("Edit", id="review-body-edit")
                yield Button("Preview", id="review-body-preview")
                yield Button("Clear", id="review-body-clear")
                yield Button("Submit", id="review-body-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def action_edit(self) -> None:
        self.query_one(TextArea).focus()

    def action_cancel(self) -> None:
        self.cleanup()
        self.dismiss(None)

    def action_clear(self) -> None:
        self.query_one(TextArea).text = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "review-body-generate": self.action_generate,
            "review-body-edit": self.action_edit,
            "review-body-preview": self.action_preview,
            "review-body-clear": self.action_clear,
            "review-body-submit": self.action_submit,
        }
        action = actions.get(event.button.id)
        if action:
            action()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "review-body-editor":
            self._invalidate_preview()

    def action_generate(self) -> None:
        result = self.stop_record.review_result
        if result is None or result.state not in {"complete", "findings"}:
            self.notify("trustworthy completed review evidence is unavailable", severity="warning")
            return
        try:
            owner, repository = self.stop_record.repository.split("/", 1)
            request = ReviewRequest(
                owner, repository, self.stop_record.number,
                str(self.stop_record.live["baseRefOid"]), str(self.stop_record.live["headRefOid"]),
                actor="maintainer", tenant="review", generated_at="dashboard",
            )
            registry = HarnessRegistry()
            registry.register(GooseHarness())
            registry.register(CodexHarness(availability=CodexHarness.probe()))
            adapter = registry.require_ready(ACTIVE_BACKEND)
            if not adapter.capabilities.body_drafting:
                raise RuntimeError(f"{ACTIVE_BACKEND} unavailable: UNSUPPORTED_CAPABILITY")
            draft = adapter.draft(
                DraftRequest(request, self.verdict, result, live_review_context(self.stop_record.live))
            )
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as error:
            self.notify(f"draft unavailable: {error}", severity="warning")
            return
        if draft.state is not DraftState.COMPLETE or not draft.markdown:
            self.notify("draft unavailable: evidence did not produce review prose", severity="warning")
            return
        self.draft_provenance = dict(getattr(draft, "provenance", {}))
        self.query_one(TextArea).text = draft.markdown

    def _command(self, body_file: str) -> list[str]:
        return ["gh", "pr", "review", str(self.stop_record.number), "--repo",
                self.stop_record.repository, f"--{self.verdict}", "--body-file", body_file]

    def action_preview(self) -> None:
        body = self.query_one(TextArea).text or "Reviewed."
        if not self._validate_body(body):
            return
        path = self._prepare_body_file(body)
        self.previewed_body = body
        self.app.push_screen(
            ReviewBodyPreview(body, self._command(path)),
            lambda submit: self.action_submit() if submit else None,
        )

    def _validate_body(self, body: str) -> bool:
        if len(body) <= MAX_REVIEW_BODY_CHARS:
            return True
        self.notify(
            f"review body is too long ({len(body)}/{MAX_REVIEW_BODY_CHARS} characters); nothing was submitted",
            severity="warning",
        )
        return False

    def _invalidate_preview(self) -> None:
        self.previewed_body = None
        if self.body_file:
            try:
                os.unlink(self.body_file)
            except FileNotFoundError:
                pass
            self.body_file = None

    def _prepare_body_file(self, body: str) -> str:
        import tempfile
        if self.body_file:
            try:
                os.unlink(self.body_file)
            except FileNotFoundError:
                pass
        os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f"review-{self.stop_record.number}-",
            suffix=".md", dir=os.path.dirname(TRACE_PATH), delete=False,
        )
        with handle:
            handle.write(body or "Reviewed.")
        self.body_file = handle.name
        return handle.name

    def action_submit(self) -> None:
        raw_body = self.query_one(TextArea).text
        if not raw_body and self.verdict != "approve":
            self.notify(f"{self.verdict} needs a reason; nothing was submitted.", severity="warning")
            return
        body = raw_body or "Reviewed."
        if not self._validate_body(body):
            return
        if self.previewed_body != body or not self.body_file:
            self.notify("preview the exact review body before submitting", severity="warning")
            return
        self.dismiss((body, self.body_file))

    def cleanup(self) -> None:
        if self.body_file:
            try:
                os.unlink(self.body_file)
            except FileNotFoundError:
                pass
            self.body_file = None
        self.previewed_body = None


class DiffScreen(ModalScreen[None]):
    """The diff, in colour, scrollable, and whole.

    The old viewer pasted `gh pr diff` into the evidence pane as plain text,
    cut at 20 000 characters with no indication it had been cut. On a real
    pull request that is a wall of grey in which `+` and `-` are one character
    of difference, and the part you needed was as likely to be past the cut as
    not. Reading the diff is the review, so it gets the screen, Pygments'
    diff lexer, and every byte GitHub returned.
    """

    BINDINGS = back_bindings("dismiss") + [
        Binding("]", "next_page", "next diff page"),
        Binding("[", "previous_page", "previous diff page"),
    ]

    # Rich renders the whole diff before Textual paints it, so an enormous
    # one is a visible stall. Cut with the size named, never silently.
    MAX_CHARS = 400_000

    def __init__(self, stop: Stop) -> None:
        super().__init__()
        self.stop_record = stop
        # What is on screen right now, so the rendering can be inspected
        # without reaching through Textual's internal wrapping.
        self.rendered: Syntax | None = None
        self.pages: list[str] = []
        self.page_index = 0
        self.state = "loading"
        self.request_generation = 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def compose(self) -> ComposeResult:
        stop = self.stop_record
        live = stop.live or {}
        size = (
            f"+{live.get('additions', '?')} -{live.get('deletions', '?')} "
            f"across {live.get('changedFiles', '?')} files"
        )
        yield Static(
            f" {link(stop.key, pr_url(stop.repository, stop.number))} — "
            f"{escape(stop.title[:70])}  ({size})  {escape('[escape]')} closes",
            id="diff-header",
        )
        with ScrollableContainer(id="diff-scroll"):
            yield Static("loading diff…", id="diff-body")
        yield Footer()

    def on_mount(self) -> None:
        self.load_diff()

    def load_diff(self) -> None:
        self.request_generation += 1
        generation = self.request_generation
        self.fetch_diff(generation)

    @work(thread=True)
    def fetch_diff(self, generation: int) -> None:
        stop = self.stop_record
        result = gh("pr", "diff", str(stop.number), "--repo", stop.repository)
        if result.returncode == 0:
            self.app.call_from_thread(self.render_diff_result, generation, result.stdout)
        else:
            self.app.call_from_thread(
                self.render_diff_error_result,
                generation,
                result.stderr.strip() or f"exit {result.returncode}",
            )

    def render_diff_result(self, generation: int, text: str) -> None:
        if generation != self.request_generation:
            return
        self.render_diff(text)

    def render_diff_error_result(self, generation: int, message: str) -> None:
        if generation != self.request_generation:
            return
        self.render_diff_error(message)

    def render_diff(self, text: str) -> None:
        body = self.query_one("#diff-body", Static)
        if not text.strip():
            self.state = "success"
            self.pages = []
            self.rendered = None
            body.update("(empty diff)")
            return
        self.state = "success"
        self.pages = [text[index:index + self.MAX_CHARS] for index in range(0, len(text), self.MAX_CHARS)]
        self.page_index = 0
        self.render_page()

    def render_page(self) -> None:
        body = self.query_one("#diff-body", Static)
        text = self.pages[self.page_index]
        page_note = f"page {self.page_index + 1}/{len(self.pages)} · [ and ] navigate · [o] optional browser escape\n\n"
        # 'ansi_dark' resolves to the terminal's own palette, so the diff
        # stays legible in whatever theme the maintainer actually uses
        # instead of assuming a dark background.
        self.rendered = Syntax(
            page_note + text, "diff", theme="ansi_dark", word_wrap=False
        )
        body.update(self.rendered)

    def render_diff_error(self, message: str) -> None:
        self.state = "error"
        self.pages = []
        self.rendered = None
        self.query_one("#diff-body", Static).update(f"ERROR loading diff: {escape(message)}")

    def action_next_page(self) -> None:
        if self.page_index + 1 < len(self.pages):
            self.page_index += 1
            self.render_page()

    def action_previous_page(self) -> None:
        if self.page_index:
            self.page_index -= 1
            self.render_page()


class HarnessTakeoff(ModalScreen[str | None]):
    """One explicit maintainer choice before a selected harness starts."""

    BINDINGS = back_bindings("dismiss(None)")

    def __init__(self, options: list[HarnessOption], initial: HarnessOption | None,
                 initial_preference: Preference | None = None) -> None:
        super().__init__()
        self.options = options
        self.initial = initial
        self.initial_preference = initial_preference

    def compose(self) -> ComposeResult:
        with Vertical(id="takeoff-box"):
            yield Label("Select review harness — Start runs only after confirmation")
            yield Select(
                [(f"{option.harness.branding.terminal_badge} {option.harness.branding.display_name} · "
                  f"{option.status} · {self._model(option)} / {self._effort(option)}",
                  option.harness.branding.harness_id) for option in self.options],
                value=(self.initial.harness.branding.harness_id if self.initial else Select.BLANK),
                id="takeoff-select",
            )
            yield Button("Start", id="takeoff-start", variant="primary")
            yield Button("Diagnostics", id="takeoff-diagnostics")

    def _model(self, option: HarnessOption) -> str:
        if self.initial_preference and self.initial_preference.harness_id == option.harness.branding.harness_id:
            return self.initial_preference.model
        return option.discovery.model

    def _effort(self, option: HarnessOption) -> str:
        if self.initial_preference and self.initial_preference.harness_id == option.harness.branding.harness_id:
            return self.initial_preference.effort
        return "low"

    def on_mount(self) -> None:
        self.query_one("#takeoff-select", Select).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "takeoff-start":
            option = self.selected_option()
            if option is None:
                self.dismiss(None)
                return
            preference = self.initial_preference
            if preference is None or preference.harness_id != option.harness.branding.harness_id:
                preference = Preference(option.harness.branding.harness_id,
                                        option.discovery.model, "low")
            self.dismiss(preference)
        elif event.button.id == "takeoff-diagnostics":
            option = self.selected_option()
            if option:
                self.notify(
                    f"{option.harness.branding.display_name}: {option.status}; "
                    f"auth={option.discovery.auth}; model={option.discovery.model}; "
                    f"effort={option.discovery.reasoning}"
                )

    def selected_option(self) -> HarnessOption | None:
        value = self.query_one("#takeoff-select", Select).value
        return next((option for option in self.options
                     if option.harness.branding.harness_id == value), None)


class ReviewScreen(Screen):
    """One Goose review, streamed live.

    The review is the reason this tool exists, so it gets the whole screen and
    reports its own outcome. ``bluefin-review`` distinguishes a review that
    completed from one whose checks never returned a verdict, and that
    distinction is carried all the way to the status line here: a review that
    did not finish must never be mistaken for a clean one.
    """

    BINDINGS = [
        *back_bindings("close"),
        Binding("x", "stop", "stop review"),
        Binding("L", "leave_review", "leave a review"),
        Binding("a", "queue", "approve and queue"),
        Binding("m", "merge_now", "merge now"),
        Binding("u", "update_branch", "update clean branch"),
        Binding("e", "toggle_evidence", "evidence"),
        Binding("r", "toggle_raw_transcript", "raw transcript"),
    ]

    def __init__(self, stop: Stop, steer: str = "", selection: Preference | None = None) -> None:
        super().__init__()
        self.stop_record = stop
        self.steer = steer
        self.selection = selection or Preference(ACTIVE_BACKEND, "gpt-5.6-luna", "low")
        self.process: subprocess.Popen | None = None
        self.finished = False
        self.stop_requested = False
        self.started = time.monotonic()
        self.output: list[str] = []
        # The card is a point-in-time record of the evidence the maintainer
        # saw when the review started. The dashboard's background workers
        # keep rewriting stop.live/stop.overlap while the review runs, so
        # reading them at finish would mix a fresh fetch into a completed
        # transcript (#339).
        self.live_snapshot = copy.deepcopy(stop.live)
        self.overlap_snapshot = copy.deepcopy(stop.overlap)

    def compose(self) -> ComposeResult:
        stop = self.stop_record
        yield Header(show_clock=True)
        yield Static(
            f" reviewing {link(stop.key, pr_url(stop.repository, stop.number))}"
            f" — {ACTIVE_BACKEND} starting…"
            + (f"  steer: {escape(self.steer)}" if self.steer else ""),
            id="review-status",
        )
        yield Static("building decision card…", id="review-card")
        yield Static("", id="review-evidence", classes="hidden")
        yield RichLog(
            highlight=False, markup=False, max_lines=200, wrap=True,
            id="review-log", classes="hidden",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#review-status", Static).add_class("running")
        self.run_review()

    @work(thread=True)
    def run_review(self) -> None:
        stop = self.stop_record
        if ACTIVE_BACKEND == "codex":
            base_sha = str(self.live_snapshot.get("baseRefOid") or "")
            head_sha = str(self.live_snapshot.get("headRefOid") or "")
            if (len(base_sha) != 40 or len(head_sha) != 40 or
                    any(char not in "0123456789abcdef" for char in (base_sha + head_sha).lower())):
                self.app.call_from_thread(
                    self.finish, None,
                    "Codex unavailable: exact PR base/head is unavailable",
                )
                return
            owner, repository = stop.repository.split("/", 1)
            binding = ReviewRequest(
                owner, repository, stop.number, base_sha, head_sha,
                actor="maintainer", tenant="review", generated_at="dashboard",
            )
            adapter = CodexHarness(availability=CodexHarness.probe())
            if adapter.availability is not Availability.READY:
                self.app.call_from_thread(
                    self.finish, None,
                    f"Codex unavailable: {adapter.availability.value}",
                )
                return
            command = adapter.command(
                binding, prompt="Produce the ReviewResult JSON.",
                model=self.selection.model, effort=self.selection.effort,
                steer=self.steer,
            )
        else:
            command = [REVIEW_COMMAND, "pr", stop.repository, str(stop.number)]
        # Maintainer steering rides the documented additive seam: it is added
        # to the review's instructions, never a replacement for the doctrine.
        environment = dict(os.environ)
        if ACTIVE_BACKEND == "codex":
            environment.pop("GH_TOKEN", None)
            environment.pop("GITHUB_TOKEN", None)
        if self.steer:
            environment["BLUEFIN_REVIEW_STEER"] = self.steer
        else:
            environment.pop("BLUEFIN_REVIEW_STEER", None)
        if self.stop_requested:
            self.app.call_from_thread(self.finish, None, "")
            return
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
                # Its own process group. A review is a shell that runs Goose,
                # which runs a subprocess per check: signalling only the shell
                # leaves those children alive holding the pipe open, and the
                # read loop below would never end.
                start_new_session=True,
            )
        except OSError as error:
            self.app.call_from_thread(self.finish, None, str(error))
            return
        self.process = process
        if self.stop_requested:
            if self.signal_group(signal.SIGTERM):
                self.app.call_from_thread(self.schedule_stop_escalation)
            else:
                self.app.call_from_thread(self.finish, process.poll(), "")
                return
        if not self.stop_requested:
            self.app.call_from_thread(self.mark_running)
        assert process.stdout is not None
        for line in process.stdout:
            self.app.call_from_thread(self.append, line.rstrip("\n"))
        self.app.call_from_thread(self.finish, process.wait(), "")

    def mark_running(self) -> None:
        stop = self.stop_record
        self.query_one("#review-status", Static).update(
            f" reviewing {link(stop.key, pr_url(stop.repository, stop.number))}"
            f" — running; {escape('[x]')} stops it"
        )

    def append(self, line: str) -> None:
        self.output.append(line)
        self.query_one("#review-log", RichLog).write(line)

    def finish(self, code: int | None, error: str) -> None:
        self.finished = True
        stop = self.stop_record
        elapsed = int(time.monotonic() - self.started)
        live_context = live_review_context(self.live_snapshot, title=stop.title)
        if ACTIVE_BACKEND == "codex":
            base_sha = str(self.live_snapshot.get("baseRefOid") or "")
            head_sha = str(self.live_snapshot.get("headRefOid") or "")
            if len(base_sha) != 40 or len(head_sha) != 40:
                result = ReviewResult(1, "failed", provenance={"backend": "codex"})
            else:
                result = CodexHarness(availability=Availability.READY).convert(
                    "\n".join(self.output),
                    ReviewRequest(
                        *stop.repository.split("/", 1), stop.number, base_sha, head_sha,
                        actor="maintainer", tenant="review", generated_at="dashboard",
                    ),
                    code or 0,
                    model=self.selection.model, effort=self.selection.effort,
                )
                result = ReviewResult(
                    result.version, result.state, result.counts, result.findings,
                    live_review_verification(self.live_snapshot), result.provenance,
                    self.overlap_snapshot, live_context, result.raw_evidence,
                )
        else:
            result = adapt_current_engine(
                "\n".join(self.output), code,
                {"backend": os.environ.get("GOOSE_PROVIDER", "goose"),
                 "model": os.environ.get("GOOSE_MODEL", "gpt-5.6-luna"),
                 "repository": stop.repository, "pull_request": stop.number},
                verification=live_review_verification(self.live_snapshot),
                overlap=self.overlap_snapshot,
                live=live_context,
            )
        if ACTIVE_BACKEND == "codex" and len(str(self.live_snapshot.get("baseRefOid") or "")) == 40 and len(str(self.live_snapshot.get("headRefOid") or "")) == 40:
            request = ReviewRequest(
                *stop.repository.split("/", 1), stop.number,
                self.live_snapshot["baseRefOid"], self.live_snapshot["headRefOid"],
                actor="maintainer", tenant="review", generated_at="dashboard",
            )
            if can_remember(result, request):
                remember_success(
                    load_preferences(), stop.repository,
                    Preference("codex", result.provenance.get("model", "gpt-5.6-luna"),
                               result.provenance.get("reasoning_effort", "low")),
                )
        card = build_decision_card(
            result, exact_head=str(self.live_snapshot.get("headRefOid") or "")
        )
        if error:
            outcome, state = "error", f"FAILED to start: {error}"
        elif self.stop_requested:
            outcome, state = "stopped", "STOPPED — you cancelled it. Nothing was submitted."
        elif code is not None and code < 0:
            outcome, state = "stopped", "STOPPED — the review was killed. Nothing was submitted."
        elif card.state in (DecisionState.CLEAN, DecisionState.FINDINGS):
            outcome = "complete"
            state = "COMPLETE — a Review Draft for you to judge. Nothing was submitted."
        elif card.state is DecisionState.STALE:
            outcome, state = "stale", "STALE — the review does not match the current head. Rerun it."
        elif card.state is DecisionState.INCOMPLETE:
            outcome = "incomplete"
            state = (
                "INCOMPLETE — part of this review returned no verdict. "
                "Its finding count is not a clean bill of health."
            )
        elif card.state is DecisionState.UNPARSABLE:
            outcome = "incomplete"
            state = "UNPARSABLE — the review output is not a clean result."
        else:
            outcome = "failed"
            state = f"FAILED (exit {code}) — the review did not run. Nothing was submitted."
        stop.review_result = result

        delta_card = self._re_review_card(result)

        status = self.query_one("#review-status", Static)
        status.remove_class("running")
        status.add_class(outcome)
        status.update(
            f" {link(stop.key, pr_url(stop.repository, stop.number))} — "
            f"{escape(state)} ({elapsed}s) — {escape('[escape]')} closes"
        )
        finding_total = sum(card.counts.values())
        lines = [
            f"{card.state.value.upper()}  {escape(stop.key)}",
            f"what changed  {escape(card.summary.what_changed)}",
            f"risk/impact  {escape(card.summary.risk_impact)}",
            f"confidence  {escape(card.summary.ci_merge_state)} · head "
            f"{escape(card.freshness.label)} "
            f"{escape((card.exact_head or card.reviewed_head or '?')[:12])}",
            f"next action  {escape(card.summary.recommended_action)}",
            (
                f"findings  {finding_total} evidenced finding"
                f"{'s' if finding_total != 1 else ''}."
                if card.findings
                else "findings  No evidenced findings."
            ),
            "severity  "
            + "  ".join(
                f"{key}:{card.counts[key]}"
                for key in ("critical", "high", "medium", "low")
            ),
        ]
        if delta_card:
            lines.extend(delta_card)
        for finding in card.findings[:5]:
            lines.append(
                f"{finding.severity.upper()}  "
                f"{escape(finding.file)}:{finding.line}  "
                f"{escape(finding.title)}"
            )
        verified = sum(1 for item in card.verification if item.state == "verified")
        unverified = sum(1 for item in card.verification if item.state == "unverified")
        lines.append(
            f"checks  {verified} verified / {unverified} unverified / "
            f"{len(card.verification)} reported"
        )
        lines.append(
            f"overlap {card.duplicate_count} duplicate / "
            f"{card.shared_file_count} shared-file hazard"
        )
        lines.append(
            f"live     CI {escape(card.ci.value)} · merge "
            f"{escape(card.mergeability.label)}/{escape(card.merge_state)} · head "
            f"{escape((card.exact_head or card.reviewed_head or '?')[:12])}"
        )
        lines.append(
            f"source  {escape(card.provenance.backend or '?')} / "
            f"{escape(card.provenance.model or '?')}"
        )
        lines.append(
            "actions  "
            f"{escape('[L]')} review  {escape('[a]')} approve+queue  "
            f"{escape('[m]')} merge  {escape('[u]')} update  "
            f"{escape('[e]')} evidence"
        )
        self.query_one("#review-card", Static).update("\n".join(lines))
        evidence = [
            "REVIEW EVIDENCE",
            f"state    {result.state.upper()}",
            f"findings {finding_total}",
            f"checks   {verified} verified / {unverified} unverified",
            f"live     CI {result.live.get('ci', 'unknown')} · "
            f"{escape(result.live.get('mergeable', '?'))}/"
            f"{escape(result.live.get('merge_state', '?'))}",
        ]
        for finding in result.findings[:12]:
            evidence.append(
                f"{finding['severity'].upper()}  "
                f"{escape(finding.get('file', '?'))}:{finding.get('line', '?')}  "
                f"{escape(finding.get('title', ''))}"
            )
        if len(result.findings) > 12:
            evidence.append(f"… {len(result.findings) - 12} more findings omitted from this bounded view")
        evidence.append("[r] raw backend transcript (last 200 lines)")
        self.query_one("#review-evidence", Static).update("\n".join(evidence))
        trace(
            {
                "action": "review",
                "repository": stop.repository,
                "number": stop.number,
                "steer": self.steer,
                "outcome": outcome,
                "exit_code": code,
                "seconds": elapsed,
            }
        )

    def _re_review_card(self, result: ReviewResult) -> list[str]:
        """Project explicit exact-head delta evidence into bounded card lines."""
        reviewed = str(result.provenance.get("head_sha") or "")
        current = str(self.live_snapshot.get("headRefOid") or "")
        base = str(self.live_snapshot.get("baseRefOid") or "")
        if (not re.fullmatch(r"[0-9a-f]{40}", reviewed) or
                not re.fullmatch(r"[0-9a-f]{40}", current) or reviewed == current or
                not re.fullmatch(r"[0-9a-f]{40}", base)):
            return []
        raw = self.live_snapshot.get("re_review")
        if not isinstance(raw, dict):
            return []
        def region(item: object) -> Region | None:
            if not isinstance(item, dict): return None
            try: return Region(str(item["path"]), int(item["start_line"]), int(item.get("end_line", item["start_line"])))
            except (KeyError, TypeError, ValueError): return None
        regions = tuple(x for item in raw.get("changed_regions", []) if (x := region(item)) is not None)
        evidence = tuple(x for item in raw.get("evidence", []) if (x := region(item)) is not None)
        prior = tuple(PriorFinding(f"{f.get('file')}:{f.get('line')}", FindingEvidence(str(f["file"]), int(f["line"]), int(f.get("end_line", f["line"]))))
                     for f in result.findings if isinstance(f, dict) and f.get("file") and isinstance(f.get("line"), int))
        ev = tuple(FindingEvidence(x.path, x.start_line, x.end_line, bool(raw.get("stale_evidence"))) for x in evidence)
        new = tuple(H1Evidence(str(x["finding_id"]), str(x["path"]), int(x["line"])) for x in raw.get("newly_supported", []) if isinstance(x, dict) and x.get("finding_id") and x.get("path") and isinstance(x.get("line"), int))
        try:
            request = ReviewRequest(*self.stop_record.repository.split("/", 1), self.stop_record.number, base, current, "maintainer", "review", generated_at="dashboard")
            delta = classify_head_delta(DeltaInput(reviewed, current, base, base, ReviewEvidenceManifest(request), regions, prior, ev, new,
                bool(raw.get("mapping_uncertain")), bool(raw.get("sensitive_surfaces_changed"))))
        except (TypeError, ValueError, KeyError):
            return []
        lines = ["", "RE-REVIEW  exact-head delta", f"reviewed {reviewed}  current {current}"]
        lines.append("dispositions  " + ", ".join(f"{x.finding_id}={x.disposition.value}" for x in delta.findings[:12]) if delta.findings else "dispositions  none")
        if delta.newly_supported: lines.append("new H1 evidence  " + ", ".join(f"{x.finding_id} ({x.path}:{x.line})" for x in delta.newly_supported[:8]))
        lines.append("No authority carried from H0.")
        if delta.full_review_required:
            lines.append("FULL REVIEW REQUIRED — " + ", ".join(x.value for x in delta.fallback_reasons) + ".")
        return lines

    def action_stop(self) -> None:
        # Signal the whole process group, and mean it. A review that ignores
        # SIGTERM — or a check subprocess that outlives its parent — gets
        # SIGKILL after a grace period, because a stop key that leaves the
        # review running is worse than no stop key.
        if self.finished or self.stop_requested:
            return
        self.stop_requested = True
        self.query_one("#review-status", Static).update(
            f" {self.stop_record.repository}#{self.stop_record.number} — stopping…"
        )
        if self.signal_group(signal.SIGTERM):
            self.set_timer(STOP_GRACE_SECONDS, self.escalate_stop)

    def escalate_stop(self) -> None:
        if not self.finished:
            self.signal_group(signal.SIGKILL)

    def schedule_stop_escalation(self) -> None:
        self.set_timer(STOP_GRACE_SECONDS, self.escalate_stop)

    def signal_group(self, number: int) -> bool:
        process = self.process
        if process is None or process.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), number)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def action_leave_review(self) -> None:
        """Leave a GitHub review from here — the draft is on screen, which is
        the moment a maintainer actually has an opinion to record."""
        self.app.leave_review(self.stop_record)

    def action_toggle_evidence(self) -> None:
        self.query_one("#review-evidence", Static).toggle_class("hidden")

    def action_toggle_raw_transcript(self) -> None:
        self.query_one("#review-log", RichLog).toggle_class("hidden")

    def return_to_queue(self, action) -> None:
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return
        self.dismiss()
        self.app.call_after_refresh(action)

    def action_queue(self) -> None:
        self.return_to_queue(self.app.action_merge)

    def action_merge_now(self) -> None:
        self.return_to_queue(self.app.action_merge_now)

    def action_update_branch(self) -> None:
        self.return_to_queue(self.app.action_update_branch)

    def action_close(self) -> None:
        # A review takes minutes. Closing mid-run would throw that away with a
        # keystroke, so an unfinished review has to be stopped deliberately.
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return
        self.dismiss()


class ReviewDashboard(App):
    """PROJECT BLUEFIN REVIEW DASHBOARD."""

    TITLE = "BLUEFIN REVIEW DASHBOARD"
    CSS = """
    #status-bar { height: 1; background: $panel; color: cyan; }
    #queue-pane { width: 45%; border: solid $secondary; }
    #right-pane { width: 55%; }
    #details-pane { height: 60%; border: solid $secondary; padding: 0 1; }
    #context-pane { height: 40%; border: solid $secondary; padding: 0 1; }
    #details, #context { height: auto; }
    #confirm-box {
        border: heavy magenta; background: $surface;
        width: 80%; height: auto; padding: 1 2; margin: 4 4;
    }
    #confirm-command, .confirm-command { color: magenta; text-style: bold; }
    #steer { border: solid $secondary; height: 3; }
    #keys-reading, #keys-acting { height: 1; background: $panel; }
    #keys-reading { color: $text; }
    #keys-acting { color: magenta; }
    #diff-header { height: 1; background: $panel; color: cyan; text-style: bold; }
    #review-card { border: solid $success; padding: 1 2; height: auto; color: $text; }
    #review-evidence.hidden, #review-log.hidden { display: none; }
    #diff-scroll { border: solid $secondary; background: $surface; }
    #diff-body { padding: 0 1; width: auto; }
    ListItem.selected { background: $primary-muted; }
    ListItem.selected Label { color: magenta; text-style: bold; }
    #review-status { height: auto; padding: 0 1; background: $panel; }
    #review-status.running { background: $panel; color: cyan; }
    #review-status.complete { background: $success; color: $text; text-style: bold; }
    #review-status.incomplete { background: $warning; color: $text; text-style: bold; }
    #review-status.stale { background: $warning; color: $text; text-style: bold; }
    #review-status.failed, #review-status.error, #review-status.stopped {
        background: $error; color: $text; text-style: bold;
    }
    #review-log { border: solid $secondary; }
    #takeoff-box { border: heavy cyan; background: $surface; width: 80%; height: auto; padding: 1 2; margin: 4 4; }
    """

    BINDINGS = bindings_for("dashboard")

    def __init__(self, filters: QueueFilters | None = None) -> None:
        super().__init__()
        self.filters = filters or QueueFilters()
        self.stops: list[Stop] = []
        self.self_login = ""
        self.generated_at = ""
        self.pulls_cache: dict[str, list[dict]] = {}
        # Repository -> whether this login may merge there. Merging without
        # the lgtm opt-in is a maintainer power, so it is asked of GitHub per
        # repository rather than assumed from the fact that a dashboard is
        # open. Unknown until asked, and never cached as True by default.
        self.merge_rights: dict[str, bool] = {}
        self.snapshot_items: list[dict] = []
        # What Hive says, when it has been asked. "" means not asked yet, so
        # the status line can tell "we have not looked" apart from "the hub is
        # down" — the first is a dashboard that never tried, which is what the
        # old permanent "Hive: not consulted" amounted to.
        self.hive_state = ""
        self.hive_workers: list[dict] = []
        # Keys to re-select after a refresh: a refresh that silently empties
        # the batch you spent a minute building is worse than no refresh.
        self.reselect: set[str] = set()
        self.all_items: list[dict] = []
        self.harness_state = "CHECKING"
        self.harness_options: list[HarnessOption] = []
        # Dispatched landing batches, oldest first. One agent runs at a time;
        # a batch confirmed while another runs waits behind it — a proper
        # queue, not a pile of concurrent agents mutating the same queue.
        self.landing_queue: list[landing.LandingTask] = []
        # The last finished batch's outcome, kept on the status line until
        # the next dispatch or refresh: a toast is gone in seconds and a
        # maintainer looks up late.
        self.last_landing_outcome = ""
        self.source_state = "loading"
        self.source_message = ""

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading queue…", id="status-bar")
        yield Static("Harness Autopilot — CHECKING…", id="harness-status")
        with Horizontal():
            with Vertical(id="queue-pane"):
                yield ListView(id="queue")
            with Vertical(id="right-pane"):
                # The evidence panes are scroll containers, not bare
                # Statics: a Static clips content it cannot fit and is not
                # focusable, so evidence taller than the pane was simply
                # unreachable, and h/l could never land on it. A
                # ScrollableContainer takes focus, so pane movement reaches
                # it and its own keys scroll it.
                with ScrollableContainer(id="details-pane"):
                    yield Static("", id="details")
                with ScrollableContainer(id="context-pane"):
                    yield Static("", id="context")
        yield Input(
            placeholder="[/] steer the review of the highlighted PR — "
            "enter runs it, esc returns to the queue",
            id="steer",
        )
        # Two lines, not Textual's one-line Footer. Fourteen bindings do not
        # fit on one row of an 80-column terminal, and a key map that is
        # truncated is a key map that teaches the wrong half of the tool.
        # Reading actions first, then the ones that change something.
        yield Static(KEYS_READING, id="keys-reading")
        yield Static(KEYS_ACTING, id="keys-acting")

    def on_mount(self) -> None:
        # The queue keeps the keystrokes. The steer box is entered on purpose
        # with [/], because a focused Input swallows every single-key binding.
        self.query_one("#queue", ListView).focus()
        self.load_queue()
        self.load_hive()
        self.discover_harness()

    @work(thread=True)
    def discover_harness(self) -> None:
        """Probe Codex off the UI thread; discovery never starts inference."""
        options = discover_all()
        self.call_from_thread(self.harness_loaded, options)

    def harness_loaded(self, options: list[HarnessOption]) -> None:
        self.harness_options = options
        result = next((option.discovery for option in options if option.harness.branding.harness_id == ACTIVE_BACKEND), options[0].discovery)
        self.harness_state = result.availability.value
        label = self.query_one("#harness-status", Static)
        if result.availability is Availability.READY:
            label.update(
                "Harness Autopilot — READY · Codex / gpt-5.6-luna · "
                "reason: low · Start requires Enter/click"
            )
        else:
            label.update(
                f"Harness Autopilot — {result.availability.value} · "
                "[Diagnostics] [Sign in] [Install] [Retry] · no fallback"
            )

    @work(thread=True)
    def load_hive(self) -> None:
        """Ask Hive what it is doing. Read-only, and never blocking."""
        if not hive_api_base():
            self.call_from_thread(self.hive_loaded, "not configured", [])
            return
        status = hive_get("/api/v1/status")
        if not status.ok:
            self.call_from_thread(self.hive_loaded, status.message, [])
            return
        contributor_result = hive_get("/api/v1/contributors")
        if not contributor_result.ok:
            self.call_from_thread(self.hive_loaded, contributor_result.message, [])
            return
        contributors = contributor_result.data.get("contributors", [])
        workers = [
            {
                "login": contributor.get("github_username", "?"),
                "task": contributor.get("current_task") or {},
            }
            for contributor in contributors
            if contributor.get("current_task")
        ]
        state = (
            f"{status.data.get('hub', 'online')} · "
            f"{status.data.get('actionable_items', '?')} actionable · "
            f"{len(workers)} working"
        )
        self.call_from_thread(self.hive_loaded, state, workers)

    def hive_loaded(self, state: str, workers: list[dict]) -> None:
        self.hive_state = state
        self.hive_workers = workers
        self.refresh_status()
        stop = self.current
        if stop:
            self.render_context(stop)

    def repo_queue(self, repository: str) -> tuple[dict[str, int], int]:
        """This repository's merge queue, by segment, and its total."""
        counts: dict[str, int] = {}
        for item in self.all_items:
            if item.get("repository") != repository:
                continue
            counts[classify_queue_item(item)] = (
                counts.get(classify_queue_item(item), 0) + 1
            )
        return counts, sum(counts.values())

    def hive_worker_for(self, stop: Stop) -> dict | None:
        """The contributor Hive currently has on this exact pull request."""
        for worker in self.hive_workers:
            task = worker["task"]
            if (
                task.get("repo") == stop.repository
                and task.get("number") == stop.number
            ):
                return worker
        return None

    def action_hive(self) -> None:
        """Ask Hive again, and say what it is working on right now."""
        self.notify("asking Hive…")
        self.load_hive()
        if not self.hive_workers:
            return
        lines = []
        for worker in self.hive_workers[:6]:
            task = worker["task"]
            repo = str(task.get("repo", "?"))
            number = task.get("number", 0)
            lines.append(
                f"{escape(worker['login'])}: "
                f"{link(f'{repo}#{number}', pr_url(repo, number))}"
            )
        self.notify("Hive is working on:\n" + "\n".join(lines))

    def action_steer(self) -> None:
        """Focus the steering box: free text that rides along with the next
        review of the highlighted stop as maintainer instructions."""
        if not self.current:
            self.notify("nothing highlighted to steer.", severity="warning")
            return
        self.query_one("#steer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "steer":
            return
        event.stop()
        steer = event.value.strip()
        field = self.query_one("#steer", Input)
        field.value = ""
        self.query_one("#queue", ListView).focus()
        stop = self.current
        if not stop or not steer:
            return
        self.start_review(stop, steer)

    def start_review(self, stop: Stop, steer: str = "") -> None:
        if ACTIVE_BACKEND == "codex":
            options = self.harness_options or discover_all()
            preferences = load_preferences()
            initial = choose_option(stop.repository, preferences, options)
            initial_preference = None
            for candidate in (preferences.get(stop.repository), preferences.get("*")):
                if initial and candidate and candidate.harness_id == initial.harness.branding.harness_id:
                    initial_preference = candidate
                    break
            if initial and initial_preference is None:
                initial_preference = Preference(initial.harness.branding.harness_id,
                                                initial.discovery.model, "low")
            def selected(selection: Preference | None) -> None:
                global ACTIVE_BACKEND
                if selection:
                    ACTIVE_BACKEND = selection.harness_id
                    self.push_screen(ReviewScreen(stop, steer=steer, selection=selection))
            self.push_screen(HarnessTakeoff(options, initial, initial_preference), selected)
            return
        self.push_screen(ReviewScreen(stop, steer=steer))

    def on_key(self, event) -> None:
        if event.key == "l" and event.character == "L":
            event.stop()
            self._dispatch_terminal_action("leave review", self.action_leave_review)
            return
        if event.key == "l" and event.character == "l":
            event.stop()
            self._dispatch_terminal_action("next pane", self.action_pane_next)
            return
        if event.key == "q" and not isinstance(self.focused, (Input, TextArea)):
            event.stop()
            self.action_back()
            return
        if event.key == "escape" and self.focused is self.query_one("#steer", Input):
            event.stop()
            self.query_one("#queue", ListView).focus()

    def _dispatch_terminal_action(self, label: str, action) -> None:
        try:
            action()
        except Exception as error:
            self.notify(
                bounded_detail(f"{label} unavailable: {error}"),
                severity="error",
            )

    # ── data layer (walker parity) ────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def load_queue(self) -> None:
        try:
            who = gh("api", "user", "--jq", ".login")
            identity_detail = (who.stderr or who.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            who = None
            identity_detail = str(error)
        self.self_login = who.stdout.strip() if who and who.returncode == 0 else ""
        if self.filters.live:
            if not self.self_login:
                self.source_state = "auth-failed"
                self.source_message = bounded_detail(identity_detail or "GitHub identity is unavailable; sign in and retry")
                self.all_items = self.snapshot_items = []
                self.call_from_thread(self.apply_filters)
                return
            snapshot = self.load_live_queue(self.filters.live_repository)
        else:
            try:
                with urllib.request.urlopen(self.filters.url, timeout=60) as response:
                    snapshot = json.load(response)
                self.source_state = "ready"
                self.source_message = ""
            except Exception as error:
                self.source_state = "error"
                self.source_message = f"static queue unavailable: {error}"
                self.call_from_thread(self.apply_filters)
                return
        self.generated_at = snapshot.get("generated_at", "")
        # Keep the whole snapshot: the action filter is a view over it, so
        # narrowing and widening never needs another fetch.
        # The unfiltered set: "how busy is this repository" must count the
        # maintainer's own pull requests too, even though they never appear
        # as stops to review.
        self.all_items = snapshot.get("items", [])
        self.snapshot_items = [
            item
            for item in self.all_items
            # Own-work filtering: a maintainer reviews other people's work.
            if not (self.self_login and item.get("author") == self.self_login)
        ]
        self.call_from_thread(self.apply_filters)

    def load_live_queue(self, repository: str) -> dict:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            self.source_state = "malformed"
            self.source_message = bounded_detail(f"invalid repository '{repository}'; use owner/repo")
            return {"items": []}
        try:
            result = gh(
                "api", "--paginate", "--slurp", "--method", "GET",
                f"repos/{repository}/pulls?state=open&per_page=100",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.source_state = "error"
            self.source_message = bounded_detail(f"GitHub could not read this repository: {error}")
            return {"items": []}
        if result.returncode:
            detail = bounded_detail((result.stderr or result.stdout).strip())
            lowered = detail.lower()
            if ("authentication" in lowered or "login" in lowered
                    or "permission" in lowered or "forbidden" in lowered
                    or "not accessible" in lowered):
                self.source_state = "inaccessible"
            elif "not found" in lowered or "could not resolve" in lowered:
                self.source_state = "missing"
            else:
                self.source_state = "error"
            self.source_message = detail or "GitHub could not read this repository"
            return {"items": []}
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("GitHub returned malformed pull-request pages")
            pulls = [pull for page in pages for pull in page]
            if any(not isinstance(pull, dict) for pull in pulls):
                raise ValueError("GitHub returned a malformed pull-request entry")
        except (json.JSONDecodeError, ValueError) as error:
            self.source_state = "malformed"
            self.source_message = bounded_detail(f"malformed GitHub response: {error}")
            return {"items": []}
        try:
            items = []
            for index, pull in enumerate(pulls, 1):
                number = pull.get("number")
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    raise ValueError(f"pull {index} has invalid number")
                if not isinstance(pull.get("title"), str):
                    raise ValueError(f"pull {index} has invalid title")
                if "user" in pull:
                    author_source = pull["user"]
                else:
                    author_source = pull.get("author")
                if author_source is not None and not isinstance(author_source, dict):
                    raise ValueError(f"pull {index} has invalid author")
                author = ""
                if author_source is not None and "login" in author_source:
                    if not isinstance(author_source["login"], str):
                        raise ValueError(f"pull {index} has invalid login")
                    author = author_source["login"]
                items.append({
                    "repository": repository,
                    "number": number,
                    "recommended_action": "review",
                    "title": pull["title"],
                    "author": author,
                    "mergeable_state": str(pull.get("mergeable", "") or "").lower(),
                    "check_state": "unknown",
                    "review_state": str(pull.get("reviewDecision", "") or "").lower(),
                })
        except ValueError as error:
            self.source_message = bounded_detail(f"malformed GitHub response: {error}")
            self.source_state = "malformed"
            return {"items": []}
        self.source_state = "empty" if not items else "ready"
        self.source_message = ""
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "items": items}

    def apply_filters(self) -> None:
        stops = [
            Stop(
                repository=item["repository"],
                number=item["number"],
                action=item.get("recommended_action", ""),
                title=item.get("title", ""),
                author=item.get("author", "") or "",
                mergeable_state=item.get("mergeable_state", "") or "",
                check_state=item.get("check_state", "") or "",
                review_state=item.get("review_state", "") or "",
            )
            for item in self.snapshot_items
            if self.filters.wants(item)
        ]
        stops.sort(key=lambda stop: (action_rank(stop.action), stop.repository, stop.number))
        self.restore_landing_marks(stops)
        if self.reselect:
            for stop in stops:
                stop.selected = stop.key in self.reselect
            self.reselect = set()
        self.populate(stops)

    def row_markup(self, stop: Stop) -> str:
        # Selection is not colour-only: a ● leads the row and the whole row
        # carries a background, so the batch in progress reads at a glance.
        selected = "● " if stop.selected else "  "
        # MECHANICAL replaces the old title-only BATCHABLE tag: it means this
        # branch can be brought current, never that the change is approved.
        tag = " (MECHANICAL)" if stop.mechanical else ""
        # A stop that would not merge says so on its own row, so a failure in
        # the middle of a batch survives the notification that reported it.
        failed = " ✗ DID NOT MERGE" if stop.failure else ""
        checks = effective_check_state(stop.check_state, stop.live)
        marks = ""
        if stop.mergeable_state == "dirty":
            marks += " ⚑ CONFLICTS"
        marks += f" {ci_marker(checks)}"
        if stop.review_state == "approved":
            marks += " ✓ approved"
        body = (
            f"{selected}{link(stop.key, pr_url(stop.repository, stop.number))}: "
            f"{escape(stop.title[:60])}{tag} "
            f"{marks} {escape('[' + stop.action + ']')}{failed}"
        )
        style = stop_style(
            stop.action, stop.mergeable_state, checks, stop.review_state
        )
        return f"[{style}]{body}[/{style}]" if style else body

    def populate(self, stops: list[Stop]) -> None:
        self.stops = stops
        queue = self.query_one("#queue", ListView)
        queue.clear()
        for stop in stops:
            item = ListItem(Label(self.row_markup(stop)))
            item.set_class(stop.selected, "selected")
            queue.append(item)
        self.refresh_status()
        if stops:
            queue.index = 0

    def refresh_rows(self) -> None:
        """Repaint the rows in place, keeping the highlight where it was."""
        queue = self.query_one("#queue", ListView)
        for stop, item in zip(self.stops, queue.children):
            labels = item.query(Label)
            if labels:
                labels.first().update(self.row_markup(stop))
            item.set_class(stop.selected, "selected")
        self.refresh_status()

    def refresh_status(self) -> None:
        selected = sum(1 for s in self.stops if s.selected)
        failed = sum(1 for s in self.stops if s.failure)
        stuck = f" | {failed} did not merge" if failed else ""
        running = sum(1 for t in self.landing_queue if t.running)
        queued = sum(
            1 for t in self.landing_queue if t.process is None and t.returncode is None
        )
        agents = (
            f" | agents: {running} running, {queued} queued [w]"
            if self.landing_queue
            else ""
        )
        freshness = self.generated_at or "unknown"
        shown = len(self.stops)
        total = len(self.snapshot_items)
        scope = self.filters.action or "all"
        # Say how much of the queue is hidden. A filtered view that looks like
        # the whole queue is how a maintainer concludes there are five open
        # pull requests when there are a hundred and twenty-one.
        held_back = f" (of {total}; [f] widens)" if shown != total else ""
        landed = (
            f" | last {self.last_landing_outcome}"
            if self.last_landing_outcome
            else ""
        )
        breakdown = ", ".join(
            f"{count} {action}"
            for action, count in sorted(
                Counter(
                    item.get("recommended_action", "") for item in self.snapshot_items
                ).items(),
                key=lambda pair: action_rank(pair[0]),
            )
        )
        self.query_one("#status-bar", Static).update(
            f" Queue: {shown} PRs{held_back} | filter {scope} | {breakdown} "
            f"| {('source ' + self.source_state + (' — ' + self.source_message if self.source_message else ''))} "
            f"| {('snapshot ' + freshness) if not self.filters.live else 'repository ' + self.filters.live_repository} | as {self.self_login or 'unknown'} "
            f"| batch: {selected}{stuck}{agents}{landed} | Hive: {escape(self.hive_state or 'asking…')}"
        )

    def action_filter(self) -> None:
        """Cycle the action filter: every action, then one at a time."""
        present = [a for a in MAINTAINER_ORDER if any(
            item.get("recommended_action") == a for item in self.snapshot_items
        )]
        scopes = [""] + present
        try:
            nxt = scopes[(scopes.index(self.filters.action) + 1) % len(scopes)]
        except ValueError:
            nxt = ""
        self.filters.action = nxt
        self.apply_filters()
        self.notify(f"filter: {nxt or 'all actions'} — {len(self.stops)} PRs")

    @property
    def current(self) -> Stop | None:
        index = self.query_one("#queue", ListView).index
        if index is None or not (0 <= index < len(self.stops)):
            return None
        return self.stops[index]

    def on_list_view_highlighted(self, _event) -> None:
        stop = self.current
        if stop:
            self.show_evidence(stop)

    @work(thread=True)
    def show_evidence(self, stop: Stop) -> None:
        live = gh(
            "pr", "view", str(stop.number), "--repo", stop.repository,
            "--json",
            "author,state,baseRefOid,headRefOid,isDraft,mergeable,mergeStateStatus,"
            "reviewDecision,additions,deletions,changedFiles,updatedAt,body,"
            "closingIssuesReferences,statusCheckRollup,labels,reviews",
        )
        stop.live = json.loads(live.stdout) if live.returncode == 0 else {}
        if stop.repository not in self.merge_rights:
            # 'push' is exactly the power to merge on GitHub: a contributor
            # agent works from a fork and has none, which is why the direct
            # merge key can never be its path.
            rights = gh(
                "api", f"repos/{stop.repository}", "--jq", ".permissions.push"
            )
            self.merge_rights[stop.repository] = (
                rights.returncode == 0 and rights.stdout.strip() == "true"
            )
        self.call_from_thread(self.render_evidence, stop)

    def repo_pulls(self, repo: str) -> list[dict]:
        if repo not in self.pulls_cache:
            listing = gh(
                "pr", "list", "--repo", repo, "--state", "open",
                "--limit", PULL_FETCH_LIMIT,
                "--json",
                "number,title,files,closingIssuesReferences,author,"
                "updatedAt,isDraft,reviewDecision,mergeable",
            )
            if listing.returncode != 0:
                return []
            self.pulls_cache[repo] = json.loads(listing.stdout)
        return self.pulls_cache[repo]

    def paint_context(
        self, stop: Stop, text: str, dupes: list[dict], overlaps: list[dict]
    ) -> None:
        stop.overlap = {
            "duplicates": [item["number"] for item in dupes],
            "overlaps": [item["number"] for item in overlaps],
        }
        context = self.query("#context")
        if context:
            context.first().update(text)

    def cluster(self, stop: Stop) -> tuple[list[int], list[int]]:
        """Duplicates and overlaps, exactly as the walker computes them."""
        pulls = self.repo_pulls(stop.repository)
        mine = next((p for p in pulls if p["number"] == stop.number), None)
        if mine is None:
            return [], []

        def issues(pr: dict) -> set:
            return {r["number"] for r in (pr.get("closingIssuesReferences") or [])}

        def paths(pr: dict) -> set:
            return {f["path"] for f in (pr.get("files") or [])}

        subject = dependency_subject(mine["title"])
        dupes, overlaps = [], []
        for other in pulls:
            if other["number"] == stop.number:
                continue
            if subject and dependency_subject(other["title"]) == subject:
                dupes.append(self.neighbour(other, f"same dependency ({subject})"))
            elif issues(mine) & issues(other):
                shared = ", ".join(f"#{n}" for n in sorted(issues(mine) & issues(other)))
                dupes.append(self.neighbour(other, f"closes the same issue ({shared})"))
            elif paths(mine) & paths(other):
                shared = sorted(paths(mine) & paths(other))
                why = f"{len(shared)} shared file{'s' if len(shared) > 1 else ''}"
                overlaps.append(self.neighbour(other, f"{why}: {shared[0]}"))
        return dupes, overlaps

    @staticmethod
    def neighbour(pull: dict, why: str) -> dict:
        """One near-neighbour, summarised well enough to judge without opening it.

        A bare "dupe-of #26, #25, #24" tells a maintainer that a decision is
        required and nothing about how to make it — which of the three to keep
        is the whole question, and answering it meant three browser tabs. The
        listing this comes from already carries the titles and states, so the
        summary is free.
        """
        return {
            "number": pull["number"],
            "title": pull.get("title", ""),
            "why": why,
            "author": (pull.get("author") or {}).get("login", "?"),
            "draft": bool(pull.get("isDraft")),
            "review": pull.get("reviewDecision") or "",
            "mergeable": pull.get("mergeable") or "",
            "files": len(pull.get("files") or []),
            "updated": (pull.get("updatedAt") or "")[:10],
        }

    def render_evidence(self, stop: Stop) -> None:
        if self.current is not stop:
            return
        live = stop.live
        self.refresh_rows()
        checks = authoritative_checks(live)
        outcomes = [c.get("conclusion") or c.get("state") or "PENDING" for c in checks]
        ok = sum(1 for o in outcomes if o in ("SUCCESS", "NEUTRAL", "SKIPPED"))
        cancelled = sum(1 for o in outcomes if o == "CANCELLED")
        bad = sum(1 for o in outcomes if o in ("FAILURE", "ERROR", "TIMED_OUT"))
        pending = len(outcomes) - ok - bad - cancelled
        issues = ", ".join(
            link(f"#{r['number']}", issue_url(stop.repository, r["number"]))
            for r in (live.get("closingIssuesReferences") or [])
        ) or "-"
        labels = ", ".join(
            escape(l["name"]) for l in (live.get("labels") or [])
        ) or "-"
        author = (live.get("author") or {}).get("login", stop.author or "-")
        # Who has reviewed, and whether their word carries write access.
        # "approved" means something different from a maintainer than from a
        # drive-by, and the single reviewDecision field cannot say which it
        # was — or that three other people also looked.
        reviews = live.get("reviews") or []
        by_reviewer: dict[str, dict] = {}
        for review in reviews:
            login = (review.get("author") or {}).get("login") or "?"
            state = review.get("state", "")
            if state == "COMMENTED" and login in by_reviewer:
                # A comment never supersedes a verdict already given.
                continue
            by_reviewer[login] = review
        if by_reviewer:
            maintainers = sum(
                1
                for review in by_reviewer.values()
                if reviewer_standing(review.get("authorAssociation", "")) == "maintainer"
            )
            summary = (
                f"{len(by_reviewer)} "
                f"({maintainers} maintainer, {len(by_reviewer) - maintainers} community)"
            )
            detail = "\n".join(
                f"         {link(login, f'https://github.com/{login}')} "
                f"{reviewer_standing(review.get('authorAssociation', ''))} "
                f"{escape(review.get('state', '?'))}"
                for login, review in by_reviewer.items()
            )
            reviews_block = f"reviews  {summary}\n{detail}"
        else:
            reviews_block = "reviews  none yet"
        # MECHANICAL is a statement about branch maintenance only, so the
        # evidence line says what makes it updateable and nothing more.
        reason = stop.mechanical
        mechanical_block = (
            f"\nupdate   [b]MECHANICAL[/b] — {escape(reason)}\n"
            "         updateable only; not approved and not merge-safe"
            if reason
            else ""
        )
        self.query_one("#details", Static).update(
            f"[b]{link(stop.key, pr_url(stop.repository, stop.number))}[/b]  "
            f"{escape(stop.title)}\n"
            f"queue says: {escape(stop.action)}\n"
            f"author   {link(author, f'https://github.com/{author}')}\n"
            f"state    {live.get('state', '?')}    "
            f"head {str(live.get('headRefOid', ''))[:12] or '?'}\n"
            f"draft    {live.get('isDraft', '?')}    "
            f"review {live.get('reviewDecision') or '-'}\n"
            f"merge    {live.get('mergeable', '?')} / {live.get('mergeStateStatus', '?')}\n"
            f"size     +{live.get('additions', '?')} -{live.get('deletions', '?')} "
            f"across {live.get('changedFiles', '?')} files\n"
            f"checks   {ok} ok, {bad} failed, {cancelled} cancelled, {pending} pending\n"
            f"{reviews_block}\n"
            f"linked   {issues}\n"
            f"labels   {labels}{mechanical_block}"
            + (
                "\n\n[b]LAST MUTATION FAILURE[/b]\n"
                f"command  {escape(stop.failure_command)}\n"
                f"error    {escape(stop.failure)}\n"
                f"checks   {escape(stop.failure_checks or 'unknown')}\n"
                f"branch   {escape(stop.failure_branch or 'unknown')}"
                if stop.failure
                else ""
            )
        )
        self.render_context(stop)

    @work(thread=True)
    def render_context(self, stop: Stop) -> None:
        dupes, overlaps = self.cluster(stop)
        lines = ["[b]CONTEXT & VERIFICATION[/b]"]

        def summarise(neighbours: list[dict], limit: int) -> list[str]:
            out = []
            for near in neighbours[:limit]:
                marks = []
                if near["draft"]:
                    marks.append("draft")
                if near["review"]:
                    marks.append(near["review"].lower())
                if near["mergeable"] == "CONFLICTING":
                    marks.append("conflicting")
                marks.append(f"{near['files']} files")
                if near["updated"]:
                    marks.append(near["updated"])
                out.append(
                    f"  {link('#' + str(near['number']), pr_url(stop.repository, near['number']))} "
                    f"{escape(near['title'][:54])}"
                )
                out.append(
                    f"     by {escape(near['author'])} · {escape(', '.join(marks))}"
                )
                out.append(f"     {escape(near['why'])}")
            if len(neighbours) > limit:
                out.append(f"  … and {len(neighbours) - limit} more")
            return out

        if dupes:
            lines.append(
                f"[b]dupe-of[/b]  {len(dupes)} doing the same work — M resolves the cluster"
            )
            lines.extend(summarise(dupes, 3))
        if overlaps:
            lines.append(
                f"[b]overlaps[/b] {len(overlaps)} touching the same files "
                "(ordering hazard, not duplication)"
            )
            lines.extend(summarise(overlaps, 2))
        if not dupes and not overlaps:
            lines.append("no duplicates or overlaps in the open set")
        counts, total = self.repo_queue(stop.repository)
        if total:
            summary = " · ".join(
                f"[{colour}]{counts[key]} {label}[/{colour}]"
                for key, label, colour in QUEUE_SEGMENTS
                if counts.get(key)
            )
            lines.append(
                f"[b]merge queue[/b] {escape(stop.repository)} — {total} open"
            )
            lines.append(f"  {meter_bar(counts)}")
            lines.append(f"  {summary}")
        lines.append(f"skills   ~/.agents/skills (org inventory)")
        worker = self.hive_worker_for(stop)
        if worker:
            # The one thing worth interrupting a review for: an agent is
            # changing this pull request right now, so the diff on screen is
            # about to be stale.
            login = worker["login"]
            task_id = str(worker["task"].get("task_id", "?"))
            lines.append(
                f"hive     {link(login, 'https://github.com/' + login)} "
                f"is working on THIS now ({escape(task_id)})"
            )
        elif self.hive_state:
            lines.append(
                f"hive     {escape(self.hive_state)} — nobody on this one "
                f"{escape('([H] asks again)')}"
            )
        lines.append(f"trace    {TRACE_PATH}")
        # The whole DOM touch goes to the main thread, query included. Textual
        # is not thread-safe, and resolving the widget here would race the
        # repaint that populate()/refresh_rows() can be doing at the same
        # moment. Upstream: "avoid calling methods on your UI directly from a
        # threaded worker" (textual.textualize.io/guide/workers).
        self.call_from_thread(
            self.paint_context, stop, "\n".join(lines), dupes, overlaps
        )

    # ── the mutation gate ─────────────────────────────────────────────────

    def mutate(self, stop: Stop, *args: str, then=None) -> None:
        """Run one gh mutation behind the typed-number confirmation."""
        self.mutate_all(stop, [["gh", *args]], then=then)

    def mutate_all(
        self, stop: Stop, commands: list[list[str]], then=None, on_error=None,
        on_cancel=None,
    ) -> None:
        """Run a sequence of mutations behind one typed-number gate.

        The sequence is the unit a maintainer decides on, so it is confirmed
        once and then runs to completion off the UI thread. A failed step
        stops the rest: half a queueing is reported, never re-confirmed.
        """
        if not commands:
            return

        def finish(confirmed: bool | None) -> None:
            if not confirmed:
                self.notify("aborted; nothing was run.", severity="warning")
                if on_cancel:
                    on_cancel()
                return
            self.notify(f"running: {' '.join(commands[0][:4])}…")
            self.run_mutations(stop, commands, then, on_error)

        self.push_screen(ConfirmMutation(commands, str(stop.number)), finish)

    @work(thread=True)
    def run_mutations(
        self, stop: Stop, commands: list[list[str]], then, on_error=None
    ) -> None:
        """Execute a confirmed sequence off the UI thread. A slow or hung
        mutation must never freeze the dashboard, so each step is bounded by
        MUTATION_TIMEOUT and reports back through call_from_thread."""
        for command in commands:
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=MUTATION_TIMEOUT
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                trace(
                    {
                        "repo": stop.repository,
                        "number": stop.number,
                        "argv": command,
                        "error": str(error),
                    }
                )
                self.call_from_thread(
                    self.mutation_failed, stop, command, str(error), on_error
                )
                return
            if result.returncode != 0:
                message = result.stderr.strip() or f"exit {result.returncode}"
                trace(
                    {
                        "repo": stop.repository,
                        "number": stop.number,
                        "argv": command,
                        "exit": result.returncode,
                        "error": bounded_detail(message),
                    }
                )
                self.call_from_thread(
                    self.mutation_failed, stop, command, message, on_error
                )
                return
            trace(
                {
                    "repo": stop.repository,
                    "number": stop.number,
                    "argv": command,
                    "exit": result.returncode,
                }
            )
        self.call_from_thread(self.mutations_finished, stop, commands, then)

    def mutation_failed(
        self, stop: Stop, command: list[str], message: str, on_error=None
    ) -> None:
        self.pulls_cache.pop(stop.repository, None)
        stop.failure = message
        stop.failure_argv = list(command)
        stop.failure_command = shlex.join(command)
        context = live_review_context(stop.live)
        stop.failure_checks = context["ci"]
        stop.failure_branch = f"{context['mergeable']}/{context['merge_state']}"
        stop.selected = True
        self.refresh_rows()
        self.notify(f"{shlex.join(command[:4])}…: {escape(message[:200])}", severity="error")
        self.show_evidence(stop)
        # A failure that only prints is a failure the maintainer has to
        # remember. Hand it to whoever asked, so they can offer a way out.
        if on_error:
            on_error(message)

    def mutations_finished(
        self, stop: Stop, commands: list[list[str]], then
    ) -> None:
        """Apply one finished sequence on the UI thread."""
        self.pulls_cache.pop(stop.repository, None)
        self.notify(f"done: {' '.join(commands[-1][:4])}…")
        if then:
            then()
        self.show_evidence(stop)

    # ── actions ───────────────────────────────────────────────────────────

    def _queue(self) -> ListView:
        return self.query_one("#queue", ListView)

    def action_navigate_down(self) -> None:
        self._queue().action_cursor_down()

    def action_navigate_up(self) -> None:
        self._queue().action_cursor_up()

    def action_navigate_first(self) -> None:
        self._queue().index = 0

    def action_navigate_last(self) -> None:
        self._queue().index = max(0, len(self._queue().children) - 1)

    def action_navigate_page_down(self) -> None:
        queue = self._queue()
        queue.index = min(len(queue.children) - 1, queue.index + max(1, queue.size.height - 1))

    def action_navigate_page_up(self) -> None:
        queue = self._queue()
        queue.index = max(0, queue.index - max(1, queue.size.height - 1))

    def action_pane_previous(self) -> None:
        self.screen.focus_previous()

    def action_pane_next(self) -> None:
        self.screen.focus_next()

    def action_activate(self) -> None:
        if self.current:
            self.action_view_diff()

    def action_back(self) -> None:
        if isinstance(self.focused, (Input, TextArea)):
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()

    def action_help(self) -> None:
        entries = "  ".join(f"{c.key or '—'} {c.label}" for c in COMMANDS)
        self.notify(entries, timeout=8)

    def action_batch(self) -> None:
        stop = self.current
        if not stop:
            return
        stop.selected = not stop.selected
        item = self.query_one("#queue", ListView).highlighted_child
        if item:
            item.set_class(stop.selected, "selected")
            labels = item.query(Label)
            if labels:
                labels.first().update(self.row_markup(stop))
        self.refresh_status()

    def action_review(self) -> None:
        stop = self.current
        if stop:
            self.start_review(stop)

    def leave_review(self, stop: Stop) -> None:
        """Submit a review to GitHub: approve, request changes, or comment.

        Seeing a pull request judged is not the same as saying so. A
        maintainer who has read the agent's draft and the diff can leave their
        verdict here without merging anything and without arming automation —
        `a` is the automation opt-in, `m` is the merge, and this is neither.
        It is the ordinary review a reviewer owes an author, including the one
        that says no.
        """
        def with_verdict(verdict: str | None) -> None:
            if not verdict:
                return

            review_body = ReviewBody(stop, verdict)

            def with_body(value) -> None:
                if not isinstance(value, tuple) or len(value) != 2:
                    return
                body, body_file = value
                if not isinstance(body, str) or not isinstance(body_file, str) or not body_file:
                    return
                if body != review_body.previewed_body or body_file != review_body.body_file:
                    return
                try:
                    with open(body_file, encoding="utf-8") as source:
                        if source.read() != body:
                            return
                except (OSError, UnicodeError):
                    return
                if not body and verdict != "approve":
                    self.notify(
                        f"{verdict} needs a reason; nothing was submitted.",
                        severity="warning",
                    )
                    return
                def clean_body_file() -> None:
                    try:
                        os.unlink(body_file)
                    except FileNotFoundError:
                        pass

                self.mutate_all(
                    stop,
                    [[
                        "gh", "pr", "review", str(stop.number),
                        "--repo", stop.repository, f"--{verdict}",
                        "--body-file", body_file,
                    ]],
                    then=clean_body_file,
                    on_error=lambda _message: clean_body_file(),
                    on_cancel=clean_body_file,
                )

            self.push_screen(review_body, with_body)

        self.push_screen(ReviewVerdict(), with_verdict)

    def action_refresh(self) -> None:
        """Re-read the queue snapshot and ask Hive again.

        The snapshot is regenerated every 15 minutes and a session outlives
        that easily; merging or updating a branch invalidates it immediately.
        Relaunching the dashboard to see current state is not a workflow.
        """
        self.reselect = {stop.key for stop in self.stops if stop.selected}
        self.last_landing_outcome = ""
        self.notify("refreshing the queue…")
        self.load_queue()
        self.load_hive()

    def action_update_branch(self) -> None:
        """Bring the branch up to date with its base — the batch, if set.

        Nineteen of the queue's stops are conflicted and many more are merely
        behind, and each of those is a maintainer opening GitHub to press one
        button. `gh pr update-branch` merges the base in exactly as that
        button does, so a batch of stale-but-clean pull requests comes current
        in one pass. A real conflict still cannot be resolved this way, and
        GitHub says so rather than pretending otherwise.
        """
        batch = [s for s in self.stops if s.selected]
        if not batch and self.current:
            batch = [self.current]
        if not batch:
            return
        updateable = []
        for stop in batch:
            mergeable = str(stop.live.get("mergeable", "")).upper()
            state = str(stop.live.get("mergeStateStatus", "")).upper()
            if mergeable == "CONFLICTING" or state == "DIRTY" or stop.mergeable_state == "dirty":
                self.notify(
                    f"{stop.key}: conflicts need manual resolution; [u] only updates clean branches.",
                    severity="warning",
                )
                continue
            updateable.append(stop)
        if not updateable:
            return
        batch = updateable

        def update_next(index: int = 0) -> None:
            if index >= len(batch):
                if len(batch) > 1:
                    self.notify(f"asked GitHub to update {len(batch)} branches.")
                return
            stop = batch[index]

            def failed(message: str) -> None:
                stop.failure = message
                self.refresh_rows()

            self.mutate_all(
                stop,
                [[
                    "gh", "pr", "update-branch", str(stop.number),
                    "--repo", stop.repository,
                ]],
                then=lambda: update_next(index + 1),
                on_error=failed,
            )

        update_next()

    def action_select_mechanical(self) -> None:
        """Select exactly the stops whose branch is safe to bring current.

        Finding the green-but-behind Renovate branches by hand is the toil
        this removes. The candidate set is narrowed by author alone — the one
        snapshot field that cannot be faked by a title — and every candidate
        is then judged on freshly fetched live evidence, so the selection is
        never wider than what GitHub currently reports.
        """
        candidates = [
            stop for stop in self.stops
            if (stop.author or "").lower() in RENOVATE_BOTS
        ]
        if not candidates:
            self.notify("no Renovate pull requests in the current view.")
            return
        self.notify(f"checking {len(candidates)} Renovate pull request(s)…")
        self.classify_mechanical(candidates)

    @work(thread=True)
    def classify_mechanical(self, candidates: list[Stop]) -> None:
        for stop in candidates:
            result = gh(
                "pr", "view", str(stop.number), "--repo", stop.repository,
                "--json", MECHANICAL_FIELDS,
            )
            if result.returncode == 0:
                try:
                    stop.live = {**stop.live, **json.loads(result.stdout)}
                except json.JSONDecodeError:
                    stop.live = {}
            else:
                stop.live = {}
        self.call_from_thread(self.apply_mechanical_selection, candidates)

    def apply_mechanical_selection(self, candidates: list[Stop]) -> None:
        chosen = [stop for stop in candidates if stop.mechanical]
        for stop in self.stops:
            stop.selected = False
        for stop in chosen:
            stop.selected = True
        self.refresh_rows()
        if chosen:
            self.notify(
                f"selected {len(chosen)} mechanical branch update(s); "
                "[u] updates them one at a time behind the gate."
            )
        else:
            self.notify("no mechanical branch updates; selection cleared.")

    def action_leave_review(self) -> None:
        stop = self.current
        if stop:
            self.leave_review(stop)

    def action_docs(self) -> None:
        self.notify(f"docs-update agent task is tracked as {DOCS_UPDATE_ISSUE}")

    def action_open_browser(self) -> None:
        stop = self.current
        if stop:
            gh("pr", "view", str(stop.number), "--repo", stop.repository, "--web")

    def action_view_diff(self) -> None:
        stop = self.current
        if stop:
            self.push_screen(DiffScreen(stop))

    def action_comment(self) -> None:
        stop = self.current
        if not stop:
            return

        def submitted(confirmed) -> None:
            pass

        # Reuse the confirm modal's input for the body first.
        class CommentBody(ModalScreen[str | None]):
            BINDINGS = [
                Binding("ctrl+s", "submit", "submit comment", priority=True),
                *back_bindings("dismiss(None)"),
            ]

            def compose(self) -> ComposeResult:
                with Vertical(id="confirm-box"):
                    yield Label("comment (empty aborts):")
                    yield Input(id="comment-input")
                    yield Static("[ctrl-s] submit · [esc] cancel", markup=False)
                    yield Button("Submit comment", id="comment-submit", variant="primary")

            def on_mount(self) -> None:
                self.query_one(Input).focus()

            def on_input_submitted(self, event: Input.Submitted) -> None:
                self.action_submit()

            def action_submit(self) -> None:
                self.dismiss(self.query_one(Input).value or None)

            def on_button_pressed(self, event: Button.Pressed) -> None:
                if event.button.id == "comment-submit":
                    self.action_submit()

        def with_body(body: str | None) -> None:
            if not body:
                return
            os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
            body_file = os.path.join(os.path.dirname(TRACE_PATH), "comment.md")
            with open(body_file, "w", encoding="utf-8") as sink:
                sink.write(body + "\n")
            command = [
                "gh", "pr", "comment", str(stop.number),
                "--repo", stop.repository, "--body-file", body_file,
            ]

            def submitted(confirmed: bool | None) -> None:
                if confirmed:
                    self.mutate(stop, *command[1:])
                else:
                    try:
                        os.unlink(body_file)
                    except FileNotFoundError:
                        pass

            self.push_screen(CommentPreview(body, command), submitted)

        self.push_screen(CommentBody(), with_body)

    def _queueable(self, stop: Stop) -> bool:
        if stop.live.get("isDraft") is True:
            self.notify(
                f"{stop.key} is a draft; the sweep ignores drafts.",
                severity="warning",
            )
            return False
        if not self.self_login:
            self.notify(
                "your GitHub login is unknown; the queue approval needs it.",
                severity="warning",
            )
            return False
        return True

    def _queue_automerge(self, stop: Stop, then=None) -> None:
        """Ask Hive to queue this pull request through its governor contract.

        The authenticated endpoint verifies merger standing and the self-merge
        ban, then creates an exact-head approval as the Hive App and applies
        the queue label. A review submitted by this human process cannot pass
        Hive's App-authorship check (#247).

        The versioned `/api/v1` route is the only one a GitHub bearer token
        may use: kubestellar/hive#4052 gave it a hosted ingress without the
        browser-login intercept, while the session-only `/api/prs` route still
        belongs to the dashboard's browser clients (#258).
        """
        base = hive_api_base()
        if not base:
            self.notify("Hive is unreachable; nothing was queued.", severity="warning")
            return
        owner, repository = stop.repository.split("/", 1)
        endpoint = (
            f"{base}/api/v1/prs/{owner}/{repository}/{stop.number}/queue-automerge"
        )
        command = [
            sys.executable,
            HIVE_API_HELPER,
            "queue",
            endpoint,
        ]

        def queued() -> None:
            stop.failure = ""
            # Supersede any persisted failure, or the next refresh folds
            # it back onto a row the maintainer just re-queued (#290).
            landing.record_event(stop.key, "queued", f"re-queued by @{self.self_login or 'maintainer'}")
            self.refresh_rows()
            if then:
                then()

        def failed(message: str) -> None:
            # A half-queued pull request is the failure this issue was filed
            # for: it must be visible on the row, not only in a notification
            # that a batch has already scrolled past.
            stop.failure = message
            stop.selected = True
            self.refresh_rows()

        self.mutate_all(stop, [command], then=queued, on_error=failed)

    def action_merge(self) -> None:
        # `a` is approve+queue only. The batch key is `A` — a selection
        # must never turn this key into an undocumented batch gate.
        stop = self.current
        if not stop:
            return
        if not stop.live:
            self.notify(f"{stop.key}: no live evidence yet; select it first.")
            return
        if self._queueable(stop):
            self._queue_automerge(stop)

    def action_land_batch(self) -> None:
        """`A`: land every selected pull request as one batch.

        The maintainer tags rows with [b] and then reaches for the capital —
        "do them All". The stronger keystroke does the strong thing: the
        batch plan gate. Without a selection there is nothing to land; the
        read-only batch queue this key used to open lives on [w].
        """
        batch = [s for s in self.stops if s.selected]
        if not batch:
            self.notify(
                "nothing selected — [b] marks rows for the batch.",
                severity="warning",
            )
            return
        self.plan_landing(batch)

    def plan_landing(self, batch: list[Stop]) -> None:
        """Batch `A`: the reviewed selection becomes one agent's brief.

        The maintainer picked every row by hand; the BatchPlanScreen is the
        proportionate gate — the whole plan and the exact command, confirmed
        with Enter. The agent then owns the batch end to end: repair the
        mechanical failures, wait for green, land what the rules allow, and
        report each state change to the status file the queue screen polls.
        """
        if not self.self_login:
            self.notify(
                "your GitHub login is unknown; the landing approval needs it.",
                severity="warning",
            )
            return
        task = landing.new_task(batch, self.self_login)

        def finish(confirmed: bool | None) -> None:
            if not confirmed:
                self.notify("aborted; nothing was dispatched.", severity="warning")
                return
            self.enqueue_landing(task)
            self.push_screen(LandingScreen(self))

        self.push_screen(BatchPlanScreen(task), finish)

    def enqueue_landing(self, task: "landing.LandingTask") -> None:
        self.landing_queue.append(task)
        # A new dispatch supersedes the previous batch's outcome line.
        self.last_landing_outcome = ""
        self.refresh_status()
        if not any(t.running for t in self.landing_queue):
            self.drain_landings()

    @work(thread=True)
    def drain_landings(self) -> None:
        """Run the landing queue FIFO, one agent at a time."""
        while True:
            task = next(
                (
                    t
                    for t in self.landing_queue
                    if t.process is None and t.returncode is None
                ),
                None,
            )
            if task is None:
                return
            self.run_landing_task(task)

    def run_landing_task(self, task: "landing.LandingTask") -> None:
        """One batch agent, off the UI thread, its own process group so
        [x] stops the agent and everything it spawned together."""
        try:
            log = open(task.log_path, "a", encoding="utf-8")
        except OSError as error:
            task.returncode = 1
            self.call_from_thread(
                self.notify, f"landing log: {error}", severity="error"
            )
            return
        with log:
            try:
                process = subprocess.Popen(
                    task.command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                task.returncode = 1
                self.call_from_thread(
                    self.notify, f"landing agent: {error}", severity="error"
                )
                return
            task.process = process
            task.returncode = process.wait()
        self.call_from_thread(self.landing_finished, task)

    def landing_finished(self, task: "landing.LandingTask") -> None:
        """Fold the agent's report back onto the rows and say so: landed
        work leaves the batch; blocked, failed, and unfinished work stays
        selected with its reason. The notification announces the outcome —
        the row marking is what survives it."""
        events = landing.parse_status(task.status_path)
        # parse_status files every pr-less line under "" — a malformed tail
        # line included — so only the exact done event closes a report.
        done = events.get("", {}).get("state") == landing.TASK_DONE
        counts: Counter[str] = Counter()
        for stop in task.stops:
            event = events.get(stop.key, {})
            state = event.get("state")
            if state == "merged":
                stop.selected = False
                stop.failure = ""
            elif state in ("blocked", "failed", "awaiting-stable"):
                # blocked/failed need a maintainer; an agent that exits with
                # a PR still short of :stable has not finished, whatever its
                # exit code — the row keeps the reason and stays selected.
                stop.selected = True
                stop.failure = f"{state}: {event.get('note', 'no reason given')}"
            else:
                detail = f"last report: {state}" if state else "no report"
                stop.selected = True
                if done:
                    # The agent closed its report but never carried this
                    # pull request to an outcome — a hole in the report,
                    # not a dead agent, and the row must say which.
                    stop.failure = f"no outcome reported ({detail})"
                    state = "no outcome"
                else:
                    # No done event at all: the agent died mid-batch,
                    # distinguishable from every state it can report.
                    stop.failure = f"agent died mid-batch ({detail})"
                    state = "died mid-batch"
            counts[state] += 1
        parts = [
            f"{counts[state]} {state}"
            for state in (
                "merged",
                "failed",
                "blocked",
                "awaiting-stable",
                "no outcome",
                "died mid-batch",
            )
            if counts[state]
        ]
        if done:
            message = f"batch {task.task_id} finished: {', '.join(parts)}"
        else:
            # The agent never closed its report: distinguishable from a
            # batch that reported done with failures.
            message = (
                f"batch {task.task_id} agent exited without reporting done: "
                f"{', '.join(parts)}"
            )
        if (
            counts["failed"]
            or counts["no outcome"]
            or counts["died mid-batch"]
            or not done
        ):
            severity = "error"
        elif counts["blocked"] or counts["awaiting-stable"]:
            severity = "warning"
        else:
            severity = "information"
        # Set before refresh_rows: refresh_status renders it onto the bar.
        self.last_landing_outcome = message
        self.refresh_rows()
        self.notify(message, severity=severity)

    def restore_landing_marks(self, stops: list[Stop]) -> None:
        """Fold a previous run's landing outcomes back onto matching rows.
        The state directory persists on the host across relaunches (#281),
        but the record only helps if the rows show it. Only the failure
        marking is restored; selecting a batch stays the maintainer's."""
        events = landing.persisted_events()
        if not events:
            return
        for stop in stops:
            event = events.get(stop.key)
            if not event:
                continue
            state = event.get("state")
            if state in ("blocked", "failed", "awaiting-stable"):
                stop.failure = f"{state}: {event.get('note', 'no reason given')}"

    def action_agents(self) -> None:
        if not self.landing_queue:
            self.notify("no batch has been dispatched yet.", severity="warning")
            return
        self.push_screen(LandingScreen(self))

    def action_merge_now(self) -> None:
        """Merge this pull request now, as a maintainer, without `lgtm`.

        `lgtm` is an explicit opt-in to automation: it hands the pull request
        to Hive's governor sweep, which re-verifies and merges on green CI.
        Not every merge wants that, and a maintainer who has read the diff
        should not have to label a pull request to arm a robot in order to
        land it. This is the direct path — same typed-number gate, the same
        squash the sweep performs, and no label.

        It is a maintainer power. GitHub's `push` permission on the repository
        is exactly that power, so it is asked of GitHub rather than assumed.
        Branch protections are never bypassed: nothing here passes the flag
        that would override them, so a repository requiring review or green
        checks still refuses, and that refusal is reported rather than worked
        around.
        """
        batch = [s for s in self.stops if s.selected]
        if not batch and self.current:
            batch = [self.current]
        queue = [stop for stop in batch if self._mergeable_now(stop)]
        if not queue:
            return

        def merge_next(index: int = 0) -> None:
            if index >= len(queue):
                landed = [stop for stop in queue if not stop.failure]
                if len(queue) > 1:
                    self.notify(
                        f"merged {len(landed)} of {len(queue)}; "
                        f"{len(queue) - len(landed)} still queued."
                    )
                return
            stop = queue[index]
            self.merge_one(stop, then=lambda: merge_next(index + 1))

        merge_next()

    def _mergeable_now(self, stop: Stop) -> bool:
        """Whether this stop can even be attempted, with the reason if not."""
        if not stop.live:
            self.notify(f"{stop.key}: no live evidence yet; select it first.")
            return False
        if stop.live.get("isDraft") is True:
            self.notify(f"{stop.key} is a draft; ready it first.", severity="warning")
            return False
        if stop.repository not in self.merge_rights:
            self.notify(
                f"still checking your permission on {stop.repository}; try again.",
                severity="warning",
            )
            return False
        if not self.merge_rights[stop.repository]:
            self.notify(
                f"merging {stop.repository} directly is a maintainer power and "
                "you do not have it there; queue it with [a] instead.",
                severity="error",
            )
            return False
        checks = effective_check_state(stop.check_state, stop.live)
        if checks in {"failure", "pending"}:
            state = "failed" if checks == "failure" else "pending"
            self.notify(
                f"{stop.key}: direct merge refused; CI is known {state}.",
                severity="error",
            )
            return False
        return True

    def merge_one(self, stop: Stop, then=None, extra: list[list[str]] | None = None) -> None:
        """One squash merge, behind the gate, with a way out when it fails."""
        commands = list(extra or [])
        commands.append([
            "gh", "pr", "merge", str(stop.number),
            "--repo", stop.repository, "--squash",
        ])

        def landed() -> None:
            stop.failure = ""
            stop.failure_command = ""
            stop.failure_argv = []
            stop.failure_checks = ""
            stop.failure_branch = ""
            stop.selected = False
            # Supersede any persisted failure, or the next refresh folds
            # it back onto a row the maintainer just merged (#290).
            landing.record_event(stop.key, "merged", f"merged directly by @{self.self_login or 'maintainer'}")
            self.refresh_rows()
            if then:
                then()

        def failed(message: str) -> None:
            # Keep it selected: an unmerged pull request stays in the batch,
            # so "put it back in the queue" is the default rather than a
            # thing the maintainer has to remember to redo.
            stop.failure = message
            stop.selected = True
            self.refresh_rows()
            self.push_screen(
                MergeRecovery(stop, message),
                lambda choice: self.recover_merge(stop, choice, then),
            )

        self.mutate_all(stop, commands, then=landed, on_error=failed)

    def recover_merge(self, stop: Stop, choice: str | None, then=None) -> None:
        if choice == "update":
            self.merge_one(
                stop,
                then=then,
                extra=[[
                    "gh", "pr", "update-branch", str(stop.number),
                    "--repo", stop.repository,
                ]],
            )
            return
        if choice == "retry":
            self.merge_one(stop, then=then)
            return
        if choice == "queue":
            self._queue_automerge(stop, then=then)
            return
        if choice == "handoff":
            self.notify(
                f"{stop.key}: exceptional manual conflict handoff; no bypass offered.",
                severity="warning",
            )
        # "skip", esc, and the exceptional handoff all continue the batch with
        # the stop still selected and still marked failed.
        if then:
            then()

    def action_reject(self) -> None:
        stop = self.current
        if not stop:
            return
        body_file = os.path.join(os.path.dirname(TRACE_PATH), "reject.md")
        os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
        with open(body_file, "w", encoding="utf-8") as sink:
            sink.write(
                "Closing after maintainer review; see the review notes above.\n"
            )
        self.mutate_all(
            stop,
            [
                [
                    "gh", "pr", "comment", str(stop.number),
                    "--repo", stop.repository, "--body-file", body_file,
                ],
                ["gh", "pr", "close", str(stop.number), "--repo", stop.repository],
            ],
        )

    def action_handoff(self) -> None:
        """Copy the stop's identity, live evidence, and cluster verdicts to
        the reviewer's clipboard (OSC 52 through the attached terminal), so
        the review context can be handed to an issue, a chat, or another
        agent. Read-only."""
        stop = self.current
        if not stop:
            return
        live = stop.live
        lines = [
            f"{stop.key} — {stop.title}",
            f"https://github.com/{stop.repository}/pull/{stop.number}",
            f"queue says: {stop.action}",
        ]
        if live:
            lines.append(
                f"state: {live.get('state', '?')}  "
                f"head: {str(live.get('headRefOid', ''))[:12]}  "
                f"draft: {live.get('isDraft', '?')}  "
                f"review: {live.get('reviewDecision') or '-'}  "
                f"merge: {live.get('mergeable', '?')}/{live.get('mergeStateStatus', '?')}"
            )
            issues = ", ".join(
                f"#{r['number']}" for r in (live.get("closingIssuesReferences") or [])
            )
            if issues:
                lines.append(f"linked issues: {issues}")
        dupes, overlaps = self.cluster(stop)
        if dupes:
            lines.append("duplicates:")
            for near in dupes:
                lines.append(
                    f"  #{near['number']} {near['title']} "
                    f"(by {near['author']}, {near['why']})"
                )
        if overlaps:
            lines.append(
                "overlaps (ordering hazard): "
                + ", ".join(f"#{near['number']}" for near in overlaps[:6])
            )
        self.copy_to_clipboard("\n".join(lines))
        self.notify(
            f"handoff for {stop.key} copied (OSC 52; the terminal must support it)."
        )

    def action_resolve_cluster(self) -> None:
        stop = self.current
        if not stop:
            return
        if not self._queueable(stop):
            return
        dupes = [near["number"] for near in self.cluster(stop)[0]]
        if not dupes:
            self.notify("no duplicates in the open set; nothing to resolve.")
            return

        def close_next(remaining: list[int]) -> None:
            if not remaining:
                self.notify("cluster resolved; recheck linked issues by hand.")
                return
            dup, rest = remaining[0], remaining[1:]
            body_file = os.path.join(os.path.dirname(TRACE_PATH), f"superseded-{dup}.md")
            with open(body_file, "w", encoding="utf-8") as sink:
                sink.write(
                    f"Superseded by #{stop.number}, which is queued for Hive "
                    "auto-merge. Closing as a duplicate; the surviving change "
                    "lands there.\n"
                )
            dup_stop = Stop(stop.repository, dup, "close", "duplicate")
            self.mutate_all(
                dup_stop,
                [
                    [
                        "gh", "pr", "comment", str(dup),
                        "--repo", stop.repository, "--body-file", body_file,
                    ],
                    ["gh", "pr", "close", str(dup), "--repo", stop.repository],
                ],
                then=lambda: close_next(rest),
            )

        os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
        self._queue_automerge(stop, then=lambda: close_next(dupes))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="review-queue",
        description="The Bluefin maintainer review dashboard.",
    )
    parser.add_argument(
        "--action",
        default="",
        help="only this recommended_action (default: every action)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every action (the default; kept so existing commands still work)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="only this repository (short name or owner/repo)",
    )
    parser.add_argument("--live-repo", default="", help="read open pull requests from owner/repo")
    parser.add_argument("--url", default=QUEUE_URL, help="read the queue from elsewhere")
    args = parser.parse_args()
    filters = QueueFilters(
        action="" if args.all else args.action,
        repository=args.repo,
        live_repository=args.live_repo,
        url=args.url,
    )
    ReviewDashboard(filters).run()


if __name__ == "__main__":
    main()
