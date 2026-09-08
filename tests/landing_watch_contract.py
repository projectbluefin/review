"""Regression contract for landing CI watches in #382 and #383.

The cases cover safe process invocation and resumable watch identity:
serialization, idempotent reporting, terminal protection, active timeout
continuation, superseded attempts, and stable error classifications.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "image"))

from tui import landing  # noqa: E402


class LandingWatchContractTests(unittest.TestCase):
    def test_landing_command_is_argv_and_preserves_hostile_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="landing hostile ") as root:
            prompt = Path(root) / 'prompt;$(touch PWNED).md'
            task = landing.LandingTask(
                task_id="batch;$(touch PWNED)",
                stops=[],
                login="reviewer",
                prompt_path=str(prompt),
            )
            command = landing.landing_command(task)
            self.assertIsInstance(command, list)
            self.assertTrue(all(isinstance(arg, str) for arg in command))
            self.assertIn(str(prompt), command)
            self.assertNotIn(" ".join(command), {"sh", "bash"})

    def test_landing_command_preserves_quoted_template_arguments(self) -> None:
        old = __import__("os").environ.get("BLUEFIN_REVIEW_LANDING_COMMAND")
        __import__("os").environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = (
            'runner --label "two words" @PROMPT'
        )
        try:
            task = landing.LandingTask(
                task_id="batch", stops=[], login="reviewer", prompt_path="/tmp/prompt with spaces"
            )
            self.assertEqual(
                landing.landing_command(task),
                ["runner", "--label", "two words", "/tmp/prompt with spaces"],
            )
        finally:
            if old is None:
                __import__("os").environ.pop("BLUEFIN_REVIEW_LANDING_COMMAND", None)
            else:
                __import__("os").environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = old

    def test_malformed_landing_template_fails_before_launch(self) -> None:
        old = __import__("os").environ.get("BLUEFIN_REVIEW_LANDING_COMMAND")
        __import__("os").environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = 'runner "unterminated'
        try:
            with self.assertRaises(ValueError):
                landing.landing_command(landing.LandingTask("batch", [], "reviewer"))
        finally:
            if old is None:
                __import__("os").environ.pop("BLUEFIN_REVIEW_LANDING_COMMAND", None)
            else:
                __import__("os").environ["BLUEFIN_REVIEW_LANDING_COMMAND"] = old

    def test_watch_target_rejects_untrusted_identity_and_unbounded_text(self) -> None:
        base = {
            "repository": "org/repo",
            "pull_request": 7,
            "head_sha": "a" * 40,
            "run_id": 1,
            "attempt": 1,
            "status": "in_progress",
            "observed_at": 100,
            "deadline": 400,
        }
        for field, value in (
            ("repository", "org/repo;rm -rf"),
            ("title", "x" * 1001),
            ("path", "x\nunsafe"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    landing.WatchTarget.from_dict({**base, field: value})

    def test_report_event_validates_pr_state_and_delimits_note(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "events.jsonl")
            self.assertNotEqual(landing.report_event(path, "bad", "merged", "note"), 0)
            self.assertEqual(landing.report_event(path, "org/repo#7", "merged", "x\nstate"), 0)
            self.assertEqual(
                landing.parse_status(path)["org/repo#7"]["note"],
                "x\nstate",
            )

    def test_watch_target_round_trips_and_matches_exact_identity(self) -> None:
        target = landing.WatchTarget(
            repository="org/repo",
            pull_request=7,
            run_id=42,
            head_sha="a" * 40,
            attempt=3,
            title="title;$(touch PWNED)",
            path="/tmp/path with spaces",
            observed_at=100.0,
            deadline=400.0,
        )
        restored = landing.WatchTarget.from_dict(
            json.loads(json.dumps(target.to_dict()))
        )
        self.assertEqual(restored, target)
        self.assertTrue(target.matches(repository=target.repository, run_id=42,
                                       head_sha="a" * 40, attempt=3))
        self.assertFalse(target.matches(repository=target.repository, run_id=42,
                                        head_sha="b" * 40, attempt=3))
        self.assertFalse(target.matches(repository=target.repository, run_id=42,
                                        head_sha="a" * 40, attempt=4))

    def test_report_watch_is_idempotent_and_refuses_terminal_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "watch.jsonl")
            target = landing.WatchTarget(
                "org/repo",
                pull_request=7,
                run_id=1,
                head_sha="a" * 40,
                attempt=1,
                title="title",
                path=root,
                observed_at=100.0,
                deadline=400.0,
            )
            self.assertEqual(landing.report_watch(path, target, "waiting-ci"), 0)
            self.assertEqual(landing.report_watch(path, target, "waiting-ci"), 0)
            self.assertEqual(landing.report_event(path, "org/repo#7", "merged", "merged"), 0)
            self.assertNotEqual(landing.report_watch(path, target, "waiting-ci"), 0)

    def test_active_timeout_continues_the_same_watch(self) -> None:
        result = landing.classify_watch_result(
            conclusion=None, timed_out=True, active=True, deadline_remaining=30
        )
        self.assertEqual(result, "continue")

    def test_superseded_head_or_attempt_is_rejected(self) -> None:
        target = landing.WatchTarget(
            "org/repo",
            pull_request=7,
            run_id=1,
            head_sha="a" * 40,
            attempt=1,
            title="title",
            path="/tmp",
            observed_at=100.0,
            deadline=400.0,
        )
        for kwargs in (
            {"head_sha": "b" * 40, "attempt": 1},
            {"head_sha": "a" * 40, "attempt": 2},
        ):
            with self.subTest(**kwargs):
                self.assertEqual(
                    landing.classify_watch_identity(target, **kwargs), "superseded"
                )

    def test_watch_classifies_permission_network_and_exhausted_deadline(self) -> None:
        cases = (
            ({"permission": True}, "permission"),
            ({"network_error": True}, "network"),
            ({"timed_out": True, "active": True, "deadline_remaining": 0}, "deadline"),
        )
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(landing.classify_watch_result(**kwargs), expected)

    def test_watch_target_rejects_invalid_repository_and_non_finite_timing(self) -> None:
        base = {
            "repository": "org/repo",
            "pull_request": 7,
            "head_sha": "a" * 40,
            "run_id": 1,
            "attempt": 1,
            "status": "in_progress",
            "observed_at": 100.0,
            "deadline": 400.0,
        }
        for key, value in (("repository", "org/repo/extra"), ("deadline", float("inf"))):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    landing.WatchTarget.from_dict({**base, key: value})

    def test_report_watch_accepts_a_newer_superseding_active_target(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "watch.jsonl")
            current = landing.WatchTarget(
                "org/repo", pull_request=7, run_id=1, head_sha="a" * 40,
                attempt=1, status="in_progress", observed_at=100.0, deadline=400.0,
            )
            superseded = landing.WatchTarget(
                "org/repo", pull_request=7, run_id=1, head_sha="b" * 40,
                attempt=1, status="in_progress", observed_at=101.0, deadline=400.0,
            )
            self.assertEqual(landing.report_watch(path, current, "waiting"), 0)
            self.assertEqual(landing.report_watch(path, superseded, "waiting"), 0)
            self.assertEqual(
                landing.watch_target(landing.parse_status(path)["org/repo#7"]).head_sha,
                "b" * 40,
            )

    def test_report_watch_rejects_an_older_active_target(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "watch.jsonl")
            current = landing.WatchTarget(
                "org/repo", pull_request=7, run_id=2, head_sha="b" * 40,
                attempt=2, status="in_progress", observed_at=101.0, deadline=401.0,
            )
            stale = landing.WatchTarget(
                "org/repo", pull_request=7, run_id=1, head_sha="a" * 40,
                attempt=1, status="in_progress", observed_at=100.0, deadline=400.0,
            )
            self.assertEqual(landing.report_watch(path, current, "waiting"), 0)
            self.assertNotEqual(landing.report_watch(path, stale, "late"), 0)

    def test_report_watch_requires_the_exact_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = landing.WatchTarget(
                "org/repo", pull_request=7, run_id=1, head_sha="a" * 40,
                attempt=None, status="in_progress", observed_at=100.0, deadline=400.0,
            )
            self.assertNotEqual(
                landing.report_watch(str(Path(root) / "watch.jsonl"), target, "waiting"),
                0,
            )


if __name__ == "__main__":
    unittest.main()
