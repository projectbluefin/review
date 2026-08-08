# review — Agent Operating Contract

`review` is a thin, foreground launcher for a QEMU VM or contributor
container running Goose. It owns VM boot, credential handoff, and review
context. Hive owns the WebSocket contributor protocol, task selection, the
`contributor` tmux session, prompt injection, and output capture.

## Read order

1. This file.
2. [`docs/factory/agentic-model.md`](docs/factory/agentic-model.md).
3. [`docs/SKILL.md`](docs/SKILL.md).
4. The one matching file in `docs/skills/`.

## Boundaries

Keep this repository small. Do not add a daemon, service, background
lifecycle, persistent state beyond launcher configuration, task-selection
logic, or a second implementation of the launcher.

The public launcher is foreground-only. Every launch path must end in a
process that is `exec`'d, or is the last foreground command whose exit status
propagates verbatim: no `nohup`, `--detach`, systemd unit, or `podman run -d`,
and no background job that outlives the run. Ctrl-C is the stop mechanism.

That rule scopes how the launcher starts the VM or container; it is not a ban
on `&` anywhere in the repository. Backgrounding is required where it is what
preserves the guarantee. Bash defers a trap handler while it waits on a
foreground child, so `image/entrypoint.sh` runs the contributor agent and
`tmux attach-session` as background jobs it `wait`s on, keeping PID 1
signal-responsive; a foreground attach swallowed SIGTERM for the whole session
and forced podman's ten-second SIGKILL. The `justfile`'s one-shot bootstrap
socket server is likewise a background job, reaped by an `EXIT/INT/TERM` trap.
Do not "fix" either one.

`podman run --rm -it` does not bind a container's lifetime to its client:
`conmon` supervises the container, survives the client, and reparents to the
user manager, so a hard-killed run can leave a fully running, ownerless,
unreachable container rather than merely a name. Each launch therefore stamps
`--label review.owner=<boot-id>:<client-pid>` and treats a container as owned
only when that PID is alive, in the same boot, and still names the container in
`/proc`. Anything else is an orphan and is reclaimed silently with `--replace`.

Do not filter, skip, reorder, prioritize, or decline Hive assignments. Hive is
the sole authority for task selection.

This is a toil-reduction factory for under-maintained projects, not a feature
factory. Repair what is broken and finish what a project already decided to
do; do not add features, dependencies, configuration surfaces, or
architecture, and size every change to be reviewable by a tired maintainer.
When a task can only be completed by out-of-scope work, an evidenced written
finding is the deliverable. See
[`docs/skills/contribution-culture.md`](docs/skills/contribution-culture.md).

Grandfathering is an antipattern here. Do not record a known-wrong thing as an
accepted exception and move on: fix it now, or delete it. An exception clause
outlives the condition that created it and converts "this is wrong" into "this
is allowed" — and a test that pins the exception makes correcting the defect
fail CI. Reject the words *grandfathered*, *sanctioned*, *legacy exception*,
*pre-existing*, *for now*, and *temporarily* in this repository's documents.

A gap is filed, not documented. When something is broken or missing — here, in
the pinned base, or upstream — open an issue and reference it by number. Do not
write a section explaining it. An issue has a state and a close event, so it
disappears when the defect does; a paragraph outlives the fix and reads as
justification. A code comment gets one line naming the issue that deletes it,
and a user-visible limitation gets one sentence naming the issue. Nothing else.

Use the tools the image already ships. If a common utility is missing, add it
to the base at the FSDK seam; never hand-roll a local reimplementation, and
never leave a shim standing once the seam fix lands. A shim is not inert
because it looks unused: `image/bin/find` and `image/bin/cmp` installed into
`/usr/local/bin`, which precedes `/usr/sbin` on `PATH`, so they shadowed the
GNU findutils and diffutils the pinned base had since gained. The `find` shim
also got `-o` precedence wrong and deleted `*.out` of any age where GNU `find`
deleted only old `*.html`, destroying fresh agent output every task cycle.
Verify a utility's absence by executing it at the pinned digest before
concluding the base lacks it. See
[`docs/skills/image-build.md`](docs/skills/image-build.md).

Reporting downstream evidence upstream to `kubestellar/hive` is expected work,
and filed issues are followed up rather than abandoned. Report observations,
reproductions, and options; upstream owns the design decision and the triage
labels. Never add a local workaround for an accepted upstream gap. See
[`docs/skills/upstream-hive.md`](docs/skills/upstream-hive.md).

## Repository layout

- `justfile` is the only shipped launcher artifact. Its
  four public recipes and private helpers intentionally live together.
- `image/` builds the FSDK-derived contributor image and its layered runtime
  configuration.
- `package.json` and `package-lock.json` at the root pin only the contributor
  relay's `ws` dependency for the image build. This repository is not a Node
  project.
- `queue/` generates the static PR queue published from `public/`.
- `scripts/` contains build-time skill generation and documentation checks.
- `tests/` contains launcher and image contracts.
- `docs/` contains the skill router and catalog.

Hive rewrites `~/.config/goose/config.yaml`. Keep the controlled Goose
configuration under `GOOSE_PATH_ROOT=/opt/bluefin/goose`; do not write it to
the Hive-managed path.

## Permitted changes

Agents may change `justfile`, `image/`, `queue/`, `scripts/`,
`tests/`, `docs/`, `README.md`, `AGENTS.md`, and `.github/workflows/`.

Do not modify `ublue-os/*`, or commit generated `.agents/skills/` content.
The generator is the artifact; `projectbluefin/common`'s
`docs/skills/index.json` is the organization-skill source. `public/` is
likewise generated: `update-pr-queue.yml` runs `queue/generate.mjs` and
deploys the result, so change the generator, not its output.

When behavior changes, update the matching user documentation. Treat the
launcher, image, and tests as the sources of truth for this repository's
behavior.

## Documentation Is the Model

[`docs/factory/agentic-model.md`](docs/factory/agentic-model.md) is the
canonical local model for the Bluefin Agentic Factory Feedback Loop. It defines
the roles, authority boundaries, and vocabulary that explain this repository.
Code, tests, user documentation, and skills must agree with it.

Every session ships the requested work and records any durable, source-backed
learning in the closest matching document under `docs/skills/`. Update
`docs/skills/index.json` in the same change with
`bash scripts/check-skill-frontmatter.sh --write`; the manifest is generated
from frontmatter, never edited by hand. Do not commit changelogs, session
notes, implementation plans, design scratchpads, or "append here" documents.
Remove stale records of that kind and route durable guidance to the matching
skill instead.

Local repository contracts take precedence; use `projectbluefin/common` as the
pinned shared factory sidecar, never as a reason to override this repository's
boundaries.

## Validation

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/hive-compatibility.sh
bash tests/bluefin-review.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
```

`tests/image-audit.sh` needs a container engine and network. It defaults to
`docker`; on a podman host pass `CONTAINER_ENGINE=podman`. Check the pinned
FSDK input alone with `--verify-base-evidence`; audit a built or published
image with `--derived <image>`. The report always records both platform slots
as native or unavailable, and `--report image-audit-report.md` writes the
Markdown report to a git-ignored file.

`pre-commit run --all-files` runs the socket-free hygiene checks locally.
ShellCheck is a CI-only manual hook because its upstream hook runs in a
container; CI invokes it explicitly.

## References

- Hive protocol, contributor runtime, and upstream issue reporting:
  `kubestellar/hive` (default branch `v2`; no contributing guide or issue
  templates, DCO sign-off required on pull requests).
- Organization skills and factory rules: `projectbluefin/common`.
- External API details: Context7 documentation. Context7 is a Hive hub
  capability delivered through Hive's knowledge export; never configure it in
  this repository's image.
