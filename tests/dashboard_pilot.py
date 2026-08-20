"""Pilot tests: drive the real dashboard and assert what it does.

These run the Textual app through ``run_test()``, so a binding that points at
nothing, a screen that never reaches a terminal state, and a review whose
outcome is misreported are all failures here. The previous contract was a set
of greps over this file's source text, which passed while the dashboard had no
way to review anything at all.

No network and no GitHub: the queue is served from a temp file and the review
engine is replaced by a stub script whose exit status the test chooses.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
import tempfile
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
sys.path.insert(0, str(TUI_DIR))

SNAPSHOT = {
    "generated_at": "2026-08-08T00:00:00Z",
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
            "recommended_action": "merge",
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

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(description)


def write_stub(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dashboard-pilot."))

    queue_file = workdir / "queue.json"
    queue_file.write_text(json.dumps(SNAPSHOT))

    # gh is read-only here: the pilot never lets a mutation reach a real
    # network, and any attempt to run one is recorded for the assertions.
    gh_log = workdir / "gh.log"
    curl_log = workdir / "curl.log"
    diff_events = workdir / "diff-events.log"
    old_request_started = workdir / f"old-request-start-{workdir.name}"
    perm_file = workdir / "permissions.push"
    perm_file.write_text("true\n")
    gh_stub = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then\n'
        '  if [ -n "${GH_USER_FAIL-}" ]; then echo "authentication required" >&2; exit 1; fi\n'
        '  echo castrojo; exit 0;\n'
        'fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
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
    os.environ["BLUEFIN_REVIEW_QUEUE_URL"] = queue_file.as_uri()
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

    import bluefin_review_tui as tui

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
    async with tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri())).run_test() as pilot:
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
        "--repo owner/repo must remain a static snapshot filter",
    )
    check(
        tui.QueueFilters(live_repository="acme/widgets").live,
        "the distinct live repository filter must select the live source",
    )
    check(
        tui.PULL_FETCH_LIMIT == os.environ.get("BLUEFIN_REVIEW_PULL_LIMIT", "200"),
        "snapshot pull fetch limit must remain configurable",
    )

    live_file = workdir / "live.json"
    live_file.write_text(json.dumps([
        {"number": 42, "title": "review me", "author": {"login": "other"},
         "state": "OPEN", "isDraft": False, "labels": [],
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": []},
        {"number": 43, "title": "my own live work", "author": {"login": "castrojo"},
         "state": "OPEN", "isDraft": False, "labels": [],
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
         "statusCheckRollup": []},
    ]))
    os.environ["LIVE_QUEUE_FILE"] = str(live_file)
    live_app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))

    async def wait_for_live_rows(app, pilot, state: str, count: int) -> None:
        for _ in range(100):
            if app.source_state == state and len(app.stops) == count:
                return
            await pilot.pause(0.05)

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
        for _ in range(100):
            if app.source_state == state:
                return
            await pilot.pause(0.05)

    async def assert_live_state(error: str, state: str, detail: str) -> None:
        os.environ["LIVE_GH_ERROR"] = error
        app = tui.ReviewDashboard(tui.QueueFilters(live_repository="acme/widgets"))
        async with app.run_test() as pilot:
            await wait_for_state(app, pilot, state)
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
    registry = tui.command_registry()
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
        [command.key for command in registry if command.action == "quit"] == ["ctrl+q"],
        "Ctrl-q must be the sole global quit binding",
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
        tui.LandingScreen: "dismiss(None)",
        tui.DiffScreen: "dismiss",
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
        tui.QueueFilters(action="review", url=queue_file.as_uri())
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
        tui.QueueFilters(action="", repository="common", url=queue_file.as_uri())
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
            "l" not in binding_keys and "p" not in binding_keys,
            f"terminal-dispatched pane navigation must not collide with a binding, got {sorted(binding_keys)}",
        )
        review = [b for b in tui.ReviewDashboard.BINDINGS if b.action == "review"]
        check(len(review) == 1, f"exactly one binding must run a review, got {len(review)}")
        check(
            bool(review) and review[0].key == "r",
            f"review must be on 'r', got {[b.key for b in review]}",
        )
        check(
            "[b]l[/b]" not in tui.KEYS_ACTING and "[b]p[/b]" not in tui.KEYS_ACTING,
            f"the acting key line must not advertise label or priority, got {tui.KEYS_ACTING!r}",
        )
        root_screen = app.screen
        await pilot.press("q")
        await pilot.pause()
        check(app.screen is root_screen, "q must be safe on the root screen")
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
    ):
        review_stub(exit_code, output)
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
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
            app.stops[0].overlap = {"duplicates": [44], "overlaps": [45, 46]}
            root_screen = app.screen
            original_adapter = tui.adapt_current_engine
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
            await pilot.press("q")
            await pilot.pause()
            tui.adapt_current_engine = original_adapter
            check(app.screen is root_screen, "q must close ReviewScreen")
            return str(status.render()), set(status.classes), str(card.render())

    # ── a selected batch dispatches one landing agent behind one gate ────
    # The selection is the review, so the batch gate is proportionate: the
    # whole plan and the exact command on one screen, Enter to dispatch —
    # not a typed count. One agent owns all selected pull requests, reports
    # per-PR state to its status file, and the queue screen polls that file.
    landing_log = workdir / "landing-argv.log"
    landing_stub = write_stub(
        workdir / "stub-landing",
        f'printf "%s\\n" "$*" >>"{landing_log}"\n'
        'prompt=""\n'
        'for arg in "$@"; do case "$arg" in *.prompt.md) prompt="$arg" ;; esac; done\n'
        'status="${prompt%.prompt.md}.jsonl"\n'
        'grep -oE "[a-z]+/[a-z-]+#[0-9]+" "$prompt" | sort -u | while read -r pr; do\n'
        '  printf "{\\"pr\\": \\"%s\\", \\"state\\": \\"merged\\", \\"note\\": \\"green\\"}\\n" "$pr" >>"$status"\n'
        'done\n'
        'printf "{\\"state\\": \\"done\\", \\"note\\": \\"all landed\\"}\\n" >>"$status"\n'
        'echo "agent log line"\n',
    )
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{landing_stub} @PROMPT"
    os.environ["BLUEFIN_REVIEW_INSTANCE"] = "review-queue-pilot"
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
            for _ in range(200):
                if isinstance(app.screen, tui.LandingScreen):
                    break
                await pilot.pause(0.05)
            check(
                isinstance(app.screen, tui.LandingScreen),
                f"dispatch must open the live batch queue, got {type(app.screen).__name__}",
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
            invocations = landing_log.read_text().splitlines()
            check(
                len(invocations) == 1,
                f"one agent must run for the whole batch, got {invocations}",
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
            screen = app.screen
            if isinstance(screen, tui.LandingScreen):
                screen.poll()
                rows = str(screen.query_one("#landing-rows", tui.Static).render())
                check(
                    rows.count("merged") == 2,
                    f"the queue must show each landed PR, got {rows!r}",
                )
                check(
                    "all landed" in rows,
                    f"the queue must show the batch summary, got {rows!r}",
                )
            status = str(app.query_one("#status-bar", tui.Static).render())
            check(
                "agents:" in status,
                f"the status bar must report the batch queue, got {status!r}",
            )
            await pilot.press("escape")
            await pilot.pause()
            check(
                not isinstance(app.screen, tui.LandingScreen),
                "escape must return to the review queue",
            )
            await pilot.press("w")
            await pilot.pause()
            check(
                isinstance(app.screen, tui.LandingScreen),
                "[w] must reopen the live batch queue",
            )
            await pilot.press("q")
            await pilot.pause()
            check(not isinstance(app.screen, tui.LandingScreen), "q must return from LandingScreen")
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
                f"batch {colour_task.task_id} — running",
                # A running batch names its heartbeat: the age of the last
                # report, so a stale wait is visible next to a healthy one
                # (#291).
                "last report",
                "✓ merged",
                "✗ failed",
                "◆ awaiting-stable",
                "◌ waiting",
                "✔ done",
                "publish workflow red",
                "two landed, one failed",
            ):
                check(
                    expected in rows,
                    f"the batch queue must show {expected!r}, got {rows!r}",
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

            header_fills = fills(f"batch {colour_task.task_id} — running")
            check(
                any(
                    style.bgcolor.get_truecolor() == theme_rgb("primary-muted")
                    for style in header_fills
                ),
                "the running batch header must be a filled bar, got "
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
    for artifact in (
        Path(colour_task.prompt_path),
        Path(colour_task.status_path),
    ):
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    restored = tui.landing.persisted_events()
    check(
        restored["projectbluefin/bluefinctl#31"]["state"] == "merged",
        "a manual success must supersede the persisted failure, "
        f"got {restored['projectbluefin/bluefinctl#31']!r}",
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
        await pilot.press("b")
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices: list[str] = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append(str(message))
            real_notify(message, *args, **kwargs)

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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices: list[tuple[str, str]] = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append((str(message), kwargs.get("severity", "information")))
            real_notify(message, *args, **kwargs)

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
            if not app.stops[0].selected and app.stops[1].selected:
                break
            await pilot.pause(0.05)
        check(
            not app.stops[0].selected and not app.stops[0].failure,
            "a merged pull request leaves the batch",
        )
        check(
            app.stops[1].selected and "failed" in app.stops[1].failure,
            "a failed pull request stays selected with its reason — the "
            "notification does not outlive the row",
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append((str(message), kwargs.get("severity", "information")))
            real_notify(message, *args, **kwargs)

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
            all(s.selected for s in app.stops),
            "an unfinished batch keeps every pull request selected",
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append((str(message), kwargs.get("severity", "information")))
            real_notify(message, *args, **kwargs)

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
        for _ in range(200):
            if app.stops[1].failure:
                break
            await pilot.pause(0.05)
        check(
            app.stops[1].failure.startswith("no outcome reported"),
            "the out-of-report pull request must be marked as a reporting "
            f"gap, got {app.stops[1].failure!r}",
        )
        check(
            all("died mid-batch" not in s.failure for s in app.stops),
            "no row may claim a dead agent when the agent reported done, "
            f"got {[s.failure for s in app.stops]}",
        )
        check(
            app.stops[1].selected,
            "the out-of-report pull request stays in the batch",
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append((str(message), kwargs.get("severity", "information")))
            real_notify(message, *args, **kwargs)

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
    os.environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = f"{blocked_stub} @PROMPT"
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if len(app.stops) == 2:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        notices = []
        real_notify = app.notify

        def record(message, *args, **kwargs):
            notices.append((str(message), kwargs.get("severity", "information")))
            real_notify(message, *args, **kwargs)

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

    # ── merging without lgtm is a maintainer power ───────────────────────
    # lgtm is an opt-in to Hive's automation, not a toll on merging: a
    # maintainer can land a pull request directly. Someone without the push
    # permission cannot, and must be told so rather than shown a gate.
    for allowed in (False, True):
        perm_file.write_text("true\n" if allowed else "false\n")
        app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
            data = self.status if path.endswith("status") else self.contributors
            return tui.hive_api.Result(True, "ok", "online", data)

    real_hive_get = tui.hive_get
    real_base = tui.hive_api_base
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
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
            stop = app.stops[0]
            worker = app.hive_worker_for(stop)
            check(
                worker is not None and worker["login"] == "someone-else",
                f"a stop Hive is working on must be identified, got {worker}",
            )
            check(
                app.hive_worker_for(app.stops[1]) is None,
                "a stop nobody is working on must not claim a worker",
            )
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
            app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.hive_state:
                    break
                await pilot.pause(0.05)
            check(
                app.hive_state == "not configured",
                f"no hub must read as not configured, got {app.hive_state!r}",
            )
    finally:
        tui.hive_get = real_hive_get
        tui.hive_api_base = real_base
    gh_log.write_text("")

    # ── the diff is coloured, scrollable, and whole ──────────────────────
    # It used to be plain text pasted into the evidence pane and cut at 20 000
    # characters with no sign it had been cut.
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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

    bracket_queue = workdir / "brackets.json"
    bracket_queue.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-08T00:00:00Z",
                "items": [
                    {
                        "repository": "projectbluefin/bluefinctl",
                        "number": 31,
                        "recommended_action": "review",
                        "title": "fix: [skip ci] guard the release",
                        "author": "someone-else",
                    }
                ],
            }
        )
    )
    app = tui.ReviewDashboard(tui.QueueFilters(url=bracket_queue.as_uri()))
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

    # ── who has reviewed, and whether their word carries write access ────
    check(
        tui.reviewer_standing("MEMBER") == "maintainer"
        and tui.reviewer_standing("OWNER") == "maintainer"
        and tui.reviewer_standing("COLLABORATOR") == "maintainer"
        and tui.reviewer_standing("CONTRIBUTOR") == "community"
        and tui.reviewer_standing("NONE") == "community",
        "author association must separate maintainers from the community",
    )
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
        stop.live = {
            "isDraft": False,
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
        app.render_evidence(stop)
        await pilot.pause()
        check(
            "reviews  none yet" in str(app.query_one("#details", tui.Static).render()),
            "an unreviewed pull request must say so plainly",
        )

    # ── leaving a review: a verdict without a merge ──────────────────────
    for verdict_key, flag in (("1", "--approve"), ("2", "--request-changes"), ("3", "--comment")):
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
            app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
            async with app.run_test() as pilot:
                await pilot.pause()
                for _ in range(200):
                    if app.stops:
                        break
                    await pilot.pause(0.05)
                stop = app.stops[0]
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
        app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
        app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
            editor.text = "manual Goose body"
            app.screen.action_generate()
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
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
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    gh_log.write_text("")

    # ── Hive owns the App approval and queue label atomically (#247) ──────
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        app.self_login = "castrojo"
        stop = app.stops[0]
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
        tui.stop_style("review", "dirty", "success", "approved") == "red",
        "a conflicted pull request must be red whatever else is true of it",
    )
    check(
        tui.stop_style("review", "clean", "failure", "unknown") == "yellow",
        "failing checks must be yellow",
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
        text_rows["failure"].index("✗ CI FAILED") < text_rows["failure"].index("[review]"),
        "failure text must outrank the healthy queue presentation",
    )

    for key in ("r", "v", "o", "h", "/", "f", "b", "H", "R", "q"):
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

    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
    async with app.run_test() as pilot:
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
        check(
            app.focused is app.query_one("#steer", tui.Input),
            "lowercase l must move focus through Textual's screen API",
        )
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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

    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
    mech_queue = workdir / "mechanical.json"
    mech_queue.write_text(json.dumps({
        "generated_at": "2026-08-08T00:00:00Z",
        "items": [
            {"repository": "o/r", "number": 101, "recommended_action": "review",
             "title": "chore(deps): update dependency alpha", "author": bot},
            {"repository": "o/r", "number": 142, "recommended_action": "review",
             "title": "chore(deps): update dependency beta", "author": bot},
            {"repository": "o/r", "number": 117, "recommended_action": "review",
             "title": "chore(deps): update dependency gamma", "author": bot},
            {"repository": "o/r", "number": 9, "recommended_action": "review",
             "title": "chore(deps): update dependency delta by hand",
             "author": "someone-else"},
        ],
    }))
    ok_json = workdir / "mech-ok.json"
    ok_json.write_text(json.dumps(live_shape()))
    bad_json = workdir / "mech-bad.json"
    bad_json.write_text(json.dumps(live_shape(mergeable="CONFLICTING")))
    write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then\n'
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=mech_queue.as_uri()))
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

    # ── duplicates come with enough summary to choose between them ───────
    # "dupe-of #26, #25, #24" says a decision is required and nothing about
    # how to make it; which one to keep is the whole question.
    cluster_gh = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if app.stops:
                break
            await pilot.pause(0.05)
        stop = app.stops[0]
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
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
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

    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
        stop = app.stops[0]
        stop.live = {"isDraft": False}
        app.render_evidence(stop)
        for _ in range(200):
            if "merge queue" in str(app.query_one("#context", tui.Static).render()):
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(action="", url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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

    # ── the steer box: typed text reaches the review as instructions ─────
    review_stub(0, "0 findings")
    steer_log.write_text("")
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
    app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
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
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.stops:
                    break
                await pilot.pause(0.05)
            app.stops[0].live = {
                "isDraft": False,
                "baseRefOid": "fedcba9876543210fedcba9876543210fedcba98",
                "headRefOid": "0123456789abcdef0123456789abcdef01234567",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "SUCCESS"}
                ],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            }
            app.stops[0].overlap = {"duplicates": [1], "overlaps": [2]}
            app.start_review(app.stops[0], "focus on exact-head evidence")
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
            card = str(app.screen.query_one("#review-card", tui.Static).render())
            check(
                "CI success" in card and "MERGEABLE/CLEAN" in card
                and "1 duplicate / 1 shared-file hazard" in card,
                f"Codex card must merge trusted live and overlap evidence, got {card!r}",
            )

        tui.subprocess.Popen = real_popen
        os.environ.pop("GH_TOKEN", None)
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
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
            "complete", "complete", "incomplete", "stale", "incomplete", "failed",
            "complete", "incomplete", "complete", "stopped", "stopped",
            "complete", "error",
        ],
        f"every review must be traced with its outcome, got {outcomes}",
    )

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print(f"dashboard pilot: {checks - len(failures)}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
