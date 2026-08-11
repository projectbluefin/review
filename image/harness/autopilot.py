"""Non-secret maintainer-side harness discovery and preference memory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .codex import CodexHarness
from .goose import GooseHarness
from tui.review_evidence_manifest import ReviewRequest
from .registry import Availability
from tui.review_result import ReviewResult


@dataclass(frozen=True)
class Discovery:
    backend: str
    installed: str
    auth: str
    capability: str
    model: str
    reasoning: str
    availability: Availability


@dataclass(frozen=True)
class HarnessOption:
    harness: object
    discovery: Discovery

    @property
    def status(self) -> str:
        return self.discovery.availability.value


@dataclass(frozen=True)
class Preference:
    harness_id: str
    model: str
    effort: str


def preference_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "bluefin-review" / "harness.json"


def load_preferences() -> dict[str, Preference]:
    path = preference_path()
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}
    result: dict[str, Preference] = {}
    for key, value in data.items() if isinstance(data, dict) else ():
        if isinstance(value, dict) and all(isinstance(value.get(k), str) for k in ("harness_id", "model", "effort")):
            result[key] = Preference(value["harness_id"], value["model"], value["effort"])
    return result


def remember_success(preferences: dict[str, Preference], repository: str,
                     preference: Preference) -> None:
    updated = dict(preferences)
    updated[repository] = preference
    updated["*"] = preference
    path = preference_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".harness.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({key: value.__dict__ for key, value in updated.items()}, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def discover() -> Discovery:
    """Run bounded foreground probes; callers must invoke this off the UI thread."""
    availability = CodexHarness.probe()
    installed = "ready" if availability is not Availability.UNAVAILABLE_BINARY else "missing"
    auth = "ready" if availability is Availability.READY else (
        "missing" if availability is Availability.UNAVAILABLE_BINARY else "failed"
    )
    capability = "ready" if availability is Availability.READY else "unavailable"
    model = "gpt-5.6-luna"
    reasoning = "low, medium, high, max"
    return Discovery("codex", installed, auth, capability, model, reasoning, availability)


def discover_all() -> list[HarnessOption]:
    """Discover every registered maintainer harness without starting inference."""
    goose = GooseHarness()
    return [
        HarnessOption(goose, Discovery(
            "goose", "ready", "ready", "ready", "gpt-5.6-luna", "low", goose.availability,
        )),
        HarnessOption(CodexHarness(), discover()),
    ]


def choose_option(repository: str, preferences: dict[str, Preference],
                  options: list[HarnessOption], configured: Preference | None = None) -> HarnessOption | None:
    by_id = {option.harness.branding.harness_id: option for option in options}
    candidates = [preferences.get(repository), preferences.get("*"), configured,
                  Preference("codex", "gpt-5.6-luna", "low")]
    for candidate in candidates[:2]:
        option = by_id.get(candidate.harness_id) if candidate else None
        if option:
            if option.discovery.availability is Availability.READY:
                return option
            return option
    for candidate in candidates[2:]:
        option = by_id.get(candidate.harness_id) if candidate else None
        if option and option.discovery.availability is Availability.READY:
            return option
    return next((option for option in options if option.discovery.availability is Availability.READY), None)


def choose(repository: str, preferences: dict[str, Preference], discovery: Discovery,
           configured: Preference | None = None) -> Preference | None:
    candidates = [preferences.get(repository), preferences.get("*"), configured,
                  Preference("codex", "gpt-5.6-luna", "low")]
    for candidate in candidates:
        if candidate and candidate.harness_id == discovery.backend and discovery.availability is Availability.READY:
            return candidate
    return None


def stale_choice(repository: str, preferences: dict[str, Preference],
                 discovery: Discovery) -> str | None:
    remembered = preferences.get(repository) or preferences.get("*")
    if remembered and discovery.availability is not Availability.READY:
        return (f"Remembered {remembered.harness_id}/{remembered.model} is "
                f"{discovery.availability.value}; confirm a replacement.")
    return None


def can_remember(result: ReviewResult, binding: ReviewRequest) -> bool:
    if result.state not in ("complete", "findings"):
        return False
    provenance = result.provenance
    return all(provenance.get(key) == value for key, value in {
        "backend": "codex", "repository": f"{binding.owner}/{binding.repository}",
        "pull_request": binding.pull_request_number, "base_sha": binding.base_sha,
        "head_sha": binding.head_sha,
    }.items())
