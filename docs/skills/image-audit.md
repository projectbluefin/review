---
name: image-audit
version: "1.0"
last_updated: 2026-09-06
id: image-audit
one_line_purpose: Audit FSDK base and review container image attestations and SBOM.
entry_point: docs/skills/image-audit.md
category: ci-ops
status: active
tags: [audit, sbom, slsa, attestation, ghcr]
description: "Audits base and derived OCI image composition, SPDX SBOM manifests, SLSA provenance attestations, and multi-arch publishing. Use when running tests/image-audit.sh or debugging publish workflows."
metadata:
  type: procedure
  context7-sources: [/websites/github_en_actions, /websites/podman_io_en]
---

# Image Audit and Attestations

> Review enforces rigorous supply-chain provenance: signed SLSA bundles,
> SPDX SBOMs, and exact multi-architecture index attestations.

## When to Use

Load this when running `tests/image-audit.sh`, checking SPDX SBOM manifests,
auditing base/derived layers, or debugging `publish-compat-image.yml`.

## When Not to Use

Do not load this for Containerfile layering (`image-build.md`) or local
container launch execution (`launcher.md`).

## Core Audit Process

1. **Verify Base Input:**
   ```bash
   bash tests/image-audit.sh --verify-base-evidence
   ```
   Verifies `projectbluefin/fsdk-containers` attestation and dual-platform manifests.
2. **Audit Derived Build:**
   ```bash
   bash tests/image-audit.sh --derived ghcr.io/projectbluefin/review:stable
   ```
   Ensures base layers are preserved, package managers stay excluded, and
   OCI annotations match source.
3. **SPDX SBOM Generation:**
   `scripts/generate-sbom-manifest.py` generates `/opt/bluefin/sbom/review-components.spdx.json`
   at build time. Syft catalogs it into the attached SBOM.
4. **Multi-Architecture Publishing:**
   Native runners (`ubuntu-26.04` and `ubuntu-26.04-arm`) build and push
   per-architecture layers with podman. The publish job assembles the OCI
   index with buildah and signs SLSA attestations.
5. **Report Generation:**
   `--report FILE` outputs markdown audit summaries. Reports are build
   artifacts and stay git-ignored.

   Platform manifests and their attestations must use the contributor image
   namespace consumed by the multi-architecture index and final audit.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "QEMU emulation is sufficient." | Emulation hides architecture-specific bugs. Build native on native runners. |
| "Skip SBOM for internal components." | Every binary from archive releases must be registered in the SPDX catalog. |

## Red Flags

- Moving `:stable` without both `amd64` and `arm64` native image attestations.
- Adding package managers (`dnf`, `apt`, `apk`) to the runtime image.
- Committing generated markdown audit reports to git.

## Verification

```bash
bash tests/sbom-manifest.sh
bash tests/image-audit.sh --verify-base-evidence
```
