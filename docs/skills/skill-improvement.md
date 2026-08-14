---
name: skill-improvement
version: "1.1"
last_updated: 2026-08-14
id: skill-improvement
one_line_purpose: Keep review documentation source-backed, current, and compact.
entry_point: docs/skills/skill-improvement.md
category: meta
mcp_compliance_level: partial
optimization_status: active
status: active
dependencies: []
tags: [skills, documentation, maintenance, factory]
description: "Maintains review documentation and skill contracts from local source evidence. Use when auditing docs, correcting stale guidance, or recording durable agent learning."
metadata:
  type: reference
---

# Documentation and Skill Improvement

## When to Use

Load this before auditing or changing documentation, skill routing, the skill
catalog, or agent-facing repository contracts.

## When Not to Use

Do not use this as a backlog, session log, or replacement for the
task-specific launcher, image, Hive, MCP app, or pull-request workflow skill.
Use it alongside the matching skill when documentation maintenance is part of
that work.

## Core Process

1. Read `AGENTS.md`, `docs/factory/agentic-model.md`, `docs/SKILL.md`, and
   the matching local skill first. The agentic model is the canonical
   explanation of local roles, vocabulary, and authority boundaries.
2. Treat the documentation, launcher, image, MCP app, and tests as one model.
   Repair any contradiction at the nearest authoritative source; never leave
   a legacy document to describe a second workflow or authority path.
   This model-alignment judgment belongs in every normal implementation and
   review loop. Explicit or periodic harvesting and gardening are secondary
   maintenance work, not substitutes for checking the current change.
3. Use `projectbluefin/common` as the pinned shared factory sidecar. It
   supplements local guidance; it does not override local repository
   boundaries or assign work.
4. Correct stale, contradictory, or missing durable guidance in the nearest
   user document or `docs/skills/` file. Regenerate `docs/skills/index.json`
   with `bash scripts/check-skill-frontmatter.sh --write` when a skill
   frontmatter field changes.
5. Keep the repository compact. Do not commit changelogs, session notes,
   planning scratchpads, design records, or "append here" instructions.
   Remove obsolete records rather than preserving them as live guidance.
6. Record only reusable, source-backed learnings. A command, API behavior, or
   configuration fact belongs in a skill only when its source can be verified.

When a human must intervene to restart continuation or correct scheduling,
classify the control failure; record the durable transition in the relevant
issue or pull request; if reusable, add the smallest preventive rule to the
closest skill and verify it where practical; never create a session diary.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The implementation is the only source of truth." | The implementation proves behavior; the documented model makes roles and authority legible to agents and users. Keep both aligned. |
| "The plan is useful history." | Git history preserves completed work. A stale plan acts as a competing current contract. |
| "This detail is too small for a skill." | If it changes how a future agent should operate, encode the timeless rule in the nearest skill. |

## Red Flags

- Copying a factory policy that conflicts with this repository's launcher
  boundaries.
- Treating a source file, test, and user document as independent policies
  instead of one documented model.
- Treating an old plan or design record as current behavior.
- Updating a skill without its matching catalog entry.
- Adding a permanent session log instead of repairing the relevant skill.
- Changing another repository before reading its `AGENTS.md`, `CONTRIBUTING.md`
  and `docs/skills/`. Those files name the seam and forbid the shortcut, so
  the two minutes spent reading them is repaid immediately; skipping them is
  what produces a rejected approach and a wasted build.
- Diagnosing a service by guessing at endpoint names when its skill documents
  the supported read-only ones.
- Claiming project-internal facts without checking the launcher, image, tests,
  workflow, or pinned common source.

## Verification

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
git diff --check
```
