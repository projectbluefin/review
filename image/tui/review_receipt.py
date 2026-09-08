from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.codex import CodexHarness
from harness.goose import GooseHarness
from harness.registry import Availability, HarnessRegistry
from tui.headroom import HeadroomSession, apply_caveman
from tui.review_evidence_manifest import ReviewRequest
from tui.review_result import ReviewResult
from tui.review_run import ReviewRun

RECEIPT_VERSION = 1
MAX_TRANSCRIPT_LINES = 200
MAX_TRANSCRIPT_CHARS = 60_000
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
BACKENDS = frozenset({"goose", "codex"})
MUTABLE_PROVENANCE_KEYS = frozenset({
    "ci",
    "checks",
    "mergeability",
    "reviews",
    "live",
    "overlap",
})


def _clean_provenance(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in mapping.items()
        if key not in MUTABLE_PROVENANCE_KEYS
    }


def _analysis_only(result: ReviewResult, provenance: Mapping[str, Any]) -> ReviewResult:
    """Copy a result down to its durable analysis, with live context dropped."""
    return replace(
        result,
        counts=dict(result.counts),
        findings=[dict(item) for item in result.findings],
        verification=[dict(item) for item in result.verification],
        provenance=dict(provenance),
        overlap={},
        live={},
        raw_evidence=[],
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty exact string")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value):
        raise ValueError(f"{field} must be a full lowercase SHA")
    return value


def _bounded_transcript(lines: Sequence[str]) -> tuple[str, ...]:
    kept: list[str] = []
    chars = 0
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("transcript lines must be strings")
        if len(kept) == MAX_TRANSCRIPT_LINES:
            break
        remaining = MAX_TRANSCRIPT_CHARS - chars
        if remaining <= 0:
            break
        value = line[:remaining]
        kept.append(value)
        chars += len(value)
        if len(value) != len(line):
            break
    return tuple(kept)


def cache_digest(run: ReviewRun | str, check_scope_version: str) -> str:
    run_id = run.identity if isinstance(run, ReviewRun) else run
    material = f"{run_id}\0{check_scope_version}"
    return sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReceiptIdentity:
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    backend: str
    model: str
    effort: str
    check_scope_version: str

    @classmethod
    def from_run(cls, run: ReviewRun, check_scope_version: str) -> "ReceiptIdentity":
        if run.backend not in BACKENDS:
            raise ValueError(f"unsupported review backend: {run.backend}")
        if isinstance(run.pull_request, bool) or run.pull_request < 1:
            raise ValueError("pull_request must be positive")
        return cls(
            run.repository,
            run.pull_request,
            _sha(run.base_sha, "base_sha"),
            _sha(run.head_sha, "head_sha"),
            _text(run.backend, "backend"),
            _text(run.model, "model"),
            _text(run.effort, "effort"),
            _text(check_scope_version, "check_scope_version"),
        )

    @property
    def run_identity(self) -> str:
        run = ReviewRun(
            self.repository,
            self.pull_request,
            self.base_sha,
            self.head_sha,
            self.base_sha[:12] + self.head_sha[:12],
            self.backend,
            self.model,
            self.effort,
        )
        return run.identity

    @property
    def cache_identity(self) -> str:
        return cache_digest(self.run_identity, self.check_scope_version)

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pull_request": self.pull_request,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
            "check_scope_version": self.check_scope_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ReceiptIdentity":
        if not isinstance(data, Mapping):
            raise ValueError("receipt identity must be an object")
        backend = _text(data.get("backend"), "backend")
        if backend not in BACKENDS:
            raise ValueError(f"unsupported review backend: {backend}")
        number = data.get("pull_request")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("receipt pull_request must be positive")
        return cls(
            _text(data.get("repository"), "repository"),
            number,
            _sha(data.get("base_sha"), "base_sha"),
            _sha(data.get("head_sha"), "head_sha"),
            backend,
            _text(data.get("model"), "model"),
            _text(data.get("effort"), "effort"),
            _text(data.get("check_scope_version"), "check_scope_version"),
        )


@dataclass(frozen=True)
class ReviewReceipt:
    version: int
    identity: ReceiptIdentity
    analysis: ReviewResult
    transcript: tuple[str, ...]
    provenance: dict[str, Any]
    created_at: str

    @classmethod
    def from_result(
        cls,
        run: ReviewRun,
        result: ReviewResult,
        transcript: Sequence[str],
        check_scope_version: str,
        provenance: Mapping[str, Any] | None = None,
    ) -> "ReviewReceipt":
        identity = ReceiptIdentity.from_run(run, check_scope_version)
        if result.state == "unparsable":
            raise ValueError("an unparsable result cannot become a receipt")
        recorded = _clean_provenance(result.provenance)
        recorded.update(_clean_provenance(provenance or {}))
        recorded.update({
            "repository": identity.repository,
            "pull_request": identity.pull_request,
            "base_sha": identity.base_sha,
            "head_sha": identity.head_sha,
            "backend": identity.backend,
            "model": identity.model,
            "effort": identity.effort,
            "check_scope_version": identity.check_scope_version,
        })
        analysis = _analysis_only(result, recorded)
        return cls(
            RECEIPT_VERSION,
            identity,
            analysis,
            _bounded_transcript(transcript),
            dict(recorded),
            datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "identity": self.identity.to_dict(),
            "analysis": self.analysis.to_dict(),
            "transcript": list(self.transcript),
            "provenance": dict(self.provenance),
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewReceipt":
        if not isinstance(data, Mapping) or data.get("version") != RECEIPT_VERSION:
            raise ValueError("unsupported review receipt version")
        identity = ReceiptIdentity.from_dict(data.get("identity", {}))
        analysis = ReviewResult.from_dict(data.get("analysis", {}))
        if analysis.state == "unparsable":
            raise ValueError("receipt analysis is unparsable")
        transcript = _bounded_transcript(data.get("transcript", []))
        provenance = data.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("receipt provenance must be an object")
        created_at = _text(data.get("created_at"), "created_at")
        expected = {
            "repository": identity.repository,
            "pull_request": identity.pull_request,
            "base_sha": identity.base_sha,
            "head_sha": identity.head_sha,
            "backend": identity.backend,
            "model": identity.model,
            "effort": identity.effort,
            "check_scope_version": identity.check_scope_version,
        }
        if any(provenance.get(key) != value for key, value in expected.items()):
            raise ValueError("receipt provenance does not match its identity")
        if any(key in provenance for key in MUTABLE_PROVENANCE_KEYS):
            raise ValueError("receipt provenance must not contain mutable evidence")
        if any(key in analysis.provenance for key in MUTABLE_PROVENANCE_KEYS):
            raise ValueError("receipt analysis provenance must not contain mutable evidence")
        if analysis.live or analysis.overlap:
            raise ValueError("receipt must not contain mutable evidence")
        return cls(RECEIPT_VERSION, identity, analysis, transcript, dict(provenance), created_at)

    @classmethod
    def from_json(cls, payload: str) -> "ReviewReceipt":
        if not isinstance(payload, str) or len(payload) > 200_000:
            raise ValueError("receipt JSON is missing or too large")
        try:
            value = json.loads(payload)
            return cls.from_dict(value)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("receipt JSON is invalid") from error

    def analysis_result(
        self,
        live: Mapping[str, Any] | None = None,
        overlap: Mapping[str, Any] | None = None,
    ) -> ReviewResult:
        return ReviewResult(
            self.analysis.version,
            self.analysis.state,
            dict(self.analysis.counts),
            [dict(item) for item in self.analysis.findings],
            [dict(item) for item in self.analysis.verification],
            dict(self.analysis.provenance),
            dict(overlap or {}),
            dict(live or {}),
            list(self.analysis.raw_evidence),
        )

    def with_provenance(self, extra: Mapping[str, Any]) -> "ReviewReceipt":
        provenance = dict(self.provenance)
        provenance.update(_clean_provenance(extra))
        return replace(
            self,
            analysis=_analysis_only(self.analysis, provenance),
            provenance=provenance,
        )


def default_harness_registry() -> HarnessRegistry:
    registry = HarnessRegistry()
    registry.register(GooseHarness(availability=GooseHarness.probe()))
    registry.register(CodexHarness(availability=CodexHarness.probe()))
    return registry


def _terminal_status(adapter: Any, result: ReviewResult) -> int:
    if hasattr(adapter, "terminal_status") and callable(adapter.terminal_status):
        return int(adapter.terminal_status(result))
    if result.state in ("complete", "findings"):
        return 0
    if result.state == "incomplete":
        return 65
    return int(result.live.get("process_exit_code", 1)) or 1


def _check_scope_args(check_scope: str) -> tuple[str, ...]:
    return ("--check-scope", check_scope) if check_scope else ()


def run_receipt(
    repository: str,
    pull_request: int,
    base_sha: str,
    head_sha: str,
    backend: str,
    model: str,
    effort: str,
    workdir: str,
    check_scope_version: str,
    check_scope: str = "",
    steer: str = "",
    registry: HarnessRegistry | None = None,
) -> tuple[ReviewReceipt, int]:
    if backend not in BACKENDS:
        raise ValueError(f"unsupported review backend: {backend}")
    owner, name = repository.split("/", 1)
    request = ReviewRequest(
        owner,
        name,
        pull_request,
        base_sha,
        head_sha,
        "maintainer",
        "review",
        generated_at="bluefin-review-receipt",
        steering=steer,
    )
    run = ReviewRun.from_request(request, backend=backend, model=model, effort=effort)
    if workdir:
        os.chdir(workdir)

    if registry is None:
        registry = default_harness_registry()
    adapter = registry.require_ready(backend)

    headroom = HeadroomSession.from_environment()
    route = headroom.route_for_call(backend)
    diff_range = f"{base_sha}...{head_sha}"
    prompt = apply_caveman(
        f"Review the exact binding by inspecting git diff {diff_range}. "
        "Return only the backend's structured ReviewResult; "
        "use compact findings with file and line evidence and no prose padding.",
        True,
    )
    transcript: list[str] = []
    extra_args = _check_scope_args(check_scope) + (diff_range,)
    result = adapter.stream(
        request,
        prompt=prompt,
        on_line=transcript.append,
        model=model,
        effort=effort,
        steer=steer or None,
        extra_args=extra_args,
    )
    exit_code = _terminal_status(adapter, result)
    receipt = ReviewReceipt.from_result(
        run,
        result,
        transcript,
        check_scope_version,
        {
            "headroom_status_line": headroom.status_line(backend, True),
            "headroom_state": route.state,
            "headroom_route": route.base_url or "",
        },
    )
    return receipt, exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bluefin-review receipt")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--check-scope-version", required=True)
    parser.add_argument("--check-scope", default="")
    parser.add_argument("--steer", default="")
    args = parser.parse_args(argv)
    receipt, exit_code = run_receipt(
        args.repository,
        args.pull_request,
        args.base_sha,
        args.head_sha,
        args.backend,
        args.model,
        args.effort,
        args.workdir,
        args.check_scope_version,
        args.check_scope,
        args.steer,
    )
    print(receipt.to_json())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
