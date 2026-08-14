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
import stat
import subprocess
import sys
import threading
import time
import tempfile
from types import SimpleNamespace
from pathlib import Path

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
    perm_file = workdir / "permissions.push"
    perm_file.write_text("true\n")
    gh_stub = write_stub(
        workdir / "gh",
        f'printf "%s\\n" "$*" >>"{gh_log}"\n'
        'if [ "$1 $2" = "api user" ]; then echo castrojo; exit 0; fi\n'
        f'case "$1 $2" in "api repos/"*) cat "{perm_file}"; exit 0 ;; esac\n'
        'if [ "$1 $2" = "pr view" ]; then echo "{}"; exit 0; fi\n'
        'if [ "$1 $2" = "pr diff" ]; then\n'
        '  printf "%s\\n" "diff --git a/x b/x" "--- a/x" "+++ b/x" "@@ -1 +1 @@" "-old" "+new"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "pr list" ]; then echo "[]"; exit 0; fi\n'
        "exit 0\n",
    )
    os.environ["PATH"] = f"{workdir}:{os.environ['PATH']}"
    os.environ["XDG_STATE_HOME"] = str(workdir / "state")
    os.environ["BLUEFIN_REVIEW_QUEUE_URL"] = queue_file.as_uri()

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
            "l" in binding_keys and "p" not in binding_keys,
            f"pane navigation must be present and priority must be absent, got {sorted(binding_keys)}",
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

        app.action_comment()
        await pilot.pause()
        check(type(app.screen).__name__ == "CommentBody", "q acceptance must activate CommentBody")
        await pilot.press("q")
        check(app.screen.query_one(tui.Input).value == "q", "q must type in the comment input")
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

    async def run_review(exit_code: int, output: str):
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
                "headRefOid": "0123456789abcdef0123456789abcdef01234567",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"name": "validate", "conclusion": "SUCCESS"},
                    {"name": "docs", "conclusion": "FAILURE"},
                ],
            }
            app.stops[0].overlap = {"duplicates": [44], "overlaps": [45, 46]}
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
            raw = screen.query_one("#review-log", tui.RichLog)
            check("hidden" in raw.classes, "completed raw evidence must start collapsed")
            await pilot.press("e")
            await pilot.pause()
            check("hidden" not in raw.classes, "[e] must reveal the raw review evidence")
            await pilot.press("e")
            await pilot.pause()
            check("hidden" in raw.classes, "[e] must return to the decision card")
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
        gate = app.screen
        check(
            isinstance(gate, tui.BatchPlanScreen),
            f"a selected batch must open the plan gate, got {type(gate).__name__}",
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
            await pilot.press("A")
            await pilot.pause()
            check(
                isinstance(app.screen, tui.LandingScreen),
                "[A] must reopen the live batch queue",
            )
            await pilot.press("escape")
            await pilot.pause()
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
        app.action_merge()
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
    gh_log.write_text("")

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
            return self.status if path.endswith("status") else self.contributors

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
        tui.hive_get = lambda path: {}
        app = tui.ReviewDashboard(tui.QueueFilters(url=queue_file.as_uri()))
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(200):
                if app.hive_state:
                    break
                await pilot.pause(0.05)
            check(
                app.hive_state == "unreachable",
                f"an unreachable hub must say so, got {app.hive_state!r}",
            )
            check(app.stops, "an unreachable hub must not empty the queue")

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
            # Truncation, when it happens, must say so.
            screen.render_diff("x" * (tui.DiffScreen.MAX_CHARS + 10))
            await pilot.pause()
            check(
                "truncated at" in getattr(screen.rendered, "code", ""),
                "a cut diff must say it was cut, and how big it really is",
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
            await pilot.press("enter")
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
        "browser" in [c for c, _ in tui.MergeRecovery.offers(
            tui.Stop("o/r", 1, "merge", "t", live={"mergeStateStatus": "DIRTY"}), ""
        )],
        "a conflicted merge must be handed to a human",
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
        '  echo "Pull request is not mergeable: the base branch is out of date" >&2\n'
        "  exit 1\n"
        "fi\n"
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

    # ── queueing must not half-apply when the label is missing (#141) ────
    # Reported from the field: the approval landed, `gh pr edit --add-label
    # lgtm` failed with "'lgtm' not found", and the pull request was left
    # formally approved for an auto-merge that could never be picked up.
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
        app.queue_label_exists[stop.repository] = False
        gh_log.write_text("")
        app.action_merge()
        await pilot.pause()
        check(
            isinstance(app.screen, tui.ConfirmMutation),
            "queueing must still gate when the label is missing",
        )
        if isinstance(app.screen, tui.ConfirmMutation):
            verbs = [tuple(c[:3]) for c in app.screen.commands]
            check(
                verbs
                == [
                    ("gh", "label", "create"),
                    ("gh", "pr", "review"),
                    ("gh", "pr", "edit"),
                ],
                f"a missing label must be created before the approval, got {verbs}",
            )
            check(
                tui.QUEUE_LABEL_COLOUR == "238636",
                "the created label must match the one the factory already uses",
            )
            await pilot.press(*app.screen.expected)
            await pilot.press("enter")
            for _ in range(200):
                if "pr edit" in gh_log.read_text():
                    break
                await pilot.pause(0.05)
            ran = gh_log.read_text()
            check(
                ran.index("label create") < ran.index("pr review"),
                "the label must exist before the approval is submitted",
            )
        # With the label present, nothing extra is run.
        app.queue_label_exists[stop.repository] = True
        gh_log.write_text("")
        app.action_merge()
        await pilot.pause()
        if isinstance(app.screen, tui.ConfirmMutation):
            check(
                [tuple(c[:3]) for c in app.screen.commands]
                == [("gh", "pr", "review"), ("gh", "pr", "edit")],
                "an existing label must not be created again",
            )
            await pilot.press("escape")
            await pilot.pause()
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
            and len(command) > 2
            and command[1] == "pr"
            and command[2] in {"review", "edit"}
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
                "a slow gh mutation must run off the UI thread, "
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
    check("No clean decision" in card, f"unparsable output must direct raw inspection, got {card!r}")

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
                and [command[:3] for command in app.screen.commands]
                == [["gh", "pr", "review"], ["gh", "pr", "edit"]],
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
            "complete", "complete", "incomplete", "incomplete", "failed",
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
