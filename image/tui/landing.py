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

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

try:
    from tui.model_profiles import (
        OPUS_TRIPLE,
        classify_batch,
        final_environment,
        final_triple,
    )
except ImportError:
    from model_profiles import (  # type: ignore[no-redef]
        OPUS_TRIPLE,
        classify_batch,
        final_environment,
        final_triple,
    )

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
# "Done" depends on the repository: with a publish pipeline it is the
# merged commit published under the repository's release tag (the factory
# convention is :stable; a repository publishing only :latest proves it
# there), and "merged" is only reported once the tag carries the change;
# with no publish workflow and no image package the GitHub merge itself
# is done. The brief has the agent detect which kind of repository it is
# landing before it merges.
PR_STATES = (
    "diagnosing",
    "fixing",
    "waiting-ci",
    "merging",
    "awaiting-stable",
    "merged",
    "blocked",
    "failed",
)
TASK_DONE = "done"

# The states that close a pull request's record. The reporter at the bottom
# of this module writes each exactly once: an identical retry is a no-op,
# and anything else after a terminal state is refused (#377).
TERMINAL_PR_STATES = ("merged", "blocked", "failed")


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
    # The final review-and-fix rounds (#378) ride in this same structure so
    # they drain through the one landing lane. A landing task leaves them
    # empty; a round names its phase, its number, the policy that chose its
    # model, and the environment overlay that model actually needs.
    phase: str = ""
    round: int = 0
    policy: str = ""
    model: str = ""
    env: dict = field(default_factory=dict)
    # How many final-round events the record already held when this round
    # was dispatched. A round that adds none reported nothing, and the queue
    # must stop rather than dispatch the same phase forever.
    rounds_seen: int = 0

    @property
    def keys(self) -> list[str]:
        return [stop.key for stop in self.stops]

    @property
    def running(self) -> bool:
        return self.process is not None and self.returncode is None


def new_task(stops: list, login: str) -> LandingTask:
    """A task with its prompt, status, and log paths laid out."""
    directory = landing_state_dir()
    # A bare one-second stamp collides for two named dashboards sharing the
    # one state directory — REVIEW_QUEUE_NAME exists precisely so both can
    # run at once — so the instance name rides in the id, which also makes
    # the record attributable. The suffix covers same-second batches from a
    # single dashboard.
    instance = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", os.environ.get("BLUEFIN_REVIEW_INSTANCE", "")
    ).strip("-.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{instance}" if instance else stamp
    task_id = base
    suffix = 2
    while os.path.exists(os.path.join(directory, f"{task_id}.prompt.md")):
        task_id = f"{base}-{suffix}"
        suffix += 1
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
    # The record opens with the maintainer's confirmed selection, so the
    # reporter's `done` gates on what was dispatched rather than on the
    # agent's own --expect list (#377). parse_status skips the header.
    with open(task.status_path, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"expect": task.keys, "ts": int(time.time())}, separators=(",", ":"))
            + "\n"
        )
    task.command = landing_command(task)
    return task


def fix_prompt(task: LandingTask, findings: list[dict], steer: str = "") -> str:
    stop = task.stops[0]
    finding_lines = []
    for f in findings:
        sev = str(f.get("severity", "medium")).upper()
        path = f.get("file") or f.get("path") or "?"
        line = f.get("line") or f.get("line_start") or "?"
        summary = f.get("summary") or f.get("title") or "unspecified issue"
        check = f.get("check", "review")
        finding_lines.append(f"- [{sev}] {path}:{line} ({check}): {summary}")
    findings_block = (
        "\n".join(finding_lines)
        if finding_lines
        else "- No specific findings; verify general correctness and clean tests."
    )
    steer_block = f"\nMaintainer guidance:\n{steer}\n" if steer else ""
    reporter = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
    status = shlex.quote(task.status_path)

    return f"""You are the Bluefin review fix-and-land agent. The maintainer reviewed
pull request {stop.key} and hit [f] to dispatch an automated fix-and-land run.
Your mission is to repair the evidenced findings, verify that CI and checks are green,
re-review, and land the pull request.

PR: {stop.key} — {stop.title}
Repository: {stop.repository}
PR Number: {stop.number}
{steer_block}
Evidenced review findings to repair:
{findings_block}

Execute the following end-to-end loop:

1. Report diagnosing:
   {reporter} report --status {status} event --pr "{stop.key}" --state "diagnosing" --note "inspecting PR and findings"
   Inspect the pull request: `gh pr view {stop.number} --repo "{stop.repository}"` and `gh pr diff {stop.number} --repo "{stop.repository}"`.

2. Check out the PR branch in a dedicated scratch directory and fix each evidenced finding:
   {reporter} report --status {status} event --pr "{stop.key}" --state "fixing" --note "applying fixes for findings"
   WORKDIR=$(mktemp -d /tmp/pr-{stop.number}-XXXXXX)
   gh repo clone "{stop.repository}" "$WORKDIR"
   cd "$WORKDIR"
   gh pr checkout {stop.number} --repo "{stop.repository}"
   Keep changes surgical, minimal (Ponytail doctrine), and scoped strictly to the reported defects.
   Run existing project tests and linters to verify the fix works and introduces no regressions.
   Commit and push the fixes to the pull request branch:
   git push origin HEAD
   cd / && rm -rf "$WORKDIR"

3. Wait for CI checks to turn green:
   {reporter} report --status {status} event --pr "{stop.key}" --state "waiting-ci" --note "waiting for CI on pushed fix"
   Check status: `gh pr checks {stop.number} --repo "{stop.repository}"`.
   If a check failed and needs rerun, verify the run's status is `completed` before rerunning:
   `gh run view <id> --repo "{stop.repository}" --json status,conclusion`
   Only when status is `completed`, rerun with `gh run rerun <id> --failed --repo "{stop.repository}"`.
   Watch with `gh run watch <id> --repo "{stop.repository}" --exit-status`. If watch times out while run is still in-progress, continue watching rather than treating timeout as failure.

4. Re-review to confirm findings are cleared. When checks are green and the PR is mergeable, land it:
   {reporter} report --status {status} event --pr "{stop.key}" --state "merging" --note "checks green; approving and merging"
   Approve it: `gh pr review {stop.number} --repo "{stop.repository}" --approve --body "Approved by @{task.login} after automated fix-and-land run."`
   Then squash-merge: `gh pr merge {stop.number} --repo "{stop.repository}" --squash`. If branch protection or merge requirements block direct merge,
   add the `lgtm` label: `gh pr edit {stop.number} --repo "{stop.repository}" --add-label lgtm`.

5. Publication & completion:
   If this repository publishes an image package (convention :stable or :latest), report `awaiting-stable`
   and watch for the publication. Once verified, report:
   {reporter} report --status {status} event --pr "{stop.key}" --state "merged" --note "fixed, verified, and landed"
   Finally report task done:
   {reporter} report --status {status} done --note "PR {stop.key} fix-and-land run complete"
"""


def new_fix_task(
    stop,
    findings: list[dict],
    login: str,
    steer: str = "",
    root: str = "",
    command: str = "",
    backend: str = "goose",
) -> LandingTask:
    directory = root or landing_state_dir()
    instance = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", os.environ.get("BLUEFIN_REVIEW_INSTANCE", "")
    ).strip("-.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{instance}-fix" if instance else f"{stamp}-fix"
    task_id = base
    suffix = 2
    while os.path.exists(os.path.join(directory, f"{task_id}.prompt.md")):
        task_id = f"{base}-{suffix}"
        suffix += 1
    task = LandingTask(
        task_id=task_id,
        stops=[stop],
        login=login,
        prompt_path=os.path.join(directory, f"{task_id}.prompt.md"),
        status_path=os.path.join(directory, f"{task_id}.jsonl"),
        log_path=os.path.join(directory, f"{task_id}.log"),
        started=time.monotonic(),
        phase="",
    )
    with open(task.prompt_path, "w", encoding="utf-8") as handle:
        handle.write(fix_prompt(task, findings, steer))
    with open(task.status_path, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"expect": task.keys, "ts": int(time.time()), "fix": True}, separators=(",", ":"))
            + "\n"
        )
    task.command = landing_command(task)
    return task


# fsdk-containers#164 ships skopeo in the base; it deletes the anonymous
# registry check in step 5 below.
def landing_prompt(task: LandingTask) -> str:
    """The batch brief. The agent acts on the maintainer's confirmed
    selection; the rules it may not cross are stated in it, not assumed."""
    rows = "\n".join(
        f"- {stop.key} — {stop.title}" for stop in task.stops
    )
    # The status-file reporter is this module itself, so the brief names
    # the interpreter and file that generated it: correct in the image and
    # under a test harness alike.
    reporter = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
    status = shlex.quote(task.status_path)
    return f"""You are the Bluefin review landing agent. The maintainer has read
and selected the pull requests below and confirmed — once, interactively —
that one agent should land the batch. That confirmation is your authority;
do not ask for more.

{rows}

For each pull request, in order:

1. Inspect it with repository-qualified commands: `gh pr view <number> --repo <owner>/<repo>`
   and `gh pr checks <number> --repo <owner>/<repo>`.
2. Repair mechanical CI failures only — a stale sha256 after a version bump,
   a lockfile, formatting. If applying fixes, operate in a scratch workdir:
   `WORKDIR=$(mktemp -d /tmp/landing-XXXXXX) && gh repo clone <owner>/<repo> "$WORKDIR" && cd "$WORKDIR" && gh pr checkout <number> --repo <owner>/<repo>`.
   Push the fix to the PR branch when you have permission: `git push origin HEAD`, then clean up.
   Never rewrite the PR's purpose.
3. Rerun flaky checks: before invoking `gh run rerun <id> --failed --repo <owner>/<repo>`,
   verify that the run status is completed with `gh run view <id> --repo <owner>/<repo> --json status,conclusion`.
   Never rerun while status is still `in_progress` or `queued`.
   Wait for completion with `gh run watch <id> --repo <owner>/<repo> --exit-status`. If watch times out
   while the run is still active, continue watching rather than treating the timeout as failure.
4. When checks are green and the PR is mergeable, approve it:
   `gh pr review <number> --repo <owner>/<repo> --approve --body "Approved by @{task.login} for Hive auto-merge on green CI."`
   then squash-merge: `gh pr merge <number> --repo <owner>/<repo> --squash`. If GitHub refuses (branch
   protection, review requirements), do not force anything — add the `lgtm`
   label instead: `gh pr edit <number> --repo <owner>/<repo> --add-label lgtm` and move on. GitHub computes mergeability asynchronously,
   so `mergeable: UNKNOWN` is a cache-warming placeholder, never a verdict:
   re-query with backoff (about every 10 seconds for up to a minute, and
   name the wait in your note) until GitHub commits to an answer, and only
   act on the computed state. Never report `blocked` on UNKNOWN alone —
   a maintainer can act on a computed state, not on a placeholder.
   A required check can also fail without ever testing the pull request:
   the external service the check drives — a lab endpoint, a runner pool —
   is unreachable. That is infrastructure unavailability, not a defect in
   the pull request, and it is never `blocked` on its own: this appliance
   owns no lab and depends on none, so a check whose service is missing
   only means its deliverable gets verified in ghcr instead.
   Prove the
   distinction in the check's logs (endpoint unreachable, not failing
   tests), then verify the check's deliverable in ghcr instead: the pull
   request head's built image published under its `sha-<head>` tag is the
   substitute evidence, gathered through the anonymous flow in step 5.
   Then continue the normal path — approve and squash-merge; when branch
   protection refuses with the check still red, add the `lgtm` label, name
   the unreachability and the ghcr evidence in your note, and move on.
   What you may never do is merge around it: no `--admin`, no bypass, and
   no reclassifying the check as unrequired.
5. Done depends on whether this repository publishes an image, so check
   BEFORE merging — never discover it after. Treat the repository as
   publishing an image unless BOTH signals are absent: no workflow under
   `.github/workflows` has a publication path that pushes this
   repository's own package `ghcr.io/<owner>/<repo>`, and that package is
   not anonymously readable. Read each workflow's YAML rather than grep
   for the registry name: a `ghcr.io` mention is not a publish signal.
   A publication path is a job that pushes this repository's own package
   on a trigger a merge can reach — `on.push` to the default branch, or
   `on.workflow_run` following the repository's CI workflow. Reusable
   workflow logic (`on.workflow_call`), manual-only workflows
   (`on.workflow_dispatch`), release-only workflows (`on.release` — a
   merge owes no publication until a release is cut), examples, inputs,
   cleanup jobs, and references to other repositories' images never
   establish one.
   For the registry signal, probe the package through the reporter:

   {reporter} probe --package <owner>/<repo>

   ghcr never 404s a missing package: the anonymous token mint is denied
   (403 DENIED) for one, and `/tags/list` answers 401/403 — a 404 is only
   ever a missing REF inside an existing package. `readable: false` is
   exactly that denial — the negative signal itself, not an error to
   retry. `readable: true` answers the package's tags. A nonzero exit
   means the probe could not answer at all, which is never evidence of
   absence: retry with backoff before concluding anything. A 403 alone is
   ambiguous with a PRIVATE package no anonymous caller can see; the
   mandatory conjunction with the workflow signal covers that case — a
   repository whose workflow publishes still takes the publish path. Only
   when both are absent, the GitHub merge itself is done: report `merged`
   right after merging, with a note like "no publish workflow, no image
   package — the merge is the deliverable". Never take that path on one
   signal alone.

   `gh pr merge` may answer "accepted by merge queue": the merge completes
   later, on the queue's terms. Never `gh run watch` the merge_group gate
   run — the pull request can be merged while that run still goes. Poll
   `gh pr view --json state` until it reads MERGED, and verify the merge
   commit's push-event publish run: on main the publish workflow triggers
   on `push`, never `merge_group`.

   A GitHub merge is otherwise not done. Done is the merged commit
   published under the repository's release tag — and which tag that is
   is a fact about the repository, never an assumption: the factory
   convention is `:stable`, but a repository that publishes only `latest`
   (projectbluefin/common is one) proves its publish there. Report
   `awaiting-stable` right after merging, then watch the repository's
   publish workflow for the merge commit (the push-event run). The image
   ships no registry client and needs none — ghcr.io serves public
   packages anonymously, whether the repository's owner is an org or a
   user, and the probe carries the whole registry flow (mint, pagination,
   content negotiation), so no ad-hoc curl can mask a failure:

   {reporter} probe --package <owner>/<repo>                      # every tag
   {reporter} probe --package <owner>/<repo> --manifest <ref>     # digest, and children for an index

   The tags answer names the release tag up front. The publish tags every
   build with the commit — `sha-<commit>` on the index here, arch-suffixed
   `sha-<commit>-amd64`/`-arm64` on its children, the bare commit
   elsewhere. The release tag carries the merge when a tag containing the
   commit resolves — through the probe — to the release tag's digest or,
   for a multi-arch index, to one of its children. A commit-tagged image
   with no moving release tag is itself a proven publish. Only then
   report `merged`, naming the evidence. If the publish fails — or no
   publication of the merge commit
   can be evidenced at all — that is the deliverable: report `failed`
   with the evidence.

   Identify the publish workflow's trigger before watching anything: it
   decides which runs can carry the merge. A push-triggered publish
   appears in the merge commit's push runs; a `workflow_run` publish
   appears in `workflow_run` runs only after its upstream workflow
   completes. List the identified trigger's runs and ask the reporter:

   gh run list --repo <owner>/<repo> --commit <merge-sha> --event <trigger> --json workflowName,status,conclusion > runs.json
   {reporter} publish-verdict --trigger <trigger> --workflow "<publish workflow name>" --runs runs.json

   `wait` means keep polling: an empty run list is never evidence — runs
   can take a moment to exist — and the wait note names its target and
   timeout as usual. `no-publication-run` means every run is terminal and
   none is the publish workflow: stop polling — terminal runs prove no
   publication exists from them, so either the workflow's triggers never
   applied to this merge (the conditional case below: report `merged`
   naming the filter) or a promised publication failed to happen (report
   `failed` with the run evidence). `verify-registry` means the publish
   run completed green: prove the release tag carries the merge commit
   with the probe before reporting `merged`. `publish-failed` names a red
   publish run: rerun it once with `gh run rerun`; still red is `failed`
   with the evidence. `publish-skipped` means the publish run ended with
   nothing published and nothing failing (skipped, cancelled, neutral):
   a job-level condition or a cancellation excluded this merge — prove
   the exclusion from the workflow YAML and report `merged` naming it,
   or report `failed` with the run evidence when no condition applies.
   A cancelled run can also mean a superseding run is on its way:
   re-list once before concluding.

   One refinement before any of that: a publish workflow can be
   conditional — path-filtered (`paths:` under its `push:` trigger),
   scheduled, or manual. When the merge commit's diff touches none of the
   workflow's triggers, no publication of it exists to evidence, and none
   is owed: the merge is the deliverable. Prove the condition from the
   workflow YAML and the merge commit's file list, then report `merged`
   with a note naming the filter — never `failed` for a publication the
   repository never promised.

   Every wait-state note names its target and timeout: `waiting-ci` names
   the run and when you give up, `merging` names the merge-queue wait and
   when you give up, `awaiting-stable` names the tag and when you give up.
6. Never merge a draft, never merge with failing required checks, never pass
   `--admin` or any flag that bypasses branch protection, never force-push.
   A pull request the rules cannot land is reported, not forced.
7. When `BLUEFIN_REVIEW_LAB_SOCKET` is set, this session was lent a lab.
   Ask `/opt/bluefin/tui/lab_client.py` for a bounded health snapshot for
   each pull request you handle, and submit an allowlisted profile only for
   its exact 40-character head; `not-applicable` means there is no lab work
   for it, which is not a finding. Never call `kubectl` or `argo` — this
   container has neither. Lab evidence is supplementary: a degraded broker,
   a timeout, or a failed workflow means the lab told you nothing. Say so in
   the note and verify the deliverable from published registry evidence
   exactly as a session with no lab does. A pull request is never blocked
   because a lab was unavailable.

A blocked verdict can be repository-level. A required check that fails on
the toolchain or the base branch — a pinned compiler with known
standard-library vulnerabilities, a workflow misconfiguration — blocks
every pull request in that repository identically, whatever its diff does.
Diagnose that once: when you report `blocked` for such a condition, check
whether the remaining pull requests from the same repository list the same
failing check and, when it fails the same way, report the shared verdict
for all of them in one pass — each note naming the one root cause — rather
than re-diagnosing them one at a time, and do not attempt repairs or CI
waits on pull requests the shared verdict already covers. Never fix a
repository-level blocker inside one pull request's branch — that rewrites
its purpose. When the root cause has a mechanical fix (a toolchain pin
bump, a workflow configuration repair), make the fix at the root: its own
branch and pull request, named in every covered note and in the done
note. A pull request you opened was not in the maintainer's confirmed
selection, so do not merge it — the covered pull requests stay blocked
behind it, each note naming the fixing pull request. A root cause with no
mechanical fix is a written finding in the done note, never work inside a
queued pull request's branch.

Report every state change the moment you observe it — in the same step
that observes it, before you touch the next pull request, never saved up
for a bulk terminal-state write at the end — and only through the status
reporter the image ships. The status file has no other writer: no printf,
no heredoc, no jq append, no direct writes of any kind.

{reporter} report --status {status} event --pr "org/repo#N" --state "waiting-ci" --note "short reason"

One call appends exactly one JSON line, serialized under flock and
stamped with `ts`. The states:
diagnosing|fixing|waiting-ci|merging|awaiting-stable, then exactly one
terminal state per pull request — merged|blocked|failed. A terminal state
is written once: an identical retry is a no-op, and a post-terminal
non-terminal write exits nonzero — when one does, stop on that pull
request and put the conflict in your note for the maintainer, never work
around the record. A terminal verdict later proven wrong is corrected,
not hidden: report the new terminal state with the evidence before
closing the batch — the latest event wins the fold.

Close the batch only when every selected pull request has its terminal
state, naming the whole selection:

{reporter} report --status {status} done --expect "org/repo#N" "org/repo#M" --note "one-line summary for the maintainer"

done exits nonzero while any expected pull request lacks a terminal state,
so the batch record never closes with a hole in it. Everything else you
print goes to the maintainer's log; keep it terse.
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


def report_age(path: str) -> str:
    """How long since the agent last appended to its report. The status
    file's mtime is the heartbeat: every reported event rewrites it, so a
    stale mtime is the only difference a healthy 20-minute CI wait and a
    dead agent have (#291)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "no report yet"
    seconds = max(0, int(time.time() - mtime))
    if seconds < 90:
        return f"last report {seconds}s ago"
    return f"last report {seconds // 60}m ago"


def parse_status(path: str) -> dict[str, dict]:
    """The latest event per pull request, plus the task-level "done" event
    under the "" key. Lines without a state — the batch's selection header
    included — are not events and are skipped, and a half-written final
    line is skipped, not fatal."""
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
                if not isinstance(event, dict) or "state" not in event:
                    continue
                latest[str(event.get("pr", ""))] = event
    except OSError:
        pass
    return latest


# ── the status-file reporter ─────────────────────────────────────────────
# The landing agent reports through this CLI and nothing else (#377): one
# call is one compact JSON line, appended under an exclusive flock so the
# agent and the dashboard can overlap, and fsync'd before the lock drops.
# Terminal states are written once per pull request — an identical retry is
# a no-op, a conflicting or post-terminal write exits nonzero — and `done`
# refuses to close the batch while a selected pull request lacks a terminal
# state, so the durable record can never disagree with GitHub silently.


def _status_events(handle) -> list[dict]:
    """Every well-formed event in an open status file, in file order — the
    same lines parse_status folds, including a final line that is complete
    JSON but missing its newline. A torn tail (unparseable) is not a
    record: skipped here, truncated on the next append."""
    events: list[dict] = []
    handle.seek(0)
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


@contextlib.contextmanager
def _locked_status(status_path: str):
    """The status file open for read/append under an exclusive flock,
    yielding (handle, events so far). The file is created when missing."""
    fd = os.open(status_path, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle, _status_events(handle)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _append_event(handle, event: dict) -> str:
    line = json.dumps(event, separators=(",", ":"))
    handle.seek(0)
    content = handle.read()
    if content and not content.endswith("\n"):
        boundary = content.rfind("\n") + 1
        try:
            parsed_tail = json.loads(content[boundary:])
        except ValueError:
            parsed_tail = None
        if isinstance(parsed_tail, dict):
            # A complete final line that only lacks its newline is a
            # record — terminate it, never truncate it.
            handle.seek(0, os.SEEK_END)
            handle.write("\n")
        else:
            # A torn tail is not a record — the writer died mid-line — so
            # truncate it rather than glue this event onto it and lose both.
            handle.truncate(boundary)
    handle.seek(0, os.SEEK_END)
    handle.write(line + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    return line


def _stamp(event: dict) -> dict:
    """Add the write timestamp: immediate-write evidence is only
    verifiable when every line carries when it was written (#377)."""
    return {**event, "ts": int(time.time())}


def report_event(status_path: str, pr: str, state: str, note: str) -> int:
    """Append one pull-request event. A terminal state is written once: an
    identical retry is a no-op, and a post-terminal non-terminal write is
    refused. A terminal verdict later proven wrong is corrected by the new
    terminal state — the latest event wins the fold — so a premature
    `merged` never becomes uncorrectable. Events after the batch's `done`
    are refused. Returns the process exit status."""
    with _locked_status(status_path) as (handle, events):
        if any(
            not event.get("pr") and event.get("state") == TASK_DONE
            for event in events
        ):
            print(
                f"error: the batch is already done; refusing {pr} {state}",
                file=sys.stderr,
            )
            return 1
        mine = [event for event in events if event.get("pr") == pr]
        if mine and mine[-1].get("state") in TERMINAL_PR_STATES:
            recorded = mine[-1]
            if recorded.get("state") == state and recorded.get("note", "") == note:
                print(json.dumps(recorded, separators=(",", ":")))
                return 0
            if state not in TERMINAL_PR_STATES:
                print(
                    f"error: {pr} is already terminal "
                    f"({recorded.get('state')}: {recorded.get('note', '')}); "
                    f"refusing non-terminal {state}",
                    file=sys.stderr,
                )
                return 1
        line = _append_event(handle, _stamp({"pr": pr, "state": state, "note": note}))
    print(line)
    return 0


def report_done(status_path: str, expect: list[str], note: str) -> int:
    """Close the batch once every expected pull request has a terminal
    state. Expected is the selection seeded at dispatch plus the call's
    --expect keys, so the gate holds even when the agent under-names its
    batch. An identical retry is a no-op; a conflicting one is refused.
    Returns the process exit status."""
    with _locked_status(status_path) as (handle, events):
        latest: dict[str, dict] = {}
        seeded: list[str] = []
        for event in events:
            pr = event.get("pr")
            if pr and "state" in event:
                latest[str(pr)] = event
            header = event.get("expect")
            if "state" not in event and isinstance(header, list):
                seeded.extend(str(key) for key in header)
        expected = seeded + [key for key in expect if key not in seeded]
        missing = [
            key
            for key in expected
            if latest.get(key, {}).get("state") not in TERMINAL_PR_STATES
        ]
        if missing:
            print(
                f"error: no terminal state for: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 1
        recorded = next(
            (
                event
                for event in reversed(events)
                if not event.get("pr") and event.get("state") == TASK_DONE
            ),
            None,
        )
        if recorded is not None:
            if recorded.get("note", "") == note:
                print(json.dumps(recorded, separators=(",", ":")))
                return 0
            print(
                f"error: the batch is already done "
                f"({recorded.get('note', '')}); refusing {note!r}",
                file=sys.stderr,
            )
            return 1
        line = _append_event(handle, _stamp({"state": TASK_DONE, "note": note}))
    print(line)
    return 0


# ── the anonymous ghcr probe ─────────────────────────────────────────────
# The registry evidence for step 5 of the brief, executable so a shell
# pipeline can never mask a denied token mint again (#375): the probe owns
# the mint, the pagination, and the content negotiation, and its exit code
# separates a definitive answer (0 — see "readable") from a probe that
# could not answer (1 — never evidence of absence).

GHCR_BASE = os.environ.get("BLUEFIN_REVIEW_GHCR_BASE", "https://ghcr.io")
OCI_ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json"
)
_PROBE_PAGES = 100


def _get_json(url: str, token: str = "") -> tuple[dict, dict]:
    """GET url as JSON, returning (body, headers lowercased). Raises
    HTTPError with the status on an HTTP answer, URLError on a transport
    failure."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read()), {
            key.lower(): value for key, value in response.headers.items()
        }


def probe_package(package: str, manifest: str = "") -> tuple[dict, int]:
    """Probe `owner/image` on ghcr anonymously. (result, exit status): a
    denied mint (403) or a 401/403 from /tags/list is the definitive
    negative signal (readable false); anything else unexpected is an
    error, never evidence of absence. With --manifest, also resolve the
    ref's digest (and an index's children)."""
    # Registry paths are lowercase; a mixed-case scope answers 400, which
    # must never masquerade as a denied mint or an error.
    package = package.lower()
    base = GHCR_BASE.rstrip("/")
    result: dict = {"package": package, "readable": None}
    try:
        body, _ = _get_json(f"{base}/token?scope=repository:{package}:pull")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return {**result, "readable": False, "signal": "token mint denied (403)"}, 0
        return {**result, "error": f"token mint answered {exc.code}"}, 1
    except (urllib.error.URLError, OSError) as exc:
        return {**result, "error": f"token mint unreachable: {exc}"}, 1
    except ValueError:
        return {**result, "error": "token mint returned invalid JSON"}, 1
    token = body.get("token")
    if not token:
        return {**result, "error": "token mint returned no token"}, 1
    url = f"{base}/v2/{package}/tags/list"
    tags: list[str] = []
    for _ in range(_PROBE_PAGES):
        try:
            page, headers = _get_json(url, token)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {
                    **result,
                    "readable": False,
                    "signal": f"tags/list answered {exc.code}",
                }, 0
            return {**result, "error": f"tags/list answered {exc.code}"}, 1
        except (urllib.error.URLError, OSError) as exc:
            return {**result, "error": f"tags/list unreachable: {exc}"}, 1
        except ValueError:
            return {**result, "error": "tags/list returned invalid JSON"}, 1
        page_tags = page.get("tags")
        if isinstance(page_tags, list):
            tags.extend(str(tag) for tag in page_tags)
        cursor = re.search(r'<([^>]+)>\s*;\s*rel="next"', headers.get("link", ""))
        if not cursor:
            break
        url = urllib.parse.urljoin(url, cursor.group(1))
    else:
        return {**result, "error": "tags/list pagination did not terminate"}, 1
    result = {**result, "readable": True, "tags": tags}
    if not manifest:
        return result, 0
    request = urllib.request.Request(
        f"{base}/v2/{package}/manifests/{manifest}",
        headers={"Accept": OCI_ACCEPT, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            digest = response.headers.get("docker-content-digest", "")
            try:
                body = json.loads(response.read())
            except ValueError:
                body = {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {**result, "manifest": manifest, "present": False}, 0
        return {**result, "error": f"manifest {manifest} answered {exc.code}"}, 1
    except (urllib.error.URLError, OSError) as exc:
        return {**result, "error": f"manifest {manifest} unreachable: {exc}"}, 1
    answer = {**result, "manifest": manifest, "present": True, "digest": digest}
    children = body.get("manifests")
    if isinstance(children, list):
        answer["children"] = [
            str(child.get("digest", "")) for child in children if isinstance(child, dict)
        ]
    return answer, 0


def publish_verdict(trigger: str, workflow: str, runs_path: str) -> tuple[dict, int]:
    """The wait/stop decision for a merge commit's publication, from the
    identified publish workflow's own trigger and runs. An empty run list
    is never evidence — runs can lag the merge — so it answers `wait`,
    and only all-terminal runs can answer `no-publication-run`."""
    if trigger == "release":
        return {
            "verdict": "not-owed",
            "reason": "the publication path is release-triggered; a merge "
            "owes no publication until a release is cut",
        }, 0
    try:
        with open(runs_path, encoding="utf-8") as handle:
            runs = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"verdict": "error", "reason": f"cannot read the runs file: {exc}"}, 1
    if not isinstance(runs, list):
        return {"verdict": "error", "reason": "the runs file is not a JSON array"}, 1
    if not runs:
        return {
            "verdict": "wait",
            "reason": "no runs recorded for the merge commit yet — an empty "
            "list is not terminal evidence",
        }, 0
    pending = [run for run in runs if not isinstance(run, dict) or run.get("status") != "completed"]
    if pending:
        names = ", ".join(str(run.get("workflowName", "?")) for run in pending if isinstance(run, dict))
        return {"verdict": "wait", "reason": f"runs not terminal: {names}"}, 0
    publish_runs = [run for run in runs if run.get("workflowName") == workflow]
    if not publish_runs:
        return {
            "verdict": "no-publication-run",
            "reason": "every run is terminal and none is the identified "
            "publish workflow — no publication exists from these runs",
        }, 0
    conclusions = [str(run.get("conclusion", "")) for run in publish_runs]
    if "success" in conclusions:
        return {
            "verdict": "verify-registry",
            "reason": "the publish run completed green; verify the release tag "
            "carries the merge commit in the registry",
        }, 0
    hard = {"failure", "timed_out", "startup_failure"} & set(conclusions)
    if hard:
        return {
            "verdict": "publish-failed",
            "reason": f"the publish workflow completed {sorted(hard)[0]}",
        }, 0
    # skipped/cancelled/neutral/stale/action_required: the run ended with
    # nothing published and nothing failed — the workflow's job conditions
    # or a cancellation excluded this merge. That is the conditional case,
    # not a red run to rerun.
    return {
        "verdict": "publish-skipped",
        "reason": f"the publish run completed {conclusions[0] or 'unknown'} — "
        "no publication happened and none failed; prove the exclusion from "
        "the workflow YAML",
    }, 0


def main(argv: list[str] | None = None) -> int:
    """The status-file reporter and registry probe the brief instructs."""
    parser = argparse.ArgumentParser(prog="landing")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="append to a landing status file")
    report.add_argument("--status", required=True, help="status JSONL path")
    kinds = report.add_subparsers(dest="kind", required=True)
    event = kinds.add_parser("event", help="report one pull-request state")
    event.add_argument("--pr", required=True, help="org/repo#N")
    event.add_argument("--state", required=True, choices=PR_STATES)
    event.add_argument("--note", required=True)
    done = kinds.add_parser("done", help="close the batch")
    done.add_argument(
        "--expect",
        required=True,
        nargs="+",
        metavar="KEY",
        help="every selected pull request key",
    )
    done.add_argument("--note", required=True)
    final = kinds.add_parser("final", help="report one final review-and-fix round")
    final.add_argument("--round", required=True, type=int)
    final.add_argument("--phase", required=True, choices=FINAL_PHASES)
    final.add_argument("--model", required=True)
    final.add_argument("--input-head", default="", metavar="SHA")
    final.add_argument("--output-head", default="", metavar="SHA")
    final.add_argument("--note", required=True)
    probe = commands.add_parser("probe", help="probe a ghcr package anonymously")
    probe.add_argument("--package", required=True, help="owner/image")
    probe.add_argument("--manifest", default="", help="also resolve this ref's digest")
    verdict = commands.add_parser(
        "publish-verdict", help="wait/stop decision for a merge commit's publication"
    )
    verdict.add_argument(
        "--trigger", required=True, choices=("push", "workflow_run", "release")
    )
    verdict.add_argument("--workflow", required=True, help="publish workflow name")
    verdict.add_argument("--runs", required=True, help="path to gh run list --json output")
    args = parser.parse_args(argv)
    if args.command == "probe":
        answer, status = probe_package(args.package, args.manifest)
        print(json.dumps(answer, separators=(",", ":")))
        return status
    if args.command == "publish-verdict":
        answer, status = publish_verdict(args.trigger, args.workflow, args.runs)
        print(json.dumps(answer, separators=(",", ":")))
        return status
    if args.kind == "event":
        return report_event(args.status, args.pr, args.state, args.note)
    if args.kind == "final":
        return report_final(
            args.status,
            args.round,
            args.phase,
            args.model,
            args.note,
            args.input_head,
            args.output_head,
        )
    return report_done(args.status, args.expect, args.note)


# ── the final review-and-fix rounds (#378) ───────────────────────────────
# A landed batch is not a reviewed batch. What follows runs after every
# selected pull request has a terminal outcome: a final reviewer reads the
# whole batch, a fixer repairs what it found, and a fresh reviewer looks
# again — each round in its own process, because asking one long-lived agent
# to review the work it just wrote is how a review becomes a rubber stamp.
#
# This is the SAME queue: rounds are LandingTasks with a phase, dispatched by
# the repository-aware scheduler, cancelled by the same [x], and recorded in
# the same status file. There is no second scheduler and no second selection
# authority — the maintainer's confirmed batch is still the only scope grant.

FINAL_POLICIES = ("automatic", "gemini", "opus", "sol", "kimi")

# The phases a round may report. `final-review-clean` and `review-blocked`
# close the phase: nothing may be written after either.
FINAL_PHASES = (
    "final-review",
    "fixing",
    "re-review",
    "cleanup",
    "final-review-clean",
    "review-blocked",
)
FINAL_TERMINAL_PHASES = ("final-review-clean", "review-blocked")

# Five rounds, then the batch is visibly blocked with its findings intact.
# A loop that never stops is not a quality gate; it is a way to burn a
# maintainer's afternoon and their inference budget.
FINAL_ROUND_LIMIT = 5

# The reserved record key for final-round events. Real keys are
# `owner/repo#N`, so this can never collide with a pull request, and the
# batch's own `done` event keeps the "" key it always had.
FINAL_KEY = "final"
FINAL_OUTCOME_NOTE_LIMIT = 320

def final_command(prompt_path: str, triple: tuple, backend: str = "") -> list[str]:
    """The argv for one round. Goose reads the prompt from a file exactly as
    the landing agent does; Codex takes the model and effort as flags,
    because its model does not come from the environment."""
    template = os.environ.get(
        "BLUEFIN_REVIEW_LANDING_COMMAND", DEFAULT_LANDING_COMMAND
    )
    argv = [arg.replace("@PROMPT", prompt_path) for arg in shlex.split(template)]
    _, model, effort = triple
    active = backend or os.environ.get("BLUEFIN_REVIEW_BACKEND", triple[0])
    if active == "codex" and template == DEFAULT_LANDING_COMMAND:
        return [
            "codex", "exec", "--ignore-user-config", "--model", model,
            "--config", f"model_reasoning_effort={effort}",
            "-", prompt_path,
        ]
    return argv


def final_rounds(status_path: str) -> list[dict]:
    """Every final-round event recorded for a batch, in order."""
    rounds: list[dict] = []
    try:
        with open(status_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("pr") == FINAL_KEY:
                    rounds.append(event)
    except OSError:
        pass
    return rounds


def final_phase(status_path: str) -> dict:
    """The batch's current final-review phase, or {} before one starts."""
    rounds = final_rounds(status_path)
    return rounds[-1] if rounds else {}


def final_outcome_rows(task: "LandingTask") -> str:
    """Render bounded terminal evidence for a recovery review prompt."""
    events = parse_status(task.status_path)
    rows = []
    for stop in task.stops:
        event = events.get(stop.key, {})
        state = str(event.get("state", ""))
        if state not in TERMINAL_PR_STATES:
            continue
        note = str(event.get("note", "no reason given")).strip()
        note = note[:FINAL_OUTCOME_NOTE_LIMIT] or "no reason given"
        rows.append(f"- {stop.key} — {state}: {json.dumps(note)}")
    return "\n".join(rows)


def report_final(
    status_path: str,
    round_number: int,
    phase: str,
    model: str,
    note: str,
    input_head: str = "",
    output_head: str = "",
) -> int:
    """Append one final-round event.

    The breaker lives here rather than in the prompt: a sixth round is
    refused by the record itself, so an agent that talks itself into one
    more pass cannot have it. Writing after the phase closed is refused for
    the same reason — `review-blocked` is a result a maintainer acts on, not
    a state an agent may quietly walk back.
    """
    if round_number < 1 or round_number > FINAL_ROUND_LIMIT:
        print(
            f"error: round {round_number} is outside 1..{FINAL_ROUND_LIMIT}; "
            "the batch is review-blocked and needs a maintainer",
            file=sys.stderr,
        )
        return 1
    for head, label in ((input_head, "--input-head"), (output_head, "--output-head")):
        if head and not re.fullmatch(r"[0-9a-f]{40}", head):
            print(
                f"error: {label} must be a 40-character head sha, got {head!r}",
                file=sys.stderr,
            )
            return 1
    with _locked_status(status_path) as (handle, events):
        rounds = [event for event in events if event.get("pr") == FINAL_KEY]
        if rounds and rounds[-1].get("phase") in FINAL_TERMINAL_PHASES:
            print(
                f"error: the final review is already {rounds[-1].get('phase')}; "
                f"refusing {phase}",
                file=sys.stderr,
            )
            return 1
        event = {
            "pr": FINAL_KEY,
            "state": phase,
            "phase": phase,
            "round": round_number,
            "model": model,
            "note": note,
        }
        if input_head:
            event["input_head"] = input_head
        if output_head:
            event["output_head"] = output_head
        line = _append_event(handle, _stamp(event))
    print(line)
    return 0


def final_prompt(
    task: "LandingTask",
    round_number: int,
    phase: str,
    triple: tuple,
    findings: str = "",
) -> str:
    """One round's brief: review, fix, or close.

    Every round is a new process reading this file, so the brief carries the
    whole context a round needs. It never widens scope: the pull requests
    below are the maintainer's confirmed selection and the only branches a
    round may write to.
    """
    reporter = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
    status = shlex.quote(task.status_path)
    rows = "\n".join(f"- {stop.key} — {stop.title}" for stop in task.stops)
    outcomes = final_outcome_rows(task)
    recovery = any(
        event.get("state") in ("blocked", "failed")
        for event in parse_status(task.status_path).values()
    )
    _, model, _effort = triple
    reporting = (
        f"{reporter} report --status {status} final --round {round_number} "
        f"--phase <phase> --model {model} --input-head <40-char sha> "
        f"--output-head <40-char sha> --note \"one line\""
    )
    if phase == "cleanup":
        return f"""You are the Bluefin batch cleanup pass, round {round_number} of
{FINAL_ROUND_LIMIT}, running as {model}.

{rows}

The final review came back clean. Remove the transient material this batch
created and nothing else:

- plan-owned workspaces, task briefs, review packages, temporary worktrees,
  and scratch reports this batch made;
- never a broad recursive delete, never a wildcard, never a path this batch
  did not create.

Never commit implementation plans, session diaries, append-only status
notes, or generated `.agents/skills/` content. What survives is git history,
the issue and pull-request receipts, and any durable source-backed lesson
recorded in the closest `docs/skills/` document — and a skill edit runs
`bash scripts/check-skill-frontmatter.sh --write` and commits the generated
`docs/skills/index.json` with it. Do not manufacture documentation when no
durable contract changed.

Report the result, naming what you removed:

{reporting}

Use phase `cleanup` for the receipt and then `final-review-clean` to close
the batch. If you cannot remove something this batch owns, say so in the
note and close with `review-blocked` instead — an incomplete cleanup is an
incomplete batch.
"""
    if phase == "fixing":
        return f"""You are the Bluefin batch fixer, round {round_number} of
{FINAL_ROUND_LIMIT}, running as {model}. A review of this batch found the
problems below. Fix them; do not review.

{rows}

Findings to repair:

{findings or "(none recorded — stop and report review-blocked)"}

Rules, in order of importance:

1. Fix the SMALLEST in-scope defect that answers each finding. You may
   commit only on the pull-request branches listed above — branches the
   maintainer already authorized. Never open new work, never widen the
   batch, never touch a repository outside it.
2. Re-read GitHub before every write. If a head moved since the review, the
   findings describe code that no longer exists: stop, report the new head
   in `--output-head`, and let the next round review the current state.
   Never write against a head you did not read.
3. Run the focused validation the change deserves and name it in your note.
4. Never `--admin`, never force-push, never remove a hold or block label,
   never bypass a required check, never merge.

Report the round with the head you started from and the head you left:

{reporting}

Use phase `fixing`. Then stop — a fresh reviewer reads your work, not you.
"""
    outcome_context = (
        f"""This batch completed with terminal landing outcomes. Your review is
one consolidated recovery pass: determine whether an in-scope repair is
needed, but never retry landing, approve, or merge.

Terminal landing outcomes needing maintainer recovery:
{outcomes or "(no terminal outcome evidence recorded)"}
"""
        if recovery
        else "This batch has landed; your job is to decide whether it is actually good.\n"
    )
    return f"""You are the Bluefin final batch reviewer, round {round_number} of
{FINAL_ROUND_LIMIT}, running as {model}.

{outcome_context}

{rows}

Review the batch as a whole against the repository's own contracts:
`AGENTS.md`, `docs/SKILL.md`, and the canonical skill catalog are
authoritative; client instruction files stay pointers to them rather than
copies of policy; no generated `.agents/skills/` content is committed.
Report a finding when a change contradicts one of those, not when it merely
differs from your taste.

Bind every observation to the exact head you read: repository, pull request,
40-character head sha, file, and the evidence. A finding without those is
not actionable and must not be reported as one.

Report the round:

{reporting}

Use phase `final-review` (or `re-review` when a fixer has already run). When
the batch is clean, say so in the note and use phase `final-review-clean` —
the cleanup pass follows. When findings remain, list them in the note; the
queue dispatches a fresh fixer, and you never fix what you reviewed.

Round {FINAL_ROUND_LIMIT} is the last one. If findings remain after it, the
batch is `review-blocked` and belongs to a maintainer: report that phase
with the remaining findings in the note rather than starting another round.
"""


def new_final_round(
    task: "LandingTask",
    phase: str,
    round_number: int,
    policy: str,
    findings: str = "",
    backend: str = "",
) -> "LandingTask":
    """One round, as a task the existing landing lane already knows how to
    run: same status file, same log, its own prompt and process."""
    triple = final_triple(policy, classify_batch(task.stops), phase)
    directory = landing_state_dir()
    prompt_path = os.path.join(
        directory, f"{task.task_id}.final-{round_number}-{phase}.prompt.md"
    )
    round_task = LandingTask(
        task_id=f"{task.task_id} · {phase} {round_number}/{FINAL_ROUND_LIMIT}",
        stops=list(task.stops),
        login=task.login,
        prompt_path=prompt_path,
        status_path=task.status_path,
        log_path=task.log_path,
        started=time.monotonic(),
    )
    round_task.phase = phase
    round_task.round = round_number
    round_task.policy = policy
    round_task.model = triple[1]
    round_task.env = final_environment(triple, backend)
    round_task.rounds_seen = len(final_rounds(task.status_path))
    with open(prompt_path, "w", encoding="utf-8") as handle:
        handle.write(final_prompt(task, round_number, phase, triple, findings))
    round_task.command = final_command(prompt_path, triple, backend)
    return round_task


# The record is durable, so it is also bounded: batch files older than
# this are pruned on read, or the state directory grows without bound
# (#290). A week covers a review queue's memory — a pull request older
# than that has left the snapshot anyway.
LANDING_RETENTION_SECONDS = 7 * 24 * 60 * 60


def prune_landings(root: str, now: float | None = None) -> None:
    """Delete batch files past the retention window. Best-effort: a file
    that cannot be stat'd or removed is left for the next pass."""
    cutoff = (time.time() if now is None else now) - LANDING_RETENTION_SECONDS
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            continue


def record_event(key: str, state: str, note: str) -> None:
    """Supersede a persisted outcome from outside a batch. A manual
    success — a re-queue, a direct merge — clears the row's marking in
    memory only; unless the record says so too, the next refresh folds
    the stale failure back onto it (#290). One appended line in a file
    whose mtime is now wins the fold. Best-effort: a state-directory
    problem must not break the mutation that just succeeded."""
    try:
        path = os.path.join(landing_state_dir(), "manual.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "pr": key,
                        "state": state,
                        "note": note,
                        "ts": int(time.time()),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        pass


def persisted_events(directory: str | None = None) -> dict[str, dict]:
    """The latest event per pull request across every recorded batch, so a
    relaunched dashboard can fold a previous run's outcomes back onto the
    rows (#281). Files fold oldest first by mtime, so newer batches win;
    the task-level "" key is not a pull request and is dropped."""
    root = directory or landing_state_dir()
    prune_landings(root)
    try:
        paths = [
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.endswith(".jsonl")
        ]
        paths.sort(key=lambda path: (os.path.getmtime(path), path))
    except OSError:
        return {}
    latest: dict[str, dict] = {}
    for path in paths:
        for key, event in parse_status(path).items():
            if key:
                latest[key] = event
    return latest


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        os._exit(1)
