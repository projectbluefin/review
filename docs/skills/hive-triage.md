---
name: hive-triage
version: "1.5"
last_updated: 2026-08-07
id: hive-triage
one_line_purpose: Diagnose why an attached contributor is never handed work.
entry_point: docs/skills/hive-triage.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [hive, triage, assignment, cooldown, websocket]
description: "Diagnoses a contributor that connects but receives no assignment without adding client-side selection or retry behavior. Use when a ready contributor remains idle."
metadata:
  type: runbook
---

# Hive Triage

## When to Use

Load this when a contributor has connected successfully but remains idle before
receiving an assignment. Use [`hive-runtime.md`](hive-runtime.md) when work
arrived and the session then failed.

## When Not to Use

Do not use this to change contributor runtime behaviour
([`hive-runtime.md`](hive-runtime.md)), and do not use it to file an upstream
issue — collect the evidence here, then follow
[`upstream-hive.md`](upstream-hive.md).

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "No task arrived, so Hive is down." | Far more often the contributor is registered against a different hub, or the trust tier admits no matching task. Check the hub the launch printed. |
| "I will skip this assignment and take the next." | Hive is the sole authority for task selection. Skipping mid-protocol is out of contract; report the problem instead. |
| "The agent looked idle, so nothing is running." | An idle pane is not an idle worker. Read the contributor state before concluding. |

## Core Process

1. Confirm the launcher reached a ready contributor session. Resolve local
   preflight, configuration, and connectivity failures before diagnosing the
   hub.
2. Read the relay log first. When the hub declines to assign work it sends a
   `task_unavailable` negative-ack and the relay prints the reason before
   re-asking 30 seconds later. The reason strings are the hub's, defined in
   `v2/pkg/dashboard/contribute_ws.go`; the relay only prints `msg.reason`, and
   its own comment naming `no_work` is stale — no such reason exists in the
   hub. Read the constants, and take the string the relay actually printed over
   any list, including this one:

   | Reason | Means |
   |---|---|
   | `no_matching_work` | Running and unsuspended, but every candidate was filtered out. The starved-queue case. |
   | `contribution_suspended` | The operator turned the whole contribute queue off. Nobody gets work. |
   | `hub_not_ready` | No status snapshot yet; transient at startup. |
   | `token_mint_failed` | A scoped token could not be minted for the tier — often a missing installation permission. |
   | `tier_disabled` | The contributor's trust tier is in `hub.disabled_tiers`. |
   | `concurrency_limit` | Assigning would exceed the tier's `max_concurrent` for this identity. |
   | `hourly_limit` / `daily_limit` | The tier's `max_per_hour` / `max_per_day` cap was hit. |

   The last four are enforced refusals aimed at this contributor; the first
   three are hub-wide conditions no local change affects. A relay predating
   these cases logs the message as an unknown type and then stops asking; that
   is a pin problem, not a hub problem.
3. Read the hub's own read-only endpoints rather than guessing at them. On a
   hosted hub these answer unauthenticated, while `/api/health` and
   `/api/status` redirect to OAuth and tell you nothing:

   | Endpoint | Answers |
   |---|---|
   | `/api/contribute/status` | `hub`, `actionable_items`, `active_contributors` |
   | `/api/contribute/triage` | count per stage; all zero means nothing is admitted |
   | `/api/contribute/metrics` | `queue_depth` history, so you can watch it drain |
   | `/api/contribute/fleet` | each connected clanker's `trust_tier` and `idle_reason` |

   `actionable_items` counts candidates, not assignable work, so a large value
   beside an empty triage total is the normal shape of a starved queue rather
   than a contradiction.
4. Classify the condition: no admissible work for any contributor, work held
   by another live contributor, repeated failures returning the same work to
   selection, or a contributor-specific connectivity/authorization problem.
   `/api/contribute/fleet` settles this in one read: when another contributor
   at a higher `trust_tier` reports the same `idle_reason`, the cause is not
   local setup, tier, or the image.
5. Reconnect only as the normal request retry after the relevant hub state
   changed. Do not add polling, selection logic, or an assignment retry loop
   to review.
6. Escalate the observed condition to the hub operator with the time window
   and classification. Hub configuration and selection behavior are fixed
   there, not in this launcher.

## Red Flags

- Treating a generic backlog count as proof that work is assignable.
- Diagnosing a permanently idle contributor as a hub fault before confirming
  the pinned relay handles `task_unavailable`.
- Treating an assigned-but-idle session with no checkout on disk as a triage
  case. That was an upstream workspace gap, fixed by `HIVE_WORKSPACE_DIR`;
  check the pin instead.
- Diagnosing an account-specific failure without comparing the same time
  window for other contributors.
- Repeatedly restarting a healthy contributor instead of checking hub state.
- Adding a local filter, poller, or retry mechanism to compensate for hub
  selection.
- Reporting completion merely to alter assignment availability.

## Verification

- [ ] The contributor reached a ready session.
- [ ] The relay log was read for a `task_unavailable` reason before any hub
      comparison.
- [ ] The comparison covers the current hub state, not an earlier window.
- [ ] The outcome is classified before any reconnect.
- [ ] No change was made to launcher or image task-selection behavior.

```bash
bash tests/just-onboarding.sh
```
