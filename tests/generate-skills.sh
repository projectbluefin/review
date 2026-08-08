#!/usr/bin/env bash
# Exercise the image-build skill generator against a local manifest so a
# malicious id cannot escape the generated skills root.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

mkdir -p "$tmpdir/source/docs/skills/nested-skill"
cat >"$tmpdir/source/docs/skills/valid-skill.md" <<'EOF'
---
name: valid-skill
description: "A valid skill."
---

# Valid Skill
EOF

cat >"$tmpdir/source/docs/skills/nested-skill/SKILL.md" <<'EOF'
---
name: nested-skill
description: "A nested skill."
---

# Nested Skill

Real sibling material: [card fields](references/card-fields.md).
A traversal attempt: [escape](references/../../../../etc/passwd.md).
A nested attempt: [deep](references/sub/dir.md).
EOF

mkdir -p "$tmpdir/source/docs/skills/nested-skill/references"
cat >"$tmpdir/source/docs/skills/nested-skill/references/card-fields.md" <<'EOF'
# Card Fields
REFERENCE_BODY_MARKER
EOF

cat >"$tmpdir/index.json" <<EOF
{
  "skills": [
    {
      "id": "valid-skill",
      "name": "valid-skill",
      "description": "A valid skill.",
      "entry_point": "docs/skills/valid-skill.md"
    },
    {
      "id": "nested-skill",
      "name": "nested-skill",
      "description": "A nested skill.",
      "entry_point": "docs/skills/nested-skill/SKILL.md"
    },
    {
      "id": "$tmpdir/escaped",
      "name": "escaped",
      "description": "Must not escape the output directory.",
      "entry_point": "docs/skills/escaped.md"
    },
    {
      "id": "invalid-entry",
      "name": "invalid-entry",
      "description": "Must stay below the skills directory.",
      "entry_point": "../outside.md"
    }
  ]
}
EOF

python3 "$repo_root/scripts/generate-skills.py" \
  --index "$tmpdir/index.json" \
  --raw-base "$tmpdir/source" \
  --out "$tmpdir/out" \
  2>"$tmpdir/stderr"

test -f "$tmpdir/out/valid-skill/SKILL.md"
test -f "$tmpdir/out/nested-skill/SKILL.md"
test ! -e "$tmpdir/escaped"
grep -Fq "skipped $tmpdir/escaped: invalid id" "$tmpdir/stderr"
grep -Fq 'skipped invalid-entry: invalid entry_point' "$tmpdir/stderr"

# A linked sibling reference is projected beside the skill, so the body's links
# resolve inside the image instead of dangling.
test -f "$tmpdir/out/nested-skill/references/card-fields.md"
grep -Fq 'REFERENCE_BODY_MARKER' "$tmpdir/out/nested-skill/references/card-fields.md"

# Reference names come from an untrusted body, so a traversal or nested path
# must never be fetched or written.
test ! -e "$tmpdir/out/nested-skill/references/sub"
test ! -e "$tmpdir/out/etc"
find "$tmpdir/out" -name 'passwd.md' | grep -q . && {
  echo "reference traversal escaped the skills root" >&2
  exit 1
}

# A single-file skill has no references directory to project from.
test ! -e "$tmpdir/out/valid-skill/references"

echo "✓ skill generator rejects unsafe manifest paths."
