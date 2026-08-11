#!/usr/bin/env python3
"""Focused contracts for the adapter-first harness seam."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from harness.codex import CodexHarness  # noqa: E402
from harness.goose import GooseHarness  # noqa: E402
from harness.registry import Availability, Binding, HarnessRegistry  # noqa: E402


class HarnessContract(unittest.TestCase):
    def setUp(self):
        self.binding = Binding("project/review", 166, "base", "head")

    def test_registry_exposes_both_adapters_without_fallback(self):
        registry = HarnessRegistry()
        registry.register(GooseHarness())
        registry.register(CodexHarness())
        self.assertEqual(registry.names(), ("goose", "codex"))
        self.assertIs(registry.get("goose"), registry.require_ready("goose"))
        with self.assertRaises(RuntimeError):
            registry.require_ready("codex")

    def test_codex_unavailable_states_are_explicit(self):
        for state in (Availability.UNAVAILABLE_BINARY, Availability.UNAVAILABLE_AUTH,
                      Availability.UNSUPPORTED_CAPABILITY,
                      Availability.FAILED_CONFORMANCE):
            adapter = CodexHarness(availability=state)
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                adapter.invoke(self.binding, prompt="p")

    def test_codex_defaults_and_provenance_capability(self):
        adapter = CodexHarness()
        self.assertEqual(adapter.model, "gpt-5.6-luna")
        self.assertEqual(adapter.effort, "max")
        self.assertTrue(adapter.capabilities.exact_binding)
        self.assertTrue(adapter.capabilities.provenance)

    def test_binding_is_exact_context_shape(self):
        self.assertEqual(self.binding.repository, "project/review")
        self.assertEqual(self.binding.pull_request, 166)
        self.assertEqual((self.binding.base_sha, self.binding.head_sha), ("base", "head"))

    def test_codex_command_binds_context_model_effort_and_json(self):
        adapter = CodexHarness(availability=Availability.READY)
        command = adapter.command(self.binding, prompt="inspect", effort="low")
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("model_reasoning_effort=low", command)
        self.assertIn("project/review#166 base=base head=head", command[-1])

    def test_codex_stream_converts_into_merged_review_result(self):
        adapter = CodexHarness(availability=Availability.READY)
        result = adapter.convert(
            '{"version":1,"state":"complete","counts":{"critical":0,"high":0,"medium":0,"low":0},"findings":[]}',
            self.binding,
        )
        self.assertEqual(result.state, "complete")
        self.assertEqual(result.provenance["backend"], "codex")
        self.assertEqual(result.provenance["model"], "gpt-5.6-luna")
        self.assertEqual(result.provenance["repository"], "project/review")

    def test_codex_cancellation_uses_process_group(self):
        adapter = CodexHarness(availability=Availability.READY)
        self.assertTrue(adapter.process_group_cancellation)


if __name__ == "__main__":
    unittest.main()
