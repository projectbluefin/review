"""Focused contracts for the adapter-first harness seam."""

import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from harness.codex import CodexHarness  # noqa: E402
from harness.goose import GooseHarness  # noqa: E402
from harness.registry import Availability, DraftRequest, DraftState, HarnessRegistry  # noqa: E402
from tui.headroom import (  # noqa: E402
    CAVEMAN_INSTRUCTIONS,
    HeadroomClient,
    HeadroomError,
    HeadroomSession,
    HeadroomStats,
    apply_caveman,
)
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

    def test_default_harnesses_use_the_gemini_max_profile(self):
        for adapter in (GooseHarness(), CodexHarness()):
            with self.subTest(adapter=adapter.name):
                self.assertEqual(adapter.model, "gemini-3.8-flash")
                self.assertEqual(adapter.effort, "max")
                self.assertTrue(adapter.capabilities.exact_binding)
                self.assertTrue(adapter.capabilities.provenance)

    def test_goose_registry_owns_invocation_and_result_conversion(self):
        adapter = GooseHarness(availability=Availability.READY)
        command = adapter.command(self.binding, prompt="inspect", model="gpt-5.6-luna", effort="high")
        self.assertEqual(command[:2], ["goose", "review"])
        self.assertIn("project/review#166", command[-1])
        self.assertIn("base=" + "a" * 40, command[-1])
        self.assertIn("head=" + "b" * 40, command[-1])
        result = adapter.convert(
            "goose review: check 'main' completed: 0 finding(s)\n"
            "goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))",
            self.binding,
            0,
            model="gpt-5.6-luna",
            effort="high",
        )
        self.assertEqual(result.state, "complete")
        self.assertEqual(result.provenance["head_sha"], "b" * 40)
        self.assertEqual(result.provenance["backend"], "goose")

    def test_goose_failures_are_explicit_and_fail_closed(self):
        for state in (Availability.UNAVAILABLE_BINARY, Availability.UNAVAILABLE_AUTH,
                      Availability.FAILED_CONFORMANCE):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                GooseHarness(availability=state).invoke(self.binding, prompt="inspect")
        adapter = GooseHarness(availability=Availability.READY)
        self.assertEqual(adapter.convert("not a Goose result", self.binding, 0).state, "unparsable")
        self.assertEqual(adapter.convert("", self.binding, 23).state, "failed")
        self.assertEqual(adapter.convert("", self.binding, 65).state, "incomplete")

    def test_goose_terminal_status_is_owned_by_typed_result(self):
        adapter = GooseHarness(availability=Availability.READY)
        for payload, expected in (
            ("malformed Goose output", 1),
            ("goose review: check 'main' failed: no verdict\n"
             "goose review: orchestrator emitted 0 finding(s) from 1 check(s) "
             "(main: ran, 0 finding(s))", 65),
        ):
            with self.subTest(payload=payload):
                result = adapter.convert(payload, self.binding, 0)
                self.assertNotEqual(result.state, "complete")
                self.assertEqual(adapter.terminal_status(result), expected)

    def test_goose_probe_uses_documented_non_secret_readiness_check(self):
        with self.subTest("binary missing"):
            self.assertEqual(
                GooseHarness.probe("/does/not/exist/goose"),
                Availability.UNAVAILABLE_BINARY,
            )

        with self.subTest("provider unavailable"):
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "goose"
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    "[[ \"$*\" == \"info --check\" ]] || exit 9\n"
                    "printf '%s\\n' 'provider unavailable' >&2\n"
                    "exit 1\n"
                )
                executable.chmod(0o755)
                self.assertEqual(
                    GooseHarness.probe(str(executable)),
                    Availability.UNAVAILABLE_AUTH,
                )

        with self.subTest("ready"):
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "goose"
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    "[[ \"$*\" == \"info --check\" ]] || exit 9\n"
                    "printf '%s\\n' 'provider ready'\n"
                )
                executable.chmod(0o755)
                self.assertEqual(
                    GooseHarness.probe(str(executable)), Availability.READY
                )

        with self.subTest("zero exit without Goose response"):
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "goose"
                executable.write_text("#!/usr/bin/env bash\nexit 0\n")
                executable.chmod(0o755)
                self.assertEqual(
                    GooseHarness.probe(str(executable)), Availability.UNAVAILABLE_AUTH
                )

    def test_goose_stream_reaches_real_process_and_returns_bound_result(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "goose"
            arguments = Path(directory) / "arguments"
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == \"info --check\" ]]; then exit 0; fi\n"
                "printf '%s\\n' \"$*\" > \"$GOOSE_ARGUMENTS\"\n"
                "printf '%s\\n' 'stream-one'\n"
                "printf '%s\\n' \"goose review: check 'main' completed: 0 finding(s)\"\n"
                "printf '%s\\n' \"goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))\"\n"
            )
            executable.chmod(0o755)
            lines = []
            adapter = GooseHarness(
                executable=str(executable), availability=Availability.READY
            )
            with patch.dict(os.environ, {"GOOSE_ARGUMENTS": str(arguments)}):
                result = adapter.stream(
                    self.binding, prompt="inspect", on_line=lines.append,
                    extra_args=("--check-scope", "/tmp/exact-scope", "main...HEAD"),
                )
            self.assertEqual(lines[0], "stream-one")
            self.assertIn("--check-scope /tmp/exact-scope main...HEAD", arguments.read_text())
            self.assertEqual(result.state, "complete")
            self.assertEqual(result.provenance["head_sha"], "b" * 40)

    def test_goose_cancel_terminates_the_process_group(self):
        class Process:
            pid = 1234
            returncode = None
            wait_called = False

            def wait(self):
                self.wait_called = True

        process = Process()
        with patch("harness.goose.os.killpg") as killpg:
            GooseHarness.cancel(process)
        killpg.assert_called_once()
        self.assertTrue(process.wait_called)

    def test_goose_result_redacts_secret_evidence(self):
        with patch.dict(os.environ, {"GOOSE_API_KEY": "goose-secret"}):
            result = GooseHarness(availability=Availability.READY).convert(
                "Authorization: Bearer goose-secret", self.binding
            )
        self.assertNotIn("goose-secret", "\n".join(result.raw_evidence))
        self.assertIn("[REDACTED]", "\n".join(result.raw_evidence))

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
        self.assertIn("gemini-3.8-flash", command)
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
        self.assertEqual(result.provenance["model"], "gemini-3.8-flash")
        self.assertEqual(result.provenance["reasoning_effort"], "max")
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

    def test_drafting_is_explicit_for_each_selected_harness(self):
        self.assertTrue(CodexHarness().capabilities.body_drafting)
        self.assertTrue(GooseHarness().capabilities.body_drafting)
        request = DraftRequest(self.binding, "approve", self._evidence(), {"title": "A PR"})
        command = GooseHarness().draft_command(request, "/tmp/review-draft-prompt")
        self.assertEqual(command, [
            "goose", "run", "--no-session", "-i", "/tmp/review-draft-prompt",
        ])

    def test_goose_draft_uses_goose_model_and_removes_prompt_and_github_tokens(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {"title": "A PR"})
        captured = {}

        class Process:
            pid = 123
            stdout = "Reviewed."
            stderr = ""
            returncode = 0

            def communicate(self):
                return self.stdout, self.stderr

        def run(command, **kwargs):
            prompt_path = command[-1]
            captured.update(command=command, prompt_path=prompt_path,
                            prompt=Path(prompt_path).read_text(), **kwargs)
            return Process()

        adapter = GooseHarness(
            availability=Availability.READY, model="gpt-goose", effort="max"
        )
        with patch.dict(os.environ, {
            "GH_TOKEN": "secret", "GITHUB_TOKEN": "secret",
            "REVIEW_GH_TOKEN": "secret",
        }), patch("harness.goose.subprocess.Popen", side_effect=run):
            result = adapter.draft(request)
        self.assertEqual(result.state, DraftState.COMPLETE)
        self.assertEqual(result.markdown, "Reviewed.")
        self.assertEqual(result.provenance["backend"], "goose")
        self.assertEqual(result.provenance["model"], "gpt-goose")
        self.assertEqual(result.provenance["effort"], "max")
        self.assertEqual(captured["command"][:4], [
            "goose", "run", "--no-session", "-i",
        ])
        self.assertIn("verdict comment", captured["prompt"])
        self.assertIn("Do not perform another code review", captured["prompt"])
        self.assertNotIn("GH_TOKEN", captured["env"])
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertNotIn("REVIEW_GH_TOKEN", captured["env"])
        self.assertEqual(captured["env"]["GOOSE_MODEL"], "gpt-goose")
        self.assertEqual(captured["env"]["GOOSE_THINKING_EFFORT"], "max")
        self.assertFalse(Path(captured["prompt_path"]).exists())

    def test_goose_draft_cancellation_terminates_process_group(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {})
        captured = {}

        class Process:
            pid = 123
            returncode = -signal.SIGTERM

            def communicate(self):
                captured["handler"](signal.SIGTERM, None)
                return "", ""

            def wait(self):
                captured["waited"] = True

        def install(signum, handler):
            captured["handler"] = handler
            return signal.SIG_DFL

        with patch("harness.goose.subprocess.Popen", return_value=Process()), \
             patch("harness.goose.signal.signal", side_effect=install), \
             patch("harness.goose.os.killpg") as killpg:
            result = GooseHarness(availability=Availability.READY).draft(request)
        self.assertEqual(result.state, DraftState.FAILED)
        killpg.assert_called_once_with(123, signal.SIGTERM)
        self.assertTrue(captured["waited"])

    def test_goose_draft_launch_failure_is_bounded_and_provenanced(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {})
        with patch(
            "harness.goose.subprocess.Popen",
            side_effect=OSError("goose launch failed"),
        ):
            result = GooseHarness(availability=Availability.READY).draft(request)
        self.assertEqual(result.state, DraftState.FAILED)
        self.assertEqual(result.provenance["backend"], "goose")
        self.assertIn("goose launch failed", "\n".join(result.raw_evidence))
        self.assertLessEqual(len(result.raw_evidence), 400)

    def test_goose_draft_runtime_failure_is_bounded_and_provenanced(self):
        request = DraftRequest(self.binding, "comment", self._evidence(), {})

        class Process:
            pid = 123

            def communicate(self):
                raise subprocess.SubprocessError("goose runtime failed")

        with patch("harness.goose.subprocess.Popen", return_value=Process()):
            result = GooseHarness(availability=Availability.READY).draft(request)
        self.assertEqual(result.state, DraftState.FAILED)
        self.assertEqual(result.provenance["backend"], "goose")
        self.assertIn("goose runtime failed", "\n".join(result.raw_evidence))
        self.assertLessEqual(len(result.raw_evidence), 400)

    def test_codex_draft_strips_github_tokens_from_subprocess_environment(self):
        request = DraftRequest(self.binding, "approve", self._evidence(), {"title": "A PR"})
        captured = {}

        class Process:
            stdout = "Reviewed."
            returncode = 0

        def run(*args, **kwargs):
            captured.update(kwargs)
            return Process()

        with patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret"}), \
             patch("harness.codex.subprocess.run", side_effect=run):
            result = CodexHarness(availability=Availability.READY).draft(request)
        self.assertEqual(result.state, DraftState.COMPLETE)
        self.assertNotIn("GH_TOKEN", captured["env"])
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertEqual(captured["env"]["PATH"], os.environ["PATH"])

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


class _FakeResponse:
    """Minimal urlopen response double with bounded-read inspection."""

    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            return self.body
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _fake_urlopen(routes, calls):
    """Build an urlopen double serving `routes` (path -> outcome list).

    Each call records (path, timeout) in `calls`; when a path's list holds
    more than one outcome the head is popped, so the last queued outcome
    repeats. An exception outcome is raised instead of returned.
    """

    def fake(request, timeout=None):
        parts = urlsplit(request.full_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        calls.append((path, timeout))
        outcomes = routes[path]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake


class _OkHandler(http.server.BaseHTTPRequestHandler):
    """Serve 200 for any GET and record the request path on the server."""

    def do_GET(self):
        self.server.hits.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RedirectHandler(_OkHandler):
    """Answer every GET with a 302 to the server's `location` target."""

    def do_GET(self):
        self.server.hits.append(self.path)
        self.send_response(302)
        self.send_header("Location", self.server.location)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler):
    """Run a loopback HTTP server on an ephemeral port; caller shuts it down."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.hits = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class HeadroomContract(unittest.TestCase):
    STATS_PAYLOAD = {
        "requests": {"total": 7},
        "tokens": {
            "saved": 1200,
            "output_saved": 80,
            "output_reduction": {
                "available": True,
                "method": "estimated",
                "reduction_percent": 12.5,
            },
        },
    }

    ENV = {"BLUEFIN_REVIEW_HEADROOM_URL": "http://127.0.0.1:8787"}

    def _stats(self, payload):
        calls = []
        response = _FakeResponse(payload)
        with patch("urllib.request.urlopen", _fake_urlopen({"/stats?cached=1": [response]}, calls)):
            stats = HeadroomClient("http://127.0.0.1:8787").stats()
        return stats, calls, response

    def test_ready_probe_is_bounded_and_subsecond(self):
        calls = []
        response = _FakeResponse(b"ok")
        with patch("urllib.request.urlopen", _fake_urlopen({"/readyz": [response]}, calls)):
            client = HeadroomClient("http://127.0.0.1:8787")
            self.assertTrue(client.ready())
            self.assertEqual(client.requested_paths, ["/readyz"])
        self.assertEqual(calls, [("/readyz", 0.5)])
        self.assertLess(calls[0][1], 1)
        self.assertEqual(response.read_sizes, [65_537])

    def test_stats_parse_delta_and_bounds(self):
        stats, calls, response = self._stats(self.STATS_PAYLOAD)
        self.assertEqual(stats.requests, 7)
        self.assertEqual(stats.tokens_saved, 1200)
        self.assertEqual(stats.output_tokens_saved, 80)
        self.assertEqual(stats.output_reduction_percent, 12.5)
        self.assertEqual(stats.output_reduction_method, "estimated")
        self.assertEqual(calls, [("/stats?cached=1", 0.5)])
        self.assertEqual(response.read_sizes, [65_537])
        baseline = HeadroomStats(5, 900, 60, None, None)
        self.assertEqual(stats.delta_from(baseline).requests, 2)
        self.assertEqual(stats.delta_from(baseline).tokens_saved, 300)
        self.assertEqual(stats.delta_from(baseline).output_tokens_saved, 20)

    def test_requested_paths_track_probe_order(self):
        calls = []
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(self.STATS_PAYLOAD)],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, calls)):
            client = HeadroomClient("http://127.0.0.1:8787")
            self.assertTrue(client.ready())
            client.stats()
            self.assertEqual(client.requested_paths, ["/readyz", "/stats?cached=1"])

    def test_output_reduction_absent_or_unavailable_is_none(self):
        for payload in (
            {"requests": {"total": 1}, "tokens": {"saved": 2, "output_saved": 3}},
            {
                "requests": {"total": 1},
                "tokens": {"saved": 2, "output_saved": 3, "output_reduction": {"available": False}},
            },
        ):
            with self.subTest(payload=payload):
                stats, _, _ = self._stats(payload)
                self.assertIsNone(stats.output_reduction_percent)
                self.assertIsNone(stats.output_reduction_method)

    def test_all_documented_reduction_methods_are_accepted(self):
        for method in ("measured", "estimated", "modelled"):
            with self.subTest(method=method):
                payload = {
                    "requests": {"total": 1},
                    "tokens": {
                        "saved": 2,
                        "output_saved": 3,
                        "output_reduction": {
                            "available": True,
                            "method": method,
                            "reduction_percent": 0,
                        },
                    },
                }
                stats, _, _ = self._stats(payload)
                self.assertEqual(stats.output_reduction_method, method)
                self.assertEqual(stats.output_reduction_percent, 0.0)

    def test_malformed_stats_payloads_are_rejected(self):
        def with_reduction(reduction):
            return {
                "requests": {"total": 1},
                "tokens": {"saved": 2, "output_saved": 3, "output_reduction": reduction},
            }

        payloads = {
            "not json": b"not json",
            "json array": [1, 2],
            "missing requests section": {"tokens": {"saved": 2, "output_saved": 3}},
            "requests not an object": {"requests": [7], "tokens": {"saved": 2, "output_saved": 3}},
            "missing total": {"requests": {}, "tokens": {"saved": 2, "output_saved": 3}},
            "boolean counter": {"requests": {"total": True}, "tokens": {"saved": 2, "output_saved": 3}},
            "float counter": {"requests": {"total": 1.5}, "tokens": {"saved": 2, "output_saved": 3}},
            "negative counter": {"requests": {"total": -1}, "tokens": {"saved": 2, "output_saved": 3}},
            "unknown method": with_reduction(
                {"available": True, "method": "guessed", "reduction_percent": 1.0}
            ),
            "non-numeric percent": with_reduction(
                {"available": True, "method": "measured", "reduction_percent": "12.5"}
            ),
            "boolean percent": with_reduction(
                {"available": True, "method": "measured", "reduction_percent": True}
            ),
            "negative percent": with_reduction(
                {"available": True, "method": "measured", "reduction_percent": -1.0}
            ),
            "non-boolean available": with_reduction({"available": "yes"}),
            "nan percent": (
                b'{"requests":{"total":1},"tokens":{"saved":2,"output_saved":3,'
                b'"output_reduction":{"available":true,"method":"measured",'
                b'"reduction_percent":NaN}}}'
            ),
            "infinity percent": (
                b'{"requests":{"total":1},"tokens":{"saved":2,"output_saved":3,'
                b'"output_reduction":{"available":true,"method":"measured",'
                b'"reduction_percent":Infinity}}}'
            ),
            "negative infinity percent": (
                b'{"requests":{"total":1},"tokens":{"saved":2,"output_saved":3,'
                b'"output_reduction":{"available":true,"method":"measured",'
                b'"reduction_percent":-Infinity}}}'
            ),
            "deeply nested": b'{"a":' * 32 + b"1" + b"}" * 32,
            "oversized": (
                b'{"requests":{"total":1},"tokens":{"saved":2,"output_saved":3},'
                b'"pad":"' + b"x" * 65_536 + b'"}'
            ),
        }
        for name, payload in payloads.items():
            with self.subTest(name=name), self.assertRaises(HeadroomError):
                self._stats(payload)

    def test_non_finite_reduction_percent_fails_numeric_validation(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            payload = (
                b'{"requests":{"total":1},"tokens":{"saved":2,"output_saved":3,'
                b'"output_reduction":{"available":true,"method":"measured",'
                b'"reduction_percent":' + token + b"}}}"
            )
            with self.subTest(token=token):
                with self.assertRaises(HeadroomError) as caught:
                    self._stats(payload)
                self.assertIn("finite", str(caught.exception))

    def test_delta_rejects_counter_decreases(self):
        baseline = HeadroomStats(7, 1200, 80, None, None)
        for current in (
            HeadroomStats(6, 1300, 90, None, None),
            HeadroomStats(8, 1100, 90, None, None),
            HeadroomStats(8, 1300, 70, None, None),
        ):
            with self.subTest(current=current), self.assertRaises(HeadroomError):
                current.delta_from(baseline)

    def test_client_normalizes_only_launcher_loopback(self):
        self.assertEqual(HeadroomClient("http://127.0.0.1:8787/").base_url, "http://127.0.0.1:8787")
        self.assertEqual(HeadroomClient("http://127.0.0.1:1").base_url, "http://127.0.0.1:1")
        for url in (
            "https://127.0.0.1:8787",
            "http://localhost:8787",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://user@127.0.0.1:8787",
            "http://127.0.0.1:8787/path",
            "http://127.0.0.1:8787?x=1",
            "http://127.0.0.1:8787#frag",
            "127.0.0.1:8787",
        ):
            with self.subTest(url=url), self.assertRaises(HeadroomError):
                HeadroomClient(url)

    def test_redirect_is_rejected_and_never_followed(self):
        target = _serve(_OkHandler)
        redirector = _serve(_RedirectHandler)
        self.addCleanup(target.shutdown)
        self.addCleanup(target.server_close)
        self.addCleanup(redirector.shutdown)
        self.addCleanup(redirector.server_close)
        redirector.location = f"http://127.0.0.1:{target.server_port}/readyz"
        client = HeadroomClient(f"http://127.0.0.1:{redirector.server_port}")
        self.assertFalse(client.ready())
        with self.assertRaises(HeadroomError) as caught:
            client.stats()
        self.assertIn("HTTP 302", str(caught.exception))
        self.assertEqual(target.hits, [])
        self.assertEqual(redirector.hits, ["/readyz", "/stats?cached=1"])

    def test_http_proxy_environment_is_ignored(self):
        proxy = _serve(_OkHandler)
        origin = _serve(_OkHandler)
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        self.addCleanup(origin.shutdown)
        self.addCleanup(origin.server_close)
        proxy_env = {
            "http_proxy": f"http://127.0.0.1:{proxy.server_port}",
            "HTTP_PROXY": f"http://127.0.0.1:{proxy.server_port}",
        }
        with patch.dict(os.environ, proxy_env):
            client = HeadroomClient(f"http://127.0.0.1:{origin.server_port}")
            self.assertTrue(client.ready())
        self.assertEqual(proxy.hits, [])
        self.assertEqual(origin.hits, ["/readyz"])

    def test_session_routing_and_status(self):
        calls = []
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(self.STATS_PAYLOAD)],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, calls)):
            session = HeadroomSession.from_environment(self.ENV)
            self.assertEqual(session.refresh("goose").state, "DIRECT")
            self.assertEqual(calls, [])
            self.assertIn("Goose/GitHub Copilot", session.status_line("goose", False))
            self.assertEqual(session.refresh("codex").state, "ACTIVE")
            self.assertEqual(session.route_for_call("codex").base_url, "http://127.0.0.1:8787")

    def test_telemetry_exposes_route_and_aggregate_output_reduction(self):
        calls = []
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(self.STATS_PAYLOAD)],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, calls)):
            session = HeadroomSession.from_environment(self.ENV)
            self.assertEqual(session.refresh("codex").state, "ACTIVE")
            self.assertEqual(
                session.route_for_call("codex").base_url,
                "http://127.0.0.1:8787",
            )
            telemetry = session.telemetry("codex")
        self.assertEqual(telemetry["state"], "ACTIVE")
        self.assertEqual(telemetry["route"], "http://127.0.0.1:8787")
        self.assertEqual(telemetry["requests"], 0)
        self.assertEqual(telemetry["tokens_saved"], 0)
        self.assertEqual(telemetry["output_tokens_saved"], 0)
        self.assertEqual(
            telemetry["output_reduction_percent"],
            self.STATS_PAYLOAD["tokens"]["output_reduction"][
                "reduction_percent"
            ],
        )
        self.assertEqual(
            telemetry["output_reduction_method"],
            self.STATS_PAYLOAD["tokens"]["output_reduction"]["method"],
        )
        self.assertFalse(telemetry["statistics_degraded"])
        self.assertIn("proxy delta", telemetry["status_line"])

    def test_invalid_configured_url_is_degraded_not_fatal(self):
        calls = []
        with patch("urllib.request.urlopen", _fake_urlopen({}, calls)):
            session = HeadroomSession.from_environment(
                {"BLUEFIN_REVIEW_HEADROOM_URL": "https://headroom.invalid:8787"}
            )
            self.assertIn("DEGRADED", session.status_line("codex", False))
            route = session.refresh("codex")
            self.assertEqual(route.state, "DEGRADED")
            self.assertIsNone(route.base_url)
            self.assertIsNone(session.route_for_call("codex").base_url)
            line = session.status_line("codex", False)
            self.assertIn("DEGRADED", line)
            self.assertIn("degraded", line)
            self.assertEqual(session.refresh("goose").state, "DIRECT")
        self.assertEqual(calls, [])

    def test_unavailable_proxy_is_degraded_and_absent_url_is_direct(self):
        calls = []
        routes = {"/readyz": [urllib.error.URLError("refused")]}
        with patch("urllib.request.urlopen", _fake_urlopen(routes, calls)):
            session = HeadroomSession.from_environment(self.ENV)
            route = session.refresh("codex")
            self.assertEqual(route.state, "DEGRADED")
            self.assertIsNone(route.base_url)
            self.assertIsNone(session.route_for_call("codex").base_url)
            self.assertIn("degraded", session.status_line("codex", False))

        calls = []
        with patch("urllib.request.urlopen", _fake_urlopen({}, calls)):
            session = HeadroomSession.from_environment({})
            route = session.refresh("codex")
            self.assertEqual(route.state, "DIRECT")
            self.assertIsNone(route.base_url)
            self.assertIsNone(session.route_for_call("codex").base_url)
        self.assertEqual(calls, [])

    def test_stats_failure_preserves_route_and_marks_statistics_degraded(self):
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(b"not json")],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, [])):
            session = HeadroomSession.from_environment(self.ENV)
            route = session.refresh("codex")
            self.assertEqual(route.state, "ACTIVE")
            self.assertEqual(route.base_url, "http://127.0.0.1:8787")
            self.assertIn("degraded", session.status_line("codex", False))

    def test_counter_decrease_keeps_route_and_degrades_statistics(self):
        first = {"requests": {"total": 9}, "tokens": {"saved": 100, "output_saved": 10}}
        reset = {"requests": {"total": 1}, "tokens": {"saved": 5, "output_saved": 1}}
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(first), _FakeResponse(reset)],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, [])):
            session = HeadroomSession.from_environment(self.ENV)
            self.assertEqual(session.refresh("codex").state, "ACTIVE")
            self.assertIn("proxy delta", session.status_line("codex", False))
            self.assertEqual(session.refresh("codex").state, "ACTIVE")
            self.assertIn("degraded", session.status_line("codex", False))

    def test_status_line_labels_numbers_as_proxy_delta(self):
        first = {"requests": {"total": 5}, "tokens": {"saved": 900, "output_saved": 60}}
        routes = {
            "/readyz": [_FakeResponse(b"ok")],
            "/stats?cached=1": [_FakeResponse(first), _FakeResponse(self.STATS_PAYLOAD)],
        }
        with patch("urllib.request.urlopen", _fake_urlopen(routes, [])):
            session = HeadroomSession.from_environment(self.ENV)
            session.refresh("codex")
            session.refresh("codex")
            line = session.status_line("codex", False)
            self.assertIn("proxy delta", line)
            self.assertIn("2 requests", line)
            self.assertIn("300", line)
            self.assertIn("20", line)
            self.assertNotIn("per-review", line)
            self.assertEqual(line.count("\n"), 0)

    def test_apply_caveman_appends_policy_exactly_once(self):
        policy = (
            "Minimum tokens. Fragments fine. No preamble, no postamble, no restating "
            "context, no rationale. Answer, smallest-possible edits, nothing else. "
            "Never drop anything the turn or task needs to be correct, including "
            "negations (not, never, no, only, except). Use full prose for destructive "
            "or irreversible actions, security warnings, and any multi-step sequence "
            "where brevity would create ambiguity."
        )
        self.assertEqual(CAVEMAN_INSTRUCTIONS, policy)
        enabled = apply_caveman("review contract", True)
        self.assertTrue(enabled.startswith("review contract"))
        self.assertTrue(enabled.endswith(policy))
        self.assertEqual(enabled.count(policy), 1)
        self.assertEqual(apply_caveman(enabled, True), enabled)
        self.assertEqual(apply_caveman("review contract", False), "review contract")
        self.assertNotIn(policy, apply_caveman("review contract", False))


if __name__ == "__main__":
    unittest.main()
