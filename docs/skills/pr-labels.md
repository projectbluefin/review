---
name: pr-labels
version: "1.0"
last_updated: 2026-09-06
id: pr-labels
one_line_purpose: Enforce the canonical factory lifecycle, admission, and automation label contract.
entry_point: docs/skills/pr-labels.md
category: meta
status: active
tags: [labels, factory, triage, automation, workflow]
description: "Defines projectbluefin's lifecycle labels, 3-clanker-queue admission, 3-human-queue routing, and the lgtm approval flag. Use when managing labels or triage."
metadata:
  type: policy
  context7-sources: [/pre-commit/pre-commit]
---

# Pull Request Labels

> Workflows own state; humans provide intent. Project Bluefin standardizes factory
> lifecycle labels, admission and routing labels, and the repository automation label.

## When to Use

Load this when triaging issues/PRs, assigning factory workflow labels, or
applying automation overrides.

## When Not to Use

Do not load this for git commit conventions or branch preparation (`pr-workflow.md`).

## Factory Lifecycle and Queue Labels

| Label | Meaning |
|---|---|
| `1-triage` | New work awaiting human triage. |
| `2-discussing` | Work requiring discussion or a clarified design. |
| `3-clanker-queue` | Explicit agent admission: reconciled OMP slice, clear dependencies, single writer. Sole positive marker for automated issue pickup; hosted runtime enforcement is owned by #169 and remains unverified until proven. |
| `3-human-queue` | Work admitted to the human-maintained queue; human routing only, never agent admission. |
| `4-review` | A pull request is awaiting review. |
| `blocked` | Progress halted on external dependency or missing infra. |
| `hold` | Work is intentionally paused. |

## Automation Labels

This repository carries one automation label:

| Label | Meaning |
|---|---|
| `lgtm` | Human approval flag permitting automated merge when CI passes. |

## Core Process

1. **Positive admission only**: `3-clanker-queue` is the sole positive marker
   for automated issue pickup. Absence of this label means ineligible for
   automated implementation. An open issue state, `3-human-queue`, `hive/*`,
   `agent/*`, priority signals, or appearance in search results never grant
   automated admission.
2. **Hosted enforcement is unverified**: Issue #169 defines and owns hosted
   runtime enforcement across participating factories. Documentation does not
   claim external enforcement is verified until #169 proves it.
3. **Human routing**: `3-human-queue` routes work to humans; it is not agent
   admission.
4. **Provenance labels**: Hive-added labels (`hive/*`, `agent/*`) record
   provenance and must remain intact on issues and PRs; they do not grant
   admission.
5. **No self-admission**: Agents and workers never apply `3-clanker-queue` to
   issues or manipulate labels to claim work.
6. **Verification before action**: Label changes reflect verified state
   transitions, not speculative intentions. Apply `lgtm` only when human
   review criteria are satisfied.

## Red Flags

- Inventing repository-local label variants that break org standardization.
- Treating open issue state or `3-human-queue` as automated admission.
- Removing Hive provenance labels (`hive/*`, `agent/*`) as an admission substitute.
- Relabelling issues to attract or shed Hive assignments.
- Removing `blocked` or `hold` before the blocking condition is genuinely resolved.

## Verification

```bash
gh label list --repo projectbluefin/review
```
