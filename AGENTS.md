# review — Agent Operating Contract

`review` is the Bluefin review appliance: the `image/extension/bluefin-review`
mode for Oh My Pi shipped in a distroless appliance image, and a launcher.
The primary maintainer review product runs via the OMP extension entrypoints:
in source via `bin/omp-review` or packaged via `just review-appliance` and
`image/appliance/Containerfile`. `review-queue` is a convenience alias for that
same appliance. The `review-container` recipe remains the isolated Hive worker.
Review owns the appliance image, extension, launcher credential handoff, and
review context; Hive owns its contributor protocol, task selection, tmux
session, prompt injection, and output capture.
OMP owns agent execution, sessions, tasks, and tool boundaries. Maintainer Hive
reads remain optional, non-mutating context and ordering, while contributor
task selection and assignment remain Hive-owned.

## Read order

1. This file.
2. [`docs/factory/agentic-model.md`](docs/factory/agentic-model.md).
3. [`docs/SKILL.md`](docs/SKILL.md).
4. The one matching file in `docs/skills/`.

## Boundaries

Keep this repository focused: it ships the OMP review appliance and the Hive
contributor runtime. Persistent maintainer state belongs to OMP's appliance
home volume; no second dashboard state store is permitted.

### Product boundary: GitHub-first core, optional Bluefin/Hive

Review is a GitHub-first core with optional Bluefin and Hive integrations
(decision in [#591](https://github.com/projectbluefin/review/issues/591)). It
works for any GitHub repository; Bluefin and Hive are additive, never required.

- **Review** (`image/extension/bluefin-review/`) provides the generic core:
  PRs, issues, queues, search, reading, inspection, review, repair,
  implementation, and landing. PRs and issues are first-class objects in any
  repository. Actions are intended to match the object: PR
  review/diff/fix/Slay, and issue inspect/implement/fix. Backend capability and
  current feature support remain authoritative; the core does not promise that
  every available control succeeds for every selected object, and never labels
  an authorized session "browse-only" when Hive is absent — GitHub actions
  remain available.
- **OMP** provides sessions, agents, execution, and traces.
- **Bluefin** adds its doctrine, specialized reviewers, labels, conventions,
  admission rules, and organization policy. It is selected where appropriate;
  general reviewers follow the target repository's own rules.
- **Hive** adds ordering, claims, stages, knowledge, and contributor context
  through the Hive MCP server when enabled. Without Hive, GitHub evidence
  remains available, but Hive ordering and Hive-only claims, stages, knowledge,
  and contributor context do not fall back to GitHub. It keeps ownership of
  assignments and completion; Review does not take over Hive assignments or
  add a scheduler.

GitHub defines what work exists. Review defines what can be done with it. Hive
may prioritize and coordinate it. Bluefin may specialize its policy. Nothing in
the core requires Bluefin or Hive: GitHub access, operator permissions,
execution requirements, and safety checks still apply. Safety is preserved as-is
— current-head verification, CI checks, branch protection, token-scope
protections, mutation guards, read-only reviewers, and explicit maintainer
authorization. This boundary does not enable workflow mutation.

The interactive recipes run the image runtime in the foreground of the
terminal that launched them, and Ctrl-C stops them. Detached contributor
containers are not supported; `REVIEW_DETACH=1` is rejected. No launch path may
background a container run — no `nohup`, no unlabeled `podman run -d`,
no `REVIEW_DETACH=1`, and no job that silently outlives the terminal. Cluster
workers run in Kubernetes and are stopped with `just review-stop` (or
`just review-stop cluster`). Cleanup of interactive runs remains
a startup concern: a launch reclaims whatever a previous run left behind.

When an operator configures an SSH-backed default Podman system connection, the
launcher stages the selected `0600` Hive contributor registration to a unique
per-run private `0700` directory on the remote engine host for the duration of the
container run, and removes only that private staging path on exit. Remote canonical
configuration (`~/.config/hive` and `contributor.env`) is never altered or deleted.
Operator control remains explicit through Podman connection configuration; no
credential values, SSH targets, or endpoints appear in arguments, logs, or committed
files. Preserve `--userns keep-id` for rootless Podman access to the `0600` Hive
contributor credential; never loosen that file's permissions as a workaround.

That rule scopes how the launcher starts the container; it is not a ban on `&`
inside a signal-aware entrypoint. `image/contribute/entrypoint.sh` starts Hive's
`contributor-agent.sh` as the owned child, attaches the attended terminal
directly to Hive's OMP tmux session, and tears both down through one bounded
trap. Do not add a second status UI around that session.

Every local review or contribution launch prefers Podman's `krun` OCI runtime.
When Podman, `krun`, or `/dev/kvm` is unavailable, it reports the reason and
falls back to isolated Apptainer execution.
KVM invocations get unique container names; fallback invocations get separate
target-specific home and workspace directories. Persistent OMP state is keyed
by repository or explicit instance name. `BLUEFIN_INSTANCE` separates
concurrent sessions for the same target. Local instances stop with their own
terminal; `review-stop` only manages the Kubernetes contributor deployment.

Hive is the sole authority for selecting and assigning contributor tasks: do
not skip, reorder, prioritize, or decline a Hive assignment mid-protocol. The
maintainer-facing queue may locally promote pull requests authored by the
authenticated user when review requested changes, but that lane is repair-only.
A reviewer never receives their own authored pull request to review, approve,
or merge.

An explicit maintainer slay delegates one bounded lifecycle to the workbench
coordinator: ordinary PR review, repair, fresh review, and landing; returned-PR
repair through a new head; or issue implementation through a submitted closing
PR. Evidence reviewers remain read-only.

The review mode in `image/extension/bluefin-review/` equips OMP with companion
review agents (`bluefin-doctrine`, `bluefin-reviewer`, `bluefin-security`,
`bluefin-correctness`, `bluefin-test-coverage`, `bluefin-simplicity`,
`bluefin-ci-triage`, `bluefin-queue-triage`) and LLM-callable inspection tools. Bluefin policy remains
in those agents and `policy.ts`; queue, trace, workflowz dispatch, and mutation
guards remain generic OMP/Hive machinery. Skills generated from the Bluefin
catalog belong under `~/.agents/skills/` for contributor sessions and do not
become review checks automatically.

Opening the maintainer workbench never starts a contributor worker. Scaling
cluster workers is an explicit, separate choice — `just review-container
cluster [N]` — and they are stopped with `just review-stop cluster`. A worker
claims Hive assignments under the contributor's own identity and books hub-side
failure cooldowns against their standing when it cannot run, so a surface whose
purpose is reviewing must never scale one as a side effect.

Static queue snapshots are an antipattern: never create or consume one to
understand pull-request status or Hive order. Read live GitHub/Hive state or
OMP's durable appliance state; never infer current queue state from captured
JSON.

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
OMP releases move both Containerfiles through the allowlisted Renovate pin-sync
task; the resulting `main` push publishes both images. See
[`image-build.md`](docs/skills/image-build.md).

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

Reporting downstream evidence upstream to `hivecommons/hive` is expected work,
and filed issues are followed up rather than abandoned. Report observations,
reproductions, and options; upstream owns the design decision and the triage
labels. Never add a local workaround for an accepted upstream gap. See
[`docs/skills/upstream-hive.md`](docs/skills/upstream-hive.md).

## Repository layout

- `justfile` is the only shipped launcher artifact. Its public recipes and
  private helpers intentionally live together; `just --list` is the list.
- `image/appliance/` builds the distroless Bluefin Review appliance image
  carrying OMP, GitHub CLI, shell, and the review extension.
- `image/extension/bluefin-review/` is the TypeScript OMP workbench extension,
  providing the queue, pipeline trace, companion agents, and tools.
- `image/contribute/` builds the OMP-only Hive contributor image. Both
  `contribute` and `review-container` launch it.
- `package.json` and `package-lock.json` pin only the contributor relay's `ws`
  dependency. This repository is not a Node application.
- `bin/omp-review` is the source entrypoint for the OMP review mode.
- `scripts/` contains build-time generators and documentation checks.
- `tests/` contains launcher, OMP-extension, contributor, and image contracts.
- `docs/` contains the skill router and catalog.
- [`docs/appliance.md`](docs/appliance.md) provides detailed appliance
  installation and configuration guidance.

## Permitted changes

Agents may change `justfile`, `deploy/`, `image/`, `scripts/`, `tests/`,
`docs/`, `README.md`, `AGENTS.md`, and `.github/workflows/`.

Do not modify `ublue-os/*`, or commit generated `.agents/skills/` content.
The generator is the artifact; `projectbluefin/common`'s
`docs/skills/index.json` is the organization-skill source.

When behavior changes, update the matching user documentation. Treat the
launcher, image, and tests as the sources of truth for this repository's
behavior.

## PR rules

- PR titles follow Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`,
  `style:`, `refactor:`, `perf:`, `test:`, `ci:`, `build:`, `revert:`), enforced
  by the required `conventional-title` check. The type must be the first token
  in the title — any prefix before it fails the check. The description is
  free-form, so trailing annotations are fine. This repository squash-merges, so
  the PR title becomes the permanent commit subject.

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
bash tests/test-registry.sh
bash tests/omp-review-mode.sh
bash tests/appliance-contract.sh
bash tests/contribute-contract.sh
bash tests/just-onboarding.sh
git diff --check
just --list
pre-commit run --all-files
```


`pre-commit run --all-files` runs the socket-free hygiene checks locally.
`scripts/check-commit-message.sh` runs as a `commit-msg` hook and refuses a
message containing one of GitHub's CI-skip directives: GitHub reads those
anywhere in the head commit message, so a commit that merely writes about one
lands with no validation and no published image, and no failed check to show
for it. This cannot be a CI check — the message that skips CI skips the check
that would catch it.
ShellCheck runs as an ordinary pre-commit hook through the shellcheck-py
wheel: the binary rides the wheel, so the hook needs no container runtime
and no registry pull. Its container-image predecessor was pulled from
Docker Hub anonymously on every CI run, and registry rate limiting turned
every main validation red.

## References

- Hive protocol, contributor runtime, and upstream issue reporting:
  `hivecommons/hive` (default branch `v4`, v2 is retired; no contributing guide or issue
  templates, DCO sign-off required on pull requests).
- Organization skills and factory rules: `projectbluefin/common`.
- External API details: Context7 documentation. The review extension bundles
  GitHub, the public Project Bluefin service, and Context7 as MCP servers;
  credentials remain runtime environment inputs, never image content.
