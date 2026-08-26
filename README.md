# review
enslaving the oppressors since 2026

**TLDR**: Direct OCI fork of Project Bluefin's `lab-runner`, published as
`ghcr.io/projectbluefin/review` for rapid iteration. Goose/Hive runtime
restoration is tracked in [#346](https://github.com/projectbluefin/review/issues/346).

![img](https://github.com/user-attachments/assets/6b8425b8-dedf-4dc9-aa54-60fa9e6cfd91)

The current image contains the published lab-runner shell and its declared
utility set. It does not contain Goose, Codex, Pi, Hive, or a review dashboard.

## What this is for

This is toil reduction for open-source maintainers. The projects we care about
are the under-maintained ones: a widely used library, one tired maintainer, a
two-year backlog, and CI that has been red since a dependency moved. Those
projects need basic work done reliably — broken builds, stale pins, drifted
docs, unreproduced bug reports, untriaged issues.

We are not building features with this. Big projects restrict large
AI-authored pull requests because they cost maintainers more attention than
they return, and they are right to. This is designed as the opposite of that
firehose: small, scoped, evidenced changes that repair what is already broken.
Unglamorous work is the product here, not the consolation prize. See
[`docs/skills/contribution-culture.md`](docs/skills/contribution-culture.md).

## Operating model

`review` participates in the **Bluefin Agentic Factory Feedback Loop**. Its
canonical local model is [`docs/factory/agentic-model.md`](docs/factory/agentic-model.md):
Hive dispatches work to Factory Workers, maintainers retain review and merge
authority, and the MCP app presents read-only Review Evidence. The
documentation, launcher, image, and tests describe one model; none may
silently create a second workflow, authority path, or task queue.

## Current image transition

The first published `review` image is an exact, digest-pinned fork of
`ghcr.io/projectbluefin/lab-runner`. It is intentionally published without
the Goose/Hive contributor runtime so the image can rev quickly; restoring that
runtime is tracked in [#346](https://github.com/projectbluefin/review/issues/346).

Until #346 is complete, the launch recipes run the direct image shell rather
than the Goose/Hive worker or dashboard:

```bash
podman run --rm -it --entrypoint /usr/bin/bash ghcr.io/projectbluefin/review:stable
```

## Reporting upstream

Running a downstream consumer of Hive's contributor protocol means we find
things upstream cannot see from inside. Reporting that evidence to
[`kubestellar/hive`](https://github.com/kubestellar/hive), and following up on
what we file, is part of the job. We report observations, reproductions, and
options with tradeoffs; upstream owns the design decision and its own triage.
We do not add a local workaround for an accepted upstream gap, because a
downstream workaround becomes upstream's compatibility burden later. See
[`docs/skills/upstream-hive.md`](docs/skills/upstream-hive.md).

## Scope

The root `justfile` remains the public launcher surface. During the
direct-copy transition, `review-container` and `review-queue` run the
published image directly; `review-doctor` runs a disposable non-persistent,
credential-free, agent-free isolation probe before checking the image runtime.

## Installing this into your own setup

NOTE: WIP - you want to run this in projectbluefin/common: the container AUTOMOUNTS the repo's agentic skills in the container so that the project context is given to every client. This is important because this let's us make more things deterministic. The more docs and scripts we can put in this thing the easier it is for less capable models to do this work. Local models are VIABLE!

For a checkout, inspect and smoke-test the direct fork:

```bash
just --list
podman run --rm --entrypoint /usr/bin/bash \
  ghcr.io/projectbluefin/review:stable -lc 'bash --version'
```

## Commands

`justfile` is the installable artifact and exposes exactly
four public recipes:

| Command | Purpose |
|---|---|
| `just review-container` | Run the direct lab-runner fork in the foreground; the restored worker is tracked in #346. |
| `just review-stop [name]` | Stop a detached worker. Refuses attended runs and containers this launcher did not start. |
| `just review-queue` | Run the direct lab-runner fork in the foreground; the restored dashboard is tracked in #346. |
| `just review-doctor` | Check the required gVisor/runsc boundary and direct image runtime. |

Interactive runs remain attached to their originating terminal, and Ctrl-C or
closing that terminal stops them. A detached worker (`REVIEW_DETACH=1`) is
the deliberate exception: it carries a `review.owner=detached` label, logs
through `podman logs -f`, is never silently reclaimed by a later launch, and
stops only through `just review-stop`.
Detaching tmux (`prefix`, then `d`) detaches the view only—the originating
terminal remains responsible for the run.

## Public PR queue

The generated public PR queue is a small, static review backlog:

- `/` serves the Markdown overview;
- `/queue.md` serves the Markdown artifact;
- `/queue.json` serves the machine-readable artifact.

Every artifact carries `generated_at`. Treat it as a recommendation snapshot:
check its freshness and verify the selected pull request directly in GitHub
before acting. The queue's actions are `fix-ci`, `resolve-conflicts`, `review`,
`investigate`, and `ready-for-human-merge`; it never authorizes a merge.

GitHub remains authoritative for pull requests, reviews, checks, and merge
state, while Hive remains authoritative for agent coordination. The queue does
not claim work, assign agents, mutate labels, include private repositories, or
run a service. The `queue.projectbluefin.io` custom-domain and DNS mapping are
an operations task outside this repository automation.

## Requirements and credentials

Direct manual image inspection needs only a container engine. The
agent-capable `review-container` and `review-queue` recipes require a trusted
host `runsc` installation and fail before starting an agent or mounting
credentials unless a credential-free rootless Podman probe reports
`OCIRuntime=runsc`. Run `just review-doctor` to check it. Bluefin provisioning
is tracked by [bluefin#1139](https://github.com/projectbluefin/bluefin/issues/1139),
and the Review contract is [#348](https://github.com/projectbluefin/review/issues/348);
the launcher never downloads a runtime or falls back to Podman's default.

Build and publish validation uses Podman, Buildah, Skopeo, `jq`, and the GitHub CLI.
The image itself inherits the published lab-runner runtime; it does not carry
Goose, Codex, Pi, Hive, or a package manager.

The published lab-runner inventory currently includes `bash`, `curl`, `git`,
`jq`, `python3`, `kubectl`, `argo`, `just`, `which`, `xargs`, `awk`, `ps`,
`tar`, `diff`, `patch`, `less`, and `file`. The fork verifies those commands
against the exact digest. The catalog/runtime mismatch for ShellCheck,
Hadolint, Actionlint, gzip, bubblewrap, and Skopeo is tracked upstream in
[fsdk-containers#205](https://github.com/projectbluefin/fsdk-containers/issues/205);
the fork does not install local replacements.

## Reviewing

Review execution is intentionally paused while #346 restores the
Goose/Hive entrypoint and dashboard layers. The direct-copy image is still a
runnable shell image, so it can be inspected with the command shown above;
`just review-container` and `just review-queue` launch that shell directly.

The generated public PR queue remains read-only guidance. GitHub is
authoritative for pull requests, checks, reviews, and merge state, while Hive
remains authoritative for contributor coordination after #346 is complete.

## Configuration

The launcher keeps its existing configuration surface for the runtime
restoration tracked in #346. During the direct-copy phase, the launch recipes
do not read AI credentials; `REVIEW_CONTRIBUTOR_IMAGE` names the image to run
and defaults to `ghcr.io/projectbluefin/review:stable`.

## Image and context

`image/Containerfile` contains only:

```Dockerfile
ARG FSDK_RUNNER_IMAGE=ghcr.io/projectbluefin/lab-runner:25.08@sha256:7c4b1e518bd1bffe2e506474e6196e9c18fb727bbd48a3c5f7ddbd3446ea5846
FROM ${FSDK_RUNNER_IMAGE}
```

The build changes no files, users, entrypoint, environment, or working
directory. The workflow adds OCI metadata at build time, builds both native
platforms, verifies exact base-layer equality, publishes immutable
`sha-$GITHUB_SHA` and `stable` tags, and attaches signed SPDX SBOM, SLSA
provenance, and GitHub artifact attestations.

The source image is `ghcr.io/projectbluefin/lab-runner`, built by
`projectbluefin/fsdk-containers` from the FSDK lab-runner stack. The
`projectbluefin/lab` repository consumes that image for its GitOps test suite;
it is not copied into the image build context.

Use the published image directly while #346 is open:

```bash
podman run --rm -it --entrypoint /usr/bin/bash \
  ghcr.io/projectbluefin/review:stable
```

Use an immutable tag when reproducing a build:

```bash
ref="ghcr.io/projectbluefin/review:sha-$(git rev-parse HEAD)"
podman build --format oci -f image/Containerfile -t "$ref" .
podman run --rm --entrypoint /usr/bin/bash "$ref" -lc 'bash --version'
```

`tests/image-audit.sh --direct-copy` verifies that the derived image has the
same rootfs layers and base runtime as the pinned source. Its platform report
marks non-native architecture runtime evidence unavailable rather than using
QEMU. `tests/image-audit.sh --verify-base-evidence` verifies the upstream
lab-runner manifest list and GitHub attestation.

## Development

The first slice is intentionally limited to the image fork. Use the existing
source-level tests and the direct-copy smoke checks:

```bash
bash tests/image-contract.sh
bash tests/image-audit.sh --verify-base-evidence
podman build --format oci -f image/Containerfile -t review:local .
podman run --rm --entrypoint /usr/bin/bash review:local -lc 'bash --version'
bash tests/image-audit.sh --derived review:local --direct-copy
git diff --check
```

The native publication workflow is the acceptance path for the multi-arch
artifact. It builds on `ubuntu-24.04` and `ubuntu-24.04-arm`, then verifies the
published `stable` and immutable SHA references resolve to the same OCI index.
You can verify that pointer directly:

```bash
token="$(curl -fsSL 'https://ghcr.io/token?scope=repository:projectbluefin/review:pull' | jq -r .token)"
manifest_digest() {
  curl -fsSI \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json' \
    "https://ghcr.io/v2/projectbluefin/review/manifests/$1" |
    awk -F': ' 'tolower($1) == "docker-content-digest" {print $2}' |
    tr -d '\r'
}
test "$(manifest_digest stable)" = "$(manifest_digest "sha-$(git rev-parse HEAD)")"
```

### Validation

```bash
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
bash tests/image-contract.sh
bash tests/bluefin-review.sh
bash tests/dashboard-contract.sh
bash tests/worktree-guard.sh
bash tests/just-onboarding.sh
bash tests/image-audit.sh --verify-base-evidence
git diff --check
just --list
pre-commit run --all-files
```

`tests/image-audit.sh` needs a container engine and network access. It uses
`podman`; `CONTAINER_ENGINE` names another one. Use `--direct-copy` for the
first image slice. Generated audit reports stay out of git.

## License

Licensed under the [Apache License 2.0](LICENSE).
