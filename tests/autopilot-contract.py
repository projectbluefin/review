import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))
from harness.autopilot import (Discovery, Preference, can_remember,
                               choose_option, discover_all, load_preferences,
                               remember_success)  # noqa: E402
from harness.registry import Availability  # noqa: E402
from tui.review_evidence_manifest import ReviewRequest  # noqa: E402
from tui.review_result import ReviewResult  # noqa: E402


class AutopilotContract(unittest.TestCase):
    def test_only_valid_terminal_exact_bound_result_can_be_remembered(self):
        binding = ReviewRequest("org", "repo", 166, "a" * 40, "b" * 40, "a", "t", generated_at="now")
        result = ReviewResult(1, "complete", provenance={
            "backend": "codex", "repository": "org/repo", "pull_request": 166,
            "base_sha": "a" * 40, "head_sha": "b" * 40,
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
                self.assertEqual(set(saved["*"]), {"harness_id", "model", "effort"})
                self.assertNotIn("token", json.dumps(saved).lower())
                self.assertEqual(load_preferences()["org/repo"].harness_id, "codex")
            finally:
                if old is None: os.environ.pop("XDG_CONFIG_HOME", None)
                else: os.environ["XDG_CONFIG_HOME"] = old

    def test_registry_drives_metadata_and_recommended_selection(self):
        with patch("harness.autopilot.CodexHarness.probe", return_value=Availability.READY), \
             patch("harness.autopilot.OmpHarness.probe", return_value=Availability.READY):
            options = discover_all()
        self.assertTrue({option.harness.branding.harness_id for option in options} >= {"omp", "codex"})
        self.assertTrue(all(len(option.harness.branding.terminal_badge) == 2 for option in options))
        selected = choose_option("org/repo", {}, options)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.harness.branding.harness_id, "codex")
        self.assertEqual(selected.discovery.availability.value, "READY")

    def test_remembered_unavailable_choice_does_not_silently_fallback(self):
        with patch("harness.autopilot.CodexHarness.probe", return_value=Availability.UNAVAILABLE_BINARY), \
             patch("harness.autopilot.OmpHarness.probe", return_value=Availability.READY):
            options = discover_all()
        remembered = {"org/repo": Preference("codex", "gpt-5.6-luna", "low")}
        selected = choose_option("org/repo", remembered, options)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.harness.branding.harness_id, "codex")
        self.assertIs(selected.discovery.availability, Availability.UNAVAILABLE_BINARY)

    def test_no_ready_harness_returns_no_selection(self):
        with patch("harness.autopilot.CodexHarness.probe", return_value=Availability.UNAVAILABLE_BINARY), \
             patch("harness.autopilot.OmpHarness.probe", return_value=Availability.UNAVAILABLE_BINARY):
            options = discover_all()
        self.assertIsNone(choose_option("org/repo", {}, options))


if __name__ == "__main__":
    unittest.main()
