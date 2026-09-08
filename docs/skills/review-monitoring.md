---
name: review-monitoring
version: "1.5"
last_updated: 2026-09-08
id: review-monitoring
one_line_purpose: Monitor running review containers, landing batch execution, and container health.
entry_point: docs/skills/review-monitoring.md
category: ci-ops
status: active
tags: [monitoring, review, landings, queue, kubernetes, countme]
description: "Use when observing, diagnosing, or monitoring running review-queue or review-container instances, landing batches, or agent process health."
metadata:
  type: procedure
  context7-sources: [/websites/podman_io_en]
---

# Review Container & Queue Monitoring

> Continuous, non-blocking observability into active review appliances,
> background landing batches, and agent health without static queue anti-patterns.

## When to Use

Use when:
- Inspecting active `review-queue` or `review-container` execution and process trees.
- Monitoring automated batch landing progress across `${XDG_STATE_HOME}/bluefin-review/landings/`.
- Diagnosing stuck landing agents, runaway CI watches, or subagent starvation.
- Auditing container state mounts, permissions, and log output.

## When Not to Use

Do not use for:
- Initial container launch or justfile recipe authoring (`launcher.md`).
- Primary TUI keybindings or review cockpit display (`review-dashboard.md`).
- Multi-worker cluster distribution (`cluster-workers.md`).

## Core Architecture & Observability

### 1. Active Container & Dashboard Discovery

Static queue snapshots are forbidden. Never create, query, or consume static
queue files or JSON dumps to monitor pull-request status, queues, or review
state. Probe live container state, GitHub, or Hive directly:

```bash
# Check running review container instances and owners
podman ps --filter "name=review-" --format "{{.ID}} {{.Names}} {{.Status}} {{.Image}}"

# Inspect runtime processes inside the appliance
podman top review-queue

# Read container mount bindings
podman inspect review-queue --format '{{json .Mounts}}' | jq .

# Follow live container log output for new errors
podman logs --tail 100 -f review-queue
```

Monitor an active maintainer dashboard (`review-queue`) via three live vectors:
1. **Live container state:** Verify the container is running and inspected.
2. **Process tree:** Inspect active processes inside the appliance
   (`podman top review-queue`) to verify the Textual TUI, runner, or subagent
   processes are running and unblocked.
3. **New log errors:** Stream live logs to catch fresh exceptions and
   tracebacks. Silence or idle periods reflect normal waits, not failures.

For `REVIEW_RUNTIME=k8s`, inspect the live Pod:

```bash
kubectl get pods -n bluefin-system -l app.kubernetes.io/name=review-queue
kubectl logs -n bluefin-system <review-queue-pod>
```

Its Pod and Secret disappear at terminal exit; `review-queue-state` persists
state. Never inspect a Secret for queue state or credentials.

### 2. Queue Reconciliation

The dashboard caches its last good live GitHub open-PR queue and read-only
Hive view. A receipt-verified clean review, successful mutation or batch queue,
or terminal landing completion schedules one asynchronous refresh; triggers
coalesce, with one bounded follow-up for a completion during refresh. It never
polls or changes Hive assignments or completion. Failure retains the aged
display; use `R` for an explicit read, never a static artifact. An explicit queue or Hive read that supersedes an exclusive reconciliation worker retains the active request identity, so its fresh replacement settles the snapshot rather than falsely leaving it unavailable.

### 3. Dashboard Activity

The normal dashboard always shows an `AGENT ACTIVITY` surface above the queue.
It separately names parent reviews (active review batches), check workers
(the review engine's active slots), landing agents, and queued landing work.
It lists a bounded number of active review, landing, and read-only Hive
contributor-assignment rows using only repository-qualified pull-request keys.
A missing or malformed Hive assignment reads as unavailable; it is never
inferred from an agent name, task identifier, prompt, or pull-request title.

`Snapshot:` makes the cached answer's state explicit:

- `current` includes the age of the oldest component of the cached answer;
- `refreshing` retains and ages the last good answer while the existing
  reconciliation is in flight;
- `retained/last good` marks cached assignment data after a failed read; and
- `unavailable` means no answer can be shown honestly.

Review and landing lifecycle completions repaint this surface through the same
operation-triggered reconciliation already used for the queue. It adds neither
a polling timer nor a Hive mutation.

### 4. Countme

Optional countme remains local to the session-secret handoff and is disabled
without that configuration. It records only bounded queue refresh duration/pages/items for both organization and single-repository queues, active counts, and review/landing outcomes: no secrets, prompts, or pull-request content. Failure affects countme only.

### 5. Remote Engine & Image Boundary

Podman remote setup is machine-local operator state (`podman system connection`,
`containers.conf`, or environment): never commit `CONTAINER_HOST`, endpoints,
SSH targets, sockets, or credentials. A default connection applies transparently.

An attended review container is user-owned; a pulled or rebuilt image affects
only future launches, never a reason to stop, restart, or kill the instance.

### 6. Batch Landing Stream

Landing state persists under `${XDG_STATE_HOME:-~/.local/state}/bluefin-review/landings/`:
- `<id>.prompt.md`: The brief dispatched to the landing agent.
- `<id>.jsonl`: Ordered append-only event stream updated by `landing.py report`.
- `<id>.log`: Stdout and stderr from the agent process.

Monitor batch state live without modifying files:

```bash
# Stream latest batch status events
tail -f "${XDG_STATE_HOME:-$HOME/.local/state}/bluefin-review/landings/"*.jsonl

# Monitor agent log output
tail -n 50 -f "${XDG_STATE_HOME:-$HOME/.local/state}/bluefin-review/landings/"*.log
```

Terminal failures, policy blocks, incomplete reports, and publication waits stay visibly marked but are not automatically reselected. Once every PR in a maintainer-confirmed batch has a terminal outcome, the same landing lane starts one consolidated recovery review with bounded terminal evidence; it neither retries nor merges. A maintainer explicitly selects and confirms any retry.

### 7. Agent Health & Diagnostics

Landing agents execute headless (`goose run --no-session -i <prompt>`).
Common health failure modes to detect:
1. **Synchronous CI Blockers:** Agent executing long-running `gh run watch` instead of polling `publish-verdict`.
2. **Permission Denied on Logs:** Missing user ownership on `/home/dev/.local/state` causing Goose logging failures.
3. **Partitioning Starvation:** Multiple repositories packed into a single task instead of concurrent per-repo lanes.

### 8. Long-Running Observation

For a long batch, sample live container or Pod state and landing events as
needed. Do not add a dashboard polling loop: event-triggered reconciliation
keeps the cached queue fresh while preserving the last good state during an
outage. Track active subagent commands for timeouts or hung external calls
(`gh run watch`) and file actionable findings with reproducible evidence.

## Showing Concurrency

Mass review runs unattended: say what is running, available capacity, and age.

Local review and landing lanes are the dashboard's own processes; Hive
contributors are other machines and must remain a separate display.

Show the **effective** capacity: the minimum of every applicable bound.

Every remote panel carries its age; unavailable data degrades visibly.

No configured Hive hub is a first-class path, not a degraded dashboard.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Read static queue.json to monitor state." | Static queue snapshots are forbidden. Inspect live container state, GitHub search, and landing JSONL. |
| "Restart or stop the container because a new image was pulled or rebuilt." | A pulled or rebuilt image cannot mutate an existing container. Attended instances are user-owned; new images apply only to future launches. |
| "Commit remote connection endpoints or CONTAINER_HOST to the repo." | Podman remote connection setup is machine-local operator state. Never put endpoints, SSH targets, sockets, or credentials in the repo. |
| "Kill the container when agent is quiet." | Agents may be waiting on legitimate image builds or CI checks; verify live processes and new log errors first. |
| "One concurrency number is simpler." | Local lanes and the Hive fleet are different machines under different authority. One number invites sizing a batch against capacity you do not own. |
| "Show the configured cap." | Capacity is the minimum of every bound. A silently throttled run and a broken scheduler look the same unless the effective cap is shown. |
| "SSE is connected, so the view is current." | A full subscriber channel drops events without disconnecting. Reconcile against `/fleet` and `/queue`. |
| "Countme failed, so stop the review." | Countme is optional. Its unavailable state affects telemetry only, not review or landing behavior. |

## Red Flags

- Creating, querying, or committing static queue snapshots.
- Stopping or restarting an active attended container because an image was pulled or rebuilt.
- Putting `CONTAINER_HOST`, remote endpoints, SSH targets, socket paths, or credentials in repository files.
- Reporting application health or diagnosing failure without verifying live container state, process trees, and new log errors.
- Scraping agent console output instead of reading structured JSONL status.
- Polling static files or mocking state when the live container is accessible.
- Ignoring permission errors in agent logs as harmless noise.
- Remote-sourced state rendered without its age.
- Local lanes and Hive fleet counts shown as one number.
- A dashboard polling loop, a queue refresh that mutates Hive assignments or
  completion, or a countme value in a log, command, durable state, or
  repository file.
