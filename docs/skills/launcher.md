---
name: launcher
version: "3.1"
last_updated: 2026-08-11
id: launcher
one_line_purpose: Change review just recipes without breaking the launch contract.
entry_point: docs/skills/launcher.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [just, launcher, podman, container]
description: "Maintains the three container-only review recipes and their credential boundaries. Use when editing justfile."
metadata:
  type: runbook
  context7-sources: [/websites/podman_io_en]
---

# Launcher

## When to Use

Load this before editing `justfile` or changing container launch,
lifecycle, or credential-passthrough behavior.

## When Not to Use

Do not use this for Hive task selection, contributor-session triage, Goose
configuration internals, or image-layer pinning. Those belong to the Hive,
Goose, or image build skill documents.

## Core Process

1. Keep exactly four public recipes:

   | Recipe | Purpose |
   |---|---|
   | `review-container` | Run the Hive queue worker: the contributor container that receives assigned tasks. `REVIEW_DETACH=1` runs it detached. |
   | `review-stop` | Stop a detached worker; refuses attended runs and unlabeled containers. |
   | `review-doctor` | Perform read-only preflight checks. |
   | `review-queue` | Walk the static PR queue in the container; no Hive registration. |

   `just` reads only the current directory's justfile, so these recipes fail
   with `justfile does not contain recipe` from any other checkout. That is
   `just`'s behavior, not a launcher bug: fix it outside the repository with a
   `~/.local/bin` shim that forwards these four names to this justfile, and
   do not add a wrapper recipe here to compensate.

2. Keep the interactive launch paths foreground, and the detached worker
   explicit. `REVIEW_DETACH=1` is the one sanctioned background launch: it
   stamps `review.owner=detached`, a later launch refuses to reclaim it, and
   `review-stop` is its only lifecycle verb — polite `podman stop`, never a
   force flag, and it refuses attended runs and containers it did not label.
   Nothing else may background a run, and no persistent launcher state is
   allowed beyond the pinned Hive checkout under `~/.local/state/review/`.
   Ctrl-C stops an interactive run; `--replace` only reclaims a container
   name when a new launch starts.
3. Keep the container path narrow. It mounts only the read-only Hive
   contributor configuration and runs the image entrypoint, which attaches to
   Hive's `contributor` session.
   `review-queue owner/repo` is the read-only live-repository form; it mounts no Hive or
   host configuration directory and starts the image with the `queue`
   argument (the launcher maps it to the dashboard's distinct `--live-repo`
   option; `--repo` remains a static snapshot filter), which the entrypoint dispatches to the maintainer dashboard
   before the Hive config gate. The
   dashboard needs a GitHub token from the first keystroke, so the recipe
   fails without one rather than warning. Leading non-flag arguments are the
   model profile and thinking effort — the same closed set `review-container`
   takes — and everything from the first `-` flag onward forwards verbatim to
   the dashboard. Its instance name is `review-queue`, overridable with
   `REVIEW_QUEUE_NAME` — the dashboard's analogue of `REVIEW_CONTAINER_NAME`,
   and likewise the only instance knob it gets.
   An explicitly set `BLUEFIN_REVIEW_BACKEND` is validated as `goose` or
   `codex` and forwarded only to this recipe. Unset preserves the dashboard's
   default; explicit Codex preselects the existing takeoff panel but never
   starts inference without Enter/click confirmation. Invalid values fail
   before a container starts, and this selector never reaches
   `review-container` or changes Hive's backend.
   Which hive a launch contributes to is launcher configuration, not task
   selection: `~/.config/hive/contributor.<name>.env` registrations sit
   beside the default `contributor.env`, and the launch picks `REVIEW_HIVE`
   first, then the current repository's directory name, then the default.
   An explicit `REVIEW_HIVE` with no file yet registers one by running
   upstream `contribute-setup` with an isolated `config_dir` so the default
   registration is never clobbered. Every launch prints the hub it will
   talk to; a silent default is how a contributor ends up watching one
   hub's dashboard while their agent asks another for work.
   Mount the selected registration and nothing else. Bind-mounting
   `~/.config/hive` and overlaying the selected file on top of it looks
   equivalent and is not: rootless Podman prepares the nested target through
   the already-mounted host directory, so a named registration made target
   creation escape to the host and leave a zero-byte `contributor.env` owned
   by a subordinate uid, which the container then failed on. Use the shared
   `:z` relabel, never `:Z` — concurrent named workers may share one
   registration, and a private MCS category revokes the running container's
   access when the next one starts.
4. Keep Goose as the default backend, with Codex and Pi as explicitly selected
   executable backends. `TOOL=goose` preserves the Copilot provider path;
   `TOOL=codex` requires a readable subscription `auth.json`, stages only a
   disposable copy for the contributor container, and never requires Goose or
   Copilot; `TOOL=pi` requires `PI_API_KEY`, passes it as the selected Pi
   process's `ANTHROPIC_API_KEY`, and lets the image entrypoint prove
   `pi --version` before Hive starts. Nothing is persisted: not a secret, not a
   provider, not a model. There is no last-selection file, and
   `tests/just-onboarding.sh` asserts one is never written. Hive remains the
   sole assignment authority.
   The configured-provider preflight reads Goose's own config and must
   accept both keys Goose has shipped: current releases record the
   selection as `active_provider:` beside a `providers:` map, older ones
   wrote a bare `provider:`. Goose migrates the host file on its own, so
   a preflight that knows only one key strands a configured host.
   `review-container` must set its own thinking-effort default before forming
   the Podman environment, while still honoring `GOOSE_THINKING_EFFORT` from
   the caller. Do not replace the
   image's direct-invocation fallback.
   That default comes from the model profile: `review-container [profile]
   [effort]` resolves `luna` to `gpt-5.6-luna` at `max` with the provider's
   own context window, `opus5` to `claude-opus-5` at `high` with
   `GOOSE_CONTEXT_LIMIT=264000`, and `kimi` to `kimi-k3` at `max` with the
   same clamp. An empty profile is `luna`; a short fixed profile list does
   not warrant a picker, so every launch is noninteractive whether or not a
   terminal is attached. Profiles are defaults, never overrides:
   `GOOSE_MODEL`, `GOOSE_THINKING_EFFORT`, and `GOOSE_CONTEXT_LIMIT` from the
   environment always win.
   The *profile name* is validated; the model ID it resolves to is not. The
   Copilot catalog is provider-side and changes without a release here, and a
   caller-supplied `GOOSE_MODEL` is passed through unvalidated by design. Form
   the environment and let Goose surface a model the provider will not serve.
   Do not add a catalog check to the launcher.
5. For container-only mode, pass Copilot and GitHub credentials by inherited
   environment (`--env NAME`), not command-line values or host configuration
   mounts. Resolve the GitHub token from `REVIEW_GH_TOKEN`, existing
   `GH_TOKEN`, then `gh auth token`.
   Codex subscription OAuth is the one file-shaped exception for
   `review-queue`: locate `${CODEX_HOME:-$HOME/.codex}/auth.json`, copy it with
   mode `0600` into a private runtime directory, mount only that disposable
   copy at `/home/dev/.codex/auth.json`, and remove it when the foreground run
   exits. The official CLI may refresh the staged copy; it must never receive
   the host Codex configuration directory or mutate the host login cache.
   An explicitly selected Codex review requires no host Goose installation,
   configuration, or Copilot credential. Missing auth remains a visible
   `NEEDS SIGN-IN` state and never selects a fallback harness.
6. When renaming launcher-facing product identifiers, do a tracked-file sweep
   for both active names and legacy spellings in code, comments, workflow
   assertions, fixture image names, and environment variables. Keep only the
   live `review` / `REVIEW_*` surface; do not leave compatibility aliases
   behind.

## Container Ownership

`podman run --rm -it` does **not** bind a container's lifetime to its client.
`conmon` supervises the container, survives the client, and reparents to
`systemd --user`, so a hard-killed terminal leaves a fully running, ownerless,
unreachable container — not merely an exited name. Inferring ownership from a
`pgrep` for the `podman run` command line cannot tell that apart from a live
session.

Ownership must therefore be proven, not guessed: stamp
`--label review.owner=<boot-id>:<client-pid>` at launch, and treat a container
as owned only when all three hold — the PID is alive, the boot id matches, and
that process still names the container in `/proc/<pid>/cmdline`. Anything else
— including an unlabelled container, which cannot have been started by this
launcher in this boot — is an orphan and is reclaimed silently at the next
launch. There is no `pgrep` fallback, and adding one back would reintroduce
exactly the guess the label exists to replace. Never answer an
ownerless container by telling a user to press Ctrl-C in a terminal that no
longer exists, and never reintroduce a user-facing stop or clean verb.

## Concurrent Instances

Every ownership check is keyed on the container name, so the name is what
scopes an instance. `REVIEW_CONTAINER_NAME` overrides the default
`review-container` and is the only supported way to run a second contributor
agent at the same time:

```bash
REVIEW_CONTAINER_NAME=review-container-2 just review-container opus5 high
```

Keep it to that one variable. Do not add a `--name` recipe parameter, instance
numbering, a multi-instance manager, or any registry of running instances;
that would be launcher state and task-selection surface this repository does
not have.

A name supplied by a user reaches `podman run --name` and the ownership
probe, so validate it against podman's own rule
(`[a-zA-Z0-9][a-zA-Z0-9_.-]*`) before launch rather than letting podman fail
late. Validate before the Hive setup so a typo costs nothing. Every
user-facing message — the refusal, the attach hint, the reclaim line — must
name the container that was actually requested, or a second agent is told to
attach to the first one's session.

Hive selects every task. The launcher must not filter, skip, rank, or decline
assignments by repository, label, title, author, or issue.

## Rootless Podman And Mounted Host Files

Rootless Podman maps the host user to container **root**, not to the container
user of the same uid. A mounted host file keeps its mode, so Hive's
`contributor.env` at `0600` arrives root-owned and the image's `dev` user
cannot read it — the agent dies at startup with `Permission denied` before any
work begins. Launch with `--userns keep-id:uid=1000,gid=1000` so the host user
maps onto `dev`. Never answer this by loosening the host file's mode; it holds
Hive credentials.

A locally built image has no registry behind it and is not a moving tag.
`podman build -t <name>:<tag>` stores the result under `localhost/`, and
refreshing that emits pull retries and an always-false "may be out of date"
warning. Detect the local build before deciding a ref is refreshable. Build
local images under the `sha-<commit>` tag CI mints for that commit: it names
exactly one build, so it is never re-pulled over, and it says which commit is
in the image.

The same reasoning governs the missing case. `localhost/` is podman's local
storage namespace, never a registry host, so pulling a `localhost/` ref that
is absent dials `https://localhost/v2/` and fails three times with a
connection-refused error that reads like a network fault instead of a missing
build. Absent from local storage is the final answer for a `localhost/` ref:
fail immediately and say it must be built, or that the override should be
dropped for the published default.

`just --list` in another repository shows only that repository's recipes, so
run `just review-container` from this checkout, or pass `--justfile`, when you
want to be certain which launcher you are invoking.

## Common Rationalizations

- "It's only a comment or test fixture." Workflow assertions, onboarding
  fixtures, and operator comments are part of the public launcher surface and
  must be rebranded with the code.
- "We can leave an alias for safety." This launcher's contract is a clean
  break; aliases preserve stale instructions and weaken test coverage.
- "Passing `--env NAME=value` is equivalent." For secrets it is not: inherited
  `--env NAME` avoids printing values into the Podman command line.
- "Mounting `~/.codex` is simpler." It also passes provider configuration and
  lets a container mutate the host login. Stage only `auth.json`; never mount
  the directory or the original file.

## Red Flags

- An undocumented public recipe, or an implicit background launch with no
  matching lifecycle verb.
- An interactive launch path whose final process is neither `exec`'d nor the
  last foreground command whose status propagates: `nohup`, `setsid`, a
  stray `podman run -d`, or a background job that silently outlives the run.
  A background job the shell `wait`s on and reaps by trap is not this, and
  removing one can break signal handling.
- A host directory mount beyond the read-only Hive configuration for the
  contributor container, or a host Codex config/login mount instead of the
  one-run staged auth file.
- A token in output, files, Podman arguments, or any persisted launcher file.
- Ownership inferred from `pgrep` rather than a label plus a live, same-boot,
  still-naming PID.
- A user-supplied container name reaching `podman run` or an ownership probe
  unvalidated, or a hint that names the default container instead of the one
  the caller asked for.
- A model-catalog or model-ID validity check in the launcher; only the profile
  name is a closed set.
- Contributor task-selection policy outside Hive (own-work exclusion on the
  maintainer queue view is the one sanctioned filter).

## Verification

```bash
just --list
just review-doctor
bash tests/just-onboarding.sh
git diff --check
```

The recipe list must contain only the four public commands. Doctor must not
start a container.

## Sources

- Podman environment inheritance: Context7 `/websites/podman_io_en`
