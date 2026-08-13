---
name: review-dashboard
version: "1.3"
last_updated: 2026-08-11
id: review-dashboard
one_line_purpose: Change the maintainer dashboard without weakening its gate or hiding the queue.
entry_point: docs/skills/review-dashboard.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [textual, tui, dashboard, review, maintainer]
description: "Maintains image/tui/bluefin_review_tui.py: the mutation gate, the queue view, and its Textual patterns. Use when editing the dashboard or its pilot tests."
metadata:
  type: runbook
  context7-sources: [/websites/textual_textualize_io]
---

# Review Dashboard

## When to Use

Load this before editing `image/tui/bluefin_review_tui.py`,
`tests/dashboard_pilot.py`, or `tests/dashboard-contract.sh` — the maintainer
surface `just review-queue` opens.

## When Not to Use

Do not use this for the launcher that starts the container
([`launcher.md`](launcher.md)), the image it runs in
([`image-build.md`](image-build.md)), the snapshot generator
([`static-pr-queue.md`](static-pr-queue.md)), or Hive's contributor protocol
([`hive-runtime.md`](hive-runtime.md)).

## Semantic Foundation

`image/tui/semantic_view.py` is the pure semantic contract for the dashboard.
Its builders consume queue snapshots and validated `ReviewResult` values; they
do not call Textual, GitHub, Hive, a harness, or a mutation gate.

`ActionID` is a stable shared registry. Verdict selection and submission are
separate intents: `CHOOSE_REVIEW_VERDICT`, `APPROVE_REVIEW`,
`REQUEST_CHANGES`, `COMMENT_REVIEW`, and `SUBMIT_REVIEW`. Pull-request
mutation intents are explicit (`APPROVE_AND_QUEUE`, `MERGE_NOW`,
`UPDATE_BRANCH`, `CLOSE_PULL_REQUEST`, `ADD_PULL_REQUEST_COMMENT`, and
`RESOLVE_DUPLICATES`); `COPY_REVIEW_CONTEXT` is read-only. Navigation owns
`NAVIGATE_UP`, `NAVIGATE_DOWN`, `NAVIGATE_FIRST`, `NAVIGATE_LAST`,
`NAVIGATE_PAGE_UP`, `NAVIGATE_PAGE_DOWN`, `PANE_NEXT`, `PANE_PREVIOUS`,
`BACK`, `HELP`, and `OPEN_COMMAND_PALETTE`. Harness preparation owns
`SWITCH_HARNESS`, `PREPARE_HARNESS`, `SIGN_IN_HARNESS`, `INSTALL_HARNESS`,
`RETRY_HARNESS_DETECTION`, `HARNESS_DIAGNOSTICS`, and `START_REVIEW`.
Review-body intent is `GENERATE_BODY`, `EDIT_BODY`, `PREVIEW_BODY`, and
`SUBMIT_REVIEW`. The registry must not reintroduce ambiguous `REJECT`,
`LEAVE_REVIEW`, `COMMENT`, or `HANDOFF` identifiers.

`ActionSpec.suspended_in_editor` marks navigation and pane intents that must
be suppressed while a text editor owns focus. `mutating` identifies a
GitHub-side mutation; `confirmation_required` also records local preparation
actions that require explicit human consent.

`QueueRow` and `DecisionCard` carry the pull-request identity, TL;DR, current
and reviewed heads, freshness, CI, mergeability, provenance, verification,
findings, and available human actions. A full current head is bound only when
Codex-style `provenance.head_sha` or the landed Goose `live.head` evidence is
the exact 40-character SHA. An abbreviated Goose head is insufficient: it
produces `STALE` and withholds the current exact head. If an adapter receives
an abbreviated Goose head, repository-backed unique expansion belongs before
this pure builder; the builder performs no repository lookup. Missing evidence
or any disagreement also produces `STALE` and cannot produce a clean card.
`effort` is preferred while `reasoning_effort` remains accepted for existing
adapters. `ReviewStateView` owns the lifecycle states `READY`, `RUNNING`,
`STALE`, and `CANCELLED`.

## Core Process

1. **Every mutation goes through `mutate_all()`.** It shows the exact `gh`
   commands and runs nothing until the maintainer types the pull request
   number. The read-only `gh()` helper must never carry a mutating verb.
2. **One decision is one gate.** An action needing several `gh` calls passes
   them all to a single `mutate_all()` so the whole sequence is confirmed
   once and then runs to completion. Never chain gated calls through a
   completion callback: it asks the maintainer to confirm the same decision
   twice, which trains the number as a reflex.
3. **Order a sequence so its first failure is harmless.** `mutate_all()`
   stops at the first non-zero exit. Put the step that can fail without
   consequence first — creating a missing `lgtm` label before submitting the
   approval means a failure leaves no approval that nothing will act on.
4. **A failure must survive the notification.** Record it on the `Stop`, mark
   the row, count it in the status line, and keep the stop selected so a
   batch carries it forward. A toast is gone before a batch of eight
   finishes.
5. **Batch every action that a maintainer repeats.** Queueing, merging and
   updating branches all take the batch selection when one exists, one gate
   per pull request.
6. **Add the behaviour to `tests/dashboard_pilot.py`**, which drives the real
   app through `run_test()`. The static greps in
   `tests/dashboard-contract.sh` are for proving *absence* — a power the
   dashboard must not have. Presence is proven by pressing the key.
7. **Completed reviews cross the `ReviewResult` contract.** The current Goose
   adapter accepts its JSONL findings and orchestrator progress records. An
   exit-zero transcript without that structure is `unparsable`, never clean.
   The Codex adapter accepts one complete official JSONL run: one thread and
   turn start, one final result-bearing `item.completed` agent message, and an
   immediately following successful `turn.completed`. Bare results,
   ambiguous result messages, and malformed, failed, cancelled, out-of-order,
   or trailing terminal events are `unparsable` with bounded raw evidence.
   It enables code-mode-only and the bundled official code-mode host, disables
   direct-tool fallback, and uses the review container as the shell isolation
   boundary so the CLI never depends on a nested bubblewrap sandbox.
   Keep the decision card concise and keep bounded raw evidence reachable with
   `e`; backend prose does not belong in Textual rendering code.
8. **Keep the acting surface explicit.** The shipped keys cover review,
   merge, branch updates, rejection, handoff, docs, Ghost Cluster, and dupe
   cleanup; label and priority mutation are not part of the dashboard.

## Textual Patterns

Verified against `/websites/textual_textualize_io`.

**Escaping is not optional, and upstream's `escape` is not sufficient.** Both
`rich.markup.escape` and `textual.markup.escape` compile
`(\\*)(\[[a-z#/@][^[]*?])` — the tag pattern matches **lowercase only** — while
the renderer consumes `[H]` and `[WIP]` all the same. A pull request titled
`[WIP] fix the thing` silently lost its prefix. Use the module's own
`escape()`, which escapes every opening bracket:

```python
def escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("[", "\\[")
```

Upstream's own recommendation for mixing variables into markup is template
substitution, which sidesteps the question entirely:

```python
Content.from_markup("hello [bold]$name[/bold]!", name=name)
```

**Links need a quoted URL.** `[link=https://…]` fails the markup value parser
at the colon; `[link="https://…"]` is correct, and reaches the terminal as
OSC 8.

**Never touch the DOM from a thread worker — including the query.** Textual
is not thread-safe. `self.call_from_thread(self.query_one(...).update, text)`
looks safe and is not: the query runs on the worker thread and can race a
repaint. Hand the whole operation over:

```python
@work(thread=True)
def render_context(self, stop: Stop) -> None:
    ...
    self.call_from_thread(self.paint_context, "\n".join(lines))

def paint_context(self, text: str) -> None:
    self.query_one("#context", Static).update(text)
```

**Diffs get Pygments through Rich**: `Syntax(text, "diff", theme="ansi_dark")`.
`ansi_dark` resolves to the terminal's own palette instead of assuming a
background colour.

## Design Rules

- **Show the whole queue by default.** Defaulting to one
  `recommended_action` rendered a 121-stop queue as five and hid every
  merge-ready pull request. When a view is filtered, the status line says how
  many stops are hidden.
- **Colour is never the only carrier of a fact.** Rows colour by state *and*
  carry `⚑ CONFLICTS`, `✓ CI GREEN`, `✗ CI FAILED`, `… CI PENDING`, or
  `? CI UNKNOWN`, as applicable.
- **Direct merge respects known CI state.** Ordinary `[m]` refuses a pull
  request whose snapshot or fetched live evidence says CI failed or is
  pending; GitHub branch protection remains an additional gate.
- **Prefer the snapshot already in memory.** `mergeable_state`, `check_state`,
  `review_state`, `labels` and every duplicate's title arrive with the queue
  and the cluster listing. Colour, the merge-queue meter and the duplicate
  summaries all cost zero extra requests.
- **Distinguish the three merge paths.** `a` approves and applies `lgtm`, an
  opt-in to Hive's sweep. `m` squashes now and is gated on GitHub's `push`
  permission, read per repository. `L` leaves a review and merges nothing.
  A review that can only be given by also queueing or merging is not a review.
- **The completed card reuses those paths.** `L`, `a`, `m`, and `u` return to
  the queue's existing handlers, so permissions, live-head checks, exact
  commands, and typed-number confirmation remain the authority boundary.
- **Show evidence state, not a verdict invented from prose.** The card carries
  exact severity counts, cited file/line findings, engine and live-CI
  verification, duplicate/overlap context, mergeability, head, and
  backend/model provenance. Incomplete, failed, and unparsable results direct
  the reviewer to raw evidence and never display a clean conclusion.
- **Never bypass branch protection.** No `--admin`, no `--delete-branch`, no
  push.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Two confirmations is safer than one." | It is the same decision twice. The second prompt teaches the number as a reflex, and an abort at it leaves half the action applied. |
| "The notification reports the failure." | It is gone before a batch finishes. Mark the row. |
| "A grep proves the feature works." | It proves the source contains a string. The pilot presses the key. |
| "I know this Textual API." | `escape` misses uppercase tags and `[link=…]` needs quotes — both were found by running it, not by remembering it. |

## Red Flags

- `then=lambda: self.mutate(...)` — a chained gate; the contract fails on it.
- Interpolating any GitHub-sourced text into markup without `escape()`.
- `self.query_one(...)` evaluated inside an `@work(thread=True)` body.
- A new mutating verb passed to the read-only `gh()` helper.
- A default view that filters the queue without saying so.
- A feature added with only a `tests/dashboard-contract.sh` grep behind it.

## Verification

```bash
bash tests/dashboard-contract.sh     # static contract + the Textual pilot
python3 tests/review_result_contract.py
bash tests/image-contract.sh
pre-commit run --all-files
```

- [ ] Every new mutation runs through `mutate_all()` and shows its commands.
- [ ] Multi-command actions are one gate, ordered so the first failure is
      harmless.
- [ ] Failures mark the row and keep the stop selected.
- [ ] All GitHub-sourced text passes through `escape()`.
- [ ] No DOM access inside a thread worker.
- [ ] The pilot presses the key and asserts the result.
