---
name: cluster-workers
version: "1.1"
last_updated: 2026-09-07
id: cluster-workers
one_line_purpose: Scale out review contributor workers across Kubernetes clusters.
entry_point: docs/skills/cluster-workers.md
category: ci-ops
status: active
tags: [kubernetes, cluster, scale, workers, turbo]
description: "Manages cluster contributor workers in bluefin-system, secret synchronization, and turbo-review orchestration. Use when scaling workers or operating review-contributor on Kubernetes."
metadata:
  type: procedure
  context7-sources: [/websites/kubernetes_io, /websites/podman_io_en]
---

# Cluster Workers

> Review scales unattended contributor workers across an active Kubernetes
> cluster, donating multi-node inference without draining local resources.

## When to Use

Load this when scaling cluster contributors, modifying `deploy/review-contributor.yaml`,
configuring `review-contributor-secret`, or running `just turbo-review`.

## When Not to Use

Do not load this for local single-container runs (`launcher.md`) or review
dashboard navigation (`review-dashboard.md`).

## Core Commands

```bash
just review-container cluster 3     # scale 3 cluster workers
just turbo-review                   # scale 3 workers and open dashboard
just turbo-review sol               # scale with Sol profile + dashboard
just review-stop cluster            # scale workers to 0
just review-doctor                  # check cluster deployment health
```

## Architecture & Lifecycle

1. **Namespace & Secret Sync:** `scale_cluster_contributors` ensures
   `bluefin-system` exists and synchronizes `review-contributor-secret`.
2. **Plaintext Protection:** Token values enter `kubectl create secret`
   via process substitution file descriptors (`--from-file=KEY=<(...)`), preventing exposure in `ps` argv.
   Server-side apply is used and legacy annotations are stripped.
3. **Hive Hub Consistency:** The launcher validates `HIVE_HUB` from
   `contributor.env` and sets it on the deployment via `kubectl set env`,
   preventing split-brain hub connections between cluster and dashboard.
4. **Independent Task Streams:** Each pod establishes its own WebSocket
   to Hive and processes assignments independently.
5. **Non-Blocking Rollout:** Rollout observation waits 15 seconds. If
   image pulls take longer, a warning is printed and the launcher continues.
6. **Teardown:** Cluster workers continue running after the interactive
   dashboard exits. Stop them explicitly with `just review-stop cluster`.

## Dashboard Session Boundary

Cluster contributor scale-out and a Kubernetes dashboard session are separate
operations. `REVIEW_RUNTIME=k8s just review-queue` starts one restricted,
foreground dashboard Pod using the dedicated `review-queue-state` claim; it
does not create, scale, or stop `review-contributor`. Apply
`deploy/review-queue-state.yaml` before selecting that dashboard runtime.
The session Pod and its credential Secret are removed when the terminal exits.
See [`launcher.md`](launcher.md) for the runtime selection and credential
boundary.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Pass tokens via --from-literal." | Command-line arguments are visible in `/proc` and `ps`. Feed tokens via process substitution file descriptors. |
| "Abort if cluster is offline." | The appliance owns no lab and depends on none. Cluster failures fall back to local mode. |

## Red Flags

- Hardcoding `HIVE_HUB` in deployment manifests.
- Leaking credentials in `kubectl.kubernetes.io/last-applied-configuration`.
- Halting maintainer review triage because of cluster connection timeouts.

## Verification

```bash
kubectl get deployment review-contributor -n bluefin-system
kubectl get pods -n bluefin-system -l app.kubernetes.io/name=review-contributor
just review-doctor
```
