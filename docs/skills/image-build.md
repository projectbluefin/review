---
name: image-build
version: "3.6"
last_updated: 2026-09-16
id: image-build
one_line_purpose: Build and pin the OMP review and contributor images.
entry_point: docs/skills/image-build.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [containerfile, image, digest, pinning, omp, hive]
description: "Use when maintaining the OMP review/contributor images, release pins, SBOM inputs, or multi-architecture publication workflows."
metadata:
  type: procedure
  context7-sources: [/websites/podman_io_en, /websites/github_en_actions]
---
# Image Build

The repository ships two images:

| Image | Source | Purpose |
| --- | --- | --- |
| `ghcr.io/projectbluefin/review` | `image/appliance/Containerfile` | OMP maintainer workbench and extension |
| `ghcr.io/projectbluefin/contribute` | `image/contribute/Containerfile` | Hive-assigned OMP worker |

There is no `review-contributor` image, SIF package, alternate agent harness, or
model-specific runtime. Both OCI images leave model and effort selection to OMP.

## Rules

1. Pin base images by tag and digest. Pin fetched artifacts by version and
   architecture-specific SHA-256.
2. Build natively per architecture; do not claim QEMU results as native evidence.
3. Never place credentials, user configuration, workspaces, or provider choices
   in an image layer.
4. Do not add package managers or duplicate a tool already present in the pinned
   FSDK closure.
5. The review appliance carries OMP, its extension, GitHub CLI, the minimal
   shell/git/Python closure required by OMP tools, and the pinned FSDK builder's
   `actionlint`, `shellcheck`, `yq`, `jq`, and `just` validators.
6. The contributor image carries OMP, Hive's pinned relay/runtime, Node with the
   locked `ws` module, GitHub CLI, tmux, and the minimal FSDK closure.
7. The contributor entrypoint accepts only `AGENT_BACKEND=omp`; provider, model,
   and effort remain OMP configuration, never launcher or image policy.
8. Local launchers prefer Podman's `krun` runtime. Merely checking or mounting
   `/dev/kvm` is not isolation; `--runtime=krun` is the VM boundary. Missing KVM
   prerequisites produce a warning and select the isolated Apptainer fallback.
9. Preserve Hive's assignment, lease, prompt, credential, and output protocol.
   Do not fork or locally patch its runtime files.
10. Generate SPDX manifests from resolved build arguments and keep build-only
    generators out of the final filesystem.
11. Version derivation rejects malformed or missing revision/base inputs,
    preserves decimal `08`/`09` revisions, and keeps both image series aligned.
12. Execute every staged command in the built image. If an allowlisted path is
    a wrapper, stage and verify its real executable target as part of the same
    closure; file presence is not runtime evidence.
13. Give Apptainer workloads instance-scoped disk-backed scratch storage.
    `--containall` otherwise supplies a 64 MiB `/tmp`, which is too small for
    repository clones and archive inspection.
14. Bundle review-appliance MCP definitions beside the packaged review
    extension in `.mcp.json`. Do not place them under `/home/bluefin`: the
    launcher's persistent home volume masks image content at that path.
15. OMP version and digest pins move as one release unit in both Containerfiles.
    The scheduled Renovate workflow refreshes the GitHub release asset digests,
    merges the validated OMP update, and lets the resulting `main` push publish
    both images.
16. Runtime contract tests for packaged appliances exercise configuration
    resolution in addition to static YAML and CLI flags: an autoslay launch with
    only `modelRoles.default` persisted must start an advisor that resolves
    through the `@default` role alias to that user model without reporting
    inactive.

## Pin maintenance

Hive's source pin appears in `justfile` and `image/contribute/Containerfile`;
move both together from Hive's `v4` branch. OMP pins appear in both
Containerfiles. `node scripts/update-omp-pins.mjs <version>` reads the published
GitHub release asset digests and updates both files atomically. Renovate runs
that command daily after changing `OMP_VERSION`, then automerges only after
repository checks pass. The merge triggers `publish-appliance.yml` and
`publish-contribute.yml`; those workflows build and execute both native
architectures before updating their published indexes. The review and
contribute image revision files remain separate product revisions.
Derived checksum automation for GitHub CLI, Node.js, tmux, and `requirements-ci.lock`
runs in their respective Renovate branches via `node scripts/update-gh-pins.mjs`,
`node scripts/update-node-pins.mjs`, `node scripts/update-tmux-pins.mjs`, and
`node scripts/update-requirements-ci-hashes.mjs`.

## Verification

```bash
node --test tests/update-omp-pins.test.mjs
node --test tests/update-derived-pins.test.mjs
bash tests/appliance-contract.sh
bash tests/contribute-contract.sh
python3 tests/appliance_sbom_contract.py
python3 tests/contribute_sbom_contract.py
bash tests/version-derivation.sh
git diff --check
```

With a container engine, build through `just review-appliance-build` and the
publish-contribute workflow's native build path.
