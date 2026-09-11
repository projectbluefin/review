#!/usr/bin/env bash
# Validate every compatibility conclusion against the exact Hive revision the
# launcher and image use. This is intentionally source-backed: these seams are
# upstream protocol behavior, not local reimplementations.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

pin="$(sed -n 's/^ARG HIVE_COMMIT=\([0-9a-f]\{40\}\)$/\1/p' image/Containerfile)"
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] || {
  echo "::error file=image/Containerfile::HIVE_COMMIT must be a full SHA" >&2
  exit 1
}

hive_source() {
  curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/hivecommons/hive/${pin}/$1"
}

agent="$(hive_source bin/contributor-agent.sh)"
backends="$(hive_source config/backends.conf)"

# Hive now exposes its refreshed knowledge through the filenames Goose reads
# natively, so a downstream CONTEXT_FILE_NAMES extension would be redundant.
# shellcheck disable=SC2016 # Exact pinned-source fragments, not shell syntax.
for link in \
  'ln -sf "$agent_md" "${HOME}/AGENTS.md"' \
  'ln -sf "$agent_md" "${HOME}/.goosehints"'; do
  grep -qF "$link" <<<"$agent" || {
    echo "::error::pinned Hive no longer creates Goose-native knowledge link: $link" >&2
    exit 1
  }
done
# shellcheck disable=SC2016 # Exact pinned-source fragment, not shell syntax.
grep -qF 'if [ ! -f "${HOME}/.config/goose/config.yaml" ]; then' <<<"$agent" || {
  echo "::error::pinned Hive no longer preserves an existing Goose config" >&2
  exit 1
}

# contributor-agent.sh sources this full upstream interface before detection;
# shrinking it locally would make review own Hive backend behavior.
grep -qF 'source /usr/local/etc/hive/backends.conf' <<<"$agent" || {
  echo "::error::pinned Hive no longer consumes the installed backends.conf" >&2
  exit 1
}
# Assert membership, not exact equality: upstream may append backends (muse,
# omp, ...) between pins, and one literal cannot track two revisions. The
# guard that matters is the interface shrinking below what review relies on.
for backend in claude copilot goose codex agy bob pi aider litellm opencode kilo; do
  grep -q "KNOWN_BACKENDS=\"[^\"]*\b${backend}\b" <<<"$backends" || {
    echo "::error::pinned Hive backend interface changed (missing: $backend)" >&2
    exit 1
  }
done

# Exercise the hook with an inert command after it has installed its wrapper.
# The exact hosted URL is rewritten and receives a Bearer token; unrelated
# curl calls retain their original arguments.
#
# Every nested shell here runs --noprofile --norc. Bash sources the invoking
# user's rc when it believes it was started by a remote shell — stdin being a
# socket is enough — and this repository's own maintainers export HIVE_HUB from
# ~/.bashrc. That silently replaced the hub these assertions set, and the suite
# failed claiming the hook had overwritten the launcher's selection.
hook_output="$(
  HIVE_HUB='wss://hosted-projectbluefin-knuckle-gjvq.hive.hivecommons.dev/contribute' \
    GH_TOKEN='compatibility-test-token' \
    bash --noprofile --norc -c '
      source image/hive-entrypoint.d/hosted-knowledge.sh
      curl_binary=/bin/echo
      curl -sf "https://hosted-projectbluefin-knuckle-gjvq.hive.hivecommons.dev/api/knowledge/export" -o /dev/null
    '
)"
[[ "$hook_output" == *'--header Authorization: Bearer compatibility-test-token'* ]] &&
  [[ "$hook_output" == *'/api/v1/knowledge'* ]] ||
  {
    echo "::error::hosted knowledge hook did not authenticate and rewrite the stock export" >&2
    exit 1
  }

if HIVE_HUB='wss://other.hive.example/contribute' GH_TOKEN='compatibility-test-token' \
  bash --noprofile --norc -c 'source image/hive-entrypoint.d/hosted-knowledge.sh; declare -F curl' |
  grep -q .; then
  echo "::error::hosted knowledge hook must not intercept other Hive deployments" >&2
  exit 1
fi

selected_hub="$(
  HIVE_HUB='wss://other.hive.example/contribute' GH_TOKEN='compatibility-test-token' \
    bash --noprofile --norc -c 'source image/hive-entrypoint.d/hosted-knowledge.sh; printf "%s\n" "$HIVE_HUB"'
)"
if [[ "$selected_hub" != 'wss://other.hive.example/contribute' ]]; then
  echo "::error::hosted knowledge hook overwrote the launcher-selected Hive" >&2
  exit 1
fi

unset_hub="$(
  # shellcheck disable=SC2016 # single quotes are intentional for bash -c script
  env -u HIVE_HUB GH_TOKEN='compatibility-test-token' \
    bash --noprofile --norc -c 'source image/hive-entrypoint.d/hosted-knowledge.sh; printf "%s\n" "${HIVE_HUB:-}"'
)"
if [[ -n "$unset_hub" ]]; then
  echo "::error::hosted knowledge hook silently selected a Hive for queue mode" >&2
  exit 1
fi

grep -qF 'export GOOSE_PATH_ROOT=' image/entrypoint.sh
if grep -qF 'CONTEXT_FILE_NAMES' image/entrypoint.sh; then
  echo "::error::entrypoint retains an obsolete context filename override" >&2
  exit 1
fi

echo "✓ pinned Hive compatibility seams hold."
