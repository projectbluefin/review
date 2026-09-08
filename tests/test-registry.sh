#!/usr/bin/env bash
# tests/test-registry.sh
#
# Every executable test under tests/ must be reachable from
# .github/workflows/validate.yml, either because the workflow runs it directly
# or because a script the workflow runs invokes it. Seven suites
# (capacity_contract.py, review_cache_contract.py, review_engine_contract.py,
# review_receipt_contract.py, review_snapshot_contract.py,
# lab-broker-contract.py, review-exec-broker-contract.py) were committed and
# then never executed by CI. This check makes that state fail the build.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$repo_root/.github/workflows/validate.yml"
tests_dir="$repo_root/tests"

failures=0
fail() {
  printf 'test registry: %s\n' "$1" >&2
  failures=$((failures + 1))
}

[[ -f "$workflow" ]] || {
  fail "missing workflow: $workflow"
  exit 1
}

# A test is "reachable" when the workflow names it, or when any file the
# workflow already reaches names it. One hop covers the shape this repo uses:
# validate.yml -> tests/dashboard-contract.sh -> the venv-only contracts.
declare -a roots=()
while IFS= read -r name; do
  roots+=("$name")
done < <(
  grep -oE 'tests/[A-Za-z0-9_.-]+\.(sh|py)' "$workflow" | sed 's|^tests/||' | sort -u
)

((${#roots[@]} > 0)) || fail "validate.yml names no test files at all"

is_referenced_by_roots() {
  local target="$1" root
  for root in "${roots[@]}"; do
    [[ "$root" == "$target" ]] && return 0
    [[ -f "$tests_dir/$root" ]] || continue
    grep -qF "tests/$target" "$tests_dir/$root" && return 0
  done
  return 1
}

checked=0
while IFS= read -r path; do
  name="$(basename "$path")"
  # Fixtures and shared helpers are data, not suites.
  case "$name" in
  __init__.py | conftest.py) continue ;;
  esac
  checked=$((checked + 1))
  is_referenced_by_roots "$name" ||
    fail "tests/$name is never executed by .github/workflows/validate.yml"
done < <(find "$tests_dir" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' \) | sort)

# A root that names a file which does not exist is the same defect inverted:
# CI would fail late, or the reference silently rots.
for root in "${roots[@]}"; do
  [[ -f "$tests_dir/$root" ]] ||
    fail "validate.yml runs tests/$root, which does not exist"
done

if ((failures > 0)); then
  printf 'test registry: %d problem(s) across %d test file(s)\n' \
    "$failures" "$checked" >&2
  exit 1
fi

printf 'test registry OK: %d test files, all reachable from validate.yml\n' \
  "$checked"
