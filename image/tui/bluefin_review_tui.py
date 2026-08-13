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
import json
import os
import re
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
)
from review_result import ReviewResult, adapt_current_engine
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from harness.codex import CodexHarness
from harness.autopilot import (HarnessOption, Preference, can_remember,
                               choose_option, discover_all, load_preferences,
                               remember_success)
from review_evidence_manifest import ReviewRequest
from harness.registry import Availability

QUEUE_URL = os.environ.get(
    "BLUEFIN_REVIEW_QUEUE_URL",
    "https://projectbluefin.github.io/review/queue.json",
)
TRACE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "bluefin-review",
    "trace.jsonl",
)
PULL_FETCH_LIMIT = os.environ.get("BLUEFIN_REVIEW_PULL_LIMIT", "200")
MUTATION_TIMEOUT = 60
HIVE_TIMEOUT = 15
# The label Hive's governor sweep scans for. It is not defined in most
# repositories, and adding a label that does not exist fails.
QUEUE_LABEL = "lgtm"
# Match the label the factory repositories that already have it use, rather
# than minting a second look for the same thing.
QUEUE_LABEL_COLOUR = "238636"
QUEUE_LABEL_DESCRIPTION = "This PR has been approved by a maintainer"

# The key map, split by what a key costs you. Nothing on the first line
# changes anything on GitHub; everything on the second goes through the
# typed-number gate.
KEYS_READING = (
    " [b]r[/b] review [b]v[/b] diff [b]o[/b] open [b]h[/b] handoff"
    " [b]/[/b] steer [b]f[/b] filter [b]b[/b] batch [b]H[/b] hive"
    " [b]R[/b] refresh [b]q[/b] quit"
)
KEYS_ACTING = (
    " [b]L[/b] leave review [b]a[/b] approve+queue [b]m[/b] merge"
    " [b]u[/b] update [b]x[/b] reject [b]M[/b] dupes"
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

# Ghost Cluster build dispatch and the docs-update agent task are tracked
# work, not silent stubs; the handlers below name the issue.
GHOST_BUILD_ISSUE = "projectbluefin/review#133"
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


def hive_get(path: str) -> dict:
    """Read one hub endpoint. Read-only, and never fatal.

    Consulting Hive must not be able to break the dashboard: an unreachable
    or unauthenticated hub is reported as unreachable, not raised. Hive
    remains the sole authority for assigning contributor tasks — nothing here
    claims, reorders, or declines any of them.
    """
    base = hive_api_base()
    token = os.environ.get("GH_TOKEN", "")
    if not base or not token:
        return {}
    request = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HIVE_TIMEOUT) as response:
            return json.load(response)
    except Exception:
        return {}


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


def effective_check_state(snapshot: str, live: dict) -> str:
    """Prefer fetched check evidence, retaining the snapshot when absent."""
    checks = live.get("statusCheckRollup") or []
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
    url: str = QUEUE_URL

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
    live: dict = field(default_factory=dict)
    overlap: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def batchable(self) -> bool:
        return dependency_subject(self.title) is not None


def live_review_context(live: dict) -> dict:
    checks = live.get("statusCheckRollup") or []
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
    return {
        "ci": ci,
        "mergeable": live.get("mergeable") or "?",
        "merge_state": live.get("mergeStateStatus") or "?",
        "head": str(live.get("headRefOid") or "?")[:12],
        "draft": live.get("isDraft", "?"),
    }


def live_review_verification(live: dict) -> list[dict]:
    records = []
    passed = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    failed = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    for index, item in enumerate(live.get("statusCheckRollup") or [], start=1):
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


class ConfirmMutation(ModalScreen[bool]):
    """The single mutation gate: show the exact commands, require the typed
    pull request number. Empty, wrong, or Esc aborts; there is no y/yes and
    no timeout.

    One decision gates one sequence. Queueing a pull request is an approval
    plus the lgtm label the sweep scans for, and reject is a comment plus a
    close: splitting either into two gates asks a maintainer to confirm the
    same decision twice, which trains them to type the number without reading
    it. Every command that will run is shown here, before the one gate.
    """

    BINDINGS = [Binding("escape", "dismiss(False)", "abort")]

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

    BINDINGS = [Binding("escape", "dismiss(None)", "keep it queued")]

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
            choices.append(("browser", "open it — the conflict needs a human"))
        choices.append(("retry", "try the merge again"))
        choices.append(("skip", "leave it queued and move on"))
        return choices

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"{self.stop_record.key} did not merge:")
            yield Static(escape(self.message[:300]), classes="confirm-command")
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

    BINDINGS = [Binding("escape", "dismiss(None)", "close")]

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


class ReviewBody(ModalScreen[str | None]):
    """The review body. Required for anything but a bare approval."""

    BINDINGS = [Binding("escape", "dismiss(None)", "close")]

    def __init__(self, verdict: str) -> None:
        super().__init__()
        self.verdict = verdict

    def compose(self) -> ComposeResult:
        optional = " (empty is allowed for an approval)" if self.verdict == "approve" else ""
        with Vertical(id="confirm-box"):
            yield Label(f"{self.verdict} — say why{optional}:")
            yield Input(id="review-body-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)


class DiffScreen(ModalScreen[None]):
    """The diff, in colour, scrollable, and whole.

    The old viewer pasted `gh pr diff` into the evidence pane as plain text,
    cut at 20 000 characters with no indication it had been cut. On a real
    pull request that is a wall of grey in which `+` and `-` are one character
    of difference, and the part you needed was as likely to be past the cut as
    not. Reading the diff is the review, so it gets the screen, Pygments'
    diff lexer, and every byte GitHub returned.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
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

    @work(thread=True)
    def load_diff(self) -> None:
        stop = self.stop_record
        result = gh("pr", "diff", str(stop.number), "--repo", stop.repository)
        text = result.stdout if result.returncode == 0 else result.stderr
        self.app.call_from_thread(self.render_diff, text)

    def render_diff(self, text: str) -> None:
        body = self.query_one("#diff-body", Static)
        if not text.strip():
            body.update("(empty diff)")
            return
        note = ""
        if len(text) > self.MAX_CHARS:
            note = (
                f"\n… truncated at {self.MAX_CHARS:,} of {len(text):,} characters; "
                "open it in the browser with [o] to read the rest.\n"
            )
            text = text[: self.MAX_CHARS]
        # 'ansi_dark' resolves to the terminal's own palette, so the diff
        # stays legible in whatever theme the maintainer actually uses
        # instead of assuming a dark background.
        self.rendered = Syntax(
            text + note, "diff", theme="ansi_dark", word_wrap=False
        )
        body.update(self.rendered)


class HarnessTakeoff(ModalScreen[str | None]):
    """One explicit maintainer choice before a selected harness starts."""

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

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
        Binding("escape", "close", "close"),
        Binding("q", "close", "close"),
        Binding("x", "stop", "stop review"),
        Binding("L", "leave_review", "leave a review"),
        Binding("a", "queue", "approve and queue"),
        Binding("m", "merge_now", "merge now"),
        Binding("u", "update_branch", "update branch"),
        Binding("e", "toggle_evidence", "raw evidence"),
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
        yield RichLog(highlight=False, markup=False, wrap=True, id="review-log")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#review-status", Static).add_class("running")
        self.run_review()

    @work(thread=True)
    def run_review(self) -> None:
        stop = self.stop_record
        if ACTIVE_BACKEND == "codex":
            base_sha = str(stop.live.get("baseRefOid") or "")
            head_sha = str(stop.live.get("headRefOid") or "")
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
        if ACTIVE_BACKEND == "codex":
            base_sha = str(stop.live.get("baseRefOid") or "")
            head_sha = str(stop.live.get("headRefOid") or "")
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
                    live_review_verification(stop.live), result.provenance,
                    stop.overlap, live_review_context(stop.live), result.raw_evidence,
                )
        else:
            result = adapt_current_engine(
                "\n".join(self.output), code,
                {"backend": os.environ.get("GOOSE_PROVIDER", "goose"),
                 "model": os.environ.get("GOOSE_MODEL", "gpt-5.6-luna"),
                 "repository": stop.repository, "pull_request": stop.number},
                verification=live_review_verification(stop.live),
                overlap=stop.overlap,
                live=live_review_context(stop.live),
            )
        if ACTIVE_BACKEND == "codex" and len(str(stop.live.get("baseRefOid") or "")) == 40 and len(str(stop.live.get("headRefOid") or "")) == 40:
            request = ReviewRequest(
                *stop.repository.split("/", 1), stop.number,
                stop.live["baseRefOid"], stop.live["headRefOid"],
                actor="maintainer", tenant="review", generated_at="dashboard",
            )
            if can_remember(result, request):
                remember_success(
                    load_preferences(), stop.repository,
                    Preference("codex", result.provenance.get("model", "gpt-5.6-luna"),
                               result.provenance.get("reasoning_effort", "low")),
                )
        if error:
            outcome, state = "error", f"FAILED to start: {error}"
        elif self.stop_requested:
            outcome, state = "stopped", "STOPPED — you cancelled it. Nothing was submitted."
        elif code is not None and code < 0:
            outcome, state = "stopped", "STOPPED — the review was killed. Nothing was submitted."
        elif result.state in ("complete", "findings"):
            outcome = "complete"
            state = "COMPLETE — a Review Draft for you to judge. Nothing was submitted."
        elif result.state == "incomplete":
            outcome = "incomplete"
            state = (
                "INCOMPLETE — part of this review returned no verdict. "
                "Its finding count is not a clean bill of health."
            )
        elif result.state == "unparsable":
            outcome = "incomplete"
            state = "UNPARSABLE — the review output is not a clean result."
        else:
            outcome = "failed"
            state = f"FAILED (exit {code}) — the review did not run. Nothing was submitted."

        status = self.query_one("#review-status", Static)
        status.remove_class("running")
        status.add_class(outcome)
        status.update(
            f" {link(stop.key, pr_url(stop.repository, stop.number))} — "
            f"{escape(state)} ({elapsed}s) — {escape('[escape]')} closes"
        )
        finding_total = sum(result.counts.values())
        headline = (
            "No evidenced findings."
            if result.is_clean else
            f"{finding_total} evidenced finding{'s' if finding_total != 1 else ''}."
            if result.state == "findings" else
            "No clean decision: inspect raw evidence."
        )
        lines = [
            f"{result.state.upper()}  {escape(stop.key)} — {headline}",
            "severity  "
            + "  ".join(
                f"{key}:{result.counts[key]}"
                for key in ("critical", "high", "medium", "low")
            ),
        ]
        for finding in result.findings[:5]:
            lines.append(
                f"{finding['severity'].upper()}  "
                f"{escape(finding.get('file', '?'))}:{finding.get('line', '?')}  "
                f"{escape(finding.get('title', ''))}"
            )
        verified = sum(1 for item in result.verification if item.get("state") == "verified")
        unverified = sum(1 for item in result.verification if item.get("state") == "unverified")
        lines.append(
            f"checks  {verified} verified / {unverified} unverified / "
            f"{len(result.verification)} reported"
        )
        duplicates = result.overlap.get("duplicates") or []
        overlaps = result.overlap.get("overlaps") or []
        lines.append(f"overlap {len(duplicates)} duplicate / {len(overlaps)} shared-file hazard")
        lines.append(
            f"live     CI {result.live.get('ci', 'unknown')} · merge "
            f"{escape(result.live.get('mergeable', '?'))}/"
            f"{escape(result.live.get('merge_state', '?'))} · "
            f"head {escape(result.live.get('head', '?'))}"
        )
        lines.append(
            f"source  {escape(result.provenance.get('backend', '?'))} / "
            f"{escape(result.provenance.get('model', '?'))}"
        )
        lines.append(
            "actions  "
            f"{escape('[L]')} review  {escape('[a]')} approve+queue  "
            f"{escape('[m]')} merge  {escape('[u]')} update  "
            f"{escape('[e]')} evidence"
        )
        self.query_one("#review-card", Static).update("\n".join(lines))
        self.query_one("#review-log", RichLog).add_class("hidden")
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
    #details { height: 60%; border: solid $secondary; padding: 0 1; }
    #context { height: 40%; border: solid $secondary; padding: 0 1; }
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
    #review-log.hidden { display: none; }
    #diff-scroll { border: solid $secondary; background: $surface; }
    #diff-body { padding: 0 1; width: auto; }
    ListItem.selected Label { color: magenta; text-style: bold; }
    #review-status { height: auto; padding: 0 1; background: $panel; }
    #review-status.running { background: $panel; color: cyan; }
    #review-status.complete { background: $success; color: $text; text-style: bold; }
    #review-status.incomplete { background: $warning; color: $text; text-style: bold; }
    #review-status.failed, #review-status.error, #review-status.stopped {
        background: $error; color: $text; text-style: bold;
    }
    #review-log { border: solid $secondary; }
    #takeoff-box { border: heavy cyan; background: $surface; width: 80%; height: auto; padding: 1 2; margin: 4 4; }
    """

    BINDINGS = [
        Binding("r", "review", "start a review"),
        Binding("L", "leave_review", "leave a review"),
        Binding("b", "batch", "batch select"),
        Binding("d", "docs", "update docs"),
        Binding("g", "ghost_build", "ghost build"),
        Binding("o", "open_browser", "open"),
        Binding("v", "view_diff", "diff"),
        Binding("c", "comment", "comment"),
        Binding("a", "merge", "approve and queue"),
        Binding("m", "merge_now", "merge now"),
        Binding("x", "reject", "reject"),
        Binding("h", "handoff", "handoff"),
        Binding("slash", "steer", "steer review"),
        Binding("f", "filter", "filter"),
        Binding("H", "hive", "ask hive"),
        Binding("R", "refresh", "refresh"),
        Binding("f5", "refresh", "refresh", show=False),
        Binding("u", "update_branch", "update branch"),
        Binding("M", "resolve_cluster", "resolve dupes", show=False),
        Binding("q", "quit", "quit"),
    ]

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
        # Repository -> whether the sweep's `lgtm` label exists there. It does
        # not exist in most repositories, and `gh pr edit --add-label` fails
        # on a label that was never defined.
        self.queue_label_exists: dict[str, bool] = {}
        # Keys to re-select after a refresh: a refresh that silently empties
        # the batch you spent a minute building is worse than no refresh.
        self.reselect: set[str] = set()
        self.all_items: list[dict] = []
        self.harness_state = "CHECKING"
        self.harness_options: list[HarnessOption] = []

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading queue…", id="status-bar")
        yield Static("Harness Autopilot — CHECKING…", id="harness-status")
        with Horizontal():
            with Vertical(id="queue-pane"):
                yield ListView(id="queue")
            with Vertical(id="right-pane"):
                yield Static("", id="details")
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
        if not status:
            self.call_from_thread(self.hive_loaded, "unreachable", [])
            return
        contributors = hive_get("/api/v1/contributors").get("contributors", [])
        workers = [
            {
                "login": contributor.get("github_username", "?"),
                "task": contributor.get("current_task") or {},
            }
            for contributor in contributors
            if contributor.get("current_task")
        ]
        state = (
            f"{status.get('hub', 'online')} · "
            f"{status.get('actionable_items', '?')} actionable · "
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
        if event.key == "escape" and self.focused is self.query_one("#steer", Input):
            event.stop()
            self.query_one("#queue", ListView).focus()

    # ── data layer (walker parity) ────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def load_queue(self) -> None:
        who = gh("api", "user", "--jq", ".login")
        self.self_login = who.stdout.strip() if who.returncode == 0 else ""
        with urllib.request.urlopen(self.filters.url, timeout=60) as response:
            snapshot = json.load(response)
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
        if self.reselect:
            for stop in stops:
                stop.selected = stop.key in self.reselect
            self.reselect = set()
        self.populate(stops)

    def row_markup(self, stop: Stop) -> str:
        tag = " (BATCHABLE)" if stop.batchable else ""
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
            f"{link(stop.key, pr_url(stop.repository, stop.number))}: "
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
        freshness = self.generated_at or "unknown"
        shown = len(self.stops)
        total = len(self.snapshot_items)
        scope = self.filters.action or "all"
        # Say how much of the queue is hidden. A filtered view that looks like
        # the whole queue is how a maintainer concludes there are five open
        # pull requests when there are a hundred and twenty-one.
        held_back = f" (of {total}; [f] widens)" if shown != total else ""
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
            f"| snapshot {freshness} | as {self.self_login or 'unknown'} "
            f"| batch: {selected}{stuck} | Hive: {self.hive_state or 'asking…'}"
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
            "reviewDecision,additions,deletions,changedFiles,updatedAt,"
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
        if stop.repository not in self.queue_label_exists:
            probe = gh(
                "api", f"repos/{stop.repository}/labels/{QUEUE_LABEL}", "--jq", ".name"
            )
            self.queue_label_exists[stop.repository] = probe.returncode == 0
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
        checks = live.get("statusCheckRollup") or []
        outcomes = [c.get("conclusion") or c.get("state") or "PENDING" for c in checks]
        ok = sum(1 for o in outcomes if o in ("SUCCESS", "NEUTRAL", "SKIPPED"))
        bad = sum(1 for o in outcomes if o in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"))
        pending = len(outcomes) - ok - bad
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
            f"checks   {ok} ok, {bad} failed, {pending} pending\n"
            f"{reviews_block}\n"
            f"linked   {issues}\n"
            f"labels   {labels}"
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
        self, stop: Stop, commands: list[list[str]], then=None, on_error=None
    ) -> None:
        """Run a sequence of gh mutations behind one typed-number gate.

        The sequence is the unit a maintainer decides on, so it is confirmed
        once and then runs to completion off the UI thread. A failed step
        stops the rest: half a queueing is reported, never re-confirmed.
        """
        if not commands:
            return

        def finish(confirmed: bool | None) -> None:
            if not confirmed:
                self.notify("aborted; nothing was run.", severity="warning")
                return
            self.notify(f"running: {' '.join(commands[0][:4])}…")
            self.run_mutations(stop, commands, then, on_error)

        self.push_screen(ConfirmMutation(commands, str(stop.number)), finish)

    @work(thread=True)
    def run_mutations(
        self, stop: Stop, commands: list[list[str]], then, on_error=None
    ) -> None:
        """Execute a confirmed sequence off the UI thread. A slow or hung gh
        call must never freeze the dashboard, so each step is bounded by
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
            trace(
                {
                    "repo": stop.repository,
                    "number": stop.number,
                    "argv": command,
                    "exit": result.returncode,
                }
            )
            if result.returncode != 0:
                message = result.stderr.strip()[:200] or f"exit {result.returncode}"
                self.call_from_thread(
                    self.mutation_failed, stop, command, message, on_error
                )
                return
        self.call_from_thread(self.mutations_finished, stop, commands, then)

    def mutation_failed(
        self, stop: Stop, command: list[str], message: str, on_error=None
    ) -> None:
        self.pulls_cache.pop(stop.repository, None)
        self.notify(f"{' '.join(command[:4])}…: {message}", severity="error")
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

    def action_batch(self) -> None:
        stop = self.current
        if not stop:
            return
        stop.selected = not stop.selected
        item = self.query_one("#queue", ListView).highlighted_child
        if item:
            item.set_class(stop.selected, "selected")
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

            def with_body(body: str | None) -> None:
                if body is None:
                    return
                body = body.strip()
                if not body and verdict != "approve":
                    self.notify(
                        f"{verdict} needs a reason; nothing was submitted.",
                        severity="warning",
                    )
                    return
                os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
                body_file = os.path.join(
                    os.path.dirname(TRACE_PATH), f"review-{stop.number}.md"
                )
                with open(body_file, "w", encoding="utf-8") as sink:
                    sink.write((body or "Reviewed.") + "\n")
                self.mutate_all(
                    stop,
                    [[
                        "gh", "pr", "review", str(stop.number),
                        "--repo", stop.repository, f"--{verdict}",
                        "--body-file", body_file,
                    ]],
                )

            self.push_screen(ReviewBody(verdict), with_body)

        self.push_screen(ReviewVerdict(), with_verdict)

    def action_refresh(self) -> None:
        """Re-read the queue snapshot and ask Hive again.

        The snapshot is regenerated every 15 minutes and a session outlives
        that easily; merging or updating a branch invalidates it immediately.
        Relaunching the dashboard to see current state is not a workflow.
        """
        self.reselect = {stop.key for stop in self.stops if stop.selected}
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

    def action_leave_review(self) -> None:
        stop = self.current
        if stop:
            self.leave_review(stop)

    def action_docs(self) -> None:
        self.notify(f"docs-update agent task is tracked as {DOCS_UPDATE_ISSUE}")

    def action_ghost_build(self) -> None:
        self.notify(f"Ghost Cluster build dispatch is tracked as {GHOST_BUILD_ISSUE}")

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
            BINDINGS = [Binding("escape", "dismiss(None)", "close")]

            def compose(self) -> ComposeResult:
                with Vertical(id="confirm-box"):
                    yield Label("comment (empty aborts):")
                    yield Input(id="comment-input")

            def on_mount(self) -> None:
                self.query_one(Input).focus()

            def on_input_submitted(self, event: Input.Submitted) -> None:
                self.dismiss(event.value or None)

        def with_body(body: str | None) -> None:
            if not body:
                return
            os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
            body_file = os.path.join(os.path.dirname(TRACE_PATH), "comment.md")
            with open(body_file, "w", encoding="utf-8") as sink:
                sink.write(body + "\n")
            self.mutate(
                stop, "pr", "comment", str(stop.number),
                "--repo", stop.repository, "--body-file", body_file,
            )

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
        """Queue for Hive auto-merge: post the exact approval the governor
        sweep re-verifies, then add the label it scans for. The sweep enforces
        the self-merge ban, requires green CI, and squash-merges.

        The label does not exist in most repositories, and adding one that was
        never defined fails — which used to leave the pull request formally
        approved for an auto-merge that could never be picked up, because the
        approval had already been submitted by then. The label is created
        first when it is missing, so the sequence cannot end half-applied, and
        the whole thing is one decision behind one gate.
        """
        body = f"Approved by @{self.self_login} for Hive auto-merge on green CI."
        commands: list[list[str]] = []
        if not self.queue_label_exists.get(stop.repository, True):
            commands.append([
                "gh", "label", "create", QUEUE_LABEL,
                "--repo", stop.repository,
                "--color", QUEUE_LABEL_COLOUR,
                "--description", QUEUE_LABEL_DESCRIPTION,
            ])
        commands.append([
            "gh", "pr", "review", str(stop.number),
            "--repo", stop.repository, "--approve", "--body", body,
        ])
        commands.append([
            "gh", "pr", "edit", str(stop.number),
            "--repo", stop.repository, "--add-label", QUEUE_LABEL,
        ])

        def queued() -> None:
            stop.failure = ""
            self.queue_label_exists[stop.repository] = True
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

        self.mutate_all(stop, commands, then=queued, on_error=failed)

    def action_merge(self) -> None:
        batch = [s for s in self.stops if s.selected]
        if not batch and self.current:
            batch = [self.current]

        queue: list[Stop] = []
        for stop in batch:
            if not stop.live:
                self.notify(f"{stop.key}: no live evidence yet; select it first.")
                continue
            if self._queueable(stop):
                queue.append(stop)

        def queue_next(index: int = 0) -> None:
            if index >= len(queue):
                if len(queue) > 1:
                    self.notify(f"batch queued: {len(queue)} PRs.")
                return
            self._queue_automerge(
                queue[index],
                then=lambda next_index=index + 1: queue_next(next_index),
            )

        queue_next()

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
            stop.selected = False
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
        if choice == "browser":
            gh("pr", "view", str(stop.number), "--repo", stop.repository, "--web")
        # "skip", esc, and the browser hand-off all continue the batch with
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
    parser.add_argument("--url", default=QUEUE_URL, help="read the queue from elsewhere")
    args = parser.parse_args()
    filters = QueueFilters(
        action="" if args.all else args.action,
        repository=args.repo,
        url=args.url,
    )
    ReviewDashboard(filters).run()


if __name__ == "__main__":
    main()
