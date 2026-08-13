"""Batch landing: one agent, dispatched once, lands every selected PR.

The maintainer builds a batch with [b] and confirms it once behind the
typed-count gate. What runs next is a single background agent whose brief is
the whole batch: diagnose each pull request, repair the mechanical failures,
and land what the rules allow. The dashboard does not scrape the agent's
prose for status — the agent reports every state change as one JSON line in
a status file this module defines, and the screen polls that file.

The mutation contract is unchanged: nothing merges that a human did not
select and confirm, drafts are never merged, branch protection is never
bypassed, and a pull request the rules cannot land comes back as a written
reason, not a silent skip.
"""

from __future__ import annotations

import json
import os
import shlex
import time
from dataclasses import dataclass, field

# One-shot agent invocation. Goose's documented non-interactive entry point
# is `run --no-session -i <file>` (the same headless shape Hive's relay
# uses), so the batch agent needs no session state, reads its brief from the
# prompt file directly — no shell, the confirmation gate shows the real
# argv — and exits when the batch is done. Tests and maintainers override
# the whole command with BLUEFIN_REVIEW_LANDING_COMMAND; @PROMPT marks where
# the prompt file path goes.
DEFAULT_LANDING_COMMAND = "goose run --no-session -i @PROMPT"

# The states the agent may report, in the vocabulary the screen renders.
# "blocked" and "failed" differ: blocked means the rules forbid landing
# (draft, failing required checks, no permission); failed means the attempt
# itself errored. Both come back to the batch selected, with the note.
PR_STATES = (
    "diagnosing",
    "fixing",
    "waiting-ci",
    "merging",
    "merged",
    "blocked",
    "failed",
)
TASK_DONE = "done"


def landing_state_dir() -> str:
    root = os.environ.get(
        "XDG_STATE_HOME", os.path.expanduser("~/.local/state")
    )
    path = os.path.join(root, "bluefin-review", "landings")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class LandingTask:
    """One dispatched batch: the pull requests, the process, the report."""

    task_id: str
    stops: list  # list of Stop; duck-typed to keep this module Textual-free
    login: str
    prompt_path: str = ""
    status_path: str = ""
    log_path: str = ""
    command: list[str] = field(default_factory=list)
    process: object | None = None
    returncode: int | None = None
    started: float = 0.0

    @property
    def keys(self) -> list[str]:
        return [stop.key for stop in self.stops]

    @property
    def running(self) -> bool:
        return self.process is not None and self.returncode is None


def new_task(stops: list, login: str) -> LandingTask:
    """A task with its prompt, status, and log paths laid out."""
    task_id = time.strftime("%Y%m%d-%H%M%S")
    directory = landing_state_dir()
    task = LandingTask(
        task_id=task_id,
        stops=list(stops),
        login=login,
        prompt_path=os.path.join(directory, f"{task_id}.prompt.md"),
        status_path=os.path.join(directory, f"{task_id}.jsonl"),
        log_path=os.path.join(directory, f"{task_id}.log"),
        started=time.monotonic(),
    )
    with open(task.prompt_path, "w", encoding="utf-8") as handle:
        handle.write(landing_prompt(task))
    task.command = landing_command(task)
    return task


def landing_prompt(task: LandingTask) -> str:
    """The batch brief. The agent acts on the maintainer's confirmed
    selection; the rules it may not cross are stated in it, not assumed."""
    rows = "\n".join(
        f"- {stop.key} — {stop.title}" for stop in task.stops
    )
    return f"""You are the Bluefin review landing agent. The maintainer has read
and selected the pull requests below and confirmed — once, interactively —
that one agent should land the batch. That confirmation is your authority;
do not ask for more.

{rows}

For each pull request, in order:

1. Inspect it: `gh pr view` and `gh pr checks`.
2. Repair mechanical CI failures only — a stale sha256 after a version bump,
   a lockfile, formatting. Push the fix to the PR branch when you have
   permission. Never rewrite the PR's purpose.
3. Rerun flaky checks with `gh run rerun`; then wait for green.
4. When checks are green and the PR is mergeable, approve it:
   `gh pr review --approve --body "Approved by @{task.login} for Hive auto-merge on green CI."`
   then squash-merge: `gh pr merge --squash`. If GitHub refuses (branch
   protection, review requirements), do not force anything — add the `lgtm`
   label instead and move on.
5. Never merge a draft, never merge with failing required checks, never pass
   `--admin` or any flag that bypasses branch protection, never force-push.
   A pull request the rules cannot land is reported, not forced.

Report every state change by appending exactly one JSON line to
{task.status_path} (create it; one object per line, no other output there):
{{"pr": "org/repo#N", "state": "diagnosing|fixing|waiting-ci|merging|merged|blocked|failed", "note": "short reason"}}
When the batch is fully handled, append:
{{"state": "done", "note": "one-line summary for the maintainer"}}
Everything else you print goes to the maintainer's log; keep it terse.
"""


def landing_command(task: LandingTask) -> list[str]:
    """The argv that runs the batch agent. The prompt rides in its file so
    the confirmation gate shows a command a human can actually read."""
    template = os.environ.get(
        "BLUEFIN_REVIEW_LANDING_COMMAND", DEFAULT_LANDING_COMMAND
    )
    return [
        arg.replace("@PROMPT", task.prompt_path)
        for arg in shlex.split(template)
    ]


def parse_status(path: str) -> dict[str, dict]:
    """The latest event per pull request, plus the task-level "done" event
    under the "" key. A half-written final line is skipped, not fatal."""
    latest: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                latest[str(event.get("pr", ""))] = event
    except OSError:
        pass
    return latest
