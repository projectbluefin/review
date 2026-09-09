"""Bluefin Review Dashboard — the maintainer surface for the PR queue.

GitHub supplies the queue and the live evidence — one paginated GraphQL
search over the organization's open pull requests, never a static snapshot.
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
import logging
import logging.handlers
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from rich.syntax import Syntax
from textual import work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Button,
    Static,
    Select,
    TextArea,
)
try:
    from textual.worker import get_current_worker
except ModuleNotFoundError:  # minimal non-Textual import contracts
    def get_current_worker():
        return type("_NoWorker", (), {"is_cancelled": False})()

# image/ is the single import root: every sibling is spelled tui.*/harness.*.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tui.review_result import ReviewResult, adapt_current_engine
from tui.semantic_view import DecisionState, build_decision_card
from tui import action_plan
from tui import landing
from tui import lab_client
from tui import hive_api
from harness.codex import CodexHarness
from harness.goose import GooseHarness
from harness.autopilot import (HarnessOption, Preference, can_remember,
                               choose_option, discover_all, load_preferences,
                               remember_success)
from tui.review_evidence_manifest import ReviewRequest
from tui.re_review import (DeltaInput, FindingEvidence, H1Evidence, PriorFinding,
                           Region, classify_head_delta)
from harness.registry import Availability, DraftRequest, DraftState, HarnessRegistry
from tui.headroom import HeadroomSession
from tui.review_cache import ReviewCache
from tui.review_receipt import ReviewReceipt
from tui.observability import ReviewObservability
from tui.review_run import ReviewRun
from tui.run_state import (
    FULL_SHA,
    IllegalRunTransition,
    REVIEW_PROVIDER_TERMINALS,
    RunIdentity,
    RunRecord,
    RunState,
    RunStateStore,
    TerminalOutcome,
)
ReceiptIdentity = RunIdentity
from tui.gh_client import (
    BreakerRegistry,
    BreakerState,
    Dependency,
    GhClient,
    default_client,
    get_breaker,
    gh as gh_client_read,
    run_mutation as gh_client_run_mutation,
)

from tui.model_profiles import (
    HIGH_ASSURANCE_MODELS,
    classify_batch,
    escalation_triple,
    is_high_assurance,
    is_low_risk,
)

if TYPE_CHECKING:
    from tui.review_engine import ReviewBatch, ReviewEvent
    from tui.review_snapshot import BatchReviewItem, BatchSnapshot

GITHUB_ORG = "projectbluefin"
# The queue is one paginated GraphQL search: every open pull request in the
# organization, with the review, mergeability, and CI-rollup evidence the
# recommended action is classified from. There is no static snapshot.
ORG_QUEUE_QUERY = """\
query($endCursor: String) {
  search(query: "org:projectbluefin is:pr is:open archived:false", type: ISSUE, first: 100, after: $endCursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        updatedAt
        author { login }
        repository { nameWithOwner }
        labels(first: 100) { nodes { name } }
        reviewDecision
        baseRefOid
        headRefOid
        mergeable
        isDraft
        isCrossRepository
        maintainerCanModify
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        reviews(first: 50) { nodes { author { login } state authorAssociation } }
      }
    }
  }
}
"""
# The issues search query: every open issue in the organization.
ORG_ISSUES_QUERY = f"""\
query($endCursor: String) {{
  search(query: "org:{GITHUB_ORG} is:issue is:open archived:false", type: ISSUE, first: 100, after: $endCursor) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on Issue {{
        number
        title
        updatedAt
        createdAt
        author {{ login }}
        repository {{ nameWithOwner }}
        labels(first: 100) {{ nodes {{ name }} }}
        comments {{ totalCount }}
        body
      }}
    }}
  }}
}}
"""
PULL_FETCH_LIMIT = os.environ.get("BLUEFIN_REVIEW_PULL_LIMIT", "200")
TRACE_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "bluefin-review",
    "trace.jsonl",
)
TRACE_MAX_BYTES = int(os.environ.get("BLUEFIN_REVIEW_TRACE_MAX_BYTES", 8 * 1024 * 1024))
TRACE_BACKUP_COUNT = int(os.environ.get("BLUEFIN_REVIEW_TRACE_BACKUPS", "3"))
_TRACE_LOGGER: logging.Logger | None = None
_TRACE_LOGGER_LOCK = threading.Lock()
# Bounds for in-memory collections that would otherwise grow for the
# lifetime of an unattended run. Each is a display or scheduling cache;
# the durable record of a run is RunStateStore, which bounds itself.
MAX_REVIEW_BATCHES = int(os.environ.get("BLUEFIN_REVIEW_MAX_BATCHES", "50"))
MAX_TRIAGE_ENTRIES = int(os.environ.get("BLUEFIN_REVIEW_MAX_TRIAGE", "2000"))
MAX_MERGE_RIGHTS_ENTRIES = int(os.environ.get("BLUEFIN_REVIEW_MAX_MERGE_RIGHTS", "500"))
MAX_LANDING_QUEUE = int(os.environ.get("BLUEFIN_REVIEW_MAX_LANDING_QUEUE", "200"))
MAX_REVIEW_OUTPUT_LINES = int(os.environ.get("BLUEFIN_REVIEW_MAX_OUTPUT_LINES", "200000"))
MAX_ACTIVITY_ROWS = 8
MAX_ACTIVITY_WORK_KEYS = 2
MAX_ACTIVITY_TEXT = 96
MAX_RECENT_MERGES = 3
PERSISTED_PR_KEY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+#[1-9][0-9]*"
)
MUTATION_TIMEOUT = 60
HIVE_TIMEOUT = 15
MAX_CONCURRENT_LANDINGS = int(
    os.environ.get("BLUEFIN_REVIEW_CONCURRENT_LANDINGS", "6")
)
HIVE_API_HELPER = os.path.join(os.path.dirname(__file__), "hive_api.py")
MAX_REVIEW_BODY_CHARS = 4096
MAX_RE_REVIEW_FILES = 128
MAX_RE_REVIEW_HUNKS = 512
MAX_RE_REVIEW_RESPONSE_CHARS = 1_000_000
MAX_RE_REVIEW_FINDINGS = 12
MAX_RE_REVIEW_NEW_EVIDENCE = 8
SENSITIVE_RE_REVIEW_PATHS = (".github/workflows/",)

SLAY_DELAYS = [0.4, 0.3, 0.25, 0.35]
SLAY_FRAMES = [
    # Frame 1: Round 8 / Fight (400ms)
    (
        "[bold cyan]╔═ BLUEFIN [████████████] 100% ═╗[/]  [bold magenta]╔═ QUEUE HP [████████████] 100% ═╗[/]\n"
        "   \\   /\n"
        "  ( •_•)           ROUND 8                     [8 PRs IN QUEUE]\n"
        "  <)   )╯           FIGHT!                      [■■■■■■■■■■■■■■]\n"
        "   /   \\                                        READY TO REVIEW"
    ),
    # Frame 2: Charge / Combo (300ms)
    (
        "[bold cyan]╔═ BLUEFIN [████████████] 100% ═╗[/]  [bold magenta]╔═ QUEUE HP [████████████] 100% ═╗[/]\n"
        "   \\   /\n"
        "  ( >_<)⚡ ░▒▓█                           [8 PRs IN QUEUE]\n"
        "  <)   )⚡ ░▒▓█   ↓ ↘ → + CLAW            [■■■■■■■■■■■■■■]\n"
        "   /   \\    ⚡    SLAYDOKEN!"
    ),
    # Frame 3: Projectile / Critical hit 9999 (250ms)
    (
        "[bold cyan]╔═ BLUEFIN [████████████] 100% ═╗[/]  [bold red]╔═ QUEUE HP [            ]   0% ═╗[/]\n"
        "   \\   /\n"
        "  ( ✧Д✧) ═══[bold cyan]░▒▓█[/][bold magenta]█████[/][bold pink]█▓▒░[/]═══>     💥 [bold yellow]CRITICAL HIT! 9999![/bold yellow] 💥\n"
        "  <)   )╯                              [bold red]※※※ QUEUE SHATTERED ※※※[/bold red]\n"
        "   /   \\"
    ),
    # Frame 4: K.O. (350ms)
    (
        "[bold yellow]╔═══════════════════════════════════════════════════════════════╗[/]\n"
        "[bold yellow]║                          ★ K. O. ★                            ║[/]\n"
        "[bold yellow]║                     QUEUE FIGHTER DOWN                        ║[/]\n"
        "[bold yellow]╚═══════════════════════════════════════════════════════════════╝[/]\n"
        "                    [dim]░ ▒ ▓ █  debris clearing  █ ▓ ▒ ░[/dim]"
    ),
    # Frame 5: Player 1 Wins / ALL SYSTEMS SLAY (Held)
    (
        "[bold cyan]╔═══════════════════════════════════════════════════════════════╗[/]\n"
        "[bold cyan]║[/]                      [bold yellow]★ K. O. ★[/]                                [bold cyan]║[/]\n"
        "[bold cyan]║[/]                     [bold green]QUEUE SLAIN[/]                               [bold cyan]║[/]\n"
        "[bold cyan]║[/]                    [bold white]PLAYER 1 WINS[/]                              [bold cyan]║[/]\n"
        "[bold cyan]║[/]                                                               [bold cyan]║[/]\n"
        "[bold cyan]║[/]           [bold magenta]░▒▓█[/] [bold bright_cyan]ALL SYSTEMS SLAY[/] [bold magenta]█▓▒░[/]                          [bold cyan]║[/]\n"
        "[bold cyan]╚═══════════════════════════════════════════════════════════════╝[/]\n"
        "              [bold green]✓[/] [dim]Review queue fully drained. All clear.[/dim]"
    ),
]

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
    CommandSpec("select_all", "B", "select_all", "select/clear visible rows"),
    CommandSpec("toggle_advance", "space", "toggle_advance", "toggle and advance"),
    CommandSpec("toggle_view_alias", "I", "toggle_view", "toggle PRs/issues"),
    CommandSpec("next_unreviewed", "n", "next_unreviewed", "next PR lacking my review"),
    CommandSpec("docs", "d", "docs", "update docs"),
    CommandSpec("open_browser", "o", "open_browser", "open"),
    CommandSpec("view_diff", "v", "view_diff", "diff"),
    CommandSpec("view_comments", "C", "view_comments", "comments"),
    CommandSpec("comment", "c", "comment", "comment", mutating=True),
    CommandSpec("approve_or_land", "a", "merge", "approve+queue", mutating=True),
    CommandSpec("land_batch", "A", "land_batch", "land batch", mutating=True),
    CommandSpec("agents", "w", "agents", "watch batches"),
    CommandSpec("review_policy", "P", "review_policy", "final review policy"),
    CommandSpec("merge_now", "m", "merge_now", "merge now", mutating=True),
    CommandSpec("reject", "x", "reject", "reject", mutating=True),
    CommandSpec("update_branch", "u", "update_branch", "update clean branch", mutating=True),
    CommandSpec("select_mechanical", "U", "select_mechanical", "select mechanical"),
    CommandSpec("resolve_duplicates", "M", "resolve_cluster", "resolve dupes", mutating=True),
    CommandSpec("filter", "f", "filter", "filter"),
    CommandSpec("hive", "H", "hive", "ask hive"),
    CommandSpec("refresh", "R", "refresh", "refresh"),
    CommandSpec("slay_pr", "$", "slay_pr", "slay (review+fix+land)"),
)


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
    " [b]I[/b] issues/PRs [b]Tab[/b] focus panes"
    " [b]r[/b] review [b]v[/b] diff [b]C[/b] comments [b]o[/b] open [b]h[/b] handoff"
    " [b]/[/b] steer [b]f[/b] filter [b]b[/b]/[b]B[/b] select"
    " [b]Space[/b] select+next [b]n[/b] next lacking my review"
    " [b]w[/b] watch batches"
    " [b]P[/b] review policy [b]H[/b] hive"
    " [b]R[/b] refresh [b]q[/b]/Esc back"
)
KEYS_ACTING = (
    " [b]L[/b] leave review [b]a[/b] approve+queue [b]A[/b] land batch [b]m[/b] merge [b]$[/b] slay"
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
    """The selected hub's HTTPS root.

    The launcher may select a registered deployment, and the image hook
    supplies the default. Token-bearing dashboard requests never use plaintext
    transport or URLs containing user information.
    """
    hub = os.environ.get("HIVE_HUB", "")
    if "," in hub:
        return ""
    if hub.startswith("wss://"):
        http = "https://" + hub[len("wss://") :]
    elif hub.startswith("https://"):
        http = hub
    else:
        return ""
    try:
        parsed = urlsplit(http)
        if not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
    except ValueError:
        return ""
    return http[: -len("/contribute")] if http.endswith("/contribute") else http


# The order a maintainer wants, which is not the order GitHub returns. The
# classifier ranks by how stuck a pull request is; a reviewer opening this
# dashboard wants the ones they can act on now — the merge-ready and the
# reviewable — above the ones waiting on their author or on better evidence.
# A queue that buries what you can land under sixty things you cannot is a
# queue you stop reading.
MAINTAINER_ORDER = [
    "ready-for-human-merge",
    "review",
    "resolve-conflicts",
    "fix-ci",
    "investigate",
    "triage",
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
REVIEW_SCOPE = os.environ.get(
    "BLUEFIN_REVIEW_SCOPE_ROOT", "/opt/bluefin/review-scope"
)
REVIEW_SCOPE_VERSION = os.environ.get(
    "BLUEFIN_REVIEW_SCOPE_VERSION", "image-v1"
)
LIVE_PR_FIELDS = (
    "author,state,baseRefOid,headRefOid,isDraft,mergeable,mergeStateStatus,"
    "reviewDecision,additions,deletions,changedFiles,updatedAt,body,"
    "closingIssuesReferences,statusCheckRollup,labels,reviews,"
    "isCrossRepository,maintainerCanModify"
)

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
    # Bare subprocess.run calls replaced by throttled gh_client.read
    return gh_client_read(*args, timeout=timeout)


def _run_mutation(
    command: list[str] | Sequence[str],
    timeout: int = MUTATION_TIMEOUT,
    idempotent: bool = False,
) -> subprocess.CompletedProcess:
    # Bare subprocess.run(command) replaced by gh_client.run_mutation with deadlines
    return gh_client_run_mutation(command, timeout=timeout, idempotent=idempotent)


def fetch_live_review(repository: str, number: int) -> dict:
    result = gh(
        "pr",
        "view",
        str(number),
        "--repo",
        repository,
        "--json",
        LIVE_PR_FIELDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            bounded_detail(
                (result.stderr or result.stdout).strip()
                or f"GitHub could not read {repository}#{number}"
            )
        )
    try:
        live = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError(
            f"GitHub returned malformed evidence for {repository}#{number}"
        ) from error
    if not isinstance(live, dict):
        raise ValueError(
            f"GitHub returned malformed evidence for {repository}#{number}"
        )
    return live


@dataclass(frozen=True)
class CompareEvidence:
    """Bounded read-only H0..H1 compare evidence for a re-review."""

    regions: tuple[Region, ...] = ()
    mapping_uncertain: bool = False
    sensitive_surfaces_changed: bool = False
    bounded_risk_exceeded: bool = False
    capability_available: bool = True


def compare_hunk_regions(repository: str, old_head: str, new_head: str) -> CompareEvidence:
    """Read the GitHub compare boundary without treating partial data as proof."""
    if not (re.fullmatch(r"[0-9a-f]{40}", old_head) and re.fullmatch(r"[0-9a-f]{40}", new_head)):
        return CompareEvidence(mapping_uncertain=True, capability_available=False)
    try:
        response = gh(
            "api", f"repos/{repository}/compare/{old_head}...{new_head}",
            "--method", "GET", "--field", f"per_page={MAX_RE_REVIEW_FILES}",
            "--field", "page=1", timeout=30,
        )
        if response.returncode != 0 or len(response.stdout) > MAX_RE_REVIEW_RESPONSE_CHARS:
            return CompareEvidence(mapping_uncertain=True, capability_available=False)
        payload = json.loads(response.stdout)
        files = payload.get("files") if isinstance(payload, dict) else None
        total_files = payload.get("total_files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            return CompareEvidence(mapping_uncertain=True)
        if len(files) > MAX_RE_REVIEW_FILES:
            return CompareEvidence(mapping_uncertain=True, bounded_risk_exceeded=True)
        if total_files is not None:
            if (type(total_files) is not int or total_files < 0 or
                    total_files < len(files)):
                return CompareEvidence(mapping_uncertain=True)
            if total_files > len(files):
                return CompareEvidence(
                    mapping_uncertain=True,
                    bounded_risk_exceeded=True,
                )
        elif len(files) >= MAX_RE_REVIEW_FILES:
            return CompareEvidence(
                mapping_uncertain=True,
                bounded_risk_exceeded=True,
            )
        regions: list[Region] = []
        sensitive = False
        uncertain = False
        for item in files:
            if (not isinstance(item, dict) or
                    not isinstance(item.get("filename"), str) or
                    not item["filename"]):
                return CompareEvidence(mapping_uncertain=True)
            filename = item["filename"]
            sensitive = sensitive or filename.startswith(SENSITIVE_RE_REVIEW_PATHS)
            patch = item.get("patch")
            if not isinstance(patch, str):
                uncertain = True
                continue
            hunk_header = re.compile(
                r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$"
            )
            current_hunk: tuple[int, int, int, int, int, int] | None = None
            patch_uncertain = False
            last_body_line = False
            for line in patch.splitlines():
                match = hunk_header.fullmatch(line)
                if match:
                    if current_hunk is not None:
                        old_seen, new_seen = current_hunk[4:6]
                        if (old_seen != current_hunk[1] or
                                new_seen != current_hunk[3]):
                            patch_uncertain = True
                    old_start = int(match.group(1))
                    old_count = int(match.group(2) or 1)
                    new_start = int(match.group(3))
                    new_count = int(match.group(4) or 1)
                    invalid_hunk = (
                        (old_count == 0 and new_count == 0) or
                        (old_start == 0 and old_count != 0) or
                        (new_start == 0 and new_count != 0)
                    )
                    patch_uncertain = patch_uncertain or invalid_hunk or (
                        old_count > 0 and new_count == 0
                    )
                    current_hunk = (old_start, old_count, new_start, new_count, 0, 0)
                    last_body_line = False
                    if new_count and new_start > 0:
                        regions.append(Region(filename, new_start, new_start + new_count - 1))
                    if len(regions) >= MAX_RE_REVIEW_HUNKS:
                        return CompareEvidence(
                            tuple(regions), True, sensitive, True,
                        )
                    continue
                if current_hunk is None:
                    patch_uncertain = True
                    continue
                if line == r"\ No newline at end of file":
                    if not last_body_line:
                        patch_uncertain = True
                    last_body_line = False
                    continue
                if not line or line[0] not in " +-":
                    patch_uncertain = True
                    continue
                old_seen, new_seen = current_hunk[4:6]
                if line[0] == " ":
                    old_seen += 1
                    new_seen += 1
                elif line[0] == "-":
                    old_seen += 1
                else:
                    new_seen += 1
                current_hunk = (*current_hunk[:4], old_seen, new_seen)
                last_body_line = True
                if (old_seen > current_hunk[1] or new_seen > current_hunk[3]):
                    patch_uncertain = True
            if current_hunk is None:
                patch_uncertain = True
            else:
                old_seen, new_seen = current_hunk[4:6]
                patch_uncertain = patch_uncertain or (
                    old_seen != current_hunk[1] or new_seen != current_hunk[3]
                )
            if patch_uncertain:
                uncertain = True
        return CompareEvidence(tuple(regions), uncertain, sensitive)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, json.JSONDecodeError):
        return CompareEvidence(mapping_uncertain=True, capability_available=False)


def bounded_detail(detail: str) -> str:
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", str(detail))
    return " ".join(detail.split())[:240]


def activity_age(timestamp: float | None) -> str:
    """A compact age for a cached dashboard snapshot."""
    if timestamp is None:
        return ""
    seconds = max(0, int(time.monotonic() - timestamp))
    if seconds < 60:
        return "<1m ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


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


def _bound_map(mapping: dict, limit: int) -> None:
    """Drop the oldest entries so a per-run cache cannot grow forever.

    Insertion order is eviction order. These maps are display and
    scheduling caches; the authoritative record is the run store, so an
    evicted entry is recomputed rather than lost.
    """
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))


def _trace_logger() -> logging.Logger:
    """One process-wide, size-capped rotating sink for diagnostics."""
    global _TRACE_LOGGER
    with _TRACE_LOGGER_LOCK:
        if _TRACE_LOGGER is None:
            os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                TRACE_PATH,
                maxBytes=TRACE_MAX_BYTES,
                backupCount=TRACE_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("bluefin_review.trace")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.handlers.clear()
            logger.addHandler(handler)
            _TRACE_LOGGER = logger
        return _TRACE_LOGGER


def trace(record: dict) -> None:
    """Append a JSON trace of a maintainer action for the feedback loop.

    Diagnostics are capped and rotated. Run state lives in the durable
    run store, never here, so losing an old trace segment costs no
    decision the appliance still has to make.
    """
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    _trace_logger().info(json.dumps(record, separators=(",", ":")))


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
        return "red"
    if action == "ready-for-human-merge":
        return "bold green"
    if action in ("review", "triage") or review == "approved":
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
        if not isinstance(check, (dict, Mapping)):
            continue
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


def classify_action(check_state: str, mergeable_state: str, review_state: str) -> str:
    """The queue's recommended action, classified from live GitHub evidence.

    First match wins: a failing check is actionable before a conflict is,
    incomplete evidence is a task of its own, and only a fully green,
    approved pull request is ready for a human merge.
    """
    if check_state == "failure":
        return "fix-ci"
    if mergeable_state == "dirty":
        return "resolve-conflicts"
    if "unknown" in (check_state, mergeable_state, review_state):
        return "investigate"
    if review_state == "approved":
        return "ready-for-human-merge"
    return "review"


def org_queue_item(node: dict) -> dict:
    """One queue item from one GraphQL search node, validated field by field.

    Raises ValueError on any shape GitHub should never send, so a malformed
    response becomes one honest source state instead of a wrong queue.
    """
    number = node.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("a pull request has an invalid number")
    repository_node = node.get("repository")
    if repository_node is not None and not isinstance(repository_node, dict):
        raise ValueError(f"pull request {number} has an invalid repository")
    repository = (repository_node or {}).get("nameWithOwner")
    if not isinstance(repository, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ValueError(f"pull request {number} has an invalid repository")
    if not isinstance(node.get("title"), str):
        raise ValueError(f"{repository}#{number} has an invalid title")
    author_node = node.get("author")
    if author_node is not None and not isinstance(author_node, dict):
        raise ValueError(f"{repository}#{number} has an invalid author")
    author = ""
    if author_node and "login" in author_node:
        if not isinstance(author_node["login"], str):
            raise ValueError(f"{repository}#{number} has an invalid login")
        author = author_node["login"]
    label_nodes = (node.get("labels") or {}).get("nodes") or []
    if not isinstance(label_nodes, list):
        raise ValueError(f"{repository}#{number} has malformed labels")
    labels = []
    for label in label_nodes:
        name = label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str):
            raise ValueError(f"{repository}#{number} has a malformed label")
        labels.append(name)
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = None
    if commits and isinstance(commits[0], dict):
        rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    if rollup is None:
        # No checks at all: nothing is red and nothing is pending, which is
        # the same verdict the per-check evidence path reaches.
        check_state = "success"
    else:
        check_state = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure"}.get(rollup, "unknown")
    mergeable_state = {"MERGEABLE": "clean", "CONFLICTING": "dirty"}.get(node.get("mergeable"), "unknown")
    review_state = "approved" if node.get("reviewDecision") == "APPROVED" else "review_required"
    reviews_raw = node.get("reviews") or {}
    reviews_nodes = (
        reviews_raw.get("nodes", [])
        if isinstance(reviews_raw, dict)
        else (reviews_raw if isinstance(reviews_raw, list) else [])
    )
    return {
        "repository": repository,
        "number": number,
        "title": node["title"],
        "author": author,
        "updated_at": node.get("updatedAt", ""),
        "labels": labels,
        "review_state": review_state,
        "mergeable_state": mergeable_state,
        "check_state": check_state,
        "base_sha": str(node.get("baseRefOid") or ""),
        "head_sha": str(node.get("headRefOid") or ""),
        "isDraft": bool(node.get("isDraft", False)),
        "is_draft": bool(node.get("isDraft", False)),
        "isCrossRepository": bool(node.get("isCrossRepository", False)),
        "is_cross_repository": bool(node.get("isCrossRepository", False)),
        "maintainerCanModify": bool(node.get("maintainerCanModify", True)) if node.get("maintainerCanModify") is not None else True,
        "maintainer_can_modify": bool(node.get("maintainerCanModify", True)) if node.get("maintainerCanModify") is not None else True,
        "reviews": reviews_nodes,
        "recommended_action": classify_action(check_state, mergeable_state, review_state),
    }


def org_issue_item(node: dict) -> dict:
    """One issue item from one GraphQL search node, validated field by field.

    Raises ValueError on any shape GitHub should never send, so a malformed
    response becomes one honest source state instead of a wrong queue.
    """
    number = node.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("an issue has an invalid number")
    repository = (node.get("repository") or {}).get("nameWithOwner")
    if not isinstance(repository, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ValueError(f"issue {number} has an invalid repository")
    if not isinstance(node.get("title"), str):
        raise ValueError(f"{repository}#{number} has an invalid title")
    author_node = node.get("author")
    if author_node is not None and not isinstance(author_node, dict):
        raise ValueError(f"{repository}#{number} has an invalid author")
    author = ""
    if author_node and "login" in author_node:
        if not isinstance(author_node["login"], str):
            raise ValueError(f"{repository}#{number} has an invalid login")
        author = author_node["login"]
    label_nodes = node.get("labels")
    labels = []
    if isinstance(label_nodes, dict):
        raw_labels = label_nodes.get("nodes") or []
    elif isinstance(label_nodes, list):
        raw_labels = label_nodes
    else:
        raw_labels = []
    for label in raw_labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                labels.append(name)
        elif isinstance(label, str):
            labels.append(label)
    comments = node.get("comments")
    comments_count = 0
    if isinstance(comments, dict):
        cnt = comments.get("totalCount")
        if isinstance(cnt, int) and not isinstance(cnt, bool):
            comments_count = cnt
    elif isinstance(comments, (int, float)) and not isinstance(comments, bool):
        comments_count = int(comments)
    elif isinstance(comments, list):
        comments_count = len(comments)
    body = str(node.get("body") or "")
    return {
        "is_issue": True,
        "action": "triage",
        "recommended_action": "triage",
        "repository": repository,
        "number": number,
        "title": node["title"],
        "author": author,
        "labels": labels,
        "comments_count": comments_count,
        "body": body,
        "created_at": str(node.get("createdAt") or ""),
        "updated_at": str(node.get("updatedAt") or ""),
    }


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
    """Which of the queue's items reach the dashboard.

    The launcher passes these straight through, so 'just review-queue --repo
    bluefin' narrows the queue without a second surface to learn.
    """

    action: str = ""
    repository: str = ""
    live_repository: str = ""
    kind: str = "prs"

    @property
    def live(self) -> bool:
        return bool(self.live_repository)

    def wants_repo(self, item: dict) -> bool:
        if self.repository:
            full = item.get("repository", "")
            if full != self.repository and full.split("/")[-1] != self.repository:
                return False
        return True

    def wants_kind(self, item: dict) -> bool:
        is_issue = bool(item.get("is_issue", False))
        if self.kind == "prs":
            return not is_issue
        if self.kind == "issues":
            return is_issue
        return True

    def wants(self, item: dict) -> bool:
        if not self.wants_kind(item):
            return False
        action = item.get("recommended_action") or item.get("action", "")
        if self.action and action != self.action:
            return False
        return self.wants_repo(item)


TriageState = Literal["unseen", "reviewed", "skipped"]


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
    review_status: str = ""
    review_failure: str = ""
    cached_age: str = ""
    head_sha: str = ""
    triage_state: TriageState = "unseen"
    is_issue: bool = False

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def head_identity(self) -> str:
        return self.head_sha or str(self.live.get("headRefOid") or "")

    @property
    def triage_key(self) -> str:
        return f"{self.key}@{self.head_identity}"

    @property
    def mechanical(self) -> str | None:
        """The branch-update reason, from live evidence only."""
        return mechanical_reason(self.author, self.live)


REVIEW_FAILURES = {
    "failed",
    "cancelled",
    "missing",
    "incomplete",
    "unparsable",
    "review_failed",
    "review_missing",
    "review_incomplete",
    "review_unparsable",
}

QUEUE_STATE_RANK = {
    "failed": 0,
    "in progress": 1,
    "queued": 2,
    "ready": 3,
    "done": 4,
    "blocked": 5,
}


def classify_routability(stop: Stop, record: RunRecord | None = None) -> str | None:
    """Pure classifier returning a concrete no-automated-path reason from established evidence."""
    if stop.is_issue:
        return "action applies to pull requests only"

    live = stop.live if isinstance(stop.live, dict) else {}

    # 1. Draft PR
    if live.get("isDraft") is True or getattr(stop, "is_draft", False) is True:
        return "pull request is a draft"

    # 2. Cross-repository / fork PR that cannot be modified
    if live.get("isCrossRepository") is True and live.get("maintainerCanModify") is False:
        return "cross-repository fork cannot be modified by maintainers"

    # 3. Durable current-head mutation failure due to push permission denial
    if (
        record is not None
        and record.state == RunState.MUTATION_FAILED
        and "push permission denied" in (record.reason or "").lower()
    ):
        return "push permission denied"
    if stop.failure and "push permission denied" in stop.failure.lower():
        return "push permission denied"

    # 4. Durable human-review-required state
    if (
        record is not None
        and record.state == RunState.HUMAN_REVIEW_MISSING
    ):
        return "human review required"
    if stop.failure and "no human review" in stop.failure.lower():
        has_human = False
        reviews = live.get("reviews") or []
        if isinstance(reviews, dict):
            reviews = reviews.get("nodes", [])
        if isinstance(reviews, list):
            for r in reviews:
                if isinstance(r, dict):
                    author = r.get("author") or {}
                    login = author.get("login") if isinstance(author, dict) else str(author)
                    if login and not (login.endswith("[bot]") or login.endswith("-bot") or login in {"goose", "github-actions", "copilot"}):
                        st = str(r.get("state") or "").upper()
                        if st in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}:
                            has_human = True
                            break
        if not has_human:
            return "human review required"

    # 5. Durable head-changed state
    if (
        record is not None
        and record.state == RunState.HEAD_CHANGED
    ):
        return "head changed between review and mutation"
    if (
        record is None
        and stop.failure
        and "head changed" in stop.failure.lower()
        and stop.review_status != "unreviewed"
    ):
        if stop.head_identity:
            m = re.search(r"\(reviewed\s+([0-9a-fA-F]+),\s*live\s+([0-9a-fA-F]+)\)", stop.failure)
            if m:
                rev_prefix, live_prefix = m.group(1).lower(), m.group(2).lower()
                head_lower = stop.head_identity.lower()
                if not (head_lower.startswith(rev_prefix) or head_lower.startswith(live_prefix)):
                    return None
        return "head changed between review and mutation"

    # Ordinary transient review failures remain actionable (return None)
    return None


def parse_hive_ready_queue(data: Any) -> list[dict]:
    if isinstance(data, dict):
        q = data.get("queue") or data.get("items")
        if isinstance(q, list):
            return [it for it in q if isinstance(it, dict)]
    elif isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    return []


def parse_hive_triage(data: Any) -> list[dict]:
    if isinstance(data, dict):
        g = data.get("groups") or data.get("stages")
        if isinstance(g, list):
            return [group for group in g if isinstance(group, dict)]
    elif isinstance(data, list):
        return [group for group in data if isinstance(group, dict)]
    return []


def build_hive_rank_map(ready_items: list[dict], triage_groups: list[dict]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    rank = 0
    for it in ready_items:
        repo = it.get("repo") or it.get("repository") or ""
        number = it.get("number")
        if repo and isinstance(number, int) and number > 0:
            key = f"{repo}#{number}"
            if key not in ranks:
                ranks[key] = rank
                rank += 1
    for group in triage_groups:
        issues = group.get("issues") or group.get("items") or []
        if isinstance(issues, list):
            for it in issues:
                if not isinstance(it, dict):
                    continue
                repo = it.get("repo") or it.get("repository") or ""
                number = it.get("number")
                if repo and isinstance(number, int) and number > 0:
                    key = f"{repo}#{number}"
                    if key not in ranks:
                        ranks[key] = rank
                        rank += 1
    return ranks


def clear_review_failure_mark(stop: Stop) -> None:
    stop.review_failure = ""
    if not stop.failure.startswith((
        "review snapshot failed:",
        "review snapshot stale:",
        "review dispatch failed:",
    )):
        return
    stop.failure = ""


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


class _CompositePlan:
    def __init__(self, tasks: list["landing.LandingTask"]) -> None:
        self.tasks = tasks

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):
        return iter(self.tasks)

    def __getitem__(self, index: int):
        return self.tasks[index]

    @property
    def stops(self) -> list:
        return [s for t in self.tasks for s in t.stops]

    @property
    def keys(self) -> list[str]:
        return [k for t in self.tasks for k in t.keys]

    @property
    def task_id(self) -> str:
        return self.tasks[0].task_id if self.tasks else ""

    @property
    def prompt_path(self) -> str:
        return self.tasks[0].prompt_path if self.tasks else ""

    @property
    def command(self) -> list[str]:
        return self.tasks[0].command if self.tasks else []

    @property
    def returncode(self) -> int | None:
        codes = [t.returncode for t in self.tasks]
        if any(c is None for c in codes):
            return None
        return next((c for c in codes if c != 0), 0)


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

    def __init__(self, task: "landing.LandingTask | list[landing.LandingTask] | _CompositePlan") -> None:
        super().__init__()
        if isinstance(task, list):
            self.plan = _CompositePlan(task) if len(task) > 1 else task[0]
        else:
            self.plan = task

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            num_agents = len(self.plan.tasks) if isinstance(self.plan, _CompositePlan) else 1
            agent_label = "one agent" if num_agents == 1 else f"{num_agents} concurrent agents"
            yield Label(
                f"{agent_label} will land {len(self.plan.stops)} pull requests:",
                id="confirm-heading",
            )
            for stop in self.plan.stops:
                yield Static(
                    f"  {stop.key} — {stop.title}", classes="confirm-command"
                )
            if isinstance(self.plan, _CompositePlan):
                for task in self.plan.tasks:
                    yield Static(" ".join(task.command), classes="confirm-command")
            else:
                yield Static(" ".join(self.plan.command), classes="confirm-command")
            yield Label("[enter] dispatch · [esc] abort")

    def action_dispatch(self) -> None:
        self.dismiss(True)


class FinalPolicyScreen(ModalScreen[str]):
    """The one setup gate for a dashboard session's final review (#378).

    Asked once, before the first batch is dispatched, and remembered for the
    session. What it grants is narrow and is stated on the screen: a review
    round may commit only on the pull-request branches the maintainer
    already selected, it never widens that selection, and it never bypasses
    branch protection. Changing it later is `[p]` on the queue.
    """

    BINDINGS = [
        Binding("1", "choose('automatic')", "automatic"),
        Binding("2", "choose('gemini')", "always Gemini Flash"),
        Binding("3", "choose('opus')", "always Opus 5"),
        Binding("4", "choose('sol')", "always GPT Sol"),
        Binding("5", "choose('kimi')", "always K3"),
        *back_bindings("dismiss('automatic')"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("final review for this dashboard session:", id="confirm-heading")
            yield Static(
                "  [1] automatic — Gemini Flash default review with K3 fixes",
                classes="confirm-command",
            )
            yield Static(
                "  [2] always Gemini Flash — fast review loop across all batches",
                classes="confirm-command",
            )
            yield Static(
                "  [3] always Opus 5 — deep reasoning review",
                classes="confirm-command",
            )
            yield Static(
                "  [4] always GPT Sol — structured diagnosis review",
                classes="confirm-command",
            )
            yield Static(
                "  [5] always K3 — Kimi K3 review and fix rounds",
                classes="confirm-command",
            )
            yield Static(
                "Review rounds commit only on the batch's own already-selected "
                "branches. They never add pull requests, never bypass branch "
                "protection, and stop after "
                f"{landing.FINAL_ROUND_LIMIT} rounds with the findings intact.",
                classes="confirm-command",
            )
            yield Label("[1/2/3/4/5] choose · [esc] keep automatic")

    def action_choose(self, policy: str) -> None:
        self.dismiss(policy)


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
    # The final review-and-fix phases share the row vocabulary (#378): the
    # word is the fact, the glyph is its shape, the colour is decoration.
    "final-review": ("◇", "$text-accent"),
    "re-review": ("◇", "$text-accent"),
    "cleanup": ("◌", "cyan"),
    "final-review-clean": ("✓", "bold $text-success on $success-muted"),
    "review-blocked": ("■", "bold $text-warning on $warning-muted"),
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
            if task.phase:
                # Final review-and-fix rounds (#378) belong to the batch that
                # produced them, not beside it: the final-review line below
                # carries their phase, round, and model.
                continue
            if task.returncode is None:
                state = (
                    "running"
                    if self.dashboard._landing_task_active(task)
                    else "queued"
                )
            else:
                state = f"exited {task.returncode}"
            header = f" batch {task.task_id} — {state}"
            if self.dashboard._landing_task_active(task):
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
            # The final review-and-fix phase (#378): which round, which
            # model, which heads it bound to, and what it found. The batch
            # is not finished until this line reads clean or blocked.
            final = events.get(landing.FINAL_KEY, {})
            running_round = next(
                (
                    round_task
                    for round_task in self.dashboard.landing_queue
                    if round_task.phase
                    and round_task.status_path == task.status_path
                    and round_task.returncode is None
                ),
                None,
            )
            if final or running_round:
                mark = str(final.get("phase", final.get("state", "")))
                if running_round:
                    # A dispatched round is a fact the record does not hold
                    # yet: the row says what is running, not only what was
                    # last reported.
                    mark = f"{running_round.phase} running"
                glyph, style = LANDING_STATE_STYLES.get(
                    mark.replace(" running", ""), ("?", "")
                )
                badge = (
                    f"[{style}]{glyph} {escape(mark)}[/]" if style
                    else f"{glyph} {escape(mark)}"
                )
                heads = " ".join(
                    f"{label} {escape(str(final.get(key)))[:7]}"
                    for key, label in (("input_head", "from"), ("output_head", "to"))
                    if final.get(key)
                )
                number = running_round.round if running_round else final.get("round", "?")
                model = running_round.model if running_round else final.get("model", "")
                note = escape(str(final.get("note", "")))
                lines.append(
                    f"  final review  {badge}"
                    f"  round {number}/{landing.FINAL_ROUND_LIMIT}"
                    f"  {escape(str(model))}"
                    + (f"  {heads}" if heads else "")
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
        running = sum(
            1
            for t in self.dashboard.landing_queue
            if self.dashboard._landing_task_active(t)
        )
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
            target_name = "issue" if any("issue" in cmd for cmd in self.commands) else "pull request"
            yield Label(
                f"type the {target_name} number ({self.expected}) to run it; "
                "empty or Esc aborts"
            )
            yield Input(placeholder=self.expected, id="confirm-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() == self.expected)


class BatchMutationConfirmation(ModalScreen[str | None]):
    BINDINGS = [
        Binding("enter", "submit", "confirm exact list", priority=True),
        *back_bindings("dismiss(None)"),
    ]

    def __init__(self, preview: action_plan.BatchActionPreview) -> None:
        super().__init__()
        self.preview_record = preview

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("type every exact PR and head, separated by spaces:")
            for item in self.preview_record.items:
                yield Static(f"  {item.identity}", classes="confirm-command")
            yield Input(id="batch-confirmation")
            yield Static("[enter] confirm exact list · [esc] abort", markup=False)

    def on_mount(self) -> None:
        try:
            self.query_one("#batch-confirmation", Input).focus()
        except (NoMatches, ScreenStackError):
            pass

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#batch-confirmation", Input).value.strip())

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()


class SlayConfirmScreen(ModalScreen[bool]):
    """Typed confirmation gate for $ slay operations.

    Shows exact selected pull requests and their exact heads before creating
    any review run records or dispatching fix/landing work.
    Requires typing the expected confirmation (PR number for a single PR, or
    "slay" for a batch).
    """

    BINDINGS = back_bindings("dismiss(False)")

    def __init__(self, targets: list[Stop]) -> None:
        super().__init__()
        self.targets = list(targets)
        if len(self.targets) == 1:
            self.expected = str(self.targets[0].number)
        else:
            self.expected = "slay"

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("Slay (review + fix + land) will execute for:", id="confirm-heading")
            for stop in self.targets:
                head = stop.head_identity or stop.head_sha or "?"
                yield Static(f"  {stop.key} @ {head}", classes="confirm-command")
            prompt_label = (
                f"type the PR number ({self.expected}) to confirm; empty or Esc aborts"
                if len(self.targets) == 1
                else "type slay to confirm; empty or Esc aborts"
            )
            yield Label(prompt_label)
            yield Input(placeholder=self.expected, id="confirm-input")

    def on_mount(self) -> None:
        try:
            self.query_one(Input).focus()
        except (NoMatches, ScreenStackError):
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() == self.expected)


class DashboardBatchReceiptLedger:
    def __init__(self) -> None:
        self.receipts: list[action_plan.BatchActionReceipt] = []

    def record(self, receipt: action_plan.BatchActionReceipt) -> None:
        self.receipts.append(receipt)


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


def fetch_github_thread(repository: str, number: int) -> tuple[dict | None, str]:
    """Fetch GitHub issue or pull request details with comments and reviews."""
    result = gh(
        "pr", "view", str(number), "--repo", repository,
        "--json", "title,body,author,createdAt,comments,reviews",
    )
    if result.returncode != 0:
        result = gh(
            "issue", "view", str(number), "--repo", repository,
            "--json", "title,body,author,createdAt,comments",
        )
    if result.returncode != 0:
        return None, result.stderr.strip() or f"exit {result.returncode}"
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            return None, "unexpected JSON payload from GitHub"
        return data, ""
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error}"


def _thread_login(value: object) -> str:
    return value.get("login", "unknown") if isinstance(value, dict) else "unknown"


def _thread_timestamp(value: object) -> str:
    return str(value or "").replace("T", " ").replace("Z", " UTC")


def format_github_thread(data: dict, stop_key: str) -> str:
    """Format a GitHub issue/PR opening post, comments and reviews as Markdown."""
    lines: list[str] = []
    title = str(data.get("title") or "")
    body = str(data.get("body") or "").strip()

    lines.append(f"# {stop_key}: {title}\n")
    lines.append(
        f"**@{_thread_login(data.get('author'))}** opened on "
        f"{_thread_timestamp(data.get('createdAt'))}:\n"
    )
    lines.append(body if body else "*No description provided.*")

    events: list[tuple[str, str, str, str, str]] = []
    for comment in data.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        events.append((
            str(comment.get("createdAt") or ""),
            "comment",
            _thread_login(comment.get("author")),
            "",
            str(comment.get("body") or "").strip(),
        ))

    for review in data.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "REVIEW")
        review_body = str(review.get("body") or "").strip()
        # An empty comment carries no information, but an empty approval or
        # change request is itself the verdict and must still be shown.
        if not review_body and state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        events.append((
            str(review.get("submittedAt") or review.get("createdAt") or ""),
            "review",
            _thread_login(review.get("author")),
            state,
            review_body,
        ))

    events.sort(key=lambda item: item[0])

    if not events:
        lines.append("\n\n---\n\n*No comments or reviews yet.*")
    for timestamp, kind, author, state, event_body in events:
        lines.append("\n\n---\n")
        stamp = _thread_timestamp(timestamp)
        if kind == "review":
            lines.append(f"### **@{author}** ({state}) on {stamp}:\n")
        else:
            lines.append(f"### **@{author}** commented on {stamp}:\n")
        lines.append(event_body if event_body else f"*{state}*")

    return "\n".join(lines)


class CommentsScreen(ModalScreen[None]):
    """Issue or pull request comments and conversation rendered in Markdown."""

    BINDINGS = back_bindings("dismiss") + [
        Binding("r", "refresh_comments", "refresh"),
        Binding("o", "open_browser", "open in browser"),
    ]

    def __init__(self, stop: Stop) -> None:
        super().__init__()
        self.stop_record = stop
        self.request_generation = 0

    def compose(self) -> ComposeResult:
        stop = self.stop_record
        yield Static(
            f" {link(stop.key, pr_url(stop.repository, stop.number))} — "
            f"{escape(stop.title[:70])}  (comments)  {escape('[escape]')} closes",
            id="comments-header",
        )
        with ScrollableContainer(id="comments-scroll"):
            yield Markdown("loading comments…", id="comments-body")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh_comments()

    def action_refresh_comments(self) -> None:
        # A refresh that lands after a later one must not overwrite it, so
        # every response is bound to the generation that asked for it.
        self.request_generation += 1
        self.fetch_comments(self.request_generation)

    def action_open_browser(self) -> None:
        stop = self.stop_record
        gh("pr", "view", str(stop.number), "--repo", stop.repository, "--web")

    @work(thread=True)
    def fetch_comments(self, generation: int) -> None:
        stop = self.stop_record
        data, error = fetch_github_thread(stop.repository, stop.number)
        if data is not None:
            formatted = format_github_thread(data, stop.key)
            self.app.call_from_thread(self.render_comments_result, generation, formatted)
        else:
            self.app.call_from_thread(self.render_comments_error, generation, error)

    def render_comments_result(self, generation: int, text: str) -> None:
        if generation == self.request_generation:
            self.query_one("#comments-body", Markdown).update(text)

    def render_comments_error(self, generation: int, message: str) -> None:
        if generation == self.request_generation:
            self.query_one("#comments-body", Markdown).update(
                f"**ERROR loading comments:** {message}"
            )


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


class HelpScreen(ModalScreen[None]):
    """A clean, beautifully structured keyboard shortcut reference."""

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
        Binding("question_mark", "dismiss", "close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static("PROJECT BLUEFIN REVIEW · KEYBOARD REFERENCE", id="help-title")
            with Horizontal(id="help-columns"):
                with Vertical(classes="help-col"):
                    yield Static("NAVIGATION", classes="help-section-title")
                    yield Static("[bold cyan]j[/] / [bold cyan]k[/]       Next / previous PR", classes="help-row")
                    yield Static("[bold cyan]g[/] / [bold cyan]G[/]       First / last PR", classes="help-row")
                    yield Static("[bold cyan]ctrl+d/u[/]   Page down / up", classes="help-row")
                    yield Static("[bold cyan]h[/] / [bold cyan]l[/]       Switch panes", classes="help-row")
                    yield Static("[bold cyan]enter[/]     Inspect decision or diff", classes="help-row")
                    yield Static("[bold cyan]f[/]         Filter queue", classes="help-row")
                    yield Static("[bold cyan]R[/]         Refresh queue & Hive", classes="help-row")

                    yield Static("REVIEW & DETAILS", classes="help-section-title")
                    yield Static("[bold cyan]r[/]         Start automated review", classes="help-row")
                    yield Static("[bold cyan]f[/] / [bold cyan]F[/]       Auto-fix & land / steer fix", classes="help-row")
                    yield Static("[bold cyan]/[/]         Steer review with prompt", classes="help-row")
                    yield Static("[bold cyan]v[/]         View full diff", classes="help-row")
                    yield Static("[bold cyan]o[/]         Open in browser", classes="help-row")
                    yield Static("[bold cyan]y[/]         Copy review context", classes="help-row")
                    yield Static("[bold cyan]c[/]         Comment on PR", classes="help-row")

                with Vertical(classes="help-col"):
                    yield Static("BATCH & LANDING", classes="help-section-title")
                    yield Static("[bold cyan]b[/]         Toggle PR batch select", classes="help-row")
                    yield Static("[bold cyan]B[/]         Select / clear visible rows", classes="help-row")
                    yield Static("[bold cyan]space[/]     Toggle row and advance", classes="help-row")
                    yield Static("[bold cyan]n[/]         Skip to next PR lacking my review", classes="help-row")
                    yield Static("[bold magenta]$[/]         Slay PR (review+fix+land)", classes="help-row")
                    yield Static("[bold cyan]A[/]         Land selected batch", classes="help-row")
                    yield Static("[bold cyan]w[/]         Watch running agents", classes="help-row")
                    yield Static("[bold cyan]P[/]         Final review policy", classes="help-row")
                    yield Static("[bold cyan]U[/]         Select mechanical updates", classes="help-row")

                    yield Static("MUTATIONS & DECISIONS", classes="help-section-title")
                    yield Static("[bold cyan]a[/]         Approve + queue (lgtm)", classes="help-row")
                    yield Static("[bold cyan]m[/]         Merge now (maintainer)", classes="help-row")
                    yield Static("[bold cyan]u[/]         Update clean branch", classes="help-row")
                    yield Static("[bold cyan]M[/]         Resolve duplicates", classes="help-row")
                    yield Static("[bold cyan]x[/]         Reject and close", classes="help-row")
                    yield Static("[bold cyan]L[/]         Submit GitHub review", classes="help-row")

            yield Static("[dim]Press [bold]?[/bold], [bold]q[/bold], or [bold]Esc[/bold] to return to the dashboard[/dim]", id="help-footer")


class FixSteerModal(ModalScreen[str | None]):
    """Prompt maintainer for optional guidance before background fix-and-land."""

    BINDINGS = [
        Binding("ctrl+s", "submit", "submit", priority=True),
        Binding("enter", "submit", "submit", priority=True),
        *back_bindings("dismiss(None)"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("guidance for background fix-and-land (empty for default):")
            yield Input(id="fix-steer-input")
            yield Static("[enter] dispatch · [esc] cancel", markup=False)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


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
        Binding("f", "spawn_fix", "fix & land in background"),
        Binding("F", "steer_fix", "steer fix & land"),
        Binding("L", "leave_review", "leave a review"),
        Binding("a", "queue", "approve and queue"),
        Binding("m", "merge_now", "merge now"),
        Binding("u", "update_branch", "update clean branch"),
        Binding("e", "toggle_evidence", "evidence"),
        Binding("r", "toggle_raw_transcript", "raw transcript"),
        Binding("c", "view_comments", "comments"),
        Binding("v", "view_diff", "diff"),
    ]

    def __init__(
        self,
        stop: Stop,
        steer: str = "",
        selection: Preference | None = None,
        headroom_session: HeadroomSession | None = None,
        headroom_lock: threading.Lock | None = None,
    ) -> None:
        super().__init__()
        self.stop_record = stop
        self.steer = steer
        self.selection = selection or Preference(ACTIVE_BACKEND, "gemini-3.8-flash", "max")
        self.headroom_session = (
            headroom_session or HeadroomSession.from_environment()
        )
        self.headroom_lock = headroom_lock or threading.Lock()
        self.headroom_status_line = self.headroom_session.status_line(
            ACTIVE_BACKEND, True
        )
        self.process: subprocess.Popen | None = None
        self.finished = False
        self.stop_requested = False
        self.started = time.monotonic()
        self.output: list[str] = []
        self.output_overflowed = False
        # The card is a point-in-time record of the evidence the maintainer
        # saw when the review started. The dashboard's background workers
        # keep rewriting stop.live/stop.overlap while the review runs, so
        # reading them at finish would mix a fresh fetch into a completed
        # transcript (#339).
        self.live_snapshot = copy.deepcopy(stop.live)
        self.overlap_snapshot = copy.deepcopy(stop.overlap)
        self.prior_result = copy.deepcopy(stop.review_result)
        self.compare_evidence = CompareEvidence()

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
        prior_head = str((self.prior_result.provenance if self.prior_result else {}).get("head_sha") or "")
        current_head = str(self.live_snapshot.get("headRefOid") or "")
        if self.prior_result is not None and prior_head and prior_head != current_head:
            self.compare_evidence = compare_hunk_regions(
                stop.repository, prior_head, current_head
            )
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
        with self.headroom_lock:
            self.headroom_session.route_for_call(ACTIVE_BACKEND)
            self.headroom_status_line = self.headroom_session.status_line(
                ACTIVE_BACKEND, True
            )
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
        code = process.wait()
        with self.headroom_lock:
            self.headroom_session.refresh(ACTIVE_BACKEND)
            self.headroom_status_line = self.headroom_session.status_line(
                ACTIVE_BACKEND, True
            )
        self.app.call_from_thread(self.finish, code, "")

    def mark_running(self) -> None:
        stop = self.stop_record
        clear_review_failure_mark(stop)
        self.query_one("#review-status", Static).update(
            f" reviewing {link(stop.key, pr_url(stop.repository, stop.number))}"
            f" — running; {escape(self.headroom_status_line)}; "
            f"{escape('[x]')} stops it"
        )

    def append(self, line: str) -> None:
        if len(self.output) < MAX_REVIEW_OUTPUT_LINES:
            self.output.append(line)
        else:
            self.output_overflowed = True
        self.query_one("#review-log", RichLog).write(line)

    def finish(self, code: int | None, error: str) -> None:
        self.finished = True
        # A transcript we stopped capturing cannot be parsed into a verdict
        # anyone may act on, so overflow fails closed down the error path
        # rather than yielding findings extracted from a partial capture.
        if self.output_overflowed and not error:
            error = (
                f"review output exceeded {MAX_REVIEW_OUTPUT_LINES} lines; "
                "the transcript is incomplete and its verdict is not trustworthy"
            )
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
                 "model": os.environ.get("GOOSE_MODEL", "gemini-3.8-flash"),
                 "repository": stop.repository, "pull_request": stop.number},
                verification=live_review_verification(self.live_snapshot),
                overlap=self.overlap_snapshot,
                live=live_context,
            )
        result.provenance.setdefault("base_sha", str(self.live_snapshot.get("baseRefOid") or ""))
        result.provenance.setdefault("head_sha", str(self.live_snapshot.get("headRefOid") or ""))
        if ACTIVE_BACKEND == "codex" and len(str(self.live_snapshot.get("baseRefOid") or "")) == 40 and len(str(self.live_snapshot.get("headRefOid") or "")) == 40:
            request = ReviewRequest(
                *stop.repository.split("/", 1), stop.number,
                self.live_snapshot["baseRefOid"], self.live_snapshot["headRefOid"],
                actor="maintainer", tenant="review", generated_at="dashboard",
            )
            if can_remember(result, request):
                remember_success(
                    load_preferences(), stop.repository,
                    Preference("codex", result.provenance.get("model", "gemini-3.8-flash"),
                               result.provenance.get("reasoning_effort", "max")),
                )
        reviewed_base = str(self.live_snapshot.get("baseRefOid") or "")
        reviewed_head = str(self.live_snapshot.get("headRefOid") or "")
        current_base = str(stop.live.get("baseRefOid") or "")
        current_head = stop.head_identity
        identity_stale = not (
            re.fullmatch(r"[0-9a-f]{40}", current_base)
            and re.fullmatch(r"[0-9a-f]{40}", current_head)
            and current_base == reviewed_base
            and current_head == reviewed_head
        )
        card = build_decision_card(result, exact_head=current_head)
        decision_state = (
            DecisionState.STALE if identity_stale else card.state
        )
        if self.stop_requested:
            outcome, state = "stopped", "STOPPED — you cancelled it. Nothing was submitted."
        elif error:
            outcome, state = "error", f"FAILED to start: {error}"
        elif code is not None and code < 0:
            outcome, state = "stopped", "STOPPED — the review was killed. Nothing was submitted."
        elif decision_state in (DecisionState.CLEAN, DecisionState.FINDINGS):
            outcome = "complete"
            state = "COMPLETE — a Review Draft for you to judge. Nothing was submitted."
        elif decision_state is DecisionState.STALE:
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
        if not identity_stale:
            stop.review_result = result
            stop.review_status = result.state
            stop.review_failure = (
                ""
                if result.state in {"complete", "findings"}
                else state
            )
            if (
                decision_state
                in (DecisionState.CLEAN, DecisionState.FINDINGS)
                and reviewed_head
            ):
                stop.head_sha = reviewed_head
                stop.triage_state = "reviewed"
                triage = getattr(self.app, "triage", None)
                if isinstance(triage, dict):
                    triage[stop.triage_key] = "reviewed"

        delta_card = self._re_review_card(result)

        status = self.query_one("#review-status", Static)
        status.remove_class("running")
        status.add_class(outcome)
        status.update(
            f" {link(stop.key, pr_url(stop.repository, stop.number))} — "
            f"{escape(state)} ({elapsed}s) — "
            f"{escape(self.headroom_status_line)} — "
            f"{escape('[escape]')} closes"
        )
        finding_total = sum(card.counts.values())
        diff_info = (
            f"+{self.live_snapshot.get('additions', '?')} "
            f"-{self.live_snapshot.get('deletions', '?')} "
            f"across {self.live_snapshot.get('changedFiles', '?')} file(s)"
            + (f" by @{escape(stop.author)}" if stop.author else "")
        )
        lines = [
            f"{decision_state.value.upper()}  {escape(stop.key)}  ({diff_info})",
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
        for finding in card.findings[:8]:
            sev = finding.severity.lower()
            badge = "🔴" if sev == "critical" else "🟠" if sev == "high" else "🟡" if sev == "medium" else "⚪"
            lines.append(
                f"{finding.severity.upper()}  "
                f"{escape(finding.file)}:{finding.line}  "
                f"{escape(finding.title)}  {badge}"
            )
        if len(card.findings) > 8:
            lines.append(f"  … {len(card.findings) - 8} more findings (press [e] for full evidence)")
        verified = sum(1 for item in card.verification if item.state == "verified")
        unverified = sum(1 for item in card.verification if item.state == "unverified")
        lines.append(
            f"checks  {verified} verified / {unverified} unverified / "
            f"{len(card.verification)} reported"
        )
        check_items = [f"{('✓' if item.state == 'verified' else '✗')} {escape(item.name)}" for item in card.verification]
        if check_items:
            lines.append("        " + "  ".join(check_items[:6]))
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
            "actions   "
            f"{escape('[f]')} fix & land  {escape('[F]')} steer fix  "
            f"{escape('[L]')} review  {escape('[c]')} comments  {escape('[v]')} diff  "
            f"{escape('[a]')} approve+queue  {escape('[m]')} merge  {escape('[u]')} update  "
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
        prior = self.prior_result
        if prior is None:
            return []
        reviewed = str(prior.provenance.get("head_sha") or "")
        current = str(self.live_snapshot.get("headRefOid") or "")
        base = str(self.live_snapshot.get("baseRefOid") or "")
        if reviewed == current and re.fullmatch(r"[0-9a-f]{40}", current):
            return []
        if not all(re.fullmatch(r"[0-9a-f]{40}", value or "") for value in (reviewed, current, base)):
            return self._re_review_fallback(
                reviewed, current, "exact H0/H1 identity or current H1 base unavailable"
            )
        historical_base = str(prior.provenance.get("base_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", historical_base):
            return self._re_review_fallback(reviewed, current, "historical H0 merge base unavailable")

        def findings(value: ReviewResult) -> tuple[tuple[PriorFinding, ...], bool, bool]:
            mapped: list[PriorFinding] = []
            malformed = False
            for item in value.findings[:MAX_RE_REVIEW_FINDINGS + 1]:
                if (not isinstance(item, dict) or not isinstance(item.get("file"), str) or
                        not isinstance(item.get("line"), int) or
                        not isinstance(item.get("end_line", item.get("line")), int)):
                    malformed = True
                    continue
                path = item["file"]
                start = item["line"]
                end = item.get("end_line", start)
                if not path or len(path) > 512 or start < 1 or end < start:
                    malformed = True
                    continue
                try:
                    mapped.append(PriorFinding(
                        f"{path}:{start}", FindingEvidence(path, start, end)
                    ))
                except ValueError:
                    malformed = True
            return tuple(mapped[:MAX_RE_REVIEW_FINDINGS]), malformed, len(value.findings) > MAX_RE_REVIEW_FINDINGS

        prior_findings, prior_malformed, prior_risk = findings(prior)
        current_findings, current_malformed, current_risk = findings(result)
        h1_stale = (
            result.state not in {"complete", "findings"}
            or self.compare_evidence.mapping_uncertain
        )
        evidence = tuple(
            FindingEvidence(item.evidence.path, item.evidence.start_line,
                            item.evidence.end_line, h1_stale)
            for item in current_findings if item.evidence is not None
        )
        prior_ids = {item.finding_id for item in prior_findings}
        new = tuple(
            H1Evidence(item.finding_id, item.evidence.path, item.evidence.start_line)
            for item in current_findings
            if item.evidence is not None and item.finding_id not in prior_ids
        )
        try:
            request = ReviewRequest(*self.stop_record.repository.split("/", 1), self.stop_record.number, base, current, "maintainer", "review", generated_at="dashboard")
            delta = classify_head_delta(DeltaInput(
                reviewed, current, historical_base, base, request,
                self.compare_evidence.regions, prior_findings, evidence, new,
                self.compare_evidence.mapping_uncertain or prior_malformed or current_malformed,
                self.compare_evidence.sensitive_surfaces_changed,
                prior.state in {"complete", "findings"},
                self.compare_evidence.bounded_risk_exceeded or prior_risk or current_risk,
                self.compare_evidence.capability_available,
            ))
        except (TypeError, ValueError, KeyError):
            return self._re_review_fallback(reviewed, current, "malformed re-review evidence")
        lines = [
            "", "RE-REVIEW  exact-head delta",
            f"reviewed {escape(reviewed)}  current {escape(current)}",
            f"H1 manifest  base {escape(base)}  head {escape(delta.current_h1_request.head_sha)}  result {escape(result.state)}",
        ]
        lines.append(
            "dispositions  " + ", ".join(
                f"{escape(item.finding_id)}={escape(item.disposition.value)}"
                for item in delta.findings[:MAX_RE_REVIEW_FINDINGS]
            ) if delta.findings else "dispositions  none"
        )
        if delta.newly_supported:
            lines.append("new H1 evidence  " + ", ".join(
                f"{escape(item.finding_id)} ({escape(item.path)}:{item.line})"
                for item in delta.newly_supported[:MAX_RE_REVIEW_NEW_EVIDENCE]
            ))
        lines.append("No authority carried from H0.")
        if delta.full_review_required:
            lines.append("FULL REVIEW REQUIRED — " + ", ".join(
                escape(item.value) for item in delta.fallback_reasons
            ) + ".")
        return lines

    @staticmethod
    def _re_review_fallback(reviewed: str, current: str, reason: str) -> list[str]:
        """Keep malformed historic evidence visible and fail closed."""
        return [
            "", "RE-REVIEW  FULL REVIEW REQUIRED",
            f"reviewed {escape(reviewed or '?')}  current {escape(current or '?')}",
            f"reason  {escape(reason)}.",
            "No authority carried from H0.",
        ]

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

    def action_view_comments(self) -> None:
        self.app.push_screen(CommentsScreen(self.stop_record))

    def action_view_diff(self) -> None:
        self.app.push_screen(DiffScreen(self.stop_record))

    def dismiss_to_queue(self, action=None) -> None:
        self.dismiss()

        def refresh_then_act() -> None:
            self.app.refresh_rows()
            if action is not None:
                action()

        self.app.call_after_refresh(refresh_then_act)

    def return_to_queue(self, action) -> None:
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return
        self.dismiss_to_queue(action)

    def action_queue(self) -> None:
        self.return_to_queue(self.app.action_merge)

    def action_merge_now(self) -> None:
        self.return_to_queue(self.app.action_merge_now)

    def action_update_branch(self) -> None:
        self.return_to_queue(self.app.action_update_branch)

    def action_spawn_fix(self) -> None:
        """Spawn background fix-and-land subagent for evidenced findings."""
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return
        self._dispatch_fix(steer="")

    def action_steer_fix(self) -> None:
        """Prompt for guidance, then spawn background fix-and-land subagent."""
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return

        def with_steer(value: str | None) -> None:
            if value is not None:
                self._dispatch_fix(steer=value.strip())

        self.app.push_screen(FixSteerModal(), with_steer)

    def _dispatch_fix(self, steer: str = "") -> None:
        if not self.app.self_login:
            self.notify("your GitHub login is unknown; needed for agent fix.", severity="warning")
            return
        findings = list(self.stop_record.review_result.findings if self.stop_record.review_result else [])
        task = landing.new_fix_task(self.stop_record, findings, self.app.self_login, steer=steer)
        self.app.enqueue_landing(task)
        self.notify(f"dispatched auto-fix & land for {self.stop_record.key} [w]")
        self.dismiss_to_queue()

    def action_close(self) -> None:
        # A review takes minutes. Closing mid-run would throw that away with a
        # keystroke, so an unfinished review has to be stopped deliberately.
        if not self.finished:
            self.notify("review still running — [x] stops it")
            return
        self.dismiss_to_queue()


class ReviewDecisionScreen(ModalScreen[None]):
    BINDINGS = [*back_bindings("dismiss(None)")]

    def __init__(self, stop: Stop) -> None:
        super().__init__()
        self.stop_record = stop

    def compose(self) -> ComposeResult:
        stop = self.stop_record
        result = stop.review_result
        assert result is not None
        exact_head = str(
            stop.live.get("headRefOid") or stop.head_sha
        )
        card = build_decision_card(result, exact_head=exact_head)
        lines = [
            f"{card.state.value.upper()} {escape(stop.key)}",
            f"head {escape(exact_head)}",
            f"live CI {escape(str(stop.live.get('ci') or '?').upper())}",
            f"merge {escape(str(stop.live.get('mergeStateStatus') or '?'))}",
            f"findings {len(card.findings)}",
            "[escape] closes",
        ]
        for finding in card.findings[:8]:
            lines.append(
                f"{finding.severity.upper()} "
                f"{escape(finding.file)}:{finding.line} "
                f"{escape(finding.title)}"
            )
        yield Static("\n".join(lines), id="cached-decision-card")


class ReviewDashboard(App):
    """PROJECT BLUEFIN REVIEW DASHBOARD."""

    issues_items: list[dict] = []
    batch_mutation_in_flight: bool = False
    _current_batch_plan: action_plan.BatchActionPlan | None = None
    _batch_generation_token: int = 0

    TITLE = "BLUEFIN REVIEW DASHBOARD"
    CSS = """
    #status-bar { height: 1; background: $panel; color: cyan; }
    #activity {
        border: heavy $primary; height: auto; padding: 0 1;
        color: $text;
    }
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
    #comments-header { height: 1; background: $panel; color: cyan; text-style: bold; }
    #comments-scroll { border: solid $secondary; background: $surface; }
    #comments-body { padding: 0 1; width: auto; }
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
    #help-box {
        border: heavy cyan; background: $surface;
        width: 76; height: auto; padding: 1 2; margin: 2 4;
    }
    #help-title { text-align: center; height: 1; margin-bottom: 1; border-bottom: solid $secondary; color: cyan; text-style: bold; }
    #help-columns { width: 100%; height: auto; }
    .help-col { width: 50%; height: auto; padding: 0 1; }
    .help-section-title { margin-top: 1; margin-bottom: 0; color: magenta; text-style: bold; }
    .help-row { height: 1; }
    #help-footer { text-align: center; margin-top: 1; color: $text-muted; }
    """

    BINDINGS = bindings_for("dashboard")

    def __init__(
        self,
        filters: QueueFilters | None = None,
        *,
        run_store: RunStateStore | None = None,
        gh_client: GhClient | None = None,
    ) -> None:
        super().__init__()
        self.filters = filters or QueueFilters()
        self.run_store = run_store or RunStateStore()
        self.gh_client = gh_client or default_client
        self.observability = ReviewObservability.from_environment()
        self.issues_items = []
        self.stops: list[Stop] = []
        self.self_login = ""
        self.pulls_cache: dict[str, list[dict]] = {}
        # Repository -> whether this login may merge there. Merging without
        # the lgtm opt-in is a maintainer power, so it is asked of GitHub per
        # repository rather than assumed from the fact that a dashboard is
        # open. Unknown until asked, and never cached as True by default.
        self.merge_rights: dict[str, bool] = {}
        self.queue_items: list[dict] = []
        # What Hive says, when it has been asked. "" means not asked yet, so
        # the status line can tell "we have not looked" apart from "the hub is
        # down" — the first is a dashboard that never tried, which is what the
        # old permanent "Hive: not consulted" amounted to.
        self.hive_state = ""
        self.hive_workers: list[dict] = []
        self.hive_unavailable = False
        self.hive_workers_stale = False
        self.queue_snapshot_at: float | None = None
        self.hive_snapshot_at: float | None = None
        self.reconciliation_updated_at: float | None = None
        self._reconciliation_request = 0
        self._reconciliation_waiting: set[str] = set()
        self._reconciliation_success: dict[str, bool] = {}
        self._reconciliation_source_attempts = {"queue": 0, "hive": 0}
        self._reconciliation_pending = False
        self.reconciliation_state = "not refreshed"
        self._queue_load_lock = threading.Lock()
        # Keys to re-select after a refresh: a refresh that silently empties
        # the batch you spent a minute building is worse than no refresh.
        self.reselect: set[str] = set()
        self.all_items: list[dict] = []
        self.harness_state = "CHECKING"
        self.harness_options: list[HarnessOption] = []
        # Dispatched landing batches, oldest first. Independent repositories
        # may use separate lanes while batches touching one repository wait.
        self.landing_queue: list[landing.LandingTask] = []
        self._landing_active: set[int] = set()
        self._landing_condition = threading.Condition()
        # One dispatcher owns scheduling. A final-review round is enqueued
        # from a finished task's callback, so this flag keeps a second
        # dispatcher from racing the first for the same task (#378).
        self.landing_draining = False
        # The last finished batch's outcome, kept on the status line until
        # the next dispatch or refresh: a toast is gone in seconds and a
        # maintainer looks up late.
        self.last_landing_outcome = ""
        self.recent_merges: list[str] = []
        # The session's final-review policy (#378): asked once before the
        # first dispatch, kept in memory only, changed with [p]. None means
        # the session has not been asked yet.
        self.final_policy: str | None = None
        # The optional lab (#379): OFF until a status answer says otherwise.
        # The dashboard never blocks on it and never depends on it.
        self.lab_state = lab_client.LAB_OFF
        self.lab_detail = ""
        self.pr_source_state = "loading"
        self.pr_source_message = ""
        self.issues_source_state = "loading"
        self.issues_source_message = ""
        self.source_state = "loading"
        self.source_message = ""
        self.slay_frame = -1
        self._had_nonempty_queue = False
        self.headroom_lock = threading.Lock()
        self.headroom_session = HeadroomSession.from_environment()
        with self.headroom_lock:
            self.headroom_status_line = self.headroom_session.status_line(
                ACTIVE_BACKEND, True
            )
            self.headroom_output_reduction = (
                self.headroom_session.telemetry(ACTIVE_BACKEND)
            )
        self.review_cache = ReviewCache()
        # review_snapshot consumes live_review_verification from this module.
        # Register the direct-script module name before loading ReviewEngine
        # so the package import resolves to this completed module.
        sys.modules.setdefault(
            "tui.bluefin_review_tui", sys.modules[__name__]
        )
        self.review_engine = ReviewEngine(
            headroom_session=self.headroom_session,
            headroom_lock=self.headroom_lock,
        )
        self.review_batches: list[ReviewBatch] = []
        self.review_pending_keys: set[str] = set()
        self.review_expected_heads: dict[str, str] = {}
        self.review_batch_ids: dict[str, str] = {}
        self.pending_review_events: dict[str, list[ReviewEvent]] = {}
        self.evidence_generation: dict[str, int] = {}
        self.triage: dict[str, TriageState] = {}
        self.triage_by_key = self.triage
        self.review_scope = REVIEW_SCOPE
        self.review_scope_version = REVIEW_SCOPE_VERSION
        self.batch_action_receipt: action_plan.BatchActionReceipt | None = None
        self.batch_mutation_in_flight: bool = False
        self._current_batch_plan: action_plan.BatchActionPlan | None = None
        self._batch_generation_token: int = 0
        self._batch_receipt_ledger = DashboardBatchReceiptLedger()
        self.fixer_advanced_heads: set[tuple[str, int, str]] = set()
        self.active_review_identities: dict[str, RunIdentity] = {}
        self.review_started_at: dict[str, float] = {}
        self.hive_ranks = {}
        self.hive_ready_queue = []
        self.hive_triage_groups = []
        self.hive_queue_unavailable = False
        self.hive_queue_stale = False
        self.hive_triage_unavailable = False
        self.hive_triage_stale = False

    def hive_rank_display(self, stop: Stop) -> str:
        val = self.hive_ranks.get(stop.key)
        if val is None:
            return ""
        is_stale = (self.hive_triage_stale if stop.is_issue else self.hive_queue_stale) or self.hive_unavailable
        is_unavail = (self.hive_triage_unavailable if stop.is_issue else self.hive_queue_unavailable) or self.hive_unavailable
        if is_stale or is_unavail:
            age = activity_age(self.hive_snapshot_at)
            status = f"retained {age}".strip() if age else "retained"
            return f" [dim]#{val + 1} ({status})[/dim]"
        return f" [cyan]#{val + 1}[/cyan]"

    def stop_blocked_reason(
        self, stop: Stop, record_snapshot: dict[str, RunRecord] | None = None
    ) -> str | None:
        if stop.is_issue:
            return None
        if getattr(self, "_repaint_blocked_cache", None) is not None and stop.key in self._repaint_blocked_cache:
            return self._repaint_blocked_cache[stop.key]
        rec = None
        if hasattr(self, "run_store"):
            try:
                base_sha = str(stop.live.get("baseRefOid") or "")
                head_sha = stop.head_identity
                if FULL_SHA.fullmatch(base_sha) and FULL_SHA.fullmatch(head_sha):
                    ident = self.run_identity(stop)
                    if record_snapshot is not None:
                        rec = record_snapshot.get(ident.cache_identity)
                    elif getattr(self, "_repaint_record_snapshot", None) is not None:
                        rec = self._repaint_record_snapshot.get(ident.cache_identity)
                    else:
                        rec = self.run_store.get(ident)
                if rec is None and stop.review_result:
                    prov = getattr(stop.review_result, "provenance", None)
                    rev_head = (prov.get("head_sha") if isinstance(prov, dict) else None) or head_sha
                    rev_model = prov.get("model") if isinstance(prov, dict) else None
                    rev_effort = (prov.get("effort") or prov.get("reasoning_effort")) if isinstance(prov, dict) else None
                    if rev_head and FULL_SHA.fullmatch(rev_head) and FULL_SHA.fullmatch(base_sha):
                        rev_ident = self.run_identity(stop, head_sha=rev_head, model=rev_model, effort=rev_effort)
                        if record_snapshot is not None:
                            rec = record_snapshot.get(rev_ident.cache_identity)
                        elif getattr(self, "_repaint_record_snapshot", None) is not None:
                            rec = self._repaint_record_snapshot.get(rev_ident.cache_identity)
                        else:
                            rec = self.run_store.get(rev_ident)
            except Exception:
                rec = None
        result = classify_routability(stop, record=rec)
        if getattr(self, "_repaint_blocked_cache", None) is not None:
            self._repaint_blocked_cache[stop.key] = result
        return result

    def _pr_action_targets(self) -> list[Stop] | None:
        """Centralized PR-only action guard for batch/current targets."""
        if self.view_mode == "issues":
            self.notify("action applies to pull requests only", severity="warning")
            return None
        selected = [s for s in self.stops if s.selected]
        if selected:
            prs = [s for s in selected if not s.is_issue]
            if len(prs) < len(selected):
                self.notify("selected issues skipped; action applies to pull requests only", severity="warning")
            if not prs:
                return None
            return prs
        elif self.current:
            if self.current.is_issue:
                self.notify("action applies to pull requests only", severity="warning")
                return None
            return [self.current]
        return None

    @property
    def view_mode(self) -> str:
        return self.filters.kind

    @view_mode.setter
    def view_mode(self, value: str) -> None:
        self.filters.kind = value

    @property
    def active_source_items(self) -> list[dict]:
        items: list[dict] = []
        if self.filters.wants_kind({"is_issue": False}):
            items.extend(self.queue_items)
        if self.filters.wants_kind({"is_issue": True}):
            items.extend(self.issues_items)
        return items

    def _sync_source_state(self) -> None:
        if self.view_mode == "issues":
            self.source_state = self.issues_source_state
            self.source_message = self.issues_source_message
        elif self.view_mode == "prs":
            self.source_state = self.pr_source_state
            self.source_message = self.pr_source_message
        elif self.filters.live:
            if self.pr_source_state not in ("ready", "empty"):
                self.source_state = self.pr_source_state
                self.source_message = self.pr_source_message
            elif self.issues_source_state not in ("ready", "empty"):
                if self.active_source_items:
                    self.source_state = "degraded"
                    self.source_message = f"Issue source failed: {self.issues_source_message or self.issues_source_state}"
                else:
                    self.source_state = self.issues_source_state
                    self.source_message = self.issues_source_message
            elif not self.active_source_items:
                self.source_state = "empty"
                self.source_message = ""
            else:
                self.source_state = "ready"
                self.source_message = ""
        else:  # "mixed" org-wide
            pr_ok = self.pr_source_state in ("ready", "empty")
            issue_ok = self.issues_source_state in ("ready", "empty")
            if pr_ok and issue_ok:
                if not self.active_source_items:
                    self.source_state = "empty"
                else:
                    self.source_state = "ready"
                self.source_message = ""
            elif not self.active_source_items:
                self.source_state = self.pr_source_state if not pr_ok else self.issues_source_state
                self.source_message = self.pr_source_message if not pr_ok else self.issues_source_message
            elif pr_ok and not issue_ok:
                self.source_state = "degraded"
                self.source_message = f"Issue source failed: {self.issues_source_message or self.issues_source_state}"
            elif not pr_ok and issue_ok:
                self.source_state = "degraded"
                self.source_message = f"PR source failed: {self.pr_source_message or self.pr_source_state}"
            elif self.pr_source_state == "loading" or self.issues_source_state == "loading":
                self.source_state = "loading"
                self.source_message = ""
            else:
                self.source_state = self.pr_source_state if not pr_ok else self.issues_source_state
                self.source_message = self.pr_source_message or self.issues_source_message

    def record_fixer_head(self, repository: str, number: int, head_sha: str) -> None:
        """Explicitly record that a head was produced and pushed by our fixer."""
        if FULL_SHA.fullmatch(head_sha):
            self.fixer_advanced_heads.add((repository, number, head_sha))

    def is_fixer_head(self, repository: str, number: int, head_sha: str) -> bool:
        """Deliberately and explicitly distinguish our fixer's pushed head from a foreign head change."""
        return (repository, number, head_sha) in self.fixer_advanced_heads

    def escalation_profile(self, stop: Stop) -> tuple[str, str, str]:
        """The (backend, model, effort) triple for an escalated, high-assurance review."""
        policy = self.final_policy or "automatic"
        classification = classify_batch([stop])
        return escalation_triple(policy, classification)

    def run_identity(
        self,
        stop: Stop,
        *,
        model: str | None = None,
        effort: str | None = None,
        head_sha: str | None = None,
    ) -> RunIdentity:
        base_sha = str(stop.live.get("baseRefOid") or "")
        if not FULL_SHA.fullmatch(base_sha):
            raise ValueError(f"invalid base_sha: {base_sha!r}; strict full lowercase 40-hex SHA required")
        if head_sha is None:
            head_sha = stop.head_identity
        if not FULL_SHA.fullmatch(head_sha):
            raise ValueError(f"invalid head_sha: {head_sha!r}; strict full lowercase 40-hex SHA required")

        ident_cache = getattr(self, "_repaint_ident_cache", None)
        cache_key = (stop.key, head_sha, model, effort, base_sha) if ident_cache is not None else None
        if cache_key and cache_key in ident_cache:
            return ident_cache[cache_key]

        if model is None or effort is None:
            default_model, default_effort = self.review_profile(stop.repository)
            if model is None:
                model = default_model
            if effort is None:
                effort = default_effort
        result = RunIdentity(
            repository=stop.repository,
            pull_request=stop.number,
            base_sha=base_sha,
            head_sha=head_sha,
            backend=ACTIVE_BACKEND,
            model=model,
            effort=effort,
            check_scope_version=self.review_scope_version,
        )
        if cache_key is not None:
            ident_cache[cache_key] = result
        return result

    def safe_run_identity(
        self,
        stop: Stop,
        *,
        model: str | None = None,
        effort: str | None = None,
        head_sha: str | None = None,
    ) -> RunIdentity | None:
        try:
            return self.run_identity(stop, model=model, effort=effort, head_sha=head_sha)
        except ValueError:
            return None

    @staticmethod
    def is_bot_login(login: str) -> bool:
        lowered = login.lower()
        return (
            lowered.endswith("[bot]")
            or lowered.endswith("-bot")
            or lowered in {"goose", "github-actions", "copilot"}
        )

    def has_human_review(self, live: dict) -> bool:
        reviews = live.get("reviews") or []
        if isinstance(reviews, dict):
            reviews = reviews.get("nodes", [])
        if not isinstance(reviews, list):
            return False
        for review in reviews:
            if not isinstance(review, dict):
                continue
            author = review.get("author")
            login = (
                author.get("login")
                if isinstance(author, dict)
                else (author if isinstance(author, str) else "")
            )
            if not login or self.is_bot_login(login):
                continue
            state = str(review.get("state") or "").upper()
            if state in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}:
                return True
        return False

    def stop_lacks_my_review(self, stop: Stop) -> bool:
        if stop.is_issue or not self.self_login:
            return False
        reviews = stop.live.get("reviews") if isinstance(stop.live, dict) else None
        if isinstance(reviews, dict):
            reviews = reviews.get("nodes", [])
        if not isinstance(reviews, list) or not reviews:
            return True
        for r in reviews:
            if not isinstance(r, dict):
                continue
            author = r.get("author")
            login = (
                author.get("login")
                if isinstance(author, dict)
                else (author if isinstance(author, str) else "")
            )
            if login == self.self_login:
                state = str(r.get("state") or "").upper()
                if state != "DISMISSED":
                    return False
        return True

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading queue…", id="status-bar")
        yield Static("AGENT ACTIVITY\nSnapshot: unavailable", id="activity")
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
        with self.headroom_lock:
            self.headroom_session.refresh(ACTIVE_BACKEND)
            self.headroom_status_line = self.headroom_session.status_line(
                ACTIVE_BACKEND, True
            )
            self.headroom_output_reduction = (
                self.headroom_session.telemetry(ACTIVE_BACKEND)
            )
        self.refresh_status()
        self.load_queue()
        self.load_issues()
        self.load_hive()
        self.discover_harness()
        # The lab is polled coarsely and off the UI thread (#379): 30 seconds
        # is fast enough for a status word and slow enough that a wedged
        # broker cannot become the dashboard's pace.
        if lab_client.lab_configured():
            self.poll_lab()
            self.set_interval(30.0, self.poll_lab)

    @work(thread=True, group="lab", exclusive=True)
    def poll_lab(self) -> None:
        """Ask the broker what the lab is doing. Never blocking, never fatal."""
        answer = lab_client.status()
        state = lab_client.lab_state(answer)
        detail = str(answer.get("detail") or answer.get("error") or "")
        self.call_from_thread(self.lab_polled, state, detail)

    def lab_polled(self, state: str, detail: str) -> None:
        self.lab_state = state
        self.lab_detail = detail
        try:
            self.refresh_status()
        except NoMatches:
            # The poll can land after Textual tore the dashboard down.
            return

    def action_review_policy(self) -> None:
        """Change the session's final-review policy (#378)."""
        def chosen(policy: str | None) -> None:
            self.final_policy = policy or "automatic"
            self.notify(f"final review policy: {self.final_policy}")
            self.refresh_status()

        self.push_screen(FinalPolicyScreen(), chosen)

    @work(thread=True)
    def discover_harness(self) -> None:
        """Probe Codex off the UI thread; discovery never starts inference."""
        options = discover_all()
        self.call_from_thread(self.harness_loaded, options)

    def harness_loaded(self, options: list[HarnessOption]) -> None:
        self.harness_options = options
        result = next((option.discovery for option in options if option.harness.branding.harness_id == ACTIVE_BACKEND), options[0].discovery)
        self.harness_state = result.availability.value
        try:
            label = self.query_one("#harness-status", Static)
        except NoMatches:
            # The asynchronous probe can finish after Textual has torn down
            # this dashboard. State remains useful for a live screen, but an
            # unmounted screen has nowhere safe to render it.
            return
        if result.availability is Availability.READY:
            label.update(
                "Harness Autopilot — READY · Codex / gemini-3.8-flash · "
                "reason: max · Start requires Enter/click"
            )
        else:
            label.update(
                f"Harness Autopilot — {result.availability.value} · "
                "[Diagnostics] [Sign in] [Install] [Retry] · no fallback"
            )
        try:
            self.refresh_rows()
        except Exception:
            pass

    @work(thread=True, group="hive", exclusive=True)
    def load_hive(
        self, reconciliation_request: int = 0, reconciliation_attempt: int = 0
    ) -> None:
        """Ask Hive what it is doing. Read-only, and never blocking.

        Hive probes are only retried by an explicit maintainer action. The
        exclusive worker prevents repeated key presses from stacking network
        calls while the hub is unavailable.
        """
        success = False
        try:
            if not hive_api_base():
                if not get_current_worker().is_cancelled:
                    self.call_from_thread(self.hive_not_configured)
                success = True
                return
            status = hive_get("/api/v1/status")
            if not status.ok:
                if not get_current_worker().is_cancelled:
                    self.call_from_thread(self.hive_failed, status.message)
                return
            contributor_result = hive_get("/api/v1/contributors")
            if not contributor_result.ok:
                if not get_current_worker().is_cancelled:
                    self.call_from_thread(self.hive_failed, contributor_result.message)
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
            ready_res = hive_get("/api/contribute/queue")
            ready_items = parse_hive_ready_queue(ready_res.data) if ready_res.ok else []
            ready_ok = ready_res.ok and (
                isinstance(ready_res.data, list)
                or (isinstance(ready_res.data, dict) and ("queue" in ready_res.data or "items" in ready_res.data))
            )

            triage_res = hive_get("/api/contribute/triage")
            triage_groups = parse_hive_triage(triage_res.data) if triage_res.ok else []
            triage_ok = triage_res.ok and isinstance(triage_res.data, (dict, list))

            if get_current_worker().is_cancelled:
                return
            success = bool(status.ok and contributor_result.ok)
            self.call_from_thread(
                self.hive_loaded,
                state,
                workers,
                ready_items,
                triage_groups,
                ready_ok,
                triage_ok,
            )
        finally:
            if reconciliation_request:
                self.call_from_thread(
                    self._reconciliation_finished,
                    "hive",
                    reconciliation_request,
                    reconciliation_attempt,
                    success,
                )

    def hive_loaded(
        self,
        state: str,
        workers: list[dict],
        ready_items: list[dict] | None = None,
        triage_groups: list[dict] | None = None,
        ready_ok: bool = True,
        triage_ok: bool = True,
    ) -> None:
        self.hive_state = state
        self.hive_workers = workers
        self.hive_unavailable = False
        self.hive_workers_stale = False
        self.hive_snapshot_at = time.monotonic()

        if ready_items is not None or triage_groups is not None:
            if ready_ok or triage_ok:
                new_ready = ready_items if ready_ok else self.hive_ready_queue
                new_triage = triage_groups if triage_ok else self.hive_triage_groups
                self.hive_ranks = build_hive_rank_map(new_ready or [], new_triage or [])
                if ready_ok:
                    self.hive_ready_queue = new_ready or []
                    self.hive_queue_unavailable = False
                    self.hive_queue_stale = False
                else:
                    self.hive_queue_unavailable = True
                    self.hive_queue_stale = bool(self.hive_ready_queue)
                if triage_ok:
                    self.hive_triage_groups = new_triage or []
                    self.hive_triage_unavailable = False
                    self.hive_triage_stale = False
                else:
                    self.hive_triage_unavailable = True
                    self.hive_triage_stale = bool(self.hive_triage_groups)
            else:
                self.hive_queue_unavailable = True
                self.hive_queue_stale = bool(self.hive_ready_queue or self.hive_ranks)
                self.hive_triage_unavailable = True
                self.hive_triage_stale = bool(self.hive_triage_groups or self.hive_ranks)

        self.refresh_rows()
        stop = self.current
        if stop:
            self.render_context(stop)

    def hive_failed(self, state: str) -> None:
        """Keep the dashboard usable when a read-only Hive probe fails."""
        self.hive_state = state
        self.hive_unavailable = True
        self.hive_workers_stale = bool(self.hive_workers)
        self.hive_queue_unavailable = True
        self.hive_queue_stale = bool(self.hive_ranks or self.hive_ready_queue)
        self.hive_triage_unavailable = True
        self.hive_triage_stale = bool(self.hive_ranks or self.hive_triage_groups)
        self.refresh_status()
        stop = self.current
        if stop:
            self.render_context(stop)

    def hive_not_configured(self) -> None:
        """Clear Hive state when this dashboard has no Hive projection."""
        self.hive_state = "not configured"
        self.hive_workers = []
        self.hive_unavailable = False
        self.hive_workers_stale = False
        self.refresh_status()
        stop = self.current
        if stop:
            self.render_context(stop)

    def _request_reconciliation(self) -> None:
        """Refresh retained GitHub and Hive evidence after a completed operation."""
        if self._reconciliation_waiting:
            self._reconciliation_pending = True
            return
        self._start_reconciliation()

    def _start_reconciliation(self) -> None:
        self._reconciliation_request += 1
        request = self._reconciliation_request
        self._reconciliation_waiting = {"queue", "hive"}
        self._reconciliation_success = {"queue": False, "hive": False}
        self.reconciliation_state = "refreshing"
        self.reselect = {stop.key for stop in self.stops if stop.selected}
        self.refresh_status()
        self._start_reconciliation_source("queue", request)
        self._start_reconciliation_source("hive", request)

    def _start_reconciliation_source(self, source: str, request: int) -> None:
        """Start one source attempt and identify its eventual callback."""
        self._reconciliation_source_attempts[source] += 1
        attempt = self._reconciliation_source_attempts[source]
        if source == "queue":
            self.load_queue(request, attempt)
        else:
            self.load_hive(request, attempt)

    def _reconciliation_finished(
        self, source: str, request: int, attempt: int, success: bool
    ) -> None:
        if not request or request != self._reconciliation_request:
            return
        if attempt != self._reconciliation_source_attempts.get(source):
            return
        self._reconciliation_success[source] = success
        self._reconciliation_waiting.discard(source)
        if self._reconciliation_waiting:
            return
        self.reconciliation_state = (
            "fresh"
            if all(self._reconciliation_success.values())
            else "unavailable"
        )
        if self.reconciliation_state == "fresh":
            self.reconciliation_updated_at = time.monotonic()
        self.refresh_status()
        if self._reconciliation_pending:
            self._reconciliation_pending = False
            self._start_reconciliation()

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
        if self.hive_workers_stale:
            return None
        return self.last_known_hive_worker_for(stop)

    def last_known_hive_worker_for(self, stop: Stop) -> dict | None:
        """Return the last worker evidence, even when the hub is unavailable."""
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
        request = self._reconciliation_request if self._reconciliation_waiting else 0
        if request:
            self._start_reconciliation_source("hive", request)
        else:
            self.load_hive()

    def action_steer(self) -> None:
        """Focus the steering box: free text that rides along with the next
        review of the highlighted stop as maintainer instructions."""
        if self.view_mode == "issues":
            self.notify("action applies to pull requests only", severity="warning")
            return
        if not self.current:
            self.notify("nothing highlighted to steer.", severity="warning")
            return
        if self.current.is_issue:
            self.notify("action applies to pull requests only", severity="warning")
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
        if stop.is_issue:
            self.notify("action applies to pull requests only", severity="warning")
            return
        self.start_review(stop, steer)

    def start_review(self, stop: Stop, steer: str = "") -> None:
        if stop.is_issue:
            self.notify("action applies to pull requests only", severity="warning")
            return
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
                    self.push_screen(
                        ReviewScreen(
                            stop,
                            steer=steer,
                            selection=selection,
                            headroom_session=self.headroom_session,
                            headroom_lock=self.headroom_lock,
                        )
                    )
            self.push_screen(HarnessTakeoff(options, initial, initial_preference), selected)
            return
        self.push_screen(
            ReviewScreen(
                stop,
                steer=steer,
                headroom_session=self.headroom_session,
                headroom_lock=self.headroom_lock,
            )
        )

    @work(thread=True)
    def start_review_batch(
        self, stops: list[Stop], *, model: str | None = None, effort: str | None = None
    ) -> None:
        from tui.review_snapshot import hydrate_batch_snapshot

        snapshot = hydrate_batch_snapshot(stops, fetch_live_review)
        self.call_from_thread(
            self.begin_review_batch, list(stops), snapshot, model=model, effort=effort
        )

    def begin_review_batch(
        self,
        stops: list[Stop],
        snapshot: BatchSnapshot,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        requested_keys = {stop.key for stop in stops}
        self.review_pending_keys.difference_update(requested_keys)
        current_by_key = {stop.key: stop for stop in self.stops}
        if not requested_keys.issubset(current_by_key):
            for stop in stops:
                id_ = self.active_review_identities.get(stop.key) or self.safe_run_identity(stop)
                rec = self.run_store.get(id_) if id_ else None
                if rec and rec.state in (RunState.REVIEWING, RunState.RE_REVIEWING):
                    self.run_store.transition(id_, RunState.REVIEW_FAILED, reason="queue changed")
            self.notify(
                "batch review not started: the visible queue changed.",
                severity="warning",
            )
            return
        current_stops = [current_by_key[stop.key] for stop in stops]
        if not snapshot.ready:
            for stop in current_stops:
                try:
                    id_ = self.active_review_identities.get(stop.key) or self.run_identity(stop)
                except ValueError:
                    id_ = None
                if id_:
                    rec = self.run_store.get(id_)
                    if rec and rec.state in (RunState.REVIEWING, RunState.RE_REVIEWING):
                        self.run_store.transition(id_, RunState.REVIEW_FAILED, reason="snapshot failed")
                failure = snapshot.failures.get(stop.key)
                if failure:
                    stop.failure = f"review snapshot failed: {failure}"
                    stop.failure_command = "gh pr view"
                    stop.review_status = "failed"
                    stop.review_failure = failure
                    stop.review_result = None
                    stop.cached_age = ""
            self.refresh_rows()
            self.notify(
                "batch review not started: exact-head snapshot failed.",
                severity="error",
            )
            return
        by_key = {item.key: item for item in snapshot.items}
        stale = [
            stop
            for stop in current_stops
            if stop.head_identity
            and stop.head_identity != by_key[stop.key].head_sha
        ]
        if stale:
            for stop in stale:
                try:
                    id_ = self.active_review_identities.get(stop.key) or self.run_identity(stop)
                except ValueError:
                    id_ = None
                if id_:
                    rec = self.run_store.get(id_)
                    if rec and rec.state in (RunState.REVIEWING, RunState.RE_REVIEWING):
                        self.run_store.revalidate_head(id_, by_key[stop.key].head_sha)
                stop.failure = "review snapshot stale: head changed"
                stop.failure_command = "gh pr view"
                stop.review_status = "failed"
                stop.review_failure = "head changed during snapshot"
                stop.review_result = None
                stop.cached_age = ""
            self.refresh_rows()
            self.notify(
                "batch review not started: a selected head changed.",
                severity="warning",
            )
            return
        default_model, default_effort = self.review_profile(
            current_stops[0].repository
        )
        selected_model = model or default_model
        selected_effort = effort or default_effort
        for stop in current_stops:
            item = by_key[stop.key]
            self.evidence_generation[stop.key] = (
                self.evidence_generation.get(stop.key, 0) + 1
            )
            stop.live = dict(item.live)
            stop.live.update(
                live_review_context(stop.live, title=stop.title)
            )
            stop.live["repository"] = stop.repository
            stop.live["number"] = stop.number
            stop.head_sha = item.head_sha
            stop.review_status = "running"
            clear_review_failure_mark(stop)
            stop.review_result = None
            stop.cached_age = ""
            stop.triage_state = "reviewed"
            self.triage[self.triage_key(stop)] = "reviewed"
            _bound_map(self.triage, MAX_TRIAGE_ENTRIES)
            self.active_review_identities[stop.key] = self.run_identity(
                stop, model=selected_model, effort=selected_effort, head_sha=item.head_sha
            )
        event_heads = {
            item.key: item.head_sha for item in snapshot.items
        }
        self.review_expected_heads.update(event_heads)
        review_started = time.monotonic()
        for key in event_heads:
            self.review_started_at[key] = review_started
        try:
            batch = self.review_engine.start(
                snapshot,
                ACTIVE_BACKEND,
                selected_model,
                selected_effort,
                check_scope_version=self.review_scope_version,
                check_scope=self.review_scope,
                on_event=self.review_event,
            )
        except (OSError, RuntimeError, ValueError) as error:
            for key in event_heads:
                self.review_expected_heads.pop(key, None)
                self.review_started_at.pop(key, None)
            detail = bounded_detail(str(error) or type(error).__name__)
            for stop in current_stops:
                try:
                    id_ = self.run_identity(stop)
                    rec = self.run_store.get(id_)
                    if rec and rec.state == RunState.REVIEWING:
                        self.run_store.transition(id_, RunState.REVIEW_FAILED, reason=detail)
                except ValueError:
                    pass
                stop.review_status = "failed"
                stop.review_failure = detail
                stop.failure = f"review dispatch failed: {detail}"
                stop.failure_command = "bluefin-review receipt"
            self.refresh_rows()
            self.notify(
                f"batch review not started: {detail}",
                severity="error",
            )
            return
        self.review_batches.append(batch)
        self.prune_review_batches()
        for item in batch.items:
            self.review_batch_ids[item.key] = batch.batch_id
        pending_events = self.pending_review_events.pop(
            batch.batch_id, []
        )
        for event in pending_events:
            self.apply_review_event(event)
        self.watch_review_batch(batch)
        self.refresh_rows()

    def review_event(self, event: ReviewEvent) -> None:
        if threading.current_thread() is threading.main_thread():
            self.apply_review_event(event)
        else:
            self.call_from_thread(self.apply_review_event, event)

    def apply_review_event(self, event: ReviewEvent) -> None:
        stop = next(
            (candidate for candidate in self.stops if candidate.key == event.key),
            None,
        )
        if stop is None:
            return
        id_: RunIdentity | None = None
        if (
            event.batch_id
            and self.review_batch_ids.get(event.key) != event.batch_id
        ):
            if not any(
                batch.batch_id == event.batch_id
                for batch in self.review_batches
            ):
                self.pending_review_events.setdefault(
                    event.batch_id, []
                ).append(event)
            return
        expected_head = (
            event.head_sha
            or self.review_expected_heads.get(event.key)
        )
        if (
            expected_head
            and stop.head_identity
            and expected_head != stop.head_identity
        ):
            id_ = self.active_review_identities.get(event.key) or self.safe_run_identity(stop)
            rec = self.run_store.get(id_) if id_ else None
            if rec and rec.state == RunState.REVIEWING:
                self.run_store.revalidate_head(id_, stop.head_identity)
            if event.state in {
                "cached",
                "complete",
                "findings",
                "failed",
                "cancelled",
            }:
                self.review_expected_heads.pop(event.key, None)
                self.review_batch_ids.pop(event.key, None)
                self.active_review_identities.pop(event.key, None)
                stop.review_status = ""
                stop.review_result = None
                stop.cached_age = ""
            return
        batch = (
            next(
                (
                    candidate
                    for candidate in self.review_batches
                    if candidate.batch_id == event.batch_id
                ),
                None,
            )
            if event.batch_id
            else next(
                (
                    candidate
                    for candidate in reversed(self.review_batches)
                    if any(
                        item.key == event.key
                        for item in candidate.items
                    )
                ),
                None,
            )
        )
        batch_item = (
            next(
                (item for item in batch.items if item.key == event.key),
                None,
            )
            if batch is not None
            else None
        )
        if (
            batch_item is not None
            and stop.head_identity
            and batch_item.head_sha != stop.head_identity
        ):
            id_ = self.active_review_identities.get(event.key) or self.safe_run_identity(stop)
            rec = self.run_store.get(id_) if id_ else None
            if rec and rec.state == RunState.REVIEWING:
                self.run_store.revalidate_head(id_, stop.head_identity)
            stop.review_status = ""
            stop.review_result = None
            stop.cached_age = ""
            return
        if event.receipt:
            receipt_name = os.path.basename(event.receipt)
            id_ = self.active_review_identities.get(event.key) or self.safe_run_identity(stop)
            rec = self.run_store.get(id_) if id_ else None
            if receipt_name != event.receipt:
                stop.review_status = "review_failed"
                stop.review_failure = "invalid review receipt path"
                self.notify(
                    f"{stop.key}: invalid review receipt path",
                    severity="error",
                )
                if rec and rec.state == RunState.REVIEWING:
                    self.run_store.transition(id_, RunState.REVIEW_FAILED, reason="invalid review receipt path")
            else:
                cache = getattr(
                    self.review_engine, "cache", self.review_cache
                )
                receipt_path = cache.root / receipt_name
                if not receipt_path.exists():
                    stop.review_status = "review_missing"
                    stop.review_failure = "review receipt missing"
                    self.notify(
                        f"{stop.key}: review receipt missing",
                        severity="error",
                    )
                    if rec and rec.state == RunState.REVIEWING:
                        self.run_store.transition(id_, RunState.REVIEW_MISSING, reason="review receipt missing")
                else:
                    try:
                        receipt = ReviewReceipt.from_json(
                            receipt_path.read_text(
                                encoding="utf-8"
                            )
                        )
                    except (
                        json.JSONDecodeError,
                        UnicodeError,
                        ValueError,
                    ) as error:
                        stop.review_status = "review_unparsable"
                        stop.review_failure = bounded_detail(
                            str(error) or type(error).__name__
                        )
                        self.notify(
                            f"{stop.key}: review receipt unparsable: "
                            f"{bounded_detail(str(error))}",
                            severity="error",
                        )
                        if rec and rec.state == RunState.REVIEWING:
                            self.run_store.transition(id_, RunState.REVIEW_UNPARSABLE, reason=stop.review_failure)
                    except OSError as error:
                        stop.review_status = "review_failed"
                        stop.review_failure = bounded_detail(
                            str(error) or type(error).__name__
                        )
                        self.notify(
                            f"{stop.key}: review receipt unavailable: "
                            f"{bounded_detail(str(error))}",
                            severity="error",
                        )
                        if rec and rec.state == RunState.REVIEWING:
                            self.run_store.transition(id_, RunState.REVIEW_FAILED, reason=stop.review_failure)
                    else:
                        live_base = str(stop.live.get("baseRefOid") or "")
                        base_mismatch = (
                            receipt.identity.base_sha != live_base
                            if live_base
                            else (id_ is not None and receipt.identity.base_sha != id_.base_sha)
                        )
                        if (
                            receipt.identity.repository != stop.repository
                            or receipt.identity.pull_request != stop.number
                            or receipt.identity.head_sha != stop.head_identity
                            or base_mismatch
                        ):
                            if rec and rec.state == RunState.REVIEWING:
                                if receipt.identity.head_sha != stop.head_identity:
                                    self.run_store.revalidate_head(id_, stop.head_identity)
                                else:
                                    self.run_store.transition(id_, RunState.REVIEW_FAILED, reason="receipt identity mismatch")
                            self.review_expected_heads.pop(event.key, None)
                            self.review_batch_ids.pop(event.key, None)
                            self.active_review_identities.pop(event.key, None)
                            stop.review_status = ""
                            stop.review_result = None
                            stop.cached_age = ""
                            return
                        stop.review_result = receipt.analysis_result(
                            live=stop.live, overlap=stop.overlap
                        )
                        stop.review_failure = ""
                        stop.cached_age = (
                            self.receipt_age(receipt)
                            if event.state == "cached"
                            else ""
                        )
                        stop.review_status = event.state
        else:
            if event.state in {"missing", "review_missing"}:
                stop.review_status = "review_missing"
                stop.review_result = None
                stop.cached_age = ""
                stop.review_failure = event.note
            elif event.state in {"incomplete", "review_incomplete"} or (event.note and "incomplete" in event.note.lower()):
                stop.review_status = "review_incomplete"
                stop.review_result = None
                stop.cached_age = ""
                stop.review_failure = event.note
            elif event.state in {"unparsable", "review_unparsable"}:
                stop.review_status = "review_unparsable"
                stop.review_result = None
                stop.cached_age = ""
                stop.review_failure = event.note
            elif event.state in {"failed", "review_failed"}:
                stop.review_status = "failed"
                stop.review_result = None
                stop.cached_age = ""
                stop.review_failure = event.note
            else:
                stop.review_status = event.state

        terminal_states = {
            "cached",
            "complete",
            "findings",
            "failed",
            "cancelled",
            "missing",
            "incomplete",
            "unparsable",
            "review_failed",
            "review_missing",
            "review_incomplete",
            "review_unparsable",
        }
        if event.state in terminal_states or stop.review_status in terminal_states:
            id_ = self.active_review_identities.pop(event.key, None) or id_ or self.safe_run_identity(stop)
        else:
            id_ = self.active_review_identities.get(event.key) or id_ or self.safe_run_identity(stop)
        rec = self.run_store.get(id_) if id_ else None
        if rec and rec.state in (RunState.REVIEWING, RunState.RE_REVIEWING):
            if stop.review_status in {"cached", "complete", "findings"}:
                self.review_expected_heads.pop(event.key, None)
                self.review_batch_ids.pop(event.key, None)
                findings = list(stop.review_result.findings if stop.review_result else [])
                target_state = (
                    RunState.REVIEW_FINDINGS
                    if (findings or stop.review_status == "findings")
                    else RunState.REVIEW_CLEAN
                )
                self.run_store.transition(id_, target_state)
                self._dispatch_slay_landing(stop, identity=id_)
            elif (
                stop.review_status in {
                    "failed",
                    "cancelled",
                    "missing",
                    "incomplete",
                    "unparsable",
                    "review_failed",
                    "review_missing",
                    "review_incomplete",
                    "review_unparsable",
                }
                or event.state in {
                    "failed",
                    "cancelled",
                    "missing",
                    "incomplete",
                    "unparsable",
                    "review_failed",
                    "review_missing",
                    "review_incomplete",
                    "review_unparsable",
                }
            ):
                self.review_expected_heads.pop(event.key, None)
                self.review_batch_ids.pop(event.key, None)
                if stop.review_status in {"missing", "review_missing"} or event.state in {"missing", "review_missing"}:
                    self.run_store.transition(id_, RunState.REVIEW_MISSING, reason=stop.review_failure or "review missing")
                elif stop.review_status in {"incomplete", "review_incomplete"} or event.state in {"incomplete", "review_incomplete"}:
                    self.run_store.transition(id_, RunState.REVIEW_INCOMPLETE, reason=stop.review_failure or "review incomplete")
                elif stop.review_status in {"unparsable", "review_unparsable"} or event.state in {"unparsable", "review_unparsable"}:
                    self.run_store.transition(id_, RunState.REVIEW_UNPARSABLE, reason=stop.review_failure or "review unparsable")
                else:
                    self.run_store.transition(id_, RunState.REVIEW_FAILED, reason=stop.review_failure or "review failed")
                self.notify(
                    f"[$] {stop.key}: review {stop.review_status}, auto-landing aborted",
                    severity="warning",
                )
        elif event.state in {
            "cached",
            "complete",
            "findings",
            "failed",
            "cancelled",
        }:
            self.review_expected_heads.pop(event.key, None)
            self.review_batch_ids.pop(event.key, None)
        if batch is not None:
            self.sync_batch_headroom(batch)
        if event.state in terminal_states:
            started = self.review_started_at.pop(event.key, None)
            if started is not None:
                if stop.review_status == "findings":
                    outcome = "findings"
                elif event.state == "cancelled":
                    outcome = "cancelled"
                elif stop.review_status in {
                    "incomplete",
                    "unparsable",
                    "review_incomplete",
                    "review_unparsable",
                    "missing",
                    "review_missing",
                }:
                    outcome = "incomplete"
                elif stop.review_status in {"cached", "complete"}:
                    outcome = "complete"
                else:
                    outcome = "failed"
                self.observability.operation(
                    f"review.{outcome}", time.monotonic() - started
                )
        if event.state == "complete" and stop.review_status == "complete":
            self._request_reconciliation()
        self.refresh_rows()

    def sync_batch_headroom(self, batch: ReviewBatch) -> None:
        self.headroom_status_line = batch.headroom_status_line
        self.headroom_output_reduction = dict(
            batch.headroom_output_reduction
        )
        self.refresh_status()

    def prune_review_batches(self) -> None:
        """Bound the finished-batch history without dropping live work.

        Running batches are never evicted: the watcher and every event
        route through them. Only finished batches age out, oldest first.
        """
        if len(self.review_batches) <= MAX_REVIEW_BATCHES:
            return
        running = [batch for batch in self.review_batches if batch.running]
        finished = [batch for batch in self.review_batches if not batch.running]
        keep = max(0, MAX_REVIEW_BATCHES - len(running))
        evicted = {id(batch) for batch in finished[:-keep]} if keep else {
            id(batch) for batch in finished
        }
        if not evicted:
            return
        self.review_batches = [
            batch for batch in self.review_batches if id(batch) not in evicted
        ]

    def watch_review_batch(self, batch: ReviewBatch) -> None:
        self.sync_batch_headroom(batch)
        if batch.running:
            self.set_timer(
                0.5,
                lambda batch=batch: self.watch_review_batch(batch),
            )

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
            return
    def _dispatch_terminal_action(self, label: str, action) -> None:
        try:
            action()
        except Exception as error:
            self.notify(
                bounded_detail(f"{label} unavailable: {error}"),
                severity="error",
            )

    # ── data layer (walker parity) ────────────────────────────────────────

    @work(thread=True, group="queue", exclusive=True)
    def load_queue(
        self, reconciliation_request: int = 0, reconciliation_attempt: int = 0
    ) -> None:
        success = False
        try:
            with self._queue_load_lock:
                snapshot = self._load_queue_data()
            if get_current_worker().is_cancelled:
                return
            success = snapshot["state"] in {"ready", "empty"}
            self.call_from_thread(self._apply_queue_snapshot, snapshot)
        finally:
            if reconciliation_request:
                self.call_from_thread(
                    self._reconciliation_finished,
                    "queue",
                    reconciliation_request,
                    reconciliation_attempt,
                    success,
                )

    def _load_queue_data(self) -> dict:
        try:
            who = gh("api", "user", "--jq", ".login")
            identity_detail = (who.stderr or who.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            who = None
            identity_detail = str(error)
        login = who.stdout.strip() if who and who.returncode == 0 else ""
        if not login:
            return {
                "self_login": "",
                "state": "auth-failed",
                "message": bounded_detail(
                    identity_detail or "GitHub identity is unavailable; sign in and retry"
                ),
                "items": [],
            }
        snapshot = (
            self.load_live_queue(self.filters.live_repository)
            if self.filters.live
            else self.load_org_queue()
        )
        snapshot["self_login"] = login
        return snapshot

    def _apply_queue_snapshot(self, snapshot: dict) -> None:
        self.self_login = str(snapshot["self_login"])
        state = str(snapshot["state"])
        self.pr_source_state = state
        self.pr_source_message = str(snapshot.get("message", ""))
        if state in {"ready", "empty"}:
            self.all_items = snapshot["items"]
            self.queue_items = [
                item
                for item in self.all_items
                if not (self.self_login and item.get("author") == self.self_login)
            ]
            self.queue_snapshot_at = time.monotonic()
        self._sync_source_state()
        self.apply_filters(refreshed_source="prs")

    def load_org_queue(self) -> dict:
        """Every open pull request in the organization, live from GitHub.

        One paginated GraphQL search carries the evidence the recommended
        action is classified from; there is no static snapshot behind this.
        """
        started = time.monotonic()
        pages_count = 0
        items_count = 0

        def finished(state: str, message: str, items: list[dict]) -> dict:
            self.observability.operation(
                "queue.refresh",
                time.monotonic() - started,
                pages=pages_count,
                items=items_count,
            )
            return {"state": state, "message": message, "items": items}

        try:
            result = gh(
                "api", "graphql", "--paginate", "--slurp",
                "-f", f"query={ORG_QUEUE_QUERY}",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return finished(
                "error",
                bounded_detail(
                    f"GitHub could not list the {GITHUB_ORG} queue: {error}"
                ),
                [],
            )
        if result.returncode:
            detail = bounded_detail((result.stderr or result.stdout).strip())
            lowered = detail.lower()
            if ("authentication" in lowered or "login" in lowered
                    or "permission" in lowered or "forbidden" in lowered
                    or "not accessible" in lowered):
                state = "inaccessible"
            else:
                state = "error"
            return finished(
                state,
                detail or f"GitHub could not list the {GITHUB_ORG} queue",
                [],
            )
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
                raise ValueError("GitHub returned malformed search pages")
            items = []
            pages_count = len(pages)
            for page in pages:
                nodes = ((page.get("data") or {}).get("search") or {}).get("nodes")
                if not isinstance(nodes, list):
                    raise ValueError("GitHub returned a page without search nodes")
                for node in nodes:
                    if not isinstance(node, dict):
                        raise ValueError("GitHub returned a malformed pull-request node")
                    if not node:
                        continue
                    items.append(org_queue_item(node))
            items_count = len(items)
        except (json.JSONDecodeError, ValueError) as error:
            return finished(
                "malformed",
                bounded_detail(f"malformed GitHub response: {error}"),
                [],
            )
        return finished("empty" if not items else "ready", "", items)

    def load_live_queue(self, repository: str) -> dict:
        started = time.monotonic()
        pages_count = 0
        items_count = 0

        def finished(state: str, message: str, items: list[dict]) -> dict:
            self.observability.operation(
                "queue.refresh",
                time.monotonic() - started,
                pages=pages_count,
                items=items_count,
            )
            return {"state": state, "message": message, "items": items}

        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            return finished(
                "malformed",
                bounded_detail(
                    f"invalid repository '{repository}'; use owner/repo"
                ),
                [],
            )
        try:
            result = gh(
                "api", "--paginate", "--slurp", "--method", "GET",
                f"repos/{repository}/pulls?state=open&per_page=100",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return finished(
                "error",
                bounded_detail(
                    f"GitHub could not read this repository: {error}"
                ),
                [],
            )
        if result.returncode:
            detail = bounded_detail((result.stderr or result.stdout).strip())
            lowered = detail.lower()
            if ("authentication" in lowered or "login" in lowered
                    or "permission" in lowered or "forbidden" in lowered
                    or "not accessible" in lowered):
                state = "inaccessible"
            elif "not found" in lowered or "could not resolve" in lowered:
                state = "missing"
            else:
                state = "error"
            return finished(
                state, detail or "GitHub could not read this repository", []
            )
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("GitHub returned malformed pull-request pages")
            pages_count = len(pages)
            pulls = [pull for page in pages for pull in page]
            if any(not isinstance(pull, dict) for pull in pulls):
                raise ValueError("GitHub returned a malformed pull-request entry")
        except (json.JSONDecodeError, ValueError) as error:
            return finished(
                "malformed",
                bounded_detail(f"malformed GitHub response: {error}"),
                [],
            )
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
                is_cross = bool(pull.get("isCrossRepository", False))
                if not is_cross:
                    head_repo = (pull.get("head") or {}).get("repo") or {}
                    base_repo = (pull.get("base") or {}).get("repo") or {}
                    if head_repo and base_repo and head_repo.get("full_name") != base_repo.get("full_name"):
                        is_cross = True
                can_modify = pull.get("maintainerCanModify")
                if can_modify is None:
                    can_modify = pull.get("maintainer_can_modify")
                if can_modify is None:
                    can_modify = True
                else:
                    can_modify = bool(can_modify)
                is_draft = bool(pull.get("draft") if "draft" in pull else pull.get("isDraft", False))
                items.append({
                    "repository": repository,
                    "number": number,
                    "recommended_action": "review",
                    "title": pull["title"],
                    "author": author,
                    "mergeable_state": str(pull.get("mergeable", "") or "").lower(),
                    "check_state": "unknown",
                    "review_state": str(pull.get("reviewDecision", "") or "").lower(),
                    "base_sha": str(
                        pull.get("baseRefOid")
                        or (pull.get("base") or {}).get("sha")
                        or ""
                    ),
                    "head_sha": str(
                        pull.get("headRefOid")
                        or (pull.get("head") or {}).get("sha")
                        or ""
                    ),
                    "isDraft": is_draft,
                    "is_draft": is_draft,
                    "isCrossRepository": is_cross,
                    "is_cross_repository": is_cross,
                    "maintainerCanModify": can_modify,
                    "maintainer_can_modify": can_modify,
                })
            items_count = len(items)
        except ValueError as error:
            return finished(
                "malformed",
                bounded_detail(f"malformed GitHub response: {error}"),
                [],
            )
        return finished("empty" if not items else "ready", "", items)

    @work(thread=True, group="issues", exclusive=True)
    def load_issues(self) -> None:
        if self.filters.live:
            snapshot = self.load_live_issues(self.filters.live_repository)
        else:
            snapshot = self.load_org_issues()
        if get_current_worker().is_cancelled:
            return
        self.call_from_thread(self._apply_issues_snapshot, snapshot)

    def _apply_issues_snapshot(self, snapshot: dict) -> None:
        state = str(snapshot.get("state", "ready"))
        self.issues_source_state = state
        self.issues_source_message = str(snapshot.get("message", ""))
        if state in {"ready", "empty"}:
            self.issues_items = snapshot.get("items", [])
        self._sync_source_state()
        self.apply_filters(refreshed_source="issues")

    def load_org_issues(self) -> dict:
        """Every open issue in the organization, live from GitHub.

        One paginated GraphQL search carries open issues; there is no static snapshot behind this.
        """
        try:
            result = gh(
                "api", "graphql", "--paginate", "--slurp",
                "-f", f"query={ORG_ISSUES_QUERY}",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "state": "error",
                "message": bounded_detail(
                    f"GitHub could not list the {GITHUB_ORG} issues: {error}"
                ),
                "items": [],
            }
        if result.returncode:
            detail = bounded_detail((result.stderr or result.stdout).strip())
            lowered = detail.lower()
            if ("authentication" in lowered or "login" in lowered
                    or "permission" in lowered or "forbidden" in lowered
                    or "not accessible" in lowered):
                state = "inaccessible"
            else:
                state = "error"
            return {
                "state": state,
                "message": detail or f"GitHub could not list the {GITHUB_ORG} issues",
                "items": [],
            }
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
                raise ValueError("GitHub returned malformed search pages")
            items = []
            for page in pages:
                nodes = ((page.get("data") or {}).get("search") or {}).get("nodes")
                if not isinstance(nodes, list):
                    raise ValueError("GitHub returned a page without search nodes")
                for node in nodes:
                    if not isinstance(node, dict):
                        raise ValueError("GitHub returned a malformed issue node")
                    if not node:
                        continue
                    items.append(org_issue_item(node))
        except (json.JSONDecodeError, ValueError) as error:
            return {
                "state": "malformed",
                "message": bounded_detail(f"malformed GitHub response: {error}"),
                "items": [],
            }
        return {
            "state": "empty" if not items else "ready",
            "message": "",
            "items": items,
        }

    def load_live_issues(self, repository: str) -> dict:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            return {
                "state": "malformed",
                "message": bounded_detail(f"invalid repository '{repository}'; use owner/repo"),
                "items": [],
            }
        try:
            result = gh(
                "api", "--paginate", "--slurp", "--method", "GET",
                f"repos/{repository}/issues?state=open&per_page=100",
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "state": "error",
                "message": bounded_detail(f"GitHub could not read this repository: {error}"),
                "items": [],
            }
        if result.returncode:
            detail = bounded_detail((result.stderr or result.stdout).strip())
            lowered = detail.lower()
            if ("authentication" in lowered or "login" in lowered
                    or "permission" in lowered or "forbidden" in lowered
                    or "not accessible" in lowered):
                state = "inaccessible"
            elif "not found" in lowered or "could not resolve" in lowered:
                state = "missing"
            else:
                state = "error"
            return {
                "state": state,
                "message": detail or "GitHub could not read this repository",
                "items": [],
            }
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("GitHub returned malformed issue pages")
            raw_issues = [issue for page in pages for issue in page]
            if any(not isinstance(issue, dict) for issue in raw_issues):
                raise ValueError("GitHub returned a malformed issue entry")
        except (json.JSONDecodeError, ValueError) as error:
            return {
                "state": "malformed",
                "message": bounded_detail(f"malformed GitHub response: {error}"),
                "items": [],
            }
        try:
            items = []
            for index, issue in enumerate(raw_issues, 1):
                if "pull_request" in issue:
                    continue
                number = issue.get("number")
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    raise ValueError(f"issue {index} has invalid number")
                if not isinstance(issue.get("title"), str):
                    raise ValueError(f"issue {index} has invalid title")
                author_source = issue.get("user") if "user" in issue else issue.get("author")
                if author_source is not None and not isinstance(author_source, dict):
                    raise ValueError(f"issue {index} has invalid author")
                author = ""
                if author_source is not None and "login" in author_source:
                    if not isinstance(author_source["login"], str):
                        raise ValueError(f"issue {index} has invalid login")
                    author = author_source["login"]
                labels = []
                for label in issue.get("labels") or []:
                    if isinstance(label, dict) and isinstance(label.get("name"), str):
                        labels.append(label["name"])
                    elif isinstance(label, str):
                        labels.append(label)
                comments_count = issue.get("comments", 0)
                if not isinstance(comments_count, int) or isinstance(comments_count, bool):
                    comments_count = 0
                items.append({
                    "is_issue": True,
                    "repository": repository,
                    "number": number,
                    "action": "triage",
                    "recommended_action": "triage",
                    "title": issue["title"],
                    "author": author,
                    "labels": labels,
                    "comments_count": comments_count,
                    "body": str(issue.get("body") or ""),
                    "created_at": str(issue.get("created_at", "") or ""),
                    "updated_at": str(issue.get("updated_at", "") or ""),
                })
        except ValueError as error:
            return {
                "state": "malformed",
                "message": bounded_detail(f"malformed GitHub response: {error}"),
                "items": [],
            }
        return {
            "state": "empty" if not items else "ready",
            "message": "",
            "items": items,
        }

    def apply_filters(self, refreshed_source: str | None = None) -> None:
        record_snapshot = (
            self.run_store.snapshot()
            if hasattr(self, "run_store") and hasattr(self.run_store, "snapshot")
            else None
        )
        self._repaint_record_snapshot = record_snapshot
        self._repaint_profile_cache = {}
        self._repaint_ident_cache = {}
        self._repaint_blocked_cache = {}
        self._repaint_preferences = load_preferences()
        try:
            prior_stops = {stop.key: stop for stop in self.stops}
            for stop in self.stops:
                if refreshed_source in ("prs", "pr") and stop.is_issue:
                    continue
                if refreshed_source in ("issues", "issue") and not stop.is_issue:
                    continue
                self.evidence_generation[stop.key] = (
                    self.evidence_generation.get(stop.key, 0) + 1
                )
            stops: list[Stop] = []

            if self.filters.wants_kind({"is_issue": False}):
                for item in self.queue_items:
                    if not self.filters.wants(item):
                        continue
                    head_sha = item.get("head_sha", "") or ""
                    key = f"{item['repository']}#{item['number']}"
                    is_draft = bool(item.get("isDraft") if "isDraft" in item else item.get("is_draft", False))
                    is_cross = bool(item.get("isCrossRepository") if "isCrossRepository" in item else item.get("is_cross_repository", False))
                    can_modify = item.get("maintainerCanModify") if "maintainerCanModify" in item else item.get("maintainer_can_modify", True)
                    if can_modify is None:
                        can_modify = True
                    else:
                        can_modify = bool(can_modify)
                    stop = prior_stops.get(key)
                    if stop is None or stop.is_issue:
                        stop = Stop(
                            repository=item["repository"],
                            number=item["number"],
                            action=item.get("recommended_action", ""),
                            title=item.get("title", ""),
                            author=item.get("author", "") or "",
                            mergeable_state=item.get("mergeable_state", "") or "",
                            check_state=item.get("check_state", "") or "",
                            review_state=item.get("review_state", "") or "",
                            live={
                                "baseRefOid": item.get("base_sha", "") or "",
                                "headRefOid": head_sha,
                                "reviews": item.get("reviews", []),
                                "isDraft": is_draft,
                                "isCrossRepository": is_cross,
                                "maintainerCanModify": can_modify,
                            },
                            head_sha=head_sha,
                            is_issue=False,
                        )
                    else:
                        stop.action = item.get("recommended_action", "")
                        stop.title = item.get("title", "")
                        stop.author = item.get("author", "") or ""
                        stop.mergeable_state = item.get("mergeable_state", "") or ""
                        stop.check_state = item.get("check_state", "") or ""
                        stop.review_state = item.get("review_state", "") or ""
                        stop.live.update({
                            "baseRefOid": item.get("base_sha", "") or "",
                            "headRefOid": head_sha,
                            "reviews": item.get("reviews", []),
                            "isDraft": is_draft,
                            "isCrossRepository": is_cross,
                            "maintainerCanModify": can_modify,
                        })
                        stop.head_sha = head_sha
                    stop.triage_state = self.triage.get(
                        self.triage_key(stop), "unseen"
                    )
                    stops.append(stop)

            if self.filters.wants_kind({"is_issue": True}):
                for item in self.issues_items:
                    if not self.filters.wants(item):
                        continue
                    key = f"{item['repository']}#{item['number']}"
                    stop = prior_stops.get(key)
                    if stop is None or not stop.is_issue:
                        stop = Stop(
                            repository=item["repository"],
                            number=item["number"],
                            action=item.get("action", "triage"),
                            title=item.get("title", ""),
                            author=item.get("author", "") or "",
                            live={
                                "labels": item.get("labels", []),
                                "comments_count": item.get("comments_count", 0),
                                "body": item.get("body", ""),
                                "created_at": item.get("created_at", ""),
                                "updated_at": item.get("updated_at", ""),
                            },
                            is_issue=True,
                        )
                    else:
                        stop.action = item.get("action", "triage")
                        stop.title = item.get("title", "")
                        stop.author = item.get("author", "") or ""
                        stop.live.update({
                            "labels": item.get("labels", []),
                            "comments_count": item.get("comments_count", 0),
                            "body": item.get("body", ""),
                            "created_at": item.get("created_at", ""),
                            "updated_at": item.get("updated_at", ""),
                        })
                    stop.triage_state = self.triage.get(
                        self.triage_key(stop), "unseen"
                    )
                    stops.append(stop)

            self.restore_landing_marks(stops)
            if self.reselect:
                for stop in stops:
                    stop.selected = stop.key in self.reselect
                self.reselect = set()

            self.stops = stops
            self._sort_stops(record_snapshot=record_snapshot)
            empty_source = not self.active_source_items and self.source_state in ("empty", "ready")
            if empty_source:
                if self._had_nonempty_queue and self.slay_frame < 0:
                    self.start_slay_sequence()
                    return
                elif not self._had_nonempty_queue and self.slay_frame < 0:
                    self.slay_frame = len(SLAY_FRAMES) - 1
            elif self.active_source_items:
                self._had_nonempty_queue = True
                self.slay_frame = -1

            self.populate(stops, record_snapshot=record_snapshot)
        finally:
            self._repaint_record_snapshot = None
            self._repaint_profile_cache = None
            self._repaint_ident_cache = None
            self._repaint_blocked_cache = None
            self._repaint_preferences = None

    def _sort_stops(
        self, record_snapshot: dict[str, RunRecord] | None = None
    ) -> None:
        self.stops.sort(
            key=lambda stop: (
                QUEUE_STATE_RANK.get(
                    self._queue_state(stop, record_snapshot=record_snapshot), 99
                ),
                0 if self.stop_lacks_my_review(stop) else 1,
                action_rank(stop.action),
                stop.repository,
                stop.number,
            )
        )

    def _advance_slay_frame(self) -> None:
        if 0 <= self.slay_frame < len(SLAY_FRAMES) - 1:
            self.slay_frame += 1
            self.populate(self.stops)
            if self.slay_frame < len(SLAY_FRAMES) - 1:
                delay = SLAY_DELAYS[self.slay_frame]
                self.set_timer(delay, self._advance_slay_frame)

    def start_slay_sequence(self) -> None:
        self.slay_frame = 0
        self.populate(self.stops)
        try:
            self.set_timer(SLAY_DELAYS[0], self._advance_slay_frame)
        except RuntimeError:
            pass

    def row_markup(
        self, stop: Stop, record_snapshot: dict[str, RunRecord] | None = None
    ) -> str:
        # Selection is not colour-only: a ● leads the row and the whole row
        # carries a background, so the batch in progress reads at a glance.
        selected = "● " if stop.selected else "  "
        if stop.is_issue:
            labels_source = stop.live.get("labels", [])
            labels_list = []
            for l in labels_source:
                if isinstance(l, dict) and "name" in l:
                    labels_list.append(l["name"])
                elif isinstance(l, str):
                    labels_list.append(l)
            labels_str = f" {escape('[' + ', '.join(labels_list) + ']')}" if labels_list else ""
            comments_source = stop.live.get("comments")
            if isinstance(comments_source, list):
                comments_count = len(comments_source)
            elif isinstance(comments_source, dict):
                comments_count = comments_source.get("totalCount", 0)
            elif isinstance(stop.live.get("comments_count"), int):
                comments_count = stop.live.get("comments_count", 0)
            else:
                comments_count = 0
            hive_rank_str = self.hive_rank_display(stop)
            body = (
                f"{selected}{link(stop.key, issue_url(stop.repository, stop.number))}: "
                f"{escape(stop.title[:60])}{labels_str}{hive_rank_str} "
                f"({comments_count} comments) {escape('[triage]')}"
            )
            style = stop_style(stop.action, "", "", "")
            return f"[{style}]{body}[/{style}]" if style else body
        # MECHANICAL replaces the old title-only BATCHABLE tag: it means this
        # branch can be brought current, never that the change is approved.
        tag = " (MECHANICAL)" if stop.mechanical else ""
        # A stop that would not merge says so on its own row, so a failure in
        # the middle of a batch survives the notification that reported it.
        failed = " ✗ DID NOT MERGE" if stop.failure else ""
        marks = self._review_badge(stop, record_snapshot=record_snapshot)
        checks = effective_check_state(stop.check_state, stop.live)
        if stop.mergeable_state == "dirty":
            marks += " ⚑ CONFLICTS"
        marks += f" {ci_marker(checks)}"
        if stop.review_state == "approved":
            marks += " ✓ approved"
        hive_rank_str = self.hive_rank_display(stop)
        body = (
            f"{selected}{link(stop.key, pr_url(stop.repository, stop.number))}: "
            f"{escape(stop.title[:60])}{tag}{hive_rank_str} "
            f"{marks} {escape('[' + stop.action + ']')}{failed}"
        )
        style = stop_style(
            stop.action, stop.mergeable_state, checks, stop.review_state
        )
        return f"[{style}]{body}[/{style}]" if style else body

    def _queue_state(
        self_or_stop: Any,
        maybe_stop: Stop | None = None,
        record_snapshot: dict[str, RunRecord] | None = None,
    ) -> str:
        if maybe_stop is not None:
            self = self_or_stop
            stop = maybe_stop
        else:
            self = None
            stop = self_or_stop

        blocked = (
            self.stop_blocked_reason(stop, record_snapshot=record_snapshot)
            if self is not None and hasattr(self, "stop_blocked_reason")
            else classify_routability(stop)
        )
        if not stop.is_issue and blocked:
            return "blocked"
        if (
            stop.failure
            or stop.review_status in REVIEW_FAILURES
            or stop.mergeable_state == "dirty"
            or effective_check_state(stop.check_state, stop.live) == "failure"
        ):
            return "failed"
        if stop.review_status in {"queued", "running"}:
            return "in progress"
        if stop.review_result is not None:
            return "done"
        if stop.selected:
            return "queued"
        return "ready"

    def _review_badge(
        self_or_cls: Any,
        stop: Stop,
        record_snapshot: dict[str, RunRecord] | None = None,
    ) -> str:
        state = (
            self_or_cls._queue_state(stop, record_snapshot=record_snapshot)
            if hasattr(self_or_cls, "_queue_state")
            else ReviewDashboard._queue_state(stop, record_snapshot=record_snapshot)
        )
        age = f" {stop.cached_age}" if stop.cached_age else ""
        if state == "blocked":
            return "[bold yellow]⛔ BLOCKED[/bold yellow]"
        if state == "failed":
            return "[bold red]✗ FAILED[/bold red]"
        if state == "in progress":
            return f"[bold yellow]⏳ IN PROGRESS[/bold yellow]{age}"
        if state == "done":
            badge = "✓" if stop.review_result and stop.review_result.is_clean else "✗"
            cached = f" cached {stop.cached_age}" if stop.cached_age else ""
            return f"[bold green]{badge} DONE[/bold green]{cached}"
        if state == "queued":
            return "[bold cyan]QUEUED[/bold cyan]"
        return "[dim]READY[/dim]"

    def populate(
        self, stops: list[Stop], record_snapshot: dict[str, RunRecord] | None = None
    ) -> None:
        self.stops = stops
        try:
            queue = self.query_one("#queue", ListView)
        except (NoMatches, ScreenStackError):
            return
        queue.clear()
        if not stops:
            empty_source = not self.active_source_items and self.source_state in ("empty", "ready")
            if self.source_state not in ("ready", "empty"):
                noun = "issues" if self.view_mode == "issues" else ("pull requests" if self.view_mode == "prs" else "workboard")
                err = f"Could not load {noun} ({self.source_state})"
                if self.source_message:
                    err += f": {self.source_message}"
                queue.append(ListItem(Static(f"[bold red]{escape(err)}[/bold red]")))
                try:
                    details = self.query_one("#details", Static)
                    details.update(f"[bold red]{escape(err)}[/bold red]")
                    context = self.query_one("#context", Static)
                    context.update("")
                except (NoMatches, ScreenStackError):
                    pass
            elif self.view_mode == "issues":
                if not stops and self.issues_items:
                    queue.append(ListItem(Static("[dim]No issues match the active filter. Press [bold]f[/bold] to widen.[/dim]")))
                    try:
                        details = self.query_one("#details", Static)
                        details.update("[dim]No issues match the active filter.[/dim]")
                        context = self.query_one("#context", Static)
                        context.update("")
                    except (NoMatches, ScreenStackError):
                        pass
                else:
                    queue.append(ListItem(Static("[dim]No open issues found.[/dim]")))
                    try:
                        details = self.query_one("#details", Static)
                        details.update("[dim]No open issues.[/dim]")
                        context = self.query_one("#context", Static)
                        context.update("")
                    except (NoMatches, ScreenStackError):
                        pass
            elif empty_source and self.slay_frame >= 0:
                frame_text = SLAY_FRAMES[self.slay_frame]
                queue.append(ListItem(Static(frame_text)))
                try:
                    details = self.query_one("#details", Static)
                    details.update("[bold cyan]ALL SYSTEMS SLAY[/bold cyan]\n\n[green]★[/green] Review queue fully drained.\nEnjoy the victory.")
                except (NoMatches, ScreenStackError):
                    pass
            elif not stops and self.active_source_items:
                queue.append(ListItem(Static("[dim]No items match the active filter. Press [bold]f[/bold] to widen.[/dim]")))
            else:
                queue.append(ListItem(Static("[dim]No open pull requests or issues found.[/dim]")))
        else:
            for stop in stops:
                item = ListItem(Label(self.row_markup(stop, record_snapshot=record_snapshot)))
                item.set_class(stop.selected, "selected")
                queue.append(item)
            queue.index = 0
        self.refresh_status(record_snapshot=record_snapshot)

    def refresh_rows(self) -> None:
        """Repaint queue lifecycle changes in their current priority order."""
        try:
            queue = self.query_one("#queue", ListView)
        except (NoMatches, ScreenStackError):
            return
        current = self.current
        current_key = current.key if current else ""
        prior_order = [stop.key for stop in self.stops]

        record_snapshot = (
            self.run_store.snapshot()
            if hasattr(self, "run_store") and hasattr(self.run_store, "snapshot")
            else None
        )
        self._repaint_record_snapshot = record_snapshot
        self._repaint_profile_cache = {}
        self._repaint_ident_cache = {}
        self._repaint_blocked_cache = {}
        self._repaint_preferences = load_preferences()
        try:
            self._sort_stops(record_snapshot=record_snapshot)
            if (
                prior_order == [stop.key for stop in self.stops]
                and len(queue.children) == len(self.stops)
                and all(list(item.query(Label)) for item in queue.children)
            ):
                for stop, item in zip(self.stops, queue.children):
                    item.query(Label).first().update(
                        self.row_markup(stop, record_snapshot=record_snapshot)
                    )
                    item.set_class(stop.selected, "selected")
                self.refresh_status(record_snapshot=record_snapshot)
                return
            self.populate(self.stops, record_snapshot=record_snapshot)
            if current_key:
                queue.index = next(
                    (
                        index
                        for index, stop in enumerate(self.stops)
                        if stop.key == current_key
                    ),
                    0,
                )
        finally:
            self._repaint_record_snapshot = None
            self._repaint_profile_cache = None
            self._repaint_ident_cache = None
            self._repaint_blocked_cache = None
            self._repaint_preferences = None

    def refresh_selection_rows(self) -> None:
        """Repaint batch markers without re-sorting the queue."""
        try:
            queue = self.query_one("#queue", ListView)
        except (NoMatches, ScreenStackError):
            return
        record_snapshot = self.run_store.snapshot()
        self._repaint_record_snapshot = record_snapshot
        self._repaint_profile_cache = {}
        self._repaint_ident_cache = {}
        self._repaint_blocked_cache = {}
        self._repaint_preferences = load_preferences()
        try:
            for stop, item in zip(self.stops, queue.children):
                labels = item.query(Label)
                if labels:
                    labels.first().update(
                        self.row_markup(stop, record_snapshot=record_snapshot)
                    )
                item.set_class(stop.selected, "selected")
            self.refresh_status(record_snapshot=record_snapshot)
        finally:
            self._repaint_record_snapshot = None
            self._repaint_profile_cache = None
            self._repaint_ident_cache = None
            self._repaint_blocked_cache = None
            self._repaint_preferences = None

    def _activity_freshness(self) -> str:
        timestamps = [
            timestamp
            for timestamp in (
                self.queue_snapshot_at,
                self.hive_snapshot_at,
                self.reconciliation_updated_at,
            )
            if timestamp is not None
        ]
        age = activity_age(min(timestamps)) if timestamps else ""
        if self.reconciliation_state == "refreshing":
            return (
                f"refreshing — last good {age}"
                if age else "refreshing"
            )
        if self.hive_unavailable or self.reconciliation_state == "unavailable":
            return (
                f"retained/last good — {age}"
                if age else "unavailable"
            )
        if timestamps:
            return f"current — {age}" if age else "current"
        return "unavailable"

    @staticmethod
    def _activity_work(keys: list[str]) -> str:
        shown = [
            bounded_detail(key)[:MAX_ACTIVITY_TEXT]
            for key in keys[:MAX_ACTIVITY_WORK_KEYS]
        ]
        if len(keys) > len(shown):
            shown.append(f"+{len(keys) - len(shown)}")
        return ", ".join(shown)[:MAX_ACTIVITY_TEXT] or "assignment unavailable"

    def _review_activity_rows(self) -> list[str]:
        """Summarize selected review state without another GitHub request."""
        selected = [stop for stop in self.stops if stop.selected]
        if not selected:
            return ["Review status: select a PR to inspect its review state"]
        active = []
        drafts = []
        submitted = []
        for stop in selected:
            if stop.review_status in {"queued", "running"}:
                active.append(stop.key)
            result = stop.review_result
            if result and stop.review_status in {"cached", "complete", "findings"}:
                verdict = "clean" if result.is_clean else "findings"
                drafts.append(f"{stop.key} ({verdict})")
            reviews = stop.live.get("reviews") or []
            if isinstance(reviews, dict):
                reviews = reviews.get("nodes") or []
            if not isinstance(reviews, list):
                continue
            submitted_state = ""
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                login = (review.get("author") or {}).get("login")
                state = str(review.get("state") or "").upper()
                if login != self.self_login:
                    continue
                if state in {"APPROVED", "CHANGES_REQUESTED"}:
                    submitted_state = state
                elif state == "COMMENTED" and not submitted_state:
                    submitted_state = state
            if submitted_state:
                submitted.append(f"{stop.key} ({submitted_state})")
        rows = []
        if active:
            rows.append(f"Remote analysis: {self._activity_work(active)}")
        if drafts:
            rows.append(f"Local draft: {self._activity_work(drafts)}")
        if submitted:
            rows.append(f"GitHub review: {self._activity_work(submitted)}")
        return rows or ["Review status: no active analysis, draft, or GitHub review"]

    def refresh_activity(self) -> None:
        """Render current lifecycle state without discovering new work."""
        try:
            panel = self.query_one("#activity", Static)
        except (NoMatches, ScreenStackError):
            return
        active_reviews: list[str] = []
        parent_reviews = 0
        for batch in self.review_batches:
            if not getattr(batch, "running", False):
                continue
            parent_reviews += 1
            for item in getattr(batch, "items", ()):
                key = str(getattr(item, "key", ""))
                if key and key not in active_reviews:
                    active_reviews.append(key)
        active_reviews.extend(
            stop.key
            for stop in self.stops
            if stop.review_status == "running" and stop.key not in active_reviews
        )
        check_workers = getattr(
            self.review_engine, "active_review_slots", lambda: 0
        )()
        active_landings = [
            task for task in self.landing_queue
            if self._landing_task_active(task)
        ]
        queued_landings = [
            task for task in self.landing_queue
            if (
                not self._landing_task_active(task)
                and task.process is None
                and task.returncode is None
            )
        ]
        lines = [
            "AGENT ACTIVITY",
            f"Parent reviews: {parent_reviews}",
            f"Check workers: {check_workers}",
            f"Landing agents: {len(active_landings)}",
            f"Queued work: {len(queued_landings)}",
            f"Snapshot: {self._activity_freshness()}",
        ]
        lines.extend(self._review_activity_rows())
        rows = [f"Review — {self._activity_work([key])}" for key in active_reviews]
        rows.extend(
            f"Landing — {self._activity_work(list(task.keys))}"
            for task in active_landings
        )
        for worker in self.hive_workers:
            task = worker.get("task")
            repository = task.get("repo") if isinstance(task, dict) else None
            number = task.get("number") if isinstance(task, dict) else None
            login = bounded_detail(str(worker.get("login") or "?"))[:48]
            if (
                isinstance(repository, str)
                and re.fullmatch(r"[^/\s]+/[^/\s]+", repository)
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
            ):
                prefix = "Hive last known @" if self.hive_workers_stale else "Hive @"
                rows.append(
                    f"{prefix}{login} — "
                    f"{self._activity_work([f'{repository}#{number}'])}"
                )
            else:
                rows.append(f"Hive @{login} — assignment unavailable")
        if self.hive_unavailable and not self.hive_workers:
            rows.append("Hive assignments unavailable")
        if len(rows) > MAX_ACTIVITY_ROWS:
            rows = rows[:MAX_ACTIVITY_ROWS - 1] + [
                f"… {len(rows) - (MAX_ACTIVITY_ROWS - 1)} more active assignments"
            ]
        panel.update("\n".join(escape(line) for line in [*lines, *rows]))

    def refresh_status(
        self, record_snapshot: dict[str, RunRecord] | None = None
    ) -> None:
        selected = sum(1 for s in self.stops if s.selected)
        failed = sum(1 for s in self.stops if s.failure)
        stuck = f" | {failed} did not merge" if failed else ""
        review_failed = sum(
            1 for stop in self.stops if stop.review_status in REVIEW_FAILURES
        )
        review_failures = (
            f" | {review_failed} review failed"
            if review_failed
            else ""
        )
        running = sum(
            1 for t in self.landing_queue if self._landing_task_active(t)
        )
        queued = sum(
            1
            for t in self.landing_queue
            if not self._landing_task_active(t)
            and t.process is None
            and t.returncode is None
        )
        agents = (
            f" | agents: {running} running, {queued} queued [w]"
            if self.landing_queue
            else ""
        )
        review_cap = getattr(self.review_engine, "effective_review_cap", lambda: 0)()
        review_running = getattr(self.review_engine, "active_review_slots", lambda: 0)()
        active_reviews = sum(
            1 for stop in self.stops if stop.review_status == "running"
        )
        self.observability.state("reviews.active", active_reviews)
        self.observability.state("landings.active", running)
        self.observability.state("check_workers.active", review_running)
        reviews = f" | review slots: {review_running}/{review_cap}"
        breaker_parts = []
        for dep in Dependency:
            b_state = getattr(self.gh_client, "state", lambda d: BreakerState(False))(dep)
            if not b_state.blocked:
                d_state = get_breaker(dep)
                if d_state.blocked:
                    b_state = d_state
            if b_state.blocked:
                retry_detail = f" (retry_at {b_state.retry_at})" if b_state.retry_at is not None else ""
                breaker_parts.append(f" | {dep.value} breaker: blocked{retry_detail}")
        breakers = "".join(breaker_parts)
        shown = len(self.stops)
        active_items = self.active_source_items
        total = len(active_items)
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
        recent_merges = (
            f" | last merged: {escape(', '.join(self.recent_merges))}"
            if self.recent_merges
            else ""
        )
        breakdown = ", ".join(
            f"{count} {action}"
            for action, count in sorted(
                Counter(
                    (item.get("recommended_action") or item.get("action", ""))
                    for item in active_items
                ).items(),
                key=lambda pair: action_rank(pair[0]),
            )
        )
        states = Counter(
            self._queue_state(stop, record_snapshot=record_snapshot)
            for stop in self.stops
        )
        queue_status = (
            f"queue: {states['ready']} ready, {states['queued']} queued, "
            f"{states['in progress']} in progress, "
            f"{states['done']} done, {states['failed']} failed"
        )
        hive = escape(self.hive_state or "asking…")
        if self.hive_unavailable:
            retained = (
                " (last-known assignments retained)"
                if self.hive_workers_stale
                else ""
            )
            hive = f"unavailable — {hive}{retained}"
        # The lab is a word first (#379). The lightning bolt is decoration on
        # top of ACTIVE and never the only carrier: a maintainer reading the
        # word alone learns the same fact.
        lab = f"LAB {self.lab_state}"
        if self.lab_state == lab_client.LAB_ACTIVE:
            lab = "LAB ⚡ ACTIVE"
        policy = (
            f" | review: {self.final_policy}" if self.final_policy else ""
        )
        headroom = escape(self.headroom_status_line)
        reduction = self.headroom_output_reduction.get(
            "output_reduction_percent"
        )
        method = self.headroom_output_reduction.get(
            "output_reduction_method"
        )
        headroom_reduction = (
            f" | output reduction {reduction:g}% {escape(str(method))}"
            if isinstance(reduction, (int, float))
            and isinstance(method, str)
            else ""
        )
        countme = (
            " | Countme unavailable"
            if self.observability.status == "unavailable"
            else ""
        )
        reconciliation = f" | queue {self.reconciliation_state}"
        self.refresh_activity()
        try:
            status_bar = self.query_one("#status-bar", Static)
        except (NoMatches, ScreenStackError):
            return
        view_tag = (
            f"{escape('[I]')} Mixed view | "
            if self.view_mode == "mixed"
            else (
                f"{escape('[I]')} PR view | "
                if self.view_mode == "prs"
                else f"{escape('[I]')} Issues view | "
            )
        )
        if self.view_mode == "issues":
            status_bar.update(
                f" {view_tag}Issues: {shown} open "
                f"| {('source ' + self.source_state + (' — ' + escape(self.source_message) if self.source_message else ''))} "
                f"| {('org ' + GITHUB_ORG) if not self.filters.live else 'repository ' + self.filters.live_repository} | as {self.self_login or 'unknown'} "
                f"| batch: {selected}{reconciliation}"
                f"{reviews}{breakers}{countme} | {headroom}{headroom_reduction} | {lab} | Hive: {hive}"
            )
        elif self.view_mode == "prs":
            status_bar.update(
                f" {view_tag}Queue: {shown} PRs{held_back} | {queue_status} "
                f"| filter {scope} | {breakdown} "
                f"| {('source ' + self.source_state + (' — ' + escape(self.source_message) if self.source_message else ''))} "
                f"| {('org ' + GITHUB_ORG) if not self.filters.live else 'repository ' + self.filters.live_repository} | as {self.self_login or 'unknown'} "
                f"| batch: {selected}{reconciliation}{stuck}{review_failures}{agents}{landed}{recent_merges}{policy}"
                f"{reviews}{breakers}{countme} | {headroom}{headroom_reduction} | {lab} | Hive: {hive}"
            )
        else:
            pr_shown = sum(1 for s in self.stops if not s.is_issue)
            issue_shown = sum(1 for s in self.stops if s.is_issue)
            status_bar.update(
                f" {view_tag}Board: {shown} items ({pr_shown} PRs, {issue_shown} issues){held_back} | {queue_status} "
                f"| filter {scope} | {breakdown} "
                f"| {('source ' + self.source_state + (' — ' + escape(self.source_message) if self.source_message else ''))} "
                f"| {('org ' + GITHUB_ORG) if not self.filters.live else 'repository ' + self.filters.live_repository} | as {self.self_login or 'unknown'} "
                f"| batch: {selected}{reconciliation}{stuck}{review_failures}{agents}{landed}{recent_merges}{policy}"
                f"{reviews}{breakers}{countme} | {headroom}{headroom_reduction} | {lab} | Hive: {hive}"
            )

    def action_filter(self) -> None:
        """Cycle the action filter: every action, then one at a time."""
        active_items = self.active_source_items
        present = [a for a in MAINTAINER_ORDER if any(
            (item.get("recommended_action") or item.get("action")) == a for item in active_items
        )]
        scopes = [""] + present
        try:
            nxt = scopes[(scopes.index(self.filters.action) + 1) % len(scopes)]
        except ValueError:
            nxt = ""
        self.filters.action = nxt
        self.apply_filters()
        noun = "items" if self.view_mode == "mixed" else ("issues" if self.view_mode == "issues" else "PRs")
        self.notify(f"filter: {nxt or 'all actions'} — {len(self.stops)} {noun}")

    @property
    def current(self) -> Stop | None:
        try:
            index = self.query_one("#queue", ListView).index
        except (NoMatches, ScreenStackError):
            return None
        if index is None or not (0 <= index < len(self.stops)):
            return None
        return self.stops[index]

    def on_list_view_highlighted(self, _event) -> None:
        stop = self.current
        if stop:
            self.show_evidence(stop)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_activate()

    def show_evidence(
        self, stop: Stop, open_decision: bool = False
    ) -> None:
        generation = self.evidence_generation.get(stop.key, 0) + 1
        self.evidence_generation[stop.key] = generation
        try:
            self.fetch_evidence(stop, generation, open_decision)
        except RuntimeError:
            pass

    @work(thread=True)
    def fetch_evidence(
        self,
        stop: Stop,
        generation: int,
        open_decision: bool,
    ) -> None:
        if stop.is_issue:
            try:
                result = gh(
                    "issue",
                    "view",
                    str(stop.number),
                    "--repo",
                    stop.repository,
                    "--json",
                    "title,body,author,labels,comments,createdAt,updatedAt,state",
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        bounded_detail(
                            (result.stderr or result.stdout).strip()
                            or f"GitHub could not read {stop.repository}#{stop.number}"
                        )
                    )
                live = json.loads(result.stdout)
                if not isinstance(live, dict):
                    raise ValueError(
                        f"GitHub returned malformed evidence for {stop.repository}#{stop.number}"
                    )
            except (
                OSError,
                RuntimeError,
                subprocess.TimeoutExpired,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                self.call_from_thread(
                    self.evidence_failed,
                    stop,
                    bounded_detail(str(error) or type(error).__name__),
                    generation,
                    open_decision,
                )
                return
            self.call_from_thread(
                self.evidence_loaded,
                stop,
                live,
                None,
                generation,
                open_decision,
            )
            return
        try:
            live = fetch_live_review(stop.repository, stop.number)
        except (
            OSError,
            RuntimeError,
            subprocess.TimeoutExpired,
            ValueError,
        ) as error:
            self.call_from_thread(
                self.evidence_failed,
                stop,
                bounded_detail(str(error) or type(error).__name__),
                generation,
                open_decision,
            )
            return
        merge_rights: bool | None = None
        if stop.repository not in self.merge_rights:
            # 'push' is exactly the power to merge on GitHub: a contributor
            # agent works from a fork and has none, which is why the direct
            # merge key can never be its path.
            rights = gh(
                "api", f"repos/{stop.repository}", "--jq", ".permissions.push"
            )
            merge_rights = (
                rights.returncode == 0 and rights.stdout.strip() == "true"
            )
        self.call_from_thread(
            self.evidence_loaded,
            stop,
            live,
            merge_rights,
            generation,
            open_decision,
        )

    def evidence_failed(
        self,
        stop: Stop,
        message: str,
        generation: int,
        open_decision: bool,
    ) -> None:
        if (
            self.current is not stop
            or self.evidence_generation.get(stop.key) != generation
        ):
            return
        if open_decision:
            self.notify(
                f"{stop.key}: current evidence unavailable: {message}",
                severity="error",
            )
            return
        stop.live = {}
        stop.head_sha = ""
        stop.review_result = None
        stop.review_status = ""
        stop.review_failure = ""
        stop.cached_age = ""
        self.render_evidence(stop)

    def evidence_loaded(
        self,
        stop: Stop,
        live: dict,
        merge_rights: bool | None,
        generation: int,
        open_decision: bool,
    ) -> None:
        if (
            not any(candidate is stop for candidate in self.stops)
            or self.evidence_generation.get(stop.key) != generation
        ):
            return
        if stop.is_issue:
            stop.live = dict(live)
            stop.live["repository"] = stop.repository
            stop.live["number"] = stop.number
            stop.triage_state = self.triage.get(
                self.triage_key(stop), "unseen"
            )
            self.render_evidence(stop)
            return
        previous_base = str(stop.live.get("baseRefOid") or "")
        previous_head = stop.head_identity
        stop.live = dict(live)
        if stop.live:
            stop.live.update(
                live_review_context(stop.live, title=stop.title)
            )
            stop.live["repository"] = stop.repository
            stop.live["number"] = stop.number
        stop.head_sha = str(stop.live.get("headRefOid") or "")
        if merge_rights is not None:
            self.merge_rights[stop.repository] = merge_rights
            _bound_map(self.merge_rights, MAX_MERGE_RIGHTS_ENTRIES)
        current_base = str(stop.live.get("baseRefOid") or "")
        provenance = getattr(stop.review_result, "provenance", None) or {}
        result_base = str(provenance.get("base_sha") or "")
        result_head = str(provenance.get("head_sha") or "")
        identity_changed = (
            (previous_base and previous_base != current_base)
            or (previous_head and previous_head != stop.head_identity)
            or (result_base and result_base != current_base)
            or (result_head and result_head != stop.head_identity)
        )
        if identity_changed:
            stop.review_result = None
            stop.review_status = ""
            stop.cached_age = ""
        stop.triage_state = self.triage.get(
            self.triage_key(stop), "unseen"
        )
        self.annotate_cached_review(stop)
        self.render_evidence(stop)
        if (
            open_decision
            and self.current is stop
            and stop.review_result is not None
        ):
            self.push_screen(ReviewDecisionScreen(stop))

    def annotate_cached_review(self, stop: Stop) -> None:
        if stop.review_status in REVIEW_FAILURES:
            return
        base_sha = str(stop.live.get("baseRefOid") or "")
        head_sha = stop.head_identity
        if not (
            re.fullmatch(r"[0-9a-f]{40}", base_sha)
            and re.fullmatch(r"[0-9a-f]{40}", head_sha)
        ):
            return
        model, effort = self.review_profile(stop.repository)
        run = ReviewRun(
            stop.repository,
            stop.number,
            base_sha,
            head_sha,
            base_sha[:12] + head_sha[:12],
            ACTIVE_BACKEND,
            model,
            effort,
        )
        if stop.review_result is not None and stop.review_status != "cached":
            result = stop.review_result
            stop.review_result = ReviewResult(
                result.version,
                result.state,
                dict(result.counts),
                [dict(item) for item in result.findings],
                [dict(item) for item in result.verification],
                dict(result.provenance),
                dict(stop.overlap),
                dict(stop.live),
                list(result.raw_evidence),
            )
            return
        receipt = self.review_cache.get(
            run, self.review_scope_version
        )
        if receipt is None:
            return
        stop.review_result = receipt.analysis_result(
            live=stop.live, overlap=stop.overlap
        )
        stop.review_status = "cached"
        stop.cached_age = self.receipt_age(receipt)

    @staticmethod
    def receipt_age(receipt: ReviewReceipt) -> str:
        try:
            created = datetime.fromisoformat(receipt.created_at)
        except ValueError:
            return "?"
        age = max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - created.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )
        if age < 60:
            return "<1m"
        if age < 3600:
            return f"{age // 60}m"
        if age < 86400:
            return f"{age // 3600}h"
        return f"{age // 86400}d"

    def review_profile(self, repository: str) -> tuple[str, str]:
        if ACTIVE_BACKEND == "goose":
            return (
                os.environ.get("GOOSE_MODEL", "gemini-3.8-flash"),
                os.environ.get("GOOSE_THINKING_EFFORT", "max"),
            )
        if getattr(self, "_repaint_profile_cache", None) is not None and repository in self._repaint_profile_cache:
            return self._repaint_profile_cache[repository]

        options = self.harness_options
        if not options:
            result = ("gemini-3.8-flash", "max")
            if getattr(self, "_repaint_profile_cache", None) is not None:
                self._repaint_profile_cache[repository] = result
            return result

        preferences = (
            getattr(self, "_repaint_preferences", None)
            if getattr(self, "_repaint_preferences", None) is not None
            else load_preferences()
        )
        selected = choose_option(
            repository, preferences, options
        )
        if selected is None:
            result = ("gemini-3.8-flash", "max")
            if getattr(self, "_repaint_profile_cache", None) is not None:
                self._repaint_profile_cache[repository] = result
            return result
        preference = next(
            (
                candidate
                for candidate in (
                    preferences.get(repository),
                    preferences.get("*"),
                )
                if candidate
                and candidate.harness_id
                == selected.harness.branding.harness_id
            ),
            None,
        )
        supported_efforts = tuple(getattr(selected.harness, "SUPPORTED_EFFORTS", ())) or ("low", "medium", "high", "max")
        default_effort = "low" if "low" in supported_efforts else (getattr(selected.harness, "effort", None) or supported_efforts[0])
        if preference and preference.effort in supported_efforts:
            effort = preference.effort
        else:
            raw_reasoning = getattr(selected.discovery, "reasoning", None) or getattr(selected.discovery, "reasoning_effort", None)
            effort = raw_reasoning if (raw_reasoning and raw_reasoning in supported_efforts) else default_effort
        result = (
            preference.model if preference else selected.discovery.model,
            effort,
        )
        if getattr(self, "_repaint_profile_cache", None) is not None:
            self._repaint_profile_cache[repository] = result
        return result

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
        if stop.is_issue:
            self.refresh_rows()
            live = stop.live or {}
            title = live.get("title") or stop.title
            author_source = live.get("author")
            author = (
                author_source.get("login", stop.author or "-")
                if isinstance(author_source, dict)
                else (stop.author or "-")
            )
            state = str(live.get("state", "OPEN") or "OPEN")
            created_at = str(live.get("createdAt", "") or live.get("created_at", "") or "")
            updated_at = str(live.get("updatedAt", "") or live.get("updated_at", "") or "")
            raw_labels = live.get("labels") or []
            label_names = []
            for l in raw_labels:
                if isinstance(l, dict) and "name" in l:
                    label_names.append(l["name"])
                elif isinstance(l, str):
                    label_names.append(l)
            labels_str = ", ".join(escape(n) for n in label_names) or "-"

            raw_comments = live.get("comments")
            if isinstance(raw_comments, list):
                comments_count = len(raw_comments)
            elif isinstance(raw_comments, dict):
                comments_count = raw_comments.get("totalCount", 0)
            elif isinstance(stop.live.get("comments_count"), int):
                comments_count = stop.live.get("comments_count", 0)
            else:
                comments_count = 0

            body = str(live.get("body") or "").strip()
            body_display = escape(body) if body else "[dim]No description provided.[/dim]"
            issue_link = link(stop.key, issue_url(stop.repository, stop.number))

            try:
                details = self.query_one("#details", Static)
            except NoMatches:
                return
            details.update(
                f"[b]{issue_link}[/b]  {escape(title)}\n"
                f"author     {link(author, f'https://github.com/{author}')}\n"
                f"state      {escape(state)}\n"
                f"created    {escape(created_at[:19] if created_at else '-')}\n"
                f"updated    {escape(updated_at[:19] if updated_at else '-')}\n"
                f"labels     {labels_str}\n"
                f"comments   {comments_count}\n\n"
                f"[b]DESCRIPTION[/b]\n"
                f"{body_display}"
            )
            self.render_issue_context(stop)
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
        ci_triage_block = ""
        if bad > 0:
            lines_ci = ["\n[b red]CI FAILURE TRIAGE[/b red]"]
            for check in checks:
                conc = check.get("conclusion") or check.get("state") or "PENDING"
                if conc in ("FAILURE", "ERROR", "TIMED_OUT"):
                    wf = check.get("workflowName") or ""
                    job_name = check.get("name") or check.get("context") or "check"
                    step_name = check.get("stepName") or ""
                    step_info = f" › {escape(step_name)}" if step_name else ""
                    url = check.get("detailsUrl") or check.get("url") or ""
                    started = str(check.get("startedAt") or "")[:19]
                    completed = str(check.get("completedAt") or "")[:19]
                    time_info = f" ({started} -> {completed})" if started and completed else ""
                    head_sha = str(live.get("headRefOid") or "")[:12]
                    sha_info = f" @ {head_sha}" if head_sha else ""
                    lines_ci.append(
                        f"  [red]✗ {conc}[/red] {escape(wf + ' / ' if wf else '')}[b]{escape(job_name)}[/b]{step_info}{sha_info}{time_info}"
                    )
                    if url:
                        lines_ci.append(f"    evidence: {link(url, url)}")
            ci_triage_block = "\n" + "\n".join(lines_ci)

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
            f"checks   {ok} ok, {bad} failed, {cancelled} cancelled, {pending} pending"
            f"{ci_triage_block}\n"
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
            + (
                "\n\n[b]LAST REVIEW FAILURE[/b]\n"
                f"error    {escape(stop.review_failure)}"
                if stop.review_failure
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
        last_worker = self.last_known_hive_worker_for(stop)
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
        elif last_worker and self.hive_unavailable:
            login = last_worker["login"]
            task_id = str(last_worker["task"].get("task_id", "?"))
            lines.append(
                f"hive     last known Hive assignment: "
                f"{link(login, 'https://github.com/' + login)} "
                f"({escape(task_id)}); current assignment is unknown"
            )
        elif self.hive_unavailable:
            lines.append(
                f"hive     {escape(self.hive_state)} — current assignment is unknown "
                f"{escape('([H] asks again)')}"
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

    def render_issue_context(self, stop: Stop) -> None:
        live = stop.live or {}
        raw_comments = live.get("comments")
        lines = ["[b]RECENT COMMENTS[/b]"]
        if isinstance(raw_comments, list) and raw_comments:
            for comment in raw_comments[-5:]:
                if isinstance(comment, dict):
                    author_obj = comment.get("author")
                    c_author = author_obj.get("login", "?") if isinstance(author_obj, dict) else "?"
                    c_created = escape(str(comment.get("createdAt", "") or "")[:10])
                    c_body = str(comment.get("body", "") or "").strip()
                    first_line = c_body.splitlines()[0] if c_body else ""
                    lines.append(f"• [b]{escape(c_author)}[/b] ({c_created}): {escape(first_line[:80])}")
        else:
            lines.append("[dim]no comments on this issue yet[/dim]")
        lines.append("")
        lines.append("[b]Triage keys[/b]: [b]c[/b] comment · [b]x[/b] close · [b]o[/b] browser · [b]y[/b] copy link")
        context = self.query("#context")
        if context:
            context.first().update("\n".join(lines))

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
        if self.batch_mutation_in_flight:
            self.notify("a batch mutation is already in flight", severity="warning")
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
                result = _run_mutation(
                    command, timeout=MUTATION_TIMEOUT, idempotent=False
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
        if stop in self.stops:
            self.show_evidence(stop)
        elif self.current:
            self.show_evidence(self.current)
        if not stop.is_issue:
            self._request_reconciliation()

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
        stop = self.current
        if stop and stop.is_issue:
            self.show_evidence(stop)
        elif stop and stop.review_result is not None:
            self.show_evidence(stop, open_decision=True)
        elif stop:
            self.action_view_diff()

    def action_back(self) -> None:
        if isinstance(self.focused, (Input, TextArea)):
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_batch(self) -> None:
        stop = self.current
        if not stop:
            return
        stop.selected = not stop.selected
        self.refresh_selection_rows()

    def action_select_all(self) -> None:
        should_select = (
            not self.stops
            or not all(stop.selected for stop in self.stops)
        )
        for stop in self.stops:
            stop.selected = should_select
        self.refresh_selection_rows()

    def action_toggle_advance(self) -> None:
        queue = self._queue()
        index = queue.index
        stop = self.current
        if stop is None:
            return
        successor = (
            self.stops[index + 1]
            if index is not None and index + 1 < len(self.stops)
            else None
        )
        stop.selected = not stop.selected
        self.refresh_selection_rows()
        if successor is not None:
            queue.index = self.stops.index(successor)

    def action_toggle_view(self) -> None:
        if self.view_mode == "prs":
            self.view_mode = "issues"
            self.notify("switched to issues view")
        elif self.view_mode == "issues":
            self.view_mode = "mixed"
            self.notify("switched to mixed workboard view")
        else:
            self.view_mode = "prs"
            self.notify("switched to pull requests view")
        self._sync_source_state()
        self.apply_filters()
        if self.current:
            self.show_evidence(self.current)

    def action_next_unreviewed(self) -> None:
        if not self.stops:
            return
        start = self._queue().index or 0
        for offset in range(1, len(self.stops) + 1):
            index = (start + offset) % len(self.stops)
            stop = self.stops[index]
            if self.stop_lacks_my_review(stop):
                self._queue().index = index
                return
        self.notify(
            "no visible pull requests lack your review.",
            severity="information",
        )

    @staticmethod
    def triage_key(stop: Stop) -> str:
        return stop.triage_key

    def action_review(self) -> None:
        targets = self._pr_action_targets()
        if not targets:
            return
        if any(s.selected for s in self.stops) or len(targets) > 1:
            batch = targets
            keys = {stop.key for stop in batch}
            active = {
                item.key
                for review_batch in self.review_batches
                if review_batch.running
                for item in review_batch.items
            }
            if keys & (active | self.review_pending_keys):
                self.notify(
                    "a selected pull request is already being reviewed.",
                    severity="warning",
                )
                return
            self.review_pending_keys.update(keys)
            self.start_review_batch(batch)
        else:
            self.start_review(targets[0])

    def action_slay_pr(self) -> None:
        """`$`: slay a pull request (review if unreviewed, fix if findings, land in batch)."""
        if self.view_mode == "issues":
            self.notify("action applies to pull requests only", severity="warning")
            return
        if not self.self_login:
            self.notify("your GitHub login is unknown; needed for landing.", severity="warning")
            return

        selected = [s for s in self.stops if s.selected]
        if selected:
            prs = [s for s in selected if not s.is_issue]
            if len(prs) < len(selected):
                self.notify("selected issues skipped; action applies to pull requests only", severity="warning")
            if not prs:
                return
            targets = prs
        elif self.current:
            if self.current.is_issue:
                self.notify("action applies to pull requests only", severity="warning")
                return
            targets = [self.current]
        else:
            self.notify("nothing selected to slay", severity="warning")
            return

        actionable = []
        for stop in targets:
            reason = self.stop_blocked_reason(stop)
            if reason:
                self.notify(f"[$] {stop.key} cannot be slayed: {reason}", severity="warning")
                continue
            actionable.append(stop)

        if not actionable:
            return

        def confirmed(ok: bool) -> None:
            if not ok:
                return
            self._execute_slay(actionable)

        self.push_screen(SlayConfirmScreen(actionable), confirmed)

    def _execute_slay(self, targets: list[Stop]) -> None:
        review_targets = []
        for stop in targets:
            has_review = (
                stop.review_status in {"cached", "complete", "findings"}
                or stop.review_result is not None
            )
            prov = getattr(stop.review_result, "provenance", None) if stop.review_result else None
            reviewed_head = (
                (prov.get("head_sha") if isinstance(prov, dict) else None)
                or stop.head_identity
            )
            rev_model = prov.get("model") if isinstance(prov, dict) else None
            rev_effort = (prov.get("effort") or prov.get("reasoning_effort")) if isinstance(prov, dict) else None
            reviewed_identity = None
            if has_review and reviewed_head and FULL_SHA.fullmatch(reviewed_head):
                try:
                    reviewed_identity = self.run_identity(
                        stop, head_sha=reviewed_head, model=rev_model, effort=rev_effort
                    )
                except ValueError as error:
                    self.notify(f"[$] {stop.key}: {error}", severity="error")
                    continue

            try:
                live_data = self.fetch_live_pr(stop.repository, stop.number, force=True)
                stop.live.update(live_data)
            except Exception:
                live_data = {}

            reason = self.stop_blocked_reason(stop)
            if reason:
                self.notify(f"[$] {stop.key} cannot be slayed: {reason}", severity="warning")
                continue

            live_head_sha = str(stop.live.get("headRefOid") or live_data.get("headRefOid") or "")
            if not FULL_SHA.fullmatch(live_head_sha):
                live_head_sha = reviewed_head if (has_review and reviewed_head) else stop.head_identity

            if has_review and reviewed_identity is not None:
                record = self.run_store.get(reviewed_identity)
                if live_head_sha != reviewed_identity.head_sha:
                    if record is not None and record.is_terminal:
                        if record.state == RunState.HEAD_CHANGED:
                            stop.failure = f"landing aborted: head changed (reviewed {reviewed_identity.head_sha[:12]}, live {live_head_sha[:12]})"
                            stop.failure_command = "gh pr view"
                            self.notify(
                                f"[$] {stop.key}: head changed between review and mutation; landing aborted",
                                severity="error",
                            )
                        else:
                            self.notify(
                                f"[$] {stop.key}: cannot land — run state {record.state.value} cannot mutate",
                                severity="error",
                            )
                        self.refresh_rows()
                        continue
                    stop.review_result = None
                    stop.review_status = ""
                    fixer_advanced = self.is_fixer_head(stop.repository, stop.number, live_head_sha)
                    if record is None:
                        record = self.run_store.create(reviewed_identity)
                        self.run_store.transition(reviewed_identity, RunState.REVIEWING)
                        self.run_store.transition(reviewed_identity, RunState.REVIEW_CLEAN)
                    record = self.run_store.revalidate_head(
                        reviewed_identity, live_head_sha, fixer_advanced=fixer_advanced
                    )
                    if record.state == RunState.HEAD_CHANGED:
                        stop.failure = f"landing aborted: head changed (reviewed {reviewed_identity.head_sha[:12]}, live {live_head_sha[:12]})"
                        stop.failure_command = "gh pr view"
                        self.notify(
                            f"[$] {stop.key}: head changed between review and mutation; landing aborted",
                            severity="error",
                        )
                        self.refresh_rows()
                        continue
                    elif record.state == RunState.ESCALATION_REQUIRED:
                        esc_backend, esc_model, esc_effort = self.escalation_profile(stop)
                        new_id = self.run_identity(
                            stop, head_sha=live_head_sha, model=esc_model, effort=esc_effort
                        )
                        new_rec = self.run_store.get(new_id)
                        if new_rec is None:
                            new_rec = self.run_store.create(new_id)
                        if new_rec.state != RunState.RE_REVIEWING:
                            self.run_store.transition(new_id, RunState.RE_REVIEWING)
                        stop.head_sha = live_head_sha
                        stop.live["headRefOid"] = live_head_sha
                        stop.review_status = "running"
                        stop.review_result = None
                        self.active_review_identities[stop.key] = new_id
                        self.notify(
                            f"[$] {stop.key}: head advanced by fixer ({live_head_sha[:8]}) — triggering fresh strong review ({esc_model})…",
                        )
                        review_targets.append((stop, esc_model, esc_effort))
                        continue

                identity = reviewed_identity
                if record is not None:
                    if record.in_flight:
                        self.notify(f"[$] {stop.key} is already being slayed", severity="warning")
                        continue
                    if record.is_terminal:
                        if record.state in REVIEW_PROVIDER_TERMINALS:
                            self.run_store.retry_review(identity)
                            self.notify(f"[$] retrying review for {stop.key}…")
                            review_targets.append((stop, identity.model, identity.effort))
                            continue
                        elif record.state == RunState.HEAD_CHANGED:
                            stop.failure = f"landing aborted: head changed (reviewed {identity.head_sha[:12]}, live {live_head_sha[:12]})"
                            stop.failure_command = "gh pr view"
                            self.notify(
                                f"[$] {stop.key}: head changed between review and mutation; landing aborted",
                                severity="error",
                            )
                            self.refresh_rows()
                            continue
                        else:
                            self.notify(
                                f"[$] {stop.key} at {identity.head_sha[:8]} already reached terminal state {record.state.value}; head must advance to re-slay",
                                severity="warning",
                            )
                            continue
                else:
                    record = self.run_store.create(identity)

                findings = list(stop.review_result.findings if stop.review_result else [])
                target_state = (
                    RunState.REVIEW_FINDINGS
                    if (findings or stop.review_status == "findings")
                    else RunState.REVIEW_CLEAN
                )
                if record.state == RunState.PENDING:
                    self.run_store.transition(identity, RunState.REVIEWING)
                    record = self.run_store.transition(identity, target_state)
                self._dispatch_slay_landing(stop, identity=identity)
                continue

            try:
                identity = self.run_identity(stop, head_sha=live_head_sha)
            except ValueError as error:
                self.notify(f"[$] {stop.key}: {error}", severity="error")
                continue

            record = self.run_store.get(identity)
            if record is not None:
                if record.in_flight:
                    self.notify(f"[$] {stop.key} is already being slayed", severity="warning")
                    continue
                if record.is_terminal:
                    if record.state in REVIEW_PROVIDER_TERMINALS:
                        self.run_store.retry_review(identity)
                        self.notify(f"[$] retrying review for {stop.key}…")
                        review_targets.append((stop, identity.model, identity.effort))
                        continue
                    else:
                        self.notify(
                            f"[$] {stop.key} at {identity.head_sha[:8]} already reached terminal state {record.state.value}; head must advance to re-slay",
                            severity="warning",
                        )
                        continue
            else:
                record = self.run_store.create(identity)

            if record.state == RunState.PENDING:
                self.run_store.transition(identity, RunState.REVIEWING)
            self.notify(f"[$] slaying {stop.key}: running review…")
            review_targets.append((stop, None, None))
        if not review_targets:
            return
        active = {
            item.key
            for review_batch in self.review_batches
            if review_batch.running
            for item in review_batch.items
        }
        ready: list[tuple[Stop, str | None, str | None]] = []
        for stop, model, effort in review_targets:
            if stop.key in (active | self.review_pending_keys):
                # The claim in run_store stays: the review already
                # running will complete, and its event handler lands it.
                self.notify(
                    f"[$] {stop.key} is already being reviewed.",
                    severity="warning",
                )
            else:
                ready.append((stop, model, effort))
        if not ready:
            return

        batches_by_profile: dict[tuple[str | None, str | None], list[Stop]] = {}
        for stop, model, effort in ready:
            batches_by_profile.setdefault((model, effort), []).append(stop)

        ready_keys = {stop.key for stop, _, _ in ready}
        self.review_pending_keys.update(ready_keys)

        for (model, effort), stops in batches_by_profile.items():
            if model is not None and effort is not None:
                self.start_review_batch(stops, model=model, effort=effort)
            elif model is not None:
                self.start_review_batch(stops, model=model)
            else:
                self.start_review_batch(stops)

    def _dispatch_slay_landing(
        self, stop: Stop, identity: RunIdentity | None = None
    ) -> None:
        if not self.self_login:
            self.notify("your GitHub login is unknown; needed for landing.", severity="warning")
            return
        if identity is None:
            identity = self.run_identity(stop)

        try:
            live_data = self.fetch_live_pr(stop.repository, stop.number, force=True)
            stop.live.update(live_data)
        except Exception as error:
            self.notify(
                f"[$] {stop.key}: live re-fetch failed: {error}",
                severity="error",
            )
            return

        reason = self.stop_blocked_reason(stop)
        if reason:
            self.notify(
                f"[$] {stop.key} cannot be slayed: {reason}",
                severity="warning",
            )
            return

        record = self.run_store.get(identity)
        if record is None:
            record = self.run_store.create(identity)
        if not record.may_mutate():
            self.notify(
                f"[$] {stop.key}: cannot land — run state {record.state.value} cannot mutate",
                severity="error",
            )
            return

        live_head_sha = str(live_data.get("headRefOid") or "")
        if not FULL_SHA.fullmatch(live_head_sha):
            live_head_sha = identity.head_sha

        # Revalidate head (#410, #411)
        head_moved = (live_head_sha != identity.head_sha)
        fixer_advanced = head_moved and self.is_fixer_head(stop.repository, stop.number, live_head_sha)

        record = self.run_store.revalidate_head(identity, live_head_sha, fixer_advanced=fixer_advanced)
        if record.state == RunState.HEAD_CHANGED:
            stop.failure = f"landing aborted: head changed (reviewed {identity.head_sha[:12]}, live {live_head_sha[:12]})"
            stop.failure_command = "gh pr view"
            self.notify(
                f"[$] {stop.key}: head changed between review and mutation; landing aborted",
                severity="error",
            )
            self.refresh_rows()
            return

        if record.state == RunState.ESCALATION_REQUIRED:
            # Head was advanced by our fixer (#411). Trigger fresh high-assurance review of the new head.
            esc_backend, esc_model, esc_effort = self.escalation_profile(stop)
            new_id = self.run_identity(
                stop, head_sha=live_head_sha, model=esc_model, effort=esc_effort
            )
            new_rec = self.run_store.get(new_id)
            if new_rec is None:
                new_rec = self.run_store.create(new_id)
            if new_rec.state != RunState.RE_REVIEWING:
                self.run_store.transition(new_id, RunState.RE_REVIEWING)
            stop.head_sha = live_head_sha
            stop.live["headRefOid"] = live_head_sha
            stop.review_status = "running"
            stop.review_result = None
            self.active_review_identities[stop.key] = new_id
            if stop.key not in self.review_pending_keys:
                self.review_pending_keys.add(stop.key)
                self.start_review_batch([stop], model=esc_model, effort=esc_effort)
            self.notify(
                f"[$] {stop.key}: head advanced by fixer ({live_head_sha[:8]}) — triggering fresh strong review ({esc_model})…",
            )
            self.refresh_rows()
            return

        findings = list(stop.review_result.findings if stop.review_result else [])
        if findings or stop.review_status == "findings":
            task = landing.new_fix_task(stop, findings, self.self_login)
            task.policy = self.final_policy or "automatic"
            self.enqueue_landing(task)
            self.notify(f"[$] {stop.key}: findings detected — dispatched auto-fix & land [w]")
            stop.selected = False
            self.refresh_rows()
            return

        # Escalation check on clean review (#411)
        low_risk = is_low_risk(stop)
        high_assurance = is_high_assurance(identity.model)
        if not high_assurance and not low_risk:
            # Cheap first-pass clean verdict cannot authorise merge on its own (#411).
            # Escalation is skippable only for the explicit low-risk class.
            self.run_store.transition(
                identity,
                RunState.ESCALATION_REQUIRED,
                reason=f"cheap model {identity.model} clean verdict cannot authorise merge",
            )
            esc_backend, esc_model, esc_effort = self.escalation_profile(stop)
            new_id = self.run_identity(
                stop, head_sha=live_head_sha, model=esc_model, effort=esc_effort
            )
            new_rec = self.run_store.get(new_id)
            if new_rec is None:
                new_rec = self.run_store.create(new_id)
            if new_rec.state != RunState.RE_REVIEWING:
                self.run_store.transition(new_id, RunState.RE_REVIEWING)
            stop.review_status = "running"
            stop.review_result = None
            self.active_review_identities[stop.key] = new_id
            if stop.key not in self.review_pending_keys:
                self.review_pending_keys.add(stop.key)
                self.start_review_batch([stop], model=esc_model, effort=esc_effort)
            self.notify(
                f"[$] {stop.key}: clean first pass ({identity.model}) cannot authorise merge — triggering strong review ({esc_model})…",
            )
            self.refresh_rows()
            return

        # Transition to MUTATING before gate checks
        self.run_store.transition(identity, RunState.MUTATING, low_risk=low_risk)

        # Human review invariant at the landing gate (#414)
        if not self.has_human_review(live_data):
            self.run_store.transition(
                identity,
                RunState.HUMAN_REVIEW_MISSING,
                reason="no human review on GitHub",
            )
            stop.failure = "landing refused: no human review on GitHub"
            stop.failure_command = "landing gate"
            self.notify(
                f"[$] {stop.key}: landing refused: no human review on GitHub",
                severity="error",
            )
            self.refresh_rows()
            return

        # Permissions check
        try:
            perm_res = self.gh_client.read(
                "api", f"repos/{stop.repository}", "--jq", ".permissions.push"
            )
            if perm_res.returncode == 0 and perm_res.stdout.strip():
                has_push = perm_res.stdout.strip().lower() == "true"
                self.merge_rights[stop.repository] = has_push
                _bound_map(self.merge_rights, MAX_MERGE_RIGHTS_ENTRIES)
                if not has_push:
                    self.run_store.transition(
                        identity,
                        RunState.MUTATION_FAILED,
                        reason="push permission denied",
                    )
                    stop.failure = "landing refused: push permission denied"
                    stop.failure_command = "permissions.push"
                    self.notify(
                        f"[$] {stop.key}: push permission denied",
                        severity="error",
                    )
                    self.refresh_rows()
                    return
        except Exception:
            pass

        task = landing.new_task([stop], self.self_login)
        task.policy = self.final_policy or "automatic"
        self.enqueue_landing(task)
        self.notify(f"[$] {stop.key}: review clean — dispatched batch landing [w]")
        stop.selected = False
        self.refresh_rows()

    def fetch_live_pr(self, repository: str, number: int, force: bool = False) -> dict[str, Any]:
        stop = next((s for s in self.stops if s.repository == repository and s.number == number), None)
        if not force and stop and stop.live.get("baseRefOid") and stop.live.get("headRefOid"):
            return stop.live
        live = self.gh_client.read(
            "pr", "view", str(number), "--repo", repository,
            "--json",
            "author,state,baseRefOid,headRefOid,isDraft,mergeable,mergeStateStatus,"
            "reviewDecision,additions,deletions,changedFiles,updatedAt,body,"
            "closingIssuesReferences,statusCheckRollup,labels,reviews,title",
        )
        if live.returncode == 0:
            data = json.loads(live.stdout) if (live.stdout and live.stdout.strip()) else {}
            if stop:
                if data:
                    merged = dict(stop.live)
                    merged.update(data)
                    stop.live = merged
                    if data.get("headRefOid"):
                        stop.head_sha = str(data["headRefOid"])
                    stop.triage_state = self.triage.get(self.triage_key(stop), "unseen")
                    return merged
                return stop.live
            return data
        if not force and stop and stop.live:
            return stop.live
        raise RuntimeError(f"failed to fetch live PR {repository}#{number}: {live.stderr}")

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
        if self.view_mode in ("mixed", "issues"):
            self.load_issues()
        if self.view_mode in ("mixed", "prs"):
            request = (
                self._reconciliation_request
                if self._reconciliation_waiting
                else 0
            )
            if request:
                self._start_reconciliation_source("queue", request)
                self._start_reconciliation_source("hive", request)
            else:
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
        batch = self._pr_action_targets()
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
        if self.view_mode == "issues" or (self.current and self.current.is_issue):
            self.notify("action applies to pull requests only", severity="warning")
            return
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
            if stop.is_issue:
                self.notify("action applies to pull requests only", severity="warning")
                return
            self.leave_review(stop)

    def action_docs(self) -> None:
        self.notify(f"docs-update agent task is tracked as {DOCS_UPDATE_ISSUE}")

    def action_open_browser(self) -> None:
        stop = self.current
        if stop:
            target = "issue" if stop.is_issue else "pr"
            gh(target, "view", str(stop.number), "--repo", stop.repository, "--web")

    def action_view_diff(self) -> None:
        stop = self.current
        if not stop:
            return
        if self.view_mode == "issues" or stop.is_issue:
            self.notify("action applies to pull requests only", severity="warning")
            return
        self.push_screen(DiffScreen(stop))

    def action_view_comments(self) -> None:
        # Unlike the diff, a comment thread exists for issues too, and the
        # issues view is where triage reads them.
        stop = self.current
        if stop:
            self.push_screen(CommentsScreen(stop))

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
            target = "issue" if stop.is_issue else "pr"
            command = [
                "gh", target, "comment", str(stop.number),
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
        may use: hivecommons/hive#4052 gave it a hosted ingress without the
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

    def build_batch_queue_plan(self, batch: list[Stop]) -> action_plan.BatchActionPlan:
        items = []
        for stop in batch:
            try:
                live = self.fetch_live_pr(stop.repository, stop.number, force=True)
            except Exception:
                continue
            if not live or not isinstance(live, dict):
                continue
            head = str(live.get("headRefOid") or "")
            if not head:
                continue
            stop.live = live
            stop.head_sha = head
            if live.get("isDraft"):
                continue
            if not self._queueable(stop):
                continue
            owner, repository = stop.repository.split("/", 1)
            base = hive_api_base() or "https://hive.example"
            endpoint = (
                f"{base}/api/v1/prs/{owner}/{repository}/{stop.number}/queue-automerge"
            )
            items.append(
                action_plan.BatchMutationItem(
                    stop.repository,
                    stop.number,
                    head,
                    action_plan.Prerequisites.from_mappings(
                        permissions={"self_login": self.self_login},
                        checks={"ci": effective_check_state(stop.check_state, live)},
                    ),
                    (("python3", "image/tui/hive_api.py", "queue", endpoint),),
                )
            )
        if not items:
            raise action_plan.InvalidPlanError("no queueable pull requests in batch")
        return action_plan.BatchActionPlan.build(
            actor=self.self_login,
            tenant="projectbluefin",
            action_kind="approve-and-queue",
            items=tuple(items),
        )

    def batch_queue_automerge(self, batch: list[Stop]) -> None:
        if self.batch_mutation_in_flight:
            self.notify("a batch mutation is already in flight", severity="warning")
            return
        if not self.self_login:
            self.notify(
                "your GitHub login is unknown; the queue approval needs it.",
                severity="warning",
            )
            return
        base = hive_api_base()
        if not base:
            self.notify("Hive is unreachable; nothing was queued.", severity="warning")
            return
        self.batch_mutation_in_flight = True
        self.notify("preparing batch action plan…")
        self._hydrate_batch_and_confirm(batch)

    @work(thread=True)
    def _hydrate_batch_and_confirm(self, batch: list[Stop]) -> None:
        try:
            plan = self.build_batch_queue_plan(batch)
        except action_plan.ActionPlanError as error:
            self.call_from_thread(self._on_batch_hydration_error, f"could not build batch plan: {error}")
            return
        except Exception as error:
            self.call_from_thread(self._on_batch_hydration_error, f"could not build batch plan: {error}")
            return
        self.call_from_thread(self._present_batch_confirmation, plan, batch)

    def _on_batch_hydration_error(self, message: str) -> None:
        self.batch_mutation_in_flight = False
        self._current_batch_plan = None
        self.notify(message, severity="error")

    def _present_batch_confirmation(
        self,
        plan: action_plan.BatchActionPlan,
        batch: list[Stop],
    ) -> None:
        if not self.batch_mutation_in_flight:
            return
        self._current_batch_plan = plan
        self._batch_generation_token += 1
        current_token = self._batch_generation_token
        preview = plan.preview()

        def confirmed(typed_items: str | None) -> None:
            if self._batch_generation_token != current_token:
                # A stale callback from a dismissed/aborted modal. A newer batch
                # may already own batch_mutation_in_flight/_current_batch_plan;
                # touching either here would clobber that active batch's state.
                return
            if not self.batch_mutation_in_flight or self._current_batch_plan is not plan:
                self.batch_mutation_in_flight = False
                self._current_batch_plan = None
                self.notify("batch mutation aborted or stale.", severity="warning")
                return
            if not typed_items:
                self.batch_mutation_in_flight = False
                self._current_batch_plan = None
                self.notify("aborted; nothing was run.", severity="warning")
                return
            try:
                confirmation = plan.confirm_human(
                    preview=preview,
                    actor=self.self_login,
                    tenant="projectbluefin",
                    typed_items=typed_items,
                )
                eligibility = plan.execution_eligibility(confirmation)
            except action_plan.ActionPlanError as error:
                self.batch_mutation_in_flight = False
                self._current_batch_plan = None
                self.notify(f"confirmation failed: {error}", severity="error")
                return
            self.execute_batch_queue_plan(plan, eligibility, batch)

        self.push_screen(BatchMutationConfirmation(preview), confirmed)

    @work(thread=True)
    def execute_batch_queue_plan(
        self,
        plan: action_plan.BatchActionPlan,
        eligibility: action_plan.BatchExecutionEligibility,
        batch: list[Stop],
    ) -> None:
        stops_by_key = {s.key: s for s in batch}

        def current_state_fetcher(item: action_plan.BatchMutationItem) -> action_plan.CurrentState:
            result = gh(
                "pr", "view", str(item.pull_request),
                "--repo", item.repository,
                "--json", "headRefOid,statusCheckRollup,mergeable,mergeStateStatus,isDraft",
            )
            if result.returncode != 0:
                raise action_plan.PlanDriftError("cannot fetch live PR state")
            try:
                live_data = json.loads(result.stdout)
            except Exception as error:
                raise action_plan.PlanDriftError(f"malformed live PR data: {error}")
            if live_data.get("isDraft"):
                raise action_plan.PlanDriftError("PR is draft")
            head = str(live_data.get("headRefOid") or "")
            if not head:
                raise action_plan.PlanDriftError("live PR has no head SHA")
            stop = stops_by_key.get(f"{item.repository}#{item.pull_request}")
            snapshot_check = stop.check_state if stop else "unknown"
            return action_plan.CurrentState.capture(
                actor=self.self_login,
                tenant="projectbluefin",
                repository=item.repository,
                pull_request=item.pull_request,
                head_sha=head,
                permissions={"self_login": self.self_login},
                checks={"ci": effective_check_state(snapshot_check, live_data)},
                live=live_data,
            )

        def executor(
            item: action_plan.BatchMutationItem,
            operation: tuple[str, ...],
        ) -> action_plan.OperationResult:
            cmd = list(operation)
            if (
                len(cmd) == 4
                and cmd[0] == "python3"
                and cmd[1] == "image/tui/hive_api.py"
                and HIVE_API_HELPER != "image/tui/hive_api.py"
            ):
                cmd[0] = sys.executable
                cmd[1] = HIVE_API_HELPER
            res = _run_mutation(
                cmd,
                timeout=MUTATION_TIMEOUT,
                idempotent=False,
            )
            return action_plan.OperationResult(
                return_code=res.returncode,
                detail=res.stderr.strip() if res.returncode != 0 else "",
            )

        try:
            receipt = plan.execute(
                eligibility,
                current_state_fetcher,
                executor,
                ledger=self._batch_receipt_ledger,
            )
            self.call_from_thread(self._on_batch_execution_finished, receipt, batch, plan)
        except action_plan.PlanExpiredError as error:
            self.call_from_thread(self._on_batch_execution_error, str(error), batch, plan)
        except Exception as error:
            self.call_from_thread(self._on_batch_execution_error, str(error), batch, plan)

    def _on_batch_execution_error(
        self,
        error_message: str,
        batch: list[Stop],
        plan: action_plan.BatchActionPlan | None,
    ) -> None:
        self.batch_mutation_in_flight = False
        self._current_batch_plan = None
        self.notify(f"batch mutation failed: {error_message}", severity="error")
        if plan and hasattr(plan, "items"):
            stops_by_key = {s.key: s for s in batch}
            for item in plan.items:
                stop = stops_by_key.get(f"{item.repository}#{item.pull_request}")
                if stop:
                    stop.failure = error_message
        self.refresh_rows()

    def _on_batch_execution_finished(
        self,
        receipt: action_plan.BatchActionReceipt,
        batch: list[Stop],
        plan: action_plan.BatchActionPlan,
    ) -> None:
        self.batch_mutation_in_flight = False
        self._current_batch_plan = None
        self.batch_action_receipt = receipt
        stops_by_key = {s.key: s for s in batch}

        for item in plan.items:
            stop = stops_by_key.get(f"{item.repository}#{item.pull_request}")
            if not stop:
                continue
            item_key = (item.repository, item.pull_request)
            if item_key in receipt.succeeded or item.identity in receipt.succeeded or stop.key in receipt.succeeded:
                stop.failure = ""
                landing.record_event(
                    stop.key, "queued", f"queued by @{self.self_login or 'maintainer'}"
                )
            elif item_key in receipt.rejected or item.identity in receipt.rejected or stop.key in receipt.rejected:
                reason = (
                    receipt.rejected.get(item_key)
                    or receipt.rejected.get(item.identity)
                    or receipt.rejected.get(stop.key)
                )
                stop.failure = str(reason)
            elif item_key in receipt.failed or item.identity in receipt.failed or stop.key in receipt.failed:
                reason = (
                    receipt.failed.get(item_key)
                    or receipt.failed.get(item.identity)
                    or receipt.failed.get(stop.key)
                )
                stop.failure = str(reason)

        self.refresh_rows()
        if receipt.succeeded:
            self._request_reconciliation()

    def action_merge(self) -> None:
        batch = self._pr_action_targets()
        if not batch:
            return
        if len(batch) > 1 or any(s.selected for s in self.stops):
            if len(batch) > 1:
                self.batch_queue_automerge(batch)
                return
            stop = batch[0]
            if self.batch_mutation_in_flight:
                self.notify("a batch mutation is already in flight", severity="warning")
                return
            if not stop.live:
                self.notify(f"{stop.key}: no live evidence yet; select it first.")
                return
            if self._queueable(stop):
                self._queue_automerge(stop)
            return
        if self.batch_mutation_in_flight:
            self.notify("a batch mutation is already in flight", severity="warning")
            return
        stop = batch[0]
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
        if self.view_mode == "issues":
            self.notify("action applies to pull requests only", severity="warning")
            return
        selected = [s for s in self.stops if s.selected]
        if not selected:
            if self.current and self.current.is_issue:
                self.notify("action applies to pull requests only", severity="warning")
                return
            self.notify(
                "nothing selected — [b] marks rows for the batch.",
                severity="warning",
            )
            return
        batch = [s for s in selected if not s.is_issue]
        if len(batch) < len(selected):
            self.notify("selected issues skipped; action applies to pull requests only", severity="warning")
        if not batch:
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
        # The final-review policy is a session decision, asked once before
        # the first batch of the session is dispatched (#378) and kept only
        # in memory. Every later batch reuses it; [p] changes it.
        if self.final_policy is None:
            def chosen(policy: str | None) -> None:
                self.final_policy = policy or "automatic"
                self.refresh_status()
                self.plan_landing(batch)

            self.push_screen(FinalPolicyScreen(), chosen)
            return
        # Partition stops by repository to allow concurrent landing lanes (#399)
        groups: dict[str, list[Stop]] = {}
        for stop in batch:
            repo = (
                getattr(stop, "repository", "")
                or getattr(stop, "repo", "")
                or (stop.key.rsplit("#", 1)[0] if "#" in getattr(stop, "key", "") else "")
            )
            groups.setdefault(repo, []).append(stop)

        should_partition = (
            len(groups) > 1
            and os.environ.get("BLUEFIN_REVIEW_PARTITION_BATCH", "1") != "0"
        )
        if should_partition:
            tasks = [landing.new_task(repo_stops, self.self_login) for repo_stops in groups.values()]
        else:
            tasks = [landing.new_task(batch, self.self_login)]

        for task in tasks:
            task.policy = self.final_policy

        def finish(confirmed: bool | None) -> None:
            if not confirmed:
                self.notify("aborted; nothing was dispatched.", severity="warning")
                return
            for task in tasks:
                self.enqueue_landing(task)

        self.push_screen(BatchPlanScreen(tasks if should_partition else tasks[0]), finish)

    def _landing_repositories(self, task: "landing.LandingTask") -> set[str]:
        repositories: set[str] = set()
        for stop in getattr(task, "stops", ()):
            repository = (
                getattr(stop, "repository", "")
                or getattr(stop, "repo", "")
            )
            if repository:
                repositories.add(str(repository))
        if repositories:
            return repositories
        repositories = {
            str(key).rsplit("#", 1)[0]
            for key in getattr(task, "keys", ())
            if "#" in str(key) and str(key).rsplit("#", 1)[0]
        }
        if repositories:
            return repositories
        return {str(getattr(task, "repo", "") or "")}

    def _landing_task_active(self, task: "landing.LandingTask") -> bool:
        return (
            id(task) in self._landing_active
            or bool(getattr(task, "running", False))
            or (
                getattr(task, "process", None) is not None
                and getattr(task, "returncode", None) is None
            )
        )

    def _prune_landing_queue(self) -> None:
        """Bound the landing history. Caller must hold _landing_condition.

        Only finished tasks are evicted, oldest first, so a running or
        not-yet-started landing is never dropped and the newest task stays
        last -- the status line reads the queue's tail.
        """
        if len(self.landing_queue) <= MAX_LANDING_QUEUE:
            return
        surplus = len(self.landing_queue) - MAX_LANDING_QUEUE
        kept: list["landing.LandingTask"] = []
        for task in self.landing_queue:
            finished = (
                task.returncode is not None
                and id(task) not in self._landing_active
            )
            if surplus > 0 and finished:
                # id() is reused once the object is freed, so a stale
                # membership entry would misreport an unrelated later task
                # as active.
                self._landing_active.discard(id(task))
                surplus -= 1
                continue
            kept.append(task)
        self.landing_queue = kept

    def enqueue_landing(self, task: "landing.LandingTask") -> None:
        with self._landing_condition:
            self.landing_queue.append(task)
            self._prune_landing_queue()
            if not task.phase:
                # A new dispatch supersedes the previous batch's outcome line.
                # A final-review round belongs to the batch already on that
                # line, so it must not wipe the outcome it is reviewing (#378).
                self.last_landing_outcome = ""
            self._landing_condition.notify_all()
        self.refresh_status()
        self.drain_landings()

    @work(thread=True)
    def drain_landings(self) -> None:
        """Dispatch eligible landing tasks without crossing repository lanes."""
        def pending() -> bool:
            return any(
                task.process is None
                and task.returncode is None
                and id(task) not in self._landing_active
                for task in self.landing_queue
            )

        with self._landing_condition:
            if self.landing_draining:
                self._landing_condition.notify_all()
                return
            self.landing_draining = True
        try:
            while True:
                with self._landing_condition:
                    active = [
                        task
                        for task in self.landing_queue
                        if self._landing_task_active(task)
                    ]
                    running_repos: set[str] = set()
                    for task in active:
                        running_repos.update(self._landing_repositories(task))
                    slots = MAX_CONCURRENT_LANDINGS - len(active)
                    for task in self.landing_queue:
                        if slots <= 0:
                            break
                        if (
                            task.process is not None
                            or task.returncode is not None
                            or id(task) in self._landing_active
                        ):
                            continue
                        task_repos = self._landing_repositories(task)
                        if not task_repos.isdisjoint(running_repos):
                            continue
                        self._landing_active.add(id(task))
                        active.append(task)
                        running_repos.update(task_repos)
                        slots -= 1
                        worker = threading.Thread(
                            target=self.run_landing_task,
                            args=(task,),
                            name=f"landing-{task.task_id}",
                            daemon=True,
                        )
                        try:
                            worker.start()
                        except RuntimeError as error:
                            self._landing_active.discard(id(task))
                            active.remove(task)
                            running_repos = set()
                            for running in active:
                                running_repos.update(
                                    self._landing_repositories(running)
                                )
                            slots += 1
                            task.returncode = 1
                            self.call_from_thread(
                                self.notify,
                                f"landing worker: {error}",
                                severity="error",
                            )
                            self.call_from_thread(self.landing_finished, task)
                    if not pending() and not active:
                        return
                    self._landing_condition.wait()
        finally:
            with self._landing_condition:
                self.landing_draining = False
                restart = pending()
            if restart:
                self.drain_landings()

    def run_landing_task(self, task: "landing.LandingTask") -> None:
        """One batch agent, off the UI thread, its own process group so
        [x] stops the agent and everything it spawned together."""
        task.started = time.monotonic()
        try:
            try:
                log = open(task.log_path, "a", encoding="utf-8")
            except OSError as error:
                task.returncode = 1
                self.call_from_thread(
                    self.notify, f"landing log: {error}", severity="error"
                )
            else:
                with log:
                    try:
                        process = subprocess.Popen(
                            task.command,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                            # A final-review round runs with the model its phase
                            # chose, passed explicitly (#378): the launch-time
                            # GOOSE_MODEL is whatever the maintainer picked
                            # for the dashboard, so inheriting it silently
                            # reviews with the wrong model. A landing task
                            # carries no overlay and inherits the environment
                            # exactly as it always did.
                            env={**os.environ, **task.env} if task.env else None,
                        )
                    except OSError as error:
                        task.returncode = 1
                        self.call_from_thread(
                            self.notify,
                            f"landing agent: {error}",
                            severity="error",
                        )
                    else:
                        task.process = process
                        self.call_from_thread(self.refresh_status)
                        task.returncode = process.wait()
        finally:
            with self._landing_condition:
                self._landing_active.discard(id(task))
                self._landing_condition.notify_all()
            self.call_from_thread(self.landing_finished, task)

    def _wake_landing_dispatch(self) -> None:
        with self._landing_condition:
            self._landing_condition.notify_all()
        self.drain_landings()

    def landing_finished(self, task: "landing.LandingTask") -> None:
        """Fold the agent's report back onto rows without re-arming retries.

        Landing results remain visible as row failures, but every retry needs
        an explicit maintainer selection and confirmation.
        """
        if task.phase:
            # A final review-and-fix round (#378) is not a landing: the pull
            # requests already have their outcomes, so re-folding them would
            # re-announce a batch that already finished. What a round
            # reports is its phase.
            self.final_round_finished(task)
            self._wake_landing_dispatch()
            return
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
                stop.selected = False
                stop.failure = (
                    f"{state}: "
                    f"{bounded_detail(str(event.get('note', 'no reason given')))}"
                )
            else:
                detail = f"last report: {state}" if state else "no report"
                stop.selected = False
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
        duration = max(0.0, time.monotonic() - task.started)
        if counts["failed"]:
            outcome = "failed"
        elif counts["no outcome"] or counts["died mid-batch"] or not done:
            outcome = "incomplete"
        elif counts["blocked"] or counts["awaiting-stable"]:
            outcome = "blocked"
        else:
            outcome = "complete"
        self.observability.operation(f"landing.{outcome}", duration)
        # Set before refresh_rows: refresh_status renders it onto the bar.
        self.last_landing_outcome = message
        self.restore_recent_merges()
        self.refresh_rows()
        self.notify(message, severity=severity)
        if done:
            self._request_reconciliation()
        self.advance_final_review(task)
        self._wake_landing_dispatch()

    def final_round_finished(self, task: "landing.LandingTask") -> None:
        """Announce one finished final-review round and start the next."""
        recorded = landing.final_phase(task.status_path)
        phase = str(recorded.get("phase", "")) or "no result"
        message = (
            f"batch {task.task_id.split(' · ')[0]} final review "
            f"round {task.round}/{landing.FINAL_ROUND_LIMIT} "
            f"({task.model}): {phase}"
        )
        note = str(recorded.get("note", ""))
        if note:
            message = f"{message} — {note}"
        severity = (
            "information" if phase == "final-review-clean"
            else "error" if phase in ("review-blocked", "no result")
            else "warning"
        )
        self.notify(message, severity=severity)
        self.advance_final_review(task)
        self.refresh_status()

    def advance_final_review(self, task: "landing.LandingTask") -> None:
        """Drive the final review-and-fix rounds for a finished batch (#378).

        Every round is a fresh process in the SAME lane: this appends the
        next round to the landing queue and returns. What decides the next
        round is the durable record, not memory, so a round that reported
        `final-review-clean` or `review-blocked` ends the phase whatever
        this process believes, and a dashboard restart reads the same
        answer. A landing that never reached terminal outcomes gets no
        final review at all — there is nothing settled to review.
        """
        policy = task.policy or self.final_policy or "automatic"
        events = landing.parse_status(task.status_path)
        terminal = all(
            events.get(stop.key, {}).get("state") in landing.TERMINAL_PR_STATES
            for stop in task.stops
        )
        if not task.stops or not terminal:
            return
        recorded = landing.final_phase(task.status_path)
        phase = str(recorded.get("phase", ""))
        if phase in landing.FINAL_TERMINAL_PHASES:
            return
        if task.phase and len(landing.final_rounds(task.status_path)) <= task.rounds_seen:
            # The round just run wrote nothing to the record. Dispatching the
            # same phase again would loop forever on a broken agent, so the
            # batch stops here with whatever the record does hold.
            landing.report_final(
                task.status_path,
                min(task.round or 1, landing.FINAL_ROUND_LIMIT),
                "review-blocked",
                task.model or "",
                f"{task.phase} round {task.round} reported nothing",
            )
            self.notify(
                f"batch {task.task_id} reported no {task.phase} result; "
                "the batch is review-blocked.",
                severity="error",
            )
            self.refresh_status()
            return
        round_number = int(recorded.get("round") or 0)
        findings = str(recorded.get("note", ""))
        if not phase:
            nxt, number = "final-review", 1
        elif phase in ("final-review", "re-review"):
            # A review that found something hands its findings to a fresh
            # fixer; the reviewer never fixes what it reviewed.
            nxt, number = "fixing", round_number
        elif phase == "fixing":
            nxt, number = "re-review", round_number + 1
        else:
            nxt, number = "cleanup", round_number
        if number > landing.FINAL_ROUND_LIMIT:
            # The breaker: the record refuses a sixth round, and the batch
            # stays visibly blocked with its findings rather than looping.
            landing.report_final(
                task.status_path,
                landing.FINAL_ROUND_LIMIT,
                "review-blocked",
                task.model or "",
                f"{landing.FINAL_ROUND_LIMIT} rounds did not clear the findings",
            )
            self.notify(
                f"batch {task.task_id} is review-blocked after "
                f"{landing.FINAL_ROUND_LIMIT} rounds; findings are kept.",
                severity="error",
            )
            self.refresh_status()
            return
        try:
            round_task = landing.new_final_round(
                task, nxt, number, policy, findings, ACTIVE_BACKEND
            )
        except OSError as error:
            self.notify(f"final review: {error}", severity="error")
            return
        self.enqueue_landing(round_task)

    def restore_landing_marks(self, stops: list[Stop]) -> None:
        """Fold a previous run's landing outcomes back onto matching rows.
        The state directory persists on the host across relaunches (#281),
        but the record only helps if the rows show it. Only the failure
        marking is restored; selecting a batch stays the maintainer's."""
        events = landing.persisted_events()
        self.restore_recent_merges(events)
        if not events:
            return
        for stop in stops:
            event = events.get(stop.key)
            if not event:
                continue
            state = event.get("state")
            if state in ("blocked", "failed", "awaiting-stable"):
                stop.failure = (
                    f"{state}: "
                    f"{bounded_detail(str(event.get('note', 'no reason given')))}"
                )

    def restore_recent_merges(self, events: dict[str, dict] | None = None) -> None:
        events = landing.persisted_events() if events is None else events
        merged: list[tuple[int, int, str]] = []
        for order, (key, event) in enumerate(events.items()):
            timestamp = event.get("ts")
            if (
                event.get("state") == "merged"
                and PERSISTED_PR_KEY_PATTERN.fullmatch(key)
                and isinstance(timestamp, int)
                and not isinstance(timestamp, bool)
                and timestamp >= 0
            ):
                merged.append((timestamp, order, key))
        merged.sort(key=lambda item: (-item[0], -item[1]))
        self.recent_merges = [
            key for _, _, key in merged[:MAX_RECENT_MERGES]
        ]

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
        batch = self._pr_action_targets()
        if not batch:
            return
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
            self.restore_recent_merges()
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
        target = "issue" if stop.is_issue else "pr"
        message = (
            "Closing issue after maintainer triage.\n"
            if stop.is_issue
            else "Closing after maintainer review; see the review notes above.\n"
        )
        with open(body_file, "w", encoding="utf-8") as sink:
            sink.write(message)
        then = None
        if stop.is_issue:
            def on_issue_closed() -> None:
                self.issues_items = [
                    it for it in self.issues_items
                    if not (it.get("repository") == stop.repository and it.get("number") == stop.number)
                ]
                self.apply_filters(refreshed_source="issues")
            then = on_issue_closed

        self.mutate_all(
            stop,
            [
                [
                    "gh", target, "comment", str(stop.number),
                    "--repo", stop.repository, "--body-file", body_file,
                ],
                ["gh", target, "close", str(stop.number), "--repo", stop.repository],
            ],
            then=then,
        )

    def action_handoff(self) -> None:
        """Copy the stop's identity, live evidence, and cluster verdicts to
        the reviewer's clipboard (OSC 52 through the attached terminal), so
        the review context can be handed to an issue, a chat, or another
        agent. Read-only."""
        stop = self.current
        if not stop:
            return
        if stop.is_issue:
            live = stop.live
            lines = [
                f"{stop.key} — {stop.title}",
                issue_url(stop.repository, stop.number),
                f"author: {stop.author or (live.get('author') or {}).get('login', '?')}",
                f"state: {live.get('state', 'OPEN')}",
            ]
            raw_labels = live.get("labels") or []
            label_names = [
                l["name"] if isinstance(l, dict) and "name" in l else l
                for l in raw_labels
            ]
            if label_names:
                lines.append(f"labels: {', '.join(label_names)}")
            body = str(live.get("body") or "").strip()
            if body:
                lines.append(f"summary:\n{body[:500]}")
            self.copy_to_clipboard("\n".join(lines))
            self.notify(
                f"handoff for {stop.key} copied (OSC 52; the terminal must support it)."
            )
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
        if self.view_mode == "issues" or (self.current and self.current.is_issue):
            self.notify("action applies to pull requests only", severity="warning")
            return
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


# review_snapshot imports live_review_verification from this module. Loading
# the engine after the dashboard definitions keeps that shared seam acyclic
# when this file is launched directly instead of as tui.bluefin_review_tui.
sys.modules.setdefault("tui.bluefin_review_tui", sys.modules[__name__])
from tui.review_engine import ReviewBatch, ReviewEngine, ReviewEvent
from tui.review_snapshot import BatchReviewItem, BatchSnapshot


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
    args = parser.parse_args()
    filters = QueueFilters(
        action="" if args.all else args.action,
        repository=args.repo,
        live_repository=args.live_repo,
    )
    ReviewDashboard(filters).run()


if __name__ == "__main__":
    main()
