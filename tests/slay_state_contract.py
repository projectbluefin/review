# tests/slay_state_contract.py
"""Contract tests for slay ($) state machine and invariants (#409, #410, #414)."""

import atexit
import glob
import json
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
        backend="omp",
        model="gemini-3.8-flash",
        effort="high",
        check_scope_version="image-v1",
    )


class SlayStateMachineContractTests(unittest.TestCase):
    def setUp(self):
        # Landing task creation writes prompt and status records through
        # landing_state_dir(). Unpatched, that is the maintainer's real
        # ${XDG_STATE_HOME}/bluefin-review/landings/ — this suite once littered
        # it with records for fixture pull requests that do not exist.
        scratch = _TEST_ROOT / "slay-state-contract"
        scratch.mkdir(parents=True, exist_ok=True)
        self._landing_dir = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self._landing_dir.cleanup)
        patcher = mock.patch.object(
            tui.landing, "landing_state_dir", return_value=self._landing_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The dispatch preflight refuses the real headless-omp command when
        # no Copilot credential is present. These tests never run the agent
        # (drain_landings is stubbed), so they opt out with the same override
        # every stubbed-agent test uses.
        env = mock.patch.dict(
            os.environ, {"BLUEFIN_REVIEW_LANDING_COMMAND": "/bin/true @PROMPT"}
        )
        env.start()
        self.addCleanup(env.stop)

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
                {"author": {"login": "ci-runner[bot]"}, "state": "APPROVED"},
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
        app.refresh_status = lambda *a, **kw: None
        app.notify = lambda *a, **kw: None
        app.drain_landings = lambda: None
        app.start_review_batch = lambda stops, **kw: None
        app._request_reconciliation = lambda: None
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
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("1"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_foreign.head_sha = _sha("1")
            id_foreign = _identity(410, _sha("1"))
            app.run_store.create(id_foreign)
            app.run_store.transition(id_foreign, RunState.REVIEWING)
            app.run_store.transition(id_foreign, RunState.REVIEW_CLEAN)

            # Live PR moved to sha("2")
            app.fetch_live_pr = lambda repo, num, force=False: {"baseRefOid": _sha("a"), "headRefOid": _sha("2"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
            app._dispatch_slay_landing(stop_foreign, identity=id_foreign)

            rec_foreign = app.run_store.get(id_foreign)
            self.assertEqual(rec_foreign.state, RunState.HEAD_CHANGED)

            # Fixer-advanced head triggers fresh review bound to new head
            stop_fixer = tui.Stop(
                repository="projectbluefin/review",
                number=411,
                action="review",
                title="feat: fixer change",
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("1"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_fixer.head_sha = _sha("1")
            id_fixer = _identity(411, _sha("1"))
            app.run_store.create(id_fixer)
            app.run_store.transition(id_fixer, RunState.REVIEWING)
            app.run_store.transition(id_fixer, RunState.REVIEW_FINDINGS)

            # Simulate fix-and-land completion with fixer reporting pushed head sha("3")
            status_path = Path(root) / "fix-landing.jsonl"
            status_path.write_text(
                f'{{"pr":"{stop_fixer.key}","state":"waiting-ci","head":"{_sha("3")}","note":"pushed fix"}}\n'
                '{"state":"done","note":"fix completed"}\n'
            )
            task = tui.landing.LandingTask(
                task_id="fix-task",
                stops=[stop_fixer],
                login="jorge",
                status_path=str(status_path),
                log_path=str(Path(root) / "fix-task.log"),
                started=0.0,
            )
            app.advance_final_review = lambda _task: None
            app.landing_finished(task)

            self.assertTrue(app.is_fixer_head(stop_fixer.repository, stop_fixer.number, _sha("3")))
            self.assertFalse(app.is_fixer_head(stop_fixer.repository, stop_fixer.number, _sha("4")))

            app.fetch_live_pr = lambda repo, num, force=False: {"baseRefOid": _sha("a"), "headRefOid": _sha("3"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
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
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("5"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_feat.head_sha = _sha("5")
            stop_feat.review_status = "complete"
            id_cheap = RunIdentity(
                repository="projectbluefin/review",
                pull_request=501,
                base_sha=_sha("a"),
                head_sha=_sha("5"),
                backend="omp",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            app.run_store.create(id_cheap)
            app.run_store.transition(id_cheap, RunState.REVIEWING)
            app.run_store.transition(id_cheap, RunState.REVIEW_CLEAN)

            app.fetch_live_pr = lambda repo, num, force=False: {"baseRefOid": _sha("a"), "headRefOid": _sha("5"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
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
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("6"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]},
            )
            stop_dep.head_sha = _sha("6")
            stop_dep.review_status = "complete"
            id_dep = RunIdentity(
                repository="projectbluefin/review",
                pull_request=502,
                base_sha=_sha("a"),
                head_sha=_sha("6"),
                backend="omp",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="image-v1",
            )
            app.run_store.create(id_dep)
            app.run_store.transition(id_dep, RunState.REVIEWING)
            app.run_store.transition(id_dep, RunState.REVIEW_CLEAN)

            app.fetch_live_pr = lambda repo, num, force=False: {"baseRefOid": _sha("a"), "headRefOid": _sha("6"), "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
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
                "backend": "omp",
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
            provenance={"backend": "omp", "model": "gpt-5.6-sol", "head_sha": _sha("7")},
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

    def test_generation_tokens_reject_late_editor_results(self):
        """#196: cancellation, replacement, and edits invalidate a draft."""
        self.assertTrue(tui.generation_is_current(4, 2, 4, 2, False))
        self.assertFalse(tui.generation_is_current(4, 2, 5, 2, False))
        self.assertFalse(tui.generation_is_current(4, 2, 4, 3, False))
        self.assertFalse(tui.generation_is_current(4, 2, 4, 2, True))

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

    def test_reporter_keeps_newline_notes_in_one_jsonl_record(self):
        """#383: newline data is encoded, not evaluated or rejected."""
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as root:
            status_path = Path(root) / "newline.jsonl"
            self.assertEqual(
                tui.landing.report_event(
                    str(status_path),
                    "projectbluefin/review#383",
                    "failed",
                    "first line\nsecond line",
                ),
                0,
            )
            lines = status_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(
                json.loads(lines[0])["note"], "first line\nsecond line"
            )

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

    def test_apply_review_event_handles_empty_live_state_safely(self):
        """When async evidence has replaced stop.live with {}, apply_review_event falls back safely."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=999,
                action="review",
                title="feat: pr with empty live state",
                live={},
            )
            app.stops = [stop]
            event = tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean review",
                timestamp=1700000000,
            )
            app.apply_review_event(event)
            self.assertEqual(stop.review_status, "complete")

    def test_apply_review_event_receipt_with_empty_live_base_sha_preserves_semantics(self):
        """A normal receipt-bearing completion event succeeds when async evidence removed baseRefOid."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            head_sha = _sha("1")
            base_sha = _sha("a")
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=999,
                action="review",
                title="feat: pr with empty live base SHA",
                head_sha=head_sha,
                live={"baseRefOid": base_sha, "headRefOid": head_sha},
            )
            app.stops = [stop]
            ident = app.run_identity(stop)
            app.active_review_identities[stop.key] = ident
            app.run_store.create(ident)
            app.run_store.transition(ident, RunState.REVIEWING)

            # Async evidence now removes baseRefOid from live
            stop.live["baseRefOid"] = ""

            run = tui.ReviewRun(
                stop.repository,
                stop.number,
                base_sha,
                head_sha,
                base_sha[:12] + head_sha[:12],
                "omp",
                "gemini-3.8-flash",
                "max",
            )
            receipt = tui.ReviewReceipt.from_result(
                run,
                tui.ReviewResult(
                    1,
                    "complete",
                    {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    [],
                    [],
                    {"backend": "omp", "model": "gemini-3.8-flash"},
                ),
                ["transcript"],
                app.review_scope_version,
            )
            receipt_path = app.review_cache.put(receipt)

            event = tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean review",
                timestamp=1700000000,
                receipt=receipt_path.name,
                head_sha=head_sha,
            )
            app.apply_review_event(event)

            self.assertEqual(stop.review_status, "complete")
            self.assertIsNotNone(stop.review_result)
            rec = app.run_store.get(ident)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.state, RunState.REVIEW_CLEAN)

    def test_apply_review_event_receipt_with_empty_live_base_sha_and_no_active_identity_safely_falls_back(self):
        """When both live base SHA and active identity are unavailable, receipt processing does not crash."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            head_sha = _sha("1")
            base_sha = _sha("a")
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=998,
                action="review",
                title="feat: pr with empty live base SHA and no active identity",
                head_sha=head_sha,
                live={"baseRefOid": "", "headRefOid": head_sha},
            )
            app.stops = [stop]

            run = tui.ReviewRun(
                stop.repository,
                stop.number,
                base_sha,
                head_sha,
                base_sha[:12] + head_sha[:12],
                "omp",
                "gemini-3.8-flash",
                "max",
            )
            receipt = tui.ReviewReceipt.from_result(
                run,
                tui.ReviewResult(
                    1,
                    "complete",
                    {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    [],
                    [],
                    {"backend": "omp", "model": "gemini-3.8-flash"},
                ),
                ["transcript"],
                app.review_scope_version,
            )
            receipt_path = app.review_cache.put(receipt)

            event = tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean review",
                timestamp=1700000000,
                receipt=receipt_path.name,
                head_sha=head_sha,
            )
            app.apply_review_event(event)

            self.assertEqual(stop.review_status, "complete")
            self.assertIsNotNone(stop.review_result)

    def test_prior_clean_head_advancing_then_slay_cannot_land(self):
        """#410: prior clean review on head 1 advancing to head 2 on GitHub aborts slay without landing."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=600,
                action="review",
                title="feat: prior clean PR",
                live={
                    "baseRefOid": _sha("a"),
                    "headRefOid": _sha("1"),
                    "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop.head_sha = _sha("1")
            stop.review_status = "complete"

            id_old = app.run_identity(stop)
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)

            # Live PR has now advanced to sha("2") on GitHub
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("2"),
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            app._execute_slay([stop])

            # Landing must NOT be dispatched
            self.assertEqual(len(app.landing_queue), 0)
            # The new head must NOT have been marked REVIEW_CLEAN
            id_new = app.run_identity(stop, head_sha=_sha("2"))
            self.assertIsNone(app.run_store.get(id_new))
            # The old head record must reflect HEAD_CHANGED
            rec_old = app.run_store.get(id_old)
            self.assertIsNotNone(rec_old)
            self.assertEqual(rec_old.state, RunState.HEAD_CHANGED)
            self.assertIn("landing aborted: head changed", stop.failure)

    def test_head_changed_then_fresh_reviewed_identity_at_advanced_head_can_proceed(self):
        """HIGH: landing aborted: head changed on stop must not block slay when advanced head has fresh valid review."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            head1 = _sha("1")
            head2 = _sha("2")

            stop = tui.Stop(
                repository="projectbluefin/review",
                number=800,
                action="review",
                title="feat: head changed regression",
                live={
                    "baseRefOid": _sha("a"),
                    "headRefOid": head1,
                    "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop.head_sha = head1
            stop.review_status = "complete"

            id1 = app.run_identity(stop, head_sha=head1)
            app.run_store.create(id1)
            app.run_store.transition(id1, RunState.REVIEWING)
            app.run_store.transition(id1, RunState.REVIEW_CLEAN)

            # Live PR has advanced to head2 on GitHub
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": head2,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            # Slay first reaches HEAD_CHANGED
            app._execute_slay([stop])
            self.assertEqual(len(app.landing_queue), 0)
            rec1 = app.run_store.get(id1)
            self.assertIsNotNone(rec1)
            self.assertEqual(rec1.state, RunState.HEAD_CHANGED)
            self.assertIn("landing aborted: head changed", stop.failure)

            # Protection remains for current-head HEAD_CHANGED while pointing at head1
            self.assertEqual(
                tui.classify_routability(stop, record=rec1),
                "head changed between review and mutation",
            )

            # Now PR stop is updated to advanced head2, and supplies a fresh reviewed identity
            stop.head_sha = head2
            stop.live["headRefOid"] = head2
            stop.review_status = "complete"
            _, esc_model, esc_effort = app.escalation_profile(stop)
            stop.review_result = mock.MagicMock(findings=[], provenance={"head_sha": head2, "model": esc_model, "effort": esc_effort})
            # Crucially: stop.failure STILL contains the old failure string
            self.assertIn("landing aborted: head changed", stop.failure)

            id2 = app.run_identity(stop, head_sha=head2, model=esc_model, effort=esc_effort)
            rec2 = app.run_store.create(id2)
            app.run_store.transition(id2, RunState.REVIEWING)
            app.run_store.transition(id2, RunState.REVIEW_CLEAN)

            # Stop blocked reason and routability must NOT block the fresh reviewed head
            blocked_reason = app.stop_blocked_reason(stop)
            self.assertIsNone(blocked_reason)

            # Slay must proceed with the fresh reviewed head and enqueue landing
            app._execute_slay([stop])
            self.assertEqual(len(app.landing_queue), 1)
            self.assertEqual(app.run_store.get(id2).state, RunState.MUTATING)

    def test_refresh_rows_snapshots_run_state_once_per_repaint(self):
        """refresh_rows must not repeatedly read/parse the run_state file for each row and sub-render."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            # Create 10 stops
            stops = []
            for i in range(1, 11):
                h = f"{i:040x}"[-40:]
                stop = tui.Stop(
                    repository="projectbluefin/review",
                    number=i,
                    action="review",
                    title=f"PR {i}",
                    head_sha=h,
                    live={"baseRefOid": _sha("a"), "headRefOid": h},
                )
                stops.append(stop)
                ident = app.run_identity(stop)
                app.run_store.create(ident)

            app.stops = stops
            app.refresh_rows = lambda: tui.ReviewDashboard.refresh_rows(app)
            queue_mock = mock.MagicMock()
            queue_mock.children = []
            queue_mock.index = 0
            queue_mock.content_region.width = 80
            app.query_one = lambda id, *args, **kwargs: queue_mock if id == "#queue" else mock.MagicMock()

            load_count = 0
            original_load = app.run_store._load

            def counting_load(*args, **kwargs):
                nonlocal load_count
                load_count += 1
                return original_load(*args, **kwargs)

            app.run_store._load = counting_load
            app.refresh_rows()

            # Must load at most once (for the snapshot), NOT once per stop/render
            self.assertEqual(load_count, 1)

    def test_apply_filters_snapshots_run_state_once_across_sort_render_and_counters(self):
        """apply_filters must snapshot run_state once instead of 3N loads across sort, render, and counters."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            queue_items = []
            for i in range(1, 11):
                h = f"{i:040x}"[-40:]
                stop = tui.Stop(
                    repository="projectbluefin/review",
                    number=i,
                    action="review",
                    title=f"PR {i}",
                    head_sha=h,
                    live={"baseRefOid": _sha("a"), "headRefOid": h},
                )
                ident = app.run_identity(stop)
                app.run_store.create(ident)
                queue_items.append({
                    "repository": "projectbluefin/review",
                    "number": i,
                    "recommended_action": "review",
                    "title": f"PR {i}",
                    "author": "someone",
                    "mergeable_state": "clean",
                    "check_state": "success",
                    "review_state": "pending",
                    "base_sha": _sha("a"),
                    "head_sha": h,
                })

            app.queue_items = queue_items
            app.refresh_status = lambda *a, **kw: tui.ReviewDashboard.refresh_status(app, *a, **kw)
            queue_mock = mock.MagicMock()
            queue_mock.children = []
            queue_mock.index = 0
            queue_mock.content_region.width = 80
            app.query_one = lambda id, *args, **kwargs: queue_mock if id == "#queue" else mock.MagicMock()

            load_count = 0
            original_load = app.run_store._load

            def counting_load(*args, **kwargs):
                nonlocal load_count
                load_count += 1
                return original_load(*args, **kwargs)

            app.run_store._load = counting_load
            app.apply_filters()

            self.assertEqual(load_count, 1)

    def test_execute_slay_handles_terminal_head_changed_safely(self):
        """CRITICAL: _execute_slay must not re-transition terminal HEAD_CHANGED records or crash."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=410,
                action="review",
                title="PR 410",
                review_status="complete",
                review_result=mock.MagicMock(findings=[], provenance={"head_sha": _sha("1")}),
                head_sha=_sha("1"),
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("1")},
            )
            id_old = app.run_identity(stop, head_sha=_sha("1"))
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)
            app.run_store.revalidate_head(id_old, _sha("2"))
            self.assertEqual(app.run_store.get(id_old).state, RunState.HEAD_CHANGED)

            # Live PR is at sha("2") or moved again to sha("3")
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("3"),
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            notices: list[tuple[str, str]] = []
            app.notify = lambda message, severity="information": notices.append((message, severity))

            # Must not throw IllegalRunTransition
            app._execute_slay([stop])

            self.assertEqual(len(app.landing_queue), 0)
            self.assertEqual(app.run_store.get(id_old).state, RunState.HEAD_CHANGED)
            self.assertTrue(any("head changed" in m.lower() for m, _ in notices))

    def test_execute_slay_handles_terminal_human_review_missing_safely(self):
        """CRITICAL: _execute_slay must not re-transition terminal HUMAN_REVIEW_MISSING records or crash."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            stop = tui.Stop(
                repository="projectbluefin/review",
                number=414,
                action="review",
                title="PR 414",
                review_status="complete",
                review_result=mock.MagicMock(findings=[], provenance={"head_sha": _sha("1")}),
                head_sha=_sha("2"),
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("2")},
            )
            id_old = app.run_identity(stop, head_sha=_sha("1"))
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)
            app.run_store.transition(id_old, RunState.MUTATING, low_risk=True)
            app.run_store.transition(
                id_old,
                RunState.HUMAN_REVIEW_MISSING,
                reason="no human review on GitHub",
            )
            self.assertEqual(app.run_store.get(id_old).state, RunState.HUMAN_REVIEW_MISSING)

            # Live PR has now advanced to sha("2") on GitHub
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("2"),
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            notices: list[tuple[str, str]] = []
            app.notify = lambda message, severity="information": notices.append((message, severity))

            # Must not throw IllegalRunTransition
            app._execute_slay([stop])

            self.assertEqual(len(app.landing_queue), 0)
            self.assertEqual(app.run_store.get(id_old).state, RunState.HUMAN_REVIEW_MISSING)
            self.assertTrue(
                any("cannot mutate" in m.lower() or "human review" in m.lower() for m, _ in notices)
            )

    def test_retained_terminal_rows_cannot_reopen_slay_confirmation_gate(self):
        """CRITICAL: routability must block retained terminal rows so Slay confirmation gate does not reopen."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            # Case 1: Terminal HEAD_CHANGED record
            id_head_changed = _identity(501, _sha("1"))
            stop_head_changed = tui.Stop(
                repository="projectbluefin/review",
                number=501,
                action="review",
                title="PR 501",
                head_sha=_sha("1"),
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("1")},
                failure="landing aborted: head changed (reviewed 1111, live 2222)",
            )
            app.run_store.create(id_head_changed)
            app.run_store.transition(id_head_changed, RunState.REVIEWING)
            app.run_store.transition(id_head_changed, RunState.REVIEW_CLEAN)
            app.run_store.revalidate_head(id_head_changed, _sha("2"))

            reason = app.stop_blocked_reason(stop_head_changed)
            self.assertIsNotNone(reason)
            self.assertIn("head changed", reason.lower())

            # Verify action_slay_pr does NOT push SlayConfirmScreen
            screens_pushed = []
            app.push_screen = lambda scr, cb=None: screens_pushed.append(scr)
            app.stops = [stop_head_changed]
            type(app).current = property(lambda self: stop_head_changed)
            notices: list[tuple[str, str]] = []
            app.notify = lambda message, severity="information": notices.append((message, severity))

            app.action_slay_pr()
            self.assertEqual(len(screens_pushed), 0)
            self.assertTrue(any(s == "warning" for _, s in notices))

            # Case 2: Terminal HUMAN_REVIEW_MISSING record
            id_human = _identity(502, _sha("1"))
            stop_human = tui.Stop(
                repository="projectbluefin/common",
                number=502,
                action="review",
                title="PR 502",
                head_sha=_sha("1"),
                live={"baseRefOid": _sha("a"), "headRefOid": _sha("1")},
                failure="landing refused: no human review on GitHub",
            )
            app.run_store.create(id_human)
            app.run_store.transition(id_human, RunState.REVIEWING)
            app.run_store.transition(id_human, RunState.REVIEW_CLEAN)
            app.run_store.transition(id_human, RunState.MUTATING, low_risk=True)
            app.run_store.transition(
                id_human,
                RunState.HUMAN_REVIEW_MISSING,
                reason="no human review on GitHub",
            )

            reason_human = app.stop_blocked_reason(stop_human)
            self.assertIsNotNone(reason_human)
            self.assertTrue("human" in reason_human.lower() or "review" in reason_human.lower())

            screens_pushed.clear()
            notices.clear()
            app.stops = [stop_human]
            type(app).current = property(lambda self: stop_human)

            app.action_slay_pr()
            self.assertEqual(len(screens_pushed), 0)
            self.assertTrue(any(s == "warning" for _, s in notices))

    def test_repaint_avoids_ui_harness_probes_and_memoizes_profile_resolution(self):
        """HIGH: queue repaint must not invoke external harness probes on UI thread and memoizes profile resolution."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            # Create 10 stops
            stops = []
            for i in range(1, 11):
                h = f"{i:040x}"[-40:]
                stop = tui.Stop(
                    repository="projectbluefin/review",
                    number=i,
                    action="review",
                    title=f"PR {i}",
                    head_sha=h,
                    live={"baseRefOid": _sha("a"), "headRefOid": h},
                )
                stops.append(stop)

            app.stops = stops
            app.refresh_rows = lambda: tui.ReviewDashboard.refresh_rows(app)
            queue_mock = mock.MagicMock()
            queue_mock.children = []
            queue_mock.index = 0
            queue_mock.content_region.width = 80
            app.query_one = lambda id, *args, **kwargs: queue_mock if id == "#queue" else mock.MagicMock()

            discover_calls = 0
            preferences_calls = 0

            def fake_discover_all():
                nonlocal discover_calls
                discover_calls += 1
                return []

            def fake_load_preferences():
                nonlocal preferences_calls
                preferences_calls += 1
                return {}

            original_backend = tui.ACTIVE_BACKEND
            try:
                tui.ACTIVE_BACKEND = "codex"
                app.harness_options = []  # Background discovery not yet complete

                with mock.patch.object(tui, "discover_all", fake_discover_all), \
                     mock.patch.object(tui, "load_preferences", fake_load_preferences):
                    app.refresh_rows()

                # External harness discovery must NEVER be called on UI thread
                self.assertEqual(discover_calls, 0, "discover_all must not be called during queue repaint")
                # Preferences should be loaded at most once per repaint (snapshot), not per row/sub-render
                self.assertLessEqual(preferences_calls, 1, "load_preferences must be loaded at most once per repaint")
            finally:
                tui.ACTIVE_BACKEND = original_backend

    def test_harness_loaded_updates_profile_and_refreshes_rows(self):
        """HIGH: when asynchronous harness discovery completes, harness_loaded updates state accurately."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            original_backend = tui.ACTIVE_BACKEND
            try:
                tui.ACTIVE_BACKEND = "codex"
                app.harness_options = []

                from harness.autopilot import Availability, Discovery, HarnessOption
                from harness.codex import CodexHarness

                with mock.patch.object(tui, "load_preferences", return_value={}):
                    # Before discovery arrives, default fallback profile is returned without probes
                    prof_before = app.review_profile("projectbluefin/review")
                    self.assertEqual(prof_before, ("gemini-3.8-flash", "max"))

                    discovered_option = HarnessOption(
                        harness=CodexHarness(),
                        discovery=Discovery(
                            backend="codex",
                            installed="ok",
                            auth="ok",
                            capability="ok",
                            model="gpt-5.4",
                            reasoning="medium",
                            availability=Availability.READY,
                        ),
                    )

                    refresh_called = False
                    app.refresh_rows = lambda: None  # mock refresh_rows
                    static_mock = mock.MagicMock()
                    app.query_one = lambda *a, **kw: static_mock

                    app.harness_loaded([discovered_option])

                    self.assertEqual(app.harness_options, [discovered_option])
                    prof_after = app.review_profile("projectbluefin/review")
                    self.assertEqual(prof_after[0], "gpt-5.4")
                    self.assertEqual(prof_after[1], "medium")
            finally:
                tui.ACTIVE_BACKEND = original_backend

    def test_codex_discovery_comma_separated_effort_fallback_with_no_preference(self):
        """CRITICAL: discovery.reasoning ('low, medium, high, max') must fall back to a valid supported effort."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            original_backend = tui.ACTIVE_BACKEND
            try:
                tui.ACTIVE_BACKEND = "codex"
                app.harness_options = []

                from harness.autopilot import Availability, Discovery, HarnessOption, discover
                from harness.codex import CodexHarness
                from tui.review_evidence_manifest import ReviewRequest

                # Use the actual discovery reasoning produced by autopilot.discover()
                real_codex_discovery = discover()
                self.assertEqual(real_codex_discovery.reasoning, "low, medium, high, max")

                discovered_option = HarnessOption(
                    harness=CodexHarness(),
                    discovery=Discovery(
                        backend="codex",
                        installed="ready",
                        auth="ready",
                        capability="ready",
                        model="gemini-3.8-flash",
                        reasoning=real_codex_discovery.reasoning,
                        availability=Availability.READY,
                    ),
                )

                app.refresh_rows = lambda: None
                static_mock = mock.MagicMock()
                app.query_one = lambda *a, **kw: static_mock

                app.harness_loaded([discovered_option])

                with mock.patch.object(tui, "load_preferences", return_value={}):
                    model, effort = app.review_profile("projectbluefin/review")

                    # Must select one supported effort, preserving established default behavior ('low')
                    self.assertEqual(model, "gemini-3.8-flash")
                    self.assertEqual(effort, "low")
                    self.assertIn(effort, CodexHarness.SUPPORTED_EFFORTS)

                    # Flow into valid RunIdentity
                    stop = tui.Stop("projectbluefin/review", 42, "fix", "ready")
                    stop.live = {"baseRefOid": "a" * 40}
                    stop.head_sha = "b" * 40
                    ident = app.run_identity(stop)
                    self.assertEqual(ident.effort, "low")
                    self.assertIn(ident.effort, CodexHarness.SUPPORTED_EFFORTS)

                    # Flow into valid review dispatch semantics without ValueError
                    req = ReviewRequest(
                        "projectbluefin", "review", 42,
                        "a" * 40, "b" * 40, "maintainer", "review",
                        generated_at="test",
                    )
                    harness = CodexHarness()
                    command = harness.command(req, prompt="review", model=model, effort=effort)
                    self.assertIn("--config", command)
                    self.assertIn("model_reasoning_effort=low", command)

                # When a valid user preference is present, it is respected
                from harness.autopilot import Preference
                pref = Preference("codex", "gpt-5.6-luna", "high")
                with mock.patch.object(tui, "load_preferences", return_value={"projectbluefin/review": pref}):
                    p_model, p_effort = app.review_profile("projectbluefin/review")
                    self.assertEqual(p_model, "gpt-5.6-luna")
                    self.assertEqual(p_effort, "high")
                    self.assertIn(p_effort, CodexHarness.SUPPORTED_EFFORTS)
            finally:
                tui.ACTIVE_BACKEND = original_backend

    def test_execute_slay_fixer_advanced_escalation_dispatches_high_assurance_profile_and_completes(self):
        """CRITICAL: _execute_slay fixer-advanced escalation dispatches explicit high-assurance profile, retains active identity, and completes transition."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            app.watch_review_batch = lambda batch: None
            app.review_engine = mock.MagicMock()
            app.call_from_thread = lambda fn, *args, **kwargs: fn(*args, **kwargs)

            head1 = _sha("1")
            head2 = _sha("2")

            stop = tui.Stop(
                repository="projectbluefin/review",
                number=700,
                action="review",
                title="feat: fixer escalation test",
                head_sha=head1,
                review_status="complete",
                live={
                "baseRefOid": _sha("a"),
                "headRefOid": head1,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop.review_result = mock.MagicMock(findings=[], provenance={"head_sha": head1})

            id_old = app.run_identity(stop, head_sha=head1)
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)

            app.record_fixer_head(stop.repository, stop.number, head2)
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": head2,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            app.stops = [stop]
            dispatched: list[tuple[list[tui.Stop], dict]] = []

            from tui.review_snapshot import BatchSnapshot, BatchReviewItem
            def intercept_start_review_batch(stops, **kw):
                dispatched.append((list(stops), dict(kw)))
                snapshot = BatchSnapshot((
                BatchReviewItem(
                    stop.key,
                    stop.repository,
                    stop.number,
                    stop.title,
                    _sha("a"),
                    head2,
                    stop.live,
                    [],
                ),
                ), {})
                app.begin_review_batch(stops, snapshot, **kw)

            app.start_review_batch = intercept_start_review_batch
            app._execute_slay([stop])

            # 1. Assert dispatched args to start_review_batch
            esc_backend, esc_model, esc_effort = app.escalation_profile(stop)
            self.assertEqual(len(dispatched), 1)
            dispatched_stops, dispatched_kwargs = dispatched[0]
            self.assertEqual(dispatched_stops, [stop])
            self.assertEqual(dispatched_kwargs.get("model"), esc_model)
            self.assertEqual(dispatched_kwargs.get("effort"), esc_effort)

            # 2. Assert engine active identity in app matches high-assurance escalation profile
            active_id = app.active_review_identities.get(stop.key)
            self.assertIsNotNone(active_id)
            self.assertEqual(active_id.model, esc_model)
            self.assertEqual(active_id.effort, esc_effort)
            self.assertEqual(active_id.head_sha, head2)

            # 3. Assert eventual successful transition: clean review completion transitions RE_REVIEWING -> REVIEW_CLEAN -> MUTATING
            esc_id = app.run_identity(stop, head_sha=head2, model=esc_model, effort=esc_effort)
            rec_re = app.run_store.get(esc_id)
            self.assertIsNotNone(rec_re)
            self.assertEqual(rec_re.state, RunState.RE_REVIEWING)

            event = tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean high-assurance review",
                timestamp=1700000000,
                head_sha=head2,
            )
            # Must complete transition cleanly without IllegalRunTransition
            app.apply_review_event(event)

            rec_final = app.run_store.get(esc_id)
            self.assertIsNotNone(rec_final)
            self.assertEqual(rec_final.state, RunState.MUTATING)

    def test_execute_slay_fixer_advanced_escalation_running_then_complete_retains_identity_and_mutates(self):
        """CRITICAL: ReviewEngine running event must retain active escalation identity, completing to mutating without orphan."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            app.watch_review_batch = lambda batch: None
            app.review_engine = mock.MagicMock()
            app.call_from_thread = lambda fn, *args, **kwargs: fn(*args, **kwargs)

            head1 = _sha("1")
            head2 = _sha("2")

            stop = tui.Stop(
                repository="projectbluefin/review",
                number=705,
                action="review",
                title="feat: fixer escalation running to complete test",
                head_sha=head1,
                review_status="complete",
                live={
                    "baseRefOid": _sha("a"),
                    "headRefOid": head1,
                    "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop.review_result = mock.MagicMock(findings=[], provenance={"head_sha": head1})

            id_old = app.run_identity(stop, head_sha=head1)
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)

            app.record_fixer_head(stop.repository, stop.number, head2)
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": head2,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            app.stops = [stop]
            from tui.review_snapshot import BatchSnapshot, BatchReviewItem
            def intercept_start_review_batch(stops, **kw):
                snapshot = BatchSnapshot((
                    BatchReviewItem(
                        stop.key,
                        stop.repository,
                        stop.number,
                        stop.title,
                        _sha("a"),
                        head2,
                        stop.live,
                        [],
                    ),
                ), {})
                app.begin_review_batch(stops, snapshot, **kw)

            app.start_review_batch = intercept_start_review_batch
            app._execute_slay([stop])

            esc_backend, esc_model, esc_effort = app.escalation_profile(stop)
            esc_id = app.run_identity(stop, head_sha=head2, model=esc_model, effort=esc_effort)
            rec_re = app.run_store.get(esc_id)
            self.assertIsNotNone(rec_re)
            self.assertEqual(rec_re.state, RunState.RE_REVIEWING)

            # Nonterminal event: running
            running_event = tui.ReviewEvent(
                key=stop.key,
                state="running",
                note="review dispatched",
                timestamp=1700000000,
                head_sha=head2,
            )
            app.apply_review_event(running_event)

            # Identity MUST remain through nonterminal event
            self.assertIn(stop.key, app.active_review_identities)
            self.assertEqual(app.active_review_identities[stop.key], esc_id)

            # Terminal event: complete
            complete_event = tui.ReviewEvent(
                key=stop.key,
                state="complete",
                note="clean high-assurance review",
                timestamp=1700000001,
                head_sha=head2,
            )
            app.apply_review_event(complete_event)

            # Identity must be cleaned on terminal event
            self.assertNotIn(stop.key, app.active_review_identities)

            # Record must become mutating
            rec_final = app.run_store.get(esc_id)
            self.assertIsNotNone(rec_final)
            self.assertEqual(rec_final.state, RunState.MUTATING)
            self.assertGreater(len(app.landing_queue), 0)

    def test_execute_slay_mixed_batch_preserves_per_target_escalation_profiles(self):
        """CRITICAL: batch with ordinary targets and different escalation targets dispatches each profile safely."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            app._request_reconciliation = lambda: None
            app.watch_review_batch = lambda batch: None

            head_ord = _sha("1")
            head_esc1 = _sha("2")
            head_esc2 = _sha("3")

            stop_ord = tui.Stop(
                repository="projectbluefin/review",
                number=701,
                action="review",
                title="feat: ordinary PR",
                head_sha=head_ord,
                live={"baseRefOid": _sha("a"), "headRefOid": head_ord},
            )

            stop_esc1 = tui.Stop(
                repository="projectbluefin/review",
                number=702,
                action="review",
                title="feat: fixer escalation PR 1",
                head_sha=_sha("4"),
                review_status="complete",
                live={
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("4"),
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop_esc1.review_result = mock.MagicMock(findings=[], provenance={"head_sha": _sha("4")})
            id_esc1_old = app.run_identity(stop_esc1, head_sha=_sha("4"))
            app.run_store.create(id_esc1_old)
            app.run_store.transition(id_esc1_old, RunState.REVIEWING)
            app.run_store.transition(id_esc1_old, RunState.REVIEW_CLEAN)
            app.record_fixer_head(stop_esc1.repository, stop_esc1.number, head_esc1)

            stop_esc2 = tui.Stop(
                repository="projectbluefin/other",
                number=703,
                action="review",
                title="feat: fixer escalation PR 2",
                head_sha=_sha("5"),
                review_status="complete",
                live={
                "baseRefOid": _sha("a"),
                "headRefOid": _sha("5"),
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop_esc2.review_result = mock.MagicMock(findings=[], provenance={"head_sha": _sha("5")})
            id_esc2_old = app.run_identity(stop_esc2, head_sha=_sha("5"))
            app.run_store.create(id_esc2_old)
            app.run_store.transition(id_esc2_old, RunState.REVIEWING)
            app.run_store.transition(id_esc2_old, RunState.REVIEW_CLEAN)
            app.record_fixer_head(stop_esc2.repository, stop_esc2.number, head_esc2)

            def mock_fetch_live(repo, num, force=False):
                if num == 701:
                    return {"baseRefOid": _sha("a"), "headRefOid": head_ord}
                elif num == 702:
                    return {"baseRefOid": _sha("a"), "headRefOid": head_esc1, "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
                elif num == 703:
                    return {"baseRefOid": _sha("a"), "headRefOid": head_esc2, "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}]}
                return {}

            app.fetch_live_pr = mock_fetch_live
            app.stops = [stop_ord, stop_esc1, stop_esc2]

            # Custom escalation profile for repo 'projectbluefin/other'
            orig_esc_profile = app.escalation_profile
            def custom_esc_profile(stop):
                if stop.repository == "projectbluefin/other":
                    return ("omp", "claude-opus-5", "high")
                return orig_esc_profile(stop)
            app.escalation_profile = custom_esc_profile

            dispatched: list[tuple[list[tui.Stop], dict]] = []
            app.start_review_batch = lambda stops, **kw: dispatched.append((list(stops), dict(kw)))

            app._execute_slay([stop_ord, stop_esc1, stop_esc2])

            # There should be 3 distinct dispatches: ordinary, esc1, and esc2
            self.assertEqual(len(dispatched), 3)

            # Ordinary stop must NOT have an escalation profile applied
            ord_dispatch = next(d for d in dispatched if any(s.number == 701 for s in d[0]))
            self.assertEqual(ord_dispatch[0], [stop_ord])
            self.assertIsNone(ord_dispatch[1].get("model"))

            # Escalation stop 1 must have its profile
            esc1_dispatch = next(d for d in dispatched if any(s.number == 702 for s in d[0]))
            self.assertEqual(esc1_dispatch[0], [stop_esc1])
            self.assertEqual(esc1_dispatch[1].get("model"), "gpt-5.6-sol")
            self.assertEqual(esc1_dispatch[1].get("effort"), "medium")

            # Escalation stop 2 must have its distinct profile
            esc2_dispatch = next(d for d in dispatched if any(s.number == 703 for s in d[0]))
            self.assertEqual(esc2_dispatch[0], [stop_esc2])
            self.assertEqual(esc2_dispatch[1].get("model"), "claude-opus-5")
            self.assertEqual(esc2_dispatch[1].get("effort"), "high")

    def test_execute_slay_avoids_duplicate_re_reviewing_transition(self):
        """CRITICAL: _execute_slay avoids duplicate transition when escalation record is already RE_REVIEWING."""
        with self._store_dir() as root:
            app = self._setup_app(root)
            head1 = _sha("1")
            head2 = _sha("2")

            stop = tui.Stop(
                repository="projectbluefin/review",
                number=704,
                action="review",
                title="feat: duplicate re-reviewing test",
                head_sha=head1,
                review_status="complete",
                live={
                "baseRefOid": _sha("a"),
                "headRefOid": head1,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
                },
            )
            stop.review_result = mock.MagicMock(findings=[], provenance={"head_sha": head1})

            id_old = app.run_identity(stop, head_sha=head1)
            app.run_store.create(id_old)
            app.run_store.transition(id_old, RunState.REVIEWING)
            app.run_store.transition(id_old, RunState.REVIEW_CLEAN)

            app.record_fixer_head(stop.repository, stop.number, head2)
            app.fetch_live_pr = lambda repo, num, force=False: {
                "baseRefOid": _sha("a"),
                "headRefOid": head2,
                "reviews": [{"author": {"login": "jorge"}, "state": "APPROVED"}],
            }

            # Pre-create the escalation identity already in RE_REVIEWING
            esc_backend, esc_model, esc_effort = app.escalation_profile(stop)
            new_id = app.run_identity(stop, head_sha=head2, model=esc_model, effort=esc_effort)
            app.run_store.create(new_id)
            app.run_store.transition(new_id, RunState.RE_REVIEWING)

            app.stops = [stop]
            app.start_review_batch = lambda stops, **kw: None

            # Must NOT crash with IllegalRunTransition: cannot transition from re_reviewing to re_reviewing
            app._execute_slay([stop])
            rec = app.run_store.get(new_id)
            self.assertEqual(rec.state, RunState.RE_REVIEWING)


if __name__ == "__main__":
    unittest.main()
