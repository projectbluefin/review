---
name: pr-workflow
version: "1.10"
last_updated: 2026-08-16
id: pr-workflow
one_line_purpose: Open review pull requests that merge cleanly.
entry_point: docs/skills/pr-workflow.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [git, pullrequest, branches, labels, testing]
description: "Defines pull request, branch, title, trailer, and validation requirements, the factory's seven-label contract and this repository's three automation labels, how to reconcile a long-lived branch with squash-merged main, and why a test must run a feature."
metadata:
  type: policy
  context7-sources: [/pre-commit/pre-commit]
---

# PR Workflow

## When to Use

Load this before creating a branch, preparing a commit, or opening a pull
request in this repository.

## When Not to Use

Do not use this to decide *what* to change or how large it should be — that is
[`contribution-culture.md`](contribution-culture.md) — or to satisfy another
repository's contribution rules, which take precedence in their own tree.

## Core Process

0. Know which of the two workflows you are in. **Hive-assigned contributor
   work is always a branch and a pull request** — the protocol reports a PR
   link as the completion artifact, and the contributor has no write access to
   the target repository anyway. **A maintainer working in their own checkout
   of this repository is not bound by that**: when @castrojo asks for a change
   here, commit it and push it to `main`. Do not route his work through a
   branch and a pull request unless he asks for one, and do not tell him a
   direct push is forbidden. The gate that matters is the validation suite,
   which runs the same either way.

1. Work on a branch for Hive-assigned work, named `clanker/<task_id>`, with
   this commit trailer:

   ```text
   Hive-Task-Id: <task_id>
   ```

2. Open one logical change per pull request. Link its issue with `Closes #NNN`
   when appropriate. Size it for a tired maintainer: repair what is broken,
   leave unrelated fixes for their own change, and never bundle a refactor,
   feature, or new dependency with a fix. See
   [`contribution-culture.md`](contribution-culture.md).
3. Make the pull request title a Conventional Commit. Squash merging makes
   that title the permanent commit message.
4. Update the matching skill document when behavior changes. Do not treat
   documentation-only work as exempt from the protected-branch workflow.
5. Treat local hooks as feedback, not enforcement. GitHub rulesets and
   required checks determine whether a pull request can merge.
6. When asked to merge, clear the blocker rather than reporting it. A stale
   expectation in a test, a missing executable bit, a formatting failure, or a
   branch that is merely `BEHIND` is yours to fix. Only a genuine policy gate —
   the Hive protocol gate, or a required human review — is a stopping point,
   and name it explicitly when you stop. `gh pr merge` refusing while
   `mergeable` is `MERGEABLE` and every check passes usually means the pull
   request is still a draft; check `isDraft` and run `gh pr ready`.
7. Push and open the pull request early for Hive work. The scoped token lasts
   55 minutes and is refreshed at 50 minutes only while the socket stays up,
   and a completion carrying a PR link starts a 168-hour cooldown; report
   completion only after the pull request or other required artifact is
   verifiable.
8. Never add or remove a task-admission label on an issue or pull request —
   including in this repository — to influence what work Hive assigns. Hive is
   the sole authority for task selection, and relabelling to attract or shed an
   assignment is task selection. See [`upstream-hive.md`](upstream-hive.md) for
   the full rule and for upstream triage boundaries.

## The Factory Label Contract

This repository carries `projectbluefin/common`'s canonical label workflow —
*workflows own state; humans provide intent* — and adds nothing to it. Seven
labels exist, and they are the same seven in every factory repository:

| Label | Meaning |
|---|---|
| `1-triage` | New work awaiting human triage |
| `2-discussing` | Work requiring discussion or a clarified design |
| `3-human-queue` | Work admitted to the human-maintained queue |
| `3-clanker-queue` | Work admitted to the agent-maintained queue |
| `4-review` | A pull request awaiting review |
| `blocked` | Blocked on human input or an external dependency |
| `hold` | Intentionally paused |

A human selects at most one numbered label to express the intended next step,
with `blocked` or `hold` as an optional overlay. Everything else — kind, area,
size, priority, source — is issue-body prose or project-field metadata, never a
label. Do not invent a priority taxonomy, and do not build a second state
machine out of comments, slash commands, or local scripts.

Alongside those seven, this repository runs exactly two local automation
labels, each owned by a named mechanism and applied by it alone: `lgtm`
(applied by Hive's authenticated queue endpoint after a maintainer opts in — see
[`review-dashboard.md`](review-dashboard.md)) and `dependencies` (Renovate,
per `renovate.json`). Adding a third means adding the mechanism that owns
it, in the same change.

The full contract, including the human and agent action lists, lives in
`projectbluefin/common`'s `docs/skills/label-workflow.md`. Read it there rather
than restating it here; this section records only what is local.

## Reconciling A Long-Lived Branch

`main` squash-merges, which rewrites commit SHAs. A branch that predates
several merges therefore looks far more divergent than it is, and the usual
measurements all mislead:

- `git cherry main <branch>` reports content already on `main` as unmerged,
  because the squashed commit is a different object.
- The three-dot range `main...HEAD`, which is what the pull request renders,
  inflates the change by replaying work `main` already has.
- The two-dot range `main..HEAD` understates it by hiding what the merge
  will remove.

Read both ranges before judging the size of a reconciliation, and confirm
per file rather than trusting either total:

```bash
git diff --stat main...HEAD   # what the pull request shows
git diff --stat main..HEAD    # the true net difference
git log --oneline main --  <path>   # did this land on main already?
```

Merge `main` into the branch. Do not rebase: features that landed on `main`
after the merge base exist only on that side, and rebasing replays the stale
branch on top of them, reintroducing deletions of files the branch never had.

Resolve add/add conflicts hunk by hunk. `git checkout --ours` and
`--theirs` operate on the whole file and silently discard the hunks Git
already merged correctly from the other side. Keep the feature that landed on
`main` **and** the newer behavior from the branch, then confirm both survived
before committing. A merge can also duplicate an adjacent block that neither
side duplicated; re-read the resolved file rather than trusting the marker
count to reach zero.

When a pinned dependency conflicts, resolve by date rather than by side:

```bash
gh api repos/<owner>/<repo>/commits/<sha> --jq '.commit.committer.date'
```

## Committing In A Repository That Has Uncommitted Work

A working tree you did not create may hold someone's uncommitted work. Staging
in it is destructive: `git add -A`, `git add .`, and a bare `git checkout --`
will sweep up or discard changes that are not yours, and the loss is silent
because the diff you review afterwards looks correct.

Before committing anywhere, check:

```bash
git status --short
```

If that prints anything you did not write, do not commit in place. Create a
throwaway worktree from the pushed branch point, make the change there, and
leave the original tree untouched:

```bash
git fetch origin
git worktree add /tmp/work -b my-change origin/main
cd /tmp/work
```

Commit, push, and open the pull request from `/tmp/work`, then remove it with
`git worktree remove /tmp/work`. The dirty tree never changes state, so there
is nothing to restore.

Stage files by name, never by wildcard, even in a clean tree. `git add
path/to/file` cannot pick up a file you did not intend to touch.

If work is clobbered anyway, `git add` has already written the blob to the
object database and `git reset` does not remove it. Recover with:

```bash
git fsck --unreachable --no-reflogs | grep blob
git cat-file -p <sha>
```

Prefer not needing that.

## Never Quote A CI-Skip Directive In A Commit Message

GitHub reads its skip directives — `[skip ci]`, `[ci skip]`, `[skip actions]`,
`[actions skip]` — anywhere in the head commit message, not just the subject.
A commit that merely *writes about* one skips its own validation and publish,
lands on `main`, and shows no failed check because nothing ran.

That happened here: the commit teaching the dashboard to escape bracketed
titles quoted the directive as its example, and the fix sat on `main` while
`:stable` stayed on the parent commit.

Write "GitHub's CI-skip directive" or `skip-ci` without brackets.
`scripts/check-commit-message.sh` runs as a `commit-msg` hook and refuses the
bracketed forms unless `ALLOW_SKIP_CI=1` says the skip is meant. It cannot be
a CI check: a message that skips CI skips the check that would catch it.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The branch is stale but I can resolve the conflicts." | Count them first. Sixteen conflicts to land a one-file change produces a diff nobody can review against the change it claims to make; re-cut from the current base instead. |
| "It passed CI, so it shipped." | A green workflow has meant "nothing ran" and "nothing published" here. Verify the artifact, not the check. |
| "Documentation-only work can skip the workflow." | The protected-branch rules do not have a docs exemption, and a stale skill misroutes every agent that reads it. |

## Red Flags

- Quoting a GitHub CI-skip directive in a commit message.
- Committing from a working tree that holds someone else's uncommitted work.
- Staging with `git add -A` or `git add .` rather than by explicit path.
- Routing a maintainer's own change through a branch and pull request when
  they asked for a push, or calling a direct push to `main` forbidden.
- A non-Conventional pull request title.
- Combining unrelated changes in one pull request.
- Growing a diff because scoping it was harder than writing it.
- Adding a feature, dependency, or refactor to a repair.
- Omitting the Hive task trailer from assigned work.
- Using `--no-verify` to bypass a real failure.
- Reporting task completion before the required artifact exists.
- Adding or removing a task-admission label to influence a Hive assignment.
- A label outside the seven canonical names plus this repository's three
  documented automation labels.
- Recording kind, area, size, or priority as a label instead of as issue text
  or a project field.
- Rebasing a long-lived branch onto `main` instead of merging `main` into it.
- Resolving a conflicted file with `--ours` or `--theirs` when both sides
  carry changes worth keeping.
- Declaring a reconciliation finished because no conflict markers remain,
  without re-running the suite that covers the resolved files.
- A test suite that passes while the feature under test is missing.
- Adding a test without once watching it fail.

## Test A Feature By Running It, Not By Grepping For It

A test that asserts over a file's source text proves the text exists, not that
the feature works. `tests/dashboard-contract.sh` once consisted entirely of
`grep`s over `bluefin_review_tui.py` — it passed for as long as the dashboard
had no way to review a pull request at all, because no assertion ever started
the app. Prefer, in order:

1. **Drive the real thing.** Textual ships `App.run_test()`; the pilot in
   `tests/dashboard_pilot.py` presses keys, waits for a terminal state, and
   asserts what the maintainer is told. A binding pointing at a missing action
   fails there and nowhere else.
2. **Assert on observable outcomes**, not on the strings that produce them: the
   status text and its style class, the process argv, the exit status, the
   trace record.
3. **Keep source-text assertions only for absence.** A power a component must
   never have — `--admin`, `git push`, a direct merge — cannot be proven
   missing by exercising it, so grep is the right tool for exactly that.

Confirm a new test can fail: break the behaviour it covers, watch it go red,
then restore. A test never observed failing is an assumption.

## Verification

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/just-onboarding.sh
git diff --check
just --list
```

`pre-commit run --all-files` runs all socket-free contributor hygiene checks.
ShellCheck is a manual container-backed hook that the required `validate`
workflow invokes explicitly, so a missing local container socket does not
block the local gate. That also means it passes locally and then fails CI:
before pushing a shell change, run the manual stage too.

```bash
pre-commit run shellcheck --hook-stage manual --all-files
```

`gh label list -R projectbluefin/review` returns the seven canonical labels and
the three automation labels above, and nothing else.

A new script must carry the executable bit and match its directory's `shfmt`
style — two spaces under `scripts/` and `tests/`, tabs under `image/`. Set the
bit through git, or a locally-correct file still fails CI:

```bash
git update-index --chmod=+x <path>
```

After resolving a merge, confirm no marker survived anywhere. Anchor the
search, because shell here-strings legitimately contain `<<<`:

```bash
grep -rnE '^(<<<<<<< |=======$|>>>>>>> )' . && echo 'markers remain'
```

Before review, confirm the pull request title, branch, checks, and required
trailer with `gh pr view` and `gh pr checks`. A reconciliation is done when
`gh pr view --json mergeable,mergeStateStatus` reports `MERGEABLE` and
`CLEAN`.
