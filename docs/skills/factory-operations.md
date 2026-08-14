---
name: factory-operations
version: "1.0"
last_updated: 2026-08-14
id: factory-operations
one_line_purpose: Keep bounded factory work moving until it lands or is externally blocked.
entry_point: docs/skills/factory-operations.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: [contribution-culture, skill-improvement, pr-workflow]
tags: [factory, operations, continuation, scheduling, verification]
description: "Defines repository-neutral continuation, capacity, ownership, and evidence rules for bounded factory work."
metadata:
  type: policy
---

# Factory Operations

## When to Use

Use this when coordinating multiple bounded changes or deciding whether work
should continue after a worker, check, review, or remote operation changes
state.

## Operating Rules

- The default state is continue. Worker completion is an event, not supervisor
  completion.
- Assign one sole writer to each branch or worktree and state its explicit
  write set. Waiting, CI, review, and remote work do not consume writable
  capacity.
- An exact candidate SHA binds focused verification, hosted CI, and independent
  review. Evidence from a superseded SHA is stale.
- Blockers are lane-local: park only that lane and record discovered
  dependencies. Refill writable capacity immediately with the highest-priority
  ready work that does not conflict.
- Three writers is a ceiling, not a utilization target. Never invent filler
  work.
- Put important transition receipts in the repository's durable issue or pull
  request system.
- Repair concrete, validated failures without speculative re-architecture.
- Knowing the next steps is not a stop condition. Continue until the outcome
  is merged or concretely externally blocked.

## Human Continuation

When a human must intervene to restart continuation or correct scheduling,
classify the control failure. Record the durable transition in the relevant
issue or pull request. If the lesson is reusable, add the smallest preventive
rule to the closest skill and verify it where practical; never create a
session diary.
