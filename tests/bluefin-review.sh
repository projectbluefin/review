#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review="$repo_root/image/bin/bluefin-review"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin"

cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${GOOSE_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/goose"

expected_banner=$'+------------------------+\n| BLUEFIN REVIEW         |\n| HUMAN DECISION REQUIRED|\n+------------------------+'

# Pin the skills root away from the caller's real ~/.agents/skills so the
# baseline assertions below do not depend on whether this host happens to have
# the projected org skills. The context test further down opts back in.
export BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent"

# --- default mode: banner, then hand the range to goose review ---------------
# 'main...HEAD' is a real 'goose review' argument. An earlier version of this
# test asserted a '--task' flag that goose review does not accept; the stub
# accepted anything, so the bogus contract went unnoticed.
set +e
banner="$(PATH="$scratch/bin:$PATH" GOOSE_ARGS="$scratch/goose-args" \
  "$review" main...HEAD)"
status=$?
set -e

[[ "$banner" == "$expected_banner" ]]
[[ "$status" -eq 23 ]]
[[ "$(cat "$scratch/goose-args")" == "review main...HEAD" ]]

# --- no arguments still reviews the working tree ------------------------------
set +e
PATH="$scratch/bin:$PATH" GOOSE_ARGS="$scratch/goose-args-empty" "$review" >/dev/null
set -e
[[ "$(cat "$scratch/goose-args-empty")" == 'review' ]]

# --- help never invokes goose -------------------------------------------------
rm -f "$scratch/goose-args-help"
help_out="$(PATH="$scratch/bin:$PATH" GOOSE_ARGS="$scratch/goose-args-help" "$review" --help)"
[[ "$help_out" == *'bluefin-review queue'* ]]
[[ ! -e "$scratch/goose-args-help" ]]

# --- queue mode reads the snapshot and filters it ------------------------------
cat >"$scratch/queue.json" <<'EOF'
{
  "generated_at": "2026-08-08T00:00:00.000Z",
  "items": [
    {"repository":"projectbluefin/alpha","number":1,"recommended_action":"review","title":"first","author":"someone"},
    {"repository":"projectbluefin/beta","number":2,"recommended_action":"fix-ci","title":"second","author":"someone"},
    {"repository":"projectbluefin/alpha","number":3,"recommended_action":"review","title":"third","author":"me"}
  ]
}
EOF

# gh is stubbed so the test never touches the network or a real repository.
# 'api user' answers the walker's identity: their own pull requests are not
# work for them, and alpha#3 above is theirs.
cat >"$scratch/bin/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${GH_CALLS:?}"
case "$*" in
  "api user"*) printf 'me\n' ;;
  *--json*) printf '{"author":{"login":"someone"},"isDraft":false,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","reviewDecision":null,"additions":1,"deletions":0,"changedFiles":1,"updatedAt":"2026-08-08T00:00:00Z","statusCheckRollup":[{"conclusion":"SUCCESS"}]}' ;;
esac
EOF
chmod +x "$scratch/bin/gh"

# Walk the one remaining 'review' item with Enter, then fall off the end of
# the queue. The loop reads keys from /dev/tty, so this only asserts fully
# under a terminal; without one, assert the explicit refusal instead of
# skipping.
set +e
out="$(printf '\n\n' | PATH="$scratch/bin:$PATH" GH_CALLS="$scratch/gh-calls" \
  "$review" queue --url "file://$scratch/queue.json" 2>&1)"
set -e

if [[ "$out" == *'needs an interactive terminal'* ]]; then
  printf 'queue mode correctly refuses a non-interactive run\n'
else
  [[ "$out" == *'1 pull request(s) to walk'* ]]
  [[ "$out" == *'projectbluefin/alpha#1'* ]]
  [[ "$out" != *'projectbluefin/alpha#3'* ]]
  [[ "$out" != *'projectbluefin/beta#2'* ]]
fi

# --- org review context is injected from the projected common skills ---------
# 'goose review' reads .agents/REVIEW.md and .agents/checks/*.md from the
# REVIEWED repository, not from ~/.agents/skills, so the org's review doctrine
# only reaches it through --instructions.
mkdir -p "$scratch/skills/pr-review/references"
cat >"$scratch/skills/pr-review/SKILL.md" <<'EOF'
---
name: pr-review
---
ORG_REVIEW_DOCTRINE_MARKER
EOF
cat >"$scratch/skills/pr-review/references/card-fields.md" <<'EOF'
ORG_REVIEW_REFERENCE_MARKER
EOF

cat >"$scratch/bin/goose-capture" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${GOOSE_ARGV:?}"
EOF
chmod +x "$scratch/bin/goose-capture"
cp "$scratch/bin/goose-capture" "$scratch/bin/goose"

BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv" \
  "$review" main...HEAD >/dev/null

argv="$(tr '\0' '\n' <"$scratch/argv")"
[[ "$(sed -n 1p <<<"$argv")" == 'review' ]]
[[ "$(sed -n 2p <<<"$argv")" == '--instructions' ]]
# A POINTER to the doctrine, not the doctrine itself: inlining every projected
# skill would repeat ~33 KB on each check subprocess of every review.
[[ "$argv" == *"$scratch/skills/pr-review/SKILL.md"* ]]
[[ "$argv" == *"$scratch/skills/pr-review/references/"* ]]
[[ "$argv" != *'ORG_REVIEW_DOCTRINE_MARKER'* ]]
[[ "$argv" != *'ORG_REVIEW_REFERENCE_MARKER'* ]]
instructions_bytes="$(tr '\0' '\n' <"$scratch/argv" | sed -n 3p | wc -c)"
((instructions_bytes < 4096))

# Hive's knowledge export is named explicitly so a review can reach the hub's
# knowledge base without relying on Goose context-file inheritance.
printf 'HIVE_KB\n' >"$scratch/agent.md"
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/agent.md" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-kb" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-kb")" == *"$scratch/agent.md"* ]]

# An absent knowledge file must not leave a dangling pointer in the prompt.
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/no-agent.md" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-nokb" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-nokb")" != *'knowledge base'* ]]

# Upstream writes a non-empty placeholder when the hub fetch fails; a size
# check alone would announce that dead export as the knowledge base.
printf 'Knowledge base not yet available.\n' >"$scratch/placeholder.md"
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/placeholder.md" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-placeholder" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-placeholder")" != *'knowledge base'* ]]
# The range must survive as its own argument, after the instructions.
[[ "$(tr '\0' '\n' <"$scratch/argv" | tail -1)" == 'main...HEAD' ]]

# With no projected skills there is nothing to add, and the call stays bare.
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-bare" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-bare")" == $'review\nmain...HEAD' ]]

# restore the exit-code stub for any later assertions
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${GOOSE_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/goose"

# --- duplicate detection ------------------------------------------------------
# A pull request's near-neighbours are part of the evidence: Renovate opens a
# digest bump and a version bump for the same dependency, and several agents can
# close one issue from separate pull requests.

cat >"$scratch/bin/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${GH_CALLS:?}"
case "$*" in
  *"pr list"*) cat "${GH_PR_LIST:?}" ;;
  *--json*) printf '{"author":{"login":"someone"},"isDraft":false,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","reviewDecision":null,"additions":1,"deletions":0,"changedFiles":1,"updatedAt":"2026-08-08T00:00:00Z","statusCheckRollup":[{"conclusion":"SUCCESS"}]}' ;;
esac
EOF
chmod +x "$scratch/bin/gh"

cat >"$scratch/pulls.json" <<'EOF'
[
 {"number":1,"title":"chore(deps): update actions/checkout action to v7",
  "files":[{"path":".github/workflows/ci.yml"}],"closingIssuesReferences":[]},
 {"number":2,"title":"chore(deps): update actions/checkout digest to d23441a",
  "files":[{"path":".github/workflows/ci.yml"}],"closingIssuesReferences":[]},
 {"number":3,"title":"fix: unrelated change","files":[{"path":".github/workflows/ci.yml"}],
  "closingIssuesReferences":[]},
 {"number":4,"title":"feat: close it","files":[{"path":"a.md"}],
  "closingIssuesReferences":[{"number":57}]},
 {"number":5,"title":"feat: close it differently","files":[{"path":"b.md"}],
  "closingIssuesReferences":[{"number":57}]}
]
EOF

analyzer_out() {
  GH_PR_LIST="$scratch/pulls.json" GH_CALLS="$scratch/gh-dup-calls" \
    PATH="$scratch/bin:$PATH" \
    bash -c '
      source_file="$1"; number="$2"; cache="$3"; mode="${4:-}"
      CACHE_DIR="$(dirname "$cache")"
      eval "$(sed -n "/^DUPLICATE_ANALYZER=/,/^'"'"'$/p" "$source_file")"
      if [[ -n "$mode" ]]; then
        python3 -c "$DUPLICATE_ANALYZER" --numbers "$number" "$cache"
      else
        python3 -c "$DUPLICATE_ANALYZER" "$number" "$cache"
      fi
    ' _ "$review" "$1" "$scratch/pulls.json" "${2:-}"
}

# Same dependency, different bump style: a real duplicate.
[[ "$(analyzer_out 1)" == *'dupe-of'* ]]
[[ "$(analyzer_out 1)" == *'#2 (same dependency actions/checkout)'* ]]

# Two pull requests closing one issue are duplicates too.
[[ "$(analyzer_out 4)" == *'#5 (both close #57)'* ]]

# Sharing a file is an ordering hazard, not duplication. Across the live queue
# that signal fires on an order of magnitude more pairs than real duplicates,
# so it must never be reported as one.
[[ "$(analyzer_out 3)" == *'overlaps'* ]]
[[ "$(analyzer_out 3)" != *'dupe-of'* ]]

# The --numbers mode feeds run_goose_review's cluster fetch: bare numbers, no
# prose, so the shell never parses a human sentence into a pull request number.
[[ "$(analyzer_out 1 numbers)" == *$'dupes\t2'* ]]
[[ "$(analyzer_out 4 numbers)" == *$'dupes\t5'* ]]
[[ "$(analyzer_out 3 numbers)" == *$'overlaps\t1 2'* ]]
[[ "$(analyzer_out 3 numbers)" != *'same dependency'* ]]

# --- failures are reported, never mistaken for an empty queue -----------------
# Reading from a process substitution discards the producer's status, which made
# a failed fetch look like "nothing to review" and exit 0.
set +e
bad_out="$(PATH="$scratch/bin:$PATH" "$review" queue --url "file://$scratch/missing.json" 2>&1)"
bad_status=$?
set -e
((bad_status != 0))
[[ "$bad_out" == *'could not read the queue'* ]]
[[ "$bad_out" != *'Nothing in the queue'* ]]
# Reported once, not once per layer.
[[ "$(grep -c 'could not read the queue' <<<"$bad_out")" -eq 1 ]]

printf 'not json\n' >"$scratch/malformed.json"
set +e
PATH="$scratch/bin:$PATH" "$review" queue --url "file://$scratch/malformed.json" >/dev/null 2>&1
malformed_status=$?
set -e
((malformed_status != 0))

# --- the reviewer never approves, submits, or merges --------------------------
# A menu that can merge is a different tool with a different authority claim.
if grep -q 'pr merge' "$review"; then
  echo "bluefin-review must never merge" >&2
  exit 1
fi
if grep -q 'pr review' "$review"; then
  echo "bluefin-review must never submit a review" >&2
  exit 1
fi

printf 'bluefin-review contract OK\n'
