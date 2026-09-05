# review — Agent Operating Contract

`review` is the Bluefin review appliance: one OCI image fork and a launcher.
The current `review-container` and `review-queue` recipes run the restored
Goose/Hive worker and maintainer dashboard. Review owns the image, publication,
launcher credential handoff, and review context; Hive owns its contributor
protocol, task selection, tmux session, prompt injection, and output capture.

## Read order

1. This file.
2. [`docs/factory/agentic-model.md`](docs/factory/agentic-model.md).
3. [`docs/SKILL.md`](docs/SKILL.md).
4. The one matching file in `docs/skills/`.

## Boundaries

Keep this repository focused: it ships the review appliance and nothing
beside it. Persistent state stays limited to launcher configuration and the
review-queue landing record the launcher mounts for the dashboard.

The interactive recipes run the image runtime in the foreground of the
terminal that launched them, and Ctrl-C stops them. The one permitted
background path is the detached worker: `REVIEW_DETACH=1` labels the
container `review.owner=detached`, a later launch refuses to reclaim it, and
`just review-stop` is its explicit lifecycle verb. No launch path may
background a run implicitly — no `nohup`, no unlabeled `podman run -d`, no
job that silently outlives the terminal. Cleanup of interactive runs remains
a startup concern: a launch reclaims whatever a previous run left behind.

Every launcher path that can execute agent-controlled work uses gVisor's
host-side `runsc` OCI runtime explicitly. The launcher first runs a
credential-free rootless Podman probe and verifies the probe container's
reported OCI runtime; a missing, unusable, or mismatched runtime fails before
an agent starts or credentials are mounted. There is no fallback to Podman's
configured default. Build, audit, and unrelated utility containers are not
agent execution paths and do not inherit this runtime policy.

That rule scopes how the launcher starts the container; it is not a ban
on `&` anywhere in the repository. Backgrounding is required where it is what
preserves signal responsiveness. Bash defers a trap handler while it waits on
a foreground child, so `image/entrypoint.sh` runs the contributor agent and
`tmux attach-session` as background jobs it `wait`s on, keeping PID 1
signal-responsive; a foreground attach swallowed SIGTERM for the whole session
and forced podman's ten-second SIGKILL. Do not "fix" that.

`podman run --rm -it` does not bind a container's lifetime to its client:
`conmon` supervises the container, survives the client, and reparents to the
user manager, so a hard-killed run can leave a fully running, ownerless,
unreachable container rather than merely a name. Each launch therefore stamps
`--label review.owner=<boot-id>:<client-pid>` and treats a container as owned
only when that PID is alive, in the same boot, and still names the container in
`/proc`. Anything else is an orphan and is reclaimed silently with `--replace`.

Hive is the sole authority for selecting and assigning contributor tasks: do
not skip, reorder, prioritize, or decline a Hive assignment mid-protocol. The
one permitted filter is own-work exclusion on the maintainer-facing queue
view — a reviewer never receives their own authored pull requests to review.

This appliance owns no lab and depends on none. Nothing in this repository
may require, integrate with, or gate on maintainer-local infrastructure: a
review decision that needs someone's private endpoint to be reachable is
wrong by construction. When a check backed by such a service cannot run,
the deliverable it would have validated is verified from published registry
evidence instead, and the absence of that evidence is reported as a
finding, never as a blocked pull request.

Latest upstream, everywhere. Every dependency — base image, runtimes,
tools, protocols — tracks the newest upstream version, and Renovate moves
every pin automatically. A pin is a checkpoint the automation advances,
never a human gate: no dependency bump may wait on manual review, an audit
checklist, or a conditional workflow. If a bump breaks something, the fix
is forward — a follow-up change — not a brake on the update stream.

The work the appliance produces for other repositories is toil reduction for
under-maintained projects, not feature work: agents repair what is broken and
finish what a project already decided to do, and size every change to be
reviewable by a tired maintainer. When a task can only be completed by
out-of-scope work, an evidenced written finding is the deliverable. That
scope rule governs agent output; this repository itself is a product and
evolves deliberately — see the README for its roadmap. See
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
bash tests/sbom-manifest.sh
bash tests/image-contract.sh
bash tests/bluefin-review.sh
bash tests/dashboard-contract.sh
bash tests/worktree-guard.sh
bash tests/runsc-isolation.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
```

`tests/dashboard-contract.sh` drives the real Textual app through
`tests/dashboard_pilot.py`, so it builds a hash-locked Textual virtualenv from
`image/tui/requirements.lock` at `.cache/tui-venv` on first run (`uv` when
present, `python3 -m venv` otherwise) and reuses it until the lock changes.
`BLUEFIN_REVIEW_TUI_VENV` points it elsewhere.

`tests/image-audit.sh` needs a container engine and network. It uses `podman`;
`CONTAINER_ENGINE` names another one. Check the pinned
FSDK input alone with `--verify-base-evidence`; audit a built or published
image with `--derived <image>`. The report always records both platform slots
as native or unavailable, and `--report image-audit-report.md` writes the
Markdown report to a git-ignored file.

`pre-commit run --all-files` runs the socket-free hygiene checks locally.
`scripts/check-commit-message.sh` runs as a `commit-msg` hook and refuses a
message containing one of GitHub's CI-skip directives: GitHub reads those
anywhere in the head commit message, so a commit that merely writes about one
lands with no validation and no published image, and no failed check to show
for it. This cannot be a CI check — the message that skips CI skips the check
that would catch it.
ShellCheck is a CI-only manual hook because its upstream hook runs in a
container; CI invokes it explicitly.

## References

- Hive protocol, contributor runtime, and upstream issue reporting:
  `kubestellar/hive` (default branch `v2`; no contributing guide or issue
  templates, DCO sign-off required on pull requests).
- Organization skills and factory rules: `projectbluefin/common`.
- External API details: Context7 documentation. Context7 reaches agents both
  through Hive's hub-side knowledge export and through the image's
  configured `context7` extension (keyless public endpoint).
