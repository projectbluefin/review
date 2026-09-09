---
name: landing-batches
version: "1.1"
last_updated: 2026-09-08
id: landing-batches
one_line_purpose: Manage multi-PR landing batches and automated fix-and-land agents.
entry_point: docs/skills/landing-batches.md
category: ci-ops
status: active
tags: [batches, landing, tui, agent, automerge]
description: "Manages batch landings, multi-repo lane partitioning, fix-and-land agents, and landing status persistence. Use when modifying batch execution or landing.py."
metadata:
  type: procedure
  context7-sources: [/websites/textual_textualize_io, /textualize/textual]
---

# Landing Batches

> The review dashboard coordinates parallel batch landings and automated
> fix-and-land subagents without blocking interactive maintainer triage.

## When to Use

Load this when editing `image/tui/landing.py`, modifying batch dispatching,
adjusting landing lane concurrency, or working with `${XDG_STATE_HOME}/bluefin-review/landings/`.

## When Not to Use

Do not load this for primary cockpit layout and key navigation (`review-dashboard.md`)
or cluster scale-out (`cluster-workers.md`).

## Core Architecture

1. **Selection & Confirmation:** `[b]` marks stops for batching; `[A]` opens
   `BatchPlanScreen` showing every selected PR and the exact agent command.
   Enter dispatches; Escape aborts.
2. **Multi-Repository Partitioning:** Multi-repo selections partition into
   independent per-repository `LandingTask` lanes.
3. **Concurrent Execution:** Up to `BLUEFIN_REVIEW_CONCURRENT_LANDINGS`
   (default 6) run concurrently across disjoint repository sets.
4. **Fix & Land:** From `ReviewScreen`, `[f]` dispatches a background
   fix-and-land agent (`new_fix_task`) seeded with evidenced review findings.
   `[F]` prompts for steering guidance before dispatching.
   `[$]` ("slay") executes the pipeline end-to-end. See below: it is a durable
   per-pull-request state machine, not a sequence of dispatches.
5. **State Directory:** State persists at `${XDG_STATE_HOME}/bluefin-review/landings/`.
   Each batch receives `.jsonl` events, `.log` output, and `.prompt.md`.
   Filenames qualify with `BLUEFIN_REVIEW_INSTANCE` to avoid cross-session collisions.
6. **Reporting Seam:** The landing agent never writes status directly; it calls:
   `/opt/bluefin/tui/.venv/bin/python /opt/bluefin/tui/landing.py report ...`
7. **Process Termination:** The agent runs in its own process group; `[x]` on
   the batch screen stops it cleanly via `SIGTERM`.

## `[$]` is a state machine

`[$]` takes one typed `slay` confirmation for a batch (or the pull request
number for one pull request) and then mutates pull requests without further human input. Everything it does afterwards is therefore a safety
property, and safety properties cannot live in membership sets scattered across
the dashboard: a key added to one set and dropped on an early return is a pull
request that is permanently stuck or, worse, permanently in flight.

Each selected pull request gets **one durable run record** carrying its own
state, its exact head, and its terminal outcome. The pipeline is:

```text
review (fresh context, exact head)
  → findings? fix agent : land
  → fixer pushes a new head
  → fresh strong review of that new head
  → live validation
  → land
```

Three invariants govern it.

**Exact head, carried and revalidated.** The head reviewed is the head landed.
A fixer produces a *new* head, and that head has never been reviewed — so it
receives its own full review before it may land. Revalidate head, checks, and
permissions live immediately before every mutation; a snapshot taken earlier in
the pipeline is evidence about a commit that may no longer be current.

**Escalation is triggered by mutation, not by a verdict.** A cheap model
reporting "clean" is exactly the case that most needs a second opinion, so its
own verdict can never be the thing that decides to skip one. Any path that
reaches a mutation gets a fresh high-assurance review of the head being landed.
A cheap first pass may triage and may seed a fixer; it may not authorise a
merge. Escalation is skippable only for an explicit, deterministic, low-risk
class, never because the weak model was satisfied.

**Human review is enforced at the gate.** A pull request lacking a human review
is stopped at the landing gate, using live GitHub reviewer evidence. Sorting
such pull requests to the top of the queue is prioritisation, not enforcement;
`[$]`'s own review-state fields describe machine reviews and say nothing about
who approved.

Missing, failed, incomplete, or unparsable review results are terminal for that
run. They never fall through to landing.

Mutations for one repository run in a single FIFO lane, so two runs cannot race
each other's branch state. Reviews are not lane-bound; they are admitted by the
process-wide scheduler in
[`review-scheduler.md`](review-scheduler.md).

## Terminal Recovery

A completed landing never silently arms a retry. Merged rows leave the
selection. Failed, blocked, unfinished, missing-outcome, and
`awaiting-stable` rows retain a bounded visible reason but are deselected; a
maintainer explicitly selects and reconfirms any later retry.

After every confirmed batch reaches terminal PR outcomes, the existing
final-review lane starts exactly one consolidated recovery review. Its prompt
names only that confirmed batch and includes its bounded, JSON-quoted terminal
states and reasons. The reviewer and any fresh fixer may repair only those
already authorized branches; neither retries landing, approves, merges,
completes Hive work, or expands the batch. Human retry and merge remain
separate explicit actions.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Let agents write JSONL directly." | Direct writes risk file corruption and concurrent races. Use `landing.py report`. |
| "Serialize all batches." | Repositories with disjoint dependencies can land safely in parallel. |
| "The review passed, so the fixed head is fine." | The fixer produced a head nobody reviewed. Review it before landing it. |
| "Flash said clean, so escalation is unnecessary." | A false negative reports clean. The verdict of the weakest model cannot be what decides to trust it. |
| "Sorting unreviewed PRs to the top ensures human review." | Sorting is prioritisation. Enforcement is a landing gate reading live reviewer evidence. |
| "Track in-flight PRs in a set." | Every early return leaks a key. Use one durable run record per pull request with an explicit terminal state. |
| "Keep a failed row selected so the next slay can retry it." | That replays a failed mutation without confirmation. Keep its reason visible and require an explicit new selection. |

## Red Flags

- Shared state directory writes without instance-qualified batch IDs.
- Leaving agent process groups running after `[x]` stop requests.
- Mutating PRs without prior maintainer batch confirmation.
- Landing a head that differs from the head that was reviewed.
- A mutation using snapshot evidence rather than a live pre-mutation check.
- In-flight state represented as membership in a set rather than a run record.

## Verification

```bash
python3 -m unittest discover -s tests -p "*test*landing*"
bash tests/dashboard-contract.sh
```
