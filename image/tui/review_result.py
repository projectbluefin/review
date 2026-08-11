"""Backend-neutral, versioned result contract for the maintainer cockpit."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low")
STATES = ("complete", "findings", "incomplete", "failed", "unparsable")
MAX_RAW_LINES = 400
MAX_RAW_CHARS = 120_000
VERIFICATION_STATES = ("verified", "unverified", "skipped")


def _raw_lines(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.splitlines()
    if not isinstance(value, list) or any(not isinstance(line, str) for line in value):
        raise ValueError("raw evidence must be text or a list of text lines")
    return value


def _raw(value: str | list[str] | None) -> list[str]:
    lines = _raw_lines(value)
    text = "\n".join(str(line) for line in lines)
    return text[:MAX_RAW_CHARS].splitlines()[:MAX_RAW_LINES]


def _raw_truncated(value: str | list[str] | None) -> bool:
    lines = _raw_lines(value)
    return len(lines) > MAX_RAW_LINES or len("\n".join(lines)) > MAX_RAW_CHARS


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unparsable(raw_evidence: Any, payload: Any) -> "ReviewResult":
    try:
        evidence = _raw(raw_evidence if raw_evidence is not None else payload)
    except (TypeError, ValueError):
        try:
            evidence = _raw(payload)
        except (TypeError, ValueError):
            evidence = []
    return ReviewResult(1, "unparsable", raw_evidence=evidence)


@dataclass(frozen=True)
class ReviewResult:
    version: int
    state: str
    counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in SEVERITIES})
    findings: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    overlap: dict[str, Any] = field(default_factory=dict)
    live: dict[str, Any] = field(default_factory=dict)
    raw_evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewResult":
        if not isinstance(data, dict) or "counts" not in data or "findings" not in data:
            return cls(1, "unparsable")
        try:
            version = data["version"]
        except (KeyError, TypeError):
            return cls(1, "unparsable")
        state = data.get("state")
        if not _integer(version) or not isinstance(state, str):
            return cls(1, "unparsable")
        if version != 1 or state not in STATES:
            state = "unparsable"
        raw_counts = data["counts"]
        raw_findings = data["findings"]
        if not isinstance(raw_counts, dict) or not isinstance(raw_findings, list):
            return cls(1, "unparsable")
        if set(raw_counts) != set(SEVERITIES):
            return cls(1, "unparsable")
        try:
            counts = {severity: raw_counts.get(severity, 0) for severity in SEVERITIES}
        except (TypeError, ValueError):
            return cls(1, "unparsable")
        if any(not _integer(value) or value < 0 for value in counts.values()):
            return cls(1, "unparsable")
        findings = list(raw_findings)
        observed = {severity: 0 for severity in SEVERITIES}
        for finding in findings:
            if (
                not isinstance(finding, dict)
                or finding.get("severity") not in SEVERITIES
                or not _text(finding.get("file"))
                or not _integer(finding.get("line"))
                or finding["line"] < 1
                or not _text(finding.get("title"))
                or ("end_line" in finding and (
                    not _integer(finding["end_line"]) or finding["end_line"] < finding["line"]
                ))
            ):
                return cls(1, "unparsable")
            observed[finding["severity"]] += 1
        if counts != observed:
            state = "unparsable"
        if state == "complete" and findings:
            state = "findings"
        verification = data["verification"] if "verification" in data else []
        provenance = data["provenance"] if "provenance" in data else {}
        overlap = data["overlap"] if "overlap" in data else {}
        live = data["live"] if "live" in data else {}
        if (
            not isinstance(verification, list)
            or not isinstance(provenance, dict)
            or not isinstance(overlap, dict)
            or not isinstance(live, dict)
        ):
            return cls(1, "unparsable")
        if any(
            not isinstance(item, dict)
            or not _text(item.get("name"))
            or item.get("state") not in VERIFICATION_STATES
            or not _text(item.get("evidence"))
            for item in verification
        ):
            return cls(1, "unparsable")
        if state == "complete" and any(item["state"] == "unverified" for item in verification):
            state = "incomplete"
        if provenance and (
            not _text(provenance.get("backend")) or not _text(provenance.get("model"))
        ):
            return cls(1, "unparsable")
        if overlap and (
            not isinstance(overlap.get("duplicates"), list)
            or not isinstance(overlap.get("shared_files"), list)
            or any(not _integer(item) for item in overlap["duplicates"])
            or any(not _text(item) for item in overlap["shared_files"])
        ):
            return cls(1, "unparsable")
        try:
            raw_evidence = _raw(data.get("raw_evidence"))
        except (TypeError, ValueError):
            return cls(1, "unparsable")
        return cls(
            version,
            state,
            counts,
            findings,
            list(verification),
            dict(provenance),
            dict(overlap),
            dict(live),
            raw_evidence,
        )

    @property
    def is_clean(self) -> bool:
        return self.state == "complete" and not any(self.counts.values()) and not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "state": self.state,
            "counts": self.counts,
            "findings": self.findings,
            "verification": self.verification,
            "provenance": self.provenance,
            "overlap": self.overlap,
            "live": self.live,
            "raw_evidence": self.raw_evidence,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def parse_review_result(payload: str, raw_evidence: str | list[str] | None = None) -> ReviewResult:
    if not isinstance(payload, str) or len(payload) > MAX_RAW_CHARS:
        return _unparsable(raw_evidence, payload)
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("result is not an object")
        result = ReviewResult.from_dict(value)
        if result.state == "unparsable":
            return _unparsable(raw_evidence, payload)
        return result
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _unparsable(raw_evidence, payload)


def adapt_current_engine(
    output: str,
    exit_code: int | None,
    provenance: dict[str, Any] | None = None,
    *,
    verification: list[dict[str, Any]] | None = None,
    overlap: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
) -> ReviewResult:
    """Adapt Goose's JSONL findings and line-oriented progress records."""
    raw = _raw(output)
    truncated = _raw_truncated(output)
    base = dict(provenance or {})
    base.setdefault("backend", "goose")
    base.setdefault("model", "unknown")
    base.setdefault("engine", "bluefin-review")
    findings: list[dict[str, Any]] = []
    counts = {s: 0 for s in SEVERITIES}
    checks = list(verification or [])
    check_pattern = re.compile(
        r"^goose review: check '([^']+)' (completed|failed):\s*(.*)$"
    )
    summary_pattern = re.compile(
        r"^goose review: orchestrator emitted (\d+) finding\(s\) from (\d+) "
        r"check\(s\) \(main: (ran|skipped), (\d+) finding\(s\)(?:;[^)]*)?\)$"
    )
    summary: re.Match[str] | None = None
    malformed = False
    parsed_main_count = 0
    reported_main_count: int | None = None
    check_count_pattern = re.compile(r"^(\d+) finding\(s\)$")
    for line in raw:
        check = check_pattern.match(line)
        if check:
            if check.group(1) == "main":
                reported = check_count_pattern.match(check.group(3))
                if reported:
                    try:
                        reported_main_count = int(reported.group(1))
                    except (TypeError, ValueError, OverflowError):
                        malformed = True
            checks.append({
                "name": check.group(1),
                "state": "verified" if check.group(2) == "completed" else "unverified",
                "evidence": check.group(3),
                "source": "engine",
            })
            continue
        matched_summary = summary_pattern.match(line)
        if matched_summary:
            summary = matched_summary
            checks.append({
                "name": "main",
                "state": "verified" if matched_summary.group(3) == "ran" else "skipped",
                "evidence": f"{matched_summary.group(4)} finding(s)",
                "source": "engine",
            })
            continue
        try:
            item = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        finding_keys = {"severity", "path", "line_start", "summary", "check"}
        if not finding_keys.intersection(item):
            continue
        if not finding_keys.issubset(item):
            malformed = True
            continue
        severity = str(item["severity"]).lower()
        if severity not in SEVERITIES:
            malformed = True
            continue
        try:
            start_line = item["line_start"]
            end_line = item.get("line_end", start_line)
            if (
                not _text(item["path"])
                or not _text(item["summary"])
                or not _text(item["check"])
                or not _integer(start_line)
                or not _integer(end_line)
                or start_line < 1
                or end_line < start_line
            ):
                raise ValueError("invalid finding line evidence")
        except (TypeError, ValueError):
            malformed = True
            continue
        counts[severity] += 1
        findings.append({
            "severity": severity,
            "file": str(item["path"]),
            "line": start_line,
            "end_line": end_line,
            "title": str(item["summary"]),
            "check": str(item["check"]),
            "evidence": line,
        })
        if item["check"] == "main":
            parsed_main_count += 1

    context = {
        "verification": checks,
        "provenance": base,
        "overlap": dict(overlap or {}),
        "live": dict(live or {}),
        "raw_evidence": raw,
    }
    if truncated:
        return ReviewResult(1, "unparsable", counts=counts, findings=findings, **context)
    if exit_code not in (0, None, 65):
        return ReviewResult(1, "failed", **context)
    if malformed:
        return ReviewResult(1, "unparsable", counts=counts, findings=findings, **context)
    if exit_code == 65 or any("INCOMPLETE" in line.upper() for line in raw) or any(
        item.get("state") == "unverified" and item.get("source", "engine") == "engine"
        for item in checks
    ):
        return ReviewResult(1, "incomplete", counts=counts, findings=findings, **context)
    if summary is None:
        return ReviewResult(1, "unparsable", counts=counts, findings=findings, **context)
    try:
        summary_count = int(summary.group(1))
        main_count = int(summary.group(4))
    except (TypeError, ValueError, OverflowError):
        return ReviewResult(1, "unparsable", counts=counts, findings=findings, **context)
    if (
        summary_count != len(findings)
        or main_count > summary_count
        or main_count != parsed_main_count
        or (reported_main_count is not None and reported_main_count != main_count)
    ):
        return ReviewResult(1, "unparsable", counts=counts, findings=findings, **context)
    return ReviewResult(1, "findings" if findings else "complete", counts=counts,
                        findings=findings, **context)
