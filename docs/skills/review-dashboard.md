---
name: review-dashboard
version: "3.1"
last_updated: 2026-09-09
id: review-dashboard
one_line_purpose: Maintain the Oh My Pi review extension and the retained Textual compatibility dashboard.
entry_point: docs/skills/review-dashboard.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [omp, extension, dashboard, review, textual, maintainer]
description: "Maintains the primary Oh My Pi review extension in image/extension/bluefin-review/ and the retained Textual compatibility dashboard in image/tui/bluefin_review_tui.py. Use when editing review UI or action seams."
metadata:
  type: runbook
  context7-sources: [/websites/textual_textualize_io, /textualize/textual]
---

# Review Dashboard

The primary maintainer review product is the Oh My Pi extension in
`image/extension/bluefin-review/`, launched in source mode via `bin/omp-review`
or packaged via `just review-appliance` and `image/appliance/Containerfile`.
The retained Textual maintainer dashboard in `image/tui/bluefin_review_tui.py`
is launched via `just review-queue` and serves compatibility consumers in
`image/Containerfile`.

## Oh My Pi Review Mode (Primary Maintainer Product)
`bin/omp-review` and `just review-appliance` launch Oh My Pi in dedicated
review mode. The extension provides:

1. **Queue rail & status**: Live queue rail beneath editor with active bindings
   (`alt+b` dashboard, `alt+j`/`alt+k` next/prev, `alt+i` PRs/issues, `alt+o` repo,
   `alt+u` refresh, `alt+y` cite, `alt+x` select).
2. **Review dashboard overlay**: Modal dashboard (`alt+b`) with keyboard navigation
   (`j`/`k` move, `tab` pane, `h`/`l` fold, `/` filter, `r` review, `d` diff,
   `a` approve+merge, `f` fix, `s` slay, `b` snapshot, `?` help, `q` close).
   Right pane displays a pipeline trace (run state, review/landing events, findings).
3. **Companion review agents**: Specialized task agents under
   `image/extension/bluefin-review/agents/` (`bluefin-doctrine`, `bluefin-reviewer`,
   `bluefin-security`, `bluefin-correctness`, `bluefin-test-coverage`,
   `bluefin-simplicity`, `bluefin-ci-triage`, `k3-final-review`).
4. **Inspection tools**: Registered tools in `tools.ts` (`bluefin_review_status`,
   `bluefin_review_queue`, `bluefin_review_diff`, `bluefin_review_trace`,
   `bluefin_hive_lookup`) returning real structured data.
5. **Hive context**: When configured, Hive positions order the queue (read-only).
   Otherwise, the queue is classified from live GitHub evidence into the action
   vocabulary (`ready-for-human-merge`, `review`, `resolve-conflicts`, `fix-ci`,
   `investigate`, `triage`).
Detailed appliance instructions live in [`docs/appliance.md`](../appliance.md).

## Retained Textual Compatibility Dashboard

`just review-queue` serves compatibility consumers in `image/Containerfile`.
It reads open PRs and issues via GraphQL, displaying review, mergeability, and
CI-rollup evidence. It retains the last good live GitHub queue and read-only Hive view.
Receipt-verified clean reviews and terminal landing completion request
reconciliation without polling. `R` explicitly refetches.
`A` confirms the selected PRs as a landing batch and returns to the queue. `w` opens
the landing view, where `j`/`k` select a batch target, `x` stops it, and Escape returns.
The landing view reports observed stage, model, round, and terminal counts without inventing an ETA.
Clickable footers keep controls visible across standard and compact layouts.
## When to Use

Load this before editing `image/extension/bluefin-review/` (the primary OMP
extension), or before maintaining the retained Textual dashboard in
`image/tui/bluefin_review_tui.py`, `tests/dashboard_pilot.py`, or
`tests/dashboard-contract.sh`.

## When Not to Use

Do not use this for the root launcher recipes ([`launcher.md`](launcher.md)),
appliance or contributor image builds ([`image-build.md`](image-build.md)), or
the Hive contributor WebSocket protocol ([`hive-runtime.md`](hive-runtime.md)).
For general appliance setup and configuration, see [`docs/appliance.md`](../appliance.md).

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

Focused CI-evidence fixtures must disable unrelated mount-time readers so callbacks
do not race. Verified against Context7 `/textualize/textual`:
- **Bracket Escaping:** Always escape opening brackets in PR titles or git text via `escape(text)`.
- **Quoted Links:** Terminal OSC 8 links require quotes: `[link="https://..."]`.
- **Theme Variables:** Use theme pairs like `[$text-success on $success-muted]` for status bars.
- **Thread Safety:** Never touch the DOM or call `query_one()` from worker threads.
- **Diffs & Conversations:** Rich Pygments for diffs (`Syntax(text, "diff", theme="ansi_dark")`)
  and Textual `Markdown` widget for conversations (`CommentsScreen`).
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
- **Distinguish the merge paths.** `a` requests Hive's App-authored approval and applies `lgtm`.
  On a selection, `A` dispatches one landing agent for the batch; without selection `A` no-ops.
  `w` focuses batch controls, `m` squashes now (gated on `push` permission), and `L` leaves a review.
  `$` ("slay") executes the full review weapon pipeline: reviews unreviewed PRs,
  dispatches automated fix-and-land if findings are detected, and enqueues batch
  landing if clean. Selected issues ride the same gate and dispatch an issue
  fix agent whose deliverable is a pull request under the maintainer's own
  account (or one evidenced finding comment) — never a merge, approval, or
  label: review policy routes the opened pull request to another contributor.
  Prior reviewed identities are captured before live refresh,
  and exact-head revalidation aborts landing when a pull request advances to a
  new head on GitHub, preventing stale approvals from landing unreviewed code.
- **Mixed workboard and three-way view cycle:** The dashboard opens on PRs. `I` cycles
  `prs -> issues -> mixed -> prs`. Issues display metadata/description and recent comments.
  Triage actions: `c` comments, `x` closes with triage comment behind typed number gate,
  `o` opens browser, `y` copies handoff. PR actions (`r`, `v`, `m`, `u`, `a`/`A`, `L`) guard against issues.
- **Keyboard reference modal on `?`**: `?` opens `HelpScreen`, a modal
  grouping navigation, review, batching, and mutations with semantic accent and
  warning badges; dismisses cleanly with `?`, `q`, or `Esc`.
- **Evidence-first CI failure triage card**: Failing, errored, or timed-out checks surface an immediate `CI FAILURE TRIAGE` section displaying the workflow name, job/check context, failing step, head SHA, execution timestamps, and direct evidence URLs.
- **CI evidence stays bounded and untrusted**: Log acquisition is on demand for selected repo, PR, head, run, attempt. Displayed text is bounded, redacted, and stripped of terminal controls.
- **Review/action receipts stay observational**: A completed `ReviewResult` and correlated action are retained only in bounded session memory, keyed by repo, PR, and head. Receipts describe evidence/action pairs and have no merge authority.
- **Responsive screens & celebration**: Small terminals condense activity and keep key rows visible. Draining active reviews triggers celebration (`ALL SYSTEMS SLAY`).
- **Treat the Hive API as JSON, not a browser.** The read-only status probe reports missing config, credentials, transport failure, and concise states. Ranks `#N` are display evidence only.
## Batch Review and Landing

Batch landings partition across independent repository lanes via background agents. Evidenced review findings are repaired through `[$]` behind slay's gates; see [`landing-batches.md`](landing-batches.md) and [`review-scheduler.md`](review-scheduler.md). `b` toggles highlighted rows, `B` selects/clears visible rows, `Space` toggles and advances, and `r` reviews the batch.

## Common Rationalizations & Red Flags

- Two confirmations is reflex, not safety. The first prompt is the decision.
- Never interpolate GitHub- or agent-sourced text without `escape()`. Never access `query_one()` from worker threads.
- Remote-sourced state must always display its age.
- Headless tests verify logic; Pilot verifies live interaction and state transitions.
## Verification

```bash
bash tests/omp-review-mode.sh          # OMP extension unit & contract tests
bash tests/dashboard-contract.sh       # static contract + Textual pilot
python3 tests/review_result_contract.py
bash tests/image-contract.sh
pre-commit run --all-files
```
- [ ] Every new mutation runs through `mutate_all()` and shows its commands.
- [ ] Multi-command actions are one gate, ordered so the first failure is harmless.
- [ ] Failures mark the row; terminal landing outcomes require explicit reselection.
- [ ] All GitHub- and agent-sourced text passes through `escape()`. No thread DOM access.
- [ ] The pilot presses the key and asserts the result.
