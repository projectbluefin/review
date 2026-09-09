"""Focused contract for evidence-first CI failure drill-down."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TUI_DIR = Path(__file__).resolve().parent.parent / "image" / "tui"
sys.path.insert(0, str(TUI_DIR.parent))

from tui import bluefin_review_tui as tui  # noqa: E402


HEAD = "a" * 40


class CIFailureEvidenceContractTests(unittest.TestCase):
    def test_failure_evidence_binds_exact_head_and_all_metadata(self) -> None:
        live = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "databaseId": 7001,
                    "name": "linux / unit",
                    "workflowName": "validate",
                    "conclusion": "FAILURE",
                    "startedAt": "2026-09-08T10:00:00Z",
                    "completedAt": "2026-09-08T10:05:00Z",
                    "detailsUrl": "https://github.com/acme/widgets/actions/runs/9001",
                    "headSha": HEAD,
                        "checkSuite": {
                        "workflowRun": {
                            "databaseId": 9001,
                            "runAttempt": 2,
                            "workflowName": "validate",
                            "workflowId": 8001,
                        }
                    },
                    "jobId": 7101,
                    "steps": [
                        {"name": "install", "conclusion": "SUCCESS"},
                        {"name": "unit tests", "conclusion": "FAILURE"},
                    ],
                    "annotations": {
                        "nodes": [
                            {
                                "path": "tests/test_widgets.py",
                                "startLine": 17,
                                "endLine": 17,
                                "message": "assertion failed",
                                "annotationLevel": "FAILURE",
                            }
                        ]
                    },
                }
            ],
        }

        failure = tui.ci_failure_evidence(live)[0]

        self.assertEqual(failure["repository"], "acme/widgets")
        self.assertEqual(failure["pull_request"], 42)
        self.assertEqual(failure["head_sha"], HEAD)
        self.assertEqual(failure["check_id"], 7001)
        self.assertEqual(failure["workflow"], "validate")
        self.assertEqual(failure["workflow_id"], 8001)
        self.assertEqual(failure["job"], "linux / unit")
        self.assertEqual(failure["job_id"], 7101)
        self.assertEqual(failure["run_id"], 9001)
        self.assertEqual(failure["attempt"], 2)
        self.assertEqual(failure["step"], "unit tests")
        self.assertEqual(failure["conclusion"], "FAILURE")
        self.assertEqual(failure["started_at"], "2026-09-08T10:00:00Z")
        self.assertEqual(failure["completed_at"], "2026-09-08T10:05:00Z")
        self.assertEqual(failure["url"], "https://github.com/acme/widgets/actions/runs/9001")
        self.assertEqual(failure["annotations"][0]["path"], "tests/test_widgets.py")
        self.assertEqual(failure["annotations"][0]["start_line"], "17")

    def test_forced_live_refresh_rejects_empty_or_headless_success(self) -> None:
        class EmptyClient:
            def __init__(self, stdout: str):
                self.stdout = stdout

            def read(self, *args: str):
                return subprocess.CompletedProcess(
                    ["gh", *args], 0, self.stdout, ""
                )

        for stdout in ("{}\n", '{"repository":"acme/widgets"}\n'):
            stop = tui.Stop(
                "acme/widgets",
                42,
                "fix-ci",
                "failing check",
                head_sha=HEAD,
                live={
                    "headRefOid": HEAD,
                    "statusCheckRollup": [
                        {"name": "linux", "conclusion": "FAILURE"}
                    ],
                },
            )
            app = tui.ReviewDashboard(gh_client=EmptyClient(stdout))
            app.stops = [stop]
            with self.assertRaises(RuntimeError):
                app.fetch_live_pr("acme/widgets", 42, force=True)

    def test_missing_fields_render_as_unknown_and_stale_heads_are_ignored(self) -> None:
        live = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [
                {"name": "old", "conclusion": "FAILURE", "headSha": "b" * 40},
                {"conclusion": "ERROR"},
            ],
        }

        failures = tui.ci_failure_evidence(live)
        text = "\n".join(tui.format_ci_failure_evidence(failures[0]))

        self.assertEqual(len(failures), 1)
        self.assertIn("repository  acme/widgets", text)
        self.assertIn("pull request 42", text)
        self.assertRegex(text, r"check id\s+unknown")
        self.assertRegex(text, r"workflow\s+unknown")
        self.assertRegex(text, r"job\s+unknown")
        self.assertRegex(text, r"run\s+unknown")
        self.assertRegex(text, r"attempt\s+unknown")
        self.assertRegex(text, r"step\s+unknown")
        self.assertIn("annotations unknown", text)
        self.assertIn("evidence  unknown", text)

    def test_logs_are_bounded_redacted_and_terminal_control_free(self) -> None:
        raw = (
            "token=top-secret ghp_abcdefghijklmnopqrstuvwxyz1234567890\x1b[31m\n"
            + "x" * (tui.CI_LOG_MAX_BYTES + 100)
        )

        state, lines = tui.sanitize_ci_log(raw)
        rendered = "\n".join(lines)

        self.assertEqual(state, "available")
        self.assertLessEqual(len(rendered.encode()), tui.CI_LOG_MAX_BYTES)
        self.assertLessEqual(len(lines), tui.CI_LOG_MAX_LINES)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("ghp_", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_failure_action_fetches_current_head_evidence_on_demand(self) -> None:
        class Dashboard(tui.ReviewDashboard):
            def load_queue(self, *args, **kwargs):
                return None

            def load_hive(self, *args, **kwargs):
                return None

            def discover_harness(self, *args, **kwargs):
                return None

        stop = tui.Stop(
            "acme/widgets", 42, "fix-ci", "failing check", check_state="failure",
            head_sha=HEAD,
            live={"headRefOid": HEAD},
        )
        live = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [{
                "name": "linux",
                "conclusion": "FAILURE",
                "runId": 9001,
                "headSha": HEAD,
            }],
        }

        async def exercise():
            app = Dashboard()
            app.fetch_live_pr = lambda repository, number, force=False: live
            async with app.run_test(size=(80, 24)) as pilot:
                app.stops = [stop]
                app.populate(app.stops)
                app.open_ci_failure_logs(stop)
                await app.workers.wait_for_complete()
                for _ in range(100):
                    if isinstance(app.screen, tui.CIFailureScreen):
                        break
                    await pilot.pause()
                self.assertIsInstance(app.screen, tui.CIFailureScreen)
                self.assertEqual(stop.live.get("headRefOid"), HEAD)

        asyncio.run(exercise())

        self.assertEqual(tui.sanitize_ci_log(None)[0], "missing")
        self.assertEqual(tui.sanitize_ci_log("",)[0], "empty")
        self.assertEqual(tui.ci_log_failure_state("401 authentication failed"), "authentication failed")
        self.assertEqual(tui.ci_log_failure_state("403 forbidden"), "permission denied")
        self.assertEqual(tui.ci_log_failure_state("404 log not found"), "expired or unavailable")
        self.assertEqual(tui.ci_log_failure_state("connection reset"), "transport failed")

    def test_on_demand_failure_survives_a_failed_background_refresh(self) -> None:
        class Dashboard(tui.ReviewDashboard):
            def load_queue(self, *args, **kwargs):
                return None

            def load_hive(self, *args, **kwargs):
                return None

            def discover_harness(self, *args, **kwargs):
                return None

            def show_evidence(self, *args, **kwargs):
                return None

        stop = tui.Stop(
            "acme/widgets", 42, "fix-ci", "failing check", check_state="failure",
            head_sha=HEAD,
            live={"headRefOid": HEAD},
        )
        live = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [{
                "name": "linux",
                "conclusion": "FAILURE",
                "runId": 9001,
                "headSha": HEAD,
            }],
        }

        async def exercise():
            app = Dashboard()
            async with app.run_test(size=(80, 24)) as pilot:
                app.stops = [stop]
                app.populate(app.stops)
                app.evidence_generation[stop.key] = 1
                expected_head = stop.head_identity
                app.evidence_failed(stop, "background refresh failed", 1, False)
                self.assertEqual(stop.head_identity, "")
                app.show_ci_failure_evidence(
                    stop,
                    expected_head,
                    live,
                    tui.ci_failure_evidence(live),
                    "",
                )
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.CIFailureScreen)
                self.assertEqual(stop.live.get("headRefOid"), HEAD)

        asyncio.run(exercise())

    def test_background_refresh_cannot_erase_displayed_ci_head(self) -> None:
        class Dashboard(tui.ReviewDashboard):
            def load_queue(self, *args, **kwargs):
                return None

            def load_hive(self, *args, **kwargs):
                return None

            def discover_harness(self, *args, **kwargs):
                return None

            def show_evidence(self, *args, **kwargs):
                return None

        stop = tui.Stop(
            "acme/widgets", 42, "fix-ci", "failing check", check_state="failure",
            head_sha=HEAD,
            live={"headRefOid": HEAD},
        )
        live = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [{
                "name": "linux",
                "conclusion": "FAILURE",
                "runId": 9001,
                "headSha": HEAD,
            }],
        }

        async def exercise():
            app = Dashboard()
            async with app.run_test(size=(80, 24)) as pilot:
                app.stops = [stop]
                app.populate(app.stops)
                app.evidence_generation[stop.key] = 1
                app.show_ci_failure_evidence(
                    stop,
                    HEAD,
                    live,
                    tui.ci_failure_evidence(live),
                    "",
                )
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.CIFailureScreen)
                app.evidence_failed(stop, "background refresh failed", 1, False)
                self.assertEqual(stop.live.get("headRefOid"), HEAD)

        asyncio.run(exercise())

    def test_failure_action_revalidates_cached_failure_before_display(self) -> None:
        class Dashboard(tui.ReviewDashboard):
            def load_queue(self, *args, **kwargs):
                return None

            def load_hive(self, *args, **kwargs):
                return None

            def discover_harness(self, *args, **kwargs):
                return None

            def show_evidence(self, *args, **kwargs):
                return None

        cached = {
            "repository": "acme/widgets",
            "number": 42,
            "headRefOid": HEAD,
            "statusCheckRollup": [{
                "name": "linux",
                "conclusion": "FAILURE",
                "runId": 9001,
                "headSha": HEAD,
            }],
        }
        current = {
            **cached,
            "statusCheckRollup": [{
                "name": "linux",
                "conclusion": "FAILURE",
                "runId": 9002,
                "headSha": HEAD,
            }],
        }
        stop = tui.Stop(
            "acme/widgets", 42, "fix-ci", "failing check", check_state="failure",
            head_sha=HEAD,
            live=cached,
        )
        calls = []

        async def exercise():
            app = Dashboard()

            def fetch_live_pr(repository, number, force=False):
                calls.append(force)
                return current

            app.fetch_live_pr = fetch_live_pr
            async with app.run_test(size=(80, 24)) as pilot:
                app.stops = [stop]
                app.populate(app.stops)
                app.open_ci_failure_logs(stop)
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(calls, [True])
                self.assertIsInstance(app.screen, tui.CIFailureScreen)
                self.assertEqual(app.screen.evidence["run_id"], 9002)

        asyncio.run(exercise())

    def test_late_ci_results_cannot_cross_selection_head_or_cancel(self) -> None:
        self.assertTrue(tui.ci_result_is_current("acme/widgets", 42, HEAD, 7, False,
                                                  "acme/widgets", 42, HEAD, 7))
        self.assertFalse(tui.ci_result_is_current("acme/widgets", 42, HEAD, 6, False,
                                                   "acme/widgets", 42, HEAD, 7))
        self.assertFalse(tui.ci_result_is_current("acme/widgets", 42, HEAD, 7, False,
                                                   "acme/other", 42, HEAD, 7))
        self.assertFalse(tui.ci_result_is_current("acme/widgets", 42, HEAD, 7, False,
                                                   "acme/widgets", 42, "b" * 40, 7))
        self.assertFalse(tui.ci_result_is_current("acme/widgets", 42, HEAD, 7, True,
                                                   "acme/widgets", 42, HEAD, 7))


class CIFailurePilotContractTests(unittest.TestCase):
    def test_i_opens_current_failure_and_loads_untrusted_logs(self) -> None:
        stop = tui.Stop(
            "acme/widgets",
            42,
            "fix-ci",
            "fix CI",
            head_sha=HEAD,
            live={
                "repository": "acme/widgets",
                "number": 42,
                "headRefOid": HEAD,
                "statusCheckRollup": [{"name": "linux", "conclusion": "FAILURE", "runId": 9001}],
            },
        )

        class PilotDashboard(tui.ReviewDashboard):
            def load_issues(self, *args, **kwargs):
                return None

            def discover_harness(self, *args, **kwargs):
                return None

            def on_mount(self) -> None:
                self.stops = [stop]
                self.populate(self.stops)
                self.query_one("#queue", tui.ListView).focus()

        log = "line\x1b[2K\nBearer secret-value\n"
        def gh_stub(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
            if args[:2] == ("run", "view"):
                return subprocess.CompletedProcess(["gh", *args], 0, log, "")
            if args[:2] == ("pr", "view"):
                return subprocess.CompletedProcess(
                    ["gh", *args],
                    0,
                    '{"repository":"acme/widgets","number":42,"headRefOid":"'
                    + HEAD
                    + '","statusCheckRollup":[{"name":"linux","conclusion":"FAILURE","runId":9001}]}',
                    "",
                )
            if args[:2] == ("pr", "list"):
                return subprocess.CompletedProcess(["gh", *args], 0, "[]", "")
            return subprocess.CompletedProcess(["gh", *args], 0, "[]", "")

        async def journey() -> None:
            with patch.object(tui, "gh", side_effect=gh_stub), \
                    patch.object(tui.ReviewDashboard, "load_queue", lambda self: None), \
                    patch.object(tui.ReviewDashboard, "show_evidence", lambda *args: None):
                async with PilotDashboard(tui.QueueFilters()).run_test() as pilot:
                    await pilot.pause()
                    pilot.app._queue().index = 0
                    pilot.app.fetch_live_pr = lambda *args, **kwargs: dict(stop.live)
                    await pilot.press("i")
                    await pilot.app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertIsInstance(pilot.app.screen, tui.CIFailureScreen)
                    evidence_scroll = pilot.app.screen.query_one("#ci-failure-evidence-scroll")
                    self.assertTrue(evidence_scroll.can_focus)
                    await pilot.press("i")
                    await pilot.app.workers.wait_for_complete()
                    await pilot.pause()
                    state = str(pilot.app.screen.query_one("#ci-log-state", tui.Static).render())
                    log_text = "\n".join(
                        str(line)
                        for line in pilot.app.screen.query_one("#ci-log", tui.RichLog).lines
                    )
                    self.assertIn("UNTRUSTED", state)
                    self.assertIn("line", log_text)
                    self.assertNotIn("\x1b", log_text)
                    self.assertNotIn("secret-value", log_text)

        asyncio.run(journey())


if __name__ == "__main__":
    unittest.main()
