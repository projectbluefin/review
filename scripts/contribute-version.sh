#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
containerfile="${repo_root}/image/contribute/Containerfile"
revision_file="${repo_root}/image/contribute/REVISION"
base_ref="$(sed -nE 's/^ARG FSDK_BASE_IMAGE=(.*)$/\1/p' "$containerfile" | head -1)"
base_tag="${base_ref#*base:}"
base_tag="${base_tag%%@*}"
[[ "$base_tag" =~ ^([0-9]{2}\.[0-9]{2}) ]] || {
  echo "contribute-version: invalid base tag" >&2
  exit 1
}
revision="$(tr -d '[:space:]' <"$revision_file")"
[[ "$revision" =~ ^[0-9]+$ ]] || {
  echo "contribute-version: invalid revision" >&2
  exit 1
}
printf '%s.%02d\n' "${BASH_REMATCH[1]}" "$((10#$revision))"
