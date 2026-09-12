---
name: image-build
version: "2.25"
last_updated: 2026-09-11
id: image-build
one_line_purpose: Derive and pin the review contributor image safely.
entry_point: docs/skills/image-build.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [containerfile, image, digest, pinning, build, audit]
description: "Use when maintaining the pinned FSDK-derived contributor image, audit, OMP assets, Hive runtime, or publishing path."
metadata:
  type: procedure
  context7-sources: [/websites/podman_io_en, /websites/github_en_actions]
---
# Image Build
## When to Use

For build commands and image architecture, see the
[image and development guide](../image-and-development.md).
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

## Base Gaps Are Filed Upstream

When the base is missing a utility, **open an issue on `projectbluefin/fsdk-containers`
and reference it by number.** Never describe gaps or grandfather workarounds in docs.
A code comment gets one line naming the issue that deletes it; user-visible limitations
get one sentence naming the issue.

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
3. Add only the contributor delta: OMP, the pinned official Codex CLI,
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
5. **Use tools in the base; add missing tools at the FSDK seam; never hand-roll shims.**
   Never answer a missing utility with a local shim. Shims in `/usr/local/bin`
   shadow upstream fixes and risk semantic bugs. The Containerfile proves
   canonical GNU `find` and `cmp` resolve from `/usr/sbin` and rejects any
   shadowing from `/usr/local/bin`.
6. Pin Node, GitHub CLI, tmux, Codex CLI, and OMP versions and verify their
   checksums. Codex comes only from OpenAI's official architecture-specific
   Linux release assets, installs as the upstream binary without repacking,
   and is executable in the final runtime as `codex`.
   OMP installs from official releases via pinned architecture assets
   (`OMP_VERSION`, `OMP_X86_64_SHA256`, `OMP_AARCH64_SHA256`) and verifies
   checksums before chmod/execution, matching the pattern in
   `image/contribute/Containerfile`. No attestation verification is used.
   Extract safely; never compile, strip, repack, or fork
   agent binaries; preserve glibc loader links for dynamic Node and GitHub CLI. Lock
   `ws` in root `package-lock.json` with `npm ci --omit=dev --ignore-scripts`;
   keep fixed Node/gh/tmux/Codex/ws ahead of OMP. Unpack with the base's
   own GNU tar, never a hand-rolled extractor — `tar -xO ... --occurrence=1`
   for a single binary, `--strip-components=1` for Node's versioned tree — and
   keep each `sha256sum -c -` ahead of the archive's first read. A missing
   member fails cleanly. Remove only Node headers and unused npm cache;
   retain `node`, `npm`, and `corepack`.
7. The container needs no separate agent configuration file; OMP requires
   no static image-owned configuration file.
8. Generate org skills at build time from the pinned common catalog into
   `/home/dev/.agents/skills`. Review the generator and catalog inputs, never
   generated output. Remove build-only generation tooling from the final
   filesystem when the build shape permits it.
9. Keep credentials, workspaces, and host configuration out of image layers.
   Codex subscription OAuth is runtime-only: the image carries the CLI and an empty
   `/home/dev/.codex`, never an auth cache or provider configuration.
10. Treat the image as a task runtime, not a general validation distribution.
   At startup, probe the baseline validation commands (`bats`, `shellcheck`,
   `hadolint`, `systemd-analyze`, `pre-commit`, `just`, `podman`, and
   `actionlint`) and
   report only the missing ones, naming fsdk-containers#89 so the absence is
   traceable — `just` comes from the FSDK base — without blocking Hive or
   installing them solely to hide the absence.
11. Audit base inputs and derived image composition with `tests/image-audit.sh`.
   See [`image-audit.md`](image-audit.md) for SPDX SBOM generation, SLSA provenance attestation, and native multi-architecture publishing on `ubuntu-26.04`.
12. Measure compressed manifest, unpacked filesystem, layer deltas, and cold/warm builds before and after each composition change.

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
default branch is `v4`, not `main`. Resolve a candidate SHA from `v4` and use
the full 40-character commit; the launcher rejects a branch name.

Hive is a **protocol** dependency. The image consumes `bin/contributor-agent.sh`,
`bin/contributor-relay.sh`, and `config/backends.conf`. Never add local workarounds
for upstream protocol gaps; move the pin forward instead.

## When Not to Use

Do not use this for Hive task assignment or contributor protocol behavior (use `hive-runtime.md`).

## Common Rationalizations

- "A digest with no tag is safest." Untracked pins freeze; always carry a tag.
- "Replace find/cmp with faster tools." Standard utilities are script interfaces; install modern tools beside them without shadowing.
- "Writing gaps in docs tracks them." Documentation outlives fixes. File issues on upstream repositories instead.
## What The Image Audit Forbids

See [`image-audit.md`](image-audit.md) for full image audit assertions, package manager prohibitions, and SPDX SBOM cataloging.

## Red Flags

- Floating base images or unverified downloads.
- A bare-digest reference with no tag, or an unmanaged pin.
- Custom compiles, repacked bundles, package managers, or shadowed standard commands.
- Local reimplementations of standard utilities or shims surviving upstream FSDK additions.
- Reaching for BuildKit, `docker buildx`, or QEMU cross-building. Always build natively with Podman/Buildah.
- Committing generated `.agents/skills/` output or markdown audit reports.

`image/contribute/Containerfile` is the separate distroless Hive + OMP closure. It stages only OMP, Node, GitHub CLI, tmux, locked `ws`, the merged Hive runtime, and the FSDK shell/git/python closure; it must not absorb Codex, Pi, dashboard, review scope, or generated skills.

## Verification

```bash
bash tests/image-contract.sh
bash tests/hive-compatibility.sh
bash tests/generate-skills.sh
bash tests/image-audit.sh --verify-base-evidence
grep -Fq "$(sed -n 's/^hive_commit := "\(.*\)"$/\1/p' justfile)" README.md
ref="ghcr.io/projectbluefin/review:sha-$(git rev-parse HEAD)"
bash tests/image-audit.sh --derived "$ref"
git diff --check
```

The `find` and `cmp` Hive's relay calls come from the FSDK base, so there is
nothing in the checkout to test: `image/Containerfile` proves them at build
time against the real base and the build fails if either regresses.
- Hive `v4`: `bin/contributor-agent.sh`, `bin/contributor-relay.sh`, `config/backends.conf`; OMP; Context7 `/npm/cli`, `/websites/podman_io_en`, `/podman-container-tools/buildah`, `/podman-container-tools/skopeo`, `/websites/cli_github_manual`, `/websites/github_en_actions`.
