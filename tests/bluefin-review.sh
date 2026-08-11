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
[[ "$help_out" == *'bluefin-review pr'* ]]
[[ ! -e "$scratch/goose-args-help" ]]

# --- pr mode: check the pull request out and review it against its base -------
# The dashboard's review key shells out to exactly this path, so it is the one
# place a pull request becomes a diff for Goose to judge.
mkdir -p "$scratch/workspace/alpha"
git -C "$scratch/workspace/alpha" init --quiet
git -C "$scratch/workspace/alpha" config user.email t@example.com
git -C "$scratch/workspace/alpha" config user.name t

cat >"$scratch/bin/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${GH_CALLS:?}"
case "$*" in
  *baseRefName*) printf 'main\n' ;;
  "pr list"*) printf '[]\n' ;;
  "api user"*) printf 'me\n' ;;
esac
exit 0
EOF
chmod +x "$scratch/bin/gh"

set +e
pr_out="$(PATH="$scratch/bin:$PATH" GH_CALLS="$scratch/gh-calls-pr" \
  GOOSE_ARGS="$scratch/goose-args-pr" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  "$review" pr projectbluefin/alpha 31 2>&1)"
pr_status=$?
set -e

[[ "$pr_status" -eq 23 ]]
[[ "$(cat "$scratch/goose-args-pr")" == "review origin/main...HEAD" ]]
grep -q 'pr checkout 31 --repo projectbluefin/alpha' "$scratch/gh-calls-pr"
[[ "$pr_out" == *'HUMAN DECISION REQUIRED'* ]]
# A Goose failure is never announced as a finished draft.
[[ "$pr_out" == *'Review did not complete'* ]]
[[ "$pr_out" != *'for you to judge'* ]]

# A pull request number is the only thing 'pr' accepts as one.
set +e
PATH="$scratch/bin:$PATH" "$review" pr projectbluefin/alpha HEAD >/dev/null 2>&1
bad_number=$?
PATH="$scratch/bin:$PATH" "$review" pr projectbluefin/alpha >/dev/null 2>&1
missing_number=$?
set -e
((bad_number != 0))
((missing_number != 0))

# --- a review whose checks returned no verdict is never reported as clean -----
# 'goose review' exits 0 when a check answers with prose or an empty response
# instead of JSON, and still prints a finding count. Reading that as a clean
# review is the worst outcome this tool can produce, so it gets its own status.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
cat >&2 <<'OUT'
goose review: discovered 2 check(s):
goose review: check 'bluefin-doctrine' failed: parse check JSON: The model returned an empty response.
goose review: main pass on '.github/workflows/ci.yml' failed: parse check JSON: I'll verify the rest.
goose review: orchestrator emitted 0 finding(s) from 2 check(s) (main: ran, 0 finding(s))
OUT
exit 0
EOF
chmod +x "$scratch/bin/goose"

set +e
incomplete_out="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-inc" "$review" pr projectbluefin/alpha 31 2>&1)"
incomplete_status=$?
set -e

# 65, not 0: the caller must be able to tell this apart from a clean review.
((incomplete_status == 65))
[[ "$incomplete_out" == *'REVIEW INCOMPLETE'* ]]
[[ "$incomplete_out" == *'2 part(s)'* ]]
[[ "$incomplete_out" == *'bluefin-doctrine'* ]]
[[ "$incomplete_out" == *'not a clean bill of health'* ]]
# It must never also claim to be a finished draft.
[[ "$incomplete_out" != *'The Review Draft above is for you to judge'* ]]

# A run where every check answered stays clean, and stays exit 0.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
echo "goose review: check 'bluefin-doctrine' completed: 0 finding(s)" >&2
echo "goose review: orchestrator emitted 0 finding(s) from 1 check(s)" >&2
exit 0
EOF
chmod +x "$scratch/bin/goose"

set +e
clean_out="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-clean" "$review" pr projectbluefin/alpha 31 2>&1)"
clean_status=$?
set -e
((clean_status == 0))
[[ "$clean_out" == *'The Review Draft above is for you to judge'* ]]
[[ "$clean_out" != *'REVIEW INCOMPLETE'* ]]

# restore the exit-code stub for the assertions that follow
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${GOOSE_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/goose"

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

# --- the image review scope replaces the --instructions pointer ---------------
# With the overlay present, goose review runs with --check-scope on a scratch
# copy carrying the static doctrine plus, when a cluster exists, a per-stop
# cluster-resolution check. The scratch scope is deleted after the run, so the
# stub records its contents at invocation time.
mkdir -p "$scratch/overlay/.agents/checks"
printf 'SCOPED REVIEW PROMPT\n' >"$scratch/overlay/.agents/REVIEW.md"
printf -- '---\nname: bluefin-doctrine\n---\nDOCTRINE CHECK\n' \
  >"$scratch/overlay/.agents/checks/bluefin-doctrine.md"

cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${GOOSE_ARGV:?}"
scope=""
while (($#)); do
  case "$1" in
    --check-scope) scope="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ -n "$scope" ]]; then
  find "$scope" -type f | sed "s|^$scope/||" | sort >"${SCOPE_LISTING:?}"
  cat "$scope/.agents/checks/cluster-resolution.md" >"${SCOPE_CLUSTER:?}" 2>/dev/null || : >"${SCOPE_CLUSTER:?}"
fi
EOF
chmod +x "$scratch/bin/goose"

BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-scope" \
  SCOPE_LISTING="$scratch/scope-listing" SCOPE_CLUSTER="$scratch/scope-cluster" \
  "$review" main...HEAD >/dev/null

argv_scope="$(tr '\0' '\n' <"$scratch/argv-scope")"
[[ "$(sed -n 1p <<<"$argv_scope")" == 'review' ]]
[[ "$(sed -n 2p <<<"$argv_scope")" == '--check-scope' ]]
[[ "$argv_scope" != *'--instructions'* ]]
grep -q '^\.agents/REVIEW\.md$' "$scratch/scope-listing"
grep -q '^\.agents/checks/bluefin-doctrine\.md$' "$scratch/scope-listing"
# No cluster on a plain review: the scratch scope carries no resolution check.
if grep -q 'cluster-resolution' "$scratch/scope-listing"; then
  echo "plain review must not carry a cluster-resolution check" >&2
  exit 1
fi
# The overlay itself is never written to; the scratch copy is.
[[ "$argv_scope" != *"$scratch/overlay"* ]]

# A duplicate cluster adds the per-stop resolution check to the scratch scope.
BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_RELATED='These pull requests are the SAME work: #7' \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-scope2" \
  SCOPE_LISTING="$scratch/scope-listing2" SCOPE_CLUSTER="$scratch/scope-cluster2" \
  "$review" main...HEAD >/dev/null
grep -q '^\.agents/checks/cluster-resolution\.md$' "$scratch/scope-listing2"
grep -q '#7' "$scratch/scope-cluster2"
grep -q 'name: cluster-resolution' "$scratch/scope-cluster2"

# Maintainer steering from the dashboard's steer box reaches the review as an
# additional check in the scratch scope, never as a replacement for doctrine.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
scope=""
while (($#)); do
  case "$1" in
    --check-scope) scope="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ -n "$scope" ]]; then
  find "$scope" -type f | sed "s|^$scope/||" | sort >"${SCOPE_LISTING:?}"
  cat "$scope/.agents/checks/maintainer-steering.md" >"${SCOPE_STEER:?}" 2>/dev/null || : >"${SCOPE_STEER:?}"
fi
EOF
chmod +x "$scratch/bin/goose"

BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_STEER='check the CI permissions block' \
  PATH="$scratch/bin:$PATH" \
  SCOPE_LISTING="$scratch/scope-listing3" SCOPE_STEER="$scratch/scope-steer3" \
  "$review" main...HEAD >/dev/null
grep -q '^\.agents/checks/maintainer-steering\.md$' "$scratch/scope-listing3"
grep -q 'name: maintainer-steering' "$scratch/scope-steer3"
grep -q 'check the CI permissions block' "$scratch/scope-steer3"
# Steering is additive: the doctrine check is still there, and steering never
# licenses a state change.
grep -q '^\.agents/checks/bluefin-doctrine\.md$' "$scratch/scope-listing3"
grep -q 'human makes every approval and merge decision' "$scratch/scope-steer3"

# Without a steer, no steering check is written at all.
BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  PATH="$scratch/bin:$PATH" \
  SCOPE_LISTING="$scratch/scope-listing4" SCOPE_STEER="$scratch/scope-steer4" \
  "$review" main...HEAD >/dev/null
if grep -q 'maintainer-steering' "$scratch/scope-listing4"; then
  echo "an unsteered review must carry no maintainer-steering check" >&2
  exit 1
fi

# restore the exit-code stub for any later assertions
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${GOOSE_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/goose"

# --- the engine has no mutation path at all -----------------------------------
# This used to be a set of "every mutation goes through the one gate" checks,
# because the walk owned maintainer actions. It no longer does: approve, merge,
# comment and close belong to the dashboard, behind its typed-number gate. The
# engine's contract is now absolute rather than conditional — it cannot change
# anything on GitHub — which is a far cheaper property to keep true.

# Join backslash continuations first so a wrapped call is scanned as the one
# command it becomes.
review_joined="$scratch/review-joined"
sed -e :a -e '/\\$/N; s/\\\n//; ta' "$review" >"$review_joined"

while IFS= read -r line; do
  echo "the review engine must not mutate GitHub: $line" >&2
  exit 1
done < <(grep -E 'gh (pr (merge|close|comment|edit|review)|issue (close|comment|edit|reopen))' "$review_joined" | grep -vE '^[[:space:]]*#')

if grep -qE -- '--admin' "$review"; then
  echo "bluefin-review must never bypass branch protections with --admin" >&2
  exit 1
fi
if grep -qE -- '--delete-branch' "$review"; then
  echo "bluefin-review must never delete branches" >&2
  exit 1
fi
if grep -qE '(^|[^[:alnum:]_])git +push' "$review"; then
  echo "bluefin-review must never push" >&2
  exit 1
fi

# The gh verbs it does use are readers, and the repository it clones is a
# throwaway inside the container's workspace.
while IFS= read -r line; do
  case "$line" in
  *'gh pr list'* | *'gh pr view'* | *'gh pr checkout'* | *'gh pr diff'* | *'gh repo clone'*) ;;
  *)
    echo "unexpected gh verb in the review engine: $line" >&2
    exit 1
    ;;
  esac
done < <(grep -oE 'gh (pr|repo|issue|api) [a-z-]+' "$review_joined" | sort -u)

printf 'bluefin-review contract OK\n'
