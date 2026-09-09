"""Contract checks for the mixed-workboard foundation slice.

Validates that:
- QueueFilters defaults to kind="prs" and supports "mixed" and "issues".
- QueueFilters.wants_kind and wants filter correctly for PRs and issues.
- ReviewDashboard composes PR and issue items into a single stops list.
- I toggles cycle prs -> issues -> mixed -> prs.
- PR and issue source states are modeled independently (failing issue fetch does
  not corrupt PR source state or wipe out PR stops, and vice versa).
- Selection (b/B/Space) is shared and type-agnostic across PRs and issues.
- Sorting correctly ranks lifecycle state, lack of review, action, and repo/number.
- Status bar accurately reflects mixed/PR/issues lens.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIR = REPO_ROOT / "image"
if str(IMAGE_DIR) not in sys.path:
    sys.path.insert(0, str(IMAGE_DIR))

os.environ["BLUEFIN_REVIEW_COMMAND"] = "true"
import tui.bluefin_review_tui as tui


class MixedWorkboardContractTests(unittest.TestCase):
    def test_queue_filters_default_to_pull_requests(self) -> None:
        filters = tui.QueueFilters()
        self.assertEqual(filters.kind, "prs")

    def test_queue_filters_wants_kind(self) -> None:
        pr_item = {"repository": "acme/repo", "number": 1, "is_issue": False}
        issue_item = {"repository": "acme/repo", "number": 2, "is_issue": True}

        mixed_filter = tui.QueueFilters(kind="mixed")
        self.assertTrue(mixed_filter.wants_kind(pr_item))
        self.assertTrue(mixed_filter.wants_kind(issue_item))

        prs_filter = tui.QueueFilters(kind="prs")
        self.assertTrue(prs_filter.wants_kind(pr_item))
        self.assertFalse(prs_filter.wants_kind(issue_item))

        issues_filter = tui.QueueFilters(kind="issues")
        self.assertFalse(issues_filter.wants_kind(pr_item))
        self.assertTrue(issues_filter.wants_kind(issue_item))

    def test_queue_filters_wants_with_action_and_repo(self) -> None:
        pr1 = {"repository": "projectbluefin/bluefinctl", "number": 31, "recommended_action": "review", "is_issue": False}
        pr2 = {"repository": "projectbluefin/common", "number": 7, "recommended_action": "investigate", "is_issue": False}
        issue1 = {"repository": "projectbluefin/bluefinctl", "number": 42, "recommended_action": "triage", "is_issue": True}

        # Mixed filter with repo
        repo_filter = tui.QueueFilters(repository="bluefinctl", kind="mixed")
        self.assertTrue(repo_filter.wants(pr1))
        self.assertFalse(repo_filter.wants(pr2))
        self.assertTrue(repo_filter.wants(issue1))

        # Action filter: "review" should keep pr1, drop pr2 and issue1
        action_filter = tui.QueueFilters(action="review", kind="mixed")
        self.assertTrue(action_filter.wants(pr1))
        self.assertFalse(action_filter.wants(pr2))
        self.assertFalse(action_filter.wants(issue1))

    def test_dashboard_default_view_mode_is_pull_requests(self) -> None:
        app = tui.ReviewDashboard()
        self.assertEqual(app.view_mode, "prs")
        self.assertEqual(app.filters.kind, "prs")

    def test_tab_cycles_prs_issues_mixed_prs(self) -> None:
        app = tui.ReviewDashboard()
        self.assertEqual(app.view_mode, "prs")
        app.action_toggle_view()
        self.assertEqual(app.view_mode, "issues")
        self.assertEqual(app.filters.kind, "issues")
        app.action_toggle_view()
        self.assertEqual(app.view_mode, "mixed")
        self.assertEqual(app.filters.kind, "mixed")
        app.action_toggle_view()
        self.assertEqual(app.view_mode, "prs")
        self.assertEqual(app.filters.kind, "prs")

    def test_i_is_the_only_view_toggle_binding(self) -> None:
        self.assertEqual(
            [command.key for command in tui.COMMANDS if command.action == "toggle_view"],
            ["I"],
        )

    def test_queue_controls_have_direct_key_bindings(self) -> None:
        self.assertEqual(
            [
                command.key
                for command in tui.COMMANDS
                if command.action in {
                    "decrease_landing_concurrency",
                    "increase_landing_concurrency",
                    "toggle_landing_pause",
                }
            ],
            ["-", "+", "p"],
        )

    def test_only_named_repositories_block_missing_human_review(self) -> None:
        for repository in (
            "projectbluefin/common",
            "projectbluefin/bluefin",
            "projectbluefin/bluefin-lts",
            "projectbluefin/dakota",
        ):
            self.assertEqual(
                tui.classify_routability(
                    tui.Stop(
                        repository,
                        1,
                        "review",
                        "one",
                        failure="landing refused: no human review on GitHub",
                    )
                ),
                "human review required",
            )
        self.assertIsNone(
            tui.classify_routability(
                tui.Stop(
                    "projectbluefin/documentation",
                    1,
                    "review",
                    "one",
                    failure="landing refused: no human review on GitHub",
                )
            )
        )

    def test_multi_pr_slay_confirmation_uses_one_word(self) -> None:
        gate = tui.SlayConfirmScreen([
            tui.Stop("acme/repo", 1, "review", "one"),
            tui.Stop("acme/repo", 2, "review", "two"),
        ])
        self.assertEqual(gate.expected, "slay")

    def test_batch_selection_does_not_run_lifecycle_refresh(self) -> None:
        app = tui.ReviewDashboard()
        stop = tui.Stop("acme/repo", 1, "review", "one")
        with (
            mock.patch.object(
                type(app), "current", new_callable=mock.PropertyMock, return_value=stop
            ),
            mock.patch.object(app, "refresh_rows") as refresh_rows,
        ):
            app.action_batch()
        self.assertTrue(stop.selected)
        refresh_rows.assert_not_called()

    def test_apply_filters_composes_prs_and_issues_in_mixed_mode(self) -> None:
        app = tui.ReviewDashboard()
        app.view_mode = "mixed"
        app.filters.kind = "mixed"
        app.queue_items = [
            {
                "repository": "projectbluefin/bluefinctl",
                "number": 31,
                "recommended_action": "review",
                "title": "fix: ci permissions",
                "author": "contributor-a",
                "is_issue": False,
            }
        ]
        app.issues_items = [
            {
                "repository": "projectbluefin/review",
                "number": 42,
                "action": "triage",
                "recommended_action": "triage",
                "title": "bug: test issue",
                "author": "issue-reporter",
                "is_issue": True,
            }
        ]
        app.pr_source_state = "ready"
        app.issues_source_state = "ready"

        # In mixed mode: both items become stops
        app.apply_filters()
        self.assertEqual(len(app.stops), 2)
        pr_stops = [s for s in app.stops if not s.is_issue]
        issue_stops = [s for s in app.stops if s.is_issue]
        self.assertEqual(len(pr_stops), 1)
        self.assertEqual(len(issue_stops), 1)
        self.assertEqual(pr_stops[0].key, "projectbluefin/bluefinctl#31")
        self.assertEqual(issue_stops[0].key, "projectbluefin/review#42")

        # In prs lens: only PR stop
        app.action_toggle_view()  # mixed -> prs
        self.assertEqual(app.view_mode, "prs")
        self.assertEqual(len(app.stops), 1)
        self.assertFalse(app.stops[0].is_issue)
        self.assertEqual(app.stops[0].key, "projectbluefin/bluefinctl#31")

        # In issues lens: only issue stop
        app.action_toggle_view()  # prs -> issues
        self.assertEqual(app.view_mode, "issues")
        self.assertEqual(len(app.stops), 1)
        self.assertTrue(app.stops[0].is_issue)
        self.assertEqual(app.stops[0].key, "projectbluefin/review#42")

    def test_independent_source_states_and_messages(self) -> None:
        app = tui.ReviewDashboard()
        app.queue_items = [
            {
                "repository": "projectbluefin/bluefinctl",
                "number": 31,
                "recommended_action": "review",
                "title": "fix: ci",
                "author": "someone",
                "is_issue": False,
            }
        ]
        app._apply_queue_snapshot({
            "self_login": "tester",
            "state": "ready",
            "message": "",
            "items": app.queue_items,
        })
        self.assertEqual(app.pr_source_state, "ready")

        # Failing issue snapshot must not corrupt PR source state or drop PR stops
        app._apply_issues_snapshot({
            "state": "error",
            "message": "GraphQL 502 Bad Gateway",
            "items": [],
        })
        self.assertEqual(app.issues_source_state, "error")
        self.assertEqual(app.issues_source_message, "GraphQL 502 Bad Gateway")
        self.assertEqual(app.pr_source_state, "ready")
        self.assertEqual(app.pr_source_message, "")

        # In mixed mode, PR stops remain rendered and usable
        self.assertEqual(len(app.stops), 1)
        self.assertEqual(app.stops[0].key, "projectbluefin/bluefinctl#31")

        # In issues lens, issue error is shown
        app.view_mode = "issues"
        app._sync_source_state()
        self.assertEqual(app.source_state, "error")
        self.assertEqual(app.source_message, "GraphQL 502 Bad Gateway")

        # In prs lens, PR state is ready
        app.view_mode = "prs"
        app._sync_source_state()
        self.assertEqual(app.source_state, "ready")
        self.assertEqual(app.source_message, "")

        # Symmetrically: when issues are ready, a failing PR snapshot must not corrupt issue state or drop issue stops
        app.issues_items = [
            {
                "repository": "projectbluefin/review",
                "number": 42,
                "action": "triage",
                "recommended_action": "triage",
                "title": "bug: test",
                "author": "someone",
                "is_issue": True,
            }
        ]
        app._apply_issues_snapshot({
            "state": "ready",
            "message": "",
            "items": app.issues_items,
        })
        self.assertEqual(app.issues_source_state, "ready")
        # Failing PR snapshot: PR error recorded, but last-good data is retained
        app._apply_queue_snapshot({
            "self_login": "tester",
            "state": "error",
            "message": "GitHub GraphQL error 500",
            "items": [],
        })
        self.assertEqual(app.pr_source_state, "error")
        self.assertEqual(app.pr_source_message, "GitHub GraphQL error 500")
        self.assertEqual(app.issues_source_state, "ready")
        # In mixed mode, both last-good PR stop and current issue stop remain usable
        app.view_mode = "mixed"
        app.apply_filters()
        self.assertEqual(len(app.stops), 2)
        self.assertEqual(
            {s.key for s in app.stops},
            {"projectbluefin/bluefinctl#31", "projectbluefin/review#42"},
        )

    def test_shared_type_agnostic_selection(self) -> None:
        app = tui.ReviewDashboard()
        pr_stop = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
        )
        issue_stop = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
        )
        app.stops = [pr_stop, issue_stop]

        # Select both using selection keys
        pr_stop.selected = True
        issue_stop.selected = True

        self.assertEqual(app._queue_state(pr_stop), "queued")
        self.assertEqual(app._queue_state(issue_stop), "queued")

    def test_sorting_with_mixed_items(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        pr_unreviewed = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR unreviewed",
            is_issue=False,
        )
        pr_reviewed = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=32,
            action="review",
            title="PR reviewed",
            live={"reviews": [{"author": {"login": "tester"}, "state": "APPROVED"}]},
            is_issue=False,
        )
        issue = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue triage",
            is_issue=True,
        )
        failed_issue = tui.Stop(
            repository="projectbluefin/review",
            number=43,
            action="triage",
            title="Issue failed",
            failure="mutation failed: not found",
            is_issue=True,
        )
        app.stops = [issue, pr_reviewed, failed_issue, pr_unreviewed]
        app._sort_stops()

        # Expected order:
        # 1. failed_issue (QUEUE_STATE_RANK["failed"] == 0)
        # 2. pr_unreviewed (QUEUE_STATE_RANK["ready"] == 3, stop_lacks_my_review == 0, action_rank("review") == 1)
        # 3. pr_reviewed (QUEUE_STATE_RANK["ready"] == 3, stop_lacks_my_review == 1, action_rank("review") == 1)
        # 4. issue (QUEUE_STATE_RANK["ready"] == 3, stop_lacks_my_review == 1, action_rank("triage") == 5)
        self.assertEqual(
            [s.key for s in app.stops],
            [
                "projectbluefin/review#43",
                "projectbluefin/bluefinctl#31",
                "projectbluefin/bluefinctl#32",
                "projectbluefin/review#42",
            ],
        )

    def test_hive_rank_is_display_only_not_queue_sort_priority(self) -> None:
        """Hive contributor projections are read-only display evidence, not maintainer queue priority."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        pr_unreviewed = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR unreviewed",
            is_issue=False,
        )
        issue = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue triage",
            is_issue=True,
        )
        app.stops = [issue, pr_unreviewed]
        # Assign Hive rank 0 to issue and rank 10 to PR
        app.hive_ranks = {
            "projectbluefin/review#42": 0,
            "projectbluefin/bluefinctl#31": 10,
        }
        app._sort_stops()

        # PR unreviewed (stop_lacks_my_review == 0, action_rank == 1) MUST precede issue (action_rank == 5)
        # Hive rank must NOT invert or override maintainer-owned priority!
        self.assertEqual(
            [s.key for s in app.stops],
            [
                "projectbluefin/bluefinctl#31",
                "projectbluefin/review#42",
            ],
        )

    def test_hive_loaded_uses_refresh_rows_to_keep_queue_synchronized(self) -> None:
        """hive_loaded() must use refresh_rows() highlight-preserving repaint path, not bare _sort_stops."""
        app = tui.ReviewDashboard()
        refresh_rows_called = False

        def fake_refresh_rows():
            nonlocal refresh_rows_called
            refresh_rows_called = True

        app.refresh_rows = fake_refresh_rows
        app.hive_loaded("online · 1 working", [])
        self.assertTrue(refresh_rows_called)

    def test_stop_lacks_my_review_false_for_issues(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        issue = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
        )
        self.assertFalse(app.stop_lacks_my_review(issue))

    def test_mixed_selection_filters_to_prs_and_notifies_issues_skipped_for_pr_automation(self) -> None:
        app = tui.ReviewDashboard()
        pr_stop = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
        )
        issue_stop = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
            live={"labels": [], "comments_count": 0},
        )
        app.stops = [pr_stop, issue_stop]
        pr_stop.selected = True
        issue_stop.selected = True

        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append((message, severity))

        # 1. action_review
        reviewed_batches: list[list[tui.Stop]] = []
        app.start_review_batch = lambda batch, **kwargs: reviewed_batches.append(batch)
        app.action_review()

        self.assertEqual(len(reviewed_batches), 1)
        self.assertEqual(reviewed_batches[0], [pr_stop])
        # Issue selection must remain intact
        self.assertTrue(issue_stop.selected)
        # Must notify that selected issues were skipped
        self.assertTrue(any("issue" in m.lower() and "skipped" in m.lower() for m, _ in notices))

        # 2. action_land_batch
        notices.clear()
        planned_batches: list[list[tui.Stop]] = []
        app.plan_landing = lambda batch: planned_batches.append(batch)
        app.action_land_batch()

        self.assertEqual(len(planned_batches), 1)
        self.assertEqual(planned_batches[0], [pr_stop])
        self.assertTrue(issue_stop.selected)
        self.assertTrue(any("issue" in m.lower() and "skipped" in m.lower() for m, _ in notices))

        # 3. action_update_branch
        notices.clear()
        mutated_stops: list[tui.Stop] = []
        mutated_cmds: list[list[list[str]]] = []
        app.mutate_all = lambda stop, cmds, then=None, on_error=None: (mutated_stops.append(stop), mutated_cmds.append(cmds))
        app.action_update_branch()

        self.assertEqual(mutated_stops, [pr_stop])
        self.assertTrue(issue_stop.selected)
        self.assertTrue(any("issue" in m.lower() and "skipped" in m.lower() for m, _ in notices))
        for cmds in mutated_cmds:
            for cmd in cmds:
                self.assertNotIn("42", cmd)

        # 4. action_merge (batch queue automerge)
        pr_stop2 = tui.Stop(
            repository="projectbluefin/common",
            number=7,
            action="review",
            title="PR 7",
            is_issue=False,
            head_sha="c" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "c" * 40},
        )
        pr_stop2.selected = True
        app.stops = [pr_stop, pr_stop2, issue_stop]
        queued_batches: list[list[tui.Stop]] = []
        app.batch_queue_automerge = lambda batch: queued_batches.append(batch)
        notices.clear()
        app.action_merge()

        self.assertEqual(len(queued_batches), 1)
        self.assertEqual(queued_batches[0], [pr_stop, pr_stop2])
        self.assertTrue(issue_stop.selected)
        self.assertTrue(any("issue" in m.lower() and "skipped" in m.lower() for m, _ in notices))

        # 5. When ONLY issues are selected, PR automation must do nothing and notify
        pr_stop.selected = False
        pr_stop2.selected = False
        issue_stop.selected = True
        reviewed_batches.clear()
        notices.clear()
        app.action_review()
        self.assertEqual(len(reviewed_batches), 0)
        self.assertTrue(any("issue" in m.lower() and "skipped" in m.lower() for m, _ in notices))

        # 6. Current issue with no selection receives normal PR-only warning
        issue_stop.selected = False
        # Mock current property returning issue_stop
        type(app).current = property(lambda self: issue_stop)
        notices.clear()
        app.action_review()
        self.assertTrue(any("pull requests only" in m.lower() for m, _ in notices))

    def test_action_filter_and_status_include_triage_and_accurate_mixed_totals(self) -> None:
        app = tui.ReviewDashboard()
        app.queue_items = [
            {"repository": "projectbluefin/bluefinctl", "number": 31, "recommended_action": "review", "title": "PR 31", "author": "dev", "is_issue": False},
            {"repository": "projectbluefin/common", "number": 7, "recommended_action": "investigate", "title": "PR 7", "author": "dev", "is_issue": False},
        ]
        app.issues_items = [
            {"repository": "projectbluefin/review", "number": 42, "action": "triage", "recommended_action": "triage", "title": "Issue 42", "author": "dev", "is_issue": True},
        ]
        app.view_mode = "mixed"
        app.filters.kind = "mixed"
        app.apply_filters()

        # Composed source set should have 3 items
        self.assertEqual(len(app.active_source_items), 3)

        # Status totals and breakdown in mixed view before filtering
        updated_status: list[str] = []
        mock_status_bar = mock.MagicMock()
        mock_status_bar.update = lambda text: updated_status.append(text)
        app.query_one = lambda sel, *args: mock_status_bar if sel == "#status-bar" else mock.MagicMock()
        app.refresh_status()
        self.assertTrue(updated_status)
        status_text = updated_status[-1]
        self.assertIn("Board: 3 items (2 PRs, 1 issues)", status_text)
        self.assertIn("1 triage", status_text)
        self.assertNotIn("of 2", status_text)  # not falsely held back when all 3 shown

        # Cycling action filter should include "triage"
        self.assertEqual(app.filters.action, "")
        app.action_filter()
        self.assertEqual(app.filters.action, "review")
        app.action_filter()
        self.assertEqual(app.filters.action, "investigate")
        app.action_filter()
        self.assertEqual(app.filters.action, "triage")
        self.assertEqual(len(app.stops), 1)
        self.assertEqual(app.stops[0].key, "projectbluefin/review#42")

        # In filtered state (triage only), held-back count accurately reflects 3 total items
        updated_status.clear()
        app.refresh_status()
        status_text = updated_status[-1]
        self.assertIn("Board: 1 items (0 PRs, 1 issues) (of 3; [f] widens)", status_text)

        app.action_filter()
        self.assertEqual(app.filters.action, "")
        self.assertEqual(len(app.stops), 3)

    def test_mixed_nonempty_issues_prevent_victory_state(self) -> None:
        app = tui.ReviewDashboard()
        app.view_mode = "mixed"
        app.filters.kind = "mixed"
        app.pr_source_state = "empty"
        app.issues_source_state = "ready"
        app.queue_items = []
        app.issues_items = [
            {"repository": "projectbluefin/review", "number": 42, "action": "triage", "recommended_action": "triage", "title": "Issue 42", "author": "dev", "is_issue": True},
        ]
        app._sync_source_state()
        app.apply_filters()

        # Must not celebrate victory: slay_frame must be -1
        self.assertEqual(app.slay_frame, -1)
        self.assertEqual(len(app.stops), 1)

        # Only when both sources are empty should mixed mode enter victory state
        app.issues_items = []
        app.issues_source_state = "empty"
        app._sync_source_state()
        app._had_nonempty_queue = True
        app.slay_frame = -1
        app.apply_filters()
        self.assertGreaterEqual(app.slay_frame, 0)

    def test_cross_source_callbacks_do_not_discard_evidence_or_bump_unrelated_generation(self) -> None:
        app = tui.ReviewDashboard()
        app.view_mode = "mixed"
        app.filters.kind = "mixed"
        app.queue_items = [
            {"repository": "projectbluefin/bluefinctl", "number": 31, "recommended_action": "review", "title": "PR 31", "author": "dev", "is_issue": False},
        ]
        app.issues_items = [
            {"repository": "projectbluefin/review", "number": 42, "action": "triage", "recommended_action": "triage", "title": "Issue 42", "author": "dev", "is_issue": True},
        ]
        app.apply_filters()

        pr_key = "projectbluefin/bluefinctl#31"
        issue_key = "projectbluefin/review#42"
        app.evidence_generation[pr_key] = 1
        app.evidence_generation[issue_key] = 1
        pr_gen_before = app.evidence_generation[pr_key]
        issue_gen_before = app.evidence_generation[issue_key]

        # Issue snapshot arrival must NOT bump PR evidence generation
        app._apply_issues_snapshot({
            "state": "ready",
            "message": "",
            "items": app.issues_items,
        })
        self.assertEqual(app.evidence_generation[pr_key], pr_gen_before)

        # Record issue generation after issue refresh
        issue_gen_after_issue_refresh = app.evidence_generation[issue_key]

        # PR snapshot arrival must NOT bump issue evidence generation
        app._apply_queue_snapshot({
            "self_login": "tester",
            "state": "ready",
            "message": "",
            "items": app.queue_items,
        })
        self.assertEqual(app.evidence_generation[issue_key], issue_gen_after_issue_refresh)

    def test_issue_steering_and_direct_review_blocked(self) -> None:
        app = tui.ReviewDashboard()
        issue_stop = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
        )
        pr_stop = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
        )
        app.stops = [issue_stop, pr_stop]

        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append((message, severity))

        screens_pushed: list[object] = []
        app.push_screen = lambda screen, callback=None: screens_pushed.append(screen)

        # 1. Central direct-review entrypoint guard: start_review on an issue
        app.start_review(issue_stop, steer="check this")
        self.assertEqual(len(screens_pushed), 0)
        self.assertTrue(
            any("pull requests only" in m.lower() and s == "warning" for m, s in notices),
            f"start_review on issue must notify with PR-only warning, got {notices}",
        )

        # 2. action_steer on highlighted issue
        notices.clear()
        type(app).current = property(lambda self: issue_stop)
        steer_box = mock.MagicMock()
        steer_box.id = "steer"
        steer_box.value = ""
        focused_widgets: list[object] = []
        steer_box.focus = lambda: focused_widgets.append(steer_box)
        queue_widget = mock.MagicMock()
        queue_widget.focus = lambda: focused_widgets.append(queue_widget)

        def mock_query_one(sel, *args):
            if sel == "#steer":
                return steer_box
            if sel == "#queue":
                return queue_widget
            return mock.MagicMock()

        app.query_one = mock_query_one

        app.action_steer()
        self.assertNotIn(steer_box, focused_widgets)
        self.assertTrue(
            any("pull requests only" in m.lower() and s == "warning" for m, s in notices),
            f"action_steer on issue must notify with PR-only warning, got {notices}",
        )

        # 3. action_steer when view_mode == "issues"
        notices.clear()
        focused_widgets.clear()
        app.view_mode = "issues"
        app.action_steer()
        self.assertNotIn(steer_box, focused_widgets)
        self.assertTrue(
            any("pull requests only" in m.lower() and s == "warning" for m, s in notices),
            f"action_steer in issues view must notify with PR-only warning, got {notices}",
        )
        app.view_mode = "mixed"

        # 4. on_input_submitted with #steer on an issue
        notices.clear()
        focused_widgets.clear()
        screens_pushed.clear()
        reviews_started: list[tuple[tui.Stop, str]] = []
        original_start_review = app.start_review
        app.start_review = lambda stop, steer="": reviews_started.append((stop, steer))

        event = mock.MagicMock()
        event.input.id = "steer"
        event.value = "steer instruction"
        steer_box.value = "steer instruction"

        app.on_input_submitted(event)

        self.assertEqual(steer_box.value, "")
        self.assertIn(queue_widget, focused_widgets)
        self.assertEqual(len(reviews_started), 0, "on_input_submitted on issue must not invoke start_review")
        self.assertTrue(
            any("pull requests only" in m.lower() and s == "warning" for m, s in notices),
            f"on_input_submitted on issue must notify with PR-only warning, got {notices}",
        )

        # 5. Normal PR steering must be preserved
        app.start_review = original_start_review
        notices.clear()
        focused_widgets.clear()
        screens_pushed.clear()
        type(app).current = property(lambda self: pr_stop)

        app.action_steer()
        self.assertIn(steer_box, focused_widgets)
        self.assertEqual(len(notices), 0)

        focused_widgets.clear()
        steer_box.value = "valid pr steer"
        event.value = "valid pr steer"
        app.on_input_submitted(event)
        self.assertEqual(steer_box.value, "")
        self.assertIn(queue_widget, focused_widgets)
        self.assertEqual(len(screens_pushed), 1)
        review_screen = screens_pushed[0]
        self.assertIsInstance(review_screen, tui.ReviewScreen)
        self.assertEqual(review_screen.stop_record, pr_stop)
        self.assertEqual(review_screen.steer, "valid pr steer")

    def test_routability_classifier_is_pure_and_detects_structural_blocks(self) -> None:
        # 1. Draft PR
        pr_draft = tui.Stop(
            repository="projectbluefin/review",
            number=1,
            action="review",
            title="Draft PR",
            is_issue=False,
            live={"isDraft": True},
        )
        reason_draft = tui.classify_routability(pr_draft)
        self.assertIsNotNone(reason_draft)
        self.assertIn("draft", reason_draft.lower())

        # Purity check: repeated calls return same result, inputs unchanged
        self.assertEqual(tui.classify_routability(pr_draft), reason_draft)
        self.assertEqual(pr_draft.live.get("isDraft"), True)

        # 2. Cross-repository / fork PR that cannot be modified
        pr_fork_unmodifiable = tui.Stop(
            repository="projectbluefin/review",
            number=2,
            action="review",
            title="Fork PR unmodifiable",
            is_issue=False,
            live={"isCrossRepository": True, "maintainerCanModify": False},
        )
        reason_fork = tui.classify_routability(pr_fork_unmodifiable)
        self.assertIsNotNone(reason_fork)
        self.assertTrue(
            any(w in reason_fork.lower() for w in ("fork", "cross-repository", "modify", "permission"))
        )

        # Modifiable fork must NOT be blocked
        pr_fork_modifiable = tui.Stop(
            repository="projectbluefin/review",
            number=3,
            action="review",
            title="Fork PR modifiable",
            is_issue=False,
            live={"isCrossRepository": True, "maintainerCanModify": True},
        )
        self.assertIsNone(tui.classify_routability(pr_fork_modifiable))

        # 3. Durable current-head mutation failure due to push permission denial
        run_ident = tui.ReceiptIdentity(
            repository="projectbluefin/review",
            pull_request=4,
            base_sha="a" * 40,
            head_sha="b" * 40,
            backend="goose",
            model="gemini-3.8-flash",
            effort="high",
            check_scope_version="image-v1",
        )
        rec_push_denied = tui.RunRecord(
            identity=run_ident,
            state=tui.RunState.MUTATION_FAILED,
            terminal_outcome=tui.TerminalOutcome.MUTATION_FAILED,
            reason="push permission denied",
            retry_at="",
            created_at=100,
            updated_at=100,
            sequence=1,
        )
        pr_push_denied = tui.Stop(
            repository="projectbluefin/review",
            number=4,
            action="review",
            title="Push denied PR",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40},
        )
        reason_push = tui.classify_routability(pr_push_denied, record=rec_push_denied)
        self.assertIsNotNone(reason_push)
        self.assertIn("push", reason_push.lower())

        # Also detected from durable stop.failure mark
        pr_push_denied_mark = tui.Stop(
            repository="projectbluefin/review",
            number=5,
            action="review",
            title="Push denied mark PR",
            is_issue=False,
            failure="landing refused: push permission denied",
        )
        self.assertIsNotNone(tui.classify_routability(pr_push_denied_mark))

        # 4. Durable human review required state
        rec_human_req = tui.RunRecord(
            identity=run_ident,
            state=tui.RunState.HUMAN_REVIEW_MISSING,
            terminal_outcome=tui.TerminalOutcome.HUMAN_REVIEW_MISSING,
            reason="no human review on GitHub",
            retry_at="",
            created_at=100,
            updated_at=100,
            sequence=1,
        )
        pr_human_req = tui.Stop(
            repository="projectbluefin/common",
            number=4,
            action="review",
            title="Human review PR",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40},
        )
        reason_human = tui.classify_routability(pr_human_req, record=rec_human_req)
        self.assertIsNotNone(reason_human)
        self.assertTrue(
            any(w in reason_human.lower() for w in ("human", "review"))
        )

        # 5. Durable head-changed state
        rec_head_changed = tui.RunRecord(
            identity=run_ident,
            state=tui.RunState.HEAD_CHANGED,
            terminal_outcome=tui.TerminalOutcome.HEAD_CHANGED,
            reason="reviewed 1111, live 2222",
            retry_at="",
            created_at=100,
            updated_at=100,
            sequence=1,
        )
        pr_head_changed = tui.Stop(
            repository="projectbluefin/review",
            number=5,
            action="review",
            title="Head changed PR",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40},
        )
        reason_head_changed = tui.classify_routability(pr_head_changed, record=rec_head_changed)
        self.assertIsNotNone(reason_head_changed)
        self.assertIn("head changed", reason_head_changed.lower())

        # Also detected from durable stop.failure mark
        pr_head_changed_mark = tui.Stop(
            repository="projectbluefin/review",
            number=6,
            action="review",
            title="Head changed mark PR",
            is_issue=False,
            failure="landing aborted: head changed (reviewed 1111, live 2222)",
        )
        self.assertIsNotNone(tui.classify_routability(pr_head_changed_mark))

        # 6. Absent or malformed evidence must NOT be classified as blocked
        pr_empty = tui.Stop(
            repository="projectbluefin/review",
            number=7,
            action="review",
            title="Empty PR",
            is_issue=False,
            live={},
        )
        self.assertIsNone(tui.classify_routability(pr_empty))

        # 7. Ordinary transient review failure must remain actionable (NOT blocked)
        pr_transient = tui.Stop(
            repository="projectbluefin/review",
            number=8,
            action="review",
            title="Transient review fail PR",
            is_issue=False,
            review_status="failed",
            review_failure="review dispatch failed: connection reset",
        )
        self.assertIsNone(tui.classify_routability(pr_transient))

    def test_slay_action_warns_and_no_ops_on_each_structural_block(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        # Structural block cases
        cases = [
            ("draft", tui.Stop("projectbluefin/review", 1, "review", "draft", is_issue=False, live={"isDraft": True})),
            ("unmodifiable fork", tui.Stop("projectbluefin/review", 2, "review", "fork", is_issue=False, live={"isCrossRepository": True, "maintainerCanModify": False})),
            ("push denied", tui.Stop("projectbluefin/review", 3, "review", "push", is_issue=False, failure="landing refused: push permission denied")),
            ("human review required", tui.Stop("projectbluefin/common", 4, "review", "human", is_issue=False, failure="landing refused: no human review on GitHub")),
            ("head changed", tui.Stop("projectbluefin/review", 5, "review", "head changed", is_issue=False, failure="landing aborted: head changed (reviewed 1111, live 2222)")),
        ]

        for label, stop in cases:
            app.stops = [stop]
            type(app).current = property(lambda self, s=stop: s)
            notices: list[tuple[str, str]] = []
            app.notify = lambda message, severity="information": notices.append((message, severity))
            reviews_started: list[list[tui.Stop]] = []
            app.start_review_batch = lambda batch: reviews_started.append(batch)
            landings_dispatched: list[tui.Stop] = []
            app._dispatch_slay_landing = lambda s, **kw: landings_dispatched.append(s)

            app.action_slay_pr()

            self.assertEqual(len(reviews_started), 0, f"{label} must not start review")
            self.assertEqual(len(landings_dispatched), 0, f"{label} must not dispatch landing")
            self.assertTrue(
                any(s == "warning" for _, s in notices),
                f"{label} must emit a warning notification, got {notices}",
            )

    def test_slay_action_remains_available_for_transient_review_failure(self) -> None:
        store_dir = REPO_ROOT / ".cache" / "test_slay_store"
        store_dir.mkdir(parents=True, exist_ok=True)
        runs_file = store_dir / "run-state.json"
        if runs_file.exists():
            runs_file.unlink()

        app = tui.ReviewDashboard(run_store=tui.RunStateStore(store_dir))
        app.self_login = "tester"
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=10,
            action="review",
            title="Transient fail PR",
            is_issue=False,
            review_status="failed",
            review_failure="provider failed",
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "isDraft": False},
        )
        app.stops = [stop]
        type(app).current = property(lambda self: stop)

        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append((message, severity))
        reviews_started: list[list[tui.Stop]] = []
        app.start_review_batch = lambda batch: reviews_started.append(batch)
        app.push_screen = lambda s, cb=None: cb(True) if cb else None

        try:
            app.action_slay_pr()

            # Transient review failure must start review to retry
            self.assertEqual(len(reviews_started), 1)
            self.assertEqual(reviews_started[0], [stop])
        finally:
            if store_dir.exists():
                shutil.rmtree(store_dir, ignore_errors=True)

    def test_blocked_items_render_and_sort_separately_without_disappearing(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        pr_actionable_fail = tui.Stop(
            repository="projectbluefin/review",
            number=1,
            action="review",
            title="Transient fail",
            is_issue=False,
            review_status="failed",
            review_failure="timeout",
        )
        pr_blocked = tui.Stop(
            repository="projectbluefin/review",
            number=2,
            action="review",
            title="Draft PR",
            is_issue=False,
            live={"isDraft": True},
        )
        pr_ready = tui.Stop(
            repository="projectbluefin/review",
            number=3,
            action="review",
            title="Ready PR",
            is_issue=False,
        )

        app.stops = [pr_ready, pr_blocked, pr_actionable_fail]

        # Blocked state must be distinct
        self.assertEqual(app._queue_state(pr_actionable_fail), "failed")
        self.assertEqual(app._queue_state(pr_blocked), "blocked")
        self.assertEqual(app._queue_state(pr_ready), "ready")

        # Must render visibly
        badge_blocked = app._review_badge(pr_blocked)
        self.assertIn("BLOCKED", badge_blocked)

        # Must sort separately from actionable failures
        app._sort_stops()
        keys = [s.key for s in app.stops]
        self.assertEqual(keys, [pr_actionable_fail.key, pr_ready.key, pr_blocked.key])
        self.assertIn(pr_blocked.key, keys)  # blocked is retained in stops
        self.assertNotEqual(keys[0], pr_blocked.key)  # blocked does not collide with actionable failures

        # Blocked items do NOT disappear from source
        app.queue_items = [
            {"repository": "projectbluefin/review", "number": 1, "is_issue": False},
            {"repository": "projectbluefin/review", "number": 2, "is_issue": False, "is_draft": True},
            {"repository": "projectbluefin/review", "number": 3, "is_issue": False},
        ]
        app.apply_filters()
        self.assertEqual(len(app.stops), 3)
        self.assertIn(pr_blocked.key, [s.key for s in app.stops])

    def test_hive_ranked_pr_and_issue_sorting(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        pr1 = tui.Stop(repository="projectbluefin/bluefinctl", number=31, action="review", title="PR 31", is_issue=False)
        pr2 = tui.Stop(repository="projectbluefin/common", number=7, action="review", title="PR 7", is_issue=False)
        issue1 = tui.Stop(repository="projectbluefin/review", number=42, action="triage", title="Issue 42", is_issue=True)
        issue2 = tui.Stop(repository="projectbluefin/review", number=99, action="triage", title="Issue 99", is_issue=True)

        # Hive ranks: issue1 is rank 0 (#1 priority), pr2 is rank 1, pr1 is rank 2; issue2 is unranked
        ready_payload = [
            {"repo": "projectbluefin/review", "number": 42},
            {"repo": "projectbluefin/common", "number": 7},
        ]
        triage_payload = [
            {"level": "ready", "issues": [{"repo": "projectbluefin/bluefinctl", "number": 31}]},
        ]
        ranks = tui.build_hive_rank_map(ready_payload, triage_payload)
        self.assertEqual(ranks["projectbluefin/review#42"], 0)
        self.assertEqual(ranks["projectbluefin/common#7"], 1)
        self.assertEqual(ranks["projectbluefin/bluefinctl#31"], 2)
        self.assertNotIn("projectbluefin/review#99", ranks)

        app.hive_ranks = ranks
        app.stops = [issue2, pr1, pr2, issue1]
        app._sort_stops()

        # Maintainer-owned order (action rank: review before triage; repo; number):
        # 1. projectbluefin/bluefinctl#31 (action: review)
        # 2. projectbluefin/common#7 (action: review)
        # 3. projectbluefin/review#42 (action: triage)
        # 4. projectbluefin/review#99 (action: triage)
        self.assertEqual(
            [s.key for s in app.stops],
            [
                "projectbluefin/bluefinctl#31",
                "projectbluefin/common#7",
                "projectbluefin/review#42",
                "projectbluefin/review#99",
            ],
        )
        # Hive ranks are display-only evidence rendered into row markup
        self.assertIn("[cyan]#1[/cyan]", app.row_markup(issue1))
        self.assertIn("[cyan]#2[/cyan]", app.row_markup(pr2))
        self.assertIn("[cyan]#3[/cyan]", app.row_markup(pr1))
        self.assertNotIn("[cyan]#", app.row_markup(issue2))
        self.assertNotIn("[dim]#", app.row_markup(issue2))

    def test_retained_hive_rank_evidence_renders_status_and_age(self) -> None:
        """Retained/unavailable Hive rank evidence must carry status/age rather than unqualified current #N."""
        import time

        app = tui.ReviewDashboard()
        app.self_login = "tester"
        pr = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
        )
        app.stops = [pr]
        app.hive_ranks = {"projectbluefin/bluefinctl#31": 0}
        app.hive_snapshot_at = time.monotonic() - 120  # 2 minutes ago
        app.hive_queue_stale = True

        # Stale/retained rank MUST carry status and age
        markup = app.row_markup(pr)
        self.assertNotIn("[cyan]#1[/cyan]", markup)
        self.assertIn("[dim]#1 (retained 2m ago)[/dim]", markup)

        # Fresh rank renders current cyan #1
        app.hive_queue_stale = False
        app.hive_queue_unavailable = False
        fresh_markup = app.row_markup(pr)
        self.assertIn("[cyan]#1[/cyan]", fresh_markup)
        self.assertNotIn("(retained", fresh_markup)

    def test_hive_reconciliation_healthy_when_optional_enrichment_absent(self) -> None:
        """Healthy Hive status/fleet must reconcile successfully even if optional queue/triage payloads are absent or triage counts."""
        app = tui.ReviewDashboard()
        reconciliation_finished_args = []

        def fake_finished(source, req, attempt, success):
            reconciliation_finished_args.append((source, success))

        app._reconciliation_finished = fake_finished

        # Simulate _poll_hive with healthy status and fleet, but absent queue (404) and triage counts shape
        status_res = tui.hive_api.Result(True, "ok", "", {"hub": "online", "actionable_items": 5})
        fleet_res = tui.hive_api.Result(True, "ok", "", {"contributors": []})
        queue_res = tui.hive_api.Result(False, "not_found", "404 Not Found", {})
        triage_res = tui.hive_api.Result(True, "ok", "", {"stages": {"triage": 2, "ready": 1}})

        def fake_get(endpoint):
            if endpoint in ("/api/contribute/status", "/api/v1/status"):
                return status_res
            if endpoint in ("/api/contribute/fleet", "/api/v1/contributors"):
                return fleet_res
            if endpoint == "/api/contribute/queue":
                return queue_res
            if endpoint == "/api/contribute/triage":
                return triage_res
            return tui.hive_api.Result(False, {}, "unknown", "unknown")

        with (
            mock.patch.object(tui, "hive_api_base", return_value="https://hive.example"),
            mock.patch.object(tui, "hive_get", side_effect=fake_get),
            mock.patch.object(tui, "get_current_worker") as mock_worker,
        ):
            mock_worker.return_value.is_cancelled = False
            app.call_from_thread = lambda fn, *args: fn(*args)
            app.load_hive.__wrapped__(app, reconciliation_request=1, reconciliation_attempt=1)

        self.assertEqual(len(reconciliation_finished_args), 1)
        source, success = reconciliation_finished_args[0]
        self.assertEqual(source, "hive")
        # Reconciliation MUST be considered successful because core Hive status + fleet were ok
        self.assertTrue(success)

    def test_repo_scoped_and_mixed_source_failure_never_displays_empty_successful_queue(self) -> None:
        """Repo-scoped/mixed source failure must never display as empty successful queue."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        app.filters.live_repository = "projectbluefin/review"
        app.queue_items = [{"repository": "projectbluefin/review", "number": 1, "is_issue": False}]
        app.pr_source_state = "ready"
        app.issues_source_state = "error"
        app.issues_source_message = "GraphQL 502"

        # 1. In issues view with repo-scope, source_state must reflect issue error, NOT PR ready
        app.view_mode = "issues"
        app._sync_source_state()
        self.assertEqual(app.source_state, "error")
        self.assertEqual(app.source_message, "GraphQL 502")

        # 2. In mixed view with repo-scope, source_state must reflect degraded state
        app.view_mode = "mixed"
        app._sync_source_state()
        self.assertEqual(app.source_state, "degraded")
        self.assertIn("Issue source failed", app.source_message)

        # 3. populate() with failed source state must render error ListItem, not empty queue success
        app.source_state = "error"
        app.source_message = "API unavailable"
        queue_mock = mock.MagicMock()
        items_appended = []
        queue_mock.append = lambda item: items_appended.append(item)
        app.query_one = lambda id, *args, **kwargs: queue_mock if id == "#queue" else mock.MagicMock()

        app.populate([])
        self.assertTrue(len(items_appended) > 0)
        first_static = items_appended[0]._pending_children[0]
        first_text = str(first_static.render())
        self.assertNotIn("No open pull requests or issues found", first_text)
        self.assertIn("Could not load", first_text)

    def test_unavailable_hive_projection_leaves_board_usable(self) -> None:
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        pr1 = tui.Stop(repository="projectbluefin/bluefinctl", number=31, action="review", title="PR 31", is_issue=False)
        issue1 = tui.Stop(repository="projectbluefin/review", number=42, action="triage", title="Issue 42", is_issue=True)

        app.stops = [issue1, pr1]
        # Hive projection is unavailable: hive_ranks is empty, hive_queue_stale is True
        app.hive_ranks = {}
        app.hive_queue_unavailable = True
        app.hive_queue_stale = False
        app._sort_stops()

        # Board remains completely sorted and usable
        self.assertEqual(len(app.stops), 2)
        # Unranked items sort by default rules (PR review comes before issue triage)
        self.assertEqual([s.key for s in app.stops], ["projectbluefin/bluefinctl#31", "projectbluefin/review#42"])

        # Status and context visibly reflect unavailable/retained state
        status_bar = mock.MagicMock()
        status_texts: list[str] = []
        status_bar.update = lambda t: status_texts.append(t)
        app.query_one = lambda sel, *args: status_bar if sel == "#status-bar" else mock.MagicMock()
        app.refresh_status()
        self.assertTrue(status_texts)

    def test_batch_review_dispatches_without_manual_entry_and_excludes_issues(self) -> None:
        store_dir = REPO_ROOT / ".cache" / "test_batch_store"
        store_dir.mkdir(parents=True, exist_ok=True)
        runs_file = store_dir / "run-state.json"
        if runs_file.exists():
            runs_file.unlink()

        app = tui.ReviewDashboard(run_store=tui.RunStateStore(store_dir))
        app.self_login = "tester"

        pr1 = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "isDraft": False},
        )
        pr2 = tui.Stop(
            repository="projectbluefin/common",
            number=7,
            action="review",
            title="PR 7",
            is_issue=False,
            head_sha="c" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "c" * 40, "isDraft": False},
        )
        issue1 = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
            live={"labels": [], "comments_count": 0},
        )

        app.stops = [pr1, pr2, issue1]
        pr1.selected = True
        pr2.selected = True
        issue1.selected = True

        screens_pushed: list[tuple[object, object]] = []
        app.push_screen = lambda screen, callback=None: screens_pushed.append((screen, callback))

        batches_started: list[list[tui.Stop]] = []
        app.start_review_batch = lambda batch, **kwargs: batches_started.append(batch)

        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append((message, severity))

        # 1. Test 'r' (action_review): dispatches batch review without any manual prompt
        app.action_review()

        # Must never push any screen requiring typed numbers or SHAs
        self.assertEqual(len(screens_pushed), 0, f"batch review must not push modal prompts, got {screens_pushed}")
        # Must dispatch review batch with exactly the selected PRs
        self.assertEqual(len(batches_started), 1)
        self.assertEqual(batches_started[0], [pr1, pr2])
        # Issues must be excluded from review batch
        self.assertNotIn(issue1, batches_started[0])
        # Exact heads must be preserved
        self.assertEqual(batches_started[0][0].head_sha, "b" * 40)
        self.assertEqual(batches_started[0][1].head_sha, "c" * 40)

        # 2. Test '$' (action_slay_pr): pushes SlayConfirmScreen showing PRs + heads, confirming dispatches
        batches_started.clear()
        screens_pushed.clear()
        app.review_pending_keys.clear()

        try:
            app.action_slay_pr()

            # Must push typed gate
            self.assertEqual(len(screens_pushed), 1)
            gate, callback = screens_pushed[0]
            self.assertIsInstance(gate, tui.SlayConfirmScreen)
            # Confirming gate dispatches batch review for PRs, excluding issues
            callback(True)
            self.assertEqual(len(batches_started), 1)
            self.assertEqual(batches_started[0], [pr1, pr2])
            self.assertNotIn(issue1, batches_started[0])
        finally:
            if store_dir.exists():
                shutil.rmtree(store_dir, ignore_errors=True)

    def test_batch_review_exact_head_hydration_and_drift_rejection(self) -> None:
        from tui.review_snapshot import hydrate_batch_snapshot

        pr1 = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
        )
        pr2 = tui.Stop(
            repository="projectbluefin/common",
            number=7,
            action="review",
            title="PR 7",
            is_issue=False,
            head_sha="c" * 40,
        )

        live_evidence = {
            ("projectbluefin/bluefinctl", 31): {
                "baseRefOid": "a" * 40,
                "headRefOid": "b" * 40,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            },
            ("projectbluefin/common", 7): {
                "baseRefOid": "a" * 40,
                "headRefOid": "c" * 40,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            },
        }

        snapshot = hydrate_batch_snapshot([pr1, pr2], lambda r, n: live_evidence[(r, n)])
        self.assertTrue(snapshot.ready)
        self.assertEqual(len(snapshot.items), 2)
        self.assertEqual(snapshot.items[0].base_sha, "a" * 40)
        self.assertEqual(snapshot.items[0].head_sha, "b" * 40)
        self.assertEqual(snapshot.items[1].head_sha, "c" * 40)

        # Drift rejection: if live head moved, begin_review_batch must reject dispatch
        app = tui.ReviewDashboard()
        app.stops = [pr1, pr2]
        notices: list[tuple[str, str]] = []
        app.notify = lambda message, severity="information": notices.append((message, severity))
        app.review_engine = mock.MagicMock()

        # Simulate pr1 head having moved on GitHub
        pr1_drifted = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="d" * 40,  # drifted from snapshot head "b"*40
            live={"baseRefOid": "a" * 40, "headRefOid": "d" * 40},
        )
        app.stops = [pr1_drifted, pr2]
        app.begin_review_batch([pr1_drifted, pr2], snapshot)

        # ReviewEngine must not have been started
        self.assertEqual(app.review_engine.start.call_count, 0)
        # Must notify with warning about head change
        self.assertTrue(
            any("head changed" in m.lower() for m, _ in notices),
            f"stale head must warn about head change, got {notices}",
        )

    def test_mutations_preserve_typed_number_confirmation(self) -> None:
        app = tui.ReviewDashboard()
        pr1 = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40},
        )
        app.stops = [pr1]

        screens_pushed: list[object] = []
        app.push_screen = lambda screen, callback=None: screens_pushed.append(screen)

        # mutate_all must push ConfirmMutation with the exact PR number string
        commands = [["gh", "pr", "comment", "31", "--body", "test"]]
        app.mutate_all(pr1, commands)

        self.assertEqual(len(screens_pushed), 1)
        confirm_screen = screens_pushed[0]
        self.assertIsInstance(confirm_screen, tui.ConfirmMutation)
        # Required typed confirmation string must be the PR number
        self.assertEqual(confirm_screen.expected, "31")

    def test_slay_action_uses_typed_gate_showing_prs_and_heads_and_preserves_issue_selection(self) -> None:
        """Finding 1: $ must use ONE typed mutation/batch gate showing exact PRs + heads.
        Selected issues are excluded before the gate and their selection remains intact.
        """
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        pr1 = tui.Stop(
            repository="projectbluefin/bluefinctl",
            number=31,
            action="review",
            title="PR 31",
            is_issue=False,
            head_sha="b" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "b" * 40, "isDraft": False},
        )
        pr2 = tui.Stop(
            repository="projectbluefin/common",
            number=7,
            action="review",
            title="PR 7",
            is_issue=False,
            head_sha="c" * 40,
            live={"baseRefOid": "a" * 40, "headRefOid": "c" * 40, "isDraft": False},
        )
        issue1 = tui.Stop(
            repository="projectbluefin/review",
            number=42,
            action="triage",
            title="Issue 42",
            is_issue=True,
            live={"labels": [], "comments_count": 0},
        )

        app.stops = [pr1, pr2, issue1]
        pr1.selected = True
        pr2.selected = True
        issue1.selected = True

        screens_pushed: list[object] = []
        app.push_screen = lambda screen, callback=None: screens_pushed.append((screen, callback))

        app.action_slay_pr()

        # Gate must be pushed
        self.assertEqual(len(screens_pushed), 1)
        gate, callback = screens_pushed[0]
        self.assertIsInstance(gate, tui.SlayConfirmScreen)
        # Gate must target only the PRs, not issues
        self.assertEqual(gate.targets, [pr1, pr2])
        # Issue selection must remain intact
        self.assertTrue(issue1.selected)
        # Gate must show exact PRs and heads behind one bounded confirmation.
        self.assertEqual(gate.expected, "slay")
        self.assertEqual(gate.targets[0].head_identity, "b" * 40)
        self.assertEqual(gate.targets[1].head_identity, "c" * 40)
        self.assertNotIn(issue1, gate.targets)

    def test_routability_evidence_preserved_from_queue_loaders_and_rechecked_before_dispatch(self) -> None:
        """Finding 2: Preserve routability evidence through loaders and apply_filters into Stop.live.
        Re-run classification against forced live evidence before dispatch.
        """
        app = tui.ReviewDashboard()
        app.self_login = "tester"
        # Item with canonical draft and fork modifiability evidence
        app.queue_items = [
            {
                "repository": "projectbluefin/review",
                "number": 101,
                "recommended_action": "review",
                "title": "PR 101",
                "author": "someone",
                "is_issue": False,
                "isDraft": False,
                "isCrossRepository": True,
                "maintainerCanModify": False,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            }
        ]
        app.apply_filters()
        self.assertEqual(len(app.stops), 1)
        stop = app.stops[0]
        # Canonical keys must be present in Stop.live
        self.assertEqual(stop.live.get("isCrossRepository"), True)
        self.assertEqual(stop.live.get("maintainerCanModify"), False)
        self.assertEqual(stop.live.get("isDraft"), False)

        # Routability classifier detects unmodifiable fork
        self.assertEqual(tui.classify_routability(stop), "cross-repository fork cannot be modified by maintainers")

        # Now test that forced live re-check right before review/landing dispatch catches changes
        app.fetch_live_pr = mock.MagicMock(return_value={
            "headRefOid": "b" * 40,
            "baseRefOid": "a" * 40,
            "isDraft": True,  # changed to draft right before dispatch!
            "isCrossRepository": False,
            "maintainerCanModify": True,
        })
        notices: list[tuple[str, str]] = []
        app.notify = lambda m, severity="information": notices.append((m, severity))

        # Re-checking live evidence before dispatch aborts with warning
        app._dispatch_slay_landing(stop, identity=app.run_identity(stop))
        self.assertTrue(any("draft" in m.lower() for m, _ in notices))
        self.assertEqual(len(app.landing_queue), 0)

    def test_durable_terminal_review_failures_permit_same_head_retry_while_structural_remain_blocked(self) -> None:
        """Finding 3: Terminal review-provider failures permit same-head $ retry;
        structural mutation/human_review outcomes remain blocked.
        """
        store_dir = REPO_ROOT / ".cache" / "test_store_finding3"
        store_dir.mkdir(parents=True, exist_ok=True)
        try:
            store = tui.RunStateStore(store_dir)
            ident = tui.RunIdentity(
                repository="projectbluefin/review",
                pull_request=50,
                base_sha="a" * 40,
                head_sha="b" * 40,
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            # Test review-provider failures permit transition back to REVIEWING
            for pr_num, fail_state in enumerate((
                tui.RunState.REVIEW_FAILED,
                tui.RunState.REVIEW_MISSING,
                tui.RunState.REVIEW_INCOMPLETE,
                tui.RunState.REVIEW_UNPARSABLE,
            ), start=50):
                ident = tui.RunIdentity(
                    repository="projectbluefin/review",
                    pull_request=pr_num,
                    base_sha="a" * 40,
                    head_sha="b" * 40,
                    backend="goose",
                    model="gemini-3.8-flash",
                    effort="high",
                    check_scope_version="image-v1",
                )
                store.create(ident)
                store.transition(ident, tui.RunState.REVIEWING)
                rec = store.transition(ident, fail_state, reason=f"failed {fail_state.value}")
                self.assertTrue(rec.is_terminal)

                # Retry transition back to REVIEWING must succeed
                retry_rec = store.retry_review(ident)
                self.assertEqual(retry_rec.state, tui.RunState.REVIEWING)
                self.assertIsNone(retry_rec.terminal_outcome)

            # Test structural mutation/human_review terminal outcomes remain blocked
            structural_ident = tui.RunIdentity(
                repository="projectbluefin/review",
                pull_request=99,
                base_sha="a" * 40,
                head_sha="b" * 40,
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            store.create(structural_ident)
            store.transition(structural_ident, tui.RunState.REVIEWING)
            store.transition(structural_ident, tui.RunState.REVIEW_CLEAN)
            store.transition(structural_ident, tui.RunState.MUTATING, low_risk=True)
            store.transition(structural_ident, tui.RunState.HUMAN_REVIEW_MISSING, reason="no human review")

            with self.assertRaises(tui.IllegalRunTransition):
                store.transition(structural_ident, tui.RunState.REVIEWING)
            with self.assertRaises(tui.IllegalRunTransition):
                store.retry_review(structural_ident)
        finally:
            if store_dir.exists():
                shutil.rmtree(store_dir, ignore_errors=True)

    def test_partial_source_failure_health_and_hive_projection_reconciliation(self) -> None:
        """Finding 4: Partial PR/issue source failure states degraded source health without
        suppressing usable rows. Failed Hive queue/triage prevents reconciliation from claiming fresh.
        """
        app = tui.ReviewDashboard()
        app.view_mode = "mixed"
        app.pr_source_state = "error"
        app.pr_source_message = "GraphQL 500 error"
        app.issues_source_state = "ready"
        app.issues_source_message = ""
        app.issues_items = [
            {"repository": "projectbluefin/review", "number": 42, "action": "triage", "title": "Issue 42", "is_issue": True}
        ]
        app._sync_source_state()
        app.apply_filters()

        # Degraded source health must be stated
        self.assertEqual(app.source_state, "degraded")
        self.assertIn("PR", app.source_message)
        # Usable issue rows must not be suppressed
        self.assertEqual(len(app.stops), 1)
        self.assertEqual(app.stops[0].key, "projectbluefin/review#42")

        # Hive queue/triage failure must prevent reconciliation claiming fresh
        app._reconciliation_request = 1
        app._reconciliation_source_attempts["hive"] = 1
        app._reconciliation_waiting = {"hive"}
        app.render_context = lambda s: None

        app.hive_loaded(
            state="online · 10 actionable",
            workers=[],
            ready_items=[],
            triage_groups=[],
            ready_ok=False,
            triage_ok=False,
        )
        # Queue projection must be marked unavailable
        self.assertTrue(app.hive_queue_unavailable)
        # Reconciliation state must NOT be fresh when Hive projection fails
        app._reconciliation_finished("hive", request=1, attempt=1, success=False)
        self.assertNotEqual(app.reconciliation_state, "fresh")

    def test_run_identity_strict_lowercase_hex_shas(self) -> None:
        """Finding 5: Require strict full lowercase hex SHAs before deriving run identity.
        Invalid values must raise ValueError, not share sentinel identities.
        """
        app = tui.ReviewDashboard()
        stop_bad_base = tui.Stop(
            repository="projectbluefin/review",
            number=1,
            action="review",
            title="PR 1",
            is_issue=False,
            live={"baseRefOid": "bad-base-sha", "headRefOid": "b" * 40},
        )
        with self.assertRaises(ValueError):
            app.run_identity(stop_bad_base)

        stop_bad_head = tui.Stop(
            repository="projectbluefin/review",
            number=1,
            action="review",
            title="PR 1",
            is_issue=False,
            live={"baseRefOid": "a" * 40, "headRefOid": "NOT-HEX" + "0" * 33},
        )
        with self.assertRaises(ValueError):
            app.run_identity(stop_bad_head)

        # stop_blocked_reason must not consult run_store for invalid SHAs
        self.assertIsNone(app.stop_blocked_reason(stop_bad_head))

    def test_sort_stops_queue_state_rank_precedes_action_and_repo_order(self) -> None:
        """Finding 6: _sort_stops must use QUEUE_STATE_RANK before action/repo ordering.
        Test with a fixture where lifecycle state conflicts with action/repo ordering.
        """
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        # Stop A has inferior action (triage, 5) and repo ("zzz/repo"), but superior lifecycle state (failed, 0)
        stop_a = tui.Stop(
            repository="zzz/repo",
            number=99,
            action="triage",
            title="Triage Failed",
            failure="mutation failed: error",
            is_issue=True,
        )
        # Stop B has superior action (ready-for-human-merge, 0) and repo ("aaa/repo"), but inferior lifecycle state (ready, 3)
        stop_b = tui.Stop(
            repository="aaa/repo",
            number=1,
            action="ready-for-human-merge",
            title="Merge Ready",
            is_issue=False,
        )

        app.stops = [stop_b, stop_a]
        app._sort_stops()

        # Stop A (failed lifecycle) MUST sort ahead of Stop B (ready lifecycle)
        self.assertEqual(app.stops[0].key, stop_a.key)
        self.assertEqual(app.stops[1].key, stop_b.key)

    def test_queue_state_rank_ready_precedes_done_and_done_precedes_blocked(self) -> None:
        """Queue sorting must rank ready work before completed (done) work, and done before blocked."""
        app = tui.ReviewDashboard()
        app.self_login = "tester"

        # Done PR with repository that would sort first alphabetically
        stop_done = tui.Stop(
            repository="aaa/repo",
            number=1,
            action="review",
            title="Done PR",
            review_result=tui.ReviewResult(1, "complete", {"critical": 0, "high": 0, "medium": 0, "low": 0}),
            is_issue=False,
            live={"baseRefOid": "a" * 40, "headRefOid": "1" * 40},
        )
        # Ready PR with repository that would sort second alphabetically
        stop_ready = tui.Stop(
            repository="mmm/repo",
            number=1,
            action="review",
            title="Ready PR",
            is_issue=False,
            live={"baseRefOid": "a" * 40, "headRefOid": "2" * 40},
        )
        # Blocked PR (draft) with repository that would sort first alphabetically
        stop_blocked = tui.Stop(
            repository="000/repo",
            number=1,
            action="review",
            title="Blocked draft PR",
            is_issue=False,
            live={"isDraft": True, "baseRefOid": "a" * 40, "headRefOid": "3" * 40},
        )

        app.stops = [stop_blocked, stop_done, stop_ready]
        app._sort_stops()

        # Expected order: ready (3) -> done (4) -> blocked (5)
        self.assertEqual(
            [s.key for s in app.stops],
            [stop_ready.key, stop_done.key, stop_blocked.key],
        )


if __name__ == "__main__":
    unittest.main()
