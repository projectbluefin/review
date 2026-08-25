#!/usr/bin/env bash
# Contract checks for image/ — the half of this repo that actually ships.
#
# These are substring assertions over files, so they are grep, not a test
# framework. They previously lived in image/image_test.go and a bespoke CI
# step, which between them required a whole Go toolchain to check that some
# strings appear in a Containerfile.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

fail=0

# require <file> <substring>... — every substring must appear.
require() {
  local file="$1" want
  shift
  for want in "$@"; do
    grep -qF -- "$want" "$file" || {
      echo "::error file=${file}::missing required: ${want}"
      fail=1
    }
  done
}

# forbid <file> <substring>... — no substring may appear.
forbid() {
  local file="$1" unwanted
  shift
  for unwanted in "$@"; do
    grep -qiF -- "$unwanted" "$file" && {
      echo "::error file=${file}::must not contain: ${unwanted}"
      fail=1
    }
  done
  return 0
}

# Digest-pinned, tag optional: the FSDK shell-enabled base is an external
# image, so assert only that it resolves to a digest and let explicit bumps
# stay green. A tag may accompany the digest — Renovate's dockerfile manager
# only tracks references that carry one — but the digest is what must build.
grep -qE '^ARG FSDK_RUNNER_IMAGE=ghcr\.io/projectbluefin/lab-runner(:[^@[:space:]]+)?@sha256:[0-9a-f]{64}$' image/Containerfile || {
  echo "::error file=image/Containerfile::FSDK_RUNNER_IMAGE must be digest-pinned to ghcr.io/projectbluefin/lab-runner"
  fail=1
}
for goose_checksum_arg in GOOSE_X86_64_SHA256 GOOSE_AARCH64_SHA256; do
  goose_checksum="$(sed -n "s/^ARG ${goose_checksum_arg}=\([0-9a-f]\{64\}\)$/\1/p" image/Containerfile | head -n 1)"
  if [[ ! "$goose_checksum" =~ ^[0-9a-f]{64}$ ]]; then
    echo "::error file=image/Containerfile::${goose_checksum_arg} must default to a SHA-256 digest"
    fail=1
  fi
done

if ! python3 - <<'PY'; then
import json
from pathlib import Path

errors = []
manifest = json.loads(Path("package.json").read_text())
lock = json.loads(Path("package-lock.json").read_text())
expected_manifest = {
    "name": "review-relay-runtime",
    "private": True,
    "dependencies": {"ws": "8.21.1"},
}
if manifest != expected_manifest:
    errors.append("package.json must declare only the exact ws relay dependency")
if lock.get("lockfileVersion") != 3:
    errors.append("package-lock.json must use lockfileVersion 3")
if set(lock.get("packages", {})) != {"", "node_modules/ws"}:
    errors.append("package-lock.json must lock only ws")
root = lock.get("packages", {}).get("", {})
ws = lock.get("packages", {}).get("node_modules/ws", {})
if root.get("dependencies") != {"ws": "8.21.1"} or ws.get("version") != "8.21.1":
    errors.append("package-lock.json must lock ws at 8.21.1")
if not ws.get("resolved") or not ws.get("integrity"):
    errors.append("package-lock.json must record ws resolution and integrity")

container = Path("image/Containerfile").read_text()
try:
    fixed_layer = container.index("COPY package.json package-lock.json /opt/hive/")
    fixed_install = container.index("npm --prefix /opt/hive ci --omit=dev --ignore-scripts;")
    goose_labels = container.index("LABEL io.projectbluefin.review.goose.channel=")
    goose_refresh = container.index("ARG GOOSE_REFRESH=0")
    goose_download = container.index(
        "https://github.com/aaif-goose/goose/releases/download/${GOOSE_CHANNEL}/"
    )
except ValueError as error:
    errors.append(f"missing locked relay or Goose layer marker: {error}")
else:
    if not fixed_layer < fixed_install < goose_labels < goose_refresh < goose_download:
        errors.append("fixed tools must precede Goose labels and refresh layer")
    for tool_url in (
        "https://nodejs.org/dist/v${NODE_VERSION}/",
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/",
        "https://github.com/tmux/tmux-builds/releases/download/v${TMUX_VERSION}/",
    ):
        if container.index(tool_url) > goose_refresh:
            errors.append(f"fixed tool download must precede Goose refresh: {tool_url}")

for error in errors:
    print(f"::error::{error}")
raise SystemExit(bool(errors))
PY
  fail=1
fi

# SC2016: every argument here is a literal to grep for, never an expansion.
# shellcheck disable=SC2016
require image/Containerfile \
  'ARG GOOSE_CHANNEL=canary' \
  'ARG PI_VERSION=0.73.1' \
  'ARG PI_SHA512=' \
  'ARG CODEX_VERSION=0.147.0' \
  'ARG CODEX_X86_64_SHA256=' \
  'ARG CODEX_AARCH64_SHA256=' \
  'ARG CODEX_CODE_MODE_HOST_X86_64_SHA256=0146adfaac8363ec9fcdb5895f7624db5b2e8617a283887938b7fb97a1dd4356' \
  'ARG CODEX_CODE_MODE_HOST_AARCH64_SHA256=dfd4ff98ea4db30ed078af9c31b6f86e3da4836d0573aa87e225e5a5b54d3c7c' \
  'ARG GOOSE_X86_64_SHA256=' \
  'ARG GOOSE_AARCH64_SHA256=' \
  'io.projectbluefin.review.goose.channel="${GOOSE_CHANNEL}"' \
  'io.projectbluefin.review.goose.x86_64-unknown-linux-musl.sha256="${GOOSE_X86_64_SHA256}"' \
  'io.projectbluefin.review.goose.aarch64-unknown-linux-musl.sha256="${GOOSE_AARCH64_SHA256}"' \
  'FROM ${FSDK_RUNNER_IMAGE}' \
  'ARG GOOSE_REFRESH=0' \
  'ARG SKILLS_COMMIT=' \
  'https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz' \
  'https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_arch}.tar.gz' \
  'https://github.com/tmux/tmux-builds/releases/download/v${TMUX_VERSION}/tmux-${TMUX_VERSION}-linux-${tmux_arch}.tar.gz' \
  'https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}-unknown-linux-musl.tar.gz' \
  'https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-${codex_arch}-unknown-linux-musl.tar.gz' \
  'codex_code_mode_host_sha="${CODEX_CODE_MODE_HOST_X86_64_SHA256}";' \
  'codex_code_mode_host_sha="${CODEX_CODE_MODE_HOST_AARCH64_SHA256}";' \
  'https://github.com/aaif-goose/goose/releases/download/${GOOSE_CHANNEL}/goose-${goose_arch}-unknown-linux-musl.tar.gz' \
  'https://registry.npmjs.org/@mariozechner/pi-coding-agent/-/pi-coding-agent-${PI_VERSION}.tgz' \
  'printf '\''%s  %s\n'\'' "$PI_SHA512" "$workdir/pi.tgz" | sha512sum -c -;' \
  'npm install --global --ignore-scripts --no-audit --no-fund "$workdir/pi.tgz"' \
  'pi --version' \
  'printf '\''%s  %s\n'\'' "$goose_sha" "$workdir/goose.tar.gz" | sha256sum -c -;' \
  'RUN --mount=type=secret,id=github_token' \
  'GH_TOKEN="$(cat /run/secrets/github_token)"' \
  'gh attestation verify "$workdir/goose.tar.gz" --repo aaif-goose/goose --signer-workflow aaif-goose/goose/.github/workflows/canary.yml' \
  'COPY package.json package-lock.json /opt/hive/' \
  'COPY --chmod=0644 image/tui/requirements.lock /opt/bluefin/tui/requirements.lock' \
  'COPY --chmod=0644 image/tui/*.py /opt/bluefin/tui/' \
  'npm --prefix /opt/hive ci --omit=dev --ignore-scripts;' \
  'npm cache clean --force;' \
  'test ! -e /root/.npm;' \
  'rm -rf /opt/node/include /opt/node/share/doc;' \
  'test ! -e /opt/node/include;' \
  'test ! -e /opt/node/share/doc;' \
  'corepack --version;' \
  'codex --version' \
  'codex-code-mode-host --help >/dev/null' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/bin/contributor-agent.sh' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/bin/contributor-relay.sh' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/config/backends.conf' \
  '/usr/local/bin/goose run --help >/dev/null' \
  'image/config/goose.yaml /opt/bluefin/goose/config/config.yaml' \
  'COPY --chmod=0644 image/review-scope/REVIEW.md /opt/bluefin/review-scope/.agents/REVIEW.md' \
  'COPY --chmod=0644 image/review-scope/checks/ /opt/bluefin/review-scope/.agents/checks/' \
  'chmod 0755 /opt/bluefin /opt/bluefin/tui /opt/bluefin/harness' \
  'chmod 0755 /opt/bluefin/review-scope /opt/bluefin/review-scope/.agents /opt/bluefin/review-scope/.agents/checks' \
  'COPY --chmod=0755 image/git-hooks/ /opt/bluefin/git-hooks/' \
  'COPY --chmod=0755 image/hive-entrypoint.d/ /etc/hive/entrypoint.d/' \
  'COPY --chmod=0755 image/bin/bluefin-review /usr/local/bin/bluefin-review' \
  'tar --no-same-owner -xf "$workdir/node.tar.xz" -C /opt/node --strip-components=1;' \
  'tar -I '\''python3 -m gzip'\'' -xOf "$workdir/gh.tar.gz" --wildcards --occurrence=1 '\''*/bin/gh'\'' > /usr/local/bin/gh;' \
  'tar -I '\''python3 -m gzip'\'' -xOf "$workdir/tmux.tar.gz" --occurrence=1 tmux > /usr/local/bin/tmux;' \
  'tar -I '\''python3 -m gzip'\'' -xOf "$workdir/codex.tar.gz" --occurrence=1' \
  'tar -I '\''python3 -m gzip'\'' -xOf "$workdir/codex-code-mode-host.tar.gz" --occurrence=1' \
  'python3 -m gzip -d < "$workdir/goose.tar.gz" > "$workdir/goose.tar";' \
  'tar -xOf "$workdir/goose.tar" --occurrence=1 ./goose > /usr/local/bin/goose;' \
  'COPY image/tmux.conf /etc/tmux.conf' \
  'infocmp -x tmux-direct | grep -q' \
  'https://raw.githubusercontent.com/projectbluefin/common/${SKILLS_COMMIT}/docs/skills/index.json' \
  '--raw-base "https://raw.githubusercontent.com/projectbluefin/common/${SKILLS_COMMIT}/"' \
  '--exclude lab-testing' \
  '--out /home/dev/.agents/skills' \
  'COPY --chmod=0755 scripts/generate-skills.py /usr/local/libexec/review-generate-skills' \
  'rm -f /usr/local/libexec/review-generate-skills;' \
  'test ! -e /usr/local/libexec/review-generate-skills' \
  'ARG REVIEW_REVISION=unknown' \
  'COPY --chmod=0755 scripts/generate-sbom-manifest.py /usr/local/libexec/review-sbom-manifest' \
  'rm -f /usr/local/libexec/review-sbom-manifest;' \
  'test ! -e /usr/local/libexec/review-sbom-manifest' \
  '--out /opt/bluefin/sbom/review-components.spdx.json' \
  'COPY --chmod=0755 image/entrypoint.sh /usr/local/bin/review-entrypoint' \
  '/home/dev/Downloads' \
  'USER dev' \
  'WORKDIR /home/dev' \
  'ENTRYPOINT ["/usr/local/bin/review-entrypoint"]'

# The FSDK base ships GNU findutils and diffutils, so review must not shim
# them. Shims are how this regressed once already: the Python `find` bound `-o`
# more loosely than GNU does, so Hive's relay expression handed `-exec rm` a
# path real find leaves alone. Assert the shims stay gone, that neither tool
# may be shadowed from /usr/local/bin, and that the build still proves both
# verbatim Hive invocations against whatever the base provides.
# SC2016: every argument here is a literal to grep for, never an expansion.
# shellcheck disable=SC2016
require image/Containerfile \
  "case \"\$(command -v find)\" in /usr/local/bin/*)" \
  "case \"\$(command -v cmp)\" in /usr/local/bin/*)" \
  "find \"\$probe\" -maxdepth 1 -type d -user dev -not -name 'tmux-*' -not -name 'claude-*' -not -name 'node-*' -not -name '.' -mmin +60 -exec rm -rf {} +" \
  "find \"\$probe\" -maxdepth 1 -type f -user dev -name '*.out' -o -name '*.html' -mmin +60 -exec rm -f {} +" \
  "find \"\$probe\" -maxdepth 1 -type f -name 'c.txt' -exec rm -f {} +" \
  "cmp -s \"\$probe/cmp-left\" \"\$probe/cmp-right\"" \
  '! cmp -s "$probe/cmp-left" "$probe/cmp-right"'

# The agent-tooling delta from #75: `rg` under its own name, from a verified
# official release, plus a build-time guard that deletes the layer's reason to
# exist the moment the base ships rg. Standard command semantics stay the
# base's, so nothing may be substituted or shadowed.
# shellcheck disable=SC2016
require image/Containerfile \
  'ARG RIPGREP_VERSION=' \
  'ARG RIPGREP_X86_64_SHA256=' \
  'ARG RIPGREP_AARCH64_SHA256=' \
  'https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-${rg_arch}.tar.gz' \
  'rg_arch=x86_64-unknown-linux-musl;' \
  'rg_arch=aarch64-unknown-linux-musl;' \
  'for dir in /usr/bin /usr/sbin /bin /sbin /usr/local/bin /usr/local/sbin; do' \
  'if command -v rg >/dev/null 2>&1; then' \
  'delete this layer instead of shadowing it' \
  'for canonical in grep find cat ls; do' \
  'rg --version;'

# Linters are fsdk-containers#89, not a review layer: installing one here is
# the local workaround review told upstream it was not adding. `fd`, `bat` and
# `eza` substitute for canonical commands and stay out too. `just` and
# mikefarah `yq` v4 come from the base, so a second copy would precede it.
# shellcheck disable=SC2016
forbid image/Containerfile \
  '/usr/local/bin/shellcheck' \
  '/usr/local/bin/hadolint' \
  '/usr/local/bin/fd' \
  '/usr/local/bin/bat' \
  '/usr/local/bin/eza' \
  '/usr/local/bin/just' \
  '/usr/local/bin/yq' \
  'alias grep=' \
  'alias find=' \
  'alias cat=' \
  'alias ls='

# The probe uses `-user dev`, exactly as Hive's relay does, so it must come
# after the layer that creates that user or the predicate cannot resolve.
probe_user_layer="$(grep -n 'dev:x:1000:1000:Developer:' image/Containerfile | head -n 1 | cut -d: -f1)"
probe_layer="$(grep -n -- '-type d -user dev -not -name' image/Containerfile | head -n 1 | cut -d: -f1)"
if [[ -z "$probe_user_layer" || -z "$probe_layer" || "$probe_layer" -lt "$probe_user_layer" ]]; then
  echo "::error file=image/Containerfile::the find/cmp probe must run after the dev user is created"
  fail=1
fi

# The release archives are unpacked by the base's own GNU tar, not by
# hand-rolled Python. `python3 -c` is how these previously reimplemented
# member selection; `python3 -m gzip` is the stdlib decompressor CLI standing
# in for the gzip binary the FSDK base does not ship, and is not the same
# thing. Guard the reimplementation, allow the codec.
# shellcheck disable=SC2016
forbid image/Containerfile 'python3 -c'

# This appliance owns no lab (AGENTS.md): the lab skills bundle and its pin
# must never return.
forbid image/Containerfile \
  'LAB_SKILLS_COMMIT' \
  'projectbluefin/lab/'
for extractor in image/bin/extract-archive image/bin/find image/bin/cmp; do
  if [[ -e "$extractor" ]]; then
    echo "::error file=${extractor}::use the base's tar/find/cmp, not a hand-rolled reimplementation"
    fail=1
  fi
done

# No archive may be read before its checksum is verified. For the two-step
# form the first read is the decompressor, not tar, so that is what gets
# ordered: the invariant is checksum-before-first-read.
if ! python3 - <<'PY'; then
from pathlib import Path

container = Path("image/Containerfile").read_text()
errors = []
for name, archive in (
    ("node", "node.tar.xz"),
    ("gh", "gh.tar.gz"),
    ("tmux", "tmux.tar.gz"),
    ("codex", "codex.tar.gz"),
    ("codex code mode host", "codex-code-mode-host.tar.gz"),
    ("goose", "goose.tar.gz"),
    ("ripgrep", "ripgrep.tar.gz"),
):
    verify = container.find(f'"$workdir/{archive}" | sha256sum -c -')
    extract = container.find(f'"$workdir/{archive}" -C')
    if extract < 0:
        extract = container.find(f'-xOf "$workdir/{archive}"')
    if extract < 0:
        # Two-step form: decompress the verified archive, then select a member
        # from the plain tar. Pinned to the decompressor so an unrelated read
        # of the same archive cannot satisfy this check.
        extract = container.find(f'python3 -m gzip -d < "$workdir/{archive}"')
    if verify < 0:
        errors.append(f"{name}: no sha256sum verification of {archive}")
    elif extract < 0:
        errors.append(f"{name}: no tar extraction of {archive}")
    elif verify > extract:
        errors.append(f"{name}: {archive} is read before its checksum is verified")

for error in errors:
    print(f"::error file=image/Containerfile::{error}")
raise SystemExit(bool(errors))
PY
  fail=1
fi

require .dockerignore \
  '!package.json' \
  '!package-lock.json' \
  '!scripts/generate-skills.py' \
  '!scripts/generate-sbom-manifest.py'

require tests/image-audit.sh \
  '#### Size deltas' \
  'Compressed delta (derived - base)' \
  'Local unpacked delta (derived - base)' \
  '--verify-base-evidence' \
  'projectbluefin/fsdk-containers' \
  'must contain exactly linux/amd64 and linux/arm64 manifests' \
  '--require-github-attestation' \
  'org.opencontainers.image.base.digest' \
  'native arm64 runtime measurement is tracked by #77' \
  'required_sbom_components' \
  'check_sbom_components' \
  'fetch_spdx_predicate' \
  '/opt/bluefin/sbom/review-components.spdx.json'

forbid tests/image-audit.sh \
  'native arm64 runtime measurement is #87' \
  'slot+=" — #87"'

# Host setup and the image relay exchange the same contributor protocol, so
# their pinned Hive revisions must remain exactly aligned.
launcher_hive_pin="$(sed -n 's/^hive_commit := "\([0-9a-f]\{40\}\)"$/\1/p' justfile)"
image_hive_pin="$(sed -n 's/^ARG HIVE_COMMIT=\([0-9a-f]\{40\}\)$/\1/p' image/Containerfile)"
if [[ -z "$launcher_hive_pin" || "$launcher_hive_pin" != "$image_hive_pin" ]]; then
  echo "::error::launcher and image Hive pins must match"
  fail=1
fi

# Upstream drift visibility.
#
# The equality check above is internal consistency only: two identical but
# equally ancient pins pass it. That is exactly how the pin sat 69 commits
# behind kubestellar/hive `v2` for days while upstream added the
# `task_unavailable` message case (hive#2436) that our pinned
# contributor-relay.sh has no `case` for — so a declined assignment was logged
# as an unknown message type and the relay, whose every `ready` is
# event-driven and none timed, had no path back to asking and wedged idle.
# Nothing reported it.
#
# Warning, not failure, and deliberately so:
#   * Pin equality stays a hard error: it is fully determined by files in this
#     repository, so a red result is always actionable here.
#   * Upstream distance is a fact about someone else's commit cadence. Failing
#     on it would turn every unrelated pull request red the moment Hive merges
#     anything, which trains people to ignore this suite — the same blindness
#     that caused the incident. It is surfaced as a GitHub Actions annotation
#     so it is visible on every run without gating merges.
#   * Merge-gating a proposed pin bump is a different job with a different
#     trigger and lives in its own workflow.
#
# The whole block is best-effort: this file is a contract test that must hold
# offline and without credentials, so every network path degrades to a notice.
hive_api() {
  local endpoint="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout 30 gh api "$endpoint" "$@" 2>/dev/null
  else
    gh api "$endpoint" "$@" 2>/dev/null
  fi
}

# Blob SHAs rather than the compare endpoint's file list: compare truncates at
# 300 files, and we only care about the handful of paths we actually consume.
hive_blob_sha() {
  hive_api "repos/kubestellar/hive/contents/$1?ref=$2" --jq '.sha'
}

report_hive_drift() {
  local pin="$1" head_sha behind ahead path pin_blob head_blob
  local consumed=() changed=()

  # Consumed paths come from the Containerfile itself, so adding or dropping a
  # fetched Hive file cannot silently fall out of this check.
  # shellcheck disable=SC2016 # Literal Containerfile text, not an expansion.
  mapfile -t consumed < <(
    grep -oE 'raw\.githubusercontent\.com/kubestellar/hive/\$\{HIVE_COMMIT\}/[^"[:space:]]+' image/Containerfile |
      sed 's#.*${HIVE_COMMIT}/##' | sort -u
  )
  if [[ "${#consumed[@]}" -eq 0 ]]; then
    echo "::warning file=image/Containerfile::no pinned kubestellar/hive files found to drift-check"
    return 0
  fi

  command -v gh >/dev/null 2>&1 || {
    echo "::notice::hive drift check skipped: gh not installed"
    return 0
  }
  if [[ -z "${GH_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]] && ! gh auth status >/dev/null 2>&1; then
    echo "::notice::hive drift check skipped: no gh credentials"
    return 0
  fi

  # kubestellar/hive's default branch is v2, not main.
  head_sha="$(hive_api repos/kubestellar/hive/commits/v2 --jq '.sha')" || head_sha=""
  if [[ ! "$head_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::notice::hive drift check skipped: kubestellar/hive v2 unreachable"
    return 0
  fi

  if [[ "$head_sha" == "$pin" ]]; then
    echo "hive pin ${pin:0:12} is at kubestellar/hive v2 HEAD."
    return 0
  fi

  behind="$(hive_api "repos/kubestellar/hive/compare/${pin}...v2" --jq '.behind_by')" || behind=""
  ahead="$(hive_api "repos/kubestellar/hive/compare/${pin}...v2" --jq '.ahead_by')" || ahead=""
  [[ -n "$behind" ]] || behind="unknown"
  [[ -n "$ahead" ]] || ahead="unknown"

  # Commit distance alone overstates risk — most upstream commits touch nothing
  # we fetch — so say which consumed files actually differ.
  for path in "${consumed[@]}"; do
    pin_blob="$(hive_blob_sha "$path" "$pin")" || pin_blob=""
    head_blob="$(hive_blob_sha "$path" "$head_sha")" || head_blob=""
    if [[ -z "$pin_blob" || -z "$head_blob" ]]; then
      echo "::notice::hive drift: could not compare ${path}"
      continue
    fi
    [[ "$pin_blob" != "$head_blob" ]] && changed+=("$path")
  done

  if [[ "${#changed[@]}" -gt 0 ]]; then
    echo "::warning file=justfile::hive pin ${pin:0:12} is ${ahead} commits behind v2 (${head_sha:0:12}); consumed files changed: ${changed[*]}"
  else
    echo "::warning file=justfile::hive pin ${pin:0:12} is ${ahead} commits behind v2 (${head_sha:0:12}); no consumed file changed (${consumed[*]})"
  fi
  [[ "$behind" != "0" && "$behind" != "unknown" ]] &&
    echo "::warning file=justfile::hive pin ${pin:0:12} has ${behind} commits not on v2; it may not be an ancestor of the default branch"
  return 0
}

if [[ -n "$launcher_hive_pin" ]]; then
  report_hive_drift "$launcher_hive_pin"
fi

# No host engine sockets or unrelated runtime glue may enter the image.
forbid image/Containerfile \
  '/var/run/docker.sock' \
  '/run/podman/podman.sock' \
  'https://nodejs.org/dist/latest-v24.x/' \
  'https://github.com/aaif-goose/goose/releases/latest/download/' \
  'unknown-linux-gnu.tar.gz' \
  'https://raw.githubusercontent.com/projectbluefin/common/main/' \
  'npm --prefix /opt/hive audit' \
  'npm --prefix /opt/hive install' \
  '# renovate: datasource=github-releases depName=aaif-goose/goose' \
  'ramalama' \
  'models.json' \
  'agent-contract.json'

# shellcheck disable=SC2016 # Literal source assertion, not shell expansion.
require image/hive-entrypoint.d/hosted-knowledge.sh \
  'hosted-projectbluefin-knuckle-gjvq.hive.kubestellar.io' \
  '/api/v1/knowledge' \
  'Authorization: Bearer ${GH_TOKEN}'

# The skill generator writes into the image as root. Its source must stay
# commit-pinned and manifest-controlled path components must not escape its
# output root.
require scripts/generate-skills.py \
  'DEFAULT_COMMON_COMMIT =' \
  'SKILL_ID_PATTERN =' \
  'invalid id' \
  'invalid entry_point'
forbid scripts/generate-skills.py \
  'projectbluefin/common/main/'

# The controlled config exists only because Hive overwrites
# ~/.config/goose/config.yaml on every start. It must not pin a provider or
# model that Hive manages: the launcher passes provider and model through
# from the contributor's own account. Context7 is image-owned, not
# Hive-managed: it is the appliance's fresh-documentation seam and must stay
# configured, keyless, against the public endpoint.
require image/config/goose.yaml \
  'GOOSE_MODE: auto' \
  'GOOSE_MAX_TOOL_RESPONSE_SIZE:' \
  'context7' \
  'uri: "https://mcp.context7.com/mcp"'

# Comments stripped first, so the prose explaining a setting is never mistaken
# for the setting.
goose_config_body="$(sed 's/#.*//' image/config/goose.yaml)"
for unwanted in GOOSE_PROVIDER GOOSE_MODEL 127.0.0.1:8000 api_key; do
  case "$goose_config_body" in
  *"$unwanted"*)
    echo "::error file=image/config/goose.yaml::must not pin: ${unwanted}"
    fail=1
    ;;
  esac
done

require image/config/local-agent-policy.md \
  'Use installed global Agent Skills when their descriptions match the task' \
  'docs/skills/index.json' \
  'inspect local repository evidence first' \
  'context7' \
  'it has no package' \
  'Probe with' \
  'are not installed' \
  'gh run watch' \
  'that is an evidenced finding'
# The policy must state the image's own tooling delta, so the agent knows rg
# is there to use.
# shellcheck disable=SC2016 # Literal policy text, not shell expansion.
require image/config/local-agent-policy.md \
  'The image adds `rg` on top' \
  'keep their standard behavior'
# The policy tells the agent what the runtime lacks, so a tool the base
# actually ships must never be named as absent: that steers every task into a
# hand-rolled substitute. These are present at the pinned base digest.
# The image installs `rg`, so the policy must not send the agent to a
# substitute for it.
# shellcheck disable=SC2016 # Literal policy text, not shell expansion.
forbid image/config/local-agent-policy.md \
  '`which`, `awk`' \
  '`yq` and the PyYAML module' \
  'use `grep -r` for `rg`' \
  '`rg` and `fd`'

# GOOSE_PATH_ROOT keeps controlled policy/data/state out of Hive's runtime
# config. The pinned runtime now links its knowledge export to Goose-native
# AGENTS.md and .goosehints itself, so no filename compatibility override stays.
# shellcheck disable=SC2016 # Literal source assertions, not shell expansions.
require image/entrypoint.sh \
  'selected_backend="${AGENT_BACKEND:-goose}"' \
  'command -v codex >/dev/null 2>&1' \
  'codex --version >/dev/null 2>&1' \
  '/home/dev/.codex/auth.json' \
  'Codex backend selected but its subscription auth.json is missing.' \
  'command -v pi >/dev/null 2>&1' \
  'pi --version >/dev/null 2>&1' \
  'ANTHROPIC_API_KEY' \
  'export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0' \
  'export GOOSE_PATH_ROOT=' \
  "note() { printf 'review: %s\\n' \"\$1\" >&2; }" \
  '[ "$GOOSE_PROVIDER" != github_copilot ]' \
  'review supports GitHub Copilot only.' \
  'export GOOSE_PROVIDER=github_copilot' \
  'GOOSE_MODEL="gpt-5.6-luna"' \
  'GOOSE_THINKING_EFFORT="${GOOSE_THINKING_EFFORT:-high}"' \
  'GOOSE_DISABLE_KEYRING=1' \
  'GOOSE_MOIM_MESSAGE_FILE' \
  '/opt/bluefin/local-agent-policy.md' \
  'core.hooksPath /opt/bluefin/git-hooks' \
  'checkout.defaultRemote origin' \
  'shopt -s nullglob' \
  'validation_tools=(bats shellcheck hadolint systemd-analyze pre-commit just podman actionlint)' \
  'validation tools unavailable: ${missing_validation_tools[*]} (fsdk-containers#89)' \
  'tmux_fallback_term=xterm-256color' \
  'infocmp "${TERM:-}"' \
  'truecolor | 24bit) tmux_fallback_term=xterm-direct ;;' \
  'TERM=${TERM:-<unset>} has no terminfo; using ${tmux_fallback_term}' \
  '/usr/local/bin/contributor-agent.sh "$@" &' \
  'review_dashboard=false' \
  '${1:-}" = queue' \
  '$review_dashboard" = false' \
  'reviewing the PR queue needs no Hive' \
  'review dashboard starting (no Hive)' \
  'for hook in /etc/hive/entrypoint.d/*.sh; do' \
  'export HIVE_HUB="$hosted_hub"' \
  'api/knowledge/export' \
  '/opt/bluefin/tui/.venv/bin/python /opt/bluefin/tui/bluefin_review_tui.py "$@" <&3 &' \
  'tui_pid=$!' \
  'kill -TERM "$tui_pid"' \
  'wait "$tui_pid"' \
  'exit "$tui_status"' \
  'tmux has-session -t contributor' \
  'tmux readiness diagnostics' \
  'tmux attach-session -t contributor' \
  'trap cleanup EXIT HUP INT TERM' \
  'tmux attach-session -t contributor <&3 &' \
  'wait "$attach_pid"' \
  'shutdown_grace_deciseconds=20' \
  'kill -TERM "$agent_pid"' \
  "note 'tmux detached; the agent remains foreground in this terminal. Press Ctrl-C or close this terminal to stop it.'" \
  'wait "$agent_pid"' \
  'tmux kill-session -t contributor'
# The hub knowledge export stays a file the agent can search. Linking it to
# Goose's context-file names loaded all 417 KB of it into every subprocess
# 'goose review' starts — one per check — and checks began answering with
# prose or an empty response instead of a verdict.
forbid image/entrypoint.sh \
  'context7' \
  'mcp.context7.com' \
  'CONTEXT_FILE_NAMES' \
  'ln -sf agent.md' \
  'bluefin-review queue'
# The dashboard must never be exec'd again (#338): an exec'd Python PID 1
# reaps nothing it did not spawn, so goose tool-call grandchildren reparented
# to PID 1 accumulated as zombies all session. The shell stays PID 1, runs
# the TUI as a background job it waits on, and reaps the adoptees.
forbid image/entrypoint.sh \
  'exec /opt/bluefin/tui/.venv/bin/python'

require README.md \
  'Goose canary snapshot' \
  'not an artifact identity' \
  'GOOSE_X86_64_SHA256' \
  'GOOSE_AARCH64_SHA256' \
  "--build-arg GOOSE_REFRESH=\"\$(date +%s)\""

# shellcheck disable=SC2016 # Literal workflow text, not shell expansion.
require .github/workflows/validate.yml \
  'attestations: read' \
  'Verify pinned FSDK input provenance and native platforms' \
  'bash tests/image-audit.sh --verify-base-evidence' \
  'just --justfile justfile --list' \
  "' justfile > recipe_bodies.sh" \
  'bash tests/image-contract.sh' \
  'bash tests/hive-compatibility.sh' \
  'GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}' \
  'Resolve official Goose canary asset identities' \
  'repos/aaif-goose/goose/releases/tags/canary' \
  'goose-x86_64-unknown-linux-musl.tar.gz' \
  'goose-aarch64-unknown-linux-musl.tar.gz' \
  'GOOSE_X86_64_SHA256="${{ steps.goose.outputs.x86_64_sha256 }}"' \
  'GOOSE_AARCH64_SHA256="${{ steps.goose.outputs.aarch64_sha256 }}"' \
  '--secret id=github_token,src="$secret_file"' \
  '-f image/Containerfile -t review:test .' \
  '["/usr/local/bin/review-entrypoint"]' \
  'Import shipped TUI module set as runtime user' \
  '--entrypoint /usr/local/bin/codex' \
  '--entrypoint /usr/local/bin/codex-code-mode-host' \
  '--entrypoint /opt/bluefin/tui/.venv/bin/python' \
  "import sys; sys.path.insert(0, '/opt/bluefin/tui'); import bluefin_review_tui" \
  'bash tests/image-audit.sh --derived review:test'

# The container toolchain is podman/buildah/skopeo everywhere, including in
# CI: an image built by a different engine than the one contributors run is
# how a builder-specific defect reaches a session unseen.
forbid .github/workflows/validate.yml \
  'docker build' \
  'docker run' \
  'docker inspect' \
  'docker/build-push-action' \
  'docker/setup-buildx-action'

# shellcheck disable=SC2016 # Literal workflow text, not shell expansion.
require .github/workflows/publish-compat-image.yml \
  'attestations: write' \
  'id-token: write' \
  'IMAGE: ghcr.io/projectbluefin/review' \
  'BUILDAH_IMAGE: quay.io/buildah/stable:v1.43@sha256:' \
  'Resolve image tags and OCI metadata' \
  'tag review:smoke' \
  '["/usr/local/bin/review-entrypoint"]' \
  '--secret id=github_token,src="$secret_file"' \
  'Resolve official Goose canary asset identities' \
  'GOOSE_X86_64_SHA256="$GOOSE_X86_64_SHA256"' \
  'GOOSE_AARCH64_SHA256="$GOOSE_AARCH64_SHA256"' \
  'org.opencontainers.image.title=Bluefin review contributor' \
  'org.opencontainers.image.description=Foreground contributor runtime for projectbluefin/review.' \
  'org.opencontainers.image.base.name=${BASE_NAME}' \
  'org.opencontainers.image.base.digest=${BASE_DIGEST}' \
  'buildah manifest annotate --index' \
  'buildah manifest push' \
  '--format oci' \
  'anchore/sbom-action@' \
  'format: spdx-json' \
  'sbom-path: review-sbom-${{ matrix.arch }}.spdx.json' \
  'actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6' \
  'subject-digest: ${{ steps.publish.outputs.digest }}' \
  'push-to-registry: true' \
  '--require-attestations' \
  '--require-github-attestation' \
  '--attestation-repository "${GITHUB_REPOSITORY}"'

# Every published architecture is built by a runner of that architecture, and
# each proves its own host, engine, and container before the audit accepts any
# runtime evidence from it. The index is assembled from those two digests, so
# no shipped layer is ever produced under emulation.
# shellcheck disable=SC2016 # Literal workflow text, not shell expansion.
require .github/workflows/publish-compat-image.yml \
  'resolve-goose:' \
  'metadata:' \
  'build:' \
  'needs: [resolve-goose, metadata]' \
  '- arch: amd64' \
  'runner: ubuntu-24.04' \
  '- arch: arm64' \
  'runner: ubuntu-24.04-arm' \
  'Prove the native host and record the container toolchain' \
  'host_arch="$(uname -m)"' \
  "engine_arch=\"\$(podman info --format '{{.Host.Arch}}')\"" \
  'test "$engine_arch" = "$EXPECTED_ARCH"' \
  'podman --version' \
  'buildah --version' \
  'skopeo --version' \
  'container_arch="$(podman run --rm --entrypoint /usr/bin/uname review:smoke -m)"' \
  'podman push --format oci' \
  'staging="ghcr.io/${GITHUB_REPOSITORY,,}"' \
  'STAGING="ghcr.io/${GITHUB_REPOSITORY,,}"' \
  'name: review-digest-${{ matrix.arch }}' \
  'pattern: review-digest-*' \
  'DERIVED_IMAGE: ${{ steps.push.outputs.image }}@${{ steps.push.outputs.digest }}' \
  'cat "$RUNNER_TEMP/review-image-audit-${ARCH}.md" >> "$GITHUB_STEP_SUMMARY"' \
  'bash tests/image-audit.sh --verify-base-evidence' \
  'bash tests/image-audit.sh --derived "$DERIVED_IMAGE"' \
  '--report "$RUNNER_TEMP/review-image-audit-${ARCH}.md"' \
  "if: github.repository == 'projectbluefin/review'" \
  'needs: [build, metadata]'

# BuildKit is not the engine this project runs. Its parent-directory handling
# for 'COPY --chmod' differs from buildah's, so an image built with it can
# carry a defect no contributor can reproduce with podman.
forbid .github/workflows/publish-compat-image.yml \
  'docker/setup-qemu-action' \
  'docker/setup-buildx-action' \
  'docker/build-push-action' \
  'docker/login-action' \
  'buildx' \
  'provenance: mode=max' \
  '--privileged' \
  'docker run --privileged'

# ':-/config}' and ':-/workspace}' are mount points Hive never used.
forbid image/entrypoint.sh \
  '/var/run/docker.sock' \
  '/run/podman/podman.sock' \
  ':-/config}' \
  ':-/workspace}' \
  'tmux_term=xterm-256color'

require image/tmux.conf \
  'set -g default-terminal "tmux-direct"' \
  'set-environment -g COLORTERM "truecolor"' \
  'set -g mouse on' \
  'set -g history-limit 50000'

# This source-backed behavioral check fetches the exact pin and exercises the
# hosted hook's curl rewrite without reaching the hosted service.
require tests/hive-compatibility.sh \
  'AGENTS.md' \
  '.goosehints' \
  'GOOSE_PATH_ROOT' \
  'KNOWN_BACKENDS="claude copilot goose codex agy bob pi aider litellm"'

# Hooks run in every repository via a global core.hooksPath, so they must never
# claim to be enforcement: --no-verify bypasses all of them.
for hook in pre-commit commit-msg post-checkout; do
  head -c 2 "image/git-hooks/${hook}" | grep -q '#!' || {
    echo "::error file=image/git-hooks/${hook}::missing shebang"
    fail=1
  }
  [[ "$hook" == "post-checkout" ]] && continue
  require "image/git-hooks/${hook}" 'no-verify'
done

require image/git-hooks/post-checkout \
  'info/exclude' \
  '.agents/skills/' \
  'docs/skills/index.json'

# The dashboard imports its siblings by bare name, so shipping only the
# entrypoint module crashes `just review-queue` at startup with
# ModuleNotFoundError. py_compile never caught that: compiling a file does not
# resolve its imports. Assert the glob COPY and the import proof, and that
# nothing has gone back to naming one module.
# shellcheck disable=SC2016 # Literal Containerfile text, not an expansion.
require image/Containerfile \
  'COPY --chmod=0644 image/tui/*.py /opt/bluefin/tui/' \
  'for tui_module in /opt/bluefin/tui/*.py; do' \
  'PYTHONPATH=/opt/bluefin/tui /opt/bluefin/tui/.venv/bin/python'

forbid image/Containerfile \
  'python -m py_compile' \
  'COPY image/tui/bluefin_review_tui.py'

[[ "$fail" -eq 0 ]] && echo "✓ image contract holds."
exit "$fail"
