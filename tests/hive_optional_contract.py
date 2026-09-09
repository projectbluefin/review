# tests/hive_optional_contract.py
"""Hive stays optional: the core review-fix-land loop with no hub at all.

The dashboard's authority for actionable work is GitHub, live; Hive is
read-only enrichment plus the one explicit queue-automerge action. This
contract pins that a dashboard with no HIVE_HUB configured still slays,
dispatches issue fix agents, and folds landing outcomes — and that the one
genuinely Hive-backed action degrades to a warning instead of an error.
"""

import glob
import os
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

site_pkgs = glob.glob(
    str(
        Path(__file__).parents[1]
        / ".cache"
        / "tui-venv"
        / "lib"
        / "python*"
        / "site-packages"
    )
)
if site_pkgs:
    sys.path.insert(0, site_pkgs[0])

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

import tui.bluefin_review_tui as tui

REPO_ROOT = Path(__file__).resolve().parent.parent


class HiveOptionalContractTests(unittest.TestCase):
    def setUp(self):
        # No hub anywhere in the environment: the degraded mode under test.
        env = mock.patch.dict(os.environ, {"HIVE_HUB": ""})
        env.start()
        self.addCleanup(env.stop)
        scratch = REPO_ROOT / ".cache" / "hive-optional-contract"
        scratch.mkdir(parents=True, exist_ok=True)
        self._landing_dir = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self._landing_dir.cleanup)
        patcher = mock.patch.object(
            tui.landing, "landing_state_dir", return_value=self._landing_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_hub_means_no_api_base(self):
        self.assertEqual(tui.hive_api_base(), "")

    def test_slay_gate_opens_without_hive(self):
        """[$] on a slayable pull request reaches its typed gate hub-less."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        pr = tui.Stop(
            repository="projectbluefin/review",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "isDraft": False},
        )
        app.stops = [pr]
        pr.selected = True
        pushed: list[object] = []
        app.push_screen = lambda screen, callback=None: pushed.append(screen)
        app.notify = lambda *a, **kw: None

        app.action_slay_pr()

        self.assertEqual(len(pushed), 1)
        self.assertIsInstance(pushed[0], tui.SlayConfirmScreen)

    def test_issue_fix_agent_dispatches_without_hive(self):
        """A confirmed issue slay dispatches its fix agent hub-less."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        issue = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
            live={"labels": [], "comments_count": 0},
        )
        app.stops = [issue]
        issue.selected = True
        enqueued: list[object] = []
        app.enqueue_landing = enqueued.append
        app.notify = lambda *a, **kw: None
        app.push_screen = lambda screen, callback=None: callback(True)

        app.action_slay_pr()

        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0].stops[0].key, "projectbluefin/review#42")

    def test_queue_automerge_degrades_to_warning_without_hive(self):
        """The one Hive-backed action warns and stops; it never raises."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "isDraft": False},
        )
        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append(
            (message, severity)
        )

        app._queue_automerge(stop)

        self.assertTrue(
            any("Hive is unreachable" in message for message, _ in notices),
            f"queue without a hub must warn, got {notices}",
        )

    def test_repo_scoped_manual_filters_survive_without_hive(self):
        """Repo filtering and hand-picking are core-loop, not Hive features."""
        filters = tui.QueueFilters(repository="projectbluefin/review")
        self.assertTrue(
            filters.wants_repo({"repository": "projectbluefin/review"})
        )
        self.assertFalse(
            filters.wants_repo({"repository": "projectbluefin/common"})
        )


if __name__ == "__main__":
    unittest.main()
