# Image and development guide

The repository ships two OMP-owned images:

- `ghcr.io/projectbluefin/review`, built by `image/appliance/Containerfile`,
  carries the maintainer workbench extension.
- `ghcr.io/projectbluefin/contribute`, built by
  `image/contribute/Containerfile`, carries Hive's contributor relay and OMP.

The contributor image leaves provider, model, and thinking-effort selection to
OMP. The review image additionally ships two native OMP routing overlays:
`copilot-mixed` is the default and `codex-subscription` is opt-in. Local
launchers select an overlay by name; OMP still resolves roles, effort, auth,
and execution. Both launchers prefer Podman's `krun` runtime and KVM, then
fall back explicitly to isolated Apptainer execution.

Both images pin FSDK bases by tag and digest and pin fetched runtime assets by
architecture-specific SHA-256. The contributor image installs the root
`package-lock.json` solely for Hive's pinned `ws` dependency. Hive retains
assignment, lease, prompt, credential, and completion authority.

The daily Renovate workflow follows stable `can1357/oh-my-pi` GitHub releases.
Its allowlisted post-upgrade task runs `node scripts/update-omp-pins.mjs`, which
requires both Containerfiles to name the same OMP version and replaces their
per-architecture digests from the matching release assets. After checks pass,
the OMP-only Renovate PR automerges; that `main` push publishes both native
multi-architecture images.

## Development

```bash
# Maintainer appliance
just review-appliance-build

# Contributor runtime
podman build --format oci -f image/contribute/Containerfile \
  -t localhost/projectbluefin/contribute:dev .
CONTRIBUTE_IMAGE=localhost/projectbluefin/contribute:dev just contribute
```

Interactive runs use unique container names, target-specific persistent volumes,
remain foreground, and stop with `Ctrl-C`.

## Validation

```bash
pre-commit run --all-files
git diff --check
bash tests/check-commit-message.sh
bash scripts/check-skill-frontmatter.sh
bash tests/generate-skills.sh
just --list
bash tests/just-onboarding.sh
bash tests/readme-quickstart.sh
bash tests/test-registry.sh
node --test tests/update-omp-pins.test.mjs
node --test tests/update-derived-pins.test.mjs
bash tests/omp-review-mode.sh
bash tests/appliance-contract.sh
bash tests/contribute-contract.sh
```

Native image builds and smoke checks run in `publish-appliance.yml` and
`publish-contribute.yml`; no third contributor image or publishing path exists.
