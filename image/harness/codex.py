"""Codex CLI adapter contract; it never substitutes another harness."""

import json
import os
import signal
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from tui.review_evidence_manifest import ReviewRequest
from .registry import Availability, DraftRequest, DraftResult, DraftState, HarnessBranding, HarnessCapabilities

try:
    from tui.review_result import ReviewResult, parse_review_result
except ImportError:
    from image.tui.review_result import ReviewResult, parse_review_result


@dataclass
class CodexHarness:
    name: str = "codex"
    branding: HarnessBranding = HarnessBranding(
        "codex", "Codex CLI", "CX", "OpenAI Codex CLI", "openai/codex", None
    )
    model: str = "gpt-5.6-luna"
    effort: str = "low"
    availability: Availability = Availability.UNAVAILABLE_BINARY
    executable: str = "codex"
    capabilities: HarnessCapabilities = HarnessCapabilities(
        binary_readiness=True, auth_preflight=True, invocation=True,
        exact_binding=True, model_effort=True, steering=True,
        streaming=True, cancellation=True, result_conversion=True,
        provenance=True, body_drafting=True,
    )

    SUPPORTED_EFFORTS = ("low", "medium", "high", "max")
    process_group_cancellation = True

    @staticmethod
    def validate_draft(request: DraftRequest) -> DraftResult:
        return DraftResult(DraftState.COMPLETE, provenance={
            "backend": "codex", "model": "gpt-5.6-luna", "effort": "low",
            "repository": f"{request.binding.owner}/{request.binding.repository}",
            "pull_request": request.binding.pull_request_number,
            "base_sha": request.binding.base_sha, "head_sha": request.binding.head_sha,
        })

    def draft_command(self, request: DraftRequest) -> list[str]:
        self.validate_draft(request)
        evidence = json.dumps({"result": request.evidence.to_dict(), "live": dict(request.live_facts)}, sort_keys=True, separators=(",", ":"))
        prompt = (f"Draft one concise Markdown review body for verdict {request.verdict}. "
                  f"Use only this bounded evidence: {evidence} "
                  "Do not perform another code review. Do not discover or invent findings. "
                  "Do not mutate GitHub, submit a review, or change repository state. "
                  "Return only Markdown text, at most 4096 characters.")
        return [self.executable, "exec", "--ignore-user-config", "--disable", "apps",
                "--config", "mcp_servers={}", "--model", self.model,
                "--config", f"model_reasoning_effort={self.effort}",
                "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", prompt]

    def convert_draft(self, payload: str, request: DraftRequest, exit_code: int = 0) -> DraftResult:
        self.validate_draft(request)
        raw = tuple(payload.splitlines()[:400])
        if exit_code != 0 or not isinstance(payload, str) or not payload.strip() or len(payload) > 4096:
            return DraftResult(DraftState.FAILED, provenance={"backend": self.name, "model": self.model, "effort": self.effort}, raw_evidence=raw)
        return DraftResult(DraftState.COMPLETE, payload.strip(), {
            "backend": self.name, "model": self.model, "effort": self.effort,
            "repository": f"{request.binding.owner}/{request.binding.repository}",
            "pull_request": request.binding.pull_request_number, "base_sha": request.binding.base_sha,
            "head_sha": request.binding.head_sha,
        }, raw)

    def draft(self, request: DraftRequest) -> DraftResult:
        if self.availability is not Availability.READY:
            raise RuntimeError(f"{self.name} unavailable: {self.availability.value}")
        process = subprocess.run(
            self.draft_command(request), capture_output=True, text=True, check=False,
        )
        return self.convert_draft(process.stdout, request, process.returncode)

    @classmethod
    def probe(cls, executable: str = "codex") -> Availability:
        if shutil.which(executable) is None:
            return Availability.UNAVAILABLE_BINARY
        check = subprocess.run(
            [executable, "login", "status"], capture_output=True, text=True,
            check=False,
        )
        return Availability.READY if check.returncode == 0 else Availability.UNAVAILABLE_AUTH

    def command(self, binding: ReviewRequest, *, prompt: str,
                effort: str | None = None, model: str | None = None,
                steer: str | None = None) -> list[str]:
        selected_effort = effort or self.effort
        selected_model = model or self.model
        if selected_effort not in self.SUPPORTED_EFFORTS:
            raise ValueError(f"unsupported Codex reasoning effort: {selected_effort}")
        context = (
            f"{binding.owner}/{binding.repository}#{binding.pull_request_number} "
            f"base={binding.base_sha} head={binding.head_sha}"
        )
        result_contract = (
            ' Return only one JSON object shaped as {"version":1,"state":"complete",'
            '"counts":{"critical":0,"high":0,"medium":0,"low":0},"findings":[]}.'
            ' Use state "findings" when findings exist; each finding requires severity, file,'
            ' line, and title, and counts must exactly match the findings. No Markdown.'
        )
        read_only_contract = (
            " This is a read-only review. Do not mutate GitHub, push commits,"
            " submit reviews, add comments, edit or merge pull requests, or change"
            " repository state."
        )
        return [self.executable, "exec", "--ignore-user-config", "--disable", "apps",
                "--config", "mcp_servers={}", "--json",
                "--enable", "code_mode_only", "--enable", "code_mode_host",
                "--config", "features.code_mode_host.disable_in_process_fallback=true",
                "--config", "suppress_unstable_features_warning=true",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--model", selected_model,
                "--config", f"model_reasoning_effort={selected_effort}",
                f"Review exact binding {context}. {prompt}"
                + (f" Maintainer steering: {steer}" if steer else "")
                + read_only_contract
                + result_contract]

    def convert(self, payload: str, binding: ReviewRequest, exit_code: int = 0,
                *, model: str | None = None, effort: str | None = None) -> ReviewResult:
        selected_model = model or self.model
        selected_effort = effort or self.effort
        if selected_effort not in self.SUPPORTED_EFFORTS:
            raise ValueError(f"unsupported Codex reasoning effort: {selected_effort}")
        lines = payload.splitlines()
        if exit_code != 0:
            raw_evidence = self._unparsable(lines).raw_evidence
            result = ReviewResult(1, "failed", raw_evidence=raw_evidence)
        else:
            result = self._convert_jsonl(lines)
        provenance = dict(result.provenance)
        provenance.update({
            "backend": "codex", "model": selected_model,
            "auth": "subscription-oauth", "repository": f"{binding.owner}/{binding.repository}",
            "pull_request": binding.pull_request_number, "base_sha": binding.base_sha,
            "head_sha": binding.head_sha, "reasoning_effort": selected_effort,
        })
        return ReviewResult(result.version, result.state, result.counts,
                            result.findings, result.verification, provenance,
                            result.overlap, result.live, result.raw_evidence)

    @staticmethod
    def _unparsable(lines: list[str]) -> ReviewResult:
        return parse_review_result("", raw_evidence=lines)

    @staticmethod
    def _convert_jsonl(lines: list[str]) -> ReviewResult:
        """Accept one complete official Codex JSONL turn and nothing else."""
        invalid = CodexHarness._unparsable(lines)
        events: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                return invalid
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                return invalid
            events.append(event)

        if len(events) < 4:
            return invalid
        thread_started = events[0]
        if (
            thread_started.get("type") != "thread.started"
            or not isinstance(thread_started.get("thread_id"), str)
            or not thread_started["thread_id"].strip()
            or events[1].get("type") != "turn.started"
        ):
            return invalid

        event_types = [event["type"] for event in events]
        if (
            event_types.count("thread.started") != 1
            or event_types.count("turn.started") != 1
            or event_types.count("turn.completed") != 1
            or event_types[-1] != "turn.completed"
            or any(
                event_type.startswith("turn.")
                and event_type not in {"turn.started", "turn.completed"}
                for event_type in event_types
            )
        ):
            return invalid
        if not isinstance(events[-1].get("usage"), dict):
            return invalid

        terminal_event = events[-2]
        terminal_item = terminal_event.get("item")
        if (
            terminal_event.get("type") != "item.completed"
            or not isinstance(terminal_item, dict)
            or terminal_item.get("type") != "agent_message"
            or not isinstance(terminal_item.get("id"), str)
            or not terminal_item["id"].strip()
            or not isinstance(terminal_item.get("text"), str)
        ):
            return invalid
        terminal = parse_review_result(terminal_item["text"])
        if terminal.state == "unparsable":
            return invalid

        result_positions: list[int] = []
        for index, event in enumerate(events):
            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
                and parse_review_result(item["text"]).state != "unparsable"
            ):
                result_positions.append(index)
        if result_positions != [len(events) - 2]:
            return invalid
        return terminal

    def stream(self, binding: ReviewRequest, *, prompt: str,
               on_line: Callable[[str], None], effort: str | None = None,
               model: str | None = None, steer: str | None = None) -> ReviewResult:
        process = subprocess.Popen(
            self.command(binding, prompt=prompt, effort=effort, model=model, steer=steer),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            bufsize=1, start_new_session=True,
        )
        lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            on_line(lines[-1])
        process.wait()
        return self.convert("\n".join(lines), binding, process.returncode,
                            model=model, effort=effort)

    @staticmethod
    def cancel(process: subprocess.Popen) -> None:
        os.killpg(process.pid, signal.SIGTERM)

    def invoke(self, binding: ReviewRequest, *, prompt: str, model: str | None = None,
               effort: str | None = None, steer: str | None = None) -> ReviewResult:
        if self.availability is not Availability.READY:
            raise RuntimeError(f"codex unavailable: {self.availability.value}")
        return self.stream(binding, prompt=prompt, on_line=lambda _line: None,
                           effort=effort, model=model, steer=steer)
