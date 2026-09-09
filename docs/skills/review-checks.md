---
name: review-checks
version: "1.1"
last_updated: 2026-09-09
id: review-checks
one_line_purpose: Maintain the five review check subagents and Goose review scope.
entry_point: docs/skills/review-checks.md
category: ci-ops
status: active
tags: [checks, review, goose, subagents, doctrine]
description: "Maintains the five image-owned review check subagents (bluefin-doctrine, security, correctness, test-coverage, simplicity) and Goose review scope. Use when changing review checks or review thread concurrency."
metadata:
  type: reference
  context7-sources: [/aaif-goose/goose, /addyosmani/agent-skills]
---

# Review Checks

> The container ships five specialized review check subagents under
> `/opt/bluefin/review-scope/.agents/checks/`. Goose executes them concurrently.

## When to Use

Load this when editing, adding, or evaluating review check subagents in
`image/review-scope/checks/`, or when tuning review thread concurrency.

## When Not to Use

Do not load this for Goose runtime configuration (`goose-context.md`) or
maintainer TUI cockpit navigation (`review-dashboard.md`).

## The Five Specialized Checks

The review scope deploys five distinct check subagents:

| Check | Responsibility |
|---|---|
| `bluefin-doctrine` | Enforces claimed scope, repository conventions, reviewable sizing, and consistency across implementation, tests, and durable documentation. |
| `security` | Detects high-confidence exploitable vulnerabilities, unsafe operations, credential leaks, and privilege-boundary failures. |
| `correctness` | Finds functional defects, silent error paths, boundary mistakes, concurrency hazards, and resource leaks. |
| `test-coverage` | Flags changed behavior lacking deterministic regression, negative, boundary, fidelity, or isolation test coverage. |
| `simplicity` | Enforces the Ponytail / YAGNI doctrine: flags premature abstractions, dead code, hand-rolled standard tools, and diff bloat. |

## Concurrency and Orchestration

1. Goose's native orchestrator dispatches check definitions found in
   `.agents/checks/*.md` as concurrent `goose run` subprocesses (capped at 4).
2. Wall-clock review time is governed by the slowest individual check rather
   than the serial sum of all passes.
3. `bluefin-review` sets up a per-review scratch scope copying the static
   checks, plus per-stop `cluster-resolution` or `maintainer-steering` when
   present.
4. `--check-scope <DIR>` replaces repo-root discovery, ensuring Bluefin review
   doctrine applies cleanly without modifying the target repository checkout.
5. In the maintainer cockpit, evidenced findings are remediated through
   `[$]` (slay), which dispatches the fixer behind its typed gate and
   durable run record.

## Core Process

1. Author each check with clear, non-overlapping evaluation criteria.
2. Mandate concrete file and line citations for every reported finding.
3. Require evidence-first reporting: state what could not be verified.
4. Keep check prompts focused so subagent token windows remain lean.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "One large check is simpler." | Monolithic checks serialize evaluation and lose multi-threaded concurrency. |
| "Style issues belong in checks." | Linters handle style. Review checks focus on high-confidence correctness, security, and doctrine. |

## Red Flags

- Omitting check files from container builds, reducing reviews to single-threaded execution.
- Checks that produce unevidenced recommendations without line citations.
- Replacing `--check-scope` with repository-local file mutation.

## Verification

```bash
podman run --rm --entrypoint /bin/ls ghcr.io/projectbluefin/review:stable /opt/bluefin/review-scope/.agents/checks/
# Verifies all 5 check files are present
```
