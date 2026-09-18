---
name: launcher
version: "5.3"
last_updated: 2026-09-15
id: launcher
one_line_purpose: Change the Bluefin application launcher without breaking its runtime contracts.
entry_point: docs/skills/launcher.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [just, launcher, podman, kubernetes, omp, hive]
description: "Maintains the OMP appliance and Hive contributor launcher recipes without crossing their credential or authority boundaries."
metadata:
  type: runbook
  context7-sources: [/websites/podman_io_en, /websites/kubernetes_io]
---

# Launcher

## Public commands

| Command | Purpose |
| --- | --- |
| `bluefin review [org/repo|flags...]` | Runs `ghcr.io/projectbluefin/review`, preferring Podman `krun` and falling back to Apptainer. |
| `bluefin contribute [instance]` | Runs the Hive-authorized OMP worker; an optional instance selects its named Hive registration. |
| `bluefin setup [instance]` | Performs attended Hive registration through Hive's pinned upstream setup recipe. |
| `bluefin doctor` | Read-only preflight; starts no agent and exports no credential. |
| `bluefin cluster scale [N]` / `bluefin cluster stop` | Scales or stops independent Kubernetes Hive + OMP workers. |

The `just review-queue`, `just review-appliance`, `just review-container`,
`just contribute`, `just review-doctor`, and `just review-stop` recipes remain
developer-compatible entry points. They carry the launcher's shared shell
functions inline and retain their established state-volume and remote-Podman
recipe bodies. Users
should not need to type `just`; internally, attended Hive registration still
executes Hive's pinned `contribute-setup` Just recipe because Hive owns the
registration format and protocol.

## Authority boundary

The maintainer workbench reads live GitHub state and optional Hive ordering. It
does not register as a Hive contributor and does not select or complete Hive
assignments.

The contributor image contains Hive's worker runtime only. Hive chooses the
task, injects the prompt, owns the `contributor` tmux session, and captures the
result. The entrypoint may validate credentials and attach the terminal; it
must not filter, reorder, retry, or interpret assignments.

## Isolation and lifecycle

Every packaged appliance command prefers `podman run --runtime=krun` when
Podman, `krun`, and `/dev/kvm` are available. Otherwise it reports the missing
prerequisite and falls back to isolated Apptainer execution. Container names
include the target and a per-process suffix, so simultaneous KVM invocations
cannot replace one another. Persistent OMP homes are target-specific;
`BLUEFIN_INSTANCE` explicitly separates two sessions for the same target.

Every interactive microVM stays attached to its launching terminal. Do not add
`--detach`, `-d`, `nohup`, `setsid`, systemd units, or resurrection commands.
Ctrl-C stops only that invocation. `bluefin cluster stop` (or the compatible
`just review-stop cluster`) is reserved for the Kubernetes worker deployment.
Apptainer omits its default `/etc/localtime` or `/etc/hosts` mount only when
that host source is absent or a dangling symlink; present sources retain the
runtime default.
Fallback also requires `squashfuse_ll` or `squashfuse` and a readable,
writable character device at `/dev/fuse`; `bluefin doctor` reports each missing
prerequisite separately before launch.
The doctor checks both published images through reachable Podman or `skopeo`.
If Apptainer is the only runtime and no read-only registry probe exists, it
reports image resolution as deferred to launch instead of misclassifying the
remote reference as a missing local SIF.
On the Podman path, every mutable image tag is refreshed before launch. A
registry outage may use an existing local copy only with an explicit stale-image
warning; a missing local copy fails before `podman run`. Digest and `sha-*`
references remain immutable and are not refreshed.
Before container execution, the launcher reports its own revision. After resolving
an image, it reports the image OCI version, source revision, and digest; missing
labels are shown as `unknown` rather than inferred. On the Apptainer fallback
path where no read-only registry probe exists, image identity is reported as
unavailable without blocking launch.
The launcher enforces appliance compatibility: the review appliance requires
series `26.08` with version >= `26.08.06`, and the contributor worker requires
version >= `26.08.02`. Incompatible images fail before execution. Explicit image
overrides (`BLUEFIN_REVIEW_IMAGE`, `BLUEFIN_REVIEW_SIF`, `REVIEW_APPLIANCE_IMAGE`,
`BLUEFIN_CONTRIBUTE_IMAGE`, `BLUEFIN_CONTRIBUTE_SIF`, `CONTRIBUTE_IMAGE`) are
honored with actionable compatibility warnings if versions differ or cannot be
verified.
Upgrades from `v26.08.05` migrate existing user sessions and configuration
from legacy state directories (`~/.local/state/bluefin-review` and
`~/.local/state/bluefin-contribute`) into instance homes without broad state
deletion. Fixed-name legacy SIF artifacts (`bluefin-review.sif`,
`bluefin-contribute.sif`) are superseded by the versioned OCI contract and
cannot silently bypass validation.

## Credentials

- Pass secrets only through inherited environment names or documented private
  mounts. Never put values in arguments, logs, image layers, socket paths, SSH
  targets, or committed files.
- Preserve `--userns keep-id` for the `0600` contributor registration.
- The OMP appliance receives GitHub/provider credentials by inherited name and,
  when `HIVE_HUB` is unset, resolves the hub from the host's default
  `~/.config/hive/contributor.env` without mounting its registration token.
- Apptainer's contained environment receives only the explicit credential and
  runtime allowlist through `APPTAINERENV_` variables. Keep `--no-eval` so
  credential and argument values remain literal inside the container.
- The forwarded provider-credential allowlist names GitHub, Copilot, Anthropic,
  OpenAI, Gemini, Hive, and terminal variables, plus the Amazon Bedrock
  credentials `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, and `AWS_DEFAULT_REGION`.
  Only those reach the contained process; the rest of the AWS environment stays
  on the host. The value travels through the environment only, never in argv,
  launcher output, test logs, image layers, or committed files.
- The contributor worker receives exactly one selected Hive registration. The
  registration's `HIVE_HUB` decides which hive's work the session does, so the
  launcher prints the resolved hub and registration filename before starting
  the container and refuses a registration whose `HIVE_HUB` is unusable. A
  default `~/.config/hive/contributor.env` written by an unrelated
  `contribute-setup` run otherwise routes every bare `bluefin contribute` to
  that other project's queue; `bluefin contribute <instance>` selects
  `contributor.<instance>.env` instead.
- The checkout contributor recipe stages remote Podman registrations privately
  and deletes only its validated staging directory. The packaged `bluefin`
  launcher uses local Apptainer when Podman selects a remote engine; it never
  sends client-side credential bind paths to that engine.

## Arguments

`scripts/parse-review-args.sh` is the single parser for OMP review scope.
Repository, `--pr`, and `--issues` arguments must reach the appliance unchanged.
Every review launch passes OMP's built-in `--advisor` flag exactly once. The
source launcher also normalizes its parser-fallback path, and the packaged
entrypoint repeats the normalization for direct image launches.
The appliance configuration maps `modelRoles.advisor` to `@default` and uses
`BLUEFIN_REVIEW_ROUTING_PROFILE` for native role routing:
`copilot-mixed` is the default and `codex-subscription` is opt-in. The
selector appends an OMP config overlay; it does not implement provider
selection. `/model` remains the root-session choice and does not rewrite named
roles. If OMP cannot authenticate a child role, its documented parent-model
fallback may cross providers.
The optional contributor argument names an isolated instance and its
`contributor.<org-repo>.env`; Hive still selects work. OMP owns model choice
and dispatch.

## Verification

```bash
bluefin doctor
just --list
bash tests/launcher-contract.sh
bash tests/just-onboarding.sh
bash tests/appliance-contract.sh
bash tests/contribute-contract.sh
git diff --check
```
