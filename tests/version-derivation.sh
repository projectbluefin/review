#!/usr/bin/env bash
# Contract for the two publish-gating version derivation scripts:
#   scripts/review-appliance-version.sh  (image/appliance/{Containerfile,REVISION})
#   scripts/contribute-version.sh        (image/contribute/{Containerfile,REVISION})
#
# Both scripts decide the tag that publish-appliance.yml and publish-contribute.yml
# push. Until now they were only ever executed on the real tree, so every failure
# branch and the zero-padding rule were unverified: a malformed REVISION or a
# rebased base tag could only be caught by a bad publish.
#
# Each case runs the real script against a synthetic repo root, so the scripts'
# own "$(dirname "$BASH_SOURCE")/.." resolution is exercised rather than mocked.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

failures=0
fail() {
  printf 'version-derivation: %s\n' "$1" >&2
  failures=$((failures + 1))
}

assert_eq() {
  local actual="$1" expected="$2" label="$3"
  [[ "$actual" == "$expected" ]] ||
    fail "${label}: expected '${expected}', got '${actual}'"
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Deterministic, collision-free scratch roots; each case gets its own tree.
case_seq=0
new_root() {
  case_seq=$((case_seq + 1))
  local root="${work}/case-${case_seq}"
  mkdir -p "$root"
  printf '%s\n' "$root"
}

# Build a throwaway checkout containing only what a version script reads.
#   $1 script basename, $2 image dir name, $3 ARG line body, $4 REVISION body
stage() {
  local script="$1" image="$2" arg_line="$3" revision="$4"
  local root
  root="$(new_root)"
  mkdir -p "${root}/scripts" "${root}/image/${image}"
  cp "${repo_root}/scripts/${script}" "${root}/scripts/${script}"
  printf '%s\n' "$arg_line" >"${root}/image/${image}/Containerfile"
  printf '%s' "$revision" >"${root}/image/${image}/REVISION"
  printf '%s\n' "$root"
}

# Run a staged script, capturing stdout, stderr and status without tripping set -e.
run_case() {
  local root="$1" script="$2"
  set +e
  out="$(bash "${root}/scripts/${script}" 2>"${root}/stderr")"
  status=$?
  set -e
  err="$(cat "${root}/stderr")"
}

# --- 1. Version format: series from the base tag, zero-padded tool revision ---
#
# The pinned base is ghcr.io/<ns>/base:<series>@sha256:<digest>. Only the first
# two dotted pairs are the FSDK series; the digest must never leak into the tag.

for script_spec in \
  "review-appliance-version.sh appliance" \
  "contribute-version.sh contribute"; do
  read -r script image <<<"$script_spec"

  format_cases=(
    # ARG line body | REVISION file body | expected stdout | label
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|7|26.08.07|single-digit revision is zero-padded"
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|12|26.08.12|two-digit revision passes through"
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|0|26.08.00|revision zero is a valid first revision"
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|08|26.08.08|leading-zero revision is decimal, not octal"
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|09|26.08.09|revision 09 is decimal, not octal"
    "ghcr.io/projectbluefin/base:26.08|3|26.08.03|digestless base tag still yields a series"
    "ghcr.io/projectbluefin/base:26.08.1@sha256:abc123|3|26.08.03|patched base tag contributes only its series"
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|  4 |26.08.04|surrounding whitespace in REVISION is stripped"
    # Characterization, not an endorsement: REVISION is squeezed with
    # 'tr -d [:space:]', so interior whitespace is deleted rather than
    # rejected and '3 4' silently publishes revision 34. Recorded here so the
    # behaviour is visible; see projectbluefin/review#574 before relying on it.
    "ghcr.io/projectbluefin/base:26.08@sha256:abc123|3 4|26.08.34|interior whitespace in REVISION is deleted, not rejected"
    "registry.example.test:5000/ns/base:26.08@sha256:abc|5|26.08.05|registry port does not break tag extraction"
  )

  for case in "${format_cases[@]}"; do
    IFS='|' read -r arg_body revision expected label <<<"$case"
    root="$(stage "$script" "$image" "ARG FSDK_BASE_IMAGE=${arg_body}" "$revision")"
    run_case "$root" "$script"
    if ((status != 0)); then
      fail "${script}: ${label}: exited ${status} (stderr: ${err})"
      continue
    fi
    assert_eq "$out" "$expected" "${script}: ${label}"
  done

  # --- 2. Failure branches must exit non-zero and explain themselves ----------
  #
  # These are the states that would otherwise publish a wrong or empty tag.

  reject_cases=(
    "FROM scratch|7|no ARG FSDK_BASE_IMAGE line at all"
    "ARG FSDK_BASE_IMAGE=|7|empty base reference"
    "ARG FSDK_BASE_IMAGE=ghcr.io/ns/base:latest|7|non-numeric base tag"
    "ARG FSDK_BASE_IMAGE=ghcr.io/ns/base:26@sha256:abc|7|truncated series (no minor)"
    "ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:26.08@sha256:abc|v3|non-integer revision"
    "ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:26.08@sha256:abc||empty revision file"
    "ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:26.08@sha256:abc|-1|negative revision"
  )

  for case in "${reject_cases[@]}"; do
    IFS='|' read -r arg_line revision label <<<"$case"
    root="$(stage "$script" "$image" "$arg_line" "$revision")"
    run_case "$root" "$script"
    if ((status == 0)); then
      fail "${script}: ${label}: accepted, printed '${out}'"
      continue
    fi
    [[ -n "$err" ]] ||
      fail "${script}: ${label}: rejected without a diagnostic on stderr"
    [[ -z "$out" ]] ||
      fail "${script}: ${label}: rejected but still printed '${out}' on stdout"
  done

  # A missing REVISION file is a checkout defect, not a publishable version.
  root="$(stage "$script" "$image" \
    "ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:26.08@sha256:abc" "7")"
  rm -f "${root}/image/${image}/REVISION"
  run_case "$root" "$script"
  ((status != 0)) ||
    fail "${script}: missing REVISION file: accepted, printed '${out}'"

  # --- 3. Only the first ARG wins, so a later build stage cannot redefine it --
  root="$(new_root)"
  mkdir -p "${root}/scripts" "${root}/image/${image}"
  cp "${repo_root}/scripts/${script}" "${root}/scripts/${script}"
  {
    printf 'ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:26.08@sha256:abc\n'
    # shellcheck disable=SC2016 # literal Containerfile text, not a shell expansion
    printf 'FROM ${FSDK_BASE_IMAGE}\n'
    printf 'ARG FSDK_BASE_IMAGE=ghcr.io/projectbluefin/base:99.99@sha256:def\n'
  } >"${root}/image/${image}/Containerfile"
  printf '7' >"${root}/image/${image}/REVISION"
  run_case "$root" "$script"
  assert_eq "$out" "26.08.07" "${script}: first ARG default wins over a later restatement"
done

# --- 4. The real tree derives a well-formed version for both images ----------
#
# Guards against a committed Containerfile or REVISION that the scripts reject.

for script in review-appliance-version.sh contribute-version.sh; do
  set +e
  real_out="$(bash "${repo_root}/scripts/${script}" 2>&1)"
  real_status=$?
  set -e
  if ((real_status != 0)); then
    fail "${script}: fails on the committed tree: ${real_out}"
    continue
  fi
  [[ "$real_out" =~ ^[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]] ||
    fail "${script}: committed tree yields malformed version '${real_out}'"
done

# --- 5. Both images derive their series from the same pinned FSDK base -------
#
# The two Containerfiles pin the base independently; a silent divergence would
# ship a contribute image and an appliance claiming different FSDK series.
appliance_series="$(bash "${repo_root}/scripts/review-appliance-version.sh" | cut -d. -f1,2)"
contribute_series="$(bash "${repo_root}/scripts/contribute-version.sh" | cut -d. -f1,2)"
assert_eq "$contribute_series" "$appliance_series" \
  "appliance and contribute images agree on the FSDK series"

if ((failures > 0)); then
  printf 'version-derivation: %d check(s) failed\n' "$failures" >&2
  exit 1
fi

echo "version-derivation: OK"
