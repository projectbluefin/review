#!/usr/bin/env bash
# Contract for scripts/check-skill-frontmatter.sh.
#
# The script is a required validate.yml gate and a pre-commit hook: it is the
# only thing asserting that docs/skills/*.md front-matter is well formed and
# that docs/skills/index.json is the generated projection of it. Nothing
# exercised it, so its hand-rolled front-matter parser, its per-field rules,
# and its manifest freshness comparison could all regress without CI noticing
# — and a stale or wrong index.json is what skill consumers read.
#
# Every case runs the script against a synthetic repository (a copy of the
# script plus a docs/ tree) so the assertions describe the rules rather than
# the current contents of this repo's own skill documents.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="$repo_root/scripts/check-skill-frontmatter.sh"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

failures=0
case_count=0
fail() {
  printf 'check-skill-frontmatter contract: %s\n' "$1" >&2
  failures=$((failures + 1))
}

# Build a synthetic repo root containing a copy of the script under test.
new_repo() {
  case_count=$((case_count + 1))
  fake="$tmpdir/case-$case_count"
  mkdir -p "$fake/scripts" "$fake/docs/skills"
  cp "$script" "$fake/scripts/check-skill-frontmatter.sh"
  printf '# Skill router\n' >"$fake/docs/SKILL.md"
}

# Write a skill document. Extra front-matter lines may be appended by the
# caller through $extra; $body_lines pads the document to a given length.
write_skill() {
  local stem="$1" status="${2:-active}" category="${3:-ci-ops}"
  local tags="${4:-[alpha, beta, gamma]}"
  local description="${5:-A synthetic skill for the contract test.}"
  local last_updated="${6:-2026-01-02}"
  cat >"$fake/docs/skills/$stem.md" <<EOF
---
name: $stem
version: 1.0
last_updated: $last_updated
id: $stem
one_line_purpose: Purpose of $stem.
entry_point: docs/skills/$stem.md
category: $category
status: $status
tags: $tags
description: "$description"
metadata:
  type: skill
---

# ${stem}
EOF
}

run_check() {
  local status=0
  (cd "$fake" && bash scripts/check-skill-frontmatter.sh "$@") \
    >"$fake/stdout" 2>"$fake/stderr" || status=$?
  return "$status"
}

expect_ok() {
  local label="$1"
  shift
  if run_check "$@"; then
    return 0
  fi
  fail "$label: expected success, got a failure"
  sed 's/^/    /' "$fake/stdout" "$fake/stderr" >&2
}

expect_fail() {
  local label="$1" needle="$2"
  shift 2
  if run_check "$@"; then
    fail "$label: expected a failure, the check passed"
    return 0
  fi
  grep -Fq "$needle" "$fake/stdout" "$fake/stderr" ||
    fail "$label: failure did not mention '$needle'"
}

# --- a valid tree with a freshly generated manifest passes -------------------

new_repo
write_skill alpha-skill
expect_ok 'valid tree, --write' --write
grep -Fq 'wrote docs/skills/index.json (1 skills)' "$fake/stdout" ||
  fail '--write: did not report the manifest it wrote'
expect_ok 'valid tree, committed manifest fresh'
grep -Fq '0 error(s), 0 warning(s)' "$fake/stdout" ||
  fail 'valid tree: expected a clean summary line'

# --- the manifest is the generated projection of the front-matter -----------

python3 - "$fake/docs/skills/index.json" <<'PY' || failures=$((failures + 1))
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    raw = handle.read()

if not raw.endswith("\n"):
    print("manifest does not end in a newline", file=sys.stderr)
    sys.exit(1)

manifest = json.loads(raw)
if manifest["schema_version"] != "1.0":
    print("schema_version drifted from 1.0", file=sys.stderr)
    sys.exit(1)
if manifest["generated_at"] != "2026-01-02":
    print("generated_at should track the newest last_updated", file=sys.stderr)
    sys.exit(1)

entry = manifest["skills"][0]
expected = {
    "id": "alpha-skill",
    "name": "alpha-skill",
    "one_line_purpose": "Purpose of alpha-skill.",
    "entry_point": "docs/skills/alpha-skill.md",
    "category": "ci-ops",
    "status": "active",
    "tags": ["alpha", "beta", "gamma"],
    "description": "A synthetic skill for the contract test.",
    "version": "1.0",
    "last_updated": "2026-01-02",
    "doc_type": "skill",
}
if entry != expected:
    print("manifest entry drifted: %r" % (entry,), file=sys.stderr)
    sys.exit(1)

# Field order is part of the byte-stable output the freshness check compares.
if list(entry) != list(expected):
    print("manifest field order drifted: %r" % (list(entry),), file=sys.stderr)
    sys.exit(1)
PY

# --- entries are sorted, generated_at tracks the newest last_updated ---------

new_repo
write_skill zulu-skill active ci-ops '[alpha, beta, gamma]' 'Zulu.' 2026-03-04
write_skill alpha-skill active meta '[alpha, beta, gamma]' 'Alpha.' 2026-01-02
expect_ok 'multi-skill tree, --write' --write
python3 - "$fake/docs/skills/index.json" <<'PY' || failures=$((failures + 1))
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    manifest = json.load(handle)

ids = [skill["id"] for skill in manifest["skills"]]
if ids != sorted(ids):
    print("skills are not sorted by id: %r" % (ids,), file=sys.stderr)
    sys.exit(1)
if manifest["generated_at"] != "2026-03-04":
    print(
        "generated_at %r is not the newest last_updated"
        % (manifest["generated_at"],),
        file=sys.stderr,
    )
    sys.exit(1)
PY

# Regeneration over an unchanged tree is byte-stable.
before="$(cat "$fake/docs/skills/index.json")"
expect_ok 'regeneration is byte-stable' --write
[[ "$before" == "$(cat "$fake/docs/skills/index.json")" ]] ||
  fail 'regeneration is byte-stable: the manifest changed with no input change'

# --- manifest freshness is enforced without --write --------------------------

new_repo
write_skill alpha-skill
expect_fail 'missing manifest' 'manifest is missing'

new_repo
write_skill alpha-skill
run_check --write || true
printf '{\n  "skills": []\n}\n' >"$fake/docs/skills/index.json"
expect_fail 'stale manifest' 'manifest is stale'
grep -Fq '+++ generated' "$fake/stdout" ||
  fail 'stale manifest: no diff was printed to show what drifted'

# A front-matter edit that is never regenerated is the same defect.
new_repo
write_skill alpha-skill
run_check --write || true
sed -i 's/^one_line_purpose: .*/one_line_purpose: Changed after generation./' \
  "$fake/docs/skills/alpha-skill.md"
expect_fail 'front-matter edited after generation' 'manifest is stale'

# --- --write refuses to bake errors into the manifest ------------------------

new_repo
write_skill alpha-skill
run_check --write || true
write_skill beta-skill bogus-status
expect_fail '--write with errors' \
  'not writing the manifest while front-matter has errors' --write
grep -Fq 'beta-skill' "$fake/docs/skills/index.json" &&
  fail '--write with errors: the bad skill was written into the manifest'

# --- per-field validation ----------------------------------------------------

new_repo
write_skill alpha-skill
sed -i '/^version:/d' "$fake/docs/skills/alpha-skill.md"
expect_fail 'missing required key' "missing required key 'version'"

new_repo
write_skill alpha-skill
sed -i 's/^one_line_purpose: .*/one_line_purpose: ""/' \
  "$fake/docs/skills/alpha-skill.md"
expect_fail 'empty required value' "missing required key 'one_line_purpose'"

new_repo
write_skill alpha-skill
sed -i '/^metadata:/,+1d' "$fake/docs/skills/alpha-skill.md"
expect_fail 'missing metadata.type' "missing required key 'metadata.type'"

new_repo
write_skill alpha-skill active ci-ops '[alpha, beta, gamma]' "$(printf 'x%.0s' $(seq 257))"
expect_fail 'over-long description' 'description is 257 chars (max 256)'

new_repo
write_skill alpha-skill active ci-ops '[alpha, beta]'
expect_fail 'too few tags' 'tags has 2 entries (expected 3-6)'

new_repo
write_skill alpha-skill active ci-ops '[a, b, c, d, e, f, g]'
expect_fail 'too many tags' 'tags has 7 entries (expected 3-6)'

new_repo
write_skill alpha-skill active ci-ops '[Alpha, beta, gamma]'
expect_fail 'uppercase tag' "tag 'Alpha' is not lowercase"

new_repo
write_skill alpha-skill
sed -i 's/^tags: .*/tags: notalist/' "$fake/docs/skills/alpha-skill.md"
expect_fail 'scalar tags' 'tags must be a list'

new_repo
write_skill alpha-skill
sed -i 's/^id: .*/id: other-skill/' "$fake/docs/skills/alpha-skill.md"
expect_fail 'id does not match stem' "does not match filename stem 'alpha-skill'"

new_repo
write_skill alpha-skill
sed -i 's|^entry_point: .*|entry_point: docs/skills/elsewhere.md|' \
  "$fake/docs/skills/alpha-skill.md"
expect_fail 'entry_point does not match stem' "should be 'docs/skills/alpha-skill.md'"

new_repo
write_skill alpha-skill retired
expect_fail 'invalid status' "status 'retired' not one of"

new_repo
write_skill alpha-skill active security
expect_fail 'invalid category' "category 'security' not one of"

# Every documented status and category is accepted, so the gate does not
# quietly narrow to whatever the repo happens to use today.
for status in active deprecated reserved; do
  new_repo
  write_skill alpha-skill "$status"
  expect_ok "status '$status' accepted" --write
done
for category in ci-ops test-authoring meta; do
  new_repo
  write_skill alpha-skill active "$category"
  expect_ok "category '$category' accepted" --write
done

# --- document size limits ----------------------------------------------------

new_repo
write_skill alpha-skill
# 15 front-matter/heading lines are already present; pad past the 200-line soft
# limit without reaching the 500-line hard limit.
for _ in $(seq 200); do printf 'padding\n' >>"$fake/docs/skills/alpha-skill.md"; done
expect_ok 'soft line limit warns but passes' --write
grep -Fq 'soft max 200' "$fake/stdout" ||
  fail 'soft line limit: expected a warning naming the soft max'
grep -Fq '0 error(s), 1 warning(s)' "$fake/stdout" ||
  fail 'soft line limit: expected exactly one warning and no errors'

new_repo
write_skill alpha-skill
for _ in $(seq 600); do printf 'padding\n' >>"$fake/docs/skills/alpha-skill.md"; done
expect_fail 'hard line limit fails' 'hard max 500'

# --- front-matter parsing ----------------------------------------------------

new_repo
write_skill alpha-skill
sed -i '1d' "$fake/docs/skills/alpha-skill.md"
expect_fail 'no front-matter' 'missing or unterminated YAML front-matter'

new_repo
write_skill alpha-skill
sed -i '0,/^---$/! {0,/^---$/ s/^---$/not-a-terminator/}' \
  "$fake/docs/skills/alpha-skill.md"
expect_fail 'unterminated front-matter' 'missing or unterminated YAML front-matter'

new_repo
write_skill alpha-skill
sed -i 's/^one_line_purpose: .*/this line has no colon/' \
  "$fake/docs/skills/alpha-skill.md"
expect_fail 'unparseable line' 'unparseable front-matter line'

# Block lists and quoted values parse to the same manifest as inline lists.
new_repo
write_skill alpha-skill
python3 - "$fake/docs/skills/alpha-skill.md" <<'PY'
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    text = handle.read()
text = text.replace(
    "tags: [alpha, beta, gamma]",
    "tags:\n  - alpha\n  - 'beta'\n  - \"gamma\"",
)
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
expect_ok 'block list front-matter' --write
python3 - "$fake/docs/skills/index.json" <<'PY' || failures=$((failures + 1))
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
tags = manifest["skills"][0]["tags"]
if tags != ["alpha", "beta", "gamma"]:
    print("block list tags parsed as %r" % (tags,), file=sys.stderr)
    sys.exit(1)
PY

# --- repository preconditions and arguments ----------------------------------

new_repo
write_skill alpha-skill
expect_fail 'unknown argument' 'unknown argument(s): --nope' --nope

new_repo
write_skill alpha-skill
rm "$fake/docs/SKILL.md"
expect_fail 'missing router' 'skill router is missing'

new_repo
expect_fail 'empty skill directory' 'no skill documents found'

new_repo
rm -rf "$fake/docs/skills"
expect_fail 'missing skill directory' 'skill directory is missing'

# --- the gate this repo actually runs ----------------------------------------
# The script only protects anything while validate.yml and pre-commit invoke
# it, and the repo's own committed manifest must be fresh.

grep -Fq 'bash scripts/check-skill-frontmatter.sh' \
  "$repo_root/.github/workflows/validate.yml" ||
  fail 'validate.yml no longer runs scripts/check-skill-frontmatter.sh'
grep -Fq 'scripts/check-skill-frontmatter.sh' \
  "$repo_root/.pre-commit-config.yaml" ||
  fail '.pre-commit-config.yaml no longer runs scripts/check-skill-frontmatter.sh'

if ((failures > 0)); then
  printf 'check-skill-frontmatter contract: %d failure(s) across %d case(s)\n' \
    "$failures" "$case_count" >&2
  exit 1
fi

printf 'check-skill-frontmatter contract OK: %d cases\n' "$case_count"
