---
name: review-dashboard
version: "5.4"
last_updated: 2026-09-16
id: review-dashboard
one_line_purpose: Maintain the queue, slay lifecycles, and workflowz workbench.
entry_point: docs/skills/review-dashboard.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [omp, extension, dashboard, review, maintainer, workflowz]
description: "Maintains the OMP review workbench. Use for queue ordering, slay or autoslay, workflowz issue batches, durable batch state, dashboard controls, or mutation guards."
metadata:
  type: runbook
  context7-sources: []
---

# Review Workbench

The only maintainer UI is `image/extension/bluefin-review/`: `bin/omp-review`
loads source, while `just review-queue` and `just review-appliance` launch the
packaged extension. The Hive contributor runtime attaches to Hive's OMP session.

## When to Use

Use this skill for queue ordering, slay/autoslay, workflowz dispatch, durable
batch state, dashboard controls, prompts, or mutation guards. Use `launcher.md`
for launch mechanics, `review-checks.md` for doctrine, and `hive-runtime.md` for
contributor assignment behavior.

## Core Process

1. Trace the key or flag from `dashboard.ts` through `extension.ts` to its prompt.
2. Keep reviewers read-only. Slay authorizes bounded PR review/repair/landing or
   issue implementation through pull-request submission.
3. Add a headless interaction test, then exercise the real foreground workbench.

## Authority

- GitHub owns repository state.
- Hive owns contributor selection, assignment, prompt injection, and output
  capture. The workbench may read Hive order but never claim contributor work.
- OMP owns sessions, agents, tasks, tools, workflowz workpools, and cancellation.
- The extension owns queue projection, durable human intent, mutation guards,
  and presentation.
- Humans own approval and merge decisions; confirmed slay intent delegates the
  bounded coordinator lifecycle that executes them.

## Screen

The queue, focused item, Dagger-style execution trace, and prompt share one
screen. The top gauge reports mode, position, repository, outcomes, freshness,
Hive ordering, and actionable count. The bottom gauge reports Hive connectivity,
selection count, and the active workflowz slay.
Hive coverage is mode-aware: issue mode counts Hive issue identities, while PR
mode counts direct Hive pull requests and explicit open linked pull requests.
Pull requests discovered through GitHub closing references still inherit the
rank of their Hive issue. Never probe an issue identity as a pull request or
report issue-only backlog as missing PR evidence.

`Tab` switches PR/issue mode and every semantic accent between the cool PR
palette and warm issue palette.

| Key | Action |
| --- | --- |
| `Tab` | Toggle pull requests and issues |
| `j` / `k` | Move through the queue |
| `Space` | Toggle the focused item |
| `A` / `x` | Select the filtered slice / clear selection |
| `Alt-B` | Select or clear the focused repository group |
| `s` | Slay selected PRs, or implement selected issues through submitted PRs |
| `Alt-S` | Repair returned PRs first, then implement the visible issue backlog |
| `f` | Fix selected items in isolated workspaces |
| `d` | Inspect bounded evidence (PR diff, issue discussion) |
| `p` | Pause or resume later wave admission |
| `r` | Refetch GitHub and Hive projections |
| `o` | Change repository or organization scope |
| `/` | Filter the queue |
| `H` / `L` | Toggle Hive-only rows / step through Hive stages |
| `t` | Focus the execution trace |
| `g` / `G` | Jump to the first / last row |
| `h` / `l` | Collapse / expand the focused trace span |
| `c` | Comment after confirmation and live revalidation |
| `Enter` | Open the reader for the focused pull request |
| `v` | Open the focused issue or pull request in a browser |
| `i` | Cite the focused item in the prompt |
| `?` | Show the key guide |
| `q` / `Esc` | Close the workbench |

## Slay execution

Slay has entity-specific terminal conditions. Ordinary pull requests run through
review, isolated repair, fresh review, and landing. Each head first runs as a
fresh `bluefin-reviewer` in one OMP workflowz `task` batch. Reviewers are
read-only; findings dispatch isolated fixers, and a fixed head receives a fresh
review before the coordinator may approve and request a squash merge.

Pull requests authored by the authenticated GitHub user with requested changes
form a `repair-requested` lane ahead of Hive-ranked review work. Workflowz
dispatches isolated fixers, never a self-review, self-approval, or self-merge.
A returned PR is terminal only when GitHub shows a new head SHA.

Issue slay reads the complete issue plus Hive's queue entry and curated
knowledge before deciding and implementing. A multi-issue wave uses one
workflowz `task` call with a fresh isolated item per issue. Each worker opens a
review-ready PR with a closing reference; the issue is terminal only when
GitHub reports that submitted PR. The worker never approves or merges it.

`--autoslay` and `Alt-S` use the same repair-first plan: unless explicitly
started in issue mode, repair all visible returned PRs, then switch to the
visible issue backlog. Work is partitioned into type-homogeneous,
repository-local waves of at most 25 items. OMP's advisor is always enabled and
resolves through `@default`, following the maintainer's selected model.

The PR queue keeps `.github/workflows/` changes and incomplete file lists visible,
marking them blocked from automated review, repair, or landing. Returned PRs remain eligible.
Ordinary PR slay excludes failing or pending CI before reviewer dispatch and
rechecks it before each wave; returned PR repair may address failing CI but
cannot run approval or merge commands. Slay never removes holds, uses admin
bypass, fabricates reviewers, force-pushes, or lands an unreviewed head.

Fresh reviewers receive explicit `repo` and `pull_request` arguments for
`hive_workbench_diff`. Repair agents use `gh repo clone` and `gh pr checkout`
under `$HOME/worktrees`, never `/tmp`, and read effective rules through
`repos/<owner>/<repo>/rules/branches/<branch>`.
The minimal appliance omits repository-specific toolchains; reviewers use
hosted check evidence and report local validation gaps instead of retrying
absent commands or installing packages.

Preserve Hive order inside each lane and partition contiguous repository runs.
Ask workflowz to execute every wave, including a singleton. Never add an
extension-local worker pool, retry loop, scheduler, or agent lifecycle. Advance
on final `agent_end` only after all wave jobs settle. Pausing stops new waves,
not an agent already running.

Persist slay intent, item identity, wave position, and terminal outcomes.
Interrupted slays stay blocked and require explicit redispatch. Never replay a
confirmed mutation. An ordinary PR wave is terminal when every target is closed
or GitHub accepts auto-merge; open targets without auto-merge block redispatch.
The merge queue's effective squash rule overrides the displayed
`autoMergeRequest.mergeMethod`: never disable and re-arm auto-merge because it
says `MERGE`. An accepted auto-merge request is terminal even when additional
human approval remains; report that outstanding gate and move on. Settled
workflowz jobs alone never advance any slay.

## Mutations

Capture repository, item number, entity type, and PR head SHA before preview.
Immediately before mutation, fetch live targets and repository rules again and
reject missing, changed, held, review-blocked, or type-mismatched targets.
Execute `gh` with an argument array, never a shell-composed command. Only a
maintainer-confirmed slay batch carries merge authority.
During an active slay, the extension's pre-execution `tool_call` guard rejects
admin merge bypasses, force pushes, and credential-bearing URL arguments even
when the coordinator ignores its prompt contract.

## Policy seam

Generic queue and execution code must not know Bluefin labels or review rules.
Bluefin action vocabulary lives in `policy.ts`; review doctrine lives in the
companion agents under `image/extension/bluefin-review/agents/`.
Every top-level TypeScript module in the extension must remain reachable from
`index.ts`; delete disconnected implementations and their tests instead of
keeping a second, unwired behavior model.

The registered inspection tools are `hive_workbench_status`,
`hive_workbench_queue`, `hive_workbench_diff`, `hive_workbench_trace`, and
`hive_workbench_lookup`.

## Common Rationalizations

- “Slay is just autoreview.” Review without repair and landing is an incomplete
  slay; reviewer agents stay read-only while the confirmed coordinator owns the
  complete lifecycle.
- “Review needs Hive admission.” Read-only review works from GitHub evidence
  when Hive is absent; issue slay and fix still enforce fresh GitHub admission
  and report unavailable Hive knowledge instead of inventing it.

## Red Flags

- A slay prompt uses the default task agent instead of `bluefin-reviewer` for
  the review stages.
- `s`, `Alt-S`, or `--autoslay` bypasses the common wave validation machinery.
- A reviewer agent approves, merges, or edits instead of returning evidence to
  the coordinator.
- Slay lands without revalidating the exact reviewed head and live GitHub rules.
- A repository wave advances before its OMP jobs settle.
- An issue wave skips Hive queue/knowledge evidence or advances without a submitted PR.

## Verification

```bash
bash tests/omp-review-mode.sh
bash tests/appliance-contract.sh
bash scripts/check-skill-frontmatter.sh
git diff --check
```

For a visible change, launch `bin/omp-review --no-session` in a foreground
terminal, exercise the changed key path, inspect the real screen, and stop it.
