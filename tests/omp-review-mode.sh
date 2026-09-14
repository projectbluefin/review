#!/usr/bin/env bash
# Contract gate for the omp review mode.
#
# The mode is the only TypeScript this repository ships, and it is loaded by omp
# from source. Nothing else validates it: there is no bundler, no tsc, and the
# extension only fails at runtime, inside a TUI, where a stack trace is a
# repainted frame. This runs the mode headlessly instead.
#
#   1. `node --test` drives the real modules against a fake omp host, a fake
#      GitHub, and a real on-disk state tree in a temp directory.
#   2. The Python harness contract covers the adapter and the package layout omp
#      needs in order to load the mode and its companion agents at all.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v node >/dev/null 2>&1; then
  echo "omp-review-mode: node is required to exercise the review mode" >&2
  exit 1
fi

# Type stripping (no transpile) is Node's default from 23.6; be explicit about
# the requirement so an older runtime fails with a sentence instead of a parse error.
node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
if ((node_major < 24)); then
  echo "omp-review-mode: node >= 24 required for TypeScript type stripping (found $(node --version))" >&2
  exit 1
fi

node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON tests/omp-review-mode.test.ts tests/blueberry_mode.test.ts tests/pr_reader.test.ts tests/ci_mode.test.ts tests/reviewer_requests.test.ts
bash tests/launcher-contract.sh

# A green `node --test` run above proves each module compiles and behaves; it
# does not prove the extension ever reaches it. That is the other half of the
# same contract, so it runs from the same gate.
bash tests/module-registry.sh
