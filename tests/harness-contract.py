"""Focused contracts for the adapter-first harness seam."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from harness.codex import CodexHarness  # noqa: E402
from harness.goose import GooseHarness  # noqa: E402
from harness.registry import Availability, DraftRequest, DraftState, HarnessRegistry  # noqa: E402
from tui.review_evidence_manifest import ReviewRequest  # noqa: E402


class HarnessContract(unittest.TestCase):
    def setUp(self):
        self.binding = ReviewRequest("project", "review", 166, "a" * 40, "b" * 40, "maintainer", "review", generated_at="test")
        self.review_payload = {
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
        }

    @staticmethod
    def stream(*events):
        return "\n".join(
            event if isinstance(event, str) else json.dumps(event)
            for event in events
        )

    def result_event(self, item_id="item_1", payload=None):
        return {
            "type": "item.completed",
            "item": {
                "id": item_id,
                "type": "agent_message",
                "text": json.dumps(payload or self.review_payload),
            },
        }

    def terminal_stream(self, *before_result, payload=None):
        return self.stream(
            {"type": "thread.started", "thread_id": "thread_1"},
            {"type": "turn.started"},
            *before_result,
            self.result_event(payload=payload),
            {"type": "turn.completed", "usage": {}},
        )

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
        self.assertEqual(command.count("--skip-git-repo-check"), 1)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("model_reasoning_effort=low", command)
        self.assertIn("project/review#166 base=" + "a" * 40 + " head=" + "b" * 40, command[-1])
        self.assertIn("Do not mutate GitHub", command[-1])

    def test_codex_command_uses_packaged_code_mode_host_without_shell_sandbox(self):
        command = CodexHarness(availability=Availability.READY).command(
            self.binding, prompt="inspect", effort="low"
        )
        enabled = [command[index + 1] for index, value in enumerate(command) if value == "--enable"]
        self.assertEqual(enabled, ["code_mode_only", "code_mode_host"])
        self.assertIn("features.code_mode_host.disable_in_process_fallback=true", command)
        self.assertIn("suppress_unstable_features_warning=true", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn('"version":1', command[-1])
        self.assertIn('"critical":0,"high":0,"medium":0,"low":0', command[-1])

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
            self.terminal_stream(),
            self.binding,
        )
        self.assertEqual(result.state, "complete")
        self.assertEqual(result.provenance["backend"], "codex")
        self.assertEqual(result.provenance["model"], "gpt-5.6-luna")
        self.assertEqual(result.provenance["reasoning_effort"], "low")
        self.assertEqual(result.provenance["repository"], "project/review")

    def test_codex_accepts_blank_jsonl_framing_lines(self):
        stream = self.terminal_stream().replace("\n", "\n \n")
        result = CodexHarness(availability=Availability.READY).convert(
            stream, self.binding
        )
        self.assertEqual(result.state, "complete")

    def test_codex_stream_keeps_stderr_out_of_official_jsonl(self):
        class Process:
            stdout = iter((self.terminal_stream() + "\n").splitlines(keepends=True))
            returncode = 0

            @staticmethod
            def wait():
                return 0

        with patch("harness.codex.subprocess.Popen", return_value=Process()) as popen:
            result = CodexHarness(availability=Availability.READY).stream(
                self.binding, prompt="inspect", on_line=lambda _line: None
            )
        self.assertEqual(result.state, "complete")
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_codex_converts_terminal_agent_message_envelope(self):
        result = CodexHarness(availability=Availability.READY).convert(
            self.terminal_stream(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "agent_message",
                        "text": "Reviewing exact-head evidence.",
                    },
                }
            ),
            self.binding,
            model="gpt-5.6-luna",
            effort="low",
        )
        self.assertEqual(result.state, "complete")

    def test_codex_rejects_bare_review_result(self):
        stream = json.dumps(self.review_payload)
        result = CodexHarness(availability=Availability.READY).convert(
            stream, self.binding
        )
        self.assertEqual(result.state, "unparsable")
        self.assertEqual(result.raw_evidence, [stream])

    def test_codex_rejects_invalid_terminal_lifecycles(self):
        started = [
            {"type": "thread.started", "thread_id": "thread_1"},
            {"type": "turn.started"},
        ]
        result = self.result_event()
        completed = {"type": "turn.completed", "usage": {}}
        finding_payload = {
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0},
            "findings": [
                {
                    "severity": "medium",
                    "file": "image/harness/codex.py",
                    "line": 1,
                    "title": "ambiguous result",
                }
            ],
        }
        cases = {
            "missing thread start": [started[1], result, completed],
            "thread start missing id": [
                {"type": "thread.started"},
                started[1],
                result,
                completed,
            ],
            "missing turn start": [started[0], result, completed],
            "duplicate turn start": [
                *started,
                {"type": "turn.started"},
                result,
                completed,
            ],
            "missing turn completion": [*started, result],
            "completion before result": [*started, completed, result],
            "valid-looking nonterminal result": [
                *started,
                result,
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_2",
                        "type": "agent_message",
                        "text": "Continuing after an intermediate result.",
                    },
                },
                completed,
            ],
            "duplicate result messages": [
                *started,
                result,
                self.result_event("item_2", finding_payload),
                completed,
            ],
            "failed turn": [
                *started,
                result,
                {"type": "turn.failed", "error": {"message": "failed"}},
            ],
            "cancelled turn": [
                *started,
                result,
                {"type": "turn.cancelled"},
            ],
            "event after completion": [
                *started,
                result,
                completed,
                {"type": "thread.started", "thread_id": "thread_2"},
            ],
            "completion missing usage": [
                *started,
                result,
                {"type": "turn.completed"},
            ],
            "completion with malformed usage": [
                *started,
                result,
                {"type": "turn.completed", "usage": []},
            ],
            "malformed terminal json": [
                *started,
                result,
                '{"type":"turn.completed"',
            ],
            "malformed nonterminal json": [
                started[0],
                '{"type":"turn.started"',
                result,
                completed,
            ],
            "malformed final result item": [
                *started,
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": None,
                    },
                },
                completed,
            ],
        }
        adapter = CodexHarness(availability=Availability.READY)
        for name, events in cases.items():
            with self.subTest(name=name):
                stream = self.stream(*events)
                converted = adapter.convert(stream, self.binding)
                self.assertEqual(converted.state, "unparsable")
                self.assertEqual(converted.raw_evidence, stream.splitlines())

    def test_codex_bounds_raw_evidence_for_malformed_stream(self):
        stream = self.stream(
            {"type": "thread.started", "thread_id": "thread_1"},
            {"type": "turn.started"},
            *(["x" * 1000] * 500),
        )
        result = CodexHarness(availability=Availability.READY).convert(
            stream, self.binding
        )
        self.assertEqual(result.state, "unparsable")
        self.assertLess(len(result.raw_evidence), 500)
        self.assertLessEqual(len(result.raw_evidence), 400)
        self.assertLessEqual(len("\n".join(result.raw_evidence)), 120_000)

    def test_codex_provenance_uses_invocation_overrides(self):
        payload = {
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 0, "medium": 1, "low": 0},
            "findings": [
                {
                    "severity": "medium",
                    "file": "image/harness/codex.py",
                    "line": 1,
                    "title": "finding",
                }
            ],
        }
        result = CodexHarness(availability=Availability.READY).convert(
            self.terminal_stream(payload=payload),
            self.binding,
            model="gpt-5.6-luna",
            effort="medium",
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

    def test_drafting_is_explicit_and_goose_does_not_fallback(self):
        self.assertTrue(CodexHarness().capabilities.body_drafting)
        self.assertFalse(GooseHarness().capabilities.body_drafting)
        request = DraftRequest(self.binding, "approve", self._evidence(), {"title": "A PR"})
        with self.assertRaises(RuntimeError) as error:
            GooseHarness().draft(request)
        self.assertIn("UNSUPPORTED_CAPABILITY", str(error.exception))

    def _evidence(self, **overrides):
        values = {
            "version": 1, "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [], "provenance": {
                "backend": "codex", "model": "gpt-5.6-luna",
                "repository": "project/review", "pull_request": 166,
                "base_sha": "a" * 40, "head_sha": "b" * 40,
            },
        }
        values.update(overrides)
        from tui.review_result import ReviewResult
        return ReviewResult.from_dict(values)

    def test_draft_request_validates_verdict_binding_evidence_and_live_facts(self):
        for verdict in ("approve", "request-changes", "comment"):
            result = CodexHarness().validate_draft(
                DraftRequest(self.binding, verdict, self._evidence(), {"title": "A PR"})
            )
            self.assertEqual(result.state, DraftState.COMPLETE)
            self.assertLessEqual(len(result.markdown), 4096)
            self.assertEqual(result.provenance["head_sha"], "b" * 40)
        for bad in (
            self._evidence(state="incomplete"),
            self._evidence(provenance={"backend": "codex", "model": "x"}),
            self._evidence(provenance={"backend": "codex", "model": "x", "repository": "project/review", "pull_request": 166, "base_sha": "a" * 40, "head_sha": "c" * 40}),
        ):
            with self.assertRaises(ValueError):
                DraftRequest(self.binding, "comment", bad, {})

    def test_draft_request_accepts_findings_only_for_safe_verdicts(self):
        finding = {"severity": "medium", "file": "image/harness/codex.py", "line": 1, "title": "validated finding"}
        evidence = self._evidence(state="findings", findings=[finding], counts={"critical": 0, "high": 0, "medium": 1, "low": 0})
        with self.assertRaises(ValueError):
            DraftRequest(self.binding, "approve", evidence, {})
        for verdict in ("request-changes", "comment"):
            DraftRequest(self.binding, verdict, evidence, {})

    def test_draft_request_rejects_every_failed_evidence_state(self):
        for state in ("failed", "unparsable", "incomplete"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                DraftRequest(self.binding, "comment", self._evidence(state=state), {})

    def test_draft_request_bounds_nested_evidence_aggregate(self):
        oversized = "x" * 200_000
        for evidence in (self._evidence(findings=[{"title": oversized}]), self._evidence(verification=[{"detail": oversized}]), self._evidence(provenance={"backend": "codex", "model": "x", "repository": "project/review", "pull_request": 166, "base_sha": "a" * 40, "head_sha": "b" * 40, "note": oversized}), self._evidence(overlap={"details": oversized}), self._evidence(raw_evidence=[oversized])):
            with self.assertRaises(ValueError):
                DraftRequest(self.binding, "comment", evidence, {})
        with self.assertRaises(ValueError):
            DraftRequest(self.binding, "comment", self._evidence(), {"nested": {"value": oversized}})

    def test_draft_request_rejects_each_exact_binding_mismatch(self):
        for field, value in {"repository": "other/review", "pull_request": 167, "base_sha": "c" * 40, "head_sha": "d" * 40}.items():
            provenance = self._evidence().provenance | {field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                DraftRequest(self.binding, "comment", self._evidence(provenance=provenance), {})

    def test_failed_draft_raw_evidence_has_line_and_character_bounds(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {})
        result = CodexHarness().convert_draft("x" * 200_000, request, exit_code=1)
        self.assertEqual(result.state, DraftState.FAILED)
        self.assertLessEqual(len(result.raw_evidence), 400)
        self.assertLessEqual(sum(map(len, result.raw_evidence)), 120_000)

    def test_draft_provenance_uses_adapter_model_and_effort(self):
        adapter = CodexHarness(model="gpt-custom", effort="high")
        result = adapter.validate_draft(DraftRequest(self.binding, "comment", self._evidence(), {}))
        self.assertEqual(result.provenance["model"], "gpt-custom")
        self.assertEqual(result.provenance["effort"], "high")

    def test_codex_draft_command_is_bounded_read_only_and_no_review(self):
        request = DraftRequest(self.binding, "request-changes", self._evidence(), {"title": "A PR"})
        command = CodexHarness().draft_command(request)
        prompt = command[-1]
        self.assertIn("request-changes", prompt)
        self.assertIn("Do not perform another code review", prompt)
        self.assertIn("Do not mutate GitHub", prompt)
        self.assertNotIn("find new", prompt.lower())

    def test_draft_failure_is_distinct_and_bounded(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {})
        result = CodexHarness().convert_draft("x" * 5000, request, exit_code=1)
        self.assertEqual(result.state, DraftState.FAILED)
        self.assertLessEqual(len(result.raw_evidence), 400)


if __name__ == "__main__":
    unittest.main()
