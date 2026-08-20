---
name: image-build
version: "2.23"
last_updated: 2026-08-20
id: image-build
one_line_purpose: Derive and pin the review contributor image safely.
entry_point: docs/skills/image-build.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [containerfile, image, digest, pinning, build, audit]
description: "Use when maintaining the pinned FSDK-derived contributor image, audit, Goose canary assets, Hive runtime, or publishing path."
metadata:
  type: procedure
---
# Image Build
## When to Use
Load this before changing `image/Containerfile`, `image/config/`, image pins,
or published contributor-image behavior.

## Ownership Boundary
Image *content* is owned upstream in `projectbluefin/fsdk-containers`, not
here. `image/Containerfile` derives from `ghcr.io/projectbluefin/lab-runner`,
which BuildStream assembles from `elements/lab-runner/lab-runner-stack.bst`
composed of `freedesktop-sdk.bst:components/*.bst`. Adding a userland tool
means adding or updating a BST element there, not patching the Containerfile.

This repository's only lever is the `FSDK_RUNNER_IMAGE` build arg, which pins
the resulting digest. Four substitutes have each been proposed and rejected:
a Containerfile package overlay, a multi-stage `COPY` out of a third-party
image such as busybox, a `curl` of a prebuilt binary, and a new intermediate
`review-base` image. Adding the component upstream and bumping the digest is
the whole fix; reach for nothing else.

Builds run on the ghost cluster's BuildBarn remote-execution grid per
`fsdk-containers`' `docs/skills/remote-execution.md`. `BST_LOCAL=1` is a
degraded-mode opt-out that must be announced when used and is not acceptable
as a permanent workaround.

## A Base Gap Is Filed, Not Described

When the base turns out to be missing something, **open an issue on
`projectbluefin/fsdk-containers` and reference it by number.** Do not describe
the gap here.

A tracked issue has a state, an assignee, and a close event, so it disappears
when the component lands. A paragraph has none of those: it survives the fix,
and a future reader cannot tell a live defect from a closed one. Worse, prose
explaining why something is missing reads as justification, which is how "this
is broken" quietly becomes "this is expected" — the grandfathering `AGENTS.md`
rejects. Write down the reproduction where someone can close it.

Only two things belong in this repository. A code comment gets one line where
the code is otherwise unreadable — what the line does and which issue deletes
it — and nothing more. A user-visible limitation gets one sentence naming the
issue.

Report what you can reproduce, follow the evidence rules in
[`upstream-hive.md`](upstream-hive.md), and let the component owner decide the
fix.

## Core Process
1. Derive from the FSDK lab-runner base pinned by a tagged digest
   (`name:tag@sha256:`). The digest is the security property; the tag is what
   makes the pin *trackable*, because a reference carrying no tag gives an
   update manager no version series to compare against. A bare digest is not a
   stricter pin, it is an untracked one. Keep the Hive commit equal to the
   launcher setup commit so both use the same protocol revision.
2. Audit the exact base digest at runtime before adding anything. Moving FSDK
   source, image labels, and SBOM package records can disagree with the
   filesystem; command execution and file inspection against the pinned digest
   define the base interface.
3. Add only the contributor delta: Goose, the pinned official Codex CLI,
   tmux, GitHub CLI, Node with `ws`, the pinned Hive runtime, controlled
   policy/configuration, and approved agent tools. Do not duplicate a
   capability already present in the verified base.
   Give copied runtime files explicit image modes; never inherit readability
   from the checkout's umask or filesystem defaults.
   Create `/home/dev/Downloads` for Textual's built-in SVG screenshot
   delivery; a runtime home without that standard destination makes the
   authentic command-palette capture fail.
   Do not turn the image into a general-purpose distribution.
4. Preserve canonical command semantics. Never shadow `grep`, `find`, `cat`, or
   `ls` — with a modern alternative or with a hand-written one. If a modern
   tool is added, install it under its own native name (e.g. `rg`) beside the
   canonical command, never as a replacement. `rg` is the only one installed
   today, from its official architecture-specific release with a pinned
   checksum. The layer guards itself: it refuses to build if `rg` is already
   on PATH or in any standard binary directory, and re-proves after
   installing that `grep`, `find`, `cat` and `ls` still resolve outside
   `/usr/local/bin`.
   `just` and mikefarah `yq` v4 come from the base and must not be installed
   again. Linters are fsdk-containers#89, not a layer here; `fd`, `bat`,
   `eza`, editors, pagers and compilers stay out.
5. The rule, without exceptions: **use the tools already in the image; if a
   common tool is missing, add it at the FSDK seam; never hand-roll a
   reimplementation; and never leave a shim standing once the seam fix
   lands.** There is no standing exception to this and no wording that creates
   one. Missing standard runtime utilities belong at the FSDK seam, and going
   there works: `lab-runner` once shipped coreutils alone, so `which`, `xargs`, `ps`,
   `awk`, `tar`, `diff` and `patch` exited 127 in live runs until the
   components were added upstream, fixing every consumer at once. Never answer
   a missing utility with a shim here — **and a shim left standing after the
   seam fix lands is worse than the original gap.** review's Python `find` and
   `cmp` shims outlived the base gaining GNU findutils 4.10.0 and diffutils
   3.12. Because PATH is
   `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` and the shims
   installed to `/usr/local/bin`, they *shadowed* the real tools in `/usr/sbin`
   rather than filling a gap — the install location that makes a shim work is
   the same one that hides the fix. `find` was also wrong, and
   destructively so. In Hive's real prune, `find /tmp -maxdepth 1 -type f
   -user dev -name '*.out' -o -name '*.html' -mmin +60 -exec rm -f {} +`, GNU
   binds the action into the second `-o` group and deletes only `*.html` older
   than 60 minutes; the shim applied the action across both OR-groups and so
   also deleted every `*.out` regardless of age. Measured on one tree (`a.out`
   seconds old, `c.html` and `d.out` three hours old): GNU removed `c.html`,
   the shim removed `a.out`, `c.html` and `d.out`. That ran every ten minutes
   against `/tmp` with stderr discarded, silently destroying fresh agent task
   output each cycle, so deleting it is a data-loss fix. **Recheck the base
   before assuming a gap, and check PATH order before assuming a shim is
   inert.**
   The build asserts the seam instead, which is stronger than a test that only
   ever read the shim files: `image/Containerfile` runs both verbatim Hive
   invocations as the `dev` user in a layer after that user exists, and rejects
   a `find` or `cmp` resolving from `/usr/local/bin`; `tests/image-contract.sh`
   pins that layer, its ordering, and the shims' absence; and
   `tests/image-audit.sh` checks provenance in the built runtime.
   `tests/find-semantics.sh` is deleted -- the shim it read no longer exists,
   and the build is now the gate.
6. Pin Node, GitHub CLI, tmux, and Codex CLI versions and verify their
   checksums. Codex comes only from OpenAI's official architecture-specific
   Linux release assets, installs as the upstream binary without repacking,
   and is executable in the final runtime as `codex`. For
   mutable Goose `canary`, CI resolves official `unknown-linux-musl` asset
   digests before each build, passes them as build inputs, and records them in
   image configuration and provenance. The build verifies the selected archive
   checksum and `gh attestation verify` provenance against the official
   repository and `canary.yml`; a moved asset fails rather than silently
   changing an image. Extract safely; never compile, strip, repack, or fork
   Goose; preserve glibc loader links for dynamic Node and GitHub CLI. Lock
   `ws` in root `package-lock.json` with `npm ci --omit=dev --ignore-scripts`;
   keep fixed Node/gh/tmux/Codex/ws ahead of mutable Goose. Unpack with the base's
   own GNU tar, never a hand-rolled extractor — `tar -xO ... --occurrence=1`
   for a single binary, `--strip-components=1` for Node's versioned tree — and
   keep each `sha256sum -c -` ahead of the archive's first read. A missing
   member then
   fails the build with `Not found in archive` rather than writing an empty
   binary. The `tar -I 'python3 -m gzip'` filter supplies the one codec the
   base lacks; see fsdk-containers#87, which deletes it. Do not combine that
   filter with `--occurrence=1` on an archive whose wanted member is followed
   by others: tar stops reading as soon as it has the member, and the Python
   codec dies on the resulting `BrokenPipeError` where the gzip binary would
   have exited quietly, so the build fails with `tar: Child returned status 1`.
   Decompress whole first (`python3 -m gzip -d < x.tar.gz > x.tar`), then
   select the member from the plain tar. Remove only Node
   headers and verified-unused npm cache; retain `node`, `npm`, and
   `corepack`.
7. Place controlled Goose configuration under `/opt/bluefin/goose` as the
   image-owned policy, data, and state seam. Revalidate compatibility settings
   against the pinned Hive runtime before retaining them; do not preserve stale
   workarounds solely because an older Hive revision needed them. The current
   pin preserves its runtime config when present and creates Goose-native
   `AGENTS.md` and `.goosehints` links, so do not add a `CONTEXT_FILE_NAMES`
   compatibility override for legacy `CLAUDE.md`.
8. Generate org skills at build time from the pinned common catalog into
   `/home/dev/.agents/skills`. Review the generator and catalog inputs, never
   generated output. Remove build-only generation tooling from the final
   filesystem when the build shape permits it.
9. Keep credentials, workspaces, and host configuration out of image layers.
   Supply the GitHub token used for canary provenance verification as the
   required `github_token` build secret; it is available only to that `RUN`
   step and must not be an argument or environment layer. Codex subscription
   OAuth is likewise runtime-only: the image carries the CLI and an empty
   `/home/dev/.codex`, never an auth cache or provider configuration.
10. Treat the image as a task runtime, not a general validation distribution.
   At startup, probe the baseline validation commands (`bats`, `shellcheck`,
   `hadolint`, `systemd-analyze`, `pre-commit`, `just`, `podman`, and
   `actionlint`) and
   report only the missing ones, naming fsdk-containers#89 so the absence is
   traceable — `just` comes from the FSDK base — without blocking Hive or
   installing them solely to hide the absence.
11. Before an FSDK pin is built, audit with
    `bash tests/image-audit.sh --verify-base-evidence`; it verifies the
    `projectbluefin/fsdk-containers` attestation and both platform manifests.
    Audit each derived build with `bash tests/image-audit.sh --derived <image>`.
    The audit is the CI gate: it fails on a disappeared base command, an
    appeared package manager, a base manifest list without exactly
    linux/amd64 and linux/arm64, a derived rootfs that does not preserve the
    exact base layers, and — when given the publish flags — a published
    manifest list missing a platform or whose OCI labels diverge from the
    pinned source. Its report states only current exact-digest facts; retired
    comparisons stay in #70/#87. Both platform slots are always recorded —
    native or unavailable — and an unavailable slot is never a skipped row or
    a QEMU substitute (native arm64 runtime measurement is #77). `--report
    FILE` writes the Markdown report to a file; reports are generated output
    and stay out of git (`image-audit-report.md` is ignored).
    Publishing requires a signed SLSA provenance bundle and a signed SPDX SBOM
    attached to the published index digest, a GitHub artifact attestation for
    that digest, and post-publish verification of both platforms, OCI
    labels/annotations, both signed bundles, and the GitHub attestation. Never
    call QEMU runtime proof native.
    The publish workflow builds each architecture on a runner of that
    architecture — `ubuntu-24.04` and `ubuntu-24.04-arm` — with podman, proving
    the host, podman engine, and container architecture in each job before it
    pushes and audits its own image. Those generated reports in the GitHub
    Actions step summary are the acceptance artifact. The `publish` job
    assembles an OCI index from the two native digests with buildah and cannot
    move `:stable` without both.
12. Measure compressed manifest, unpacked filesystem, layer/directory deltas,
    cold/warm builds, and native amd64/arm64 runtime behavior before and after
    each composition change. Deleting inherited files in a later layer does not
    reclaim the base layer.
The publish workflow moves `:stable` on every push to main, and can be run by
hand with `workflow_dispatch`. It also publishes immutable `sha-<commit>`
tags; use an immutable tag or digest when reproducibility is required. Do not
use `:latest`.

Goose's `canary` asset moves without a commit here, so `:stable` tracks it
only as far as the last push. That is deliberate: it was previously chased by
an hourly scheduled rebuild, which shared a concurrency group with the push
build and cancelled it, then skipped its own build because the Goose digests
had not changed — reporting success while leaving a merged commit unpublished.
A publish that only runs when the source changes cannot do that. Run the
workflow by hand when a canary refresh is wanted without a commit.

Two rules follow, and both are about trusting the wrong signal:

- **A scheduled run must never share `cancel-in-progress` with a push.** The
  schedule's whole purpose is a condition the push does not know about, so
  when it cancels a push it substitutes a run that cannot publish the source.
- **Verify the published image, never the green check.** After any change that
  must reach users, confirm `:stable` moved onto the merge commit. No registry
  client is needed — the contributor image ships none (fsdk-containers#164),
  and ghcr.io serves public packages anonymously. The publish tags every image
  `sha-<commit>`, so compare the digests the two refs return:

  ```bash
  tok=$(curl -fsSL "https://ghcr.io/token?scope=repository:projectbluefin/review:pull" | jq -r .token)
  curl -fsSI -H "Authorization: Bearer $tok" \
    -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json" \
    https://ghcr.io/v2/projectbluefin/review/manifests/stable \
    | grep -i docker-content-digest
  ```

  HEAD `manifests/sha-<commit>` the same way; `:stable` has the change when
  the digests match. ghcr content-negotiates strictly: an `Accept` header
  that omits the stored media type answers 404 for a ref that exists.

  A green publish workflow has meant "nothing was published" twice — once from
  the cancelled schedule, once from a commit message that skipped CI entirely.
## Pin Maintenance
**An unmaintainable pin is a stale pin.** A pin's strictness is worthless if
no automation can see past it, and a frozen pin raises no failing check — it
looks maximally strict while being maximally stale. Both pins in this image
reached that state at once: the Hive commit had no manager able to match it,
and the FSDK base carried a digest with no tag. When adding or reshaping a
pin, establish its update path in the same change and prefer a reference shape
a manager can resolve. The Hive SHA lives in three places that must move
together in one commit:

| Location | Form |
|---|---|
| `justfile` | `hive_commit := "<sha>"` |
| `image/Containerfile` | `ARG HIVE_COMMIT=<sha>` |
| `README.md` | the bare SHA in prose |

CI enforces this: `tests/image-contract.sh` requires the launcher and image
pins to be equal, and `.github/workflows/validate.yml` requires `README.md` to
contain the launcher pin. Updating any two of the three fails the build. Hive's
default branch is `v2`, not `main`. Resolve a candidate SHA from `v2` and use
the full 40-character commit; the launcher rejects a branch name.

Hive is a **protocol** dependency, not a library. The image consumes exactly
three upstream files — `bin/contributor-agent.sh`, `bin/contributor-relay.sh`,
and `config/backends.conf`. A bump is only safe to automerge when those three
are unchanged between the old and new SHA; otherwise read the diff and update
[`hive-runtime.md`](hive-runtime.md) and [`hive-triage.md`](hive-triage.md) in
the same change. That condition is machine-checked by
`.github/workflows/hive-pin-gate.yml`, which derives the consumed-file list from
`image/Containerfile` rather than a hand-maintained list; keep the two in step
when the image starts or stops consuming an upstream file.

Never add a downstream workaround for an upstream protocol gap. Moving the pin
is the fix; a local retry, poll, timeout, or shim becomes a permanent
compatibility burden for both sides. See [`upstream-hive.md`](upstream-hive.md).

## When Not to Use

Do not use this runbook to change Hive assignment, checkout, or contributor protocol behavior; those belong in Hive. Do not switch the final image to a shell-less base or copy a hand-selected dynamic-library closure from another distribution.

## Common Rationalizations

- "Renovate covers it." Confirm the pin's shape is one a manager can resolve. A
  bare `image@sha256:` reference and an unmanaged shell variable both look
  pinned and never move.
- "A digest with no tag is the safest possible pin." It is the safest *build*
  and the least maintainable pin; carry the tag so the digest moves forward
  deliberately.
- "It's only a SHA bump, automerge it." Hive is a protocol dependency; verify
  the three consumed files are unchanged first.
- "Replacing grep/find/cat/ls makes every agent faster." These commands are a
  script interface, and the scripts are not all yours: Hive's relay calls `find`
  too. Install modern tools beside them, never substituting semantics.
- "Passing `--env NAME=value` is harmless." Podman exposes command arguments
  locally; export the value and use `--env NAME` so Podman inherits only that
  host environment entry.
- "Writing the gap down means it's tracked." A document cannot be assigned,
  queried, or closed. It outlives the fix and turns a defect into expected
  behavior. File the issue; reference the number.
- "Someone needs the background." The reproduction belongs in the issue, where
  the person who can fix it will look. Nobody debugging the base reads a
  downstream skill first.

## What The Image Audit Forbids

`tests/image-audit.sh` keeps two rules apart that are easy to conflate. A
**package manager** (`apt`, `dnf`, `apk`) is forbidden in both images always:
content comes from BST elements, so a self-mutating runtime is a defect.
**Anything review installs itself** (`node`, `npm`, `gh`, `tmux`, `codex`,
`goose`) is forbidden in the base only, because a second copy means two
versions and no way to know which an agent ran.

**Ordinary userland is forbidden nowhere unless review installs it.** `find`,
`cmp`, `diff`, `fd`, `yq` and ShellCheck belong in the base when a
contributor needs them; their absence is what made live agents fail with
`command not found`. Add them at
the BST seam, never here, and file the ones that are missing rather than
describing them. For `find`, `cmp`, `diff`, `grep`, `cat` and `ls` the audit
checks provenance as well as presence: Hive's relay calls `find` and `cmp`
directly, the base carries real GNU implementations, and the audit fails if
any of those six resolves under `/usr/local/bin` — the shape a
reintroduced shim would take.

`rg` is the one exempt from that check, because review installs it there
(#75). It is forbidden in the base and required in the derived image, so if
the base ever ships it the audit fails and review's layer is deleted.

## Red Flags

- A floating base image or unverified download. Goose's canary source is
  mutable by design, but its archive needs verified signed provenance.
- A bare-digest reference with no tag, or any pin with no update path.
- Treating current FSDK source or labels as proof of an older digest.
- A Hive pin differing from the launcher setup pin, a bump moving fewer than
  all three locations, or an automerge whose consumed upstream files changed.
- A secret, host workspace, or host configuration baked into a layer;
  writing Goose configuration to `~/.config/goose`.
- Committing generated `.agents/skills/` output, adding a second agent backend
  or unrelated runtime package.
- A custom compile, repacked bundle, package manager, command shadow, or copied
  cross-distribution closure.
- A local reimplementation of a standard utility, or a shim surviving a gap the
  FSDK base has since closed.
- A document, prompt, or policy naming a tool as absent without executing it at
  the pinned digest first. `local-agent-policy.md` told the agent that `which`,
  `awk`, `xargs`, `ps`, `tar`, `less`, `file`, `diff` and `patch` were "not
  installed" while all nine were present in `/usr/sbin`, so every task routed
  around them into hand-rolled `sed`/`python3` substitutes. A false absence
  claim costs the same as a missing tool and is invisible in a passing test.
- A base gap described in a document instead of filed as an issue, or any
  section that exists to explain a known-broken thing.
- An anti-duplication guard that probes only one of PATH or a fixed directory
  list. Each misses what the other catches: `ENV PATH=/opt/node/bin:${PATH}`
  puts a directory ahead of `/usr/local/bin` that no standard-directory walk
  names, while a copy outside the build user's PATH is still a duplicate the
  runtime user resolves. Probe both, or the guard passes while two copies ship
  and nothing records which one an agent ran.
- A multi-file payload copied one named file at a time, or a build proof that
  only compiles it. Compiling resolves no imports, so `py_compile` on the
  dashboard entry point passed while `review_result.py` was never copied, and
  `just review-queue` died at startup with `ModuleNotFoundError` against the
  published `:stable`. Copy the whole set with a glob and prove each module by
  importing it.

- Reaching for BuildKit, `docker buildx`, `docker/build-push-action`, or QEMU
  cross-building. The toolchain is podman, buildah and skopeo — the engines
  that actually run this image — and each architecture is built by a runner of
  that architecture. Building with an engine no contributor runs hides
  engine-specific defects: BuildKit gives every parent directory a `COPY
  --chmod` creates the copied file's mode, so `COPY --chmod=0644
  image/tui/requirements.lock /opt/bluefin/tui/requirements.lock` created
  `/opt/bluefin` and `/opt/bluefin/tui` with no execute bit. Root ignored it
  for the rest of the build and every layer passed; the unprivileged `dev`
  user could not traverse into `/opt/bluefin/goose`, and Goose died at startup
  with `Failed to read config file: Permission denied`. Podman creates those
  parents `0755`, so no contributor could reproduce the published image's
  defect locally. The mode is now asserted in a layer of its own (`find
  /opt/bluefin \( -type d ! -perm -o=rx \) ...`), never by a string match on
  the Containerfile.

- Assuming a GitHub runner's tool versions. `ubuntu-24.04` and
  `ubuntu-24.04-arm` ship podman 4.9.3 and buildah 1.33.7, which predate both
  `--secret id=NAME,env=VAR` and `buildah manifest annotate --index`. The
  publish path therefore passes the token as a `0600` file
  (`--secret id=github_token,src=FILE`) and assembles the index in a
  digest-pinned `quay.io/buildah/stable` container, unprivileged, moving
  descriptors rather than layers. Every job prints `podman --version`,
  `buildah --version` and `skopeo --version` so a mutated runner image fails
  with the tool's own name in the log rather than inside the step that needed
  it.
- Relying on credential lookup order. podman falls back to
  `$HOME/.docker/config.json` when no containers auth file exists, but
  `actions/attest` reads *only* that path — so writing podman's own default
  left both build jobs green through push and scan, then failing on
  `Error: No credentials found for registry ghcr.io`. Write both files and
  name the file at every use (`--authfile`), rather than depending on which
  one a given tool searches first. `actions/attest` also needs
  `artifact-metadata: write`, or every attestation step warns and its storage
  record is dropped.
- Attaching one SBOM to a multi-architecture index. An SBOM describes one root
  filesystem, and syft scanning an index reports whichever platform it
  resolved. Generate the SBOM in the job that built that architecture and
  attest it against that platform's digest; the index carries provenance.
- Assembling an index without checking the digests differ. buildah keys index
  entries by digest, so two identical digests collapse into a single entry and
  `--arch` silently relabels it: the result claims one platform while looking
  like a normal push. The publish job refuses equal digests instead.

## Verification

```bash
bash tests/image-contract.sh
bash tests/hive-compatibility.sh
bash tests/generate-skills.sh
bash tests/image-audit.sh --verify-base-evidence
grep -Fq "$(sed -n 's/^hive_commit := "\(.*\)"$/\1/p' justfile)" README.md
ref="ghcr.io/projectbluefin/review:sha-$(git rev-parse HEAD)"
# `canary` is mutable, so the Containerfile's Goose checksum defaults go stale
# and a local build fails at `sha256sum -c -` even though nothing is wrong.
# CI resolves these per build; resolve them the same way by hand.
goose_sha() {
  curl -fsSL "https://github.com/aaif-goose/goose/releases/download/canary/goose-$1-unknown-linux-musl.tar.gz" |
    sha256sum | cut -d' ' -f1
}
GH_TOKEN="$(gh auth token)" podman build \
  --format oci \
  --secret id=github_token,env=GH_TOKEN \
  --build-arg GOOSE_REFRESH="$(date +%s)" \
  --build-arg GOOSE_X86_64_SHA256="$(goose_sha x86_64)" \
  --build-arg GOOSE_AARCH64_SHA256="$(goose_sha aarch64)" \
  -f image/Containerfile -t "$ref" .
bash tests/image-audit.sh --derived "$ref"
# Optional: keep the Markdown report (generated output, git-ignored).
bash tests/image-audit.sh --derived "$ref" --report image-audit-report.md
git diff --check
```

The `find` and `cmp` Hive's relay calls come from the FSDK base, so there is
nothing in the checkout to test: `image/Containerfile` proves them at build
time against the real base and the build fails if either regresses.
## Sources
- Hive `v2`: `bin/contributor-agent.sh`, `bin/contributor-relay.sh`,
  `config/backends.conf`; Goose `canary` assets; Context7 `/npm/cli`,
  `/websites/podman_io_en` (`--secret` forms, authfile lookup order),
  `/podman-container-tools/buildah` (`manifest annotate --index`),
  `/podman-container-tools/skopeo`, `/websites/cli_github_manual`,
  `/websites/github_en_actions`.
