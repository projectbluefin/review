#!/usr/bin/env bash
# Exercise the image-build skill generator against a local manifest so a
# malicious id cannot escape the generated skills root.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The generator's input is docs/skills/index.json, which
# scripts/check-skill-frontmatter.sh generates from skill front-matter. Run
# that producer's contract first, so a manifest defect is reported at the
# layer that produced it rather than as a confusing generator failure.
bash "$repo_root/tests/check-skill-frontmatter-contract.sh"

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
    },
    {
      "id": "excluded-skill",
      "name": "excluded-skill",
      "description": "Excluded by the build.",
      "entry_point": "docs/skills/valid-skill.md"
    }
  ]
}
EOF

python3 "$repo_root/scripts/generate-skills.py" \
  --index "$tmpdir/index.json" \
  --raw-base "$tmpdir/source" \
  --exclude excluded-skill \
  --out "$tmpdir/out" \
  2>"$tmpdir/stderr"

test -f "$tmpdir/out/valid-skill/SKILL.md"
test -f "$tmpdir/out/nested-skill/SKILL.md"
test ! -e "$tmpdir/escaped"
grep -Fq "skipped $tmpdir/escaped: invalid id" "$tmpdir/stderr"
grep -Fq 'skipped invalid-entry: invalid entry_point' "$tmpdir/stderr"
test ! -e "$tmpdir/out/excluded-skill"
grep -Fq 'skipped excluded-skill: excluded by build' "$tmpdir/stderr"

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

# Community catalogs commonly keep each skill in a standard directory rather
# than below docs/skills. A local manifest resolves relative to its own
# directory and projects the complete skill tree without following symlinks.
mkdir -p \
  "$tmpdir/community/community-skill/assets" \
  "$tmpdir/community/community-skill/scripts"
cat >"$tmpdir/community/community-skill/SKILL.md" <<'EOF'
---
name: community-skill
description: "A community skill."
license: Apache-2.0
---

# Community Skill
EOF
printf 'COMMUNITY_ASSET_MARKER\n' \
  >"$tmpdir/community/community-skill/assets/template.txt"
printf '#!/usr/bin/env bash\nprintf "community helper\\n"\n' \
  >"$tmpdir/community/community-skill/scripts/helper.sh"
chmod +x "$tmpdir/community/community-skill/scripts/helper.sh"
ln -s /etc/passwd "$tmpdir/community/community-skill/assets/unsafe-link"

cat >"$tmpdir/community/index.json" <<'EOF'
{
  "skills": [
    {
      "id": "community-skill",
      "name": "community-skill",
      "description": "A community skill.",
      "entry_point": "community-skill"
    }
  ]
}
EOF

cat >"$tmpdir/direct-skill.md" <<'EOF'
---
name: direct-skill
description: "A directly projected skill."
license: MIT
---

# Direct Skill
EOF

python3 "$repo_root/scripts/generate-skills.py" \
  --source "$tmpdir/community/index.json" \
  --source "$tmpdir/direct-skill.md" \
  --out "$tmpdir/community-out" \
  2>"$tmpdir/community-stderr"

test -f "$tmpdir/community-out/community-skill/SKILL.md"
test -f "$tmpdir/community-out/community-skill/assets/template.txt"
test -f "$tmpdir/community-out/community-skill/scripts/helper.sh"
test -x "$tmpdir/community-out/community-skill/scripts/helper.sh"
grep -Fq 'COMMUNITY_ASSET_MARKER' \
  "$tmpdir/community-out/community-skill/assets/template.txt"
test ! -e "$tmpdir/community-out/community-skill/assets/unsafe-link"
grep -Fq \
  'skipped community-skill/assets/unsafe-link: symlink or unsupported file' \
  "$tmpdir/community-stderr"

# A direct SKILL.md source is copied verbatim, retaining standard community
# frontmatter fields that are not part of the Bluefin factory manifest.
test -f "$tmpdir/community-out/direct-skill/SKILL.md"
grep -Fq 'license: MIT' "$tmpdir/community-out/direct-skill/SKILL.md"

# Refuse an output nested inside its source before replacing any files.
if python3 "$repo_root/scripts/generate-skills.py" \
  --source "$tmpdir/community/community-skill" \
  --out "$tmpdir/community/community-skill/projected" \
  2>"$tmpdir/overlap-stderr"; then
  echo "skill generator accepted overlapping source and output" >&2
  exit 1
fi
test -f "$tmpdir/community/community-skill/SKILL.md"
test ! -e "$tmpdir/community/community-skill/projected"
grep -Fq 'source and target directories overlap' "$tmpdir/overlap-stderr"

# HTTP sources use the same projection path without depending on the public
# network. The local server also proves linked references resolve beside a
# remote SKILL.md.
mkdir -p "$tmpdir/remote/remote-skill/references"
cat >"$tmpdir/remote/remote-skill/SKILL.md" <<'EOF'
---
name: remote-skill
description: "A remotely projected skill."
---

# Remote Skill

[Remote reference](references/remote.md)
EOF
printf 'REMOTE_REFERENCE_MARKER\n' \
  >"$tmpdir/remote/remote-skill/references/remote.md"

REPO_ROOT="$repo_root" REMOTE_ROOT="$tmpdir/remote" \
  REMOTE_OUT="$tmpdir/remote-out" python3 - <<'PY'
import functools
import http.server
import os
import pathlib
import subprocess
import threading


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


handler = functools.partial(
    QuietHandler,
    directory=os.environ["REMOTE_ROOT"],
)
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    source = (
        f"http://127.0.0.1:{server.server_port}/remote-skill/SKILL.md"
    )
    subprocess.run(
        [
            "python3",
            str(pathlib.Path(os.environ["REPO_ROOT"]) / "scripts/generate-skills.py"),
            "--source",
            source,
            "--out",
            os.environ["REMOTE_OUT"],
        ],
        check=True,
        stderr=subprocess.DEVNULL,
    )
finally:
    server.shutdown()
    thread.join()
    server.server_close()
PY

test -f "$tmpdir/remote-out/remote-skill/SKILL.md"
grep -Fq 'REMOTE_REFERENCE_MARKER' \
  "$tmpdir/remote-out/remote-skill/references/remote.md"

echo "✓ skill generator safely projects manifests and community skills."
