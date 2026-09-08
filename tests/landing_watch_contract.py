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


if __name__ == "__main__":
    unittest.main()
