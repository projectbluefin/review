"""Codex CLI adapter contract; it never substitutes another harness."""

import os
import signal
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from tui.review_evidence_manifest import ReviewRequest
from .registry import Availability, HarnessBranding, HarnessCapabilities

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
        provenance=True,
    )

    SUPPORTED_EFFORTS = ("low", "medium", "high", "max")
    process_group_cancellation = True

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
        return [self.executable, "exec", "--json", "--model", selected_model,
                "--config", f"model_reasoning_effort={selected_effort}",
                f"Review exact binding {context}. {prompt}"
                + (f" Maintainer steering: {steer}" if steer else "")]

    def convert(self, payload: str, binding: ReviewRequest, exit_code: int = 0) -> ReviewResult:
        # ``codex exec --json`` is a JSONL progress stream; the final object
        # is the ReviewResult payload.
        result = parse_review_result(payload.splitlines()[-1] if payload.splitlines() else payload)
        if exit_code != 0:
            return ReviewResult(1, "failed", raw_evidence=payload.splitlines())
        provenance = dict(result.provenance)
        provenance.update({
            "backend": "codex", "model": self.model,
            "auth": "subscription-oauth", "repository": f"{binding.owner}/{binding.repository}",
            "pull_request": binding.pull_request_number, "base_sha": binding.base_sha,
            "head_sha": binding.head_sha, "reasoning_effort": self.effort,
        })
        return ReviewResult(result.version, result.state, result.counts,
                            result.findings, result.verification, provenance,
                            result.overlap, result.live, result.raw_evidence)

    def stream(self, binding: ReviewRequest, *, prompt: str,
               on_line: Callable[[str], None], effort: str | None = None,
               model: str | None = None, steer: str | None = None) -> ReviewResult:
        process = subprocess.Popen(
            self.command(binding, prompt=prompt, effort=effort, model=model, steer=steer),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True,
        )
        lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            on_line(lines[-1])
        process.wait()
        return self.convert("\n".join(lines), binding, process.returncode)

    @staticmethod
    def cancel(process: subprocess.Popen) -> None:
        os.killpg(process.pid, signal.SIGTERM)

    def invoke(self, binding: ReviewRequest, *, prompt: str, model: str | None = None,
               effort: str | None = None, steer: str | None = None) -> ReviewResult:
        if self.availability is not Availability.READY:
            raise RuntimeError(f"codex unavailable: {self.availability.value}")
        return self.stream(binding, prompt=prompt, on_line=lambda _line: None,
                           effort=effort, model=model, steer=steer)
