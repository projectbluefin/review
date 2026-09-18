#!/usr/bin/env bash
# tests/module-registry.sh
#
# Every TypeScript module under image/extension/bluefin-review/ must be
# reachable from the extension entrypoint that package.json declares
# ("omp": { "extensions": ["./index.ts"] }), following relative imports
# transitively.
#
# tests/test-registry.sh gates the test side: no test file may sit in tests/
# without CI running it. This is the source-side mirror: no module may sit in
# the extension without the extension reaching it. Without it a module can be
# written, unit-tested, merged green, and never execute — the test suite then
# reports coverage for behaviour the product does not have.
#
# QUARANTINE is the escape hatch, and it is deliberately loud: a quarantined
# module is a known defect with a tracking issue, not an accepted shape.
#
# Run in CI from tests/omp-review-mode.sh, the gate .github/workflows/validate.yml
# already names for everything TypeScript in this repository.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ext_dir="$repo_root/image/extension/bluefin-review"
entrypoint="index.ts"

# Modules known to be unreachable, each with the issue that owns its
# disposition. Add an entry only alongside a tracking issue; remove it when the
# module is wired or deleted. Empty is the intended steady state.
declare -A QUARANTINE=()

failures=0
fail() {
  printf 'module registry: %s\n' "$1" >&2
  failures=$((failures + 1))
}

[[ -d "$ext_dir" ]] || {
  fail "missing extension directory: $ext_dir"
  exit 1
}

[[ -f "$ext_dir/$entrypoint" ]] || {
  fail "missing entrypoint: $ext_dir/$entrypoint"
  exit 1
}

# package.json must actually name the entrypoint this gate walks from,
# otherwise the gate proves reachability from a file nobody loads.
grep -Fq "./$entrypoint" "$ext_dir/package.json" ||
  fail "package.json does not declare ./$entrypoint as an OMP extension"

# Transitive closure over relative imports, breadth-first.
declare -A reached=()
queue=("$entrypoint")
reached["$entrypoint"]=1

while ((${#queue[@]} > 0)); do
  current="${queue[0]}"
  queue=("${queue[@]:1}")
  [[ -f "$ext_dir/$current" ]] || continue

  while IFS= read -r spec; do
    target="${spec#./}"
    [[ -n "$target" ]] || continue
    [[ -f "$ext_dir/$target" ]] || continue
    [[ -n "${reached[$target]:-}" ]] && continue
    reached["$target"]=1
    queue+=("$target")
  done < <(
    grep -oE 'from[[:space:]]+"\./[A-Za-z0-9_./-]+"' "$ext_dir/$current" |
      sed -E 's|.*"(\./[A-Za-z0-9_./-]+)"|\1|' | sort -u
  )
done

checked=0
while IFS= read -r path; do
  name="${path#"$ext_dir"/}"
  checked=$((checked + 1))
  [[ -n "${reached[$name]:-}" ]] && continue

  if [[ -n "${QUARANTINE[$name]:-}" ]]; then
    printf 'module registry: QUARANTINED %s (%s)\n' "$name" "${QUARANTINE[$name]}" >&2
    continue
  fi

  fail "$name is never imported, directly or transitively, from $entrypoint"
done < <(find "$ext_dir" -type f -name '*.ts' | sort)

# A quarantine entry for a module that is now reachable, or now gone, is stale
# bookkeeping that would hide the next real orphan.
for name in "${!QUARANTINE[@]}"; do
  if [[ ! -f "$ext_dir/$name" ]]; then
    fail "quarantine names $name, which does not exist — drop the entry"
  elif [[ -n "${reached[$name]:-}" ]]; then
    fail "quarantine names $name, which is now reachable — drop the entry"
  fi
done

if ((failures > 0)); then
  printf 'module registry: %d problem(s) across %d module(s)\n' \
    "$failures" "$checked" >&2
  exit 1
fi

printf 'module registry OK: %d modules, %d reachable from %s, %d quarantined\n' \
  "$checked" "${#reached[@]}" "$entrypoint" "${#QUARANTINE[@]}"
