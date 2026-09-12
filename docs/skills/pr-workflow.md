---
name: pr-workflow
version: "1.11"
last_updated: 2026-09-09
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

See [`pr-labels.md`](pr-labels.md) for projectbluefin's factory lifecycle labels, `3-clanker-queue` admission, `3-human-queue` routing, and the `lgtm` automation label.

## Reconciling Long-Lived Branches

`main` squash-merges, rewriting commit SHAs. Always merge `main` into the feature
branch; do not rebase. Resolve conflicts hunk by hunk, avoiding `--ours`/`--theirs`
which overwrite entire files.

## Uncommitted Work Safety

Before committing, run `git status --short`. If unrelated changes exist, use a
throwaway worktree (`git worktree add /tmp/work -b branch origin/main`) to isolate
edits. Always stage files by explicit path, never `git add .` or `git add -A`.

## CI-Skip Directives

Never quote GitHub skip directives (`[skip ci]`, `[ci skip]`) in commit messages;
GitHub parses them anywhere in the message and aborts workflow publication.
Write `skip-ci` without brackets.

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

## Test By Running, Not Grepping

Source-text greps prove text exists, not that features work. Drive real execution
via Textual pilot tests (`App.run_test()`). Reserve grep assertions strictly
for proving absence of forbidden powers.

## Verification

CI enforces the complete verification suite in `.github/workflows/validate.yml` (see [`docs/image-and-development.md`](../image-and-development.md#validation) for the full local command list).

For pull request workflow, documentation, and skill changes, run this focused surface subset:

```bash
pre-commit run --all-files
git diff --check
just --list
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/just-onboarding.sh
```
`pre-commit run --all-files` runs all contributor hygiene checks, ShellCheck
included: the hook uses the shellcheck-py wheel, so it needs no container
socket and behaves identically locally and in the required `validate`
workflow.

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
