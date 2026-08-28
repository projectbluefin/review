#!/usr/bin/env bash
# Small drift guard for the above-the-fold README onboarding contract.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readme="$repo_root/README.md"
failures=0

require_text() {
  local text="$1"
  if ! grep -Fq -- "$text" "$readme"; then
    printf 'missing README onboarding contract: %s\n' "$text" >&2
    failures=$((failures + 1))
  fi
}

require_heading() {
  if ! grep -Eq '^## +(Start here|Quick start)([[:space:]]|$)' "$readme"; then
    printf 'missing README onboarding heading: Start here / Quick start\n' >&2
    failures=$((failures + 1))
  fi
}

require_before() {
  local earlier="$1" later="$2" earlier_line later_line
  earlier_line="$(grep -n -m1 -E "$earlier" "$readme" | cut -d: -f1 || true)"
  later_line="$(grep -n -m1 -E "$later" "$readme" | cut -d: -f1 || true)"
  if [[ -z "$earlier_line" || -z "$later_line" || "$earlier_line" -ge "$later_line" ]]; then
    printf 'README onboarding must precede %s\n' "$later" >&2
    failures=$((failures + 1))
  fi
}

require_heading
require_before '^## +(Start here|Quick start)([[:space:]]|$)' '^## +What this is for([[:space:]]|$)'

for command in review-doctor review-queue review-container review-stop; do
  require_text "just $command"
done

require_text 'BLUEFIN_REVIEW_BACKEND=codex just review-queue'
require_text 'TOOL=goose'
require_text 'TOOL=codex'
require_text 'TOOL=pi'
require_text 'REVIEW_DETACH=1'
require_text 'just review-stop'

if [[ "$failures" -ne 0 ]]; then
  printf '%d README onboarding assertion(s) failed.\n' "$failures" >&2
  exit 1
fi

printf 'README quick-start contract passed.\n'
