---
name: review-dashboard
version: "3.1"
last_updated: 2026-09-09
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

`just review-queue` reads the organization's open pull requests and open issues live through paginated GraphQL searches using the shipped GitHub CLI, carrying the review, mergeability, and CI-rollup evidence each recommended action is classified from. It opens on the pull-request view; `I` reaches issues and the mixed workboard when needed. `just review-queue owner/repo` reads that repository's open pull requests and issues the same way and normalizes them into repository-qualified queue rows. The flag form `--repo` narrows the queue to one repository without disabling mixed view or issue navigation. Work that is out of the maintainer's hands stays out of the queue: the authenticated maintainer's own pull requests and pull requests already carrying their APPROVED or CHANGES_REQUESTED verdict are hidden, and the status line counts both ("out of my hands: N own, M reviewed by me") so a shrunken queue never reads as a dead organization. The dashboard distinguishes ready, empty, missing, inaccessible, malformed, and failed sources; `R` rereads whichever source is active. A source failure displays an explicit error row and status message rather than rendering an empty successful queue.

The dashboard retains its last good live GitHub queue and read-only Hive view. Receipt-verified clean reviews, successful mutations or batch queues, and terminal landing completion request reconciliation. Requests coalesce with one bounded follow-up; there is no polling or Hive assignment/completion mutation. `R` is the explicit-read control; failed reads retain visibly aged data.

The display prefix comes from the image-owned `/opt/bluefin/config/display-brand`
file (the source default is `Project Bluefin Review`); a missing file falls
back to `Review`. It changes screen branding only and never changes repository
targets or permissions.

`A` confirms the selected pull requests as a landing batch and returns the
maintainer to the live queue. `w` opens the deliberate landing view, where
`j`/`k` select an explicit batch target, `x` stops that target's owned process
group, and Escape returns to the queue. The landing view reports observed
stage, model and round when available, terminal/waiting/blocked/failed counts,
elapsed time, and evidence age; it does not invent an ETA.
The waiting count covers only explicit CI or publication waits; a pull request
without a report is shown as `? unreported` rather than being counted as
waiting. The landing viewer keeps only its batch controls visible in the
clickable footer so the key labels remain readable at 120 columns; the CI
failure hint likewise keeps its literal `i` and `Esc` controls. Standard and
desktop layouts reserve 55% for queue rows so identity, CI, and action suffixes
stay together. At compact widths, the activity panel keeps the active landing
identity visible and the steering/key controls use a condensed layout.

## When to Use

Load this before editing `image/tui/bluefin_review_tui.py`,
`tests/dashboard_pilot.py`, or `tests/dashboard-contract.sh` — the maintainer
surface `just review-queue` opens.

## When Not to Use

Do not use this for the launcher ([`launcher.md`](launcher.md)), image build ([`image-build.md`](image-build.md)), or Hive protocol ([`hive-runtime.md`](hive-runtime.md)).

## Semantic Foundation

`image/tui/semantic_view.py` defines the pure semantic contract for the dashboard.
`ActionID` separates verdict selection, review submission, PR mutations, and navigation.
`COMMANDS` projects live bindings (`j/k`, `g/G`, `Ctrl-d/Ctrl-u`, `h/l`, Enter, Escape, `q`, `Ctrl-C`, `/`, `r`, `y`, `Ctrl-p`, `:`, `?`, `I`, `+`, `-`, `p`); `Tab` retains Textual's native focus traversal.
`QueueRow` and `DecisionCard` bind head SHA, CI rollup, mergeability, and findings.
Right-hand panes scroll evidence (`h`/`l`), `e` opens decisions, and `[u]` updates clean branches.

**Textual list behavior is preserved.** Up/down only highlight and update evidence. Enter or click activates the highlighted item. `b` and Space explicitly toggle batch selection; marking repaints its marker in place and never reloads or re-sorts the queue.

## Core Process

1. **Every mutation goes through `mutate_all()`.** It shows the exact command or Hive request and runs nothing until the maintainer types the pull request number. The read-only `gh()` helper must never carry a mutating verb.
2. **One decision is one gate.** An action needing several `gh` calls passes them all to a single `mutate_all()` so the whole sequence is confirmed once and then runs to completion. Never chain gated calls through a completion callback: it asks the maintainer to confirm the same decision twice, training the number as a reflex.
3. **Order a sequence so its first failure is harmless.** `mutate_all()` stops at the first non-zero exit. Put the step that can fail without consequence first — creating a missing `lgtm` label before submitting the approval leaves no orphan approval.
4. **A failure must survive the notification.** Record it on the `Stop`, mark the row, and count it in the status line. Review and mutation failures keep selection; terminal landing outcomes are deselected and require reselection.
5. **Batch every action that a maintainer repeats.** Merging and updating branches take the batch selection when one exists. `A` dispatches one landing agent for the whole selection behind one proportionate gate (see "Batch landing" below).
6. **Add behaviour to `tests/dashboard_pilot.py`**, which drives the real app through `run_test()`. Static assertions in `tests/dashboard-contract.sh` prove absence; presence is proven by pressing the key.
7. **Completed reviews cross the `ReviewResult` contract.** Transcripts without valid JSONL findings and terminal events are `unparsable`, never clean. Keep decision cards concise and bounded raw evidence on `e`.
8. **Keep the queue, controls, and activity explicit.** The main screen has three rows: the full-width actionable item list; one review-metadata row that keeps the harness status, evidence, and context together; then a persistent queue row split between `AGENT ACTIVITY` and `LANDING QUEUE`. The landing side lists active, pending, and bounded recent terminal PRs with their agent-reported lifecycle state and model; it never requires a separate watch screen. `+` and `-` change only this dashboard session's pending-landing concurrency, while `p` pauses or resumes dispatch without interrupting active agents. `w` focuses those controls. Hive rows are read-only, unavailable when malformed, and never inferred. Selected rows state remote analysis in progress, local drafts, and maintainer reviews from retained live evidence with no polling.

## Textual Patterns

Focused CI-evidence fixtures must disable unrelated mount-time queue, issue,
Hive, and harness readers. An issue refresh can otherwise replace the synthetic
PR selection while its CI callback is pending; keep an assertion that the
fixture makes no unrelated GitHub calls rather than masking the race with sleeps.

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
  carry `⚑ CONFLICTS`, `✓ CI GREEN`, `✗ CI FAILED`, `… CI PENDING`, `? CI UNKNOWN`,
  or badges like `⛔ BLOCKED` for unroutable items (branch conflicts, missing required
  clean reviews, or terminal non-mutating states). The batch queue applies the
  same rule three layers deep — printed state word, glyph from
  `LANDING_STATE_STYLES`, then colour — so a colourless read loses nothing.
  Selection leads with `●` and carries a full-row background.
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
  batch; without a selection `A` no-ops. `w` focuses the persistent batch controls.
  `m` squashes
  now (gated on `push` permission). `L` leaves a review and merges nothing.
  `$` ("slay") executes the full review weapon pipeline: reviews unreviewed PRs,
  dispatches automated fix-and-land if findings are detected, and enqueues batch
  landing if clean. Selected issues ride the same gate and dispatch an issue
  fix agent whose deliverable is a pull request under the maintainer's own
  account (or one evidenced finding comment) — never a merge, approval, or
  label: review policy routes the opened pull request to another contributor.
  Prior reviewed identities are captured before live refresh,
  and exact-head revalidation aborts landing when a pull request advances to a
  new head on GitHub, preventing stale approvals from landing unreviewed code.
- **Mixed workboard and three-way view cycle:** The dashboard opens on pull
  requests. `I` cycles through PRs only, issues only, and the mixed
  workboard (both PRs and issues) (`prs -> issues -> mixed -> prs`).
  Highlighting an issue renders its metadata and description in details, and
  recent comments in context. Triage actions: `c` comments via `CommentBody`,
  `CommentPreview`, and the typed issue-number gate; `x` closes the issue with a
  triage comment behind the typed number gate; `o` opens in browser; `y` copies
  handoff. PR actions (`r`, `v`, `m`, `u`, `a`/`A`, `L`) guard against
  issues and apply to pull requests only; `$` applies to both kinds, giving
  issues the fix-agent lane above.
- **Keyboard reference modal on `?`**: `?` opens `HelpScreen`, a modal
  grouping navigation, review, batching, and mutations with semantic accent and
  warning badges; dismisses cleanly with `?`, `q`, or `Esc`.
- **Evidence-first CI failure triage card**: Failing, errored, or timed-out checks surface an immediate `CI FAILURE TRIAGE` section displaying the workflow name, job/check context, failing step, head SHA, execution timestamps, and direct evidence URLs.
- **CI evidence stays bounded and untrusted**: Log acquisition is on demand and
  tied to the selected repository, pull request, head, run, and attempt. Missing
  logs, permission failures, transport failures, and available logs remain
  distinct; displayed text is bounded, redacted, and stripped of terminal
  controls.
- **Review/action receipts stay observational**: A completed `ReviewResult` and
  a successful correlated action are retained only in bounded session memory,
  keyed by repository, pull request, and exact head. Supported categories keep
  their identity (`approve`, `request-changes`, `approve-and-queue`, or a
  verified `merge`); a queue request is not a completed merge. Missing
  repository/PR provenance, stale heads, failed actions, comments, branch
  updates, and unsupported actions are `unclassified`. The receipt describes an
  evidence/action pair and has no approval or merge authority.
- **Responsive screens preserve focus**: Draft generation runs through a
  Textual worker with generation and edit revision guards, while log refreshes
  read bounded tails and restore the user's scroll position. Small terminals
  condense activity, keep both key rows visible, skip hidden panes during focus
  navigation, keep transient notices above the controls, and expose compact CI
  evidence through a focusable scroll pane. Theme-token accent, warning, error,
  selection, and muted metadata colors supplement text and icons; no-color,
  reduced-motion, and ASCII modes carry the same facts in text.
- **Empty queue celebration (`ALL SYSTEMS SLAY`)**: Draining the active review source (repository-scoped, own-work excluded) triggers a one-shot 1.3s retro sequence: Round 8/Fight (400ms) → Bluefin charging `SLAYDOKEN!` (300ms) → `9999!` hit (250ms) → `K.O.` (350ms) → `ALL SYSTEMS SLAY` held frame. Startup with an empty queue skips directly to the held frame. Action-filtered views do not trigger celebration.
- **Treat the Hive API as JSON, not a browser.** The read-only status probe
  reports missing hub config, missing credentials, network/auth failure,
  edge/login redirects, malformed responses, and server failure as separate
  concise states. The queue POST succeeds only when bounded JSON says `queued`;
  the typed PR-number gate remains the boundary. Failed probes leave
  queue/review evidence visible and mark retained assignments as last-known.
  Hive contributor projections (ready-queue ranks `#N` and triage stages) are
  read-only display evidence and do not dictate maintainer queue ordering or
  selection priority. Retained or unavailable Hive rank evidence carries an
  explicit status and age (for example, `[dim]#1 (retained <1m ago)[/dim]`)
  rather than unqualified current ranks. Optional Hive enrichment endpoints
  (`/api/contribute/queue`, `/api/contribute/triage`) with documented shapes
  (such as stage count maps) do not fail a healthy Hive reconciliation when
  absent. Direct GitHub review and merge stay available.

## Batch Review and Landing

Batch landings partition across independent repository lanes and execute via background agents. Evidenced review findings are repaired through `[$]`, which dispatches the fixer behind slay's gates; the standalone `[f]`/`[F]` fix lane is deleted — it ran the same fixer with no confirmation, no blocked-reason check, and no durable run record. See [`landing-batches.md`](landing-batches.md) for the `[$]` state machine, landing gate, concurrency lanes, and state persistence, and [`review-scheduler.md`](review-scheduler.md) for admission, capacity, and transport reuse.
The review lane keeps separate `review-batches/` JSONL state. Resolve every exact cache hit before applying capacity, and trust a receipt only when its full run and check-scope identity matches. Review the explicit `base...head` range from a clean isolated worktree. Synchronize cancellation with submission and cache publication; a cancelled run cannot publish or delete another session's receipt. Local lanes and the Hive fleet are two separate concurrency displays and are never conflated; see [`review-monitoring.md`](review-monitoring.md).
On the dashboard, `b` toggles the highlighted row, `B` selects or clears every visible row, `Space` toggles the highlighted row and advances, `n` jumps to the next pull request lacking the maintainer's own GitHub review, and `r` reviews the selection as a batch while retaining its selection on snapshot failure. Rows say `QUEUED` in cyan, `IN PROGRESS` in yellow, `DONE` in green, `FAILED` in red, or `BLOCKED` in yellow; failures lead the queue, followed by active, queued, ready, completed, and unroutable blocked work. A completed review remains visible until normal live reconciliation removes a merged pull request. The status bar summarizes those states and retains the three newest repository-qualified merged pull requests from landing records. `Enter` on a reviewed row reloads GitHub evidence before rendering cached analysis; CI, mergeability, reviews, and overlap are never restored from the cache.

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

## Acceptance Evidence

Use the locked `image/tui/requirements.lock` environment for Pilot journeys.
Pilot verifies interaction and state transitions; it does not replace rendered
evidence. For a final candidate, capture the running application at 80x24,
120x40, and a normal desktop size, including queue-after-`A`, landing progress,
CI failure drill-down, editor/slow-provider, focus/resize, and the worker
companion when applicable. Record the exact candidate SHA, scenario, terminal
dimensions, and read-only fixture provenance. A shipped-image PTY smoke with
fake external operations is separate proof from a headless or fixture render.

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
