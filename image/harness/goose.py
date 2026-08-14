"""Goose review adapter for the shared harness contract."""

import os
import re
import signal
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from tui.review_evidence_manifest import ReviewRequest
from tui.review_result import ReviewResult, adapt_current_engine

from .registry import (
    Availability,
    DraftRequest,
    DraftResult,
    DraftState,
    HarnessBranding,
    HarnessCapabilities,
)


@dataclass
class GooseHarness:
    name: str = "goose"
    branding: HarnessBranding = HarnessBranding(
        "goose", "Goose", "GS", "Goose", "aaif-goose/goose", None
    )
    model: str = "gpt-5.6-luna"
    effort: str = "high"
    availability: Availability = Availability.READY
    executable: str = "goose"
    capabilities: HarnessCapabilities = HarnessCapabilities(
        binary_readiness=True, auth_preflight=True, invocation=True,
        exact_binding=True, model_effort=True, steering=True,
        streaming=True, cancellation=True, result_conversion=True,
        provenance=True, body_drafting=False,
    )
    process_group_cancellation = True

    def command(self, binding: ReviewRequest, *, prompt: str, model: str | None = None,
                effort: str | None = None, steer: str | None = None,
                extra_args: tuple[str, ...] = ()) -> list[str]:
        selected_model = model or self.model
        selected_effort = effort or self.effort
        context = (
            f"{binding.owner}/{binding.repository}#{binding.pull_request_number} "
            f"base={binding.base_sha} head={binding.head_sha}"
        )
        instruction = (
            f"Review exact binding {context}. {prompt} "
            f"Use model {selected_model} with reasoning effort {selected_effort}."
        )
        if steer:
            instruction += f" Maintainer steering: {steer}"
        command = [self.executable, "review"]
        if prompt or steer:
            command.extend(("--instructions", instruction))
        command.extend(extra_args)
        return command

    @staticmethod
    def _redact(value: str) -> str:
        secrets = [os.environ.get(key, "") for key in (
            "GH_TOKEN", "GITHUB_TOKEN", "REVIEW_GH_TOKEN", "GOOSE_API_KEY",
        )]
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(Bearer\s+)[^\s]+", r"\1[REDACTED]", value, flags=re.I)

    def convert(self, payload: str, binding: ReviewRequest, exit_code: int = 0,
                *, model: str | None = None, effort: str | None = None) -> ReviewResult:
        selected_model = model or self.model
        selected_effort = effort or self.effort
        result = adapt_current_engine(
            self._redact(payload), exit_code,
            {"backend": self.name, "model": selected_model,
             "repository": f"{binding.owner}/{binding.repository}",
             "pull_request": binding.pull_request_number,
             "base_sha": binding.base_sha, "head_sha": binding.head_sha,
             "reasoning_effort": selected_effort},
        )
        return result

    def stream(self, binding: ReviewRequest, *, prompt: str,
               on_line: Callable[[str], None], model: str | None = None,
               effort: str | None = None, steer: str | None = None,
               extra_args: tuple[str, ...] = ()) -> ReviewResult:
        process = subprocess.Popen(
            self.command(binding, prompt=prompt, model=model, effort=effort,
                         steer=steer, extra_args=extra_args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True,
        )
        lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = self._redact(line.rstrip("\n"))
            lines.append(line)
            on_line(line)
        process.stdout.close()
        process.wait()
        result = self.convert("\n".join(lines), binding, process.returncode,
                              model=model, effort=effort)
        return ReviewResult(
            result.version, result.state, result.counts, result.findings,
            result.verification, result.provenance, result.overlap,
            {**result.live, "exit_code": process.returncode},
            result.raw_evidence,
        )

    @staticmethod
    def cancel(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def invoke(self, binding: ReviewRequest, *, prompt: str, model: str | None = None,
               effort: str | None = None, steer: str | None = None,
               extra_args: tuple[str, ...] = ()) -> ReviewResult:
        if self.availability is not Availability.READY:
            raise RuntimeError(f"{self.name} unavailable: {self.availability.value}")
        return self.stream(binding, prompt=prompt, on_line=lambda _line: None,
                           model=model, effort=effort, steer=steer,
                           extra_args=extra_args)

    @classmethod
    def probe(cls, executable: str = "goose") -> Availability:
        resolved = shutil.which(executable)
        if resolved is None:
            return Availability.UNAVAILABLE_BINARY
        try:
            check = subprocess.run(
                [resolved, "info", "--check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, check=False,
            )
        except OSError:
            return Availability.UNAVAILABLE_BINARY
        return Availability.READY if check.returncode == 0 else Availability.UNAVAILABLE_AUTH

    def draft(self, request: DraftRequest) -> DraftResult:
        raise RuntimeError(f"{self.name} unavailable: UNSUPPORTED_CAPABILITY")
