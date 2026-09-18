---
name: review-checks
version: "2.1"
last_updated: 2026-09-14
id: review-checks
one_line_purpose: Maintain the OMP workbench review agents and policy seam.
entry_point: docs/skills/review-checks.md
category: ci-ops
status: active
tags: [checks, review, subagents, doctrine, omp]
description: "Maintains the review agents shipped by the OMP workbench. Use when changing review doctrine, specialist responsibilities, or orchestration prompts."
metadata:
  type: reference
  context7-sources: []
---

# Review Agents

The appliance ships companion agent definitions under
`image/extension/bluefin-review/agents/`. OMP discovers and runs them directly;
there is no Python harness, consolidated review-scope overlay, or alternate
backend adapter.

## Agents

- `bluefin-doctrine`: repository policy and project-specific invariants.
- `bluefin-correctness`: observable correctness and failure behavior.
- `bluefin-security`: trust boundaries, credentials, and unsafe mutations.
- `bluefin-test-coverage`: valuable behavioral coverage and missing regressions.
- `bluefin-simplicity`: duplication, dead machinery, and avoidable complexity.
- `bluefin-ci-triage`: live CI failure analysis.
- `bluefin-queue-triage`: Hive-ordered queue analysis.
- `bluefin-reviewer`: coordinates the specialist findings into a human-facing
  review draft.

Keep generic Hive queue mechanics out of these files. Bluefin vocabulary and
policy belong here or in `policy.ts`; OMP owns agent execution and workflowz.

## Rules

1. Shipped definitions select semantic OMP roles such as `@fast` and `@review`.
   The active routing profile resolves those roles to concrete providers,
   models, and effort levels; no provider mapping is duplicated in agent prose.
2. Review agents read evidence and return findings. They never comment, submit,
   approve, enqueue, push, or merge. Enforce that boundary in their tool
   allowlist: a prompt prohibition alone is not a capability boundary.
3. Verdict labels such as `clean` are recommendations returned to the
   coordinator, never authorization for the reviewer to mutate GitHub.
4. The coordinator must preserve specialist evidence and surface uncertainty;
   it must not turn absence of evidence into approval.
5. Prompts reference live queue items and bounded diffs, never static queue
   snapshots.
6. Delete a specialist when its responsibility is fully duplicated by OMP or
   another agent; do not preserve wrappers for compatibility.

## Verification

```bash
bash tests/omp-review-mode.sh
bash tests/appliance-contract.sh
git diff --check
```
