# tests/slay_state_contract.py
"""Contract tests for slay ($) state machine and invariants (#409, #410, #414)."""

import atexit
import glob
import os
import shutil
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

_ORIGINAL_ENVIRONMENT = {
    key: os.environ.get(key)
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
}
_TEST_HOST_ROOT = Path(tempfile.mkdtemp(prefix="slay-state-host-"))
_TEST_ROOT = _TEST_HOST_ROOT / "test-root"
_TEST_ROOT.mkdir()
os.environ.update(
    {
        "HOME": str(_TEST_ROOT / "home"),
        "XDG_STATE_HOME": str(_TEST_ROOT / "state"),
        "XDG_CONFIG_HOME": str(_TEST_ROOT / "config"),
        "XDG_CACHE_HOME": str(_TEST_ROOT / "cache"),
    }
)


def _restore_environment() -> None:
    for key, value in _ORIGINAL_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TEST_HOST_ROOT, ignore_errors=True)


atexit.register(_restore_environment)

site_pkgs = glob.glob(str(Path(__file__).parents[1] / ".cache" / "tui-venv" / "lib" / "python*" / "site-packages"))
if site_pkgs:
    sys.path.insert(0, site_pkgs[0])

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.run_state import (
    IllegalRunTransition,
    RunIdentity,
    RunState,
    RunStateStore,
    TerminalOutcome,
)
import tui.bluefin_review_tui as tui


def _sha(char: str) -> str:
    return char * 40


def _identity(number: int, head: str | None = None) -> RunIdentity:
    return RunIdentity(
        repository="projectbluefin/review",
        pull_request=number,
        base_sha=_sha("a"),
        head_sha=head or f"{number:040x}"[-40:],
        backend="goose",
        model="gemini-3.8-flash",
        effort="high",
        check_scope_version="image-v1",
    )


class SlayStateMachineContractTests(unittest.TestCase):
    def _store_dir(self):
        scratch = _TEST_ROOT / "run-state-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=scratch)

    def test_untrustworthy_review_results_drive_terminal_states_and_forbid_mutation(self):
        """#409: missing, failed, incomplete, unparsable results are distinct & cannot mutate."""
        cases = {
            RunState.REVIEW_MISSING: TerminalOutcome.REVIEW_MISSING,
            RunState.REVIEW_FAILED: TerminalOutcome.REVIEW_FAILED,
            RunState.REVIEW_INCOMPLETE: TerminalOutcome.REVIEW_INCOMPLETE,
            RunState.REVIEW_UNPARSABLE: TerminalOutcome.REVIEW_UNPARSABLE,
        }
        with self._store_dir() as root:
            store = RunStateStore(root)
            seen_states = set()
            for offset, (state, outcome) in enumerate(cases.items(), start=1):
                identity = _identity(offset)
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)
                record = store.transition(identity, state, reason=f"untrusted: {state.value}")

                seen_states.add(record.state)
                self.assertEqual(record.terminal_outcome, outcome)
                self.assertTrue(record.is_terminal)
                self.assertFalse(record.may_mutate())
                self.assertFalse(store.may_mutate(identity))

                # Attempting to mutate must raise IllegalRunTransition
                with self.assertRaises(IllegalRunTransition):
                    store.transition(identity, RunState.MUTATING)

            self.assertEqual(seen_states, set(cases))

    def test_revalidate_head_abort_on_head_change(self):
        """#410: head changed between review and mutation aborts run with HEAD_CHANGED."""
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(410, _sha("1"))
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)

            # Revalidating unchanged head retains REVIEW_CLEAN
            same = store.revalidate_head(identity, _sha("1"))
            self.assertEqual(same.state, RunState.REVIEW_CLEAN)
            self.assertTrue(same.may_mutate())

            # Revalidating mutated head transitions to HEAD_CHANGED
            changed = store.revalidate_head(identity, _sha("2"))
            self.assertEqual(changed.state, RunState.HEAD_CHANGED)
            self.assertEqual(changed.terminal_outcome, TerminalOutcome.HEAD_CHANGED)
            self.assertTrue(changed.is_terminal)
            self.assertFalse(changed.may_mutate())

    def test_human_review_invariant_detection(self):
        """#414: landing gate recognizes human vs bot review evidence."""
        app = tui.ReviewDashboard()

        # No reviews
        self.assertFalse(app.has_human_review({}))
        self.assertFalse(app.has_human_review({"reviews": []}))

        # Bot reviews only
        bot_live = {
            "reviews": [
                {"author": {"login": "goose"}, "state": "APPROVED"},
                {"author": {"login": "github-actions[bot]"}, "state": "APPROVED"},
                {"author": {"login": "renovate-bot"}, "state": "COMMENTED"},
            ]
        }
        self.assertFalse(app.has_human_review(bot_live))

        # Dismissed reviews only
        dismissed_live = {
            "reviews": [
                {"author": {"login": "human-reviewer"}, "state": "DISMISSED"},
            ]
        }
        self.assertFalse(app.has_human_review(dismissed_live))

        # Real human review
        human_live = {
            "reviews": [
                {"author": {"login": "human-reviewer"}, "state": "APPROVED"},
            ]
        }
        self.assertTrue(app.has_human_review(human_live))

    def test_queue_sorting_and_n_binding_lack_my_review(self):
        """#414: queue sorting and n surface PRs lacking user review with no local state."""
        app = tui.ReviewDashboard()
        app.self_login = "jorge"

        stop_unreviewed = tui.Stop(
            repository="projectbluefin/review",
            number=1,
            action="review",
            title="PR lacking my review",
            live={"reviews": [{"author": {"login": "alice"}, "state": "APPROVED"}]},
        )
        stop_reviewed = tui.Stop(
            repository="projectbluefin/review",
            number=2,
            action="review",
            title="PR reviewed by me",
            live={"reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
        )

        self.assertTrue(app.stop_lacks_my_review(stop_unreviewed))
        self.assertFalse(app.stop_lacks_my_review(stop_reviewed))

        # Sort order: lacking my review (0) precedes already reviewed by me (1)
        stops = [stop_reviewed, stop_unreviewed]
        stops.sort(key=lambda s: (0 if app.stop_lacks_my_review(s) else 1, s.number))
        self.assertEqual(stops, [stop_unreviewed, stop_reviewed])

    def _setup_app(self, store_dir):
        app = tui.ReviewDashboard()
        app.self_login = "jorge"
        app.run_store = RunStateStore(store_dir)
        app.refresh_rows = lambda: None
        app.refresh_status = lambda: None
        app.notify = lambda *a, **kw: None
        app.drain_landings = lambda: None
        app.start_review_batch = lambda stops, **kw: None
        return app

    def test_fixer_advanced_head_distinguished_from_foreign_head_change(self):
        """#411: head moved by our fixer triggers fresh review; foreign change aborts."""
        with self._store_dir() as root:
            app = self._setup_app(root)

            # Foreign head change aborts
            stop_foreign = tui.Stop(
                repository="projectbluefin/review",
                number=410,
                action="review",
                title="feat: foreign change",
                live={"headRefOid": _sha("1"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_foreign.head_sha = _sha("1")
            id_foreign = _identity(410, _sha("1"))
            app.run_store.create(id_foreign)
            app.run_store.transition(id_foreign, RunState.REVIEWING)
            app.run_store.transition(id_foreign, RunState.REVIEW_CLEAN)

            # Live PR moved to sha("2")
            app.fetch_live_pr = lambda repo, num, force=False: {"headRefOid": _sha("2"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
            app._dispatch_slay_landing(stop_foreign, identity=id_foreign)

            rec_foreign = app.run_store.get(id_foreign)
            self.assertEqual(rec_foreign.state, RunState.HEAD_CHANGED)

            # Fixer-advanced head triggers fresh review bound to new head
            stop_fixer = tui.Stop(
                repository="projectbluefin/review",
                number=411,
                action="review",
                title="feat: fixer change",
                live={"headRefOid": _sha("1"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_fixer.head_sha = _sha("1")
            id_fixer = _identity(411, _sha("1"))
            app.run_store.create(id_fixer)
            app.run_store.transition(id_fixer, RunState.REVIEWING)
            app.run_store.transition(id_fixer, RunState.REVIEW_FINDINGS)

            # Record that fixer pushed sha("3")
            app.record_fixer_head(stop_fixer.repository, stop_fixer.number, _sha("3"))
            self.assertTrue(app.is_fixer_head(stop_fixer.repository, stop_fixer.number, _sha("3")))
            self.assertFalse(app.is_fixer_head(stop_fixer.repository, stop_fixer.number, _sha("4")))

            app.fetch_live_pr = lambda repo, num, force=False: {"headRefOid": _sha("3"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
            app._dispatch_slay_landing(stop_fixer, identity=id_fixer)

            # Old record reached ESCALATION_REQUIRED (not HEAD_CHANGED)
            rec_old = app.run_store.get(id_fixer)
            self.assertEqual(rec_old.state, RunState.ESCALATION_REQUIRED)

            # New identity exists at sha("3") with high-assurance profile
            esc_profile = app.escalation_profile(stop_fixer)
            new_id = app.run_identity(stop_fixer, head_sha=_sha("3"), model=esc_profile[1], effort=esc_profile[2])
            rec_new = app.run_store.get(new_id)
            self.assertIsNotNone(rec_new)
            self.assertEqual(rec_new.identity.head_sha, _sha("3"))
            self.assertEqual(rec_new.identity.model, esc_profile[1])
            self.assertIn(rec_new.state, (RunState.REVIEWING, RunState.RE_REVIEWING))

    def test_cheap_clean_verdict_refused_without_escalation_unless_low_risk(self):
        """#411: cheap model clean verdict cannot authorise merge on its own."""
        with self._store_dir() as root:
            app = self._setup_app(root)

            # Case 1: non-low-risk PR with cheap clean review -> escalation triggered, no landing task
            stop_feat = tui.Stop(
                repository="projectbluefin/review",
                number=501,
                action="review",
                title="feat: add widget",
                live={"headRefOid": _sha("5"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_feat.head_sha = _sha("5")
            stop_feat.review_status = "complete"
            id_cheap = RunIdentity(
                repository="projectbluefin/review",
                pull_request=501,
                base_sha=_sha("a"),
                head_sha=_sha("5"),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            app.run_store.create(id_cheap)
            app.run_store.transition(id_cheap, RunState.REVIEWING)
            app.run_store.transition(id_cheap, RunState.REVIEW_CLEAN)

            app.fetch_live_pr = lambda repo, num, force=False: {"headRefOid": _sha("5"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
            init_landings = len(app.landing_queue)
            app._dispatch_slay_landing(stop_feat, identity=id_cheap)

            self.assertEqual(len(app.landing_queue), init_landings)
            self.assertEqual(app.run_store.get(id_cheap).state, RunState.ESCALATION_REQUIRED)

            # Case 2: low-risk PR with cheap clean review -> escalation skipped, landing task dispatched
            stop_dep = tui.Stop(
                repository="projectbluefin/review",
                number=502,
                action="review",
                title="chore(deps): update foo",
                live={"headRefOid": _sha("6"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_dep.head_sha = _sha("6")
            stop_dep.review_status = "complete"
            id_dep = RunIdentity(
                repository="projectbluefin/review",
                pull_request=502,
                base_sha=_sha("a"),
                head_sha=_sha("6"),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            app.run_store.create(id_dep)
            app.run_store.transition(id_dep, RunState.REVIEWING)
            app.run_store.transition(id_dep, RunState.REVIEW_CLEAN)

            app.fetch_live_pr = lambda repo, num, force=False: {"headRefOid": _sha("6"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
            init_landings = len(app.landing_queue)
            app._dispatch_slay_landing(stop_dep, identity=id_dep)

            self.assertEqual(len(app.landing_queue), init_landings + 1)
            self.assertEqual(app.run_store.get(id_dep).state, RunState.MUTATING)

    def test_failed_landing_deselects_work_and_starts_one_recovery_review(self):
        """Failed terminal work remains visible but needs explicit re-selection."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            failed = tui.Stop(
                repository="projectbluefin/review",
                number=420,
                action="review",
                title="fix: failed landing",
                selected=True,
            )
            blocked = tui.Stop(
                repository="projectbluefin/review",
                number=421,
                action="review",
                title="fix: blocked landing",
                selected=True,
            )
            status_path = Path(root) / "failed-landing.jsonl"
            status_path.write_text(
                '{"pr":"projectbluefin/review#420","state":"failed",'
                '"note":"OAuth workflow scope is missing"}\n'
                '{"pr":"projectbluefin/review#421","state":"blocked",'
                '"note":"required check is failing"}\n'
                '{"state":"done","note":"terminal outcomes recorded"}\n'
            )
            task = tui.landing.LandingTask(
                task_id="failed-landing",
                stops=[failed, blocked],
                login="jorge",
                status_path=str(status_path),
                log_path=str(Path(root) / "failed-landing.log"),
                started=0.0,
            )
            app.advance_final_review = lambda _task: None

            app.landing_finished(task)

            self.assertFalse(failed.selected)
            self.assertEqual(failed.failure, "failed: OAuth workflow scope is missing")
            self.assertFalse(blocked.selected)
            self.assertEqual(blocked.failure, "blocked: required check is failing")

            dispatched = []
            app.enqueue_landing = dispatched.append
            with mock.patch.object(tui.landing, "landing_state_dir", return_value=root):
                tui.ReviewDashboard.advance_final_review(app, task)

            self.assertEqual(len(dispatched), 1)
            recovery = dispatched[0]
            self.assertEqual(recovery.phase, "final-review")
            prompt = Path(recovery.prompt_path).read_text()
            self.assertIn("Terminal landing outcomes needing maintainer recovery:", prompt)
            self.assertIn(
                'projectbluefin/review#420 — failed: "OAuth workflow scope is missing"',
                prompt,
            )
            self.assertIn(
                'projectbluefin/review#421 — blocked: "required check is failing"',
                prompt,
            )

    def test_landing_failure_reason_is_bounded_when_finished_or_restored(self):
        """Landing notes cannot make a failed queue row unbounded."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            note = "x" * 300
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=422,
                action="review",
                title="fix: long failed landing",
                selected=True,
            )
            status_path = Path(root) / "long-failure.jsonl"
            status_path.write_text(
                f'{{"pr":"{stop.key}","state":"failed","note":"{note}"}}\n'
                '{"state":"done","note":"terminal outcome recorded"}\n'
            )
            task = tui.landing.LandingTask(
                task_id="long-failure",
                stops=[stop],
                login="jorge",
                status_path=str(status_path),
                started=0.0,
            )
            app.advance_final_review = lambda _task: None

            app.landing_finished(task)

            self.assertEqual(stop.failure, f"failed: {'x' * 240}")

            restored = tui.Stop(
                repository="projectbluefin/review",
                number=422,
                action="review",
                title="fix: restored failed landing",
            )
            with mock.patch.object(
                tui.landing,
                "persisted_events",
                return_value={stop.key: {"state": "failed", "note": note}},
            ):
                app.restore_landing_marks([restored])

            self.assertEqual(restored.failure, f"failed: {'x' * 240}")

    def test_dispatch_slay_landing_stays_inside_private_state_roots(self):
        """#424: the real slay dispatch must not use maintainer state."""
        sentinel = _TEST_HOST_ROOT / "sentinel.txt"
        sentinel.write_text("untouched\n", encoding="utf-8")
        app = self._setup_app(_TEST_ROOT / "run-state-dispatch")
        app.gh_client.read = mock.Mock(
            return_value=mock.Mock(returncode=0, stdout="true\n")
        )
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=424,
            action="review",
            title="chore(deps): test isolation",
            live={
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("1"),
                "reviews": [
                    {"author": {"login": "human"}, "state": "APPROVED"}
                ],
            },
        )
        stop.head_sha = _sha("1")
        identity = _identity(424, _sha("1"))
        app.run_store.create(identity)
        app.run_store.transition(identity, RunState.REVIEWING)
        app.run_store.transition(identity, RunState.REVIEW_CLEAN)
        app.fetch_live_pr = lambda *_args, **_kwargs: dict(stop.live)

        app._dispatch_slay_landing(stop, identity=identity)

        self.assertTrue(app.landing_queue)
        task = app.landing_queue[-1]
        self.assertTrue(
            Path(tui.TRACE_PATH).is_relative_to(_TEST_ROOT),
            f"captured trace path escaped test root: {tui.TRACE_PATH}",
        )
        for path in (task.prompt_path, task.status_path, task.log_path):
            self.assertTrue(
                Path(path).is_relative_to(_TEST_ROOT),
                f"landing artifact escaped test root: {path}",
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_landing_presentation_outcome_requires_terminal_report_evidence(self):
        """#394: process success cannot make an incomplete batch green."""
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=394,
            action="review",
            title="chore(deps): outcome contract",
        )
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            status_path = Path(root) / "outcome.jsonl"
            task = tui.landing.LandingTask(
                task_id="outcome",
                stops=[stop],
                login="tester",
                status_path=str(status_path),
                returncode=0,
            )

            status_path.write_text('{"expect":["projectbluefin/review#394"]}\n')
            self.assertEqual(
                tui.landing.batch_outcome(task).state,
                "incomplete",
            )

            status_path.write_text(
                '{"pr":"projectbluefin/review#394","state":"merged"}\n'
                '{"state":"done"}\n'
            )
            self.assertEqual(tui.landing.batch_outcome(task).state, "complete")

            status_path.write_text(
                '{"pr":"projectbluefin/review#394","state":"blocked"}\n'
                '{"state":"done"}\n'
            )
            self.assertEqual(
                tui.landing.batch_outcome(task).state,
                "completed-with-blockers",
            )

    def test_landing_presentation_keeps_active_terminal_states_distinct(self):
        """#394: running, waiting, queued, stopped, and failed stay readable."""
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=395,
            action="review",
            title="chore(deps): outcome states",
        )
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            status_path = Path(root) / "states.jsonl"
            task = tui.landing.LandingTask(
                task_id="states",
                stops=[stop],
                login="tester",
                status_path=str(status_path),
            )
            self.assertEqual(tui.landing.batch_outcome(task).state, "queued")
            task.process = object()
            status_path.write_text(
                '{"pr":"projectbluefin/review#395","state":"fixing"}\n'
            )
            self.assertEqual(tui.landing.batch_outcome(task).state, "running")
            status_path.write_text(
                '{"pr":"projectbluefin/review#395","state":"waiting-ci"}\n'
            )
            self.assertEqual(tui.landing.batch_outcome(task).state, "waiting")
            task.process = None
            task.returncode = -15
            task.stop_requested = True
            self.assertEqual(tui.landing.batch_outcome(task).state, "stopped")
            task.stop_requested = False
            task.returncode = 1
            self.assertEqual(tui.landing.batch_outcome(task).state, "failed")

    def test_malformed_progress_timestamps_remain_unknown(self):
        """#394/#425: malformed report timing cannot crash or fake freshness."""
        stop = tui.Stop(
            repository="projectbluefin/review",
            number=425,
            action="review",
            title="chore(deps): malformed progress",
        )
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            status_path = Path(root) / "malformed-progress.jsonl"
            status_path.write_text(
                '{"pr":"projectbluefin/review#425","state":"fixing","ts":"newest"}\n'
            )
            task = tui.landing.LandingTask(
                task_id="malformed-progress",
                stops=[stop],
                login="tester",
                status_path=str(status_path),
                started=1.0,
                process=object(),
            )
            progress = tui.landing.progress_snapshot(task)
            self.assertEqual(progress.evidence_age, "unknown")

    def test_ci_failure_evidence_keeps_exact_run_attempt_step_and_annotations(self):
        """#186: CI diagnosis is tied to the current head and real run fields."""
        live = {
            "headRefOid": _sha("6"),
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "unit tests",
                    "workflowName": "CI",
                    "conclusion": "FAILURE",
                    "databaseId": 186,
                    "checkSuite": {"workflowRun": {"runAttempt": 2}},
                    "stepName": "test dashboard",
                    "annotations": [
                        {"path": "tests/dashboard.py", "start_line": 12, "message": "failed"}
                    ],
                    "startedAt": "2026-09-08T00:00:00Z",
                    "completedAt": "2026-09-08T00:01:00Z",
                    "detailsUrl": "https://github.example/run/186",
                }
            ],
        }
        evidence = tui.ci_failure_evidence(live)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["run_id"], 186)
        self.assertEqual(evidence[0]["attempt"], 2)
        self.assertEqual(evidence[0]["head_sha"], _sha("6"))
        self.assertEqual(evidence[0]["step"], "test dashboard")
        self.assertEqual(evidence[0]["annotations"][0]["path"], "tests/dashboard.py")

    def test_ci_log_sanitization_bounds_secrets_and_terminal_controls(self):
        """#186: on-demand logs are bounded, untrusted, redacted text."""
        raw = "secret ghp_" + "a" * 40 + "\x1b]8;;https://evil.example\x07click\x1b]8;;\x07\n" + ("x" * 100)
        state, lines = tui.sanitize_ci_log(raw, max_lines=2, max_bytes=80)
        self.assertEqual(state, "available")
        self.assertLessEqual(len("\n".join(lines).encode()), 80)
        self.assertNotIn("ghp_" + "a" * 40, "\n".join(lines))
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_review_action_comparison_is_bounded_and_evidence_scoped(self):
        """#154: only exact-head review/action pairs receive a coarse receipt."""
        result = tui.ReviewResult(
            1,
            "findings",
            counts={"critical": 0, "high": 1, "medium": 0, "low": 0},
            findings=[{"severity": "high", "file": "a.py", "line": 1, "title": "risk"}],
            provenance={
                "backend": "goose",
                "model": "gpt-5.6-sol",
                "repository": "projectbluefin/review",
                "pull_request": 154,
                "head_sha": _sha("7"),
            },
        )
        receipt = tui.classify_review_action(
            result,
            "request-changes",
            repository="projectbluefin/review",
            number=154,
            head_sha=_sha("7"),
            actor="maintainer",
        )
        self.assertEqual(receipt.classification, "agreement")
        self.assertEqual(receipt.finding_count, 1)
        self.assertEqual(receipt.identity, "maintainer")
        self.assertEqual(
            tui.classify_review_action(
                result,
                "approve",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "disagreement",
        )
        self.assertEqual(
            tui.classify_review_action(
                result,
                "merge",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "unclassified",
        )

    def test_generation_tokens_reject_late_editor_results(self):
        """#196: cancellation, replacement, and edits invalidate a draft."""
        self.assertTrue(tui.generation_is_current(4, 2, 4, 2, False))
        self.assertFalse(tui.generation_is_current(4, 2, 5, 2, False))
        self.assertFalse(tui.generation_is_current(4, 2, 4, 3, False))
        self.assertFalse(tui.generation_is_current(4, 2, 4, 2, True))
        self.assertEqual(
            tui.classify_review_action(
                result,
                "request-changes",
                repository="projectbluefin/other",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "unclassified",
        )
        self.assertEqual(
            tui.classify_review_action(
                result,
                "request-changes",
                repository="projectbluefin/review",
                number=155,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "unclassified",
        )
        missing_identity = tui.ReviewResult(
            result.version,
            result.state,
            result.counts,
            result.findings,
            provenance={"backend": "goose", "model": "gpt-5.6-sol", "head_sha": _sha("7")},
        )
        self.assertEqual(
            tui.classify_review_action(
                missing_identity,
                "request-changes",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "unclassified",
        )
        self.assertEqual(
            tui.classify_review_action(
                result,
                "reject",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
            ).classification,
            "agreement",
        )
        self.assertEqual(
            tui.classify_review_action(
                result,
                "merge",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("7"),
                actor="maintainer",
                action_verified=True,
            ).classification,
            "disagreement",
        )
        self.assertEqual(
            tui.classify_review_action(
                result,
                "request-changes",
                repository="projectbluefin/review",
                number=154,
                head_sha=_sha("8"),
                actor="maintainer",
            ).classification,
            "unclassified",
        )

    def test_landing_task_uses_argv_cwd_and_delimits_untrusted_prompt_data(self):
        """#383: hostile titles and paths must remain data in generated work."""
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=383,
                action="review",
                title='title "quoted"\n$HOME ${{ secrets.TOKEN }}',
            )
            with mock.patch.dict(
                os.environ,
                {"BLUEFIN_REVIEW_LANDING_COMMAND": "runner --prompt @PROMPT"},
                clear=False,
            ):
                task = tui.landing.new_fix_task(
                    stop,
                    [{"title": 'finding "quoted"\n$(touch pwned)', "file": "a b.py"}],
                    "tester",
                    root=root,
                )

            self.assertEqual(task.command, ["runner", "--prompt", task.prompt_path])
            self.assertEqual(task.cwd, os.getcwd())
            prompt = Path(task.prompt_path).read_text(encoding="utf-8")
            self.assertIn("untrusted data", prompt.lower())
            self.assertIn('$(touch pwned)', prompt)
            self.assertIn("${{ secrets.TOKEN }}", prompt)
            self.assertNotIn("sh -c", prompt)

    def test_watch_target_requires_exact_head_and_terminal_reruns_only(self):
        """#382: active runs resume by identity and cannot be rerun."""
        target = tui.landing.WatchTarget.from_dict(
            {
                "repository": "projectbluefin/review",
                "pull_request": 382,
                "head_sha": _sha("3"),
                "run_id": 123,
                "attempt": 2,
                "status": "in_progress",
                "observed_at": 100,
                "deadline": 400,
            }
        )
        self.assertTrue(
            target.matches(
                repository="projectbluefin/review",
                pull_request=382,
                head_sha=_sha("3"),
                run_id=123,
                attempt=2,
            )
        )
        self.assertFalse(target.matches(head_sha=_sha("4")))
        self.assertFalse(target.can_rerun)
        self.assertTrue(target.expired(now=401))

        completed = tui.landing.WatchTarget.from_dict(
            {**target.to_dict(), "status": "completed"}
        )
        self.assertTrue(completed.can_rerun)

    def test_watch_report_persists_target_without_replacing_landing_state(self):
        """#382: the reporter retains the exact run being watched."""
        target = tui.landing.WatchTarget(
            "projectbluefin/review",
            382,
            _sha("5"),
            321,
            1,
            "in_progress",
            100.0,
            400.0,
        )
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            status_path = Path(root) / "watch.jsonl"
            status_path.write_text(
                '{"expect":["projectbluefin/review#382"],"ts":1}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                tui.landing.report_watch(
                    str(status_path), target, "CI is still running"
                ),
                0,
            )
            event = tui.landing.parse_status(str(status_path))["projectbluefin/review#382"]
            self.assertEqual(tui.landing.watch_target(event), target)
            self.assertEqual(event["state"], "waiting-ci")

    def test_log_tail_bounds_file_reading_before_decoding(self):
        """#196: the landing view must not read an entire unbounded log."""
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            path = Path(root) / "large.log"
            path.write_bytes((b"old output\n" * 20_000) + b"tail marker\n")
            lines = tui.landing.read_log_tail(str(path), max_lines=3, max_bytes=128)
            self.assertEqual(lines[-1], "tail marker")
            self.assertLessEqual(len(lines), 3)
            self.assertTrue(all(len(line) < 128 for line in lines))

    def test_late_reconciliation_callback_cannot_overwrite_fresh_source(self):
        """A cancelled worker cannot stale a source settled by its replacement."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._reconciliation_request = 1
            app._reconciliation_waiting = {"hive"}
            app._reconciliation_success = {"queue": True, "hive": False}
            app._reconciliation_source_attempts = {"queue": 1, "hive": 2}
            app.reconciliation_state = "refreshing"

            app._reconciliation_finished("hive", 1, 2, True)
            self.assertEqual(app.reconciliation_state, "fresh")

            app._reconciliation_finished("hive", 1, 1, False)
            self.assertEqual(app.reconciliation_state, "fresh")


if __name__ == "__main__":
    unittest.main()
