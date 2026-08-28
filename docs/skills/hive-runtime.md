---
name: hive-runtime
version: "2.3"
last_updated: 2026-08-25
id: hive-runtime
one_line_purpose: Operate inside Hive's tmux, token, and cooldown constraints.
entry_point: docs/skills/hive-runtime.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [hive, tmux, runtime, tokens, cooldown, workspace]
description: "Explains Hive's tmux session, workspace directory, prompt and output contract, contributor credentials, token lifetime, cooldown, and exclusive task selection. Use when operating or debugging a session."
metadata:
  type: reference
---

# Hive Runtime

> The published image contains the pinned Hive contributor runtime. These
> procedures describe its handoff from the review launcher.

## When to Use

Load this before changing code near a Hive session boundary or when an
assigned contributor session behaves unexpectedly.

## When Not to Use

Do not use this to diagnose a specific stuck or missing assignment — that is
[`hive-triage.md`](hive-triage.md) — or to report a finding upstream, which is
[`upstream-hive.md`](upstream-hive.md). Do not use it for the launcher's own
credential handling ([`launcher.md`](launcher.md)).

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "A local shim will unblock this now." | It outlives the gap it was written for and shadows the real tool once upstream lands the fix. Report it and wait. |
| "Upstream is slow; we can patch our copy." | A patched copy of a pinned upstream file silently diverges at the next bump, and nothing fails to say so. |
| "The pin is close enough to upstream." | The image consumes three pinned Hive runtime files; verify their compatibility together and do not add a downstream protocol implementation. |

## Core Process

1. Let Hive own the WebSocket protocol, assignment selection, `contributor`
   tmux session, prompt injection, and result capture. Context7 reaches the
   agent twice: the hub queries it server-side
   (`v2/pkg/knowledge/context7.go`) and delivers assigned-task context through
   its knowledge export, and the image's controlled Goose config enables the
   `context7` extension for on-demand lookups (see `goose-context.md`).
   review starts the runtime and attaches to it; it does not reproduce Hive's
   jobs.
2. Attach only to inspect or deliberately steer a live session:

   ```bash
   podman exec -it <container> tmux attach -t contributor
   ```

   Detaching tmux changes the display, not the run. Ctrl-C or
   closing the original terminal ends the contributor.
3. Expect the agent to start in Hive's prepared workspace. `contributor-agent.sh`
   exports `HIVE_WORKSPACE_DIR` (default `$HOME/workspace`), creates it, and
   starts the tmux session rooted there with `tmux new-session -c`. Clone
   assigned work into that directory; no `cd` step is required, and the
   launcher must not create or mount a workspace of its own.
4. Put the final result in the final 15 pane lines. Hive captures only those
   lines for its report.
5. Plan around the scoped assignment token's 55-minute lifetime. The hub
   proactively re-mints and pushes a fresh token to an active task after 50
   minutes, so a long task survives expiry only while its socket stays up.
   Report completion only after its verifiable artifact exists: a completion
   carrying a PR link applies the 168-hour issue cooldown, a completion with
   no PR link only 4 hours, and a failure or disconnect books the short
   10-minute failure cooldown — 6 hours once an issue is quarantined.
6. Do not filter, decline, rank, or retry assignments in this repository.
   Hive selection is the sole authority. The relay's own negative-ack handling
   is Hive's, not ours: when the hub declines to assign work it sends
   `task_unavailable` with a reason, which the relay logs before re-asking
   30 seconds later. The reasons are defined by the *hub*, in
   `v2/pkg/dashboard/contribute_ws.go`, not by the relay — read them there.
   Three are enforced refusals (`token_mint_failed`, `tier_disabled`,
   `concurrency_limit`), two are rate caps (`hourly_limit`, `daily_limit`),
   and three mean the hub simply has nothing to hand over right now
   (`contribution_suspended`, `hub_not_ready`, `no_matching_work`). A relay
   revision predating that case logs the message
   as an unknown type and then has no path back to asking, because every
   `ready` it sends is event-driven and none is timed; it wedges idle. No task
   was assigned in that state, so nothing is held. Move the pin rather than
   adding a downstream retry.
7. Treat the relay's protocol version and capability declaration as
   informational. The relay reports its runtime posture during authentication,
   and Hive stores and surfaces it without routing or gating assignments on it.
   Do not add downstream capability-based task selection.
8. Expect the interactive delivery mode. The pinned runtime reads
   `CONTRIBUTOR_MODE`, which selects between `interactive` (the default: a
   live tmux pane the relay types the prompt into) and `headless` (no tmux
   session at all — the relay drives a one-shot CLI per task and writes
   lifecycle state to `HIVE_HEADLESS_STATUS_FILE`). review sets neither and
   runs interactive, which is what `just review-container` attaches to and
   what the entrypoint's `tmux has-session -t contributor` readiness check
   requires. Headless exists for an unattended Kubernetes contributor; do not
   set it here expecting the same attachable session.

### Hive runtime contract

At Hive `0b78dc0`, the public `/api/contribute` prefix exposes read-only
status, queue, events, activity, fleet, limits, and triage projections. Prefix
publicity does not make mutation handlers unauthenticated; those handlers
still enforce their own write requirements. Review may display these
authoritative projections, but Hive owns contributor admission and ordered
individual assignment. Review must not reorder, retry, assign, or become a
second scheduler.

`ReadyQueue` is a display projection; assignment eligibility remains
`selectTask` policy. This pin exposes no assignment grouping, batching,
dependency, or relatedness signal. Do not infer one from triage or display
metadata. `max_concurrent` counts tasks held by contributor identities, not
maintainer review analyses or factory writers. Issue-to-PR linkage is a
best-effort GitHub search projection cached for about 90 seconds, not durable
truth.

### GitHub identity

The contributor container passes one contributor GitHub token as inherited
`GH_TOKEN`; it does not mount the host GitHub configuration.
Never log or persist either credential.

To inspect earlier review output, enter tmux copy-mode with `Ctrl-b [`.
PageUp or the mouse wheel scrolls, tmux search finds text, and `q` returns to
the live pane. Copy-mode changes only your view; Hive still owns output
capture.

When configuring a derived contributor image, preserve the attach client's
recognized `TERM`; tmux's pane terminal is configured separately. Enable tmux
mouse support so the wheel enters copy-mode for long output. Do not alter
Hive's session creation to accomplish either behavior.

## Red Flags

- Creating or naming tmux sessions, injecting prompts, or scraping pane output
  in the launcher or image.
- Adding assignment selectors or client-side retry loops.
- Treating a tmux detach as background execution.
- Preparing, mounting, or renaming a contributor workspace in the launcher or
  image instead of using Hive's `HIVE_WORKSPACE_DIR`.
- Adding a local retry, poll, or timeout to compensate for a relay revision
  that ignores `task_unavailable`.
- Reporting completion before the required artifact is independently visible.
- Assuming an abruptly killed contributor strands its assigned task. The hub
  releases `currentTask` in its disconnect handler and books a cooldown, and
  its heartbeat loop closes a half-open socket, so no downstream release,
  timeout, or slot-reclaim step belongs here.
- Mounting `~/.config/gh` or printing a token to provide agent identity.

## Verification

```bash
podman exec -it <container> tmux ls
podman exec -it <container> tmux attach -t contributor
bash tests/just-onboarding.sh
```

Confirm that `contributor` exists, the final pane lines contain the result,
and no launcher change duplicates Hive lifecycle behavior.

## Sources

Cite upstream by pinned permalink, never a branch path.

- Relay message cases, including `task_unavailable`:
  [`bin/contributor-relay.sh` @ fc3d717](https://github.com/kubestellar/hive/blob/fc3d7179255d13a613632fd1e982691d2d8bc0ae/bin/contributor-relay.sh)
- Workspace preparation and tmux rooting:
  [`bin/contributor-agent.sh` @ fc3d717](https://github.com/kubestellar/hive/blob/fc3d7179255d13a613632fd1e982691d2d8bc0ae/bin/contributor-agent.sh)
- Task release on disconnect:
  [`v2/pkg/dashboard/contribute_ws.go#L1057-L1090` @ fc3d717](https://github.com/kubestellar/hive/blob/fc3d7179255d13a613632fd1e982691d2d8bc0ae/v2/pkg/dashboard/contribute_ws.go#L1057-L1090)
- tmux terminal and mouse configuration: Context7 `/tmux/tmux`
- Public contribute projections and assignment policy @ `0b78dc0`:
  [`server.go`, `api_contribute.go`, `contribute_sse.go`, and
  `contribute_ws.go`](https://github.com/kubestellar/hive/tree/0b78dc096d51ad7af7408fb644f40d269a7e4fc5/v2/pkg/dashboard)
- PR-link projection @ `0b78dc0`:
  [`contribute_prlink.go`](https://github.com/kubestellar/hive/blob/0b78dc096d51ad7af7408fb644f40d269a7e4fc5/v2/pkg/dashboard/contribute_prlink.go)
