"""Pilot tests: drive the real dashboard and assert what it does.

These run the Textual app through ``run_test()``, so a binding that points at
nothing, a screen that never reaches a terminal state, and a review whose
outcome is misreported are all failures here. The previous contract was a set
of greps over this file's source text, which passed while the dashboard had no
way to review anything at all.

No network and no GitHub: the queue is served by a stubbed ``gh`` on PATH and
the review engine is replaced by a stub script whose exit status the test
chooses.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path

from textual.events import Key
from textual.geometry import Region

TUI_DIR = Path(
    os.environ.get(
        "BLUEFIN_REVIEW_TUI_DIR",
        Path(__file__).resolve().parent.parent / "image" / "tui",
    )
)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(TUI_DIR.parent))

SNAPSHOT = {
    "items": [
        {
            "repository": "projectbluefin/bluefinctl",
            "number": 31,
            "recommended_action": "review",
            "title": "fix: ci.yml add permissions block",
            "author": "someone-else",
        },
        {
            "repository": "projectbluefin/common",
            "number": 7,
            "recommended_action": "investigate",
            "title": "chore: bump digest",
            "author": "someone-else",
        },
        {
            "repository": "projectbluefin/review",
            "number": 9,
            "recommended_action": "review",
            "title": "my own work",
            "author": "castrojo",
        },
    ],
}

# Fixture recommended_action → the GraphQL search evidence that the
# dashboard's classifier maps back to exactly that action.
ORG_EVIDENCE = {
    "review": ("REVIEW_REQUIRED", "MERGEABLE", "SUCCESS"),
    "ready-for-human-merge": ("APPROVED", "MERGEABLE", "SUCCESS"),
    "fix-ci": ("REVIEW_REQUIRED", "MERGEABLE", "FAILURE"),
    "resolve-conflicts": ("REVIEW_REQUIRED", "CONFLICTING", "SUCCESS"),
    "investigate": ("REVIEW_REQUIRED", "UNKNOWN", "SUCCESS"),
}


def org_search_pages(items: list[dict], pages_count: int = 1) -> str:
    """GraphQL search pages, as `gh api graphql --paginate --slurp` emits."""
    pages = []
    for page in range(pages_count):
        nodes = []
        for item in items[page::pages_count]:
            review, mergeable, rollup = ORG_EVIDENCE[
                item.get("recommended_action", "review")
            ]
            reviews_data = item.get("reviews") or []
            if isinstance(reviews_data, list):
                reviews_nodes = {"nodes": reviews_data}
            elif isinstance(reviews_data, dict):
                reviews_nodes = reviews_data
            else:
                reviews_nodes = {"nodes": []}
            nodes.append({
                "number": item["number"],
                "title": item["title"],
                "updatedAt": item.get("updated_at", "2026-08-08T00:00:00Z"),
                "author": {"login": item.get("author", "")},
                "repository": {"nameWithOwner": item["repository"]},
                "labels": {"nodes": [{"name": name} for name in item.get("labels", [])]},
                "reviewDecision": review,
                "baseRefOid": item.get("base_sha", "a" * 40),
                "headRefOid": item.get("head_sha", f"{item['number']:040x}"),
                "mergeable": mergeable,
                "commits": {
                    "nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]
                },
                "reviews": reviews_nodes,
            })
        pages.append({
            "data": {
                "search": {
                    "pageInfo": {
                        "hasNextPage": page + 1 < pages_count,
                        "endCursor": str(page + 1) if page + 1 < pages_count else None,
                    },
                    "nodes": nodes,
                }
            }
        })
    return json.dumps(pages)

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(description)


async def settle_evidence(app, pilot) -> None:
    """Drain the dashboard's mount-time evidence workers (#339).

    Row highlight, the Hive probe, and harness discovery run as thread
    workers whose UI callbacks rewrite a stop's live/overlap evidence — and
    one of those callbacks (render_evidence/hive_loaded) spawns a follow-up
    worker (render_context) whose own callback is the final rewrite. Drain
    worker-and-callback rounds until nothing in flight can clobber fixture
    evidence between its assignment and the start of the review.
    """
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def write_stub(path: Path, body: str) -> str:
    if path.name == "gh":
        body = (
            "args=()\n"
            'for a in "$@"; do\n'
            '  if [ "$a" != "--include" ] && [ "$a" != "-i" ]; then\n'
            '    args+=("$a")\n'
            "  fi\n"
            "done\n"
            'set -- "${args[@]}"\n' + body
        )
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dashboard-pilot."))

    # The org queue: what the stubbed `gh api graphql` search returns. The
    # dashboard has no static snapshot path; this is its only default source.
    org_queue_file = workdir / "org-queue.json"

    def set_org_queue(items: list[dict], pages_count: int = 1) -> None:
        org_queue_file.write_text(org_search_pages(items, pages_count))

    set_org_queue(SNAPSHOT["items"])

    # gh is read-only here: the pilot never lets a mutation reach a real
    # network, and any attempt to run one is recorded for the assertions.
    gh_log = workdir / "gh.log"
    curl_log = workdir / "curl.log"
    queue_refresh_log = workdir / "queue-refresh.log"
    delay_queue_refresh = workdir / "delay-queue-refresh"
    diff_events = workdir / "diff-events.log"
    old_request_started = workdir / f"old-request-start-{workdir.name}"
    perm_file = workdir / "permissions.push"
    perm_file.write_text("true\n")
    org_issues_file = workdir / "org-issues.json"
    issue_view_file = workdir / "issue-view.json"

    def set_org_issues(items: list[dict]) -> None:
        org_issues_file.write_text(
            json.dumps([
                {
                    "data": {
                        "search": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": items,
                        }
                    }
                }
            ])
        )

    issue_node = {
        "number": 42,
        "title": "bug: test issue",
        "updatedAt": "2026-09-06T01:00:00Z",
        "createdAt": "2026-09-06T00:00:00Z",
        "author": {"login": "testuser"},
        "repository": {"nameWithOwner": "projectbluefin/review"},
        "labels": {"nodes": [{"name": "bug"}]},
        "comments": {"totalCount": 1},
        "body": "Issue description test body",
    }
    set_org_issues([])

    issue_view_file.write_text(
        json.dumps({
            "title": "bug: test issue",
            "body": "Issue description test body",
            "author": {"login": "testuser"},
            "labels": [{"name": "bug"}],
            "comments": [
                {
                    "createdAt": "2026-09-06T00:00:00Z",
                    "author": {"login": "helper"},
                    "body": "comment text",
                }
            ],
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T01:00:00Z",
            "state": "OPEN",
        })
    )

    org_queue_branch = (
        'if [ "$1 $2" = "api graphql" ]; then\n'
        '  if [ -n "${ORG_GH_ERROR-}" ]; then printf "%s\\n" "$ORG_GH_ERROR" >&2; exit 1; fi\n'
        '  case "$*" in *"is:issue"*) '
        '  if [ -n "${ORG_ISSUES_FAIL-}" ]; then echo "GraphQL 502 issue search failed" >&2; exit 1; fi\n'
        f'  cat "{org_issues_file}"; exit 0 ;; esac\n'
        f'  if [ -f "{delay_queue_refresh}" ]; then\n'
        f'    printf "request\\n" >>"{queue_refresh_log}"\n'
        '    sleep 0.45\n'
        '  fi\n'
        '  if [ -n "${ORG_QUEUE_GH_ERROR-}" ]; then printf "%s\\n" "$ORG_QUEUE_GH_ERROR" >&2; exit 1; fi\n'
        f'  cat "{org_queue_file}"; exit 0\n'
        'fi\n'
        'if [ "$1 $2" = "issue view" ]; then\n'
        '  if [ -n "${ISSUE_VIEW_JSON-}" ]; then printf "%s\\n" "$ISSUE_VIEW_JSON"; exit 0; fi\n'
        f'  cat "{issue_view_file}"; exit 0\n'
        'fi\n'
        'if [ "$1 $2" = "issue comment" ] || [ "$1 $2" = "issue close" ]; then\n'
        '  exit 0\n'
        'fi\n'
    )
    gh_stub = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then\n'
        '  if [ -n "${GH_USER_FAIL-}" ]; then echo "authentication required" >&2; exit 1; fi\n'
        '  echo castrojo; exit 0;\n'
        'fi\n'
        + org_queue_branch +
        'if [ "$1" = "api" ] && [[ "$2" == repos/*/compare/* ]]; then\n'
        '  if [ -n "${RE_REVIEW_COMPARE_FAIL-}" ]; then echo "compare unavailable" >&2; exit 1; fi\n'
        f'  printf "compare:%s\\n" "${{RE_REVIEW_COMPARE_JSON-UNSET}}" >>"{gh_log}"\n'
        '  if [ -n "${RE_REVIEW_COMPARE_JSON+x}" ]; then printf "%s\\n" "$RE_REVIEW_COMPARE_JSON"; else printf "%s\\n" "{}"; fi; exit 0\n'
        'fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then\n'
        '  if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi\n'
        '  echo "{}"; exit 0;\n'
        'fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        f'  request_id="${{DIFF_REQUEST_ID-unknown}}"; mode="${{DIFF_MODE-}}"\n'
        f'  if [ "$mode" = "slow-old" ]; then printf "request:%s:%s\\n" "$request_id" "$mode" >>"{diff_events}"; (sleep 0.2) & delay_pid=$!; : >"{old_request_started}"; wait "$delay_pid"; printf "response:%s:OLD-DIFF\\n" "$request_id" >>"{diff_events}"; printf "%s" "OLD-DIFF"; exit 0; fi\n'
        f'  if [ "$mode" = "fast-new" ]; then printf "request:%s:%s\\n" "$request_id" "$mode" >>"{diff_events}"; printf "response:%s:NEW-DIFF\\n" "$request_id" >>"{diff_events}"; printf "%s" "NEW-DIFF"; exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "oversized" ]; then head -c 400010 /dev/zero | tr "\\0" x; exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "empty" ]; then exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "error" ]; then printf "%s\\n" "terminal diff failure" >&2; exit 7; fi\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "api" ] && [ "$2" = "--paginate" ]; then\n'
        '  if [ -n "${LIVE_GH_ERROR-}" ]; then printf "%s\\n" "$LIVE_GH_ERROR" >&2; exit 1; fi\n'
        '  case "$*" in *"/issues"*) if [ -n "${LIVE_ISSUES_FILE-}" ]; then cat "$LIVE_ISSUES_FILE"; else echo "[]"; fi; exit 0 ;; esac\n'
        '  if [ -n "${LIVE_PAGES-}" ]; then cat "$LIVE_QUEUE_FILE"; else printf "[%s]" "$(cat "$LIVE_QUEUE_FILE")"; fi; exit 0\n'
        'fi\n'
        'if [ "$1 $2" = "pr list" ]; then\n'
        '  echo "[]"\n'
        '  exit 0\n'
        'fi\n'
        "exit 0\n",
    )
    os.environ["PATH"] = f"{workdir}:{os.environ['PATH']}"
    os.environ["XDG_STATE_HOME"] = str(workdir / "state")
    os.environ["HIVE_HUB"] = "wss://hive.example.test/contribute"
    os.environ["GH_TOKEN"] = "dashboard-pilot-token"
    write_stub(
        workdir / "curl",
        f'printf "%s\\n" "$*" >>"{curl_log}"\n'
        'if [ -n "${CURL_FAIL-}" ]; then '
        'printf "Hive queue failed %s\\n" "$(printf "e%.0s" {1..300})" >&2; exit 1; fi\n'
        'printf "%s\\n" \'{"status":"queued"}\'\n',
    )

    review_log = workdir / "review.log"
    steer_log = workdir / "steer.log"

    def review_stub(exit_code: int, output: str) -> str:
        review_output = workdir / "review-output.txt"
        review_output.write_text(output)
        return write_stub(
            workdir / "bluefin-review",
            f'printf "%s\\n" "$*" >>"{review_log}"\n'
            f'printf "%s\\n" "${{BLUEFIN_REVIEW_STEER-}}" >>"{steer_log}"\n'
            f'cat "{review_output}"\n'
            f"exit {exit_code}\n",
        )

    os.environ["BLUEFIN_REVIEW_COMMAND"] = str(workdir / "bluefin-review")
    review_stub(0, "a finding")

    import tui.bluefin_review_tui as tui
    tui.TRACE_PATH = str(Path(os.environ["XDG_STATE_HOME"]) / "bluefin-review" / "trace.jsonl")
    tui._TRACE_LOGGER = None
    reconciliation_request = tui.ReviewDashboard._request_reconciliation
    # Most fixtures below drive an isolated landing/report state machine while
    # deliberately reusing one temporary landing directory. Keep those tests
    # focused on their own reports; the dedicated reconciliation pilot binds
    # the production callback back onto its dashboard below.
    tui.ReviewDashboard._request_reconciliation = lambda self: None

    # Every batch flow below would meet the #378 final-review policy gate,
    # which is asked once per dashboard process. It gets its own coverage
    # further down; here the session answer is pre-set so the unrelated
    # flows still exercise what they were written to exercise.
    _dashboard_init = tui.ReviewDashboard.__init__

    def _dashboard_with_policy(self, *args, **kwargs):
        _dashboard_init(self, *args, **kwargs)
        self.final_policy = "automatic"

    tui.ReviewDashboard.__init__ = _dashboard_with_policy

    hive_api_stub = workdir / "hive_api_stub.py"
    hive_api_stub.write_text(
        "import json, os, sys\n"
        f"with open({str(curl_log)!r}, 'a') as sink: sink.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('CURL_FAIL'):\n"
        "    print('Hive queue failed ' + ('e' * 300), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps({'status': 'queued'}))\n"
    )
    tui.HIVE_API_HELPER = str(hive_api_stub)

    # Queueing belongs to Hive: its authenticated endpoint records the human
    # actor, enforces merger standing and self-merge protection, then creates
    # the exact-head approval as the Hive App. A human-authored `gh pr review`
    # can never satisfy that governor contract (#247).
    original_hive_hub = os.environ.get("HIVE_HUB")
    os.environ["HIVE_HUB"] = "wss://hive.example.test/contribute"
    try:
        dashboard = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        dashboard.self_login = "castrojo"
        captured_queue = []
        dashboard.mutate_all = lambda *args, **kwargs: captured_queue.append(args)
        queue_stop = SimpleNamespace(
            number=31,
            repository="projectbluefin/bluefinctl",
            live={"isDraft": False},
        )
        dashboard._queue_automerge(queue_stop)
        queue_commands = captured_queue[0][1] if captured_queue else []
        check(
            len(queue_commands) == 1
            and queue_commands[0][1] == tui.HIVE_API_HELPER
            and queue_commands[0][2] == "queue"
            and queue_commands[0][-1]
            == "https://hive.example.test/api/v1/prs/projectbluefin/bluefinctl/31/queue-automerge",
            f"queueing must call Hive's App-authored queue endpoint once, got {queue_commands}",
        )
        check(
            "--location" not in queue_commands[0]
            and "-L" not in queue_commands[0],
            f"a mutating Hive request must not follow redirects, got {queue_commands}",
        )
        check(
            not any(command[:3] == ["gh", "pr", "review"] for command in queue_commands),
            f"queueing must not create a human-authored approval, got {queue_commands}",
        )
        check(
            "dashboard-pilot-token" not in shlex.join(queue_commands[0]),
            "the confirmation and trace command must not contain the GitHub token",
        )
    finally:
        if original_hive_hub is None:
            os.environ.pop("HIVE_HUB", None)
        else:
            os.environ["HIVE_HUB"] = original_hive_hub

    # A malformed ReviewBody result is not preview-authorized and must be a
    # no-op, including no temporary file and no mutation.
    callback = {}
    dashboard = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
    dashboard.push_screen = lambda _screen, handler: callback.update(handler=handler)
    dashboard.notify = lambda *args, **kwargs: None
    mutations = []
    dashboard.mutate_all = lambda *args, **kwargs: mutations.append(args)
    review_stop = SimpleNamespace(number=31, repository="projectblue/bluefinctl")
    with tempfile.TemporaryDirectory(prefix="dashboard-body-red-") as body_dir:
        original_trace = tui.TRACE_PATH
        tui.TRACE_PATH = str(Path(body_dir) / "trace.jsonl")
        try:
            dashboard.leave_review(review_stop)
            callback["handler"]("approve")
            callback["handler"]("unpreviewed body")
            callback["handler"]((123, "not-a-body-file"))
            check(not mutations and not list(Path(body_dir).rglob("*")),
                  "malformed ReviewBody results must not create a file or mutate")
        finally:
            tui.TRACE_PATH = original_trace

    # A syntactically valid foreign body tuple must not reach mutation or
    # delete the file it names.
    async with tui.ReviewDashboard(tui.QueueFilters()).run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if pilot.app.stops:
                break
            await pilot.pause(0.05)
        app = pilot.app
        captured = {}
        original_push_screen = app.push_screen
        app.push_screen = lambda screen, handler=None, *args, **kwargs: (
            captured.update(handler=handler),
            original_push_screen(screen, handler, *args, **kwargs),
        )[1]
        mutations = []
        app.mutate_all = lambda *args, **kwargs: mutations.append(args)
        app.leave_review(app.stops[0])
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        with tempfile.TemporaryDirectory(prefix="dashboard-foreign-body-") as foreign_dir:
            foreign_path = Path(foreign_dir) / "foreign.md"
            foreign_bytes = b"foreign body sentinel\n"
            foreign_path.write_bytes(foreign_bytes)
            captured["handler"](("foreign body", str(foreign_path)))
            check(not mutations, "foreign ReviewBody results must not reach mutation")
            check(foreign_path.read_bytes() == foreign_bytes,
                  "foreign ReviewBody results must not delete or change their file")

    check(
        not tui.QueueFilters(repository="acme/widgets").live,
        "--repo owner/repo must remain an org queue filter, not the live source",
    )
    check(
        tui.QueueFilters(live_repository="acme/widgets").live,
        "the distinct live repository filter must select the live source",
    )
    check(
        tui.PULL_FETCH_LIMIT == os.environ.get("BLUEFIN_REVIEW_PULL_LIMIT", "200"),
        "the pull fetch limit must remain configurable",
    )

    # The default org-wide source is live too: a gh failure, a malformed
    # response, and an empty search each become one honest source state.
    os.environ["ORG_GH_ERROR"] = "HTTP 502: GitHub search is unavailable"
    org_error_app = tui.ReviewDashboard(tui.QueueFilters())
    async with org_error_app.run_test() as pilot:
        for _ in range(600):
            if org_error_app.source_state == "error":
                break
            await pilot.pause(0.05)
        check(org_error_app.source_state == "error" and not org_error_app.stops,
              "an org queue failure must hold rows and report an error source")
        check("GitHub search is unavailable" in org_error_app.source_message,
              "an org queue failure must carry the gh detail")
    os.environ.pop("ORG_GH_ERROR", None)

    org_queue_file.write_text("{}")
    org_malformed_app = tui.ReviewDashboard(tui.QueueFilters(kind="prs"))
    async with org_malformed_app.run_test() as pilot:
        for _ in range(600):
            if org_malformed_app.source_state == "malformed":
                break
            await pilot.pause(0.05)
        check(org_malformed_app.source_state == "malformed" and not org_malformed_app.stops,
              "a malformed org search response must become a malformed source state")
    set_org_queue([])
    org_empty_app = tui.ReviewDashboard(tui.QueueFilters())
    async with org_empty_app.run_test() as pilot:
        for _ in range(600):
            if org_empty_app.source_state == "empty":
                break
            await pilot.pause(0.05)
        check(org_empty_app.source_state == "empty" and not org_empty_app.stops,
              "an empty org queue must read as empty, not as an error")
        check(org_empty_app.slay_frame == len(tui.SLAY_FRAMES) - 1,
              "an empty queue on startup must immediately hold the ALL SYSTEMS SLAY frame")
    set_org_queue(SNAPSHOT["items"])

    live_file = workdir / "live.json"
    live_file.write_text(json.dumps([
        {"number": 42, "title": "review me", "author": {"login": "other"},
         "state": "OPEN", "isDraft": False, "labels": [],
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": [],
         "baseRefOid": "a" * 40, "headRefOid": "b" * 40},
        {"number": 43, "title": "my own live work", "author": {"login": "castrojo"},
         "state": "OPEN", "isDraft": False, "labels": [],
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": [],
         "baseRefOid": "a" * 40, "headRefOid": "b" * 40},
    ]))
    os.environ["LIVE_QUEUE_FILE"] = str(live_file)
    live_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))

    # Polling budgets are ceilings, not timings (#284): a loaded CI runner
    # needs the headroom, and a fast one returns on the first true poll.
    async def wait_for_live_rows(app, pilot, state: str, count: int) -> None:
        for _ in range(600):
            if app.source_state == state and len(app.stops) == count:
                return
            await pilot.pause(0.05)

    async def wait_for_rendered_rows(app, pilot) -> None:
        for _ in range(600):
            queue = app.query_one("#queue", tui.ListView)
            if (
                len(queue.children) == len(app.stops)
                and all(
                    (
                        labels := list(item.query(tui.Label))
                    )
                    and stop.key in str(labels[0].render())
                    and (
                        stop.is_issue
                        or {
                            "blocked": "BLOCKED",
                            "failed": "FAILED",
                            "in progress": "IN PROGRESS",
                            "queued": "QUEUED",
                            "ready": "READY",
                            "done": "DONE",
                        }[app._queue_state(stop)] in str(labels[0].render())
                    )
                    for item, stop in zip(queue.children, app.stops)
                )
            ):
                return
            await pilot.pause(0.05)

    def rendered_row(app, stop) -> str:
        index = next(
            index
            for index, candidate in enumerate(app.stops)
            if candidate is stop
        )
        return str(
            app._queue().children[index].query(tui.Label).first().render()
        )

    retry = tui.Stop("projectbluefin/review", 1, "review", "retry")
    retry.failure = "review dispatch failed: connection reset"
    retry.failure_command = "gh pr view"
    retry.failure_checks = "unknown"
    retry.review_failure = "connection reset"
    tui.clear_review_failure_mark(retry)
    check(
        retry.failure == ""
        and retry.failure_command == "gh pr view"
        and retry.failure_checks == "unknown"
        and retry.review_failure == "",
        "a fresh review must keep diagnostic metadata while clearing its review failure",
    )
    retry.failure = "landing refused: no human review on GitHub"
    tui.clear_review_failure_mark(retry)
    check(
        retry.failure == "landing refused: no human review on GitHub",
        "a fresh review must preserve an unrelated landing failure mark",
    )

    # ── batch-review selection, triage navigation, and verdict badges ───
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        await pilot.press("B")
        check(
            all(stop.selected for stop in app.stops),
            "B must select every visible row",
        )
        await pilot.press("B")
        check(
            not any(stop.selected for stop in app.stops),
            "B must clear every visible selection",
        )
        await pilot.press("space")
        check(
            app.stops[0].selected and app._queue().index == 1,
            "Space must toggle the highlighted row and advance",
        )
        triage_before = dict(app.triage)
        await pilot.press("n")
        check(
            app._queue().index == 0,
            "n must jump to the next row lacking the current user's review",
        )
        check(
            app.triage == triage_before,
            "n must not modify local triage state",
        )
        app.stops[0].review_status = "running"
        app.stops[1].review_status = "cached"
        app.stops[1].cached_age = "4m"
        app.stops[1].review_result = tui.ReviewResult(
            1,
            "complete",
            {"critical": 0, "high": 0, "medium": 0, "low": 0},
        )
        app.refresh_rows()
        rendered = [
            str(child.query(tui.Label).first().render())
            for child in app._queue().children
        ]
        check("⏳" in rendered[0], "running rows must carry the pending badge")
        check(
            "✓" in rendered[1] and "4m" in rendered[1],
            "cached rows must carry verdict and age",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "Headroom" in status or "DIRECT" in status,
            "the batch status must surface the existing Headroom route status",
        )

    # ── [r] dispatches the selected exact-head snapshot as one batch ─────
    base_sha = "a" * 40
    head_sha = "b" * 40
    os.environ["PR_VIEW_JSON"] = json.dumps(
        {
            "author": {"login": "someone-else"},
            "state": "OPEN",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [
                {"name": "validate", "conclusion": "SUCCESS"}
            ],
        }
    )
    set_org_queue(
        [
            {**item, "base_sha": base_sha, "head_sha": head_sha}
            for item in SNAPSHOT["items"]
        ]
    )

    class FakeReviewEngine:
        def __init__(self):
            self.calls = []

        def start(
            self,
            snapshot,
            backend,
            model,
            effort,
            check_scope_version,
            check_scope="",
            on_event=None,
        ):
            self.calls.append(
                (
                    snapshot,
                    backend,
                    model,
                    effort,
                    check_scope_version,
                    check_scope,
                    on_event,
                )
            )
            return SimpleNamespace(
                batch_id="pilot-batch",
                status_path=str(workdir / "pilot-batch.jsonl"),
                items=snapshot.items,
                backend=backend,
                model=model,
                effort=effort,
                running=True,
                headroom_status_line="[ACTIVE] Goose/GitHub Copilot: via Headroom",
                headroom_output_reduction={
                    "output_reduction_percent": 37.5,
                    "output_reduction_method": "measured",
                },
            )

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        fake_engine = FakeReviewEngine()
        app.review_engine = fake_engine
        await pilot.press("B")
        await pilot.press("r")
        for _ in range(200):
            if app.review_batches:
                break
            await pilot.pause(0.05)
        check(
            len(fake_engine.calls) == 1
            and len(fake_engine.calls[0][0].items) == 2,
            "r with a selection must dispatch one exact-head batch",
        )
        check(
            not isinstance(app.screen, tui.ReviewScreen)
            and all(stop.review_status == "running" for stop in app.stops),
            "batch review must remain on the dashboard and mark every row running",
        )
        check(
            all(
                stop.triage_state == "reviewed"
                and app.triage[app.triage_key(stop)] == "reviewed"
                for stop in app.stops
            ),
            "batch dispatch must mark every exact head reviewed",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "37.5% measured" in status,
            f"batch status must include aggregate Headroom telemetry, got {status!r}",
        )
        first = app.stops[0]
        run = tui.ReviewRun(
            first.repository,
            first.number,
            base_sha,
            head_sha,
            base_sha[:12] + head_sha[:12],
            "goose",
            "gemini-3.8-flash",
            "max",
        )
        receipt = tui.ReviewReceipt.from_result(
            run,
            tui.ReviewResult(
                1,
                "findings",
                {"critical": 0, "high": 1, "medium": 0, "low": 0},
                [
                    {
                        "severity": "high",
                        "file": "image/tui/example.py",
                        "line": 7,
                        "title": "event finding",
                    }
                ],
                [],
                {"backend": "goose", "model": "gemini-3.8-flash"},
            ),
            ["bounded transcript"],
            app.review_scope_version,
        )
        receipt_path = app.review_cache.put(receipt)
        app.review_event(
            tui.ReviewEvent(
                first.key,
                "cached",
                "exact identity hit",
                int(time.time()),
                receipt_path.name,
            )
        )
        await pilot.pause()
        await wait_for_rendered_rows(app, pilot)
        row = rendered_row(app, first)
        check(
            first.review_result is not None
            and first.review_result.live.get("headRefOid") == head_sha
            and "✗" in row
            and "DONE" in row
            and "cached" in row,
            "review events must merge receipt analysis with live evidence and repaint the verdict badge",
        )
        await pilot.press("r")
        await pilot.pause()
        check(
            len(fake_engine.calls) == 1,
            "repeated r must not dispatch the same selected heads twice",
        )
        first.review_result = receipt.analysis_result(live=first.live)
        app.review_event(
            tui.ReviewEvent(
                first.key,
                "failed",
                "provider failed",
                int(time.time()),
            )
        )
        await pilot.pause()
        await wait_for_rendered_rows(app, pilot)
        row = rendered_row(app, first)
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            first.review_result is None
            and first.review_failure == "provider failed"
            and "✗ FAILED" in row
            and "review failed" in status,
            "failed batch events must replace stale verdicts with a persistent failure",
        )
        first.head_sha = "c" * 40
        first.live["headRefOid"] = first.head_sha
        first.review_result = receipt.analysis_result(live=first.live)
        first.review_status = "complete"
        first.cached_age = "1m"
        app.review_event(
            tui.ReviewEvent(
                first.key,
                "cached",
                "late old-head result",
                int(time.time()),
                receipt_path.name,
            )
        )
        await pilot.pause()
        check(
            first.review_result is None
            and first.review_status == ""
            and first.cached_age == "",
            "a late event from a force-pushed head must not restore stale analysis",
        )
        first.review_result = tui.ReviewResult(1, "failed")
        first.review_status = "failed"
        first.cached_age = ""
        app.refresh_rows()
        await wait_for_rendered_rows(app, pilot)
        row = rendered_row(app, first)
        check(
            "✗ FAILED" in row,
            "failed or incomplete review results must carry the investigate badge",
        )
    app.review_cache.remove_if_matches(receipt)

    # Cache hits and immediate failures may callback before start() returns.
    class ImmediateEventEngine(FakeReviewEngine):
        def start(self, *args, **kwargs):
            snapshot = args[0]
            batch = super().start(*args, **kwargs)
            callback = kwargs["on_event"]
            callback(
                tui.ReviewEvent(
                    snapshot.items[0].key,
                    "failed",
                    "immediate provider failure",
                    int(time.time()),
                    batch_id=batch.batch_id,
                    head_sha=snapshot.items[0].head_sha,
                )
            )
            return batch

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        app.review_engine = ImmediateEventEngine()
        await pilot.press("B")
        await pilot.press("r")
        for _ in range(200):
            if app.review_batches:
                break
            await pilot.pause(0.05)
        check(
            app.stops[0].review_status == "failed"
            and app.stops[0].review_failure
            == "immediate provider failure",
            "events emitted before start returns must replay after batch registration",
        )

    # Snapshot hydration fails closed and leaves the maintainer's selection.
    os.environ["PR_VIEW_JSON"] = "{}"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        fake_engine = FakeReviewEngine()
        app.review_engine = fake_engine
        await pilot.press("B")
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(
            not fake_engine.calls,
            "a failed exact-head snapshot must not start the review engine",
        )
        check(
            all(stop.selected for stop in app.stops)
            and all(stop.failure for stop in app.stops),
            "snapshot failures must remain selected and visible on every failed row",
        )

    # A force-pushed head has a fresh triage identity and becomes unseen.
    os.environ["PR_VIEW_JSON"] = json.dumps(
        {
            "author": {"login": "someone-else"},
            "state": "OPEN",
            "baseRefOid": base_sha,
            "headRefOid": "c" * 40,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [],
        }
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    app.review_cache = tui.ReviewCache(workdir / "cache-fixture")
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        stop = app.stops[0]
        stop.head_sha = head_sha
        stop.live["headRefOid"] = head_sha
        stop.triage_state = "reviewed"
        app.triage[app.triage_key(stop)] = "reviewed"
        app.show_evidence(stop)
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(
            stop.head_identity == "c" * 40 and stop.triage_state == "unseen",
            "a force-push must reset the exact-head triage state to unseen",
        )
        stop.review_result = tui.ReviewResult(
            1,
            "complete",
            provenance={
                "backend": "goose",
                "model": "gemini-3.8-flash",
                "base_sha": base_sha,
                "head_sha": "c" * 40,
            },
        )
        stop.review_status = "complete"
        os.environ["PR_VIEW_JSON"] = json.dumps(
            {
                "author": {"login": "someone-else"},
                "state": "OPEN",
                "baseRefOid": "d" * 40,
                "headRefOid": "c" * 40,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "REVIEW_REQUIRED",
                "statusCheckRollup": [],
            }
        )
        app.show_evidence(stop)
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(
            stop.review_result is None and stop.review_status == "",
            "base motion must invalidate a verdict even when the head is unchanged",
        )

    # Older overlapping evidence reads cannot overwrite a newer head.
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        stop = app.stops[0]
        original_fetch_live_review = tui.fetch_live_review
        first_started = threading.Event()
        release_first = threading.Event()
        release_batch_fetch = threading.Event()
        fetch_count = 0
        fetch_lock = threading.Lock()

        def racing_fetch(repository, number):
            nonlocal fetch_count
            with fetch_lock:
                fetch_count += 1
                call = fetch_count
            live = {
                "author": {"login": "someone-else"},
                "state": "OPEN",
                "baseRefOid": base_sha,
                "headRefOid": ("b" if call == 1 else "c") * 40,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "REVIEW_REQUIRED",
                "statusCheckRollup": [],
            }
            if call == 1:
                first_started.set()
                release_first.wait(timeout=10)
            return live

        tui.fetch_live_review = racing_fetch
        try:
            app.show_evidence(stop)
            for _ in range(200):
                if first_started.is_set():
                    break
                await pilot.pause(0.05)
            app.show_evidence(stop)
            for _ in range(200):
                if stop.head_identity == "c" * 40:
                    break
                await pilot.pause(0.05)
            release_first.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            check(
                stop.head_identity == "c" * 40,
                "an older evidence response must not overwrite the newer head",
            )

            batch_fetch_started = threading.Event()
            def stale_during_batch(repository, number):
                batch_fetch_started.set()
                release_batch_fetch.wait(timeout=10)
                return {
                    "author": {"login": "someone-else"},
                    "state": "OPEN",
                    "baseRefOid": base_sha,
                    "headRefOid": "b" * 40,
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "REVIEW_REQUIRED",
                    "statusCheckRollup": [],
                }

            tui.fetch_live_review = stale_during_batch
            app.show_evidence(stop)
            for _ in range(200):
                if batch_fetch_started.is_set():
                    break
                await pilot.pause(0.05)
            fake_engine = FakeReviewEngine()
            app.review_engine = fake_engine
            item = tui.BatchReviewItem(
                stop.key,
                stop.repository,
                stop.number,
                stop.title,
                base_sha,
                "c" * 40,
                {
                    "baseRefOid": base_sha,
                    "headRefOid": "c" * 40,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                },
                [],
            )
            app.begin_review_batch(
                [stop], tui.BatchSnapshot((item,), {})
            )
            release_batch_fetch.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            check(
                stop.head_identity == "c" * 40
                and stop.review_status == "running",
                "an old evidence response must not overwrite a committed batch snapshot",
            )
        finally:
            release_first.set()
            release_batch_fetch.set()
            tui.fetch_live_review = original_fetch_live_review

    # Enter re-fetches live evidence before showing cached analysis.
    os.environ["PR_VIEW_JSON"] = json.dumps(
        {
            "author": {"login": "someone-else"},
            "state": "OPEN",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [
                {"name": "validate", "conclusion": "SUCCESS"}
            ],
        }
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        await settle_evidence(app, pilot)
        stop = app.stops[0]
        run = tui.ReviewRun(
            stop.repository,
            stop.number,
            base_sha,
            head_sha,
            base_sha[:12] + head_sha[:12],
            "goose",
            "gemini-3.8-flash",
            "max",
        )
        receipt = tui.ReviewReceipt.from_result(
            run,
            tui.ReviewResult(
                1,
                "findings",
                {"critical": 0, "high": 1, "medium": 0, "low": 0},
                [
                    {
                        "severity": "high",
                        "file": "image/tui/example.py",
                        "line": 7,
                        "title": "cached finding",
                    }
                ],
                [],
                {"backend": "goose", "model": "gemini-3.8-flash"},
            ),
            ["bounded transcript"],
            app.review_scope_version,
        )
        cache_path = app.review_cache.put(receipt)
        expected_run = tui.ReviewRun(
            stop.repository,
            stop.number,
            base_sha,
            head_sha,
            base_sha[:12] + head_sha[:12],
            tui.ACTIVE_BACKEND,
            *app.review_profile(stop.repository),
        )
        check(
            app.review_cache.get(expected_run, app.review_scope_version) is not None,
            "cache fixture must match the dashboard profile "
            f"(backend={tui.ACTIVE_BACKEND!r}, profile={app.review_profile(stop.repository)!r}, "
            f"stored={cache_path.name!r}, expected={app.review_cache.path_for(expected_run, app.review_scope_version).name!r})",
        )
        os.environ["PR_VIEW_JSON"] = json.dumps(
            {
                "author": {"login": "someone-else"},
                "state": "OPEN",
                "baseRefOid": base_sha,
                "headRefOid": head_sha,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "DIRTY",
                "reviewDecision": "REVIEW_REQUIRED",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "FAILURE"}
                ],
            }
        )
        stop.review_result = None
        app.show_evidence(stop)
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(
            stop.review_status == "cached"
            and stop.review_result is not None
            and stop.review_result.live.get("mergeStateStatus") == "DIRTY",
            "live-head cache lookup must annotate a row without rerunning review",
        )
        os.environ["PR_VIEW_JSON"] = json.dumps(
            {
                "author": {"login": "someone-else"},
                "state": "OPEN",
                "baseRefOid": base_sha,
                "headRefOid": head_sha,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "REVIEW_REQUIRED",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "SUCCESS"}
                ],
            }
        )
        await pilot.press("enter")
        for _ in range(200):
            if isinstance(app.screen, tui.ReviewDecisionScreen):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.ReviewDecisionScreen),
            "Enter on a reviewed row must open the cached decision card",
        )
        if isinstance(app.screen, tui.ReviewDecisionScreen):
            card = str(
                app.screen.query_one("#cached-decision-card", tui.Static).render()
            )
            check(
                "cached finding" in card
                and "live CI SUCCESS" in card
                and "merge CLEAN" in card,
                f"the decision card must merge cached analysis with fresh live evidence, got {card!r}",
            )
        check(
            '"live":{}' in cache_path.read_text()
            and "CLEAN" not in cache_path.read_text(),
            "fresh live evidence must never be written into the cached receipt",
        )
        await pilot.press("escape")
        await pilot.pause()
        stop.review_result = tui.ReviewResult(
            1,
            "findings",
            {"critical": 0, "high": 0, "medium": 1, "low": 0},
            [
                {
                    "severity": "medium",
                    "file": "image/tui/fresh.py",
                    "line": 9,
                    "title": "fresh collected finding",
                }
            ],
            [],
            {
                "backend": "goose",
                "model": "gemini-3.8-flash",
                "base_sha": base_sha,
                "head_sha": head_sha,
            },
        )
        stop.review_status = "findings"
        app.show_evidence(stop)
        await app.workers.wait_for_complete()
        await pilot.pause()
        check(
            stop.review_result.findings[0]["title"]
            == "fresh collected finding"
            and stop.review_result.live.get("ci") == "success",
            "fresh in-session analysis must outrank an older cache hit and receive current live evidence",
        )
    os.environ.pop("PR_VIEW_JSON", None)
    set_org_queue(SNAPSHOT["items"])

    async with live_app.run_test() as pilot:
        await wait_for_live_rows(live_app, pilot, "ready", 1)
        check(live_app.source_state == "ready", "live repository source should be ready")
        check([stop.key for stop in live_app.stops] == ["acme/widgets#42"],
              "the real app path excludes the authenticated maintainer's own work")
        check(live_app.stops[0].action == "review",
              "live PRs retain the existing review action semantics")
        live_file.write_text("[]")
        await pilot.press("R")
        for _ in range(100):
            if live_app.source_state == "empty":
                break
            await pilot.pause(0.05)
        check(live_app.source_state == "empty" and not live_app.stops,
              "refresh should reread the active live source and expose empty distinctly")

    # RED regressions for the independent review: identity failure must hold
    # the live queue, pagination must flatten every page, and malformed
    # elements must become a source error rather than raising.
    os.environ["GH_USER_FAIL"] = "1"
    auth_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
    async with auth_app.run_test() as pilot:
        for _ in range(100):
            if auth_app.source_state == "auth-failed":
                break
            await pilot.pause(0.05)
        check(auth_app.source_state == "auth-failed" and not auth_app.stops,
              "live queue must hold rows when viewer identity is unavailable")
    os.environ.pop("GH_USER_FAIL", None)
    async def wait_for_state(app, pilot, state: str) -> None:
        for _ in range(600):
            if app.source_state == state:
                return
            await pilot.pause(0.05)

    async def assert_live_state(error: str, state: str, detail: str) -> None:
        os.environ["LIVE_GH_ERROR"] = error
        app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
        async with app.run_test() as pilot:
            await wait_for_state(app, pilot, state)
            # The state flag lands before the status bar re-renders; wait on
            # the rendered condition, not a fixed beat after the flag (#284).
            for _ in range(600):
                status = str(app.query_one("#status-bar").render())
                if detail in app.source_message and detail in status:
                    break
                await pilot.pause(0.05)
            status = str(app.query_one("#status-bar").render())
            check(app.source_state == state and not app.stops,
                  f"real app path must hold rows for {state} source state")
            check(detail in app.source_message and detail in status,
                  f"real app path must expose actionable {state} detail")
            check("\\n" not in app.source_message and "\\x1b" not in app.source_message
                  and len(app.source_message) <= 240,
                  f"{state} detail must be bounded and sanitized")
        os.environ.pop("LIVE_GH_ERROR", None)

    await assert_live_state("HTTP 403: Resource not accessible", "inaccessible", "Resource not accessible")
    await assert_live_state("HTTP 404: Not Found", "missing", "Not Found")
    await assert_live_state("network timeout", "error", "network timeout")
    await assert_live_state("authentication required", "inaccessible", "authentication required")

    os.environ.pop("LIVE_GH_ERROR", None)
    os.environ.pop("LIVE_PAGES", None)
    malformed_repo = tui.ReviewDashboard(tui.QueueFilters(live_repository="not-a-repo"))
    async with malformed_repo.run_test() as pilot:
        await wait_for_state(malformed_repo, pilot, "malformed")
        for _ in range(600):
            status = str(malformed_repo.query_one("#status-bar").render())
            if "use owner/repo" in status:
                break
            await pilot.pause(0.05)
        status = str(malformed_repo.query_one("#status-bar").render())
        check(not malformed_repo.stops and "use owner/repo" in status,
              "real app path must report malformed repositories")

    os.environ["LIVE_PAGES"] = "1"
    live_file.write_text("{}")
    malformed_page_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
    async with malformed_page_app.run_test() as pilot:
        await wait_for_state(malformed_page_app, pilot, "malformed")
        check(not malformed_page_app.stops and "malformed GitHub response" in malformed_page_app.source_message,
              "real app path must report malformed JSON/pages")

    live_file.write_text(json.dumps([[{"number": 44}], ["not an object"]]))
    malformed_element_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
    async with malformed_element_app.run_test() as pilot:
        await wait_for_state(malformed_element_app, pilot, "malformed")
        check(not malformed_element_app.stops and "malformed GitHub response" in malformed_element_app.source_message,
              "real app path must report malformed elements")

    async def assert_malformed_pull(pull: dict, detail: str) -> None:
        live_file.write_text(json.dumps([[pull]]))
        app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
        async with app.run_test() as pilot:
            await wait_for_state(app, pilot, "malformed")
            for _ in range(100):
                status = str(app.query_one("#status-bar").render())
                if detail in app.source_message and detail in status:
                    break
                await pilot.pause(0.05)
            status = str(app.query_one("#status-bar").render())
            check(app.source_state == "malformed" and not app.stops,
                  f"invalid {detail} must produce no rows through the real app")
            check(detail in app.source_message and detail in status,
                  f"invalid {detail} must expose one actionable malformed detail")

    await assert_malformed_pull(
        {"number": 44, "title": "hostile author", "user": "not-an-object"},
        "malformed GitHub response",
    )
    await assert_malformed_pull(
        {"title": "missing number", "user": None},
        "number",
    )
    await assert_malformed_pull(
        {"number": True, "title": "boolean number", "user": None},
        "number",
    )
    await assert_malformed_pull(
        {"number": 45, "title": "invalid login", "user": {"login": 7}},
        "login",
    )

    os.environ.pop("LIVE_PAGES", None)
    live_file.write_text(json.dumps([[{"number": 44, "title": "page one", "user": {"login": "other"}}], [{"number": 45, "title": "page two", "user": {"login": "other"}}]]))
    os.environ["LIVE_PAGES"] = "1"
    paged = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets")).load_live_queue("acme/widgets")
    check(len(paged["items"]) == 2, "live pagination must flatten every returned page")
    os.environ.pop("LIVE_PAGES", None)
    live_file.write_text(json.dumps([
        [{"number": n, "title": f"PR {n}", "user": {"login": "other"}} for n in range(1, 102)],
        [{"number": n, "title": f"PR {n}", "user": {"login": "other"}} for n in range(102, 203)],
    ]))
    os.environ["LIVE_PAGES"] = "1"
    large_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
    check(
        large_app.cluster(tui.Stop("acme/widgets", 1, "review", "PR 1", "other"))
        == ([], []),
        "paginated live fixtures must remain valid for async overlap evidence",
    )
    async with large_app.run_test() as pilot:
        await wait_for_live_rows(large_app, pilot, "ready", 202)
        check(len(large_app.stops) == 202,
              "live queue must flatten multiple pages beyond 200 pull requests")
    os.environ.pop("LIVE_PAGES", None)
    live_file.write_text(json.dumps([]))

    # Semantic navigation contract: bindings, help, and the palette must be
    # projections of one registry rather than independent key lists.
    registry = tui.COMMANDS
    ids = {command.id for command in registry}
    check(
        {"navigate_down", "navigate_up", "navigate_first", "navigate_last",
         "navigate_page_down", "navigate_page_up", "pane_next", "pane_previous",
         "activate", "back", "quit", "steer", "review", "copy_review_context",
         "open_command_palette", "help"} <= ids,
        "navigation and current review commands must be semantic registry entries",
    )
    back_commands = [command for command in registry if command.action == "back"]
    check(
        {command.key for command in back_commands} == {"escape", "q"},
        "Escape and q must project the same semantic back action",
    )
    check(
        {command.key for command in registry if command.action == "quit"} == {"ctrl+c", "ctrl+q"},
        "Ctrl-C and Ctrl-q must be global quit bindings",
    )
    palette_commands = [command for command in registry if command.id.startswith("open_command_palette")]
    check(
        {command.key for command in palette_commands} == {"ctrl+p", ":"},
        "Ctrl-p and : must project the real command palette action",
    )
    check(
        tui.ReviewDashboard.BINDINGS == tui.bindings_for(tui.ReviewDashboard),
        "dashboard bindings must be generated from the semantic registry",
    )
    back_projection = {
        tui.BatchPlanScreen: "dismiss(False)",
        tui.SlayConfirmScreen: "dismiss(False)",
        tui.LandingScreen: "dismiss(None)",
        tui.DiffScreen: "dismiss",
        tui.CommentsScreen: "dismiss",
        tui.ReviewScreen: "close",
        tui.ReviewVerdict: "dismiss(None)",
        tui.MergeRecovery: "dismiss(None)",
        tui.HarnessTakeoff: "dismiss(None)",
    }
    for screen_type, action in back_projection.items():
        projected = tui.back_bindings(action)
        back_keys = {binding.key for binding in projected}
        check(
            [binding for binding in screen_type.BINDINGS if binding.key in back_keys]
            == projected,
            f"{screen_type.__name__} back keys must project from COMMANDS",
        )

    # ── the default view hides nothing ───────────────────────────────────
    # The regression this pins: the dashboard defaulted to the 'review'
    # action, so a 121-pull-request queue rendered as five stops and the
    # merge-ready work was invisible. Default is now the whole queue, ordered
    # so what a maintainer can act on comes first.
    for key, label in (("q", "q"), ("ctrl+c", "Ctrl-C")):
        quit_app = tui.ReviewDashboard(tui.QueueFilters())
        async with quit_app.run_test() as quit_pilot:
            await quit_pilot.pause()
            await quit_pilot.press(key)
            await quit_pilot.pause()
            check(quit_app._exit, f"{label} must exit the root dashboard")

    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        keys = [stop.key for stop in app.stops]
        check(
            keys == ["projectbluefin/bluefinctl#31", "projectbluefin/common#7"],
            f"the default view must show every action, got {keys}",
        )
        check(
            tui.QueueFilters().action == "",
            "the default action filter must be empty (every action)",
        )
        check(
            tui.action_rank("ready-for-human-merge") < tui.action_rank("review")
            < tui.action_rank("fix-ci")
            < tui.action_rank("investigate"),
            "merge-ready and reviewable work must sort above stuck work",
        )
        # [f] narrows to one action at a time and comes back to everything.
        await pilot.press("f")
        await pilot.pause()
        check(
            app.filters.action == "review"
            and [s.key for s in app.stops] == ["projectbluefin/bluefinctl#31"],
            f"[f] must narrow to one action, got {app.filters.action!r} "
            f"{[s.key for s in app.stops]}",
        )
        for _ in range(6):
            if app.filters.action == "":
                break
            await pilot.press("f")
            await pilot.pause()
        check(
            app.filters.action == "" and len(app.stops) == 2,
            "[f] must cycle back to every action",
        )
        await pilot.press("g")
        check(app.query_one("#queue").index == 0, "g must select the first queue item")
        await pilot.press("j")
        check(app.query_one("#queue").index == 1, "j must move to the next queue item")
        await pilot.press("k")
        check(app.query_one("#queue").index == 0, "k must move to the previous queue item")
        await pilot.press("G")
        check(app.query_one("#queue").index == 1, "G must select the last queue item")
        await pilot.press("ctrl+u")
        check(app.query_one("#queue").index == 0, "Ctrl-u must page upward")
        await pilot.press("ctrl+d")
        check(app.query_one("#queue").index == 1, "Ctrl-d must page downward")

    # ── an explicit action filter still narrows ──────────────────────────
    app = tui.ReviewDashboard(
        tui.QueueFilters(action="review")
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        keys = [stop.key for stop in app.stops]
        check(
            keys == ["projectbluefin/bluefinctl#31"],
            f"action filter + own-work filter should leave one stop, got {keys}",
        )

    # --all keeps every action; own work stays filtered out regardless.
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        check(len(app.stops) == 2, f"--all should keep both other-authored stops, got {len(app.stops)}")
        check(
            all(stop.author != "castrojo" for stop in app.stops),
            "own work must never appear in the queue",
        )

    # --repo narrows to one repository.
    app = tui.ReviewDashboard(
        tui.QueueFilters(action="", repository="common")
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        check(
            [s.key for s in app.stops] == ["projectbluefin/common#7"],
            "--repo should accept a short repository name",
        )

    # ── every binding resolves to a real action ──────────────────────────
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for binding in tui.ReviewDashboard.BINDINGS:
            name = f"action_{binding.action.split('(')[0]}"
            check(
                hasattr(app, name) or hasattr(tui.App, name),
                f"binding {binding.key!r} points at missing {name}",
            )
        binding_keys = {binding.key for binding in tui.ReviewDashboard.BINDINGS}
        check(
            "l" not in binding_keys,
            f"terminal-dispatched pane navigation must not collide with a binding, got {sorted(binding_keys)}",
        )
        review = [b for b in tui.ReviewDashboard.BINDINGS if b.action == "review"]
        check(len(review) == 1, f"exactly one binding must run a review, got {len(review)}")
        check(
            bool(review) and review[0].key == "r",
            f"review must be on 'r', got {[b.key for b in review]}",
        )
        check(
            "[b]l[/b]" not in tui.KEYS_ACTING,
            f"the acting key line must not advertise label actions, got {tui.KEYS_ACTING!r}",
        )
        root_screen = app.screen
        await pilot.press("ctrl+p")
        await pilot.pause()
        check(type(app.screen).__name__ == "CommandPalette", "Ctrl-p must open Textual's command palette")
        await pilot.press("escape")
        await pilot.press(":")
        await pilot.pause()
        check(type(app.screen).__name__ == "CommandPalette", ": must open Textual's command palette")
        await pilot.press("escape")
        app.push_screen(tui.ReviewVerdict())
        await pilot.pause()
        modal = app.screen
        check(isinstance(modal, tui.ReviewVerdict), "q acceptance must start on ReviewVerdict")
        await pilot.press("q")
        await pilot.pause()
        check(app.screen is root_screen and app.screen is not modal, "q must close a pushed screen")

        app.push_screen(tui.DiffScreen(tui.Stop("projectbluefin/review", 165, "review", "review")))
        await pilot.pause()
        check(isinstance(app.screen, tui.DiffScreen), "q acceptance must activate DiffScreen")
        await pilot.press("q")
        await pilot.pause()
        check(app.screen is root_screen, "q must close DiffScreen")

        app.push_screen(tui.CommentsScreen(tui.Stop("projectbluefin/review", 165, "review", "review")))
        await pilot.pause()
        check(isinstance(app.screen, tui.CommentsScreen), "q acceptance must activate CommentsScreen")
        await pilot.press("q")
        await pilot.pause()
        check(app.screen is root_screen, "q must close CommentsScreen")

        harness_option = SimpleNamespace(
            harness=SimpleNamespace(branding=SimpleNamespace(
                harness_id="test", terminal_badge="TT", display_name="Test",
            )),
            discovery=SimpleNamespace(availability=SimpleNamespace(value="ready"),
                                      model="test", reasoning="low"),
            status="ready",
        )
        for screen in (
            tui.MergeRecovery(tui.Stop("projectbluefin/review", 165, "review", "review"), "BEHIND"),
            tui.HarnessTakeoff([harness_option], harness_option),
        ):
            app.push_screen(screen)
            await pilot.pause()
            active = app.screen
            check(app.screen is screen, f"q acceptance must positively activate {type(screen).__name__}")
            await pilot.press("q")
            await pilot.pause()
            check(app.screen is not active, f"q must dismiss {type(screen).__name__}")

        app.push_screen(tui.ConfirmMutation([["gh", "pr", "merge"]], "165"))
        await pilot.pause()
        check(isinstance(app.screen, tui.ConfirmMutation), "q acceptance must activate ConfirmMutation")
        await pilot.press("q")
        await pilot.pause()
        check(app.screen.query_one(tui.Input).value == "q", "q must type in the confirmation input")
        await pilot.press("ctrl+a")
        await pilot.press("1", "6", "5")
        await pilot.press("enter")
        await pilot.pause()
        check(app.screen is root_screen, "typed PR-number confirmation must remain functional")

        app.push_screen(tui.ReviewBody(app.stops[0], "comment"))
        await pilot.pause()
        await pilot.press("q")
        check(app.screen.query_one(tui.TextArea).text == "q", "q must type in the review body editor")
        await pilot.press("escape")
        await pilot.pause()
        check(app.screen is root_screen, "Escape must close ReviewBody")

        app.action_comment()
        await pilot.pause()
        check(type(app.screen).__name__ == "CommentBody", "q acceptance must activate CommentBody")
        await pilot.press("q")
        check(app.screen.query_one(tui.Input).value == "q", "q must type in the comment input")
        await pilot.press("escape")
        await pilot.pause()
        app.action_comment()
        await pilot.pause()
        app.screen.query_one(tui.Input).value = "keyboard comment"
        await pilot.press("ctrl+s")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.CommentPreview),
            "Ctrl-s from the focused comment editor must preview the exact payload",
        )
        check("keyboard comment" in app.screen.body, "comment preview must show verbatim Markdown")
        await pilot.click("#comment-preview-submit")
        await pilot.pause()
        check(isinstance(app.screen, tui.ConfirmMutation), "comment preview submit must reach the existing gate")
        await pilot.press("escape")
        await pilot.pause()
        app.action_comment()
        await pilot.pause()
        app.screen.query_one(tui.Input).value = "button comment"
        await pilot.click("#comment-submit")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.CommentPreview),
            "comment submit button must preview the exact payload",
        )
        check("button comment" in app.screen.body, "button comment preview must preserve body")
        await pilot.click("#comment-preview-submit")
        await pilot.pause()
        check(isinstance(app.screen, tui.ConfirmMutation), "comment preview button must reach the gate")
        check(
            "pr comment" not in gh_log.read_text(),
            "comment controls must not mutate before confirmation",
        )
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("/")
        await pilot.press("q")
        await pilot.pause()
        check(app.query_one("#steer", tui.Input).value == "q", "q must remain typed editor input")
        check(
            any(binding.key == "ctrl+q" and binding.action == "quit"
                for binding in tui.ReviewDashboard.BINDINGS),
            "Ctrl-q must remain the quit binding",
        )

    async def run_review(
        exit_code: int,
        output: str,
        *,
        head_sha: str = "0123456789abcdef0123456789abcdef01234567",
        reviewed_head: str = "",
        prior_result: tui.ReviewResult | None = None,
        compare_json: str | None = None,
        compare_fail: bool = False,
    ):
        review_stub(exit_code, output)
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            await settle_evidence(app, pilot)
            app.stops[0].live = {
                "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
                "headRefOid": head_sha,
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "SUCCESS"},
                    {"name": "docs", "conclusion": "FAILURE"},
                ],
            }
            app.stops[0].head_sha = head_sha
            app.stops[0].overlap = {"duplicates": [44], "overlaps": [45, 46]}
            app.stops[0].review_result = prior_result
            root_screen = app.screen
            original_adapter = tui.adapt_current_engine
            if compare_json is not None:
                os.environ["RE_REVIEW_COMPARE_JSON"] = compare_json
            else:
                os.environ.pop("RE_REVIEW_COMPARE_JSON", None)
            if compare_fail:
                os.environ["RE_REVIEW_COMPARE_FAIL"] = "1"
            else:
                os.environ.pop("RE_REVIEW_COMPARE_FAIL", None)
            if reviewed_head:
                def stale_adapter(*args, **kwargs):
                    result = original_adapter(*args, **kwargs)
                    result.provenance["head_sha"] = reviewed_head
                    return result
                tui.adapt_current_engine = stale_adapter
            await pilot.press("r")
            await pilot.pause()
            screen = app.screen
            if not isinstance(screen, tui.ReviewScreen):
                check(False, f"'r' must open the review screen, got {type(screen).__name__}")
                return "", set()
            check(
                screen.headroom_session is app.headroom_session,
                "single reviews must reuse the dashboard Headroom session",
            )
            for _ in range(400):
                if screen.finished:
                    break
                await pilot.pause(0.05)
            check(screen.finished, f"review screen never finished (exit {exit_code})")
            status = screen.query_one("#review-status", tui.Static)
            card = screen.query_one("#review-card", tui.Static)
            evidence = screen.query_one("#review-evidence", tui.Static)
            raw = screen.query_one("#review-log", tui.RichLog)
            check("hidden" in evidence.classes, "completed decision evidence must start collapsed")
            check("hidden" in raw.classes, "completed raw transcript must start collapsed")
            await pilot.press("e")
            await pilot.pause()
            check(
                "hidden" not in evidence.classes
                and "REVIEW EVIDENCE" in str(evidence.render())
                and "raw backend transcript" in str(evidence.render()),
                "[e] must reveal bounded decision evidence and name raw transcript as secondary",
            )
            await pilot.press("r")
            await pilot.pause()
            check("hidden" not in raw.classes, "[r] must reveal the secondary raw transcript")
            await pilot.press("c")
            await pilot.pause()
            check(isinstance(app.screen, tui.CommentsScreen), "[c] on ReviewScreen must open CommentsScreen")
            await pilot.press("escape")
            await pilot.pause()
            check(isinstance(app.screen, tui.ReviewScreen), "escape on CommentsScreen must return to ReviewScreen")
            check("[c] comments" in str(card.render()), "decision card must advertise comments viewer")
            check("[v] diff" in str(card.render()), "decision card must advertise diff viewer")
            await pilot.press("q")
            await pilot.pause()
            tui.adapt_current_engine = original_adapter
            os.environ.pop("RE_REVIEW_COMPARE_JSON", None)
            os.environ.pop("RE_REVIEW_COMPARE_FAIL", None)
            check(app.screen is root_screen, "q must close ReviewScreen")
            if (
                "COMPLETE" in str(status.render())
                and "INCOMPLETE" not in str(status.render())
            ):
                check(
                    app.stops[0].triage_state == "reviewed"
                    and app.triage[app.triage_key(app.stops[0])]
                    == "reviewed",
                    "a completed single review must triage its exact head",
                )
            return str(status.render()), set(status.classes), str(card.render())

    # The stubs below keep writing raw JSONL on purpose: the dashboard must
    # still survive the malformed or partial records the CLI refuses.
    landing_py = TUI_DIR / "landing.py"
    landing_log = workdir / "landing-argv.log"
    landing_stub = write_stub(
        workdir / "stub-landing",
        f'printf "%s\\n" "$*" >>"{landing_log}"\n'
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'prs=$(grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u)\n'
        'for pr in $prs; do\n'
        f'  "{sys.executable}" "{landing_py}" report --status "$status" '
        'event --pr "$pr" --state merged --note green\n'
        'done\n'
        f'"{sys.executable}" "{landing_py}" report --status "$status" '
        'done --expect $prs --note "all landed"\n'
        'echo "agent log line"\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{landing_stub} @PROMPT"
    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "review-queue-pilot"
    os.environ["BLUEFIN_REVIEW_PARTITION_BATCH"] = "0"

    # ── a selected batch dispatches one landing agent behind one gate ────
    # The selection is the review, so the batch gate is proportionate: the
    # whole plan and the exact command on one screen, Enter to dispatch —
    # not a typed count. One agent owns all selected pull requests, reports
    # per-PR state to its status file, and the queue screen polls that file.
    # This stub reports through the module's report CLI exactly as the brief
    # instructs, so the happy path proves the shipped reporter end to end.
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        await pilot.press("b")
        await pilot.press("down")
        await pilot.press("b")
        await pilot.pause()
        check(
            [s.selected for s in app.stops] == [True, True],
            f"[b] must select the highlighted row, got {[s.selected for s in app.stops]}",
        )
        await pilot.press("a")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.BatchPlanScreen),
            "[a] must never open the batch gate — [A] is the only batch key",
        )
        await pilot.press("A")
        await pilot.pause()
        gate = app.screen
        check(
            isinstance(gate, tui.BatchPlanScreen),
            f"[A] on a selection must open the plan gate, got {type(gate).__name__}",
        )
        if isinstance(gate, tui.BatchPlanScreen):
            check(
                len(gate.plan.stops) == 2,
                f"the plan must cover the whole selection, got {gate.plan.keys}",
            )
            await pilot.press("enter")
            await pilot.pause()
            check(
                not isinstance(app.screen, tui.LandingScreen)
                and not isinstance(app.screen, tui.BatchPlanScreen),
                f"dispatch must return to review queue, got {type(app.screen).__name__}",
            )
            task = gate.plan
            for _ in range(400):
                if task.returncode is not None:
                    break
                await pilot.pause(0.05)
            check(
                task.returncode == 0,
                f"the landing agent must exit 0, got {task.returncode}",
            )
            # One agent lands the whole batch — never one per pull request —
            # and the final review-and-fix rounds (#378) follow it in the
            # same lane.
            for _ in range(400):
                if len(landing_log.read_text().splitlines()) > 1:
                    break
                await pilot.pause(0.05)
            invocations = landing_log.read_text().splitlines()
            check(
                len(invocations) == 2
                and invocations[0].endswith(".prompt.md")
                and "final-" not in invocations[0]
                and "final-1-final-review.prompt.md" in invocations[1],
                f"one landing agent then exactly one final review round, got "
                f"{invocations}",
            )
            rounds = [t for t in app.landing_queue if t.phase]
            check(
                [t.phase for t in rounds] == ["final-review"]
                and rounds[0].status_path == task.status_path,
                f"the round must run in the same lane and record, got "
                f"{[(t.phase, t.status_path) for t in rounds]}",
            )
            prompt_text = Path(task.prompt_path).read_text()
            check(
                task.task_id.endswith("-review-queue-pilot"),
                f"the batch id must name its instance, got {task.task_id!r}",
            )
            check(
                all(stop.key in prompt_text for stop in task.stops),
                "the agent brief must name every selected pull request",
            )
            check(
                ":stable" in prompt_text and "awaiting-stable" in prompt_text,
                "the brief must define done as the change published on :stable",
            )
            for _ in range(200):
                if not any(s.selected for s in app.stops):
                    break
                await pilot.pause(0.05)
            check(
                not any(s.selected for s in app.stops),
                "landed pull requests must leave the batch",
            )
            status = str(app.query_one("#status-bar", tui.Static).render())
            check(
                "agents:" in status,
                f"the status bar must report the batch queue, got {status!r}",
            )
            await pilot.press("w")
            await pilot.pause()
            check(
                app.query_one("#landing-pause", tui.Button).has_focus,
                "[w] must focus the persistent live batch queue",
            )
            rows = str(
                app.query_one("#landing-control-status", tui.Static).render()
            )
            check(
                rows.count("merged") == 2,
                f"the persistent queue must show each landed PR, got {rows!r}",
            )
    del os.environ["BLUEFIN_REVIEW_INSTANCE"]

    # ── landing batches share repository lanes, not one global FIFO ─────
    # Independent repositories may use the two safe worker slots together;
    # batches touching the same repository must wait for the first process.
    concurrency_script = workdir / "blocking-landing.py"
    concurrency_script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "\n"
        "started, release = map(Path, sys.argv[1:])\n"
        "started.write_text('started\\n')\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
    )

    def blocking_task(
        repository: str,
        number: int,
        started: Path,
        release: Path,
    ):
        task = tui.landing.new_task(
            [tui.Stop(repository, number, "review", f"PR {number}")],
            "tester",
        )
        task.command = [
            sys.executable,
            str(concurrency_script),
            str(started),
            str(release),
        ]
        return task

    async def wait_until(predicate, pilot, rounds: int = 400) -> bool:
        for _ in range(rounds):
            if predicate():
                return True
            await pilot.pause(0.01)
        return predicate()

    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "pilot-concurrency"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        paused_started = workdir / "paused.started"
        paused_release = workdir / "paused.release"
        paused_task = blocking_task(
            "acme/paused", 1, paused_started, paused_release
        )
        app.landing_queue.append(paused_task)
        app.refresh_status()
        app.drain_landings()
        await pilot.pause(0.1)
        check(
            app.landing_paused and not paused_started.exists(),
            "pausing must hold pending landing work without starting its agent",
        )
        control_rows = str(
            app.query_one("#landing-control-status", tui.Static).render()
        )
        check(
            "PAUSED · agents 0/6 · 1 queued" in control_rows,
            f"the persistent panel must show queue pause and counts, got {control_rows!r}",
        )
        await pilot.press("-")
        await pilot.press("+")
        check(
            app.landing_concurrency == 6,
            "the +/- controls must update the session concurrency limit",
        )
        await pilot.press("p")
        started = await wait_until(lambda: paused_task.running, pilot)
        check(started, "resuming must release pending landing work")
        paused_release.touch()
        finished = await wait_until(lambda: paused_task.returncode is not None, pilot)
        check(finished, "a resumed landing worker must finish cleanly")
        final_round = blocking_task(
            "acme/final", 2, workdir / "final.started", workdir / "final.release"
        )
        final_round.phase = "final-review"
        final_round.process = object()
        app.landing_queue.append(final_round)
        app.refresh_status()
        control_rows = str(
            app.query_one("#landing-control-status", tui.Static).render()
        )
        check(
            "agents 1/6" in control_rows,
            f"phase rounds must consume displayed landing capacity, got {control_rows!r}",
        )
        # A round rides the batch's own stops and status record; listing its
        # stops per-PR doubled every landed pull request while the round ran.
        landed_round = blocking_task(
            "acme/paused", 1, workdir / "landed-round.started", workdir / "landed-round.release"
        )
        landed_round.phase = "final-review"
        landed_round.round = 1
        landed_round.stops = list(paused_task.stops)
        landed_round.status_path = paused_task.status_path
        landed_round.process = object()
        app.landing_queue.append(landed_round)
        app.refresh_status()
        control_rows = str(
            app.query_one("#landing-control-status", tui.Static).render()
        )
        check(
            control_rows.count("acme/paused#1") == 1,
            f"a running round must not relist its batch's pull requests, got {control_rows!r}",
        )
        check(
            "final final-review round 1" in control_rows,
            f"a running round must appear as one batch-level line, got {control_rows!r}",
        )
        app.landing_queue.remove(landed_round)

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        started_a = workdir / "disjoint-a.started"
        started_b = workdir / "disjoint-b.started"
        release_a = workdir / "disjoint-a.release"
        release_b = workdir / "disjoint-b.release"
        task_a = blocking_task("acme/widgets", 1, started_a, release_a)
        task_b = blocking_task("acme/gadgets", 2, started_b, release_b)
        app.landing_queue.extend([task_a, task_b])
        app.drain_landings()
        disjoint_running = await wait_until(
            lambda: task_a.running and task_b.running,
            pilot,
        )
        check(
            disjoint_running,
            "batches on disjoint repositories must run concurrently",
        )
        check(
            task_a.process is not None and task_b.process is not None,
            "disjoint batches must each expose an active process",
        )
        release_a.touch()
        release_b.touch()
        both_finished = await wait_until(
            lambda: task_a.returncode is not None and task_b.returncode is not None,
            pilot,
        )
        check(both_finished, "disjoint landing workers must finish cleanly")

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        started_first = workdir / "same-first.started"
        started_second = workdir / "same-second.started"
        release_first = workdir / "same-first.release"
        release_second = workdir / "same-second.release"
        first = blocking_task("acme/widgets", 3, started_first, release_first)
        second = blocking_task("acme/widgets", 4, started_second, release_second)
        app.landing_queue.extend([first, second])
        app.drain_landings()
        first_running = await wait_until(lambda: first.running, pilot)
        check(first_running, "the first same-repository batch must start")
        check(
            not second.running
            and second.process is None
            and second.returncode is None
            and not started_second.exists(),
            "the second same-repository batch must wait for the first",
        )
        release_first.touch()
        second_running = await wait_until(
            lambda: first.returncode is not None and second.running,
            pilot,
        )
        check(
            second_running,
            "the second same-repository batch must start after the first finishes",
        )
        release_second.touch()
        both_finished = await wait_until(
            lambda: first.returncode is not None and second.returncode is not None,
            pilot,
        )
        check(both_finished, "serialized landing workers must finish cleanly")
    del os.environ["BLUEFIN_REVIEW_INSTANCE"]
    gh_log.write_text("")

    # ── the batch queue paints every state, and never by colour alone ────
    # Every state keeps its printed word and gains a shape-distinct glyph on
    # a styled span; terminal states sit on a muted fill and each batch
    # header is a filled state bar. A colourless read — the text alone —
    # must still carry every fact, and a bold-words-only implementation must
    # fail the background assertions.
    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "pilot-colours"
    colour_task = tui.landing.new_task(
        [
            tui.Stop("projectbluefin/bluefinctl", 31, "review", "one"),
            tui.Stop("projectbluefin/common", 7, "review", "two"),
            tui.Stop("projectbluefin/dakota", 12, "review", "three"),
            tui.Stop("projectbluefin/bluefin", 99, "review", "four"),
        ],
        "tester",
    )
    del os.environ["BLUEFIN_REVIEW_INSTANCE"]
    Path(colour_task.status_path).write_text(
        '{"pr": "projectbluefin/bluefinctl#31", "state": "merged", "note": "on :stable"}\n'
        '{"pr": "projectbluefin/common#7", "state": "failed", "note": "publish workflow red"}\n'
        '{"pr": "projectbluefin/dakota#12", "state": "awaiting-stable", "note": "watching the publish"}\n'
        '{"state": "done", "note": "two landed, one failed"}\n'
    )
    colour_task.process = object()  # a live handle: the header reads running
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        app.landing_queue.append(colour_task)
        await app.push_screen(tui.LandingScreen(app))
        await pilot.pause()
        screen = app.screen
        check(
            isinstance(screen, tui.LandingScreen),
            f"the fabricated batch must open the batch queue, got {type(screen).__name__}",
        )
        if isinstance(screen, tui.LandingScreen):
            screen.poll()
            rows_widget = screen.query_one("#landing-rows", tui.Static)
            rows = str(rows_widget.render())
            for expected in (
                f"batch {colour_task.task_id} — waiting",
                # A running batch names its heartbeat: the age of the last
                # report, so a stale wait is visible next to a healthy one
                # (#291).
                "last report",
                "✓ merged",
                "✗ failed",
                "◆ awaiting-stable",
                "? unreported",
                "✔ done",
                "publish workflow red",
                "two landed, one failed",
            ):
                check(
                    expected in rows,
                    f"the batch queue must show {expected!r}, got {rows!r}",
                )
            landing_status = str(
                screen.query_one("#landing-status", tui.Static).render()
            )
            check(
                "stage" in landing_status
                and "terminal" in landing_status
                and "elapsed" in landing_status
                and "ETA" not in landing_status,
                f"landing status must show observed progress without ETA, got {landing_status!r}",
            )

            def line_styles(fragment: str) -> list:
                """The segment styles of the rendered line holding fragment."""
                for y in range(rows_widget.region.height):
                    strip = rows_widget.render_line(y)
                    if fragment in "".join(segment.text for segment in strip):
                        return [segment.style for segment in strip]
                return []

            from rich.color import Color

            def theme_rgb(name: str):
                return Color.parse(app.theme_variables[name]).get_truecolor()

            base_rgb = theme_rgb("background")

            def fills(fragment: str) -> list:
                """Segment styles on fragment's line that sit on a real fill —
                not the screen's base background, which every segment carries."""
                return [
                    style
                    for style in line_styles(fragment)
                    if style is not None
                    and style.bgcolor is not None
                    and style.bgcolor.get_truecolor() != base_rgb
                ]

            header_fills = fills(f"batch {colour_task.task_id} — waiting")
            check(
                any(
                    style.bgcolor.get_truecolor() == theme_rgb("warning-muted")
                    for style in header_fills
                ),
                "the waiting batch header must be a filled bar, got "
                f"{header_fills!r}",
            )
            merged_fills = fills("✓ merged")
            check(
                any(
                    style.bgcolor.get_truecolor() == theme_rgb("success-muted")
                    and style.bold
                    for style in merged_fills
                ),
                "the merged state must be bold on a muted fill, got "
                f"{merged_fills!r}",
            )
            failed_fills = fills("✗ failed")
            check(
                any(
                    style.bgcolor.get_truecolor() == theme_rgb("error-muted")
                    and style.bold
                    for style in failed_fills
                ),
                "the failed state must be bold on a muted fill, got "
                f"{failed_fills!r}",
            )
        check(
            rows_widget.styles.border_top[0] == "round",
            "the batch list must carry a real border style, got "
            f"{rows_widget.styles.border_top!r}",
        )
        top_edge = "".join(
            segment.text
            for strip in rows_widget.render_lines(
                Region(0, 0, rows_widget.region.width, 1)
            )
            for segment in strip
        )
        check(
            top_edge.startswith("╭") and "BATCHES" in top_edge,
            f"the batch list must render a framed title edge, got {top_edge!r}",
        )
        await pilot.press("escape")
        await pilot.pause()
        app.refresh_status()
        control_rows = str(
            app.query_one("#landing-control-status", tui.Static).render()
        )
        for expected in (
            "LANDING QUEUE",
            "agents 1/6",
            "projectbluefin/bluefinctl#31 — merged · gemini-3.8-flash",
            "projectbluefin/common#7 — failed · gemini-3.8-flash",
            "projectbluefin/dakota#12 — awaiting-stable · gemini-3.8-flash",
            "projectbluefin/bluefin#99 — reviewing · gemini-3.8-flash",
        ):
            check(
                expected in control_rows,
                f"the persistent queue must show {expected!r}, got {control_rows!r}",
            )
    for artifact in (
        Path(colour_task.prompt_path),
        Path(colour_task.status_path),
    ):
        artifact.unlink(missing_ok=True)
    gh_log.write_text("")

    # ── landing log and stop actions share one explicit batch target ───────
    first_status = workdir / "first.jsonl"
    second_status = workdir / "second.jsonl"
    first_log = workdir / "first.log"
    second_log = workdir / "second.log"
    first_status.write_text("{\"pr\":\"projectbluefin/review#1\",\"state\":\"fixing\"}\n")
    second_status.write_text("{\"pr\":\"projectbluefin/review#2\",\"state\":\"fixing\"}\n")
    first_log.write_text("first batch log\n")
    second_log.write_text("second batch log\n")
    first_task = tui.landing.LandingTask(
        task_id="first-batch",
        stops=[tui.Stop("projectbluefin/review", 1, "review", "first")],
        login="tester",
        status_path=str(first_status),
        log_path=str(first_log),
        process=SimpleNamespace(pid=101),
    )
    second_task = tui.landing.LandingTask(
        task_id="second-batch",
        stops=[tui.Stop("projectbluefin/review", 2, "review", "second")],
        login="tester",
        status_path=str(second_status),
        log_path=str(second_log),
        process=SimpleNamespace(pid=202),
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        app.landing_queue.extend([first_task, second_task])
        await app.push_screen(tui.LandingScreen(app))
        await pilot.pause()
        screen = app.screen
        if isinstance(screen, tui.LandingScreen):
            screen.select_task(first_task.task_id)
            screen.poll()
            log_widget = screen.query_one("#landing-log", tui.RichLog)
            log = "\n".join(
                "".join(segment.text for segment in strip)
                for strip in log_widget.lines
            )
            check("first batch log" in log and "second batch log" not in log,
                  f"selected batch log must be displayed, got {log!r}")
            with mock.patch.object(tui.os, "getpgid", return_value=101) as getpgid, \
                    mock.patch.object(tui.os, "killpg") as killpg:
                screen.action_stop_agent()
            check(getpgid.call_args.args == (101,), "stop must target the displayed batch")
            check(killpg.call_args.args == (101, tui.signal.SIGTERM),
                  "stop must terminate the displayed batch process group")
            check(first_task.stop_requested and not second_task.stop_requested,
                  "stopping one batch must not target another batch")
    for artifact in (first_status, second_status, first_log, second_log):
        artifact.unlink(missing_ok=True)
    gh_log.write_text("")

    # ── a hostile agent-reported state cannot break the batch queue ──────
    # The state string is agent-sourced JSONL. An unknown state must render
    # literally, escaped like every other agent string — never parsed as
    # markup. Unescaped, "waiting[/][blink]OWNED" raises MarkupError in
    # rows.update() and takes the whole screen down from inside poll().
    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "pilot-hostile"
    hostile_task = tui.landing.new_task(
        [tui.Stop("projectbluefin/bluefin", 42, "review", "hostile")],
        "tester",
    )
    del os.environ["BLUEFIN_REVIEW_INSTANCE"]
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        app.landing_queue.append(hostile_task)
        await app.push_screen(tui.LandingScreen(app))
        await pilot.pause()
        # The state arrives after mount, the way the agent's report does.
        Path(hostile_task.status_path).write_text(
            '{"pr": "projectbluefin/bluefin#42", "state": "waiting[/][blink]OWNED"}\n'
        )
        screen = app.screen
        poll_error = None
        if isinstance(screen, tui.LandingScreen):
            try:
                screen.poll()
            except Exception as error:  # the check below reports it
                poll_error = error
        check(
            poll_error is None,
            "an unknown agent-reported state must not take the batch queue "
            f"down, got {poll_error!r}",
        )
        if poll_error is None:
            rows = str(screen.query_one("#landing-rows", tui.Static).render())
            check(
                "? waiting[/][blink]OWNED" in rows,
                f"an unknown state must render literally, got {rows!r}",
            )
    for artifact in (
        Path(hostile_task.prompt_path),
        Path(hostile_task.status_path),
    ):
        artifact.unlink(missing_ok=True)
    gh_log.write_text("")

    # ── bulk a ActionPlan and exact-list drift gate ───────────────────────
    pr7_view_count = workdir / "pr7_view_count"
    pr7_view_count.unlink(missing_ok=True)
    head_a = "a" * 40
    head_b = "b" * 40
    head_c = "c" * 40
    head_d = "d" * 40
    head_e = "e" * 40
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then\n'
        '  if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi\n'
        '  case "$3" in\n'
        f'    31) printf \'{{"headRefOid":"{head_a}","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,"statusCheckRollup":[]}}\\n\'; exit 0 ;;\n'
        f'    7) if [ -f "{pr7_view_count}" ]; then\n'
        f'         printf \'{{"headRefOid":"{head_c}","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,"statusCheckRollup":[]}}\\n\';\n'
        f'       else\n'
        f'         touch "{pr7_view_count}";\n'
        f'         printf \'{{"headRefOid":"{head_b}","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,"statusCheckRollup":[]}}\\n\';\n'
        f'       fi;\n'
        '       exit 0 ;;\n'
        f'    99) printf \'{{"headRefOid":"{head_d}","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":true,"statusCheckRollup":[]}}\\n\'; exit 0 ;;\n'
        f'    100) printf \'{{"headRefOid":"{head_e}","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,"statusCheckRollup":[]}}\\n\'; exit 0 ;;\n'
        '    *) echo "{}"; exit 0 ;;\n'
        '  esac\n'
        'fi\n'
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        "exit 0\n",
    )
    async with tui.ReviewDashboard(tui.QueueFilters(action="")).run_test() as pilot:
        app = pilot.app
        await wait_for_live_rows(app, pilot, "ready", 2)
        app.self_login = "castrojo"
        for stop in app.stops:
            stop.selected = True
        await pilot.press("a")
        for _ in range(50):
            if isinstance(app.screen, tui.BatchMutationConfirmation):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.BatchMutationConfirmation),
            "bulk a must open the exact-list ActionPlan gate",
        )
        gate = app.screen
        if isinstance(gate, tui.BatchMutationConfirmation):
            expected = " ".join(
                f"{item.repository}#{item.number}@{item.head_sha}"
                for item in gate.preview_record.items
            )
            await pilot.click("#batch-confirmation")
            await pilot.press(*expected)
            await pilot.press("enter")
            for _ in range(50):
                if getattr(app, "batch_action_receipt", None) is not None:
                    break
                await pilot.pause(0.05)
            check(
                app.batch_action_receipt.rejected == {7: "head drift invalidates the item"},
                "a changed live head must reject only that item",
            )
            check(
                app.batch_action_receipt.succeeded == {31: 1},
                "an unchanged item must still execute after another item drifts",
            )
            check(
                app.batch_mutation_in_flight is False,
                "in-flight flag must reset to False after batch execution finishes",
            )

        # In-flight guard: prevent duplicate concurrent executions
        app.batch_mutation_in_flight = True
        app.batch_queue_automerge(app.stops)
        check(
            not isinstance(app.screen, tui.BatchMutationConfirmation),
            "in-flight guard must prevent starting batch mutation when one is already in flight",
        )
        app._on_batch_execution_error("plan expired", app.stops, getattr(gate, "preview_record", None))
        check(
            app.batch_mutation_in_flight is False,
            "execution error handler must reset batch_mutation_in_flight to False",
        )

        # Cross-repository collision with mixed outcomes: repo-a#31 succeeds and repo-b#31 drifts
        stop_a = tui.Stop("projectbluefin/repo-a", 31, "review", "repo a 31")
        stop_b = tui.Stop("projectbluefin/repo-b", 31, "review", "repo b 31")
        item_a = tui.action_plan.BatchMutationItem(
            repository="projectbluefin/repo-a",
            pull_request=31,
            head_sha="a" * 40,
            prerequisites=tui.action_plan.Prerequisites.from_mappings(
                permissions={"self_login": "castrojo"}, checks={"ci": "success"}
            ),
            operations=(("python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/31"),),
        )
        item_b = tui.action_plan.BatchMutationItem(
            repository="projectbluefin/repo-b",
            pull_request=31,
            head_sha="b" * 40,
            prerequisites=tui.action_plan.Prerequisites.from_mappings(
                permissions={"self_login": "castrojo"}, checks={"ci": "success"}
            ),
            operations=(("python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/31"),),
        )
        dummy_plan = tui.action_plan.BatchActionPlan.build(
            actor="castrojo",
            tenant="projectbluefin",
            action_kind="approve-and-queue",
            items=(item_a, item_b),
        )
        succeeded_map = tui.action_plan.BatchResultMap()
        succeeded_map.record(item_a, 1)
        rejected_map = tui.action_plan.BatchResultMap()
        rejected_map.record(item_b, "head drift invalidates the item")
        collision_receipt = tui.action_plan.BatchActionReceipt(
            succeeded=succeeded_map,
            rejected=rejected_map,
            failed={},
        )
        app._on_batch_execution_finished(collision_receipt, [stop_a, stop_b], dummy_plan)
        check(
            stop_a.failure == "",
            "repo-a#31 succeeded and must have no failure",
        )
        check(
            stop_b.failure == "head drift invalidates the item",
            "repo-b#31 drifted and must record its own failure without collision",
        )

        # Draft rejection: skip draft PRs in plan building and drift on execution
        draft_stop = tui.Stop("projectbluefin/repo-draft", 99, "review", "draft PR")
        draft_stop.live = {
            "headRefOid": "d" * 40,
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        try:
            app.build_batch_queue_plan([draft_stop])
            check(False, "build_batch_queue_plan must reject/skip draft PRs")
        except tui.action_plan.InvalidPlanError as error:
            check("no queueable pull requests in batch" in str(error),
                  "batch with only draft PRs must raise InvalidPlanError")

        valid_stop = tui.Stop("projectbluefin/repo-valid", 100, "review", "valid PR")
        valid_stop.live = {
            "headRefOid": "e" * 40,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        mixed_plan = app.build_batch_queue_plan([draft_stop, valid_stop])
        check(
            len(mixed_plan.items) == 1 and mixed_plan.items[0].pull_request == 100,
            "build_batch_queue_plan must filter out draft PRs from the batch",
        )

        try:
            tui.action_plan.CurrentState.capture(
                actor="castrojo",
                tenant="projectbluefin",
                repository="projectbluefin/review",
                pull_request=100,
                head_sha="e" * 40,
                live={"isDraft": True},
            )
            check(False, "CurrentState.capture must raise PlanDriftError for draft PR")
        except tui.action_plan.PlanDriftError as error:
            check("PR is draft" in str(error), "draft PR must drift with 'PR is draft'")

        # Stale-fetch failure: fresh fetch fails and must not fall back to cached stop.live
        stale_stop = tui.Stop("projectbluefin/repo-stale", 101, "review", "stale PR")
        stale_stop.live = {
            "headRefOid": "f" * 40,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        try:
            app.build_batch_queue_plan([stale_stop])
            check(False, "stale-fetch failure must not fall back to cached stop.live")
        except tui.action_plan.InvalidPlanError as error:
            check("no queueable pull requests in batch" in str(error),
                  "stale-fetch failure must raise InvalidPlanError without fallback")

        # In-flight guard: dismissing confirmation modal resets in-flight to False
        app.batch_mutation_in_flight = True
        app._present_batch_confirmation(mixed_plan, [valid_stop])
        await pilot.pause()
        check(isinstance(app.screen, tui.BatchMutationConfirmation), "must present modal")
        app.screen.dismiss(None)
        await pilot.pause()
        check(
            app.batch_mutation_in_flight is False,
            "dismissing confirmation modal must reset batch_mutation_in_flight to False",
        )

        # Regression: a stale modal callback (mismatched batch generation token)
        # must not clear batch_mutation_in_flight/_current_batch_plan when a
        # newer batch is already active. Stub push_screen so each batch's
        # confirmation callback can be captured and invoked directly, without
        # depending on the real screen stack.
        original_push_screen = app.push_screen
        captured_callbacks = []

        def fake_push_screen(screen, callback=None, **kwargs):
            captured_callbacks.append(callback)

        app.push_screen = fake_push_screen
        try:
            app.batch_mutation_in_flight = True
            app._current_batch_plan = None
            app._present_batch_confirmation(mixed_plan, [valid_stop])
            stale_callback = captured_callbacks[-1]

            # A newer batch starts (bumping the generation token and plan)
            # before the first modal's callback ever fires.
            app._present_batch_confirmation(mixed_plan, [valid_stop])
            newer_plan = app._current_batch_plan
            newer_token = app._batch_generation_token
        finally:
            app.push_screen = original_push_screen

        # Fire the OLD (now-stale) callback, simulating a late dismiss/abort
        # delivered after the newer batch has already taken over.
        stale_callback(None)
        check(
            app.batch_mutation_in_flight is True,
            "a stale modal callback must not clear batch_mutation_in_flight of an active newer batch",
        )
        check(
            app._current_batch_plan is newer_plan,
            "a stale modal callback must not clear _current_batch_plan of an active newer batch",
        )
        check(
            app._batch_generation_token == newer_token,
            "a stale modal callback must not alter the active batch generation token",
        )

        # Cleanup so subsequent assertions in this run are unaffected.
        app.batch_mutation_in_flight = False
        app._current_batch_plan = None
    pr7_view_count.unlink(missing_ok=True)
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then\n'
        '  if [ -n "${GH_USER_FAIL-}" ]; then echo "authentication required" >&2; exit 1; fi\n'
        '  echo castrojo; exit 0;\n'
        'fi\n'
        + org_queue_branch +
        'if [ "$1" = "api" ] && [[ "$2" == repos/*/compare/* ]]; then\n'
        '  if [ -n "${RE_REVIEW_COMPARE_FAIL-}" ]; then echo "compare unavailable" >&2; exit 1; fi\n'
        f'  printf "compare:%s\\n" "${{RE_REVIEW_COMPARE_JSON-UNSET}}" >>"{gh_log}"\n'
        '  if [ -n "${RE_REVIEW_COMPARE_JSON+x}" ]; then printf "%s\\n" "$RE_REVIEW_COMPARE_JSON"; else printf "%s\\n" "{}"; fi; exit 0\n'
        'fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then\n'
        '  if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi\n'
        '  echo "{}"; exit 0;\n'
        'fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        f'  request_id="${{DIFF_REQUEST_ID-unknown}}"; mode="${{DIFF_MODE-}}"\n'
        f'  if [ "$mode" = "slow-old" ]; then printf "request:%s:%s\\n" "$request_id" "$mode" >>"{diff_events}"; (sleep 0.2) & delay_pid=$!; : >"{old_request_started}"; wait "$delay_pid"; printf "response:%s:OLD-DIFF\\n" "$request_id" >>"{diff_events}"; printf "%s" "OLD-DIFF"; exit 0; fi\n'
        f'  if [ "$mode" = "fast-new" ]; then printf "request:%s:%s\\n" "$request_id" "$mode" >>"{diff_events}"; printf "response:%s:NEW-DIFF\\n" "$request_id" >>"{diff_events}"; printf "%s" "NEW-DIFF"; exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "oversized" ]; then head -c 400010 /dev/zero | tr "\\0" x; exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "empty" ]; then exit 0; fi\n'
        '  if [ "${DIFF_MODE-}" = "error" ]; then printf "%s\\n" "terminal diff failure" >&2; exit 7; fi\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "api" ] && [ "$2" = "--paginate" ]; then\n'
        '  if [ -n "${LIVE_GH_ERROR-}" ]; then printf "%s\\n" "$LIVE_GH_ERROR" >&2; exit 1; fi\n'
        '  case "$*" in *"/issues"*) if [ -n "${LIVE_ISSUES_FILE-}" ]; then cat "$LIVE_ISSUES_FILE"; else echo "[]"; fi; exit 0 ;; esac\n'
        '  if [ -n "${LIVE_PAGES-}" ]; then cat "$LIVE_QUEUE_FILE"; else printf "[%s]" "$(cat "$LIVE_QUEUE_FILE")"; fi; exit 0\n'
        'fi\n'
        'if [ "$1 $2" = "pr list" ]; then\n'
        '  echo "[]"\n'
        '  exit 0\n'
        'fi\n'
        "exit 0\n",
    )
    gh_log.write_text("")

    # ── a relaunched dashboard restores a previous batch's failure ──────
    # The landings directory persists on the host; the rows must show what
    # it records at startup, or the failure markings are still lost on every
    # relaunch (#281). A newer batch's verdict wins over an older one.
    landings_dir = workdir / "state" / "bluefin-review" / "landings"
    landings_dir.mkdir(parents=True, exist_ok=True)
    for stale in landings_dir.iterdir():
        stale.unlink()
    older = landings_dir / "20260101-000000-review-queue.jsonl"
    older.write_text(
        '{"pr": "projectbluefin/bluefinctl#31", "state": "failed", "note": "stale first attempt"}\n'
        '{"state": "done", "note": "first batch"}\n'
    )
    newer = landings_dir / "20260102-000000-review-queue.jsonl"
    newer.write_text(
        '{"pr": "projectbluefin/bluefinctl#31", "state": "failed", "note": "publish workflow red"}\n'
        '{"state": "done", "note": "one batch, one failure"}\n'
    )
    # Fold order is mtime order, and the record is bounded to the retention
    # window (#290): recent but distinctly ordered, or the prune pass would
    # collect these fixtures as expired.
    now = time.time()
    os.utime(older, (now - 200, now - 200))
    os.utime(newer, (now - 100, now - 100))
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        marked = {stop.key: stop for stop in app.stops}
        check(
            marked["projectbluefin/bluefinctl#31"].failure
            == "failed: publish workflow red",
            "a relaunch must restore the row's failure marking from the "
            f"newest record, got {marked['projectbluefin/bluefinctl#31'].failure!r}",
        )
        check(
            marked["projectbluefin/common#7"].failure == "",
            "an unrelated row must stay unmarked",
        )
        check(
            not marked["projectbluefin/bluefinctl#31"].selected,
            "restoring a marking must not rebuild the batch selection",
        )
        row = app.row_markup(marked["projectbluefin/bluefinctl#31"])
        check(
            "DID NOT MERGE" in row,
            f"the restored failure must render on the row, got {row!r}",
        )
    for stale in landings_dir.iterdir():
        stale.unlink()
    gh_log.write_text("")

    # ── a manual success supersedes the restored failure (#290) ─────────
    # The restored mark lives in the record, so only the record can retire
    # it: clearing the row in memory lasted exactly one refresh. A success
    # path writes a superseding event, and the next restore leaves the row
    # clean. An ancient record is pruned rather than restored — the
    # directory is durable, so it must also be bounded.
    older.write_text(
        '{"pr": "projectbluefin/bluefinctl#31", "state": "failed", "note": "publish workflow red"}\n'
    )
    os.utime(older, (now - 100, now - 100))
    restored = tui.landing.persisted_events()
    check(
        restored["projectbluefin/bluefinctl#31"]["state"] == "failed",
        f"the failure must be in the record, got {restored!r}",
    )
    tui.landing.record_event(
        "projectbluefin/bluefinctl#31", "merged", "merged directly by @tester"
    )
    with (landings_dir / "manual.jsonl").open("a") as handle:
        for number in range(32, 36):
            handle.write(
                json.dumps(
                    {
                        "pr": f"projectbluefin/bluefinctl#{number}",
                        "state": "merged",
                        "ts": int(now) + (34 if number == 35 else number),
                    }
                )
                + "\n"
            )
        handle.write(
            json.dumps(
                {
                    "pr": "projectbluefin/bluefinctl#32",
                    "state": "merged",
                    "ts": int(now) + 34,
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "pr": "mame/_#99",
                    "state": "merged",
                    "ts": int(now) + 99,
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "pr": "projectbluefin/[bold]repo#99",
                    "state": "merged",
                    "ts": int(now) + 99,
                }
            )
            + "\n"
        )
    restored = tui.landing.persisted_events()
    check(
        restored["projectbluefin/bluefinctl#31"]["state"] == "merged",
        "a manual success must supersede the persisted failure, "
        f"got {restored['projectbluefin/bluefinctl#31']!r}",
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 2)
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            (
                "last merged: mame/_#99, projectbluefin/bluefinctl#32, "
                "projectbluefin/bluefinctl#35"
            ) in status
            and "projectbluefin/bluefinctl#31" not in status
            and "projectbluefin/[bold]repo#99" not in status,
            "a relaunch must show only safe, newest merged pull requests",
        )
    ancient = landings_dir / "20260103-000000-review-queue.jsonl"
    ancient.write_text(
        '{"pr": "projectbluefin/bluefinctl#31", "state": "failed", "note": "ancient"}\n'
    )
    expired = now - tui.landing.LANDING_RETENTION_SECONDS - 60
    os.utime(ancient, (expired, expired))
    restored = tui.landing.persisted_events()
    check(
        not ancient.exists(),
        "an expired record must be pruned, not kept",
    )
    check(
        restored["projectbluefin/bluefinctl#31"]["state"] == "merged",
        "a pruned record must not restore, "
        f"got {restored['projectbluefin/bluefinctl#31']!r}",
    )
    for stale in landings_dir.iterdir():
        stale.unlink()
    gh_log.write_text("")

    # ── batch ids never collide, even inside one second ──────────────────
    # Two named dashboards share one state directory, so the instance name
    # qualifies the id; two batches from one dashboard in the same second
    # get a suffix. Either collision would overwrite a batch's files.
    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "pilot-instance"
    first_task = tui.landing.new_task(
        [tui.Stop("o/r", 1, "review", "one")], "tester"
    )
    second_task = tui.landing.new_task(
        [tui.Stop("o/r", 2, "review", "two")], "tester"
    )
    del os.environ["BLUEFIN_REVIEW_INSTANCE"]
    check(
        first_task.task_id.endswith("-pilot-instance"),
        f"the batch id must name its instance, got {first_task.task_id!r}",
    )
    check(
        first_task.task_id != second_task.task_id,
        f"same-second batches must not share an id, got {first_task.task_id!r} twice",
    )
    check(
        first_task.prompt_path != second_task.prompt_path
        and first_task.status_path != second_task.status_path,
        "same-second batches must not share prompt or status files",
    )
    for stale in landings_dir.iterdir():
        stale.unlink()
    gh_log.write_text("")

    # ── aborting the plan gate dispatches nothing ────────────────────────
    landing_log.write_text("")
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.BatchPlanScreen),
            "a selected batch must gate before dispatch",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(
            not app.landing_queue and landing_log.read_text() == "",
            "escape must abort the batch without dispatching an agent",
        )
        app.action_land_batch()
        await pilot.pause()
        check(isinstance(app.screen, tui.BatchPlanScreen), "BatchPlanScreen must activate")
        await pilot.press("q")
        await pilot.pause()
        check(not isinstance(app.screen, tui.BatchPlanScreen), "q must abort BatchPlanScreen")
    gh_log.write_text("")

    # ── a selection is unmistakable on the row itself ──────────────────
    # Colour is never the only carrier of a fact: a selected row carries a
    # ● marker in its text AND a full-row background, so the batch the
    # maintainer is building is visible without reading the status line.
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        queue = app.query_one("#queue", tui.ListView)
        # The ListView cursor carries its own background, so the selection
        # background can only be proven on a selected row the cursor has
        # left: capture both reference backgrounds first.
        cursor_bg = queue.children[0].styles.background
        plain_bg = queue.children[1].styles.background

        class CountingRunStore:
            def __init__(self) -> None:
                self.snapshot_calls = 0
                self.get_calls = 0

            def snapshot(self) -> dict[str, object]:
                self.snapshot_calls += 1
                return {}

            def get(self, _identity) -> None:
                self.get_calls += 1
                return None

        run_store = CountingRunStore()
        app.run_store = run_store
        await pilot.press("b")
        check(
            run_store.snapshot_calls == 1,
            "batch selection must read run state once for the whole visible queue",
        )
        check(
            run_store.get_calls == 0,
            "batch selection must not read run state separately for each row",
        )
        await pilot.press("down")
        await pilot.pause()
        check(
            [s.selected for s in app.stops] == [True, False],
            f"[b] must select only the highlighted row, got {[s.selected for s in app.stops]}",
        )
        first = str(queue.children[0].query(tui.Label).first().render())
        second = str(queue.children[1].query(tui.Label).first().render())
        check(
            "●" in first and "●" not in second,
            f"a selected row must carry the ● marker and an unselected row "
            f"must not, got {first!r} / {second!r}",
        )
        selected_bg = queue.children[0].styles.background
        check(
            selected_bg != plain_bg and selected_bg != cursor_bg,
            "a selected row must carry a full-row background, not "
            "colour-only text",
        )
        await pilot.press("up")
        await pilot.press("b")
        await pilot.press("down")
        await pilot.pause()
        first = str(queue.children[0].query(tui.Label).first().render())
        check(
            "●" not in first,
            f"deselecting must remove the marker, got {first!r}",
        )
        check(
            queue.children[0].styles.background == plain_bg,
            "deselecting must drop the full-row background",
        )
    gh_log.write_text("")

    # ── A lands the batch; w watches it ────────────────────────────────
    # The maintainer selects with [b] and reaches for capital A — "do them
    # All". The stronger keystroke does the strong thing: A opens the batch
    # plan gate. The read-only batch queue viewer lives on w.
    landing_log.write_text("")
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices: list[str] = []
        real_notify = app.notify

        def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append(str(message))
            _notify(message, *args, **kwargs)

        app.notify = record
        await pilot.press("A")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.BatchPlanScreen)
            and not isinstance(app.screen, tui.LandingScreen),
            "[A] without a selection must open neither the gate nor the viewer",
        )
        check(
            any("[b]" in n for n in notices),
            f"[A] without a selection must say what selects, got {notices}",
        )
        await pilot.press("w")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.LandingScreen),
            "[w] before any dispatch must warn, not open an empty viewer",
        )
        await pilot.press("b")
        await pilot.press("A")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.BatchPlanScreen),
            "[A] on a selection must open the batch plan gate, got "
            f"{type(app.screen).__name__}",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(
            not app.landing_queue and landing_log.read_text() == "",
            "escape from the [A] gate must dispatch nothing",
        )
        await pilot.press("A")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.BatchPlanScreen),
            "reopening [A] must present the batch plan gate",
        )
        await pilot.press("enter")
        for _ in range(50):
            if len(app.landing_queue) == 1:
                break
            await pilot.pause(0.05)
        check(
            not isinstance(app.screen, tui.LandingScreen)
            and not isinstance(app.screen, tui.BatchPlanScreen),
            f"confirming A batch dispatch must return to canonical review queue, got {type(app.screen).__name__}",
        )
        check(
            len([t for t in app.landing_queue if not t.phase]) == 1,
            "confirming A batch dispatch must enqueue the landing task",
        )
        await pilot.press("w")
        await pilot.pause()
        check(
            app.query_one("#landing-pause", tui.Button).has_focus,
            "[w] must focus persistent queue controls instead of opening a second view",
        )
        await pilot.press("escape")
        await pilot.pause()
        check(
            app.query_one("#queue", tui.ListView).has_focus,
            "Escape from queue controls must return to the item list, not exit the dashboard",
        )
    gh_log.write_text("")

    # ── a finished batch tells the maintainer ──────────────────────────
    # "I can't tell when it's done." A completed batch announces itself:
    # the notification carries the batch id and the per-state outcome, and
    # the rows keep what the notification cannot outlive.
    mixed_stub = write_stub(
        workdir / "stub-landing-mixed",
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'prs=$(grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u)\n'
        'first=$(echo "$prs" | head -1); last=$(echo "$prs" | tail -1)\n'
        'printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"merged\\", \\"note\\": \\"green\\"}\\n" "$first" >>"$status"\n'
        'printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"failed\\", \\"note\\": \\"branch protection refused\\"}\\n" "$last" >>"$status"\n'
        'printf "{\\"state\\": \\"done\\", \\"note\\": \\"one landed, one refused\\"}\\n" >>"$status"\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{mixed_stub} @PROMPT"

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices: list[tuple[str, str]] = []
        real_notify = app.notify

        def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append((str(message), kwargs.get("severity", "information")))
            _notify(message, *args, **kwargs)

        app.notify = record
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        gate = app.screen
        check(isinstance(gate, tui.BatchPlanScreen), "the batch must gate")
        await pilot.press("enter")
        task = gate.plan
        for _ in range(400):
            if task.returncode is not None and any(
                "finished" in message for message, _ in notices
            ):
                break
            await pilot.pause(0.05)
        expected = f"batch {task.task_id} finished: 1 merged, 1 failed"
        check(
            any(expected in message for message, _ in notices),
            f"a finished batch must notify its outcome, want {expected!r} "
            f"in {notices}",
        )
        check(
            any(
                expected in message and severity == "error"
                for message, severity in notices
            ),
            "a batch carrying a failure must notify at error severity, "
            f"got {notices}",
        )
        for _ in range(200):
            if not any(s.selected for s in app.stops):
                break
            await pilot.pause(0.05)
        merged_stop = next(s for s in app.stops if s.key == "projectbluefin/bluefinctl#31")
        failed_stop = next(s for s in app.stops if s.key == "projectbluefin/common#7")
        check(
            not merged_stop.selected and not merged_stop.failure,
            "a merged pull request leaves the batch",
        )
        check(
            not failed_stop.selected and "failed" in failed_stop.failure,
            "a failed pull request keeps its reason without automatic "
            "reselection",
        )
        # The outcome also persists where a toast cannot: the status line
        # keeps the last batch's result until the next dispatch or refresh.
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            expected in status,
            f"the status line must keep the batch outcome, got {status!r}",
        )
        await pilot.press("R")
        for _ in range(200):
            status = str(app.query_one("#status-bar", tui.Static).render())
            if expected not in status and len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            expected not in status,
            f"a refresh must clear the last-batch outcome, got {status!r}",
        )
    gh_log.write_text("")

    # ── an agent that dies mid-batch says so ───────────────────────────
    # Exiting without the task-level done event used to be
    # indistinguishable from still working: the row silently kept its last
    # mark. It is its own surfaced state now.
    died_stub = write_stub(
        workdir / "stub-landing-died",
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'first=$(grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u | head -1)\n'
        'printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"fixing\\", \\"note\\": \\"retrying CI\\"}\\n" "$first" >>"$status"\n'
        'exit 1\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{died_stub} @PROMPT"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append((str(message), kwargs.get("severity", "information")))
            _notify(message, *args, **kwargs)

        app.notify = record
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        gate = app.screen
        check(isinstance(gate, tui.BatchPlanScreen), "the batch must gate")
        await pilot.press("enter")
        task = gate.plan
        for _ in range(400):
            if task.returncode is not None and any(
                "without reporting done" in message for message, _ in notices
            ):
                break
            await pilot.pause(0.05)
        check(
            any(
                f"batch {task.task_id}" in message
                and "without reporting done" in message
                and severity == "error"
                for message, severity in notices
            ),
            "a batch whose agent exits without the done event must say so "
            f"at error severity, got {notices}",
        )
        for _ in range(200):
            if all("died mid-batch" in s.failure for s in app.stops):
                break
            await pilot.pause(0.05)
        check(
            not any(s.selected for s in app.stops),
            "an unfinished batch must require explicit reselection",
        )
        check(
            all("died mid-batch" in s.failure for s in app.stops),
            "each unfinished pull request must be marked distinguishable "
            f"from a reported state, got {[s.failure for s in app.stops]}",
        )
        check(
            "fixing" in app.stops[0].failure,
            "the mark must keep the agent's last reported state, got "
            f"{app.stops[0].failure!r}",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "without reporting done" in status,
            f"the status line must keep the dead-agent outcome, got {status!r}",
        )
    gh_log.write_text("")

    # ── done with a missing outcome is a gap, not a dead agent ──────────
    # A batch that writes the task-level done event but never carried one
    # pull request to an outcome must not cry "died mid-batch" — the agent
    # finished; its report has a hole, and the hole is what is marked.
    gap_stub = write_stub(
        workdir / "stub-landing-gap",
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'first=$(grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u | head -1)\n'
        'printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"merged\\", \\"note\\": \\"green\\"}\\n" "$first" >>"$status"\n'
        'printf "{\\"state\\": \\"done\\", \\"note\\": \\"landed what I saw\\"}\\n" >>"$status"\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{gap_stub} @PROMPT"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append((str(message), kwargs.get("severity", "information")))
            _notify(message, *args, **kwargs)

        app.notify = record
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        gate = app.screen
        check(isinstance(gate, tui.BatchPlanScreen), "the batch must gate")
        await pilot.press("enter")
        task = gate.plan
        expected = f"batch {task.task_id} finished: 1 merged, 1 no outcome"
        for _ in range(400):
            if task.returncode is not None and any(
                expected in message for message, _ in notices
            ):
                break
            await pilot.pause(0.05)
        check(
            any(expected in message for message, _ in notices),
            f"a done batch with a missing outcome must say so, want "
            f"{expected!r} in {notices}",
        )
        check(
            all(", 0 " not in message for message, _ in notices),
            "the summary must count only states that occurred, got "
            f"{notices}",
        )
        check(
            all(
                "without reporting done" not in message
                for message, _ in notices
            ),
            "a batch that wrote done must not be reported as a dead agent, "
            f"got {notices}",
        )
        gap_stop = next((s for s in app.stops if s.key == "projectbluefin/common#7"), app.stops[1])
        for _ in range(200):
            if gap_stop.failure:
                break
            await pilot.pause(0.05)
        check(
            gap_stop.failure.startswith("no outcome reported"),
            "the out-of-report pull request must be marked as a reporting "
            f"gap, got {gap_stop.failure!r}",
        )
        check(
            all("died mid-batch" not in s.failure for s in app.stops),
            "no row may claim a dead agent when the agent reported done, "
            f"got {[s.failure for s in app.stops]}",
        )
        check(
            not gap_stop.selected,
            "an out-of-report pull request must require explicit reselection",
        )
    gh_log.write_text("")

    # ── a pr-less line is not the done event ────────────────────────────
    # parse_status files every line lacking "pr" under the task key, so a
    # truthiness test lets one malformed tail line pass for done. Only
    # {"state": "done"} closes a report; anything less is a dead agent.
    tail_stub = write_stub(
        workdir / "stub-landing-tail",
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u | while read -r pr; do\n'
        '  printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"merged\\", \\"note\\": \\"green\\"}\\n" "$pr" >>"$status"\n'
        'done\n'
        'printf "{\\"note\\": \\"unstructured tail\\"}\\n" >>"$status"\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{tail_stub} @PROMPT"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append((str(message), kwargs.get("severity", "information")))
            _notify(message, *args, **kwargs)

        app.notify = record
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        gate = app.screen
        check(isinstance(gate, tui.BatchPlanScreen), "the batch must gate")
        await pilot.press("enter")
        task = gate.plan
        for _ in range(400):
            if task.returncode is not None and any(
                f"batch {task.task_id}" in message for message, _ in notices
            ):
                break
            await pilot.pause(0.05)
        check(
            any(
                f"batch {task.task_id}" in message
                and "without reporting done" in message
                and severity == "error"
                for message, severity in notices
            ),
            "a malformed pr-less line must not pass for the done event, "
            f"got {notices}",
        )
        check(
            all("finished" not in message for message, _ in notices),
            f"a report without done must never read as finished, got {notices}",
        )
    gh_log.write_text("")

    # ── the summary counts only what occurred ────────────────────────────
    # An all-blocked batch reads "finished: 2 blocked" — not a litter of
    # zero counts for states nothing reached.
    blocked_stub = write_stub(
        workdir / "stub-landing-blocked",
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u | while read -r pr; do\n'
        '  printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"blocked\\", \\"note\\": \\"draft\\"}\\n" "$pr" >>"$status"\n'
        'done\n'
        'printf "{\\"state\\": \\"done\\", \\"note\\": \\"all blocked\\"}\\n" >>"$status"\n',
    )
    try:
        os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{blocked_stub} @PROMPT"
        app = tui.ReviewDashboard(tui.QueueFilters(action=""))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if len(app.stops) == 2:
                    break
                await pilot.pause(0.05)
            app.self_login = "castrojo"
            notices = []
            real_notify = app.notify

            def record(message, *args, _notices=notices, _notify=real_notify, **kwargs):
                _notices.append((str(message), kwargs.get("severity", "information")))
                _notify(message, *args, **kwargs)

            app.notify = record
            for stop in app.stops:
                stop.selected = True
            app.action_land_batch()
            await pilot.pause()
            gate = app.screen
            await pilot.press("enter")
            task = gate.plan
            expected = f"batch {task.task_id} finished: 2 blocked"
            for _ in range(400):
                if task.returncode is not None and any(
                    f"batch {task.task_id}" in message for message, _ in notices
                ):
                    break
                await pilot.pause(0.05)
            check(
                any(expected in message for message, _ in notices),
                f"an all-blocked batch must say exactly that, want {expected!r} "
                f"in {notices}",
            )
    finally:
        os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{landing_stub} @PROMPT"
    gh_log.write_text("")
    # ── the landing brief covers repositories with no image pipeline ────
    # "Done is :stable" can never resolve where nothing publishes an image
    # (observed: projectbluefin/bluespeed, a config/quadlets repository —
    # a squash-merged PR sat marked failed). The brief must have the agent
    # detect the missing pipeline before merging and treat the GitHub
    # merge itself as done there — without the packages API, whose
    # read:packages scope the shipped token lacks and whose orgs endpoint
    # 404s on user-owned repositories (a false "no package").
    probe = tui.landing.new_task(
        [SimpleNamespace(key="projectbluefin/bluespeed#63", title="chore: bump digest")],
        "castrojo",
    )
    brief = " ".join(Path(probe.prompt_path).read_text().split())
    check(
        "no publish workflow" in brief and "no image package" in brief,
        "the brief must have the agent detect a missing publish workflow "
        "and image package",
    )
    check(
        "BEFORE merging" in brief,
        "the pipeline check must happen before the merge, not after it",
    )
    check(
        "the GitHub merge itself is done" in brief,
        "the brief must define done as the GitHub merge when no image "
        "pipeline exists",
    )
    check(
        "unless BOTH signals are absent" in brief,
        "the no-pipeline path must require both signals absent — one "
        "signal alone never skips :stable verification",
    )
    check(
        "token mint is denied" in brief and "401/403" in brief,
        "the registry signal must be a denied anonymous token mint or "
        "a /tags/list 401/403 — ghcr never 404s a missing package, so a "
        "404-based test never fires (bluespeed proved it)",
    )
    check(
        "PRIVATE package" in brief and "ambiguous" in brief,
        "the brief must name the 403/private-package ambiguity and the "
        "workflow conjunction that covers it",
    )
    check(
        "not an error to retry" in brief,
        "a denied mint is the negative signal itself — the brief must "
        "say so, since curl -fsSL exits nonzero on it",
    )
    check(
        "package_type=container" not in brief,
        "the detection must not call the packages API",
    )
    check(
        ":stable" in brief and "awaiting-stable" in brief,
        "the brief must keep the :stable definition where a pipeline exists",
    )

    # On a merge-queue repository the merge completes after `gh pr merge`
    # returns (common#1008: the agent watched a merge_group gate run for 20
    # minutes on an already-merged pull request). The brief must teach the
    # accept-then-poll path and the push-event publish run.
    check(
        "accepted by merge queue" in brief and "merge_group" in brief,
        "the brief must teach the merge-queue accept, and never watching "
        "a merge_group run",
    )
    check(
        "until it reads MERGED" in brief,
        "the merge-queue wait must poll the pull request state",
    )
    check(
        "names its target and timeout" in brief,
        "every wait-state note must name its target and timeout",
    )

    # common#1008 published successfully as `common:latest` and was
    # reported blocked because common carries no `:stable` tag — the
    # release tag is the repository's fact, never the brief's assumption,
    # and blocked/failed is for a publish nothing can evidence at all.
    check(
        "is a fact about the repository" in brief and "`latest`" in brief,
        "the brief must discover the release tag, not assume :stable",
    )
    check(
        "no publication of the merge commit can be evidenced" in brief,
        "the brief must accept the publish it can prove and fail only "
        "when none can be evidenced",
    )

    # ── the brief routes every status write through the report CLI ─────
    # #377: a bulk terminal-state write died on shell quoting mid-batch, its
    # retry duplicated terminal events, and no done event ever landed — the
    # durable record disagreed with GitHub after irreversible merges. The
    # brief must have the agent report each transition immediately through
    # the image's reporter and forbid direct status-file writes outright.
    check(
        "report --status" in brief and " event --pr " in brief,
        "the brief must report per-PR events through the module's report CLI",
    )
    check(
        " done --expect " in brief,
        "the brief must close the batch through done --expect, naming the "
        "whole selection",
    )
    check(
        "flock" in brief,
        "the brief must state the reporter serializes writers under flock",
    )
    check(
        "no printf" in brief and "no heredoc" in brief,
        "the brief must forbid direct status-file writes",
    )
    check(
        "bulk terminal-state write" in brief,
        "the brief must forbid saving terminal states up for one bulk write",
    )
    check(
        "written once" in brief
        and "identical retry is a no-op" in brief
        and "the latest event wins" in brief,
        "the brief must define a terminal state as written once — identical "
        "retries no-op — and a wrong terminal verdict correctable, never "
        "final by accident",
    )
    check(
        "lacks a terminal state" in brief,
        "done must refuse to close while an expected pull request lacks a "
        "terminal state",
    )

    # #375: the denied mint must survive in code, not in a shell pipeline
    # the agent assembles: the brief delegates the whole registry flow to
    # the reporter's probe, and no curl-into-jq pipeline may survive in the
    # brief text.
    check(
        not re.search(r"curl[^\n|]*\|\s*jq", Path(probe.prompt_path).read_text()),
        "the token mint must not pipe curl into jq — jq's exit status masks "
        "a denied mint (#375)",
    )
    check(
        " probe --package " in brief,
        "the brief must probe the registry through the reporter's probe "
        "command, which preserves a denied mint in code (#375)",
    )
    check(
        "never evidence of absence" in brief,
        "a probe that cannot answer must never read as a missing package",
    )

    # #376: projectbluefin/actions mentions ghcr.io throughout its reusable
    # workflows without publishing an image itself, and the broad signal
    # stalled a batch for the full ten-minute timeout. The publish signal
    # must be an on.push publication path targeting the repository's own
    # package, and the wait must end when terminal runs answer first.
    check(
        "on.push" in brief,
        "the publish signal must be an on.push publication path, not any "
        "ghcr.io mention",
    )
    check(
        "workflow_call" in brief and "workflow_dispatch" in brief,
        "reusable and manual-only workflows must be named as non-signals",
    )
    check(
        "references to other" in brief,
        "references to other repositories' images must not count as a "
        "publication path",
    )
    check(
        "stop polling" in brief and "terminal runs prove" in brief,
        "the brief must stop the publish wait once terminal runs prove no "
        "publication is owed",
    )
    check(
        " publish-verdict " in brief and "workflow_run" in brief and "release" in brief,
        "the brief must route the wait/stop decision through the "
        "publish-verdict command, covering push, workflow_run, and release "
        "triggers",
    )
    check(
        "an empty run list is never evidence" in brief,
        "an empty gh run list must never read as 'no publication' — runs "
        "can lag the merge",
    )

    # ── the landing reporter enforces the record ───────────────────────
    # #377: terminal events are idempotent and correctable — an identical
    # retry no-ops, a terminal verdict later proven wrong is corrected by
    # the new terminal state (the latest event wins), only a post-terminal
    # NON-terminal write fails — and done refuses to close while any
    # selected pull request lacks a terminal outcome. The dashboard reads
    # what the reporter writes, so parse_status must fold the reporter's
    # lines exactly.
    cli_status = workdir / "cli.jsonl"

    def report(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(landing_py),
                "report",
                "--status",
                str(cli_status),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def cli_lines() -> list[str]:
        return (
            cli_status.read_text().splitlines() if cli_status.exists() else []
        )

    result = report(
        "event", "--pr", "org/repo#1", "--state", "fixing", "--note", "retrying CI"
    )
    check(
        result.returncode == 0,
        f"a first event must be accepted, got {result.returncode}: {result.stderr}",
    )
    result = report(
        "event", "--pr", "org/repo#1", "--state", "merged", "--note", "green"
    )
    check(
        result.returncode == 0,
        f"a terminal event must be accepted, got {result.returncode}: {result.stderr}",
    )
    check(
        len(cli_lines()) == 2,
        f"two accepted events must be two lines, got {cli_lines()}",
    )
    result = report(
        "event", "--pr", "org/repo#1", "--state", "merged", "--note", "green"
    )
    check(
        result.returncode == 0 and len(cli_lines()) == 2,
        "an identical terminal retry must be an accepted no-op, got "
        f"{result.returncode} and {cli_lines()}",
    )
    result = report(
        "event", "--pr", "org/repo#1", "--state", "failed", "--note", "publish lost"
    )
    check(
        result.returncode == 0 and len(cli_lines()) == 3,
        "a terminal verdict proven wrong must be correctable by the new "
        "terminal state — a premature merged is never uncorrectable, got "
        f"{result.returncode} and {cli_lines()}",
    )
    folded = tui.landing.parse_status(str(cli_status))
    check(
        folded.get("org/repo#1", {}).get("state") == "failed",
        f"the correction must win the fold, got {folded}",
    )
    result = report(
        "event", "--pr", "org/repo#1", "--state", "waiting-ci", "--note", "late poll"
    )
    check(
        result.returncode != 0 and len(cli_lines()) == 3,
        "a non-terminal write after a terminal state must fail, got "
        f"{result.returncode} and {cli_lines()}",
    )
    result = report(
        "done", "--expect", "org/repo#1", "org/repo#2", "--note", "closing early"
    )
    check(
        result.returncode != 0 and not any('"done"' in l for l in cli_lines()),
        "done must refuse while an expected pull request lacks a terminal "
        f"state, got {result.returncode} and {cli_lines()}",
    )
    result = report(
        "event", "--pr", "org/repo#2", "--state", "blocked", "--note", "draft"
    )
    check(
        result.returncode == 0,
        f"a second terminal event must be accepted, got {result.returncode}",
    )
    result = report(
        "done",
        "--expect",
        "org/repo#1",
        "org/repo#2",
        "--note",
        "one corrected, one blocked",
    )
    check(
        result.returncode == 0,
        f"done must close once every expected pull request is terminal, got "
        f"{result.returncode}: {result.stderr}",
    )
    check(
        len(cli_lines()) == 5,
        f"done must append exactly one line, got {cli_lines()}",
    )
    result = report(
        "done",
        "--expect",
        "org/repo#1",
        "org/repo#2",
        "--note",
        "one corrected, one blocked",
    )
    check(
        result.returncode == 0 and len(cli_lines()) == 5,
        "an identical done retry must be an accepted no-op, got "
        f"{result.returncode} and {cli_lines()}",
    )
    result = report(
        "done", "--expect", "org/repo#1", "org/repo#2", "--note", "different summary"
    )
    check(
        result.returncode != 0 and len(cli_lines()) == 5,
        "a conflicting done must fail, got "
        f"{result.returncode} and {cli_lines()}",
    )
    result = report(
        "event", "--pr", "org/repo#3", "--state", "merged", "--note", "late"
    )
    check(
        result.returncode != 0 and len(cli_lines()) == 5,
        "an event after the batch is done must fail, got "
        f"{result.returncode} and {cli_lines()}",
    )
    result = report(
        "event", "--pr", "org/repo#4", "--state", "launched", "--note", "bogus"
    )
    check(
        result.returncode != 0,
        f"a state outside the vocabulary must be rejected, got {result.returncode}",
    )
    folded = tui.landing.parse_status(str(cli_status))
    check(
        folded.get("org/repo#1", {}).get("state") == "failed"
        and folded.get("org/repo#2", {}).get("state") == "blocked"
        and folded.get("", {}).get("state") == "done",
        f"parse_status must fold the reporter's file exactly, got {folded}",
    )
    check(
        all(
            isinstance(json.loads(line), dict) and len(line.splitlines()) == 1
            for line in cli_lines()
        ),
        "every reporter line must be one JSON object on one physical line",
    )
    check(
        all(isinstance(json.loads(line).get("ts"), int) for line in cli_lines()),
        "every reporter-written line must carry a ts timestamp, so "
        "immediate-write evidence is verifiable",
    )
    newline_status = workdir / "cli-newline.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(landing_py),
            "report",
            "--status",
            str(newline_status),
            "event",
            "--pr",
            "org/repo#9",
            "--state",
            "blocked",
            "--note",
            "first line\nsecond line",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    check(
        result.returncode == 0
        and newline_status.exists()
        and len(newline_status.read_text().splitlines()) == 1,
        "a note carrying a newline must stay one physical JSONL line, got "
        f"{result.returncode} and "
        f"{newline_status.read_text().splitlines() if newline_status.exists() else []}",
    )

    # The batch record opens with the maintainer's confirmed selection, so
    # done gates on what was dispatched — an agent that under-names its
    # batch in --expect cannot close it with a hole (#377).
    seeded_header = json.loads(Path(probe.status_path).read_text().splitlines()[0])
    check(
        seeded_header.get("expect") == [stop.key for stop in probe.stops]
        and isinstance(seeded_header.get("ts"), int),
        "new_task must seed the status file with the confirmed selection "
        "and a dispatch timestamp",
    )
    seeded_status = workdir / "cli-seeded.jsonl"
    seeded_status.write_text(
        json.dumps({"expect": ["org/repo#10", "org/repo#11"]}, separators=(",", ":"))
        + "\n"
    )
    result = subprocess.run(
        [
            sys.executable, str(landing_py), "report", "--status",
            str(seeded_status), "event", "--pr", "org/repo#10",
            "--state", "merged", "--note", "green",
        ],
        capture_output=True, text=True, timeout=30,
    )
    check(result.returncode == 0, f"seeded event failed: {result.stderr}")
    result = subprocess.run(
        [
            sys.executable, str(landing_py), "report", "--status",
            str(seeded_status), "done", "--expect", "org/repo#10",
            "--note", "subset",
        ],
        capture_output=True, text=True, timeout=30,
    )
    check(
        result.returncode != 0 and "org/repo#11" in result.stderr,
        "done must refuse to close when a seeded selection member lacks a "
        f"terminal state even if --expect under-names it, got "
        f"{result.returncode}: {result.stderr}",
    )
    folded = tui.landing.parse_status(str(seeded_status))
    check(
        "" not in folded and folded.get("org/repo#10", {}).get("state") == "merged",
        f"parse_status must skip the selection header, got {folded}",
    )

    # A writer that died mid-line must not take the next event down with
    # it: the reporter truncates the unparseable partial tail rather than
    # gluing a new line onto it.
    broken_status = workdir / "cli-broken.jsonl"
    broken_status.write_text(
        '{"pr":"org/repo#1","state":"merged","note":"ok"}\n'
        '{"pr":"org/repo#2","sta'
    )
    result = subprocess.run(
        [
            sys.executable, str(landing_py), "report", "--status",
            str(broken_status), "event", "--pr", "org/repo#3",
            "--state", "blocked", "--note", "draft",
        ],
        capture_output=True, text=True, timeout=30,
    )
    broken_lines = broken_status.read_text().splitlines()
    check(
        result.returncode == 0
        and len(broken_lines) == 2
        and all(isinstance(json.loads(line), dict) for line in broken_lines),
        "an appended event must start a fresh physical line even after a "
        f"truncated tail, got {result.returncode} and {broken_lines}",
    )
    folded = tui.landing.parse_status(str(broken_status))
    check(
        folded.get("org/repo#3", {}).get("state") == "blocked",
        f"the appended event must survive in the record, got {folded}",
    )

    # A complete final line that only lacks its newline IS a record:
    # parse_status counts it, so the gates must count it and the appender
    # must preserve it — only a torn tail is truncated.
    tailonly_status = workdir / "cli-tailonly.jsonl"
    tailonly_status.write_text(
        '{"expect":["org/repo#1","org/repo#2"],"ts":1}\n'
        '{"pr":"org/repo#1","state":"merged","note":"a","ts":2}\n'
        '{"pr":"org/repo#2","state":"merged","note":"b","ts":3}'  # no trailing newline
    )

    def tailonly_report(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, str(landing_py), "report", "--status",
                str(tailonly_status), *args,
            ],
            capture_output=True, text=True, timeout=30,
        )

    result = tailonly_report(
        "done", "--expect", "org/repo#1", "org/repo#2", "--note", "all"
    )
    check(
        result.returncode == 0,
        "a complete final line missing only its newline must count toward "
        f"the done gate, got {result.returncode}: {result.stderr}",
    )
    tailonly_lines = tailonly_status.read_text().splitlines()
    check(
        len(tailonly_lines) == 4
        and sum('"pr":"org/repo#2"' in line for line in tailonly_lines) == 1
        and all(isinstance(json.loads(line), dict) for line in tailonly_lines),
        "the appender must terminate and preserve a valid unterminated "
        f"final line, got {tailonly_lines}",
    )
    check(
        tui.landing.parse_status(str(tailonly_status))
        .get("org/repo#2", {})
        .get("state")
        == "merged",
        "the preserved final line must keep its fold",
    )

    # ── the ghcr probe preserves a denied mint in code (#375) ──────────
    # The probe owns the mint, so a 403 from the token endpoint is the
    # negative signal itself — executable proof that no shell pipeline can
    # mask it. A stub server plays ghcr; BLUEFIN_REVIEW_GHCR_BASE points
    # the probe at it.
    class StubGhcr(http.server.BaseHTTPRequestHandler):
        routes: dict = {}
        hits: list = []

        def do_GET(self):
            self.hits.append(self.path)
            for prefix, (status, body, extra) in self.routes.items():
                if self.path.startswith(prefix):
                    payload = json.dumps(body).encode()
                    self.send_response(status)
                    for key, value in extra.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    ghcr_stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubGhcr)
    threading.Thread(target=ghcr_stub.serve_forever, daemon=True).start()
    ghcr_base = f"http://127.0.0.1:{ghcr_stub.server_address[1]}"

    def ghcr_probe(*args: str) -> tuple[int, dict]:
        result = subprocess.run(
            [sys.executable, str(landing_py), "probe", *args],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "BLUEFIN_REVIEW_GHCR_BASE": ghcr_base},
        )
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            return result.returncode, {}

    StubGhcr.routes = {"/token": (403, {}, {})}
    code, answer = ghcr_probe("--package", "org/missing")
    check(
        code == 0 and answer.get("readable") is False,
        "a denied token mint is the definitive negative signal, not an "
        f"error and never masked as success, got {code} and {answer}",
    )
    check(
        "denied" in answer.get("signal", ""),
        f"the negative signal must name the denied mint, got {answer}",
    )
    StubGhcr.routes = {
        "/token": (200, {"token": "t"}, {}),
        "/v2/org/repo/tags/list": (200, {"tags": ["stable", "sha-abc"]}, {}),
    }
    code, answer = ghcr_probe("--package", "org/repo")
    check(
        code == 0 and answer.get("readable") is True
        and answer.get("tags") == ["stable", "sha-abc"],
        f"a public package answers its tags, got {code} and {answer}",
    )
    StubGhcr.routes = {
        "/token": (200, {"token": "t"}, {}),
        "/v2/org/repo/tags/list": (401, {}, {}),
    }
    code, answer = ghcr_probe("--package", "org/repo")
    check(
        code == 0 and answer.get("readable") is False,
        f"a 401/403 from tags/list is also the negative signal, got {answer}",
    )
    StubGhcr.routes = {
        "/token": (200, {"token": "t"}, {}),
        "/v2/org/paged/tags/list?": (
            200,
            {"tags": ["b"]},
            {},
        ),
        "/v2/org/paged/tags/list": (
            200,
            {"tags": ["a"]},
            {"Link": f'<{ghcr_base}/v2/org/paged/tags/list?last=a>; rel="next"'},
        ),
    }
    code, answer = ghcr_probe("--package", "org/paged")
    check(
        code == 0 and answer.get("tags") == ["a", "b"],
        f"the probe must follow the tags/list pagination cursor, got {answer}",
    )
    StubGhcr.routes = {
        "/token": (200, {"token": "t"}, {}),
        "/v2/org/repo/tags/list": (200, {"tags": ["stable"]}, {}),
        "/v2/org/repo/manifests/stable": (
            200,
            {"manifests": [{"digest": "sha256:1"}, {"digest": "sha256:2"}]},
            {"docker-content-digest": "sha256:0"},
        ),
        "/v2/org/repo/manifests/missing": (404, {}, {}),
    }
    code, answer = ghcr_probe("--package", "org/repo", "--manifest", "stable")
    check(
        code == 0
        and answer.get("digest") == "sha256:0"
        and answer.get("children") == ["sha256:1", "sha256:2"],
        f"the probe must resolve a manifest digest and an index's children, "
        f"got {answer}",
    )
    code, answer = ghcr_probe("--package", "org/repo", "--manifest", "missing")
    check(
        code == 0 and answer.get("present") is False,
        f"a missing ref answers absent, got {answer}",
    )
    # Registry paths are lowercase: a mixed-case package argument must be
    # normalized, or ghcr answers 400 and the probe can never resolve.
    StubGhcr.hits = []
    StubGhcr.routes = {
        "/token": (200, {"token": "t"}, {}),
        "/v2/org/repo/tags/list": (200, {"tags": ["stable"]}, {}),
    }
    code, answer = ghcr_probe("--package", "Org/Repo")
    check(
        code == 0 and answer.get("readable") is True,
        f"a mixed-case package must be lowercased before probing, got "
        f"{code} and {answer}",
    )
    check(
        StubGhcr.hits
        and "scope=repository:org/repo:pull" in StubGhcr.hits[0]
        and any(hit.startswith("/v2/org/repo/") for hit in StubGhcr.hits),
        f"the probe must send lowercase registry paths, got {StubGhcr.hits}",
    )
    dead = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubGhcr)
    dead_port = dead.server_address[1]
    dead.server_close()
    result = subprocess.run(
        [sys.executable, str(landing_py), "probe", "--package", "org/repo"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "BLUEFIN_REVIEW_GHCR_BASE": f"http://127.0.0.1:{dead_port}"},
    )
    check(
        result.returncode != 0 and json.loads(result.stdout).get("readable") is None,
        "an unreachable registry is a probe error, never evidence of "
        f"absence, got {result.returncode} and {result.stdout}",
    )
    ghcr_stub.shutdown()

    # ── the publish wait verdict: an empty run list is not evidence ─────
    # #376's stall, generalized: the wait decision is executable, keyed to
    # the identified publish workflow and its trigger, and only all-terminal
    # runs may end the wait without registry evidence.
    runs_path = workdir / "runs.json"

    def verdict_cli(trigger: str, workflow: str, runs) -> tuple[int, dict]:
        runs_path.write_text(json.dumps(runs))
        result = subprocess.run(
            [
                sys.executable, str(landing_py), "publish-verdict",
                "--trigger", trigger, "--workflow", workflow,
                "--runs", str(runs_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            return result.returncode, {}

    code, answer = verdict_cli("push", "publish", [])
    check(
        code == 0 and answer.get("verdict") == "wait",
        "an empty run list must answer wait — never 'no publication' — "
        f"got {answer}",
    )
    code, answer = verdict_cli(
        "push", "publish",
        [{"workflowName": "ci", "status": "in_progress", "conclusion": None}],
    )
    check(
        code == 0 and answer.get("verdict") == "wait",
        f"a non-terminal run must answer wait, got {answer}",
    )
    code, answer = verdict_cli(
        "push", "publish",
        [{"workflowName": "ci", "status": "completed", "conclusion": "success"}],
    )
    check(
        code == 0 and answer.get("verdict") == "no-publication-run",
        "all-terminal runs without the publish workflow must end the wait, "
        f"got {answer}",
    )
    code, answer = verdict_cli(
        "push", "publish",
        [
            {"workflowName": "ci", "status": "completed", "conclusion": "success"},
            {"workflowName": "publish", "status": "completed", "conclusion": "success"},
        ],
    )
    check(
        code == 0 and answer.get("verdict") == "verify-registry",
        f"a green publish run must route to registry evidence, got {answer}",
    )
    code, answer = verdict_cli(
        "push", "publish",
        [{"workflowName": "publish", "status": "completed", "conclusion": "failure"}],
    )
    check(
        code == 0 and answer.get("verdict") == "publish-failed",
        f"a red publish run must be named, got {answer}",
    )
    for soft in ("skipped", "cancelled", "neutral"):
        code, answer = verdict_cli(
            "push", "publish",
            [{"workflowName": "publish", "status": "completed", "conclusion": soft}],
        )
        check(
            code == 0 and answer.get("verdict") == "publish-skipped",
            f"a {soft} publish run published nothing and failed at nothing — "
            f"it must not read as a red run, got {answer}",
        )
    code, answer = verdict_cli(
        "push", "publish",
        [
            {"workflowName": "publish", "status": "completed", "conclusion": "skipped"},
            {"workflowName": "publish", "status": "completed", "conclusion": "success"},
        ],
    )
    check(
        code == 0 and answer.get("verdict") == "verify-registry",
        f"one green publish run wins over a skipped sibling, got {answer}",
    )
    code, answer = verdict_cli(
        "workflow_run", "publish",
        [{"workflowName": "publish", "status": "completed", "conclusion": "success"}],
    )
    check(
        code == 0 and answer.get("verdict") == "verify-registry",
        f"a workflow_run publish must be evaluated like a push one, got {answer}",
    )
    code, answer = verdict_cli("release", "publish", [])
    check(
        code == 0 and answer.get("verdict") == "not-owed",
        f"a release-triggered publish owes nothing for a merge, got {answer}",
    )
    runs_path.write_text("not json")
    result = subprocess.run(
        [
            sys.executable, str(landing_py), "publish-verdict",
            "--trigger", "push", "--workflow", "publish", "--runs", str(runs_path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    check(
        result.returncode != 0,
        f"a malformed runs file must fail the verdict, got {result.returncode}",
    )

    # ── concurrent reporters serialize under flock ─────────────────────
    race_status = workdir / "cli-race.jsonl"
    race_status.write_text('{"expect":[],"ts":1}\n')
    racers = [
        subprocess.Popen(
            [
                sys.executable, str(landing_py), "report", "--status",
                str(race_status), "event", "--pr", f"org/repo#{i}",
                "--state", "blocked", "--note", "race",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for i in range(1, 17)
    ]
    codes = [racer.wait(timeout=60) for racer in racers]
    race_lines = race_status.read_text().splitlines()
    check(
        all(code == 0 for code in codes) and len(race_lines) == 17,
        f"16 concurrent reporters must all land exactly one line, got "
        f"{codes} and {len(race_lines)} lines",
    )
    check(
        all(isinstance(json.loads(line), dict) for line in race_lines),
        "no concurrent write may interleave — every line must parse",
    )
    raced = tui.landing.parse_status(str(race_status))
    check(
        sum(1 for key in raced if key.startswith("org/repo#")) == 16,
        f"every concurrent event must survive the fold, got {len(raced)}",
    )

    # ── the final review-and-fix rounds (#378) ─────────────────────────
    # The policy is a session decision asked once, the classification picks
    # a model and nothing else, every round is its own process with its own
    # explicit model environment, and five rounds is the end of the line.
    dep_stops = [
        SimpleNamespace(key="o/r#1", title="chore(deps): bump x", labels=[]),
        SimpleNamespace(key="o/r#2", title="build(deps-dev): bump y", labels=[]),
        SimpleNamespace(key="o/r#3", title="anything at all", labels=["dependencies"]),
    ]
    mixed_stops = dep_stops + [
        SimpleNamespace(key="o/r#4", title="feat: a new thing", labels=[])
    ]
    check(
        tui.landing.classify_batch(dep_stops) == "dependency",
        "a chore/deps-only batch must classify as dependency",
    )
    check(
        tui.landing.classify_batch(mixed_stops) == "mixed",
        "one feature makes the batch mixed",
    )
    check(
        tui.landing.classify_batch(
            [SimpleNamespace(key="o/r#9", title="unreadable", labels=[])]
        )
        == "mixed",
        "an unknown title must fall back to mixed classification, not "
        "dependency",
    )
    check(
        tui.landing.classify_batch([]) == "mixed",
        "an empty batch must not classify as dependency",
    )
    check(
        tui.landing.final_triple("automatic", "mixed", "final-review")[1]
        == "gemini-3.8-flash"
        and tui.landing.final_triple("automatic", "dependency", "final-review")[1]
        == "kimi-k3"
        and tui.landing.final_triple("gemini", "mixed", "final-review")[1]
        == "gemini-3.8-flash"
        and tui.landing.final_triple("opus", "dependency", "final-review")[1]
        == "claude-opus-5"
        and tui.landing.final_triple("sol", "mixed", "final-review")[1]
        == "gpt-5.6-sol"
        and tui.landing.final_triple("gpt-sol", "mixed", "final-review")[1]
        == "gpt-5.6-sol"
        and tui.landing.final_triple("kimi", "mixed", "final-review")[1] == "kimi-k3"
        and tui.landing.final_triple("k3", "mixed", "final-review")[1] == "kimi-k3"
        and tui.landing.final_triple("opus", "mixed", "fixing")[1] == "kimi-k3",
        "the policy/classification table must pick the documented models",
    )
    goose_env = tui.landing.final_environment(tui.landing.OPUS_TRIPLE, "goose")
    codex_env = tui.landing.final_environment(tui.landing.OPUS_TRIPLE, "codex")
    check(
        goose_env.get("GOOSE_MODEL") == "claude-opus-5"
        and goose_env.get("GOOSE_THINKING_EFFORT") == "high",
        f"a Goose round must carry its model explicitly, got {goose_env}",
    )
    check(
        "GOOSE_MODEL" not in codex_env
        and codex_env.get("BLUEFIN_REVIEW_BACKEND") == "codex",
        "a Codex round must not be handed Goose variables that do nothing, "
        f"got {codex_env}",
    )
    override = os.environ.pop("BLUEFIN_REVIEW_LANDING_COMMAND", "")
    codex_argv = tui.landing.final_command(
        "/tmp/round.md", tui.landing.OPUS_TRIPLE, "codex"
    )
    goose_argv = tui.landing.final_command(
        "/tmp/round.md", tui.landing.OPUS_TRIPLE, "goose"
    )
    if override:
        os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = override
    check(
        "--model" in codex_argv and "claude-opus-5" in codex_argv,
        f"a Codex round must take its model on the command line, got {codex_argv}",
    )
    check(
        goose_argv[:1] == ["goose"] and "--model" not in goose_argv,
        f"a Goose round takes its model from the environment, got {goose_argv}",
    )

    final_status = workdir / "final.jsonl"
    final_status.write_text("")

    def final_report(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, str(landing_py), "report", "--status",
                str(final_status), "final", *args,
            ],
            capture_output=True, text=True, timeout=30,
        )

    head = "b" * 40
    result = final_report(
        "--round", "1", "--phase", "final-review", "--model", "claude-opus-5",
        "--input-head", head, "--note", "one finding",
    )
    check(result.returncode == 0, f"a first round must record: {result.stderr}")
    check(
        tui.landing.final_phase(str(final_status)).get("phase") == "final-review",
        "the record must carry the round's phase",
    )
    result = final_report(
        "--round", "2", "--phase", "fixing", "--model", "kimi-k3",
        "--input-head", "abc", "--note", "bad head",
    )
    check(
        result.returncode != 0 and "40-character" in result.stderr,
        f"a short head must fail closed before any write, got {result}",
    )
    result = final_report(
        "--round", str(tui.landing.FINAL_ROUND_LIMIT + 1), "--phase", "re-review",
        "--model", "kimi-k3", "--note", "one more",
    )
    check(
        result.returncode != 0,
        "the breaker must refuse a round past the limit in the record itself",
    )
    final_report(
        "--round", "5", "--phase", "review-blocked", "--model", "kimi-k3",
        "--note", "two findings remain",
    )
    result = final_report(
        "--round", "5", "--phase", "cleanup", "--model", "kimi-k3", "--note", "late",
    )
    check(
        result.returncode != 0 and "already review-blocked" in result.stderr,
        f"a blocked batch must not be quietly walked back, got {result}",
    )
    blocked = tui.landing.final_phase(str(final_status))
    check(
        blocked.get("phase") == "review-blocked"
        and blocked.get("note") == "two findings remain",
        f"the blocked verdict must keep its findings, got {blocked}",
    )

    # The gate: one prompt per dashboard session, before the first dispatch,
    # and changeable afterwards with [P].
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    app.final_policy = None
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.FinalPolicyScreen),
            f"the first batch must ask the policy once, got "
            f"{type(app.screen).__name__}",
        )
        gate_text = " ".join(
            str(node.render())
            for node in app.screen.query(tui.Static)
        )
        check(
            "never" in gate_text and "branch protection" in gate_text,
            f"the gate must state what a review round may not do, got {gate_text!r}",
        )
        await pilot.press("5")
        await pilot.pause()
        check(
            app.final_policy == "kimi",
            f"the chosen policy must be the session's, got {app.final_policy!r}",
        )
        check(
            isinstance(app.screen, tui.BatchPlanScreen),
            f"choosing must continue to the batch plan, got "
            f"{type(app.screen).__name__}",
        )
        await pilot.press("escape")
        await pilot.pause()
        app.action_land_batch()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.BatchPlanScreen),
            "a second batch must not ask the policy again",
        )
        await pilot.press("escape")
        await pilot.pause()
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "review: kimi" in status,
            f"the status bar must show the session policy, got {status!r}",
        )
        app.action_review_policy()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.FinalPolicyScreen),
            "[P] must reopen the policy gate",
        )
        await pilot.press("1")
        await pilot.pause()
        check(app.final_policy == "automatic", "the policy must be changeable")

    # ── the lab is optional, coarse, and never load-bearing (#379) ──────
    check(
        tui.lab_client.lab_state({}) == "OFF"
        and tui.lab_client.lab_state({"ok": False, "error": "unreachable"})
        == "DEGRADED"
        and tui.lab_client.lab_state({"ok": True, "state": "READY"}) == "READY",
        "an unreachable broker must degrade, never look like a clean answer",
    )
    fresh = {"ghost": "up", "exo-0": "up", "fresh": True}
    check(
        tui.lab_client.lab_state(
            {"ok": True, "state": "READY", "active": True, "usb4": fresh}
        )
        == "ACTIVE",
        "a Review-bound workflow with both links fresh and up is ACTIVE",
    )
    for name, usb4 in (
        ("stale", {"ghost": "up", "exo-0": "up", "fresh": False}),
        ("one link down", {"ghost": "up", "exo-0": "down", "fresh": True}),
        ("unknown link", {"ghost": "unknown", "exo-0": "up", "fresh": True}),
        ("malformed", {}),
    ):
        check(
            tui.lab_client.lab_state(
                {"ok": True, "state": "READY", "active": True, "usb4": usb4}
            )
            == "READY",
            f"{name} must drop the bolt back to READY",
        )
    check(
        tui.lab_client.lab_state(
            {"ok": True, "state": "READY", "active": False, "usb4": fresh}
        )
        == "READY",
        "idle work must drop the bolt even with both links fresh",
    )
    check(
        tui.lab_client.lab_state({"ok": True, "state": "DEGRADED"}) == "DEGRADED",
        "a degraded broker stays degraded",
    )
    os.environ.pop("BLUEFIN_REVIEW_LAB_SOCKET", None)
    check(
        not tui.lab_client.lab_configured()
        and tui.lab_client.status().get("state") == "OFF",
        "no socket means OFF, and asking anyway must not raise",
    )
    os.environ["BLUEFIN_REVIEW_LAB_SOCKET"] = str(workdir / "not-a-socket")
    check(
        not tui.lab_client.lab_configured(),
        "a socket path that does not exist must not advertise a lab",
    )
    (workdir / "not-a-socket").write_text("")
    degraded = tui.lab_client.status()
    check(
        degraded.get("ok") is False and degraded.get("state") == "DEGRADED",
        f"a dead socket must degrade rather than answer, got {degraded}",
    )
    os.environ.pop("BLUEFIN_REVIEW_LAB_SOCKET", None)
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "LAB OFF" in status,
            f"a dashboard with no lab must say LAB OFF, got {status!r}",
        )
        app.lab_polled("ACTIVE", "workflow running")
        await pilot.pause()
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "LAB ⚡ ACTIVE" in status and "ACTIVE" in status,
            f"the bolt must decorate the word, never replace it, got {status!r}",
        )
        app.lab_polled("DEGRADED", "broker timeout")
        await pilot.pause()
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "LAB DEGRADED" in status and "⚡" not in status,
            f"a coarse poll must be able to remove the bolt, got {status!r}",
        )

    # ── merging without lgtm is a maintainer power ───────────────────────
    # lgtm is an opt-in to Hive's automation, not a toll on merging: a
    # maintainer can land a pull request directly. Someone without the push
    # permission cannot, and must be told so rather than shown a gate.
    for allowed in (False, True):
        perm_file.write_text("true\n" if allowed else "false\n")
        app = tui.ReviewDashboard(tui.QueueFilters(action=""))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            stop = app.stops[0]
            for _ in range(200):
                if stop.repository in app.merge_rights:
                    break
                await pilot.pause(0.05)
            check(
                app.merge_rights.get(stop.repository) is allowed,
                "the merge permission must be read from GitHub, got "
                f"{app.merge_rights.get(stop.repository)!r} for push={allowed}",
            )
            stop.live = {"isDraft": False}
            gh_log.write_text("")
            await pilot.press("m")
            await pilot.pause()
            gated = isinstance(app.screen, tui.ConfirmMutation)
            check(
                gated is allowed,
                "merging directly must be gated for a maintainer and refused "
                f"otherwise; push={allowed} produced gate={gated}",
            )
            if not allowed:
                check(
                    "pr merge" not in gh_log.read_text(),
                    "a non-maintainer must not reach 'gh pr merge'",
                )
                continue
            gate = app.screen
            check(
                [c[:3] for c in gate.commands] == [["gh", "pr", "merge"]],
                f"[m] must merge directly, got {gate.commands}",
            )
            check(
                "--squash" in gate.commands[0],
                f"the direct merge must squash, got {gate.commands[0]}",
            )
            check(
                "--admin" not in gate.commands[0]
                and "--delete-branch" not in gate.commands[0],
                f"the direct merge must not bypass or delete, got {gate.commands[0]}",
            )
            await pilot.press(*gate.expected)
            await pilot.press("enter")
            for _ in range(200):
                if "pr merge" in gh_log.read_text():
                    break
                await pilot.pause(0.05)
            merged = [
                line for line in gh_log.read_text().splitlines()
                if line.startswith("pr merge")
            ]
            check(
                len(merged) == 1 and "--squash" in merged[0],
                f"the confirmed merge must run exactly once, got {merged}",
            )
            check(
                "--add-label lgtm" not in gh_log.read_text(),
                "merging directly must not apply the lgtm automation opt-in",
            )
    perm_file.write_text("true\n")
    gh_log.write_text("")

    # ── asking Hive is easy, read-only, and never fatal ──────────────────
    # The status line used to say "Hive: not consulted" permanently, which is
    # a dashboard that never asked. It asks now, and a stop Hive is actively
    # working on says so — the diff on screen is about to be stale.
    hive_calls = workdir / "hive.log"

    class FakeHive:
        def __init__(self, status, contributors):
            self.status = status
            self.contributors = contributors

        def __call__(self, path):
            with open(hive_calls, "a") as sink:
                sink.write(path + "\n")
            if path.endswith("status"):
                data = self.status
            elif path.endswith("queue"):
                data = {"queue": []}
            elif path.endswith("triage"):
                data = {"groups": []}
            else:
                data = self.contributors
            return tui.hive_api.Result(True, "ok", "online", data)

    class FlappingHive(FakeHive):
        def __init__(self):
            super().__init__(
                {"hub": "online", "actionable_items": 185},
                {
                    "contributors": [
                        {
                            "github_username": "someone-else",
                            "current_task": {
                                "task_id": "ct-1",
                                "repo": "projectbluefin/bluefinctl",
                                "number": 31,
                            },
                        }
                    ],
                },
            )
            self.online = True
            self.calls = []

        def __call__(self, path):
            self.calls.append(path)
            if not self.online:
                return tui.hive_api.Result(False, "network", "network error", {})
            return super().__call__(path)

    real_hive_get = tui.hive_get
    real_base = tui.hive_api_base
    original_hive_hub = os.environ.get("HIVE_HUB")
    os.environ["HIVE_HUB"] = "wss://hub.example/contribute"
    check(
        tui.hive_api_base() == "https://hub.example",
        "a secure contributor URL must become the Hive HTTPS API root",
    )
    for unsafe_hub in (
        "ws://hub.example/contribute",
        "http://hub.example",
        "https://user@hub.example",
        "https://[",
        "wss://one.example/contribute,wss://two.example/contribute",
    ):
        os.environ["HIVE_HUB"] = unsafe_hub
        check(
            tui.hive_api_base() == "",
            f"unsafe Hive URL must be rejected before token use: {unsafe_hub}",
        )
    if original_hive_hub is None:
        os.environ.pop("HIVE_HUB", None)
    else:
        os.environ["HIVE_HUB"] = original_hive_hub
    tui.hive_api_base = lambda: "https://hub.example"
    tui.hive_get = FakeHive(
        {"hub": "online", "actionable_items": 185},
        {
            "contributors": [
                {
                    "github_username": "someone-else",
                    "current_task": {
                        "task_id": "ct-1",
                        "repo": "projectbluefin/bluefinctl",
                        "number": 31,
                    },
                },
                {"github_username": "idle", "current_task": None},
            ]
        },
    )
    try:
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.hive_state and app.stops:
                    break
                await pilot.pause(0.05)
            check(
                "online" in app.hive_state and "185 actionable" in app.hive_state,
                f"the status line must report what Hive said, got {app.hive_state!r}",
            )
            check(
                "not consulted" not in str(
                    app.query_one("#status-bar", tui.Static).render()
                ),
                "the dashboard must not claim Hive is unconsulted after asking",
            )
            check(
                len(app.hive_workers) == 1,
                f"only in-flight tasks count as working, got {app.hive_workers}",
            )
            stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
            other_stop = next((s for s in app.stops if s.key != "projectbluefin/bluefinctl#31"), app.stops[1])
            worker = app.hive_worker_for(stop)
            check(
                worker is not None and worker["login"] == "someone-else",
                f"a stop Hive is working on must be identified, got {worker}",
            )
            check(
                app.hive_worker_for(other_stop) is None,
                "a stop nobody is working on must not claim a worker",
            )
            app._queue().index = app.stops.index(stop)
            app.render_context(stop)
            for _ in range(200):
                if "is working on THIS" in str(
                    app.query_one("#context", tui.Static).render()
                ):
                    break
                await pilot.pause(0.05)
            check(
                "is working on THIS" in str(
                    app.query_one("#context", tui.Static).render()
                ),
                "the context pane must warn that Hive is changing this PR now",
            )
            check(
                {"/api/v1/status", "/api/v1/contributors"}
                <= set(hive_calls.read_text().split()),
                f"asking Hive must read status and contributors, got "
                f"{hive_calls.read_text().split()}",
            )
            # Read-only: consulting Hive must never mutate GitHub or Hive.
            check(
                "pr merge" not in gh_log.read_text()
                and "pr review" not in gh_log.read_text(),
                "consulting Hive must not mutate anything",
            )

        # A hub can disappear after a successful probe. Keep the last-known
        # assignment visible as stale evidence, say that current assignment
        # state is unknown, and leave direct GitHub merge actions available.
        flapping_hive = FlappingHive()
        tui.hive_get = flapping_hive
        stale_app = tui.ReviewDashboard(tui.QueueFilters())
        async with stale_app.run_test() as pilot:
            for _ in range(200):
                if stale_app.hive_state and stale_app.stops:
                    break
                await pilot.pause(0.05)
            check(
                stale_app.hive_workers,
                "the outage regression needs a last-known Hive assignment",
            )
            flapping_hive.online = False
            stale_app.load_hive()
            for _ in range(200):
                if getattr(stale_app, "hive_unavailable", False):
                    break
                await pilot.pause(0.05)
            check(
                stale_app.hive_unavailable,
                "Hive outage must be explicit after a failed refresh",
            )
            check(
                stale_app.hive_workers_stale and stale_app.hive_workers,
                "Hive outage must retain last-known workers as stale evidence",
            )
            failed_probe_calls = len(flapping_hive.calls)
            await pilot.pause(0.5)
            check(
                len(flapping_hive.calls) == failed_probe_calls,
                "Hive outage must not trigger automatic probe retries",
            )
            check(
                stale_app.hive_worker_for(stale_app.stops[0]) is None,
                "stale workers must not be presented as current assignments",
            )
            status = str(stale_app.query_one("#status-bar", tui.Static).render())
            check(
                "Hive: unavailable — network error" in status
                and "last-known assignments retained" in status,
                f"status must explain the outage without hiding retained evidence: {status!r}",
            )
            stop_assigned = next((s for s in stale_app.stops if s.key == "projectbluefin/bluefinctl#31"), stale_app.stops[0])
            stale_app._queue().index = stale_app.stops.index(stop_assigned)
            stale_app.render_context(stop_assigned)
            for _ in range(200):
                context = str(stale_app.query_one("#context", tui.Static).render())
                if "last known Hive assignment" in context:
                    break
                await pilot.pause(0.05)
            check(
                "last known Hive assignment" in context
                and "current assignment is unknown" in context,
                f"context must mark stale Hive evidence explicitly: {context!r}",
            )
            stop = stop_assigned
            stop.live = {
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }
            stale_app.merge_rights[stop.repository] = True
            stale_app.action_merge_now()
            await pilot.pause()
            check(
                isinstance(stale_app.screen, tui.ConfirmMutation),
                "Hive outage must not block the direct GitHub merge gate",
            )
            if isinstance(stale_app.screen, tui.ConfirmMutation):
                await pilot.press("escape")

        # An unreachable hub degrades to a plain statement, never a crash.
        hive_failure_states = {
            "authentication token missing": "authentication token missing",
            "network error": "network error",
            "authentication rejected (401)": "authentication rejected (401)",
            "authorization rejected (403)": "authorization rejected (403)",
            "API routing redirected (302)": "API routing redirected (302)",
            "malformed API response": "malformed API response",
            "Hive server error (503)": "Hive server error (503)",
        }
        for message, expected_state in hive_failure_states.items():
            tui.hive_get = lambda path, message=message: tui.hive_api.Result(
                False, "test", message, {}
            )
            app = tui.ReviewDashboard(tui.QueueFilters())
            async with app.run_test() as pilot:
                await pilot.pause()
                for _ in range(200):
                    if app.hive_state:
                        break
                    await pilot.pause(0.05)
                check(
                    app.hive_state == expected_state,
                    f"Hive failure must be actionable, got {app.hive_state!r}",
                )
                check(app.stops, "a Hive failure must not empty the queue")

        # No hub configured at all is its own honest answer.
        tui.hive_api_base = lambda: ""
        app = tui.ReviewDashboard(tui.QueueFilters())
        app._request_reconciliation = reconciliation_request.__get__(
            app, tui.ReviewDashboard
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.hive_state and app.stops:
                    break
                await pilot.pause(0.05)
            check(
                app.hive_state == "not configured",
                f"no hub must read as not configured, got {app.hive_state!r}",
            )
            activity = app.query_one("#activity", tui.Static)
            activity_text = str(activity.render())
            check(
                "Snapshot: current" in activity_text
                and "retained/last good" not in activity_text
                and "Hive assignments unavailable" not in activity_text,
                "an unconfigured Hive must leave the live queue snapshot "
                f"honest, got {activity_text!r}",
            )
            app._request_reconciliation()
            for _ in range(200):
                if not app._reconciliation_waiting:
                    break
                await pilot.pause(0.05)
            activity_text = str(activity.render())
            check(
                app.reconciliation_state == "fresh"
                and "Snapshot: current" in activity_text
                and "retained/last good" not in activity_text
                and "Hive assignments unavailable" not in activity_text,
                "an operation refresh without Hive must retain honest live "
                f"queue freshness, got {activity_text!r}",
            )
    finally:
        tui.hive_get = real_hive_get
        tui.hive_api_base = real_base
    gh_log.write_text("")

    # ── the diff is coloured, scrollable, and whole ──────────────────────
    # It used to be plain text pasted into the evidence pane and cut at 20 000
    # characters with no sign it had been cut.
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        await pilot.press("v")
        await pilot.pause()
        screen = app.screen
        check(
            isinstance(screen, tui.DiffScreen),
            f"'v' must open the diff screen, got {type(screen).__name__}",
        )
        if isinstance(screen, tui.DiffScreen):
            for _ in range(200):
                if screen.rendered is not None:
                    break
                await pilot.pause(0.05)
            check(
                isinstance(screen.rendered, tui.Syntax),
                f"the diff must be syntax-highlighted, got {type(screen.rendered)}",
            )
            check(
                getattr(getattr(screen.rendered, "lexer", None), "name", "") == "Diff",
                "the diff must use Pygments' diff lexer, so +/- are coloured",
            )
            check(
                "+new" in getattr(screen.rendered, "code", ""),
                "the diff screen must show the diff it fetched",
            )
            check(
                screen.query("#diff-scroll"),
                "the diff must live in a scrollable container",
            )
            # Every fetch state must travel through load_diff(), including the
            # oversized stdout and terminal error paths production uses.
            os.environ["DIFF_MODE"] = "oversized"
            screen.load_diff()
            for _ in range(400):
                if screen.page_count == 2:
                    break
                await pilot.pause(0.05)
            check(
                screen.page_count == 2 and screen.page_index == 0,
                "an oversized diff must expose bounded pages",
            )
            await pilot.press("]")
            await pilot.pause()
            check(
                screen.page_index == 1
                and screen.rendered is not None
                and "x" * 10 in screen.rendered.code[-20:],
                "the complete oversized diff must be reachable on its final page",
            )
            await pilot.press("]")
            check(screen.page_index == 1, "last-page navigation must be a no-op")
            await pilot.press("[")
            await pilot.press("[")
            check(screen.page_index == 0, "first-page navigation must be a no-op")
            selected_stop = screen.stop_record
            old_request_id = f"old-{workdir.name}"
            fast_request_id = f"fast-{workdir.name}"
            os.environ["DIFF_REQUEST_ID"] = old_request_id
            os.environ["DIFF_MODE"] = "slow-old"
            screen.load_diff()
            for _ in range(200):
                if old_request_started.exists():
                    break
                await pilot.pause(0.01)
            check(
                old_request_started.exists(),
                "the old diff request must acknowledge entering its delay",
            )
            os.environ["DIFF_REQUEST_ID"] = fast_request_id
            os.environ["DIFF_MODE"] = "fast-new"
            screen.load_diff()
            for _ in range(200):
                events = diff_events.read_text().splitlines() if diff_events.exists() else []
                if any(f"response:{fast_request_id}:" in event for event in events):
                    break
                await pilot.pause(0.05)
            events = diff_events.read_text().splitlines() if diff_events.exists() else []
            check(
                any(f"request:{old_request_id}:slow-old" in event for event in events)
                and any(f"request:{fast_request_id}:fast-new" in event for event in events),
                "the stale-diff test must record two distinct requests",
            )
            check(
                any(f"response:{fast_request_id}:NEW-DIFF" in event for event in events),
                "the new diff response must identify the new request",
            )
            for _ in range(200):
                events = diff_events.read_text().splitlines() if diff_events.exists() else []
                if any(f"response:{old_request_id}:OLD-DIFF" in event for event in events):
                    break
                await pilot.pause(0.01)
            events = diff_events.read_text().splitlines() if diff_events.exists() else []
            check(
                any(f"response:{old_request_id}:OLD-DIFF" in event for event in events),
                "the delayed old diff response must complete",
            )
            fast_response = f"response:{fast_request_id}:NEW-DIFF"
            old_response = f"response:{old_request_id}:OLD-DIFF"
            check(
                fast_response in events
                and old_response in events
                and events.index(fast_response) < events.index(old_response),
                "the old diff response must complete after the new response",
            )
            check(
                screen.rendered is not None
                and "NEW-DIFF" in screen.rendered.code
                and "OLD-DIFF" not in screen.rendered.code,
                "a stale diff response must not overwrite the current selection",
            )
            check(
                screen.page_index == 0 and screen.page_count == 1,
                "a stale diff response must not overwrite page state",
            )
            check(
                screen.stop_record is selected_stop,
                "a stale diff response must not overwrite selection state",
            )
            os.environ["DIFF_MODE"] = "empty"
            screen.load_diff()
            for _ in range(200):
                if screen.state == "success" and not screen.pages:
                    break
                await pilot.pause(0.05)
            check(
                screen.state == "success"
                and "(empty diff)" in str(screen.query_one("#diff-body", tui.Static).render()),
                "an empty diff must be a successful empty state",
            )
            os.environ["DIFF_MODE"] = "error"
            screen.load_diff()
            for _ in range(200):
                if screen.state == "error":
                    break
                await pilot.pause(0.05)
            await pilot.pause()
            check(
                screen.state == "error"
                and "terminal diff failure" in str(screen.query_one("#diff-body", tui.Static).render()),
                "a diff fetch error must be distinct from a loaded diff",
            )
            await pilot.press("escape")
            await pilot.pause()
            check(
                not isinstance(app.screen, tui.DiffScreen),
                "escape must close the diff screen",
            )
        check(
            "pr diff" in gh_log.read_text(),
            "the diff screen must actually fetch the diff",
        )
    os.environ.pop("DIFF_MODE", None)
    os.environ.pop("DIFF_REQUEST_ID", None)
    gh_log.write_text("")

    # ── comments viewer ──────────────────────────────────────────────────
    sample_thread = {
        "title": "Add comments viewer",
        "author": {"login": "castrojo"},
        "createdAt": "2026-09-07T12:00:00Z",
        "body": "Opening description",
        "comments": [
            {
                "author": {"login": "reviewer1"},
                "createdAt": "2026-09-07T12:05:00Z",
                "body": "First comment",
            }
        ],
        "reviews": [
            {
                "author": {"login": "reviewer2"},
                "createdAt": "2026-09-07T12:10:00Z",
                "state": "APPROVED",
                "body": "LGTM",
            }
        ],
    }
    rendered_thread = tui.format_github_thread(sample_thread, "projectbluefin/review#42")
    check("# projectbluefin/review#42: Add comments viewer" in rendered_thread, "thread header must render")
    check("**@castrojo** opened on 2026-09-07 12:00:00 UTC:" in rendered_thread, "author and timestamp must render")
    check("Opening description" in rendered_thread, "body must render")
    check("### **@reviewer1** commented on 2026-09-07 12:05:00 UTC:" in rendered_thread, "comment header must render")
    check("First comment" in rendered_thread, "comment body must render")
    check("### **@reviewer2** (APPROVED) on 2026-09-07 12:10:00 UTC:" in rendered_thread, "review header must render")
    check("LGTM" in rendered_thread, "review body must render")

    empty_thread = {"title": "Empty", "author": {"login": "bot"}, "createdAt": "2026-09-07T00:00:00Z", "body": ""}
    rendered_empty = tui.format_github_thread(empty_thread, "test#1")
    check("*No comments or reviews yet.*" in rendered_empty, "empty thread must report no comments")
    check("*No description provided.*" in rendered_empty, "empty body must report no description")

    # A bodyless approval is itself the verdict and must survive; a bodyless
    # plain comment carries nothing and must not become an empty entry.
    verdict_thread = {
        "title": "Verdicts",
        "author": {"login": "bot"},
        "createdAt": "2026-09-07T00:00:00Z",
        "body": "x",
        "reviews": [
            {"author": {"login": "approver"}, "submittedAt": "2026-09-07T01:00:00Z",
             "state": "APPROVED", "body": ""},
            {"author": {"login": "noisy"}, "submittedAt": "2026-09-07T02:00:00Z",
             "state": "COMMENTED", "body": ""},
        ],
    }
    rendered_verdicts = tui.format_github_thread(verdict_thread, "test#2")
    check("**@approver** (APPROVED)" in rendered_verdicts, "a bodyless approval must still render")
    check("@noisy" not in rendered_verdicts, "a bodyless plain review comment must be dropped")

    # Ordering is by timestamp across both comments and reviews, not by kind.
    interleaved = {
        "title": "Order", "author": {"login": "bot"},
        "createdAt": "2026-09-07T00:00:00Z", "body": "x",
        "comments": [{"author": {"login": "late"}, "createdAt": "2026-09-07T09:00:00Z", "body": "later"}],
        "reviews": [{"author": {"login": "early"}, "submittedAt": "2026-09-07T03:00:00Z",
                     "state": "COMMENTED", "body": "earlier"}],
    }
    rendered_order = tui.format_github_thread(interleaved, "test#3")
    check(
        rendered_order.index("@early") < rendered_order.index("@late"),
        "the thread must be ordered by timestamp across comments and reviews",
    )

    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        await pilot.press("C")
        await pilot.pause()
        screen = app.screen
        check(
            isinstance(screen, tui.CommentsScreen),
            f"'C' must open the comments screen from dashboard, got {type(screen).__name__}",
        )
        if isinstance(screen, tui.CommentsScreen):
            check(screen.query("#comments-scroll"), "the comments must live in a scrollable container")
            check(screen.query("#comments-body"), "comments body widget must exist")
        await pilot.press("escape")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.CommentsScreen),
            "escape must close the comments screen",
        )

    # ── everything identifying a pull request is a hyperlink ─────────────
    # And the bug found while adding them: Rich reads a bracket as markup, so
    # the unescaped "[review]" action tag and any title carrying "[skip ci]"
    # were being silently eaten before they reached the screen.
    check(
        tui.pr_url("o/r", 7) == "https://github.com/o/r/pull/7",
        "pull request links must point at the pull request",
    )
    check(
        tui.issue_url("o/r", 7) == "https://github.com/o/r/issues/7",
        "issue links must point at the issue, not the pull request",
    )
    check(
        tui.link("a[b]c", "https://x") == '[link="https://x"]a\\[b]c[/link]',
        f"link() must escape its text, got {tui.link('a[b]c', 'https://x')!r}",
    )
    # Neither rich's nor Textual's escape covers an uppercase tag, but the
    # renderer eats one all the same: "[WIP] fix" lost its prefix.
    from textual.content import Content as _Content

    for raw in ("[WIP] fix the thing", "([H] asks again)", "a [review] b", "100% [done]"):
        check(
            _Content.from_markup(tui.escape(raw)).plain == raw,
            f"escape() must survive the markup parser: {raw!r} became "
            f"{_Content.from_markup(tui.escape(raw)).plain!r}",
        )

    set_org_queue([
        {
            "repository": "projectbluefin/bluefinctl",
            "number": 31,
            "recommended_action": "review",
            "title": "fix: [skip ci] guard the release",
            "author": "someone-else",
        }
    ])
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        rendered = (
            app.query_one("#queue", tui.ListView)
            .children[0]
            .query_one(tui.Label)
            .render()
        )
        row = str(rendered)
        row_links = " ".join(str(span.style) for span in rendered.spans)
        check(
            "[skip ci]" in row,
            f"a bracketed title must survive to the screen, got {row!r}",
        )
        check(
            "[review]" in row,
            f"the action tag must survive to the screen, got {row!r}",
        )
        check(
            "https://github.com/projectbluefin/bluefinctl/pull/31" in row_links,
            f"each queue row must link to its pull request, got {row_links!r}",
        )
        app.stops[0].live = {
            "isDraft": False,
            "closingIssuesReferences": [{"number": 12}],
            "labels": [{"name": "kind/bug"}],
            "author": {"login": "someone-else"},
        }
        app.render_evidence(app.stops[0])
        await pilot.pause()
        rendered_details = app.query_one("#details", tui.Static).render()
        details = " ".join(str(span.style) for span in rendered_details.spans)
        check(
            "https://github.com/projectbluefin/bluefinctl/pull/31" in details,
            "the evidence pane must link the pull request",
        )
        check(
            "https://github.com/projectbluefin/bluefinctl/issues/12" in details,
            f"a linked issue must be an issue hyperlink, got {details!r}",
        )
        check(
            "https://github.com/someone-else" in details,
            "the author must link to their GitHub profile",
        )
    gh_log.write_text("")
    set_org_queue(SNAPSHOT["items"])

    # ── who has reviewed, and whether their word carries write access ────
    check(
        tui.reviewer_standing("MEMBER") == "maintainer"
        and tui.reviewer_standing("OWNER") == "maintainer"
        and tui.reviewer_standing("COLLABORATOR") == "maintainer"
        and tui.reviewer_standing("CONTRIBUTOR") == "community"
        and tui.reviewer_standing("NONE") == "community",
        "author association must separate maintainers from the community",
    )
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
        stop.live = {
            "isDraft": False,
            "baseRefOid": "a" * 40,
            "headRefOid": "b" * 40,
            "reviews": [
                {
                    "author": {"login": "hanthor"},
                    "authorAssociation": "MEMBER",
                    "state": "APPROVED",
                },
                {
                    "author": {"login": "passerby"},
                    "authorAssociation": "CONTRIBUTOR",
                    "state": "CHANGES_REQUESTED",
                },
            ],
        }
        app.render_evidence(stop)
        await pilot.pause()
        details = str(app.query_one("#details", tui.Static).render())
        for expected in (
            "reviews  2",
            "1 maintainer",
            "1 community",
            "hanthor",
            "APPROVED",
            "passerby",
            "CHANGES_REQUESTED",
        ):
            check(
                expected in details,
                f"the evidence must show {expected!r}, got {details!r}",
            )
        stop.live["reviews"] = []
        stop.live["statusCheckRollup"] = [
            {"workflowName": "ci", "name": "build", "conclusion": "FAILURE", "detailsUrl": "https://github.com/runs/123", "startedAt": "2026-09-06T00:00:00Z", "completedAt": "2026-09-06T00:05:00Z"},
        ]
        app.render_evidence(stop)
        await pilot.pause()
        details_with_ci = str(app.query_one("#details", tui.Static).render())
        check(
            "CI FAILURE TRIAGE" in details_with_ci and "build" in details_with_ci and "https://github.com/runs/123" in details_with_ci,
            "failing CI must render an evidence-first triage card",
        )
        check(
            "reviews  none yet" in details_with_ci,
            "an unreviewed pull request must say so plainly",
        )

    # ── leaving a review: a verdict without a merge ──────────────────────
    for verdict_key, flag in (("1", "--approve"), ("2", "--request-changes"), ("3", "--comment")):
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            gh_log.write_text("")
            await pilot.press("L")
            await pilot.pause()
            check(
                isinstance(app.screen, tui.ReviewVerdict),
                f"[L] must offer a verdict, got {type(app.screen).__name__}",
            )
            await pilot.press(verdict_key)
            await pilot.pause()
            check(
                isinstance(app.screen, tui.ReviewBody),
                f"a verdict must ask for a reason, got {type(app.screen).__name__}",
            )
            await pilot.press("n", "o", "p", "e")
            await pilot.press("ctrl+p")
            await pilot.pause()
            check(isinstance(app.screen, tui.ReviewBodyPreview),
                  "a review body must be previewed before submission")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            check(
                isinstance(app.screen, tui.ConfirmMutation),
                "leaving a review must reach the typed-number gate",
            )
            gate = app.screen
            check(
                flag in gate.commands[0] and gate.commands[0][:3] == ["gh", "pr", "review"],
                f"the review must carry {flag}, got {gate.commands[0]}",
            )
            check(
                "--add-label" not in gate.commands[0],
                "leaving a review must not apply the lgtm automation opt-in",
            )
            await pilot.press(*gate.expected)
            await pilot.press("enter")
            for _ in range(200):
                if "pr review" in gh_log.read_text():
                    break
                await pilot.pause(0.05)
            check(
                flag in gh_log.read_text(),
                f"the confirmed review must run with {flag}, got {gh_log.read_text()!r}",
            )
            check(
                "pr merge" not in gh_log.read_text(),
                "leaving a review must never merge",
            )

    # ── editable, generated review bodies ───────────────────────────────
    exact_markdown = "## Résumé\n\n- `literal [text]`\n- Unicode: café ☕\n\n\nfinal"
    draft_calls = []
    original_backend = tui.ACTIVE_BACKEND
    original_draft = tui.CodexHarness.draft
    original_probe = tui.CodexHarness.probe

    def draft_body(self, request):
        draft_calls.append(request)
        return SimpleNamespace(state=tui.DraftState.COMPLETE, markdown="generated blocker", provenance={})

    tui.CodexHarness.draft = draft_body
    tui.CodexHarness.probe = classmethod(lambda cls: tui.Availability.READY)
    tui.ACTIVE_BACKEND = "codex"
    try:
        for verdict, generated in (("approve", "accepted"), ("request-changes", "generated blocker"), ("comment", "observation")):
            app = tui.ReviewDashboard(tui.QueueFilters(action=""))
            async with app.run_test() as pilot:
                await pilot.pause()
                for _ in range(200):
                    if app.stops:
                        break
                    await pilot.pause(0.05)
                stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
                stop.live.update({"baseRefOid": "a" * 40, "headRefOid": "b" * 40})
                stop.review_result = tui.ReviewResult(
                    1, "complete" if verdict == "approve" else "findings",
                    findings=() if verdict == "approve" else ({"severity": "high", "title": "blocker"},),
                    provenance={"repository": "projectbluefin/bluefinctl", "pull_request": 31,
                                "base_sha": "a" * 40, "head_sha": "b" * 40},
                )
                app.leave_review(stop)
                await pilot.pause()
                await pilot.press({"approve": "1", "request-changes": "2", "comment": "3"}[verdict])
                await pilot.pause()
                hints = str(app.screen.query_one("#review-body-shortcuts", tui.Static).render())
                check(
                    "[ctrl-g]" in hints and "[ctrl-s]" in hints,
                    "review body shortcut hints must render literally",
                )
                await pilot.click("#review-body-generate")
                await pilot.pause()
                check(app.screen.query_one("#review-body-editor", tui.TextArea).text == "generated blocker",
                      f"{verdict} generation must use the drafting capability")
                await pilot.click("#review-body-edit")
                editor = app.screen.query_one("#review-body-editor", tui.TextArea)
                check(app.focused is editor, "edit button must focus the review body editor")
                editor.text = "clear me"
                await pilot.click("#review-body-clear")
                check(editor.text == "", "clear button must empty the review body editor")
                editor.text = exact_markdown
                before_preview = gh_log.read_text()
                await pilot.click("#review-body-preview")
                await pilot.pause()
                check(isinstance(app.screen, tui.ReviewBodyPreview), "preview must show before mutation")
                check(exact_markdown in app.screen.body, "preview must preserve exact Markdown")
                check(
                    gh_log.read_text() == before_preview,
                    "preview must not mutate before the typed-number gate",
                )
                await pilot.click("#review-preview-submit")
                await pilot.pause()
                for _ in range(20):
                    if isinstance(app.screen, tui.ConfirmMutation):
                        break
                    await pilot.pause(0.05)
                check(isinstance(app.screen, tui.ConfirmMutation), "preview submit must use the existing gate")
                command = app.screen.commands[0]
                check(command[:3] == ["gh", "pr", "review"], "submit must use gh pr review")
                body_path = Path(command[command.index("--body-file") + 1])
                check(body_path.read_text(encoding="utf-8") == exact_markdown,
                      "body file must preserve exact Markdown")
                await pilot.press(*app.screen.expected)
                await pilot.press("escape")
                check(not body_path.exists(), "cancelled mutation must clean the temporary body")
        check(len(draft_calls) == 3, "all three verdicts must call drafting")
    finally:
        tui.ACTIVE_BACKEND = original_backend
        tui.CodexHarness.draft = original_draft
        tui.CodexHarness.probe = original_probe

    # Missing Codex must degrade generation without touching manual prose.
    unavailable_backend = tui.ACTIVE_BACKEND
    original_unavailable_draft = tui.CodexHarness.draft
    unavailable_probe_calls = []

    def unavailable_probe(cls):
        unavailable_probe_calls.append(True)
        return tui.Availability.UNAVAILABLE_BINARY

    def unavailable_draft(self, request):
        raise FileNotFoundError("codex")

    tui.CodexHarness.probe = classmethod(unavailable_probe)
    tui.CodexHarness.draft = unavailable_draft
    tui.ACTIVE_BACKEND = "codex"
    try:
        check(tui.ACTIVE_BACKEND == "codex",
              "unavailable Codex pilot must explicitly select Codex")
        app = tui.ReviewDashboard(tui.QueueFilters(action=""))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            stop = app.stops[0]
            stop.live.update({"baseRefOid": "a" * 40, "headRefOid": "b" * 40})
            stop.review_result = tui.ReviewResult(
                1, "findings", findings=({"severity": "high", "title": "blocker"},),
                provenance={"repository": "projectbluefin/bluefinctl", "pull_request": 31,
                            "base_sha": "a" * 40, "head_sha": "b" * 40},
            )
            app.leave_review(stop)
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            editor = app.screen.query_one("#review-body-editor", tui.TextArea)
            editor.text = "manual maintainer body"
            app.screen.action_generate()
            await pilot.pause()
            check(editor.text == "manual maintainer body",
                  "unavailable Codex must preserve the manual review body")
            check(unavailable_probe_calls,
                  "unavailable Codex must be reached during generation")
            await app.workers.wait_for_complete()
            check(any("unavailable" in notification.message.lower()
                      for notification in app._notifications),
                  "unavailable Codex must show a degraded generation message")
    finally:
        tui.CodexHarness.probe = original_probe
        tui.CodexHarness.draft = original_unavailable_draft
        tui.ACTIVE_BACKEND = unavailable_backend
        check(tui.ACTIVE_BACKEND == unavailable_backend,
              "unavailable Codex pilot must restore the prior backend")

    # Goose is the selected backend by default and drafts bodies directly.
    original_backend = tui.ACTIVE_BACKEND
    original_goose_draft = tui.GooseHarness.draft
    goose_calls = []

    def goose_draft(self, request):
        goose_calls.append(request)
        return SimpleNamespace(
            state=tui.DraftState.COMPLETE,
            markdown="generated Goose body",
            provenance={"backend": "goose", "model": self.model, "effort": self.effort},
        )

    tui.ACTIVE_BACKEND = "goose"
    tui.GooseHarness.draft = goose_draft
    try:
        app = tui.ReviewDashboard(tui.QueueFilters(action=""))
        async with app.run_test() as pilot:
            await wait_for_live_rows(app, pilot, "ready", 2)
            stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
            app._queue().index = app.stops.index(stop)
            await settle_evidence(app, pilot)
            stop.live.update({"baseRefOid": "a" * 40, "headRefOid": "b" * 40})
            stop.review_result = tui.ReviewResult(
                1, "findings", findings=({"severity": "high", "title": "blocker"},),
                provenance={"repository": "projectbluefin/bluefinctl", "pull_request": 31,
                            "base_sha": "a" * 40, "head_sha": "b" * 40},
            )
            app.leave_review(stop)
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            editor = app.screen.query_one("#review-body-editor", tui.TextArea)
            editor.text = "manual Goose body"
            app.screen.action_generate()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            check(editor.text == "generated Goose body",
                  "Goose drafting must use the selected drafting capability")
            check(len(goose_calls) == 1, "Goose drafting must be invoked directly")
            editor.text = "x" * 4096
            app.screen.action_preview()
            await pilot.pause()
            check(isinstance(app.screen, tui.ReviewBodyPreview),
                  "a 4096-character body must be accepted")
            await pilot.press("escape")
            await pilot.pause()
            editor = app.screen.query_one("#review-body-editor", tui.TextArea)
            editor.text = "x" * 4097
            app.screen.action_preview()
            await pilot.pause()
            check(isinstance(app.screen, tui.ReviewBody),
                  "an oversized body must remain editable")
            check(app.screen.body_file is None,
                  "an oversized body must not create a temporary file")
    finally:
        tui.ACTIVE_BACKEND = original_backend
        tui.GooseHarness.draft = original_goose_draft

    # A verdict that is not an approval has to say why.
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        gh_log.write_text("")
        await pilot.press("L")
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.ConfirmMutation),
            "an empty request-changes must not reach the gate",
        )
        check(
            "pr review" not in gh_log.read_text(),
            "an empty request-changes must submit nothing",
        )
    gh_log.write_text("")

    # ── batch merge, and what happens when one refuses ───────────────────
    # A batch that stops dead on the first refusal is worse than no batch:
    # the maintainer is several confirmations past it before they read the
    # error. A refusal becomes a choice, and whatever is not fixed stays
    # selected so it comes back with the batch.
    check(
        [c for c, _ in tui.MergeRecovery.offers(
            tui.Stop("o/r", 1, "merge", "t", live={"mergeStateStatus": "BEHIND"}), ""
        )][:1] == ["update"],
        "a branch that is behind must be offered an update",
    )
    check(
        "queue" in [c for c, _ in tui.MergeRecovery.offers(
            tui.Stop("o/r", 1, "merge", "t", live={"mergeStateStatus": "BLOCKED"}), ""
        )],
        "a blocked merge must be offered the sweep instead",
    )
    check(
        "handoff" in [c for c, _ in tui.MergeRecovery.offers(
            tui.Stop("o/r", 1, "merge", "t", live={"mergeStateStatus": "DIRTY"}), ""
        )],
        "a conflicted merge must offer explicit exceptional handoff",
    )
    check(
        [c for c, _ in tui.MergeRecovery.offers(
            tui.Stop("o/r", 1, "merge", "t", live={}), ""
        )] == ["retry", "skip"],
        "every failure must at least offer retry and keep-it-queued",
    )

    refusing_gh = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        'if [ "$1 $2" = "pr merge" ]; then\n'
        '  printf "Pull request is not mergeable: the base branch is out of date %s\\n" "$(printf "e%.0s" {1..300})" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1 $2" = "pr review" ]; then printf "approval failed %s\\n" "$(printf "e%.0s" {1..300})" >&2; exit 1; fi\n'
        'if [ "$1 $2" = "pr edit" ]; then printf "queue label failed %s\\n" "$(printf "e%.0s" {1..300})" >&2; exit 1; fi\n'
        'if [ "$1 $2" = "pr update-branch" ]; then printf "update failed %s\\n" "$(printf "e%.0s" {1..300})" >&2; exit 1; fi\n'
        "exit 0\n",
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        for stop in app.stops:
            stop.selected = True
            stop.live = {"isDraft": False, "mergeStateStatus": "BEHIND"}
        for _ in range(200):
            if all(s.repository in app.merge_rights for s in app.stops):
                break
            await pilot.pause(0.05)
        gh_log.write_text("")
        app.action_merge_now()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            f"batch merge must gate the first PR, got {type(app.screen).__name__}",
        )
        gate = app.screen
        await pilot.press(*gate.expected)
        await pilot.press("enter")
        for _ in range(300):
            if isinstance(app.screen, tui.MergeRecovery):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.MergeRecovery),
            f"a refused merge must offer a way out, got {type(app.screen).__name__}",
        )
        if isinstance(app.screen, tui.MergeRecovery):
            check(
                app.stops[0].failure != "",
                "a refused merge must be recorded on the stop",
            )
            check(
                app.stops[0].selected,
                "a refused merge must stay in the batch, not be dropped",
            )
            row = str(
                app.query_one("#queue", tui.ListView)
                .children[0]
                .query_one(tui.Label)
                .render()
            )
            check(
                "DID NOT MERGE" in row,
                f"the row must carry the failure, got {row!r}",
            )
            check(
                "did not merge" in str(
                    app.query_one("#status-bar", tui.Static).render()
                ),
                "the status line must count what did not merge",
            )
            recovery_text = "\n".join(
                str(widget.render())
                for widget in list(app.screen.query(tui.Label))
                + list(app.screen.query(tui.Static))
            )
            check(
                "gh pr merge" in recovery_text
                and "Pull request is not mergeable" in recovery_text
                and len(app.stops[0].failure) > 200
                and app.stops[0].failure_command == shlex.join(
                    ["gh", "pr", "merge", str(app.stops[0].number),
                     "--repo", app.stops[0].repository, "--squash"]
                )
                and "checks" in recovery_text
                and "branch" in recovery_text,
                "merge recovery must keep complete error and exact argv with checks and branch evidence",
            )
            # Choosing "update the branch" retries with the update in front.
            await pilot.press("1")
            for _ in range(300):
                if isinstance(app.screen, tui.ConfirmMutation):
                    break
                await pilot.pause(0.05)
            check(
                isinstance(app.screen, tui.ConfirmMutation),
                "updating the branch must be gated like any other mutation",
            )
            retry = app.screen
            check(
                [c[:3] for c in retry.commands]
                == [["gh", "pr", "update-branch"], ["gh", "pr", "merge"]],
                f"update must run before the retry, got {retry.commands}",
            )
            await pilot.press("escape")
            await pilot.pause()
        # The batch continued to the second pull request rather than stopping.
        for _ in range(300):
            if gh_log.read_text().count("pr merge") >= 2:
                break
            await pilot.pause(0.05)
        if isinstance(app.screen, tui.ConfirmMutation):
            await pilot.press(*app.screen.expected)
            await pilot.press("enter")
            await pilot.pause()
        check(
            gh_log.read_text().count("pr merge") >= 1,
            "a batch merge must attempt the pull requests it was given",
        )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
        app._queue().index = app.stops.index(stop)
        app.render_evidence(stop)
        stop.live = {
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        app.self_login = "castrojo"
        os.environ["CURL_FAIL"] = "1"
        app._queue_automerge(stop)
        await pilot.pause()
        check(isinstance(app.screen, tui.ConfirmMutation), "approve/queue must use the mutation gate")
        if isinstance(app.screen, tui.ConfirmMutation):
            gate = app.screen
            await pilot.press(*gate.expected)
            await pilot.press("enter")
            for _ in range(300):
                if len(stop.failure) > 200:
                    break
                await pilot.pause(0.05)
            details = str(app.query_one("#details", tui.Static).render())
            check(
                len(stop.failure) > 200
                and stop.failure_command.endswith(
                    "https://hive.example.test/api/v1/prs/projectbluefin/bluefinctl/31/queue-automerge"
                )
                and "LAST MUTATION FAILURE" in details
                and stop.failure in details,
                "failed approve/queue must durably show complete stderr and quoted argv",
            )
            os.environ.pop("CURL_FAIL", None)
            stop.failure = ""
            app.mutate_all(
                stop,
                [["gh", "pr", "update-branch", str(stop.number), "--repo", stop.repository]],
            )
            await pilot.pause()
            if isinstance(app.screen, tui.ConfirmMutation):
                gate = app.screen
                await pilot.press(*gate.expected)
                await pilot.press("enter")
            for _ in range(300):
                if len(stop.failure) > 200:
                    break
                await pilot.pause(0.05)
            details = str(app.query_one("#details", tui.Static).render())
            check(
                len(stop.failure) > 200
                and stop.failure_command.startswith("gh pr update-branch ")
                and stop.failure in details,
                "failed update must retain complete stderr in the durable detail state",
            )
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        'if [ "$1" = "api" ] && [[ "$2" == repos/*/compare/* ]]; then\n'
        '  if [ -n "${RE_REVIEW_COMPARE_FAIL-}" ]; then echo "compare unavailable" >&2; exit 1; fi\n'
        f'  printf "compare:%s\\n" "${{RE_REVIEW_COMPARE_JSON-UNSET}}" >>"{gh_log}"\n'
        '  if [ -n "${RE_REVIEW_COMPARE_JSON+x}" ]; then printf "%s\\n" "$RE_REVIEW_COMPARE_JSON"; else printf "%s\\n" "{}"; fi; exit 0\n'
        'fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi; echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    gh_log.write_text("")

    # ── Hive owns the App approval and queue label atomically (#247) ──────
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
        app._queue().index = app.stops.index(stop)
        stop.live = {"isDraft": False}
        gh_log.write_text("")
        curl_log.write_text("")
        app.action_merge()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            "queueing must still gate when the label is missing",
        )
        if isinstance(app.screen, tui.ConfirmMutation):
            check(
                len(app.screen.commands) == 1
                and app.screen.commands[0][1] == str(hive_api_stub)
                and app.screen.commands[0][2] == "queue"
                and app.screen.commands[0][-1].endswith(
                    "/api/v1/prs/projectbluefin/bluefinctl/31/queue-automerge"
                ),
                f"queueing must show one Hive request, got {app.screen.commands}",
            )
            await pilot.press(*app.screen.expected)
            await pilot.press("enter")
            for _ in range(200):
                if "queue-automerge" in curl_log.read_text():
                    break
                await pilot.pause(0.05)
            check(
                "queue-automerge" in curl_log.read_text()
                and "pr review" not in gh_log.read_text()
                and "pr edit" not in gh_log.read_text(),
                "Hive must queue without a human review or direct label mutation",
            )
    gh_log.write_text("")

    # ── two key lines, colour by state, refresh, and update-branch ───────
    check(
        tui.stop_style("review", "dirty", "success", "approved") == "",
        "a conflict must not color the entire row as a failed inference",
    )
    check(
        tui.stop_style("review", "clean", "failure", "unknown") == "red",
        "failing checks must be red",
    )
    check(
        tui.stop_style("ready-for-human-merge", "clean", "success", "approved")
        == "bold green",
        "merge-ready work must stand out",
    )
    check(
        tui.stop_style("investigate", "unknown", "unknown", "unknown") == "grey62",
        "work nobody can act on must recede",
    )
    text_states = {
        "success": tui.Stop("o/r", 1, "ready-for-human-merge", "green", check_state="success"),
        "failure": tui.Stop("o/r", 2, "review", "failed", check_state="failure"),
        "pending": tui.Stop(
            "o/r", 3, "review", "pending", check_state="unknown",
            live={"statusCheckRollup": [{"state": "IN_PROGRESS"}]},
        ),
        "unknown": tui.Stop("o/r", 4, "investigate", "unknown", check_state="unknown"),
        "conflict": tui.Stop("o/r", 5, "review", "conflict", mergeable_state="dirty"),
    }
    text_rows = {state: app.row_markup(stop) for state, stop in text_states.items()}
    for state, marker in {
        "success": "✓ CI GREEN",
        "failure": "✗ CI FAILED",
        "pending": "… CI PENDING",
        "unknown": "? CI UNKNOWN",
        "conflict": "⚑ CONFLICTS",
    }.items():
        check(marker in text_rows[state], f"{state} CI must be explicit on its row")
    check(
        text_rows["conflict"].index("⚑ CONFLICTS") < text_rows["conflict"].index("[review]"),
        "conflict text must outrank the healthy queue presentation",
    )
    check(
        not text_rows["conflict"].startswith("[red]"),
        "a conflict row body must remain neutral rather than whole-row red",
    )
    check(
        "[bold red]⚑ CONFLICTS[/bold red]" in text_rows["conflict"],
        "the conflict marker itself must retain meaningful red emphasis",
    )
    check(
        text_rows["failure"].index("✗ CI FAILED") < text_rows["failure"].index("[review]"),
        "failure text must outrank the healthy queue presentation",
    )

    for key in ("r", "v", "C", "o", "h", "/", "f", "b", "H", "R", "q"):
        check(
            f"[b]{key}[/b]" in tui.KEYS_READING,
            f"the reading key line must document {key!r}",
        )
    for key in ("L", "a", "m", "u", "x", "M"):
        check(
            f"[b]{key}[/b]" in tui.KEYS_ACTING,
            f"the acting key line must document {key!r}",
        )
    for key in ("l", "p"):
        check(
            f"[b]{key}[/b]" not in tui.KEYS_ACTING,
            f"the acting key line must not advertise {key!r}",
        )

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        check(
            bool(app.query("#keys-reading")) and bool(app.query("#keys-acting")),
            "the key map must be two lines at the bottom",
        )
        # A normal terminal may report Shift-L as lower-case key identity with
        # an upper-case character. Drive that real event shape rather than
        # Pilot's synthetic ``press("L")`` event (#259).
        app.post_message(Key("l", "L"))
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ReviewVerdict),
            "terminal-normalized Shift-L must open the ordinary review verdict",
        )
        if isinstance(app.screen, tui.ReviewVerdict):
            await pilot.press("escape")
        app.query_one("#queue", tui.ListView).focus()
        app.post_message(Key("l", "l"))
        await pilot.pause()
        # The evidence panes are focusable scroll containers, so pane
        # movement reaches them in DOM order before the steering box.
        check(
            app.focused is app.query_one("#details-pane"),
            f"lowercase l must reach the details pane, got {app.focused}",
        )
        app.post_message(Key("l", "l"))
        await pilot.pause()
        check(
            app.focused is app.query_one("#context-pane"),
            f"a second l must reach the context pane, got {app.focused}",
        )

        # Evidence taller than the pane is reachable: the focused pane
        # scrolls, and the queue selection does not move.
        app.query_one("#details", tui.Static).update(
            "\n".join(f"evidence line {n}" for n in range(200))
        )
        app.query_one("#details-pane").focus()
        await pilot.pause()
        await pilot.pause()
        index_before = app.query_one("#queue", tui.ListView).index
        await pilot.press("pagedown")
        await pilot.pause()
        check(
            app.query_one("#details-pane").scroll_offset.y > 0,
            "a focused evidence pane must scroll its content",
        )
        check(
            app.query_one("#queue", tui.ListView).index == index_before,
            "scrolling a pane must not move the queue selection",
        )
        await pilot.press("home")
        await pilot.pause()
        check(
            app.query_one("#details-pane").scroll_offset.y == 0,
            "home must return the pane to the top",
        )
        app.query_one("#queue", tui.ListView).focus()
        app.query_one("#queue", tui.ListView).focus()
        app.action_pane_next = lambda: (_ for _ in ()).throw(RuntimeError("injected pane failure"))
        app.post_message(Key("l", "l"))
        await pilot.pause()
        check(
            app.screen is not None
            and any("injected pane failure" in notification.message for notification in app._notifications),
            "terminal dispatch failures must be bounded notifications without ending the dashboard",
        )
        app.query_one("#queue", tui.ListView).focus()
        # Direct merge must refuse snapshot-known red and pending checks before
        # presenting a confirmation gate or attempting the GitHub mutation.
        for known_state, live_checks in (
            ("failure", [{"conclusion": "FAILURE"}]),
            ("pending", [{"state": "IN_PROGRESS"}]),
        ):
            app.stops[0].check_state = "unknown"
            app.stops[0].live = {
                "isDraft": False,
                "statusCheckRollup": live_checks,
            }
            app.merge_rights[app.stops[0].repository] = True
            gh_log.write_text("")
            app.action_merge_now()
            await pilot.pause()
            check(
                not isinstance(app.screen, tui.ConfirmMutation),
                f"direct merge must refuse known-{known_state} CI before confirmation",
            )
            check(
                "pr merge" not in gh_log.read_text(),
                f"direct merge must not attempt known-{known_state} CI",
            )
            if isinstance(app.screen, tui.ConfirmMutation):
                await pilot.press("escape")
                await pilot.pause()
        # Colour reaches the row, from the snapshot's own state fields.
        app.stops[0].mergeable_state = "dirty"
        app.refresh_rows()
        await pilot.pause()
        row = str(
            app.query_one("#queue", tui.ListView)
            .children[0]
            .query_one(tui.Label)
            .render()
        )
        check(
            "CONFLICTS" in row,
            f"a conflicted stop must say so on its row, got {row!r}",
        )

        # [R] refreshes without losing the batch selection.
        app.stops[0].selected = True
        app.stops[1].selected = True
        await pilot.press("R")
        for _ in range(200):
            if app.stops and all(s.selected for s in app.stops):
                break
            await pilot.pause(0.05)
        check(
            len(app.stops) == 2 and all(s.selected for s in app.stops),
            "a refresh must keep the batch it was holding",
        )

        # [u] updates the branch, for the batch, behind the gate.
        gh_log.write_text("")
        await pilot.press("u")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            f"[u] must be gated, got {type(app.screen).__name__}",
        )
        if isinstance(app.screen, tui.ConfirmMutation):
            check(
                [c[:3] for c in app.screen.commands]
                == [["gh", "pr", "update-branch"]],
                f"[u] must update the branch, got {app.screen.commands}",
            )
            await pilot.press(*app.screen.expected)
            await pilot.press("enter")
            for _ in range(200):
                if "pr update-branch" in gh_log.read_text():
                    break
                await pilot.pause(0.05)
            check(
                gh_log.read_text().count("pr update-branch") >= 1,
                "the confirmed update must actually run",
            )
            if isinstance(app.screen, tui.ConfirmMutation):
                await pilot.press("escape")
                await pilot.pause()
    gh_log.write_text("")

    # A conflicted branch cannot be brought current by GitHub's update API;
    # show the maintainer the manual-resolution path instead of opening a
    # gate that is certain to fail (#261).
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
        stop.mergeable_state = "dirty"
        stop.live = {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        gh_log.write_text("")
        await pilot.press("u")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.ConfirmMutation),
            "conflicted branches must not offer the update-branch gate",
        )
        check(
            "manual" in " ".join(notification.message.lower() for notification in app._notifications),
            "conflicted branches must direct maintainers to manual resolution",
        )
        check(
            "pr update-branch" not in gh_log.read_text(),
            "conflicted branches must not invoke GitHub's update API",
        )
    gh_log.write_text("")

    # ── MECHANICAL is live evidence, not a dependency-shaped title ───────
    # The old BATCHABLE tag matched titles, which is duplicate evidence: it
    # said nothing about whether the branch could actually be brought current.
    renovate_body = (
        "This PR contains the following updates:\n\n"
        "| Package | Update | Change |\n"
        "|---|---|---|\n"
        "| [dep](https://x) | digest | `aaa` -> `bbb` |\n\n"
        "---\n### Configuration\n"
    )
    major_body = renovate_body.replace("| digest |", "| major |")
    bot = sorted(tui.RENOVATE_BOTS)[0]
    green = [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]

    def live_shape(**overrides) -> dict:
        shape = {
            "author": {"login": bot},
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
            "body": renovate_body,
            "statusCheckRollup": list(green),
        }
        shape.update(overrides)
        return shape

    check(
        tui.renovate_update_types(renovate_body) == {"digest"},
        "Renovate's own table must supply the update type",
    )
    check(
        tui.mechanical_reason(bot, live_shape()) is not None,
        "a fixture matching every required signal must be MECHANICAL",
    )
    # Removing or weakening any single required signal must disqualify it.
    for description, overrides in (
        ("a draft", {"isDraft": True}),
        ("a conflict", {"mergeable": "CONFLICTING"}),
        ("an already-current branch", {"mergeStateStatus": "CLEAN"}),
        ("a blocked branch", {"mergeStateStatus": "BLOCKED"}),
        ("a closed pull request", {"state": "CLOSED"}),
        ("a failed check", {"statusCheckRollup": [{"conclusion": "FAILURE"}]}),
        ("a pending check", {"statusCheckRollup": green + [{"state": "PENDING"}]}),
        ("no checks at all", {"statusCheckRollup": []}),
        ("a major update", {"body": major_body}),
        ("no Renovate metadata", {"body": "hand-written description"}),
    ):
        check(
            tui.mechanical_reason(bot, live_shape(**overrides)) is None,
            f"{description} must never be MECHANICAL",
        )
    check(
        tui.mechanical_reason("castrojo", live_shape()) is None,
        "a non-Renovate author must never be MECHANICAL",
    )
    check(
        tui.mechanical_reason(bot, {}) is None,
        "MECHANICAL must require live evidence, never absence of it",
    )

    # GitHub may return multiple runs for one check context at the exact head.
    # The current run is authoritative; a cancelled predecessor must not make
    # clean live evidence look failed or appear twice in review verification.
    superseded = json.loads(
        (FIXTURE_DIR / "superseded-check-rollup.json").read_text()
    )
    check(
        tui.effective_check_state("unknown", superseded) == "success",
        "a successful current run must supersede a cancelled older run",
    )
    check(
        tui.live_review_context(superseded)["ci"] == "success",
        "review context must agree with the exact-head check rollup",
    )
    verification = tui.live_review_verification(superseded)
    check(
        [record["name"] for record in verification]
        == [
            "E2E smoke",
            "validate-release-notes",
            "Check PR base branch",
            "validate",
            "Unit tests",
        ],
        "review verification must order one authoritative record per stable context",
    )
    status_contexts = {
        "statusCheckRollup": [
            {
                "__typename": "StatusContext",
                "context": "ci/vendor",
                "state": "FAILURE",
                "startedAt": "2026-08-10T00:28:00Z",
            },
            {
                "__typename": "StatusContext",
                "context": "ci/vendor",
                "state": "SUCCESS",
                "startedAt": "2026-08-10T00:29:00Z",
            },
        ]
    }
    check(
        tui.effective_check_state("unknown", status_contexts) == "success"
        and len(tui.authoritative_checks(status_contexts)) == 1,
        "a newer commit status must supersede the same stable status context",
    )

    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
        stop.live = superseded
        app.render_evidence(stop)
        details = str(app.query_one("#details", tui.Static).render())
        check(
            "checks   5 ok, 0 failed, 0 cancelled, 0 pending" in details
            and "MERGEABLE / CLEAN" in details,
            "the dashboard must render exact-head authoritative checks and merge state",
        )
        current_states = json.loads(json.dumps(superseded))
        current_states["mergeStateStatus"] = "BLOCKED"
        current_states["statusCheckRollup"][2]["conclusion"] = None
        current_states["statusCheckRollup"][2]["status"] = "IN_PROGRESS"
        current_states["statusCheckRollup"][5]["conclusion"] = "FAILURE"
        current_states["statusCheckRollup"][7]["conclusion"] = "CANCELLED"
        stop.live = current_states
        app.render_evidence(stop)
        details = str(app.query_one("#details", tui.Static).render())
        check(
            "checks   2 ok, 1 failed, 1 cancelled, 1 pending" in details
            and "MERGEABLE / BLOCKED" in details,
            "current failures, cancellations, pending checks, and merge blockers must stay distinct",
        )
        missing_required = {
            "headRefOid": superseded["headRefOid"],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [],
        }
        check(
            tui.live_review_context(missing_required)["ci"] == "unknown"
            and tui.live_review_context(missing_required)["merge_state"] == "BLOCKED",
            "missing required contexts must remain unknown beside GitHub's blocked merge state",
        )
    check(
        tui.mechanical_reason(
            "castrojo", live_shape(author={"login": "castrojo"})
        )
        is None,
        "a title-only lookalike must never be MECHANICAL",
    )
    check(
        tui.dependency_subject("chore(deps): update dependency ws to v8") is not None,
        "title normalisation must survive for duplicate detection",
    )

    mech_row = app.row_markup(
        tui.Stop("o/r", 101, "review", "chore(deps): bump", author=bot, live=live_shape())
    )
    plain_row = app.row_markup(
        tui.Stop("o/r", 117, "review", "chore(deps): bump", author=bot)
    )
    check("(MECHANICAL)" in mech_row, "a mechanical stop must say so on its row")
    check(
        "(MECHANICAL)" not in plain_row,
        "a stop without live evidence must not claim to be mechanical",
    )
    check(
        "[b]U[/b]" in tui.KEYS_ACTING,
        "the acting key line must document the mechanical selection key",
    )

    # [U] over a live queue: two qualifying Renovate branches and one that is
    # conflicted, exactly the shapes #152 names.
    set_org_queue([
        {"repository": "o/r", "number": 101, "recommended_action": "review",
         "title": "chore(deps): update dependency alpha", "author": bot},
        {"repository": "o/r", "number": 142, "recommended_action": "review",
         "title": "chore(deps): update dependency beta", "author": bot},
        {"repository": "o/r", "number": 117, "recommended_action": "review",
         "title": "chore(deps): update dependency gamma", "author": bot},
        {"repository": "o/r", "number": 9, "recommended_action": "review",
         "title": "chore(deps): update dependency delta by hand",
         "author": "someone-else"},
    ])
    ok_json = workdir / "mech-ok.json"
    ok_json.write_text(json.dumps(live_shape()))
    bad_json = workdir / "mech-bad.json"
    bad_json.write_text(json.dumps(live_shape(mergeable="CONFLICTING")))
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then\n'
        '  if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi\n'
        '  case "$3" in\n'
        f'    101|142) cat "{ok_json}" ;;\n'
        f'    117) cat "{bad_json}" ;;\n'
        '    *) echo "{}" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        "exit 0\n",
    )
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 4:
                break
            await pilot.pause(0.05)
        check(len(app.stops) == 4, "the default queue must stay unfiltered")
        await pilot.press("U")
        for _ in range(200):
            if sum(1 for s in app.stops if s.selected) == 2:
                break
            await pilot.pause(0.05)
        selected = {s.number for s in app.stops if s.selected}
        check(
            selected == {101, 142},
            f"[U] must select only the mechanical branches, got {selected}",
        )
        check(
            len(app.stops) == 4,
            "[U] must select within the queue, never filter it away",
        )
        # The selection feeds the existing gated [u] action unchanged.
        gh_log.write_text("")
        await pilot.press("u")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            "[u] on a mechanical selection must keep its confirmation gate",
        )
        if isinstance(app.screen, tui.ConfirmMutation):
            check(
                [c[:3] for c in app.screen.commands] == [["gh", "pr", "update-branch"]],
                f"[u] must update one branch at a time, got {app.screen.commands}",
            )
            await pilot.press("escape")
            await pilot.pause()
    gh_log.write_text("")
    set_org_queue(SNAPSHOT["items"])

    # ── duplicates come with enough summary to choose between them ───────
    # "dupe-of #26, #25, #24" says a decision is required and nothing about
    # how to make it; which one to keep is the whole question.
    cluster_gh = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr list" ]; then cat <<\'JSON\'\n'
        '[{"number":31,"title":"chore(deps): update actions/checkout action to v7",'
        '"files":[{"path":"a.yml"}],"closingIssuesReferences":[],'
        '"author":{"login":"renovate"},"updatedAt":"2026-08-01T00:00:00Z",'
        '"isDraft":false,"reviewDecision":"APPROVED","mergeable":"MERGEABLE"},'
        '{"number":44,"title":"chore(deps): update actions/checkout action to v8",'
        '"files":[{"path":"a.yml"},{"path":"b.yml"}],"closingIssuesReferences":[],'
        '"author":{"login":"someone"},"updatedAt":"2026-08-05T00:00:00Z",'
        '"isDraft":true,"reviewDecision":"","mergeable":"CONFLICTING"}]\n'
        "JSON\n"
        "exit 0; fi\n"
        "exit 0\n",
    )
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
        app._queue().index = app.stops.index(stop)
        stop.live = {"isDraft": False}
        dupes, _ = app.cluster(stop)
        check(
            [d["number"] for d in dupes] == [44],
            f"the duplicate must be found, got {dupes}",
        )
        if dupes:
            near = dupes[0]
            check(
                near["title"].startswith("chore(deps): update actions/checkout"),
                "a duplicate must carry its title",
            )
            check(
                "same dependency" in near["why"],
                f"a duplicate must say why it is one, got {near['why']!r}",
            )
            check(
                near["author"] == "someone" and near["draft"] is True
                and near["mergeable"] == "CONFLICTING" and near["files"] == 2,
                f"a duplicate must carry the state you judge it by, got {near}",
            )
        app.render_evidence(stop)
        for _ in range(200):
            if "dupe-of" in str(app.query_one("#context", tui.Static).render()):
                break
            await pilot.pause(0.05)
        context = str(app.query_one("#context", tui.Static).render())
        for expected in (
            "dupe-of",
            "#44",
            "actions/checkout action to v8",
            "someone",
            "draft",
            "conflicting",
            "2 files",
            "same dependency",
        ):
            check(
                expected in context,
                f"the context pane must show {expected!r}, got {context!r}",
            )
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        + org_queue_branch +
        'if [ "$1" = "api" ] && [[ "$2" == repos/*/compare/* ]]; then\n'
        '  if [ -n "${RE_REVIEW_COMPARE_FAIL-}" ]; then echo "compare unavailable" >&2; exit 1; fi\n'
        f'  printf "compare:%s\\n" "${{RE_REVIEW_COMPARE_JSON-UNSET}}" >>"{gh_log}"\n'
        '  if [ -n "${RE_REVIEW_COMPARE_JSON+x}" ]; then printf "%s\\n" "$RE_REVIEW_COMPARE_JSON"; else printf "%s\\n" "{}"; fi; exit 0\n'
        'fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then if [ -n "${PR_VIEW_JSON-}" ]; then printf "%s\\n" "$PR_VIEW_JSON"; exit 0; fi; echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    gh_log.write_text("")

    # ── the repository's merge queue, as a meter ─────────────────────────
    check(
        tui.classify_queue_item(
            {"labels": ["lgtm"], "mergeable_state": "dirty", "check_state": "failure"}
        )
        == "conflicts",
        "conflicts outrank an already queued presentation",
    )
    check(
        tui.classify_queue_item(
            {"recommended_action": "ready-for-human-merge", "labels": []}
        )
        == "ready",
        "merge-ready work must be its own segment",
    )
    check(
        tui.classify_queue_item(
            {"mergeable_state": "dirty", "check_state": "failure", "labels": []}
        )
        == "conflicts",
        "a conflict outranks a failing check — the check cannot mean anything yet",
    )
    check(
        tui.classify_queue_item({"check_state": "failure", "labels": []}) == "ci",
        "failing checks are their own segment",
    )
    check(
        tui.classify_queue_item({"labels": []}) == "unclear",
        "anything unclassified must fall to unclear, never vanish",
    )
    check(tui.meter_bar({}) == "", "an empty queue draws no bar")
    lone = tui.meter_bar({"queued": 1, "unclear": 60})
    check(
        "[green]" in lone,
        f"one pull request waiting on the sweep must still be visible, got {lone!r}",
    )

    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        # The meter counts the maintainer's own work, which never appears as a
        # stop: "how busy is this repository" is not "what is left for me".
        counts, total = app.repo_queue("projectbluefin/review")
        check(
            total == 1,
            f"the meter must count own-authored work too, got {total} for review",
        )
        check(
            not any(s.repository == "projectbluefin/review" for s in app.stops),
            "own work must still be absent from the stops",
        )
        stop = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
        app._queue().index = app.stops.index(stop)
        stop.live = {"isDraft": False}
        app.render_evidence(stop)
        for _ in range(200):
            if "projectbluefin/bluefinctl" in str(app.query_one("#context", tui.Static).render()):
                break
            await pilot.pause(0.05)
        context = str(app.query_one("#context", tui.Static).render())
        check(
            "merge queue" in context and "projectbluefin/bluefinctl" in context,
            f"the context pane must show the repository's queue, got {context!r}",
        )
        check(
            "1 open" in context,
            f"the meter must state how many are open, got {context!r}",
        )
        check("█" in context, "the meter must draw a bar")
    gh_log.write_text("")

    # ── the gate is always escapable ─────────────────────────────────────
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        app.stops[0].live = {"isDraft": False}
        app.action_merge()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            "queueing a PR must open the confirmation gate",
        )
        await pilot.press("escape")
        for _ in range(200):
            if not isinstance(app.screen, tui.ConfirmMutation):
                break
            await pilot.pause(0.05)
        check(
            not isinstance(app.screen, tui.ConfirmMutation),
            "escape must abort the confirmation gate",
        )
    gh_log.write_text("")

    # ── a slow mutation must not freeze the dashboard ────────────────────
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    real_run = subprocess.run

    def slow_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if (
            isinstance(command, (list, tuple))
            and command[:2] == ["sh", "-c"]
            and str(command[-1]).endswith("/queue-automerge")
        ):
            time.sleep(2)
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(*args, **kwargs)

    subprocess.run = slow_run
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            app.self_login = "castrojo"
            app.stops[0].live = {"isDraft": False}
            app.action_merge()
            await pilot.pause()
            expected = app.screen.expected
            await pilot.press(*expected)
            loop = asyncio.get_running_loop()
            start = loop.time()
            ticks = []

            async def heartbeat():
                while loop.time() - start < 3:
                    ticks.append(loop.time() - start)
                    await asyncio.sleep(0.1)

            beat = asyncio.create_task(heartbeat())
            await pilot.press("enter")
            await asyncio.sleep(3)
            beat.cancel()
            gaps = [b - a for a, b in zip(ticks, ticks[1:])]
            check(
                bool(gaps) and max(gaps) < 1,
                "a slow queue mutation must run off the UI thread, "
                f"but the event loop stalled {max(gaps) if gaps else 0:.2f}s",
            )
    finally:
        subprocess.run = real_run
    gh_log.write_text("")

    # ── completed work refreshes the retained queue without freezing keys ──
    # Removing the reconciliation request, making it synchronous, or launching
    # one refresh per completion must fail this test. The actual landing
    # completion callback is used; only the external GitHub/Hive transports
    # are stubbed and delayed.
    set_org_queue([
        item for item in SNAPSHOT["items"] if item["number"] != 31
    ], pages_count=3)
    hive_requests: list[str] = []
    original_hive_get = tui.hive_get

    def reconciliation_hive_get(path: str):
        hive_requests.append(path)
        time.sleep(0.15)
        if path == "/api/v1/status":
            return tui.hive_api.Result(
                True, "ok", "online",
                {"hub": "online", "actionable_items": 2},
            )
        if path == "/api/contribute/queue":
            return tui.hive_api.Result(
                True, "ok", "online",
                {"queue": []},
            )
        if path == "/api/contribute/triage":
            return tui.hive_api.Result(
                True, "ok", "online",
                {"groups": []},
            )
        return tui.hive_api.Result(
            True, "ok", "online",
            {"contributors": []},
        )

    tui.hive_get = reconciliation_hive_get
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    app._request_reconciliation = reconciliation_request.__get__(
        app, tui.ReviewDashboard
    )
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops and app.hive_state:
                    break
                await pilot.pause(0.01)
            await app.workers.wait_for_complete()
            check(
                app.reconciliation_state == "not refreshed",
                "initial Hive and queue loads must not change reconciliation state",
            )
            # The initial load is not an operation-triggered reconciliation.
            set_org_queue(SNAPSHOT["items"])
            await pilot.press("R")
            for _ in range(200):
                if any(stop.number == 31 for stop in app.stops):
                    break
                await pilot.pause(0.01)
            await app.workers.wait_for_complete()
            queue_refresh_log.write_text("")
            hive_requests.clear()
            set_org_queue([
                item for item in SNAPSHOT["items"] if item["number"] != 31
            ])
            delay_queue_refresh.touch()

            # The always-visible activity surface is assembled from the
            # dashboard's own lifecycle state: no process discovery or new
            # transport is involved. The fixture covers a parent review,
            # its check workers, an active and queued landing, and one
            # cached Hive contributor assignment.
            active_landing = tui.landing.new_task(
                [tui.Stop(
                    "projectbluefin/common", 7, "review", "active landing"
                )],
                "tester",
            )
            active_landing.process = object()
            queued_landing = tui.landing.new_task(
                [tui.Stop(
                    "projectbluefin/review", 42, "review", "queued landing"
                )],
                "tester",
            )
            app.landing_queue = [active_landing, queued_landing]
            app.review_batches = [
                SimpleNamespace(
                    batch_id="activity-review",
                    items=(SimpleNamespace(
                        key="projectbluefin/bluefinctl#31",
                    ),),
                    running=True,
                )
            ]
            app.stops[0].review_status = "running"
            app.stops[0].selected = True
            app.stops[1].selected = True
            app.self_login = "castrojo"
            app.stops[1].review_status = "cached"
            app.stops[1].review_result = tui.ReviewResult(
                1,
                "complete",
                {"critical": 0, "high": 0, "medium": 0, "low": 0},
            )
            app.stops[1].live["reviews"] = [
                {"author": {"login": "castrojo"}, "state": "APPROVED"},
                {"author": {"login": "castrojo"}, "state": "PENDING"},
                {"author": {"login": "castrojo"}, "state": "CHANGES_REQUESTED"},
            ]
            app.review_engine = SimpleNamespace(
                effective_review_cap=lambda: 6,
                active_review_slots=lambda: 2,
            )
            app.hive_workers = [{
                "login": "hive-contributor",
                "task": {
                    "repo": "projectbluefin/dakota",
                    "number": 88,
                    "task_id": "ignored-by-activity",
                },
            }]
            app.hive_unavailable = False
            app.hive_workers_stale = False
            app.reconciliation_state = "fresh"
            app.reconciliation_updated_at = time.monotonic() - 61
            app.refresh_status()
            activity = app.query("#activity")
            activity_text = (
                str(activity.first().render()) if activity else ""
            )
            for expected in (
                "AGENT ACTIVITY",
                "Parent reviews: 1",
                "Check workers: 2",
                "Landing agents: 1",
                "Queued work: 1",
                "Review — projectbluefin/bluefinctl#31",
                "Landing — projectbluefin/common#7",
                "Hive @hive-contributor — projectbluefin/dakota#88",
                "Remote analysis: projectbluefin/bluefinctl#31",
                "Local draft: projectbluefin/common#7 (clean)",
                "GitHub review: projectbluefin/common#7 (CHANGES_REQUESTED)",
                "Snapshot: current",
                "1m ago",
            ):
                check(
                    expected in activity_text,
                    "the normal dashboard activity surface must render "
                    f"{expected!r}, got {activity_text!r}",
                )
            app.stops[1].live["reviews"] = [
                {"author": {"login": "castrojo"}, "state": "PENDING"},
                {"author": {"login": "castrojo"}, "state": "DISMISSED"},
            ]
            app.refresh_activity()
            activity_text = str(activity.first().render()) if activity else ""
            check(
                "GitHub review:" not in activity_text,
                "draft and dismissed GitHub reviews must not be reported as submitted, "
                f"got {activity_text!r}",
            )

            # The active fixture is display-only. Keep the real landing
            # completion focused on reconciliation rather than leaving a
            # fabricated process in the scheduler's active lane.
            app.landing_queue = []
            app.advance_final_review = lambda _task: None

            def completed_landing(number: int):
                task = tui.landing.new_task(
                    [tui.Stop("projectbluefin/bluefinctl", number, "review", "landed")],
                    "tester",
                )
                Path(task.status_path).write_text(
                    f'{{"pr":"projectbluefin/bluefinctl#{number}","state":"merged","note":"on :stable"}}\n'
                    '{"state":"done","note":"landed"}\n'
                )
                app.landing_finished(task)
                return task

            completed_landing(31)
            activity = app.query("#activity")
            activity_text = (
                str(activity.first().render()) if activity else ""
            )
            check(
                "Snapshot: refreshing" in activity_text,
                "a completed operation must refresh the activity snapshot "
                "through cached reconciliation, got "
                f"{activity_text!r}",
            )
            completed_landing(7)
            completed_landing(8)
            await pilot.press("j")
            await pilot.pause(0.05)
            queue = app.query_one("#queue", tui.ListView)
            check(
                queue.index == 1,
                "navigation must process while the delayed queue reconciliation runs",
            )
            status = str(app.query_one("#status-bar", tui.Static).render())
            check(
                "refreshing" in status.lower(),
                f"the status bar must expose bounded refresh progress, got {status!r}",
            )
            for _ in range(200):
                if not any(stop.number == 31 for stop in app.stops):
                    break
                await pilot.pause(0.01)
            check(
                not any(stop.number == 31 for stop in app.stops),
                "a completed landing must replace the cached queue so a merged PR disappears",
            )
            for _ in range(200):
                if (
                    not app._reconciliation_waiting
                    and queue_refresh_log.read_text().splitlines() == ["request", "request"]
                    and hive_requests.count("/api/v1/contributors") == 2
                ):
                    break
                await pilot.pause(0.01)
            activity = app.query("#activity")
            activity_text = (
                str(activity.first().render()) if activity else ""
            )
            check(
                "Snapshot: current" in activity_text,
                "a completed reconciliation must refresh the visible "
                f"activity snapshot, got {activity_text!r}",
            )
            check(
                queue_refresh_log.read_text().splitlines() == ["request", "request"],
                "operations during a refresh must coalesce to one additional GitHub queue refresh",
            )
            check(
                hive_requests.count("/api/v1/status") == 2
                and hive_requests.count("/api/v1/contributors") == 2,
                f"operations during a refresh must coalesce to one additional Hive refresh, got {hive_requests!r}",
            )
            await pilot.pause(0.2)
            check(
                queue_refresh_log.read_text().splitlines() == ["request", "request"],
                "no timer may create another queue refresh after reconciliation completes",
            )

            # A completed clean review is itself a successful operation. It
            # must request the same one-shot retained-cache reconciliation as
            # landing, while every other review update remains local.
            queue_refresh_log.write_text("")
            hive_requests.clear()
            delay_queue_refresh.touch()
            app.apply_review_event(
                tui.ReviewEvent(
                    key=app.stops[0].key,
                    state="complete",
                    note="clean review",
                    timestamp=int(time.time()),
                )
            )
            for _ in range(200):
                if (
                    not app._reconciliation_waiting
                    and queue_refresh_log.read_text().splitlines() == ["request"]
                    and hive_requests.count("/api/v1/status") == 1
                    and hive_requests.count("/api/v1/contributors") == 1
                ):
                    break
                await pilot.pause(0.01)
            check(
                queue_refresh_log.read_text().splitlines() == ["request"]
                and hive_requests.count("/api/v1/status") == 1
                and hive_requests.count("/api/v1/contributors") == 1,
                "a terminal clean review must trigger exactly one GitHub and Hive reconciliation",
            )
            await pilot.pause(0.2)
            check(
                queue_refresh_log.read_text().splitlines() == ["request"]
                and hive_requests.count("/api/v1/status") == 1
                and hive_requests.count("/api/v1/contributors") == 1,
                "a terminal clean review must not start a background reconciliation repeat",
            )
            for state in ("findings", "failed", "cancelled", "incomplete", "running"):
                app.apply_review_event(
                    tui.ReviewEvent(
                        key=app.stops[0].key,
                        state=state,
                        note=f"{state} review",
                        timestamp=int(time.time()),
                    )
                )
            await pilot.pause(0.2)
            check(
                queue_refresh_log.read_text().splitlines() == ["request"]
                and hive_requests.count("/api/v1/status") == 1
                and hive_requests.count("/api/v1/contributors") == 1,
                "findings, unsuccessful, and nonterminal review events must not reconcile",
            )

            # Queue and issue workers run in separate exclusive groups. A
            # failed queue refresh must retain its good queue even when an
            # issue response updates the shared source display meanwhile.
            set_org_queue(SNAPSHOT["items"])
            await pilot.press("R")
            await app.workers.wait_for_complete()
            os.environ["ORG_QUEUE_GH_ERROR"] = "queue unavailable"
            delay_queue_refresh.touch()
            app._request_reconciliation()
            app.load_issues()
            for _ in range(200):
                if not app._reconciliation_waiting:
                    break
                await pilot.pause(0.01)
            check(
                any(stop.number == 31 for stop in app.stops),
                "a failed queue refresh concurrent with issue loading must retain the good queue",
            )
            os.environ.pop("ORG_QUEUE_GH_ERROR", None)
            delay_queue_refresh.unlink(missing_ok=True)

            # This truthy non-mapping passes the outer page validation and
            # raises while a worker normalizes the pull request. Completion
            # must still unblock a later successful operation refresh.
            malformed_pages = json.loads(org_search_pages(SNAPSHOT["items"]))
            malformed_pages[0]["data"]["search"]["nodes"][0]["repository"] = "not-a-mapping"
            org_queue_file.write_text(json.dumps(malformed_pages))
            app._request_reconciliation()
            for _ in range(200):
                if not app._reconciliation_waiting:
                    break
                await pilot.pause(0.01)
            check(
                not app._reconciliation_waiting,
                "an unexpected queue worker exception must finish reconciliation",
            )
            request_after_error = app._reconciliation_request
            set_org_queue(SNAPSHOT["items"])
            app._request_reconciliation()
            for _ in range(200):
                if not app._reconciliation_waiting:
                    break
                await pilot.pause(0.01)
            check(
                app._reconciliation_request == request_after_error + 1
                and app.reconciliation_state == "fresh",
                "a later operation must start and finish a refresh after a worker exception",
            )

            # Explicit reads supersede the operation-triggered workers in
            # Textual's exclusive groups. They must settle the same request,
            # rather than leaving a fresh snapshot labelled unavailable.
            for explicit_read in (
                lambda: pilot.press("R"),
                app.action_hive,
            ):
                queue_refresh_log.write_text("")
                delay_queue_refresh.touch()
                app._request_reconciliation()
                for _ in range(200):
                    if queue_refresh_log.read_text().splitlines() == ["request"]:
                        break
                    await pilot.pause(0.01)
                result = explicit_read()
                if asyncio.iscoroutine(result):
                    await result
                for _ in range(200):
                    if not app._reconciliation_waiting:
                        break
                    await pilot.pause(0.01)
                await app.workers.wait_for_complete()
                check(
                    app.reconciliation_state == "fresh",
                    "an explicit queue or Hive read during reconciliation "
                    "must settle the fresh snapshot, not latch unavailable",
                )
                delay_queue_refresh.unlink(missing_ok=True)
    finally:
        tui.hive_get = original_hive_get
        delay_queue_refresh.unlink(missing_ok=True)
    set_org_queue(SNAPSHOT["items"])
    gh_log.write_text("")

    # ── a completed structured review becomes a concise decision card ───
    clean_output = (FIXTURE_DIR / "goose-review-clean.txt").read_text()
    findings_output = (FIXTURE_DIR / "goose-review-findings.txt").read_text()
    incomplete_output = (FIXTURE_DIR / "goose-review-incomplete.txt").read_text()
    text, classes, card = await run_review(0, clean_output)
    check("COMPLETE" in text, f"exit 0 must report COMPLETE, got {text!r}")
    check("complete" in classes, f"exit 0 must carry the complete style, got {classes}")
    check(
        "projectbluefin/bluefinctl#31" in text,
        "the review status must name the pull request under review",
    )
    invocations = review_log.read_text().strip().splitlines() if review_log.exists() else []
    check(
        invocations[-1:] == ["pr projectbluefin/bluefinctl 31"],
        f"the review must call 'pr <repo> <number>', got {invocations[-1:]}",
    )
    for expected in (
        "what changed  fix: ci.yml add permissions block",
        "risk/impact  No evidenced review risk.",
        "confidence  CI FAILED · MERGEABLE · head CURRENT 0123456789ab",
        "findings  No evidenced findings.",
        "next action  Review the evidence; wait for green CI before landing.",
        "No evidenced findings",
        "checks  4 verified / 1 unverified",
        "overlap 1 duplicate / 2 shared-file hazard",
        "CI failure",
        "MERGEABLE/CLEAN",
        "0123456789ab",
        "[a] approve+queue",
        "[m] merge",
        "[u] update",
        "[e] evidence",
    ):
        check(expected in card, f"the completed card must show {expected!r}, got {card!r}")

    # A single review is bound to its starting base/head, not the mutable row.
    slow_output = workdir / "slow-review-output.txt"
    slow_output.write_text(clean_output)
    write_stub(
        workdir / "bluefin-review",
        f'sleep 1\ncat "{slow_output}"\nexit 0\n',
    )
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        await settle_evidence(app, pilot)
        stop = app.stops[0]
        stop.live = {
            "baseRefOid": "a" * 40,
            "headRefOid": "b" * 40,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
        }
        stop.head_sha = "b" * 40
        stop.review_result = None
        stop.review_status = ""
        await pilot.press("r")
        for _ in range(200):
            if isinstance(app.screen, tui.ReviewScreen):
                break
            await pilot.pause(0.05)
        screen = app.screen
        check(
            isinstance(screen, tui.ReviewScreen),
            "the force-push review regression needs ReviewScreen",
        )
        if isinstance(screen, tui.ReviewScreen):
            stop.live["headRefOid"] = "c" * 40
            stop.head_sha = "c" * 40
            for _ in range(400):
                if screen.finished:
                    break
                await pilot.pause(0.05)
            status = str(
                screen.query_one("#review-status", tui.Static).render()
            )
            check(
                "STALE" in status
                and stop.review_result is None
                and stop.triage_state == "unseen",
                "a force-push during a single review must not attach or triage the old-head result",
            )
            await pilot.press("q")
            await pilot.pause()

    text, classes, card = await run_review(0, findings_output)
    check("COMPLETE" in text, f"a structured findings run must complete, got {text!r}")
    for expected in (
        "FINDINGS",
        "what changed  fix: ci.yml add permissions block",
        "risk/impact  HIGH risk · 2 actionable findings",
        "confidence  CI FAILED · MERGEABLE · head CURRENT 0123456789ab",
        "next action  Request changes or comment on the cited findings.",
        "critical:0  high:1  medium:1  low:0",
        "image/entrypoint.sh:87",
        "SIGTERM [signal] no longer reaches",
    ):
        check(expected in card, f"the findings card must show {expected!r}, got {card!r}")

    # Exit zero plus arbitrary prose has no structured evidence and must not
    # be promoted to the clean state.
    text, classes, card = await run_review(0, "0 findings")
    check("UNPARSABLE" in text, f"unstructured exit 0 must be UNPARSABLE, got {text!r}")
    check("incomplete" in classes, f"unparsable output must use warning styling, got {classes}")
    check(
        "next action  Open diagnostics and rerun the review." in card,
        f"unparsable output must direct diagnostics, got {card!r}",
    )

    text, classes, card = await run_review(
        0, clean_output, reviewed_head="a" * 40
    )
    check("STALE" in text, f"a mismatched reviewed head must report STALE, got {text!r}")
    check(
        "stale" in classes and "#review-status.stale" in tui.ReviewDashboard.CSS,
        f"a stale review must apply its warning style rule, got {classes}",
    )
    check(
        "next action  Rerun the review on the current head." in card,
        f"a stale review must direct a rerun, got {card!r}",
    )

    # The H0 result is retained by the real Stop and the actual `gh api
    # repos/<repo>/compare/H0...H1` boundary supplies H1 change evidence.
    # No test-only dashboard metadata participates in this journey.
    h0 = "a" * 40
    h1 = "0123456789abcdef0123456789abcdef01234567"
    base = "fedcba9876543210fedcba9876543210fedcba98"
    prior = tui.ReviewResult(
        1, "findings", {"critical": 0, "high": 2, "medium": 1, "low": 0},
        [
            {"severity": "high", "file": "image/entrypoint.sh", "line": 87, "end_line": 89, "title": "H0 changed"},
            {"severity": "medium", "file": "tests/image-contract.sh", "line": 401, "title": "H0 unchanged"},
            {"severity": "high", "file": "gone.py", "line": 7, "title": "H0 unmappable"},
        ],
        provenance={"head_sha": h0, "base_sha": base, "approval": "must not carry"},
    )
    re_review_output = "\n".join([
        "goose review: check 'main' completed: 3 finding(s)",
        '{"severity":"high","path":"image/entrypoint.sh","line_start":87,"line_end":89,"summary":"H1 changed","check":"main"}',
        '{"severity":"medium","path":"tests/image-contract.sh","line_start":401,"line_end":401,"summary":"H1 unchanged","check":"main"}',
        '{"severity":"low","path":"new.py","line_start":3,"line_end":3,"summary":"H1 [new]","check":"main"}',
        "goose review: orchestrator emitted 3 finding(s) from 1 check(s) (main: ran, 3 finding(s))",
    ])
    mapped_compare = json.dumps({"total_files": 1, "files": [{
        "filename": "image/entrypoint.sh", "status": "modified",
        "patch": "\n".join(
            ["@@ -80,5 +80,11 @@"] + ["-old"] * 5 + ["+new"] * 11
        ),
    }]})
    text, classes, card = await run_review(
        0, re_review_output, prior_result=prior, compare_json=mapped_compare,
    )
    check("RE-REVIEW" in card and f"reviewed {h0}" in card and f"current {h1}" in card,
          "the production compare journey must name both exact H0 and H1 identities")
    check(f"H1 manifest  base {base}  head {h1}  result findings" in card,
          "the production delta must carry its current H1 manifest and result")
    check("image/entrypoint.sh:87=changed-region" in card and
          "tests/image-contract.sh:401=unchanged-region" in card and
          "gone.py:7=invalidated-unmappable" in card,
          "the production delta must render bounded changed, unchanged, and unmappable H0 dispositions: " + repr(card))
    check("new.py:3 (new.py:3)" in card and "No authority carried from H0." in card and "must not carry" not in card,
          "new H1 evidence must be escaped and H0 authority must not carry forward")
    check(any(f"api repos/projectbluefin/bluefinctl/compare/{h0}...{h1}" in line
              for line in gh_log.read_text().splitlines()),
          "the Pilot must stub and exercise the production gh compare boundary")

    # The official compare response has no total_files field. A bounded page
    # with complete patch metadata must still reach deterministic delta
    # classification, and the request must make its page bound explicit.
    official_compare = json.dumps({
        "status": "ahead", "ahead_by": 14, "behind_by": 0,
        "total_commits": 14, "commits": [],
        "files": [{
            "filename": "image/entrypoint.sh", "status": "modified",
            "patch": "\n".join(
                ["@@ -80,5 +80,11 @@"] + ["-old"] * 5 + ["+new"] * 11
            ),
        }],
    })
    text, classes, card = await run_review(
        0, re_review_output, prior_result=prior,
        compare_json=official_compare,
    )
    check(
        "FULL REVIEW REQUIRED" not in card
        and "image/entrypoint.sh:87=changed-region" in card,
        f"the official no-total_files compare shape must reach deterministic mapping: {card!r}",
    )
    check(
        any(
            f"api repos/projectbluefin/bluefinctl/compare/{h0}...{h1}" in line
            and "--method GET" in line
            and "--field per_page=128" in line
            and "--field page=1" in line
            for line in gh_log.read_text().splitlines()
        ),
        "the official compare request must explicitly bind its bounded first page",
    )

    # Agent-sourced H1 paths and finding identifiers are attacker-shaped
    # markup input. The real ReviewScreen must render them literally, not let
    # Rich interpret a tag or raise while composing the decision card.
    attacker_path = "[blink]OWNED"
    attacker_output = "\n".join([
        "goose review: check 'main' completed: 1 finding(s)",
        json.dumps({
            "severity": "high", "path": attacker_path, "line_start": 9,
            "line_end": 9, "summary": "[blink]OWNED", "check": "main",
        }),
        "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))",
    ])
    attacker_compare = json.dumps({"total_files": 1, "files": [{
        "filename": attacker_path, "status": "modified",
        "patch": "@@ -1 +9 @@\n-old\n+new",
    }]})
    text, classes, card = await run_review(
        0, attacker_output, prior_result=prior, compare_json=attacker_compare,
    )
    check(
        f"{attacker_path}:9" in card and "RE-REVIEW" in card,
        "the real re-review Pilot must render attacker-shaped evidence literally",
    )

    # GitHub compare responses must carry a trustworthy file count. Malformed
    # types, negative values, and counts inconsistent with the returned page
    # cannot support H0 mapping and must fall back to a full review.
    for label, malformed_compare in (
        ("a string total_files", {"total_files": "1", "files": []}),
        ("a negative total_files", {"total_files": -1, "files": []}),
        ("a smaller total_files", {
            "total_files": 0,
            "files": [{"filename": "image/entrypoint.sh", "patch": "@@ -80 +80 @@"}],
        }),
    ):
        _text, _classes, malformed_card = await run_review(
            0, re_review_output, prior_result=prior,
            compare_json=json.dumps(malformed_compare),
        )
        check(
            "FULL REVIEW REQUIRED" in malformed_card
            and "mapping-uncertain" in malformed_card,
            f"{label} must fail closed to full review: {malformed_card!r}",
        )

    # A file entry with a textual patch but no unified-diff hunk is partial
    # evidence, just like an omitted patch; it cannot prove unchanged H0.
    _text, _classes, no_hunk_card = await run_review(
        0, re_review_output, prior_result=prior,
        compare_json=json.dumps({
            "total_files": 1,
            "files": [{"filename": "image/entrypoint.sh", "patch": "partial file evidence"}],
        }),
    )
    check(
        "FULL REVIEW REQUIRED" in no_hunk_card
        and "mapping-uncertain" in no_hunk_card,
        f"a no-hunk partial file entry must fail closed: {no_hunk_card!r}",
    )

    for label, truncated_patch in (
        ("a bodyless hunk", "@@ -80,5 +80,5 @@"),
        ("a truncated hunk", "@@ -80,5 +80,5 @@\n-old\n+new"),
    ):
        _text, _classes, truncated_card = await run_review(
            0, re_review_output, prior_result=prior,
            compare_json=json.dumps({
                "total_files": 1,
                "files": [{"filename": "image/entrypoint.sh", "patch": truncated_patch}],
            }),
        )
        check(
            "FULL REVIEW REQUIRED" in truncated_card
            and "mapping-uncertain" in truncated_card,
            f"{label} must fail closed to full review: {truncated_card!r}",
        )

    _text, _classes, omitted_patch_card = await run_review(
        0, re_review_output, prior_result=prior,
        compare_json=json.dumps({
            "total_files": 1,
            "files": [{"filename": "image/entrypoint.sh"}],
        }),
    )
    check(
        "FULL REVIEW REQUIRED" in omitted_patch_card
        and "mapping-uncertain" in omitted_patch_card,
        f"an omitted patch must fail closed: {omitted_patch_card!r}",
    )

    # A pure addition is valid unified-diff evidence: the old side starts at
    # zero with zero lines, while the new side has real added lines. It must
    # remain deterministic rather than being rejected with every start-zero
    # hunk.
    pure_add_output = "\n".join([
        "goose review: check 'main' completed: 1 finding(s)",
        json.dumps({
            "severity": "low", "path": "added.py", "line_start": 1,
            "line_end": 2, "summary": "added evidence", "check": "main",
        }),
        "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))",
    ])
    pure_add_compare = json.dumps({"total_files": 1, "files": [{
        "filename": "added.py", "status": "added",
        "patch": "@@ -0,0 +1,2 @@\n+one\n+two",
    }]})
    _text, _classes, pure_add_card = await run_review(
        0, pure_add_output, prior_result=prior, compare_json=pure_add_compare,
    )
    check(
        "FULL REVIEW REQUIRED" not in pure_add_card
        and "added.py:1 (added.py:1)" in pure_add_card,
        f"a valid pure-add hunk must map deterministically: {pure_add_card!r}",
    )

    # A pure deletion has no H1 line range. It must not let a matching H0
    # finding fall through as unchanged evidence.
    pure_delete_output = "\n".join([
        "goose review: check 'main' completed: 1 finding(s)",
        json.dumps({
            "severity": "high", "path": "image/entrypoint.sh", "line_start": 87,
            "line_end": 89, "summary": "deleted evidence", "check": "main",
        }),
        "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))",
    ])
    pure_delete_compare = json.dumps({"total_files": 1, "files": [{
        "filename": "image/entrypoint.sh", "status": "modified",
        "patch": "@@ -87,3 +0,0 @@\n-old\n-old\n-old",
    }]})
    _text, _classes, pure_delete_card = await run_review(
        0, pure_delete_output, prior_result=prior,
        compare_json=pure_delete_compare,
    )
    check(
        "FULL REVIEW REQUIRED" in pure_delete_card
        and "mapping-uncertain" in pure_delete_card
        and "image/entrypoint.sh:87=unchanged-region" not in pure_delete_card,
        f"a pure-deletion hunk must fail closed: {pure_delete_card!r}",
    )

    for label, malformed_hunk in (
        ("a zero-count hunk", "@@ -0,0 +0,0 @@"),
        ("a nonzero old count at start zero", "@@ -0,1 +1,1 @@\n-old\n+new"),
        ("a nonzero new count at start zero", "@@ -1,1 +0,1 @@\n-old\n+new"),
    ):
        _text, _classes, malformed_hunk_card = await run_review(
            0, re_review_output, prior_result=prior,
            compare_json=json.dumps({
                "total_files": 1,
                "files": [{"filename": "image/entrypoint.sh", "patch": malformed_hunk}],
            }),
        )
        check(
            "FULL REVIEW REQUIRED" in malformed_hunk_card
            and "mapping-uncertain" in malformed_hunk_card,
            f"{label} must fail closed: {malformed_hunk_card!r}",
        )

    # An empty filename is not a map key. Exercise it with both a deletion
    # hunk and the zero-count form so neither shape bypasses the guard.
    for label, empty_filename_patch in (
        ("an empty filename deletion", "@@ -87,1 +0,0 @@\n-old"),
        ("an empty filename zero-count hunk", "@@ -0,0 +0,0 @@"),
    ):
        _text, _classes, empty_filename_card = await run_review(
            0, re_review_output, prior_result=prior,
            compare_json=json.dumps({
                "total_files": 1,
                "files": [{"filename": "", "patch": empty_filename_patch}],
            }),
        )
        check(
            "FULL REVIEW REQUIRED" in empty_filename_card
            and "mapping-uncertain" in empty_filename_card,
            f"{label} must fail closed: {empty_filename_card!r}",
        )

    stale_prior = tui.ReviewResult(
        1, "findings", {"critical": 0, "high": 1, "medium": 0, "low": 0},
        [{"severity": "high", "file": "stale.py", "line": 7, "title": "H0 stale"}],
        provenance={"head_sha": h0, "base_sha": base},
    )
    stale_output = "\n".join([
        "goose review: check 'main' completed: 1 finding(s)",
        '{"severity":"high","path":"stale.py","line_start":7,"line_end":7,"summary":"H1 stale","check":"main"}',
        "goose review: orchestrator emitted 1 finding(s) from 1 check(s) (main: ran, 1 finding(s))",
        "REVIEW INCOMPLETE — a check returned no verdict",
    ])
    text, classes, card = await run_review(
        65, stale_output, prior_result=stale_prior,
        compare_json=json.dumps({"total_files": 0, "files": []}),
    )
    check("stale.py:7=stale-re-evaluate" in card,
          "an incomplete H1 result must mark matching H0 evidence stale for re-evaluation")

    text, classes, card = await run_review(
        0, re_review_output, prior_result=prior, compare_fail=True,
    )
    check("FULL REVIEW REQUIRED" in card and "mapping-uncertain" in card and "capability-absent" in card,
          "a failed production compare must fail closed with concrete uncertainty and capability reasons: " + repr(card))

    sensitive_compare = json.dumps({"total_files": 1, "files": [{
        "filename": ".github/workflows/publish.yml", "status": "modified",
        "patch": "@@ -1 +1 @@\n-old\n+new",
    }]})
    text, classes, card = await run_review(
        0, re_review_output, prior_result=prior, compare_json=sensitive_compare,
    )
    check("sensitive-surface-changed" in card,
          "a sensitive H1 compare path must require a full review: " + repr(card))

    risk_compare = json.dumps({"total_files": 129, "files": []})
    text, classes, card = await run_review(
        0, re_review_output, prior_result=prior, compare_json=risk_compare,
    )
    check("bounded-risk-exceeded" in card,
          "a compare beyond the bounded review budget must require a full review")

    merge_base_prior = tui.ReviewResult(
        prior.version, prior.state, prior.counts, prior.findings,
        provenance={"head_sha": h0, "base_sha": "b" * 40},
    )
    text, classes, card = await run_review(
        0, re_review_output, prior_result=merge_base_prior, compare_json=mapped_compare,
    )
    check("merge-base-changed" in card,
          "a changed exact merge base must require a full review")

    incomplete_prior = tui.ReviewResult(
        prior.version, "incomplete", prior.counts, prior.findings,
        provenance={"head_sha": h0, "base_sha": base},
    )
    text, classes, card = await run_review(
        0, re_review_output, prior_result=incomplete_prior, compare_json=mapped_compare,
    )
    check("prior-review-incomplete" in card,
          "an incomplete H0 review must not supply authority to H1")

    malformed_prior = tui.ReviewResult(
        prior.version, prior.state, prior.counts, prior.findings,
        provenance={"head_sha": h0},
    )
    text, classes, card = await run_review(0, re_review_output, prior_result=malformed_prior)
    check("FULL REVIEW REQUIRED" in card and "historical H0 merge base unavailable" in card,
          "missing historical H0 evidence must fail closed instead of hiding the delta")

    same_head_prior = tui.ReviewResult(
        prior.version, prior.state, prior.counts, prior.findings,
        provenance={"head_sha": h1, "base_sha": base},
    )
    text, classes, card = await run_review(0, re_review_output, prior_result=same_head_prior)
    check("RE-REVIEW" not in card,
          "a same-head result must preserve the ordinary decision card without a re-review projection")

    # Harness discovery is asynchronous and can report after the dashboard's
    # widgets have been torn down. Exercise the actual app lifecycle and prove
    # the late callback is harmless rather than only grepping NoMatches.
    late_app = tui.ReviewDashboard(tui.QueueFilters())
    late_options = []
    async with late_app.run_test() as pilot:
        for _ in range(200):
            if late_app.harness_options:
                late_options = list(late_app.harness_options)
                break
            await pilot.pause(0.05)
        check(late_options, "the late-discovery Pilot needs real harness options")
        late_app.exit()
    late_queue_snapshot = {
        "self_login": "fixture-user",
        "state": "ready",
        "message": "",
        "items": [SNAPSHOT["items"][0]],
    }
    try:
        late_app._apply_queue_snapshot(late_queue_snapshot)
        check(
            True,
            "a queue snapshot arriving after dashboard teardown must be ignored safely",
        )
    except Exception as error:
        check(
            False,
            f"a queue snapshot arriving after dashboard teardown raised {error!r}",
        )
    try:
        late_app.harness_loaded(late_options)
        check(True, "late harness discovery after unmount must be harmless")
    except Exception as error:
        check(False, f"late harness discovery after unmount raised {error!r}")

    # ── the regression that started this: a review whose checks returned no
    # verdict must never read as clean ───────────────────────────────────
    text, classes, card = await run_review(65, incomplete_output)
    check("INCOMPLETE" in text, f"exit 65 must report INCOMPLETE, got {text!r}")
    check("incomplete" in classes, f"exit 65 must carry the incomplete style, got {classes}")
    check(
        "COMPLETE" not in text.replace("INCOMPLETE", ""),
        "an incomplete review must not also claim to be complete",
    )
    check(
        "not a clean bill of health" in text.lower() or "NOT a clean" in text,
        f"an incomplete review must say the finding count is not clean, got {text!r}",
    )

    # ── a failed review is a failure, not an empty result ────────────────
    text, classes, card = await run_review(3, "boom")
    check("FAILED" in text, f"a nonzero exit must report FAILED, got {text!r}")
    check("failed" in classes, f"a failed review must carry the failed style, got {classes}")

    # ── completed-card actions return through the existing mutation gate ─
    review_stub(0, clean_output)
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        stop = app.stops[0]
        stop.live = {
            "isDraft": False,
            "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
            "headRefOid": "0123456789abcdef0123456789abcdef01234567",
        }
        await pilot.press("r")
        for _ in range(400):
            if isinstance(app.screen, tui.ReviewScreen) and app.screen.finished:
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.ReviewScreen) and app.screen.finished,
            "the card action test needs a completed review",
        )
        await pilot.press("a")
        for _ in range(200):
            if isinstance(app.screen, tui.ConfirmMutation):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            "[a] on the decision card must reach the existing typed-number gate",
        )
        if isinstance(app.screen, tui.ConfirmMutation):
            check(
                app.screen.expected == "31"
                and len(app.screen.commands) == 1
                and app.screen.commands[0][1] == str(hive_api_stub)
                and app.screen.commands[0][2] == "queue"
                and app.screen.commands[0][-1].endswith("/queue-automerge"),
                f"the card must preserve the queue action, got {app.screen.commands}",
            )
            await pilot.press("escape")
            await pilot.pause()

        # The [f]/[F] background fix-and-land lane is deleted: findings are
        # fixed through [$], which dispatches the same fixer behind slay's
        # gates. [f] on ReviewScreen must do nothing to the landing queue.
        fix_stop = app.stops[0]
        fix_stop.live = {
            "isDraft": False,
            "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
            "headRefOid": "0123456789abcdef0123456789abcdef01234567",
        }
        fix_stop.review_result = tui.ReviewResult(
            1,
            "findings",
            findings=({"severity": "high", "title": "fix me"},),
            provenance={
                "base_sha": "fedcba9876543210fedcba9876543210fedcba98",
                "head_sha": "0123456789abcdef0123456789abcdef01234567",
            },
        )
        fix_screen = tui.ReviewScreen(fix_stop)
        fix_screen.finished = True
        app.push_screen(fix_screen)
        for _ in range(400):
            if all(w.is_finished for w in fix_screen.workers):
                break
            await pilot.pause(0.05)
        await pilot.pause()
        check(isinstance(app.screen, tui.ReviewScreen), "ReviewScreen must be active")
        await pilot.press("f")
        await pilot.pause()
        check(
            not any(t.stops and t.stops[0].number == fix_stop.number for t in app.landing_queue),
            "[f] must not dispatch a gate-bypassing fix task; slay owns fixes",
        )
        check(isinstance(app.screen, tui.ReviewScreen), "[f] must be inert on ReviewScreen")
        await pilot.press("escape")
        await pilot.pause()
        app.landing_queue.clear()

    # ── the steer box: typed text reaches the review as instructions ─────
    review_stub(0, "0 findings")
    steer_log.write_text("")
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        await pilot.press("slash")
        await pilot.pause()
        box = app.query_one("#steer", tui.Input)
        check(app.focused is box, "'/' must focus the steer box")
        await pilot.press("c", "i")
        check(box.value == "ci", f"the steer box must take keystrokes, got {box.value!r}")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        if not isinstance(screen, tui.ReviewScreen):
            check(False, f"steering must open a review, got {type(screen).__name__}")
        else:
            check(screen.steer == "ci", f"the review must carry the steer, got {screen.steer!r}")
            for _ in range(400):
                if screen.finished:
                    break
                await pilot.pause(0.05)
            check(screen.finished, "the steered review never finished")
            check(
                steer_log.read_text().splitlines()[-1:] == ["ci"],
                "the steer must reach the review engine as "
                f"BLUEFIN_REVIEW_STEER, got {steer_log.read_text()!r}",
            )
        check(
            app.query_one("#steer", tui.Input).value == "",
            "the steer box must clear after it is submitted",
        )

    # ── an unsteered review must not inherit a stale steer ───────────────
    steer_log.write_text("")
    await run_review(0, clean_output)
    check(
        steer_log.read_text().splitlines()[-1:] == [""],
        f"an unsteered review must carry no steer, got {steer_log.read_text()!r}",
    )

    # ── [x] actually stops a review ──────────────────────────────────────
    # The engine is a shell that runs Goose, which runs a subprocess per check.
    # Signalling only the shell leaves those children alive holding the pipe
    # open, and the screen would wait on them forever. This stub reproduces
    # that shape: a grandchild that survives its parent and ignores SIGTERM.
    marker = workdir / "grandchild-alive"
    write_stub(
        workdir / "bluefin-review",
        f'printf "%s\\n" "$*" >>"{review_log}"\n'
        "echo starting\n"
        f'( trap "" TERM; touch "{marker}"; sleep 60; rm -f "{marker}" ) &\n'
        'trap "" TERM\n'
        "wait\n",
    )
    app = tui.ReviewDashboard(tui.QueueFilters())
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        if not isinstance(screen, tui.ReviewScreen):
            check(False, "'r' must open the review screen for the stop test")
        else:
            for _ in range(200):
                if marker.exists():
                    break
                await pilot.pause(0.05)
            check(marker.exists(), "the stop-test stub never started its grandchild")
            tui.STOP_GRACE_SECONDS = 0.2
            await pilot.press("x")
            deadline = time.monotonic() + 30
            while not screen.finished and time.monotonic() < deadline:
                await pilot.pause(0.05)
            check(screen.finished, "[x] must end a review that ignores SIGTERM")
            status = screen.query_one("#review-status", tui.Static)
            check(
                "STOPPED" in str(status.render()),
                f"a stopped review must report STOPPED, got {str(status.render())!r}",
            )
            check(
                "COMPLETE" not in str(status.render()),
                "a stopped review must never report COMPLETE",
            )

    # ── the review path never mutates GitHub ─────────────────────────────
    # Invalid live refs disable Codex start honestly and never invoke it.
    tui.ACTIVE_BACKEND = "codex"
    real_probe = tui.CodexHarness.probe
    tui.CodexHarness.probe = classmethod(lambda cls: tui.Availability.READY)
    real_popen = tui.subprocess.Popen
    review_log.write_text("")
    try:
        codex_calls = []
        codex_output = "\n".join(
            (
                '{"type":"thread.started","thread_id":"thread_1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_1",'
                '"type":"agent_message","text":"{\\"version\\":1,'
                '\\"state\\":\\"complete\\",\\"counts\\":{\\"critical\\":0,'
                '\\"high\\":0,\\"medium\\":0,\\"low\\":0},\\"findings\\":[]}"}}',
                '{"type":"turn.completed","usage":{}}',
            )
        ) + "\n"

        class CodexProcess:
            stdout = iter(codex_output.splitlines(keepends=True))
            returncode = 0

            @staticmethod
            def wait():
                return 0

        def codex_popen(*args, **kwargs):
            command = args[0] if args else kwargs.get("args")
            if isinstance(command, (list, tuple)) and command[:2] == ["codex", "exec"]:
                codex_calls.append((command, kwargs))
                return CodexProcess()
            return real_popen(*args, **kwargs)

        tui.subprocess.Popen = codex_popen
        os.environ["GH_TOKEN"] = "write-capable-test-token"

        # Cancelling while the availability probe is still running must not
        # allow Codex to start after the stop request has already landed.
        probe_started = threading.Event()
        release_probe = threading.Event()

        def delayed_probe(cls):
            probe_started.set()
            release_probe.wait(timeout=10)
            return tui.Availability.READY

        tui.CodexHarness.probe = classmethod(delayed_probe)
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            await settle_evidence(app, pilot)
            app.stops[0].live = {
                "isDraft": False,
                "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
                "headRefOid": "0123456789abcdef0123456789abcdef01234567",
            }
            app.start_review(app.stops[0])
            await pilot.press("tab", "enter")
            for _ in range(200):
                if probe_started.is_set():
                    break
                await pilot.pause(0.05)
            check(probe_started.is_set(), "the delayed Codex probe must start")
            screen = app.screen
            await pilot.press("x")
            release_probe.set()
            for _ in range(200):
                if isinstance(screen, tui.ReviewScreen) and screen.finished:
                    break
                await pilot.pause(0.05)
            check(
                isinstance(screen, tui.ReviewScreen) and screen.finished,
                "cancelling during the Codex probe must finish the review",
            )
            check(not codex_calls, "Codex must not start after probe-time cancellation")

        tui.CodexHarness.probe = classmethod(lambda cls: tui.Availability.READY)
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            await settle_evidence(app, pilot)
            stop_target = next((s for s in app.stops if s.key == "projectbluefin/bluefinctl#31"), app.stops[0])
            stop_target.live = {
                "isDraft": False,
                "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
                "headRefOid": "0123456789abcdef0123456789abcdef01234567",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "SUCCESS"}
                ],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            }
            stop_target.overlap = {"duplicates": [1], "overlaps": [2]}
            app.start_review(stop_target, "focus on exact-head evidence")
            await pilot.press("tab", "enter")
            for _ in range(200):
                if isinstance(app.screen, tui.ReviewScreen) and app.screen.finished:
                    break
                await pilot.pause(0.05)
            status = app.screen.query_one("#review-status", tui.Static)
            check(
                "COMPLETE" in str(status.render()),
                "valid Codex stdout JSONL must produce a completed decision card",
            )
            check(
                codex_calls[-1][1]["stderr"] is subprocess.DEVNULL,
                "Codex stderr must stay outside the official stdout JSONL lifecycle",
            )
            check(
                "GH_TOKEN" not in codex_calls[-1][1]["env"]
                and "GITHUB_TOKEN" not in codex_calls[-1][1]["env"],
                "Codex review subprocess must not inherit GitHub mutation credentials",
            )
            check(
                "focus on exact-head evidence" in codex_calls[-1][0][-1],
                "dashboard steering must reach the Codex invocation prompt",
            )
            # The finished flag can land ahead of the card's evidence render;
            # wait on the card's content, not a fixed beat (#284).
            for _ in range(600):
                card = str(app.screen.query_one("#review-card", tui.Static).render())
                if "CI success" in card and "MERGEABLE/CLEAN" in card:
                    break
                await pilot.pause(0.05)
            card = str(app.screen.query_one("#review-card", tui.Static).render())
            check(
                "CI success" in card and "MERGEABLE/CLEAN" in card
                and "1 duplicate / 1 shared-file hazard" in card,
                f"Codex card must merge trusted live and overlap evidence, got {card!r}",
            )

        tui.subprocess.Popen = real_popen
        os.environ.pop("GH_TOKEN", None)
        app = tui.ReviewDashboard(tui.QueueFilters())
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            app.stops[0].live = {"isDraft": False, "headRefOid": "bad"}
            await pilot.press("r")
            await pilot.press("tab", "enter")
            for _ in range(200):
                if isinstance(app.screen, tui.ReviewScreen) and app.screen.finished:
                    break
                await pilot.pause(0.05)
            check(
                isinstance(app.screen, tui.ReviewScreen) and app.screen.finished,
                "invalid live refs must finish as unavailable",
            )
            check(
                review_log.read_text() == "",
                "invalid live refs must not invoke Codex",
            )
    finally:
        os.environ.pop("GH_TOKEN", None)
        tui.subprocess.Popen = real_popen
        tui.CodexHarness.probe = real_probe
    tui.ACTIVE_BACKEND = "goose"

    # ── the review path never mutates GitHub ─────────────────────────────
    calls = gh_log.read_text().splitlines() if gh_log.exists() else []
    mutations = [
        call
        for call in calls
        if any(
            call.startswith(verb)
            for verb in ("pr merge", "pr close", "pr comment", "pr edit", "pr review")
        )
    ]
    check(not mutations, f"reviewing must not mutate GitHub, saw: {mutations}")

    # ── the review is traced for the feedback loop ───────────────────────
    trace_file = Path(tui.TRACE_PATH)
    records = (
        [json.loads(line) for line in trace_file.read_text().splitlines() if line.strip()]
        if trace_file.exists()
        else []
    )
    outcomes = [r["outcome"] for r in records if r.get("action") == "review"]
    check(
        outcomes == [
            "complete", "stale", "complete", "incomplete", "stale", "complete",
            "complete", "complete", "complete", "complete", "complete", "complete", "complete", "complete", "complete",
            "complete", "complete", "complete", "complete", "complete", "complete", "complete",
            "incomplete",
            "complete", "complete", "complete", "complete", "complete", "complete", "complete",
            "incomplete", "failed", "complete", "complete", "stale", "complete", "stopped", "stopped",
            "complete", "error",
        ],
        f"every review must be traced with its outcome, got {outcomes}",
    )

    # ── mixed workboard: toggling, triage details, and mutations ─────────
    gh_log.write_text("")
    os.environ["GH_TOKEN"] = "dashboard-pilot-token"
    set_org_queue(SNAPSHOT["items"])
    set_org_issues([issue_node])
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with app.run_test() as pilot:
        await wait_for_live_rows(app, pilot, "ready", 3)
        check(app.view_mode == "prs", "dashboard must start in PR view_mode")
        check(
            app.stops and all(not s.is_issue for s in app.stops),
            "default PR view must contain only pull requests",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check(
            "[I] PR view" in status and "Queue: 2 PRs" in status,
            f"status bar must reflect PR lens, got {status!r}",
        )

        await pilot.press("tab")
        await pilot.pause()
        check(
            app.view_mode == "prs",
            "Tab must retain Textual's focus traversal instead of changing views",
        )
        app.query_one("#queue", tui.ListView).focus()

        # Reach the optional mixed workboard for shared PR/issue selection.
        await pilot.press("I")
        await pilot.press("I")
        for _ in range(200):
            if (
                app.view_mode == "mixed"
                and any(not s.is_issue for s in app.stops)
                and any(s.is_issue for s in app.stops)
            ):
                break
            await pilot.pause(0.05)
        check(
            app.view_mode == "mixed"
            and any(not s.is_issue for s in app.stops)
            and any(s.is_issue for s in app.stops),
            "mixed view must contain both PRs and issues",
        )

        # Shared selection across PRs and issues
        app.stops[0].selected = True
        app.stops[2].selected = True
        app.refresh_rows()
        await wait_for_rendered_rows(app, pilot)
        check(
            any(s.is_issue and s.selected for s in app.stops)
            and any(not s.is_issue and s.selected for s in app.stops),
            f"selection must cover both PR and issue rows, got {[(s.key, s.selected) for s in app.stops]}",
        )

        # Press I: verify view_mode == "prs"
        await pilot.press("I")
        check(app.view_mode == "prs", "I must switch view_mode from mixed to prs")
        for _ in range(200):
            if app.stops and all(not s.is_issue for s in app.stops):
                break
            await pilot.pause(0.05)
        check(
            len(app.stops) == 2 and all(not s.is_issue for s in app.stops),
            f"prs view must contain only PRs, got {app.stops}",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check("[I] PR view" in status, f"status bar must reflect PR lens, got {status!r}")

        # Press I again: verify view_mode == "issues"
        await pilot.press("I")
        check(app.view_mode == "issues", "I must switch view_mode from prs to issues")
        for _ in range(200):
            if app.stops and app.stops[0].is_issue and len(app._queue().children) > 0:
                break
            await pilot.pause(0.05)
        check(
            len(app.stops) > 0 and app.stops[0].is_issue is True,
            f"issues view must populate stops with is_issue=True, got {app.stops}",
        )
        status = str(app.query_one("#status-bar", tui.Static).render())
        check("[I] Issues view" in status, f"status bar must reflect issues lens, got {status!r}")

        first_item = app._queue().children[0]
        check(
            isinstance(first_item, tui.ListItem),
            f"queue child must be a ListItem, got {type(first_item).__name__}",
        )
        row = str(first_item.query(tui.Label).first().render())
        check(
            "bug: test issue" in row and "[triage]" in row,
            f"queue issue row must contain 'bug: test issue' and '[triage]', got {row!r}",
        )

        # Highlighted issue renders details (including title and body)
        for _ in range(200):
            details = str(app.query_one("#details", tui.Static).render())
            if "bug: test issue" in details and "Issue description test body" in details:
                break
            await pilot.pause(0.05)
        details = str(app.query_one("#details", tui.Static).render())
        check(
            "bug: test issue" in details and "Issue description test body" in details,
            f"highlighted issue must render details with title and body, got {details!r}",
        )

        # Steering on an issue must not focus the steer box or open ReviewScreen
        await pilot.press("slash")
        await pilot.pause()
        box = app.query_one("#steer", tui.Input)
        check(app.focused is not box, "'/' on an issue must not focus the steer box")
        check(not isinstance(app.screen, tui.ReviewScreen), "steering on an issue must not open ReviewScreen")

        # Test c: comment on issue
        gh_log.write_text("")
        await pilot.press("c")
        for _ in range(50):
            if type(app.screen).__name__ == "CommentBody":
                break
            await pilot.pause(0.05)
        check(
            type(app.screen).__name__ == "CommentBody",
            f"pressing c must open CommentBody modal, got {type(app.screen).__name__}",
        )
        app.screen.query_one(tui.Input).value = "triage comment on issue"
        await pilot.press("ctrl+s")
        for _ in range(50):
            if isinstance(app.screen, tui.CommentPreview):
                break
            await pilot.pause(0.05)
        preview = app.screen
        check(
            isinstance(preview, tui.CommentPreview),
            f"submitting comment body must open CommentPreview, got {type(preview).__name__}",
        )
        if isinstance(preview, tui.CommentPreview):
            check(
                "triage comment on issue" in preview.body,
                "CommentPreview must show comment text",
            )
        else:
            failures.append("CommentPreview must show comment text")
        await pilot.click("#comment-preview-submit")
        for _ in range(50):
            if isinstance(app.screen, tui.ConfirmMutation):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            f"comment preview submit must reach ConfirmMutation gate, got {type(app.screen).__name__}",
        )
        gate = app.screen
        check(
            gate.commands[0][:5] == ["gh", "issue", "comment", "42", "--repo"]
            and gate.commands[0][5] == "projectbluefin/review"
            and "--body-file" in gate.commands[0],
            f"exact issue comment command structure, got {gate.commands}",
        )
        await pilot.press(*gate.expected)
        await pilot.press("enter")
        for _ in range(200):
            if "issue comment 42 --repo projectbluefin/review" in gh_log.read_text():
                break
            await pilot.pause(0.05)
        check(
            "issue comment 42 --repo projectbluefin/review --body-file" in gh_log.read_text(),
            f"gh_log must record exact issue comment command with --body-file, got {gh_log.read_text()!r}",
        )
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Test x: close issue
        gh_log.write_text("")
        await pilot.press("x")
        for _ in range(50):
            if isinstance(app.screen, tui.ConfirmMutation):
                break
            await pilot.pause(0.05)
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            f"pressing x must open ConfirmMutation modal, got {type(app.screen).__name__}",
        )
        gate = app.screen
        check(
            gate.commands[0][:5] == ["gh", "issue", "comment", "42", "--repo"]
            and gate.commands[1] == ["gh", "issue", "close", "42", "--repo", "projectbluefin/review"],
            f"exact issue close command sequence, got {gate.commands}",
        )
        await pilot.press(*gate.expected)
        await pilot.press("enter")
        for _ in range(200):
            if "issue close 42 --repo projectbluefin/review" in gh_log.read_text():
                break
            await pilot.pause(0.05)
        check(
            "issue close 42 --repo projectbluefin/review" in gh_log.read_text(),
            f"gh_log must record 'issue close 42 --repo projectbluefin/review', got {gh_log.read_text()!r}",
        )
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Press I again: verify cycling back to mixed
        await pilot.press("I")
        check(app.view_mode == "mixed", "pressing I again must switch view_mode back to mixed")
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        check(
            len(app.stops) == 2 and all(not s.is_issue for s in app.stops),
            f"switching back to mixed view must restore remaining open stops, got {app.stops}",
        )
    gh_log.write_text("")
    set_org_issues([])

    # Independent error modeling: failing issue search must not overwrite usable PR source state
    os.environ["ORG_ISSUES_FAIL"] = "1"
    issue_err_app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with issue_err_app.run_test() as pilot:
        for _ in range(200):
            if issue_err_app.stops and issue_err_app.pr_source_state == "ready":
                break
            await pilot.pause(0.05)
        check(
            issue_err_app.pr_source_state == "ready",
            "failing issue fetch must not overwrite usable PR source state",
        )
        check(
            issue_err_app.issues_source_state == "error",
            "failing issue fetch must record issue source state as error",
        )
        check(
            len(issue_err_app.stops) == 2,
            "usable PR stops must remain rendered even when issue search fails",
        )
    os.environ.pop("ORG_ISSUES_FAIL", None)
    gh_log.write_text("")

    # ── multi-repo selection partitions into concurrent landing tasks (#399) ──
    os.environ["BLUEFIN_REVIEW_PARTITION_BATCH"] = "1"
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    app.final_policy = "automatic"
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        for stop in app.stops:
            stop.selected = True
        app.action_land_batch()
        await pilot.pause()
        gate = app.screen
        check(
            isinstance(gate, tui.BatchPlanScreen),
            "multi-repo batch landing must gate with BatchPlanScreen",
        )
        check(
            isinstance(gate.plan, (list, tui._CompositePlan)) and len(gate.plan) == 2,
            f"multi-repo selection must partition into 2 landing tasks, got {gate.plan}",
        )
        await pilot.press("enter")
        await pilot.pause()
        check(
            not isinstance(app.screen, tui.LandingScreen)
            and not isinstance(app.screen, tui.BatchPlanScreen),
            "confirming partitioned batch must return to review queue",
        )
        await pilot.press("w")
        await pilot.pause()
        check(
            app.query_one("#landing-pause", tui.Button).has_focus,
            "[w] must focus persistent queue controls after partitioned batch dispatch",
        )
    os.environ["BLUEFIN_REVIEW_PARTITION_BATCH"] = "0"
    gh_log.write_text("")

    # ── option $: slay PR (review + fix if needed + land in batch) ──
    app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    # This state-machine fixture mutates one in-memory stop through several
    # synthetic outcomes. The operation-triggered reconciliation contract is
    # exercised above with real queue replacement; isolate this older unit of
    # behavior from its intentionally unrelated transport refresh.
    app._request_reconciliation = lambda: None
    original_fetch_live = app.fetch_live_pr

    def fixture_live(repository, number, force=False):
        # The production forced read rejects a headless response. This fixture
        # owns the exact live snapshot for each synthetic head instead of
        # routing the state-machine checks through the generic empty response.
        if os.environ.get("PR_VIEW_JSON"):
            return original_fetch_live(repository, number, force=force)
        candidate = next(
            item
            for item in app.stops
            if item.repository == repository and item.number == number
        )
        return dict(candidate.live)

    app.fetch_live_pr = fixture_live
    app.show_evidence = lambda *a, **kw: None
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) >= 1:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]

        async def slay_and_confirm() -> None:
            await pilot.press("$")
            await pilot.pause()
            if isinstance(app.screen, tui.SlayConfirmScreen):
                await pilot.press(*app.screen.expected)
                await pilot.press("enter")
                await pilot.pause()

        # Test 1: Slay on low-risk clean PR dispatches landing task immediately (#411 low-risk skip class)
        stop.title = "chore(deps): update pin"
        stop.head_sha = "a1" + "0" * 38
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.review_status = "complete"
        stop.review_result = None
        stop.live["reviews"] = [
            {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
        ]
        initial_landings = len(app.landing_queue)
        await pilot.press("$")
        await pilot.pause()
        check(
            isinstance(app.screen, tui.SlayConfirmScreen),
            "$ must push SlayConfirmScreen gate",
        )
        if isinstance(app.screen, tui.SlayConfirmScreen):
            check(
                app.screen.targets == [stop] and app.screen.expected == str(stop.number),
                "$ gate must show exact target PR and expected number",
            )
            await pilot.press(*app.screen.expected)
            await pilot.press("enter")
            await pilot.pause()
        task = next((t for t in app.landing_queue[initial_landings:] if not t.phase), None)
        check(
            task is not None,
            f"slay on low-risk clean PR must enqueue landing task, queue={app.landing_queue[initial_landings:]}",
        )
        if task:
            check(
                task.stops[0].key == stop.key,
                f"landing task must target stop {stop.key}, got {task.stops[0].key}",
            )

        # Test 1b: Cheap clean verdict on non-low-risk PR cannot authorise merge; triggers escalation (#411)
        stop.title = "fix: core memory leak"
        stop.head_sha = "e1" + "0" * 38
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.review_status = "complete"
        stop.review_result = None
        cheap_id = app.run_identity(stop, head_sha=stop.head_sha, model="gemini-3.8-flash", effort="max")
        app.run_store.create(cheap_id)
        app.run_store.transition(cheap_id, tui.RunState.REVIEWING)
        app.run_store.transition(cheap_id, tui.RunState.REVIEW_CLEAN)
        initial_landings = len(app.landing_queue)
        app.review_pending_keys.add(stop.key)
        await slay_and_confirm()
        candidate_landing = next(
            (
                t
                for t in app.landing_queue[initial_landings:]
                if not t.phase and any(s.key == stop.key for s in getattr(t, "stops", []))
            ),
            None,
        )
        check(
            candidate_landing is None,
            "cheap clean verdict on non-low-risk PR must not land without escalation",
        )
        cheap_rec = app.run_store.get(cheap_id)
        check(
            cheap_rec is not None and cheap_rec.state == tui.RunState.ESCALATION_REQUIRED,
            f"cheap clean review must reach ESCALATION_REQUIRED, got {cheap_rec}",
        )
        esc_triple = app.escalation_profile(stop)
        check(
            esc_triple[1] in tui.HIGH_ASSURANCE_MODELS,
            f"escalation profile model must be in HIGH_ASSURANCE_MODELS, got {esc_triple}",
        )
        # Assert the resolved model in run_store matches model_profiles.py (not a string in a log)
        strong_id = app.run_identity(stop, head_sha=stop.head_sha, model=esc_triple[1], effort=esc_triple[2])
        strong_rec = app.run_store.get(strong_id)
        check(
            strong_rec is not None and strong_rec.identity.model == esc_triple[1],
            f"escalated run must use model from model_profiles ({esc_triple[1]}), got {strong_rec}",
        )
        # Deliver clean completion for the high-assurance review -> now lands
        initial_landings = len(app.landing_queue)
        stop.review_status = "complete"
        app.apply_review_event(
            tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="high-assurance clean",
                timestamp=int(time.time()),
            )
        )
        await pilot.pause()
        slay_strong_task = next((t for t in app.landing_queue[initial_landings:] if not t.phase), None)
        check(
            slay_strong_task is not None,
            f"high-assurance clean review must dispatch landing task, queue={app.landing_queue[initial_landings:]}",
        )
        check(
            app.run_store.get(strong_id).state == tui.RunState.MUTATING,
            "strong review record must reach MUTATING on landing dispatch",
        )

        # Test 2: Slay on PR with findings dispatches fix task
        class MockResult:
            state = "findings"
            is_clean = False
            findings = [{"rule": "test-finding", "message": "issue found"}]
            provenance = {
                "repository": "projectbluefin/review",
                "pull_request": 42,
                "head_sha": "a2" + "0" * 38,
            }
            counts = {"high": 1}
            raw_evidence = []

        stop.head_sha = "a2" + "0" * 38
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.review_status = "findings"
        stop.review_result = MockResult()
        stop.live["reviews"] = [
            {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
        ]
        initial_landings = len(app.landing_queue)
        await slay_and_confirm()
        fix_task = next((t for t in app.landing_queue[initial_landings:] if "-fix" in t.task_id and not t.phase), None)
        check(
            fix_task is not None,
            f"slay on PR with findings must enqueue fix task, queue={app.landing_queue[initial_landings:]}",
        )
        if fix_task:
            for _ in range(200):
                if fix_task.returncode is not None:
                    break
                await pilot.pause(0.05)

        # Test 2b: Head produced by fixer is independently reviewed before landing (#411)
        fixed_head = "b2" + "0" * 38
        app.record_fixer_head(stop.repository, stop.number, fixed_head)
        check(
            app.is_fixer_head(stop.repository, stop.number, fixed_head),
            "app must explicitly recognise fixer-advanced head",
        )
        # Slay when head was advanced by fixer
        stop.head_sha = "a2" + "0" * 38
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = fixed_head
        stop.review_status = "complete"
        stop.review_result = None
        old_identity = app.run_identity(stop, head_sha="a2" + "0" * 38)
        initial_landings = len(app.landing_queue)
        app.review_pending_keys.add(stop.key)
        app._dispatch_slay_landing(stop, identity=old_identity)
        await pilot.pause()
        check(
            len(app.landing_queue) == initial_landings,
            "fixer-advanced head must not land before fresh review",
        )
        old_rec = app.run_store.get(old_identity)
        check(
            old_rec is not None and old_rec.state == tui.RunState.ESCALATION_REQUIRED,
            f"old run record must reach ESCALATION_REQUIRED, got {old_rec}",
        )
        fixed_id = app.run_identity(stop, head_sha=fixed_head, model=esc_triple[1], effort=esc_triple[2])
        fixed_rec = app.run_store.get(fixed_id)
        check(
            fixed_rec is not None and fixed_rec.identity.head_sha == fixed_head,
            f"fresh review must be bound to exact new head {fixed_head}",
        )
        check(
            fixed_rec.identity.model == esc_triple[1],
            f"fresh review on fixed head must use escalation profile model {esc_triple[1]}",
        )
        # Completing the review on the fixed head authorises merge
        initial_landings = len(app.landing_queue)
        stop.head_sha = fixed_head
        stop.review_status = "complete"
        app.apply_review_event(
            tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean on fixed head",
                timestamp=int(time.time()),
            )
        )
        await pilot.pause()
        fixed_land_task = next((t for t in app.landing_queue[initial_landings:] if not t.phase), None)
        check(
            fixed_land_task is not None,
            f"clean review of fixer-advanced head must authorise landing, queue={app.landing_queue[initial_landings:]}",
        )
        check(
            app.run_store.get(fixed_id).state == tui.RunState.MUTATING,
            "fixed head record must reach MUTATING on landing",
        )

        # Test 3: Slay on unreviewed low-risk PR registers in run_store and triggers on event
        stop.title = "chore(deps): bump deps"
        stop.head_sha = "a3" + "0" * 38
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.review_status = "unreviewed"
        stop.review_result = None
        stop.selected = False
        stop.live["reviews"] = [
            {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
        ]
        stop_identity = app.run_identity(stop)
        app.review_pending_keys.add(stop.key)  # avoid network dispatch in pilot
        await slay_and_confirm()
        record_in_flight = app.run_store.get(stop_identity)
        check(
            record_in_flight is not None and record_in_flight.in_flight and record_in_flight.state == tui.RunState.REVIEWING,
            f"{stop.key} must be registered in run_store as REVIEWING when unreviewed",
        )
        # Deliver completion event
        initial_landings = len(app.landing_queue)
        stop.review_status = "complete"
        app.apply_review_event(
            tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean",
                timestamp=int(time.time()),
            )
        )
        await pilot.pause()
        record_after = app.run_store.get(stop_identity)
        check(
            record_after is not None and record_after.state == tui.RunState.MUTATING,
            f"{stop.key} run record must transition to MUTATING on slay dispatch",
        )
        slay_task = next((t for t in app.landing_queue[initial_landings:] if not t.phase), None)
        check(
            slay_task is not None,
            f"completion event must dispatch landing task, queue={app.landing_queue[initial_landings:]}",
        )

        # Test 4: Slay guarded in issues view
        if slay_task:
            for _ in range(400):
                if (
                    slay_task.returncode is not None
                    and any(t.phase for t in app.landing_queue[initial_landings:])
                    and all(t.returncode is not None for t in app.landing_queue[initial_landings:])
                    and not any(app._landing_task_active(t) for t in app.landing_queue)
                ):
                    break
                await pilot.pause(0.05)
        app.view_mode = "issues"
        initial_landings = len(app.landing_queue)
        await pilot.press("$")
        await pilot.pause()
        check(
            len(app.landing_queue) == initial_landings,
            "slay in issues view must be ignored",
        )
        app.view_mode = "prs"

        # ── #414: Slay refuses PR with no human review at landing gate ──
        stop.repository = "projectbluefin/common"
        stop.review_status = "complete"
        stop.review_result = None
        stop.live["reviews"] = []  # No human review on GitHub!
        # Head must advance to re-slay
        stop.head_sha = "1" * 40
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        no_human_identity = app.run_identity(stop)
        initial_landings = len(app.landing_queue)
        notices: list[str] = []
        real_notify = app.notify

        def record_notice(message, *args, _notices=notices, _notify=real_notify, **kwargs):
            _notices.append(message)
            _notify(message, *args, **kwargs)

        app.notify = record_notice
        await slay_and_confirm()
        app.notify = real_notify
        check(
            len(app.landing_queue) == initial_landings,
            "slay must refuse to land PR with no human review",
        )
        no_human_record = app.run_store.get(no_human_identity)
        check(
            no_human_record is not None
            and no_human_record.state == tui.RunState.HUMAN_REVIEW_MISSING,
            f"PR lacking human review must reach its own terminal state, distinct from a failed merge, got {no_human_record}",
        )
        check(
            "no human review" in (stop.failure or "").lower(),
            f"stop failure must record missing human review, got {stop.failure!r}",
        )
        check(
            f"[$] {stop.key}: landing blocked — GitHub has no qualifying human review. "
            f"{stop.repository} requires a human review; leave one with [L], "
            "then re-run [$]; no merge was attempted."
            in notices,
            "missing human review must explain the qualifying action and safe retry",
        )
        stop.repository = "projectbluefin/bluefinctl"

        # ── #410: Head change between review and mutation refuses landing ──
        stop.review_status = "complete"
        stop.review_result = None
        stop.head_sha = "2" * 40
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.live["reviews"] = [
            {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
        ]
        head_change_identity = app.run_identity(stop)
        app.run_store.create(head_change_identity)
        app.run_store.transition(head_change_identity, tui.RunState.REVIEWING)
        app.run_store.transition(head_change_identity, tui.RunState.REVIEW_CLEAN)
        # Advance live head to simulate head mutation on GitHub before landing
        os.environ["PR_VIEW_JSON"] = json.dumps({
            "headRefOid": "3" * 40,
            "baseRefOid": "a" * 40,
            "reviews": [
                {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
            "statusCheckRollup": [],
        })
        initial_landings = len(app.landing_queue)
        app._dispatch_slay_landing(stop, identity=head_change_identity)
        await pilot.pause()
        os.environ.pop("PR_VIEW_JSON", None)
        check(
            len(app.landing_queue) == initial_landings,
            "slay must refuse landing when head changed between review and mutation",
        )
        head_changed_record = app.run_store.get(head_change_identity)
        check(
            head_changed_record is not None and head_changed_record.state == tui.RunState.HEAD_CHANGED,
            f"mutated head must reach HEAD_CHANGED terminal state, got {head_changed_record}",
        )
        check(
            "head changed" in (stop.failure or "").lower(),
            f"stop failure must report head changed, got {stop.failure!r}",
        )

        # ── #409: Four untrustworthy review outcomes reach distinct terminal states ──
        untrustworthy_cases = [
            ("review_missing", tui.RunState.REVIEW_MISSING, tui.TerminalOutcome.REVIEW_MISSING),
            ("review_failed", tui.RunState.REVIEW_FAILED, tui.TerminalOutcome.REVIEW_FAILED),
            ("review_incomplete", tui.RunState.REVIEW_INCOMPLETE, tui.TerminalOutcome.REVIEW_INCOMPLETE),
            ("review_unparsable", tui.RunState.REVIEW_UNPARSABLE, tui.TerminalOutcome.REVIEW_UNPARSABLE),
        ]
        for idx, (status_val, expected_state, expected_outcome) in enumerate(untrustworthy_cases, start=4):
            stop.head_sha = f"{idx:040x}"
            stop.live["baseRefOid"] = "a" * 40
            stop.live["headRefOid"] = stop.head_sha
            stop.review_status = "unreviewed"
            stop.review_result = None
            stop.live["reviews"] = [
                {"author": {"login": "human-reviewer"}, "state": "APPROVED", "authorAssociation": "MEMBER"}
            ]
            case_identity = app.run_identity(stop)
            app.review_pending_keys.add(stop.key)
            await slay_and_confirm()
            initial_landings = len(app.landing_queue)
            app.apply_review_event(
                tui.ReviewEvent(
                    key=stop.key,
                    state=status_val,
                    note=f"test {status_val}",
                    timestamp=int(time.time()),
                )
            )
            await pilot.pause()
            check(
                len(app.landing_queue) == initial_landings,
                f"{status_val} must create no landing task",
            )
            case_record = app.run_store.get(case_identity)
            check(
                case_record is not None and case_record.state == expected_state,
                f"{status_val} must reach {expected_state.value}, got {case_record}",
            )
            check(
                case_record is not None and case_record.terminal_outcome == expected_outcome,
                f"{status_val} must have terminal outcome {expected_outcome.value}, got {case_record}",
            )
            check(
                case_record is not None and not case_record.may_mutate(),
                f"{status_val} may_mutate() must be False",
            )
            check(
                "failed" in stop.review_status or expected_state.value in stop.review_status,
                f"stop review_status must show {expected_state.value}, got {stop.review_status}",
            )

        # ── Re-slaying an already-terminal record at same head gives clear message ──
        stop.head_sha = "1" * 40
        stop.live["baseRefOid"] = "a" * 40
        stop.live["headRefOid"] = stop.head_sha
        stop.live["reviews"] = []
        terminal_record = app.run_store.get(no_human_identity)
        check(terminal_record is not None and terminal_record.is_terminal, "terminal_record must be terminal")
        initial_landings = len(app.landing_queue)
        await slay_and_confirm()
        check(
            len(app.landing_queue) == initial_landings,
            "re-slaying terminal record at same head must not dispatch landing",
        )
        check(
            app.run_store.get(no_human_identity).state == terminal_record.state,
            "terminal state must remain unchanged after re-slay attempt",
        )

    # ── Dependency breaker visibility in status bar and mutation idempotency ──
    from tui.gh_client import Dependency, BreakerState
    breaker_app = tui.ReviewDashboard(tui.QueueFilters(action=""))
    async with breaker_app.run_test() as pilot:
        await wait_for_live_rows(breaker_app, pilot, "ready", 2)
        # Healthy state: breaker closed, status bar has no breaker clutter
        status_healthy = str(breaker_app.query_one("#status-bar", tui.Static).render())
        check(
            "breaker" not in status_healthy and "blocked" not in status_healthy,
            f"healthy breaker state must not clutter status bar, got {status_healthy!r}",
        )
        check(
            "review slots:" in status_healthy,
            f"status bar must show review slots, got {status_healthy!r}",
        )
        # Open GitHub breaker: status bar surfaces 'blocked' and 'retry_at'
        fake_retry_at = round(time.time() + 120.0, 1)
        breaker_app.gh_client._breakers.open(Dependency.GITHUB, 120.0, "API rate limit")
        breaker_app.gh_client._breakers._states[Dependency.GITHUB] = BreakerState(
            True, fake_retry_at, "API rate limit"
        )
        breaker_app.refresh_rows()
        await pilot.pause()
        status_open = str(breaker_app.query_one("#status-bar", tui.Static).render())
        check(
            "github" in status_open and "blocked" in status_open and "retry_at" in status_open,
            f"open GitHub breaker must be visible in status bar with blocked and retry_at, got {status_open!r}",
        )
        check(
            str(fake_retry_at) in status_open,
            f"open GitHub breaker must show retry_at in status bar, got {status_open!r}",
        )
        # Close breaker: returns to healthy and uncluttered
        breaker_app.gh_client._breakers.close(Dependency.GITHUB)
        breaker_app.refresh_rows()
        await pilot.pause()
        status_closed = str(breaker_app.query_one("#status-bar", tui.Static).render())
        check(
            "blocked" not in status_closed and "retry_at" not in status_closed,
            f"closed breaker must restore clean status bar, got {status_closed!r}",
        )

    # Source-level assertion: bluefin_review_tui.py contains no bare subprocess.run(["gh", ...])
    tui_source = Path(tui.__file__).read_text()
    check(
        not re.search(r'subprocess\.run\(\s*\[\s*["\']gh["\']', tui_source),
        "bluefin_review_tui.py must contain no bare subprocess.run(['gh', ...]) bypassing gh_client",
    )
    check(
        "return subprocess.run" not in tui_source.split("def gh(")[1].split("def _run_mutation")[0],
        "gh() must delegate to gh_client rather than calling subprocess.run directly",
    )
    check(
        "return subprocess.run" not in tui_source.split("def _run_mutation(")[1].split("def fetch_live_review")[0],
        "_run_mutation() must delegate to gh_client rather than calling subprocess.run directly",
    )

    # Mutation idempotency: mutation is not retried when idempotency is not proven
    test_calls = []
    def recording_runner(cmd, timeout):
        test_calls.append(cmd)
        return subprocess.CompletedProcess(["gh"], 1, "", "gh: secondary rate limit (HTTP 403)")
    test_gh_client = tui.GhClient(
        run=recording_runner,
        clock=lambda: 1000.0,
        sleep=lambda s: None,
    )
    res_mut = test_gh_client.mutation("pr", "merge", "31", attempts=4, idempotent=False)
    check(
        res_mut.returncode == 1 and len(test_calls) == 1,
        f"mutation must not retry when idempotency is not proven, made {len(test_calls)} calls",
    )
    gh_log.write_text("")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print(f"dashboard pilot: {checks - len(failures)}/{checks} checks passed")
    return 1 if failures else 0


class DashboardPilotTest(unittest.TestCase):
    def test_dashboard_pilot(self) -> None:
        self.assertEqual(asyncio.run(main()), 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
