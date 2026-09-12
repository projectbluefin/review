---
name: local-engine
version: "1.0"
last_updated: 2026-09-12
id: local-engine
one_line_purpose: Maintain the bounded local Review workload engine contract.
entry_point: docs/skills/local-engine.md
category: ci-ops
status: active
tags: [review, engine, grpc, lifecycle, sandbox]
description: "Maintains the versioned engine protocol, physical job lifecycle, fixture executors, and terminal receipts without duplicating ReviewRun or harness authority."
metadata:
  type: procedure
---

# Local Review Engine

## Boundary

The local engine owns physical workload allocation, ordered events,
cancellation, artifacts, terminal receipts, and cleanup. The dashboard's
`ReviewRun` remains the logical review lifecycle, and the harness registry
continues to select providers and normalize results. The engine never selects
a harness, mutates GitHub, schedules Hive work, or accepts an arbitrary command.

The versioned contract is `engine/proto/review/engine/v1/engine.proto`. Keep
client and engine protocol majors equal before allocating a job. Additive minor
capabilities must be advertised; never silently substitute an executor or
runtime.

## Deterministic executors

`fixture/noop` and `fixture/blocking` are the only first-slice executors. They
exercise lifecycle, reconnect, cancellation, deadlines, output bounds, and
cleanup without Podman, provider credentials, GitHub, or network access.
Every request binds an exact 40-character source commit and digest-qualified
image. Reusing a request ID with the same canonical spec returns the existing
job; a different spec is rejected.

## Verification

Run the no-network contract:

```bash
cd engine
go test ./...
```
