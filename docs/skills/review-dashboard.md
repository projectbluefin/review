---
name: review-dashboard
version: "2.9"
last_updated: 2026-09-08
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
  context7-sources: [/websites/textual_textualize_io, /textualize/textual]
---

# Review Dashboard

`just review-queue` reads the organization's open pull requests live: one paginated GraphQL search through the shipped GitHub CLI, carrying the review, mergeability, and CI-rollup evidence each recommended action is classified from. `just review-queue owner/repo` reads that repository's open pull requests the same way and normalizes them into the same repository-qualified queue rows. The authenticated maintainer's own pull requests remain hidden. The dashboard distinguishes ready, empty, missing, inaccessible, malformed, and failed sources; `R` rereads whichever source is active. The flag form `--repo` narrows the org-wide queue to one repository.

The dashboard retains its last good live GitHub queue and read-only Hive view. Receipt-verified clean reviews, successful mutations or batch queues, and terminal landing completion request reconciliation. Requests coalesce with one bounded follow-up; there is no polling or Hive assignment/completion mutation. `R` is the explicit-read control; failed reads retain visibly aged data.

## When to Use

Load this before editing `image/tui/bluefin_review_tui.py`,
`tests/dashboard_pilot.py`, or `tests/dashboard-contract.sh` — the maintainer
surface `just review-queue` opens.

## When Not to Use

Do not use this for the launcher ([`launcher.md`](launcher.md)), the container image
([`image-build.md`](image-build.md)), or Hive's protocol ([`hive-runtime.md`](hive-runtime.md)).

## Semantic Foundation

`image/tui/semantic_view.py` defines the pure semantic contract for the dashboard.
`ActionID` separates verdict selection, review submission, PR mutations, and navigation.
`COMMANDS` projects live bindings (`j/k`, `g/G`, `Ctrl-d/Ctrl-u`, `h/l`, Enter,
Escape, `q`, `Ctrl-C`, `/`, `r`, `y`, `Ctrl-p`, `:`, `?`, `Tab`/`I`).

`QueueRow` and `DecisionCard` bind head SHA, CI rollup, mergeability, and findings.
Right-hand panes scroll evidence (`h`/`l`), `e` opens decisions, and `[u]` updates clean branches.

## Core Process

1. **Every mutation goes through `mutate_all()`.** It shows the exact command
   or Hive request and runs nothing until the maintainer types the pull request
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
   the row, and count it in the status line. Review and ordinary mutation
   failures keep their selection; terminal landing outcomes are visibly
   deselected and need an explicit new selection before retry. A toast is gone
   before a batch of eight finishes.
5. **Batch every action that a maintainer repeats.** Merging and updating
   branches take the batch selection when one exists. `A` on a selection is
   different: the reviewed batch becomes one landing agent's brief behind one
   proportionate gate (see "Batch landing" below), not one gate per PR.
6. **Add the behaviour to `tests/dashboard_pilot.py`**, which drives the real
   app through `run_test()`. The static greps in
   `tests/dashboard-contract.sh` are for proving *absence* — a power the
   dashboard must not have. Presence is proven by pressing the key.
7. **Completed reviews cross the `ReviewResult` contract.** Transcripts without
   valid JSONL findings and terminal events are `unparsable`, never clean. Keep
   decision cards concise and bounded raw evidence on `e`.
8. **Keep the acting surface and activity explicit.** The shipped keys cover review, merge, branch updates, rejection, handoff, docs, and dupe cleanup; label and priority mutation are excluded. Above the queue, `AGENT ACTIVITY` shows active parent reviews, Check workers, landing agents, queued work, bounded repository-qualified rows, and freshness. Hive rows are read-only, unavailable when malformed, and never inferred from a name, prompt, title, or task identifier. Selected rows also state remote analysis in progress, local clean/findings drafts, and a GitHub review submitted by the authenticated maintainer; all derive from retained local/live evidence and add no transport or polling.

## Textual Patterns

Verified against Context7 `/textualize/textual`:
- **Bracket Escaping:** Always escape opening brackets in PR titles or git text via `escape(text)` so `[WIP]` or `[H]` do not corrupt Rich/Textual markup.
- **Quoted Links:** Terminal OSC 8 links require quotes: `[link="https://..."]`.
- **Theme Variables:** Use theme pairs like `[$text-success on $success-muted]` for status bars.
- **Thread Safety:** Never touch the DOM or call `query_one()` from worker threads. Dispatch updates through `self.call_from_thread(self.method, data)`.

**Diffs get Pygments through Rich**: `Syntax(text, "diff", theme="ansi_dark")`. `ansi_dark` resolves to the terminal's own palette instead of assuming a background colour. `DiffScreen` keeps GitHub's complete response in bounded pages; `[` and `]` navigate them, while loading, success, and fetch error are distinct states. `[o]` is only an optional browser escape hatch.

**Conversations get Textual's `Markdown` widget**. `CommentsScreen` renders an issue or pull request's opening post, comments, and reviews as one document ordered by timestamp across both kinds. A review with no body is dropped unless its state is `APPROVED` or `CHANGES_REQUESTED`, where the state *is* the verdict. `[C]` opens it from the queue, for issues too unlike the diff; `[c]` opens it from the review screen. A late refresh is discarded unless it matches the generation that asked for it.

## Design Rules

- **Show the whole queue by default.** Defaulting to one
  `recommended_action` rendered a 121-stop queue as five and hid every
  merge-ready pull request. When a view is filtered, the status line says how
  many stops are hidden.
- **Keep mutation failures inspectable.** The selected stop and recovery screen
  retain the exact command, GitHub error, checks, and branch state after the
  notification disappears. Update, retry, queue, and skip are explicit; a true
  conflict offers manual handoff without a bypass.
- **Colour is never the only carrier of a fact.** Rows colour by state *and*
  carry `⚑ CONFLICTS`, `✓ CI GREEN`, `✗ CI FAILED`, `… CI PENDING`, or `? CI UNKNOWN`.
  The batch queue applies the same rule three layers deep — printed state word,
  glyph from `LANDING_STATE_STYLES`, then colour — so a colourless read loses
  nothing. Selection leads with `●` and carries a full-row background.
- **Direct merge respects known CI state.** Ordinary `[m]` refuses a pull
  request whose queue evidence or fetched live evidence says CI failed or is
  pending; GitHub branch protection remains an additional gate.
- **Roll up checks at the exact current head.** Fetch `headRefOid` and
  `statusCheckRollup` in one `gh pr view`, group check runs by workflow and job
  name (commit statuses by context), and use only the newest run per stable
  context, so a superseded cancellation cannot fail a successful rerun.
  Authoritative failures, cancellations, pending and absent checks, and
  GitHub's merge state remain separate evidence.
- **Prefer the queue evidence already in memory.** `mergeable_state`, `check_state`,
  `review_state`, `labels` and every duplicate's title arrive with the queue
  and the cluster listing. Colour, the merge-queue meter and the duplicate
  summaries all cost zero extra requests.
- **Classify from evidence, never from a title.** `MECHANICAL` marks
  merging the base into a green, mergeable branch that is merely behind. It
  requires a Renovate author, update type (`digest`, `pin`, `patch`, `minor`),
  an open non-draft pull request, `MERGEABLE` + `BEHIND`, and all checks green.
  `[U]` selects those stops for gated `[u]`.
- **Distinguish the merge paths.** `a` requests Hive's App-authored approval
  and applies `lgtm`. On a selection, `A` dispatches one landing agent for the
  batch; without a selection `A` no-ops. `w` opens the batch queue. `m` squashes
  now (gated on `push` permission). `L` leaves a review and merges nothing.
  `$` ("slay") executes the full review weapon pipeline: reviews unreviewed PRs,
  dispatches automated fix-and-land if findings are detected, and enqueues batch
  landing if clean.
- **Issues view and triage:** `Tab` or `I` toggles between the PR and issues
  queues. Highlighting an issue renders its metadata and description in details,
  and recent comments in context. Triage actions: `c` comments via `CommentBody`,
  `CommentPreview`, and the typed issue-number gate; `x` closes the issue with a
  triage comment behind the typed number gate; `o` opens in browser; `y` copies
  handoff. PR actions (`r`, `v`, `m`, `u`, `a`/`A`, `L`) notify PRs only.
- **Keyboard reference modal on `?`**: `?` opens `HelpScreen`, a modal
  grouping navigation, review, batching, and mutations with cyan/magenta
  badges; dismisses cleanly with `?`, `q`, or `Esc`.
- **Evidence-first CI failure triage card**: Failing, errored, or timed-out checks surface an immediate `CI FAILURE TRIAGE` section displaying the workflow name, job/check context, failing step, head SHA, execution timestamps, and direct evidence URLs.
- **Empty queue celebration (`ALL SYSTEMS SLAY`)**: Draining the active review source (repository-scoped, own-work excluded) triggers a one-shot 1.3s retro sequence: Round 8/Fight (400ms) → Bluefin charging `SLAYDOKEN!` (300ms) → `9999!` hit (250ms) → `K.O.` (350ms) → `ALL SYSTEMS SLAY` held frame. Startup with an empty queue skips directly to the held frame. Action-filtered views do not trigger celebration.
- **Treat the Hive API as JSON, not a browser.** The read-only status probe reports missing hub config, missing credentials, network/auth failure, edge/login redirects, malformed responses, and server failure as separate concise states. The queue POST succeeds only when bounded JSON says `queued`; the typed PR-number gate remains the boundary. Failed probes leave queue/review evidence visible and mark retained assignments as last-known. Direct GitHub review and merge stay available.

## Batch Review and Landing

Batch landings partition across independent repository lanes and execute via background agents. Evidenced review findings enable `[f] fix & land in background`. See [`landing-batches.md`](landing-batches.md) for the `[$]` state machine, landing gate, concurrency lanes, and state persistence, and [`review-scheduler.md`](review-scheduler.md) for admission, capacity, and transport reuse.
The review lane keeps separate `review-batches/` JSONL state. Resolve every exact cache hit before applying capacity, and trust a receipt only when its full run and check-scope identity matches. Review the explicit `base...head` range from a clean isolated worktree. Synchronize cancellation with submission and cache publication; a cancelled run cannot publish or delete another session's receipt. Local lanes and the Hive fleet are two separate concurrency displays and are never conflated; see [`review-monitoring.md`](review-monitoring.md).
On the dashboard, `B` selects or clears every visible row, `Space` toggles the highlighted row and advances, `n` jumps to the next pull request lacking the maintainer's own GitHub review, and `r` reviews the selection as a batch while retaining its selection on snapshot failure. Queue rows show running and verdict badges. `Enter` on a reviewed row reloads GitHub evidence before rendering cached analysis; CI, mergeability, reviews, and overlap are never restored from the cache.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Two confirmations is safer than one." | It is the same decision twice. The second prompt teaches the number as a reflex, and an abort at it leaves half the action applied. |
| "The notification reports the failure." | It is gone before a batch finishes. Mark the row. |
| "A grep proves the feature works." | It proves the source contains a string. The pilot presses the key. |
| "I know this Textual API." | `escape` misses uppercase tags and `[link=…]` needs quotes — both were found by running it, not by remembering it. |

## Red Flags

- `then=lambda: self.mutate(...)` — a chained gate; the contract fails on it.
- Interpolating any GitHub- or agent-sourced text into markup without `escape()`. An agent-reported JSONL state is attacker-shaped text too:
  unescaped, `waiting[/][blink]OWNED` raised `MarkupError` in
  `rows.update()` and took the whole batch-queue screen down.
- `self.query_one(...)` evaluated inside an `@work(thread=True)` body.
- A new mutating verb passed to the read-only `gh()` helper.
- A default view that filters the queue without saying so.
- A feature added with only a `tests/dashboard-contract.sh` grep behind it.
- Remote-sourced state rendered without its age.
- A core-loop path that gates on `hive_api_base()` being set.

## Exact-Head Re-Review

When a completed result is bound to an older head H0 while the live snapshot is H1,
the decision card appends a bounded delta: both identities, prior findings, and H1 evidence.
Fallback reasons direct to a full review if merge-base changes or responses are partial.

## Verification

```bash
bash tests/dashboard-contract.sh     # static contract + the Textual pilot
python3 tests/review_result_contract.py
bash tests/image-contract.sh
pre-commit run --all-files
```

- [ ] Every new mutation runs through `mutate_all()` and shows its commands.
- [ ] Multi-command actions are one gate, ordered so the first failure is harmless.
- [ ] Failures mark the row; terminal landing outcomes require explicit reselection.
- [ ] All GitHub- and agent-sourced text passes through `escape()`. No thread DOM access.
- [ ] The pilot presses the key and asserts the result.
