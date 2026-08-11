"""Focused contracts for the adapter-first harness seam."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from harness.codex import CodexHarness  # noqa: E402
from harness.goose import GooseHarness  # noqa: E402
from harness.registry import Availability, HarnessRegistry  # noqa: E402
from tui.review_evidence_manifest import ReviewRequest  # noqa: E402


class HarnessContract(unittest.TestCase):
    def setUp(self):
        self.binding = ReviewRequest("project", "review", 166, "a" * 40, "b" * 40, "maintainer", "review", generated_at="test")

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
        self.assertEqual(adapter.effort, "low")
        self.assertTrue(adapter.capabilities.exact_binding)
        self.assertTrue(adapter.capabilities.provenance)

    def test_binding_is_exact_context_shape(self):
        self.assertEqual(f"{self.binding.owner}/{self.binding.repository}", "project/review")
        self.assertEqual(self.binding.pull_request_number, 166)
        self.assertEqual((self.binding.base_sha, self.binding.head_sha), ("a" * 40, "b" * 40))

    def test_codex_command_binds_context_model_effort_and_json(self):
        adapter = CodexHarness(availability=Availability.READY)
        command = adapter.command(self.binding, prompt="inspect", effort="low")
        self.assertEqual(
            command[:8],
            [
                "codex", "exec", "--ignore-user-config", "--disable", "apps",
                "--config", "mcp_servers={}", "--json",
            ],
        )
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("model_reasoning_effort=low", command)
        self.assertIn("project/review#166 base=" + "a" * 40 + " head=" + "b" * 40, command[-1])

    def test_codex_invoke_arguments_reach_cli_command(self):
        adapter = CodexHarness(availability=Availability.READY)
        command = adapter.command(
            self.binding, prompt="inspect", model="gpt-5.6-luna",
            effort="low", steer="focus on exact-head evidence",
        )
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("focus on exact-head evidence", command[-1])

    def test_codex_stream_converts_into_merged_review_result(self):
        adapter = CodexHarness(availability=Availability.READY)
        result = adapter.convert(
            '{"version":1,"state":"complete","counts":{"critical":0,"high":0,"medium":0,"low":0},"findings":[]}',
            self.binding,
        )
        self.assertEqual(result.state, "complete")
        self.assertEqual(result.provenance["backend"], "codex")
        self.assertEqual(result.provenance["model"], "gpt-5.6-luna")
        self.assertEqual(result.provenance["reasoning_effort"], "low")
        self.assertEqual(result.provenance["repository"], "project/review")

    def test_codex_converts_terminal_agent_message_envelope(self):
        payload = {
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
        }
        stream = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": json.dumps(payload)},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        result = CodexHarness(availability=Availability.READY).convert(
            stream, self.binding, model="gpt-5.6-luna", effort="low",
        )
        self.assertEqual(result.state, "complete")

    def test_codex_provenance_uses_invocation_overrides(self):
        payload = json.dumps({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0},
            "findings": [],
        })
        result = CodexHarness(availability=Availability.READY).convert(
            payload, self.binding, model="gpt-5.6-luna", effort="medium",
        )
        self.assertEqual(result.provenance["model"], "gpt-5.6-luna")
        self.assertEqual(result.provenance["reasoning_effort"], "medium")

    def test_nonzero_codex_exit_fails_closed(self):
        adapter = CodexHarness(availability=Availability.READY)
        result = adapter.convert('{"version":1,"state":"complete","counts":{"critical":0,"high":0,"medium":0,"low":0},"findings":[]}', self.binding, 1)
        self.assertEqual(result.state, "failed")

    def test_binding_rejects_non_sha_placeholders(self):
        with self.assertRaises(ValueError):
            ReviewRequest("project", "review", 166, "?" * 40, "b" * 40, "maintainer", "review", generated_at="test")

    def test_branding_has_badge_full_name_accessible_label_and_source(self):
        for harness in (GooseHarness(), CodexHarness()):
            branding = harness.branding
            self.assertEqual(len(branding.terminal_badge), 2)
            self.assertEqual(branding.accessible_label, branding.accessible_label.strip())
            self.assertNotEqual(branding.accessible_label, branding.terminal_badge)
            self.assertTrue(branding.attribution)

    def test_missing_rich_asset_falls_back_to_full_name(self):
        branding = CodexHarness().branding
        self.assertIsNone(branding.asset_ref)
        self.assertIn("Codex", branding.display_name)

    def test_codex_cancellation_uses_process_group(self):
        adapter = CodexHarness(availability=Availability.READY)
        self.assertTrue(adapter.process_group_cancellation)


if __name__ == "__main__":
    unittest.main()
