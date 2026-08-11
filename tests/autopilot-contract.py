#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))
from harness.autopilot import (Discovery, Preference, can_remember, choose,
                               load_preferences, remember_success, stale_choice)  # noqa: E402
from harness.registry import Availability, Binding  # noqa: E402
from tui.review_result import ReviewResult  # noqa: E402


class AutopilotContract(unittest.TestCase):
    def test_selection_prefers_repo_then_global_then_configured_then_cheap_codex(self):
        ready = Discovery("codex", "ready", "ready", "ready", "gpt-5.6-luna", "low", Availability.READY)
        repo = Preference("codex", "gpt-5.6-luna", "medium")
        self.assertEqual(choose("org/repo", {"org/repo": repo}, ready).effort, "medium")
        self.assertEqual(choose("org/repo", {"*": repo}, ready).effort, "medium")

    def test_unavailable_choice_is_not_silently_replaced(self):
        missing = Discovery("codex", "missing", "missing", "unavailable", "gpt-5.6-luna", "low", Availability.UNAVAILABLE_BINARY)
        self.assertIsNone(choose("org/repo", {"*": Preference("codex", "gpt-5.6-luna", "low")}, missing))
        self.assertIn("confirm a replacement", stale_choice("org/repo", {"*": Preference("codex", "gpt-5.6-luna", "low")}, missing))

    def test_only_valid_terminal_exact_bound_result_can_be_remembered(self):
        binding = Binding("org/repo", 166, "base", "head")
        result = ReviewResult(1, "complete", provenance={
            "backend": "codex", "repository": "org/repo", "pull_request": 166,
            "base_sha": "base", "head_sha": "head",
        })
        self.assertTrue(can_remember(result, binding))
        self.assertFalse(can_remember(ReviewResult(1, "incomplete", provenance=result.provenance), binding))

    def test_success_memory_is_atomic_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = directory
            try:
                remember_success({}, "org/repo", Preference("codex", "gpt-5.6-luna", "low"))
                saved = json.loads(Path(directory, "bluefin-review", "harness.json").read_text())
                self.assertEqual(set(saved["*"]), {"backend", "model", "effort"})
                self.assertNotIn("token", json.dumps(saved).lower())
                self.assertEqual(load_preferences()["org/repo"].backend, "codex")
            finally:
                if old is None: os.environ.pop("XDG_CONFIG_HOME", None)
                else: os.environ["XDG_CONFIG_HOME"] = old


if __name__ == "__main__":
    unittest.main()
