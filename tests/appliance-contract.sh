#!/usr/bin/env bash
# Contract for the Bluefin Review appliance image.
#
# Two halves. The static half reads the Containerfile and the version machinery
# and needs no container engine, so it runs on every commit. The runtime half
# runs the built image and needs one; CI always passes --image, and locally it
# is skipped unless you pass one too.
#
#   bash tests/appliance-contract.sh
#   bash tests/appliance-contract.sh --image localhost/projectbluefin/review:dev
#
# The runtime half exists because this image is assembled rather than installed:
# a missing shared library or a pruned file does not fail the build, it fails in
# a maintainer's terminal three days later.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

containerfile="image/appliance/Containerfile"
image=""
expect_arch=""
expect_version=""
# An image that has grown past this is carrying something nobody chose: the
# first draft shipped 284 MiB of esbuild binaries for platforms it cannot run.
size_ceiling_bytes=$((700 * 1024 * 1024))

while (($#)); do
  case "$1" in
  --image)
    image="${2:?--image needs a reference}"
    shift 2
    ;;
  --expect-arch)
    expect_arch="${2:?--expect-arch needs a value}"
    shift 2
    ;;
  --expect-version)
    expect_version="${2:?--expect-version needs a value}"
    shift 2
    ;;
  *)
    echo "appliance-contract: unknown argument $1" >&2
    exit 2
    ;;
  esac
done

fail() {
  echo "appliance-contract: $*" >&2
  exit 1
}

require() {
  local path="$1"
  shift
  local needle
  for needle in "$@"; do
    grep -qF -- "$needle" "$path" || fail "${path} must contain: ${needle}"
  done
}

forbid() {
  local path="$1"
  shift
  local needle
  for needle in "$@"; do
    grep -qF -- "$needle" "$path" && fail "${path} must not contain: ${needle}"
  done
  return 0
}

# ------------------------------------------------------------------- static

# Both FSDK images are pinned as tag *and* digest: the digest is what builds,
# the tag is what Renovate can compare against.
grep -qE '^ARG FSDK_BASE_IMAGE=ghcr\.io/projectbluefin/base:[^@[:space:]]+@sha256:[0-9a-f]{64}$' "$containerfile" ||
  fail "FSDK_BASE_IMAGE must be pinned as tag@sha256 digest"
grep -qE '^ARG FSDK_BUILDER_IMAGE=ghcr\.io/projectbluefin/lab-runner:[^@[:space:]]+@sha256:[0-9a-f]{64}$' "$containerfile" ||
  fail "FSDK_BUILDER_IMAGE must be pinned as tag@sha256 digest"

# Every fetched artifact carries a per-architecture digest. A download this
# build cannot verify is a download it must not execute.
for pin in OMP_X86_64_SHA256 OMP_AARCH64_SHA256 NODE_X86_64_SHA256 NODE_AARCH64_SHA256 \
  GH_X86_64_SHA256 GH_AARCH64_SHA256; do
  grep -qE "^ARG ${pin}=[0-9a-f]{64}$" "$containerfile" ||
    fail "ARG ${pin} must be a lowercase sha256 digest"
done

# shellcheck disable=SC2016 # Literal Containerfile text, not shell expansions.
require "$containerfile" \
  'FROM ${FSDK_BUILDER_IMAGE} AS build' \
  'FROM ${FSDK_BASE_IMAGE}' \
  'sha256sum --check --status' \
  'USER 65532:65532' \
  'WORKDIR /workspace' \
  'ENTRYPOINT ["/usr/bin/omp", "--profile", "review", "--extension", "/usr/share/bluefin/review/extension"]' \
  'io.projectbluefin.review.appliance="true"' \
  'org.opencontainers.image.version="${REVIEW_VERSION}"' \
  'org.opencontainers.image.revision="${REVIEW_REVISION}"'

# The point of a distroless appliance is that nothing inside it can install
# anything. Not one of these may appear, in any stage that reaches the image.
forbid "$containerfile" \
  'dnf install' \
  'apt-get' \
  'apk add' \
  'pip install' \
  'RUN curl | ' \
  'curl -sL |'

# `SHELL` is silently ignored for the OCI image format; a RUN that relies on it
# for `set -e` is a RUN whose failures are invisible.
grep -qE '^SHELL ' "$containerfile" && fail "SHELL is ignored under --format oci; set options inside each RUN"

version="$(bash scripts/review-appliance-version.sh)"
[[ "$version" =~ ^[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]] ||
  fail "version must be <fsdk-series>.<MM>, got '${version}'"

# The FSDK series is read from the pinned base, never written down twice.
base_series="$(sed -nE 's/^ARG FSDK_BASE_IMAGE=.*base:([0-9]{2}\.[0-9]{2}).*/\1/p' "$containerfile")"
[[ "$version" == "${base_series}."* ]] ||
  fail "version '${version}' does not track the pinned FSDK series '${base_series}'"

[[ -f image/appliance/REVISION ]] || fail "image/appliance/REVISION is missing"
grep -qE '^[0-9]+$' image/appliance/REVISION || fail "image/appliance/REVISION must hold a single integer"

# The mode the image exists to run has to be in the build context.
[[ -f image/extension/bluefin-review/index.ts ]] || fail "the review mode entry point is missing"
[[ -d image/extension/bluefin-review/agents ]] || fail "the companion agents are missing"
grep -qF '!scripts/generate-appliance-sbom.py' .dockerignore ||
  fail ".dockerignore must let the appliance SBOM generator into the build context"

# The generator that fills that SBOM. Its own contract runs here rather than as
# a separate validate.yml step: the document it writes is part of this image's
# contract, and the runtime half below only reads it when --image is given.
python3_contract_output="$(python3 tests/appliance_sbom_contract.py 2>&1)" || {
  printf '%s\n' "$python3_contract_output" >&2
  fail "tests/appliance_sbom_contract.py failed"
}

require .github/workflows/publish-appliance.yml \
  'scripts/review-appliance-version.sh' \
  'tests/appliance-contract.sh' \
  'IMAGE: ghcr.io/projectbluefin/review'

if [[ -n "$expect_version" && "$expect_version" != "$version" ]]; then
  fail "expected version ${expect_version}, derived ${version}"
fi

echo "appliance-contract: static contract holds (version ${version})"

# ------------------------------------------------------------------ runtime

if [[ -z "$image" ]]; then
  echo "appliance-contract: no --image given; runtime checks skipped"
  exit 0
fi

engine="${CONTAINER_ENGINE:-podman}"
command -v "$engine" >/dev/null 2>&1 || fail "${engine} is required for runtime checks"

inspect() {
  "$engine" image inspect "$image" --format "$1"
}

# The body runs in the container's shell, so its expansions must survive this
# one untouched. Every caller below quotes accordingly.
# shellcheck disable=SC2016
run() {
  "$engine" run --rm --entrypoint /usr/bin/bash "$image" -c "$1"
}

test "$(inspect '{{.Config.User}}')" = "65532:65532" ||
  fail "image must run as the numeric nonroot uid; kubelet rejects named users under runAsNonRoot"
test "$(inspect '{{.Config.WorkingDir}}')" = "/workspace"
test "$(inspect '{{json .Config.Entrypoint}}')" = \
  '["/usr/bin/omp","--profile","review","--extension","/usr/share/bluefin/review/extension"]'
test "$(inspect '{{.ManifestType}}')" = "application/vnd.oci.image.manifest.v1+json" ||
  fail "the shipped format is OCI, not Docker schema 2"

image_version="$(inspect '{{index .Labels "org.opencontainers.image.version"}}')"
test "$image_version" = "$version" ||
  fail "image label version '${image_version}' does not match derived '${version}'"
test "$(inspect '{{index .Labels "io.projectbluefin.review.appliance"}}')" = "true"

# Sum the layer sizes rather than reading `.Size`: podman's inspect field
# double-counts files a later layer replaces, and the number a maintainer sees
# in `podman images` is the sum of the diffs.
size="$("$engine" history --format json "$image" | python3 -c '
import json, sys
print(sum(int(layer.get("size") or 0) for layer in json.load(sys.stdin)))
')"
[[ "$size" =~ ^[0-9]+$ ]] || fail "could not measure the image size"
if ((size > size_ceiling_bytes)); then
  fail "image is $((size / 1024 / 1024)) MiB, over the $((size_ceiling_bytes / 1024 / 1024)) MiB ceiling"
fi

if [[ -n "$expect_arch" ]]; then
  container_arch="$("$engine" run --rm --entrypoint /usr/bin/uname "$image" -m)"
  test "$container_arch" = "$expect_arch" ||
    fail "expected a native ${expect_arch} container, got ${container_arch}"
fi

# Every bundled binary is executed, because "the file exists" says nothing about
# whether its library closure came along.
# omp reports itself as "omp/<version>"; the label carries the bare version.
omp_label="$(inspect '{{index .Labels "io.projectbluefin.review.omp.version"}}')"
omp_version="$(run 'omp --version')"
test "$omp_version" = "omp/${omp_label}" ||
  fail "the omp binary reports '${omp_version}', but this image claims to ship ${omp_label}"
pi_version="$(run 'pi --version')"
test "$pi_version" = "$(inspect '{{index .Labels "io.projectbluefin.review.pi.version"}}')" ||
  fail "the pi CLI does not match the version this image claims to ship"

# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  node --version >/dev/null
  gh --version >/dev/null
  git --version >/dev/null
  test "$(readlink -f /bin/sh)" = /usr/bin/bash
' >/dev/null || fail "a bundled binary failed to execute"

# git is here to land fixes, which means it has to be able to commit and to
# reach GitHub over https — the remote helper and its TLS closure included.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  export HOME=/tmp/gitcheck GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com
  export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
  mkdir -p "$HOME/repo" && cd "$HOME/repo"
  git init -q .
  echo hello > file
  git add file
  git commit -qm "smoke"
  git log --oneline | grep -q smoke
  test -x /usr/libexec/git-core/git-remote-https
  ldd /usr/libexec/git-core/git-remote-https | grep -q "not found" && exit 1
  exit 0
' >/dev/null || fail "git cannot commit, or its https remote helper is missing libraries"

# The agent writes sessions, logs and caches under HOME on every run.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run 'set -eu; test -w "$HOME"; test "$HOME" = /home/bluefin' >/dev/null ||
  fail "HOME must exist and be writable by the nonroot user"

# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  test -f /usr/share/bluefin/review/extension/index.ts
  test -d /usr/share/bluefin/review/extension/agents
  test -f /usr/share/bluefin/review/sbom.spdx.json
' >/dev/null || fail "the review mode or its SBOM is missing from the image"

# Nothing inside may install anything.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  for forbidden in dnf apt apt-get apk rpm yum pip pip3 npm; do
    if command -v "$forbidden" >/dev/null 2>&1; then
      echo "found package manager: $forbidden" >&2
      exit 1
    fi
  done
' >/dev/null || fail "a package manager reached the appliance"

sbom_packages="$(run 'cat /usr/share/bluefin/review/sbom.spdx.json' | python3 -c '
import json,sys
document = json.load(sys.stdin)
print(" ".join(sorted(package["name"] for package in document["packages"])))
')"
for component in omp gh node bluefin-review-mode "@earendil-works/pi-coding-agent"; do
  grep -qF -- "$component" <<<"$sbom_packages" ||
    fail "the in-image SBOM does not record ${component}"
done

# The deliverable itself: the shipped entrypoint, with its profile and extension
# arguments, starting under the nonroot user. Extension *behavior* is covered by
# tests/omp-review-mode.sh; what this proves is that the image's own command line
# is well formed and the binary runs as shipped.
entry_version="$("$engine" run --rm "$image" --version)"
test "$entry_version" = "$omp_label" ||
  fail "the entrypoint printed '${entry_version}', expected ${omp_label}"

echo "appliance-contract: runtime contract holds ($((size / 1024 / 1024)) MiB, omp ${omp_version}, pi ${pi_version})"
