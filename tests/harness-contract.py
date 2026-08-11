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


if __name__ == "__main__":
    unittest.main()
