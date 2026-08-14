#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review="$repo_root/image/bin/bluefin-review"
export BLUEFIN_REVIEW_HARNESS_ROOT="$repo_root/image"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin"

cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s\n' "$*" >"${GOOSE_ARGS:?}"
printf '%s\n' 'adapter invoked' >"${GOOSE_ADAPTER_CALLED:-/dev/null}"
exit 23
EOF
chmod +x "$scratch/bin/goose"

expected_banner=$'+------------------------+\n| BLUEFIN REVIEW         |\n| HUMAN DECISION REQUIRED|\n+------------------------+'

# Pin the skills root away from the caller's real ~/.agents/skills so the
# baseline assertions below do not depend on whether this host happens to have
# the projected org skills. The context test further down opts back in.
export BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent"
export BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/absent"

# --- default mode: banner, then hand the range to goose review ---------------
# 'main...HEAD' is a real 'goose review' argument. An earlier version of this
# test asserted a '--task' flag that goose review does not accept; the stub
# accepted anything, so the bogus contract went unnoticed.
set +e
banner="$(PATH="$scratch/bin:$PATH" GOOSE_ARGS="$scratch/goose-args" \
  GOOSE_ADAPTER_CALLED="$scratch/adapter-called" \
  "$review" main...HEAD)"
status=$?
set -e

[[ "$banner" == "$expected_banner" ]]
[[ "$status" -eq 23 ]]
[[ "$(cat "$scratch/goose-args")" == "review main...HEAD" ]]
[[ -f "$scratch/adapter-called" ]]

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

# PR mode checks jq before it touches the mocked GitHub response. Keep this
# contract hermetic: duplicate analysis itself is exercised through Python.
cat >"$scratch/bin/jq" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$scratch/bin/jq"

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
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
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
[[ "$incomplete_out" == *'INCOMPLETE'* ]]
grep -q 'bluefin-doctrine' <<<"$incomplete_out"
grep -q 'orchestrator emitted 0 finding' <<<"$incomplete_out"
# It must never also claim to be a finished draft.
[[ "$incomplete_out" != *'The Review Draft above is for you to judge'* ]]

# A run where every check answered stays clean, and stays exit 0.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
echo "goose review: check 'bluefin-doctrine' completed: 0 finding(s)" >&2
echo "goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))" >&2
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

# A zero-exit malformed stream is still an adapter failure, not a clean draft.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s\n' 'malformed Goose output'
exit 0
EOF
chmod +x "$scratch/bin/goose"
set +e
malformed_status="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-malformed" "$review" pr projectbluefin/alpha 31 2>/dev/null)"
malformed_exit=$?
set -e
((malformed_exit != 0))
[[ "$malformed_status" == *'Review did not complete'* ]]
[[ "$malformed_status" != *'The Review Draft above is for you to judge'* ]]

# TERM requests adapter cancellation before the launcher cleans up. The
# adapter-created process group must not leave its PATH-local Goose behind.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s\n' "$$" >"${GOOSE_PID_FILE:?}"
exec sleep 1000
EOF
chmod +x "$scratch/bin/goose"
PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GOOSE_PID_FILE="$scratch/goose-pid" GH_CALLS="$scratch/gh-calls-signal" \
  "$review" pr projectbluefin/alpha 31 >"$scratch/signal-output" 2>&1 &
launcher_pid=$!
for _ in {1..50}; do
  [[ -s "$scratch/goose-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/goose-pid" ]]
goose_pid="$(<"$scratch/goose-pid")"
kill -TERM "$launcher_pid"
set +e
wait "$launcher_pid"
signal_exit=$?
set -e
((signal_exit != 0))
if ps -p "$goose_pid" -o comm= 2>/dev/null | grep -qx 'sleep'; then
  echo "Goose process survived launcher TERM: $goose_pid" >&2
  exit 1
fi

# The shipped local-range path must forward TERM to the adapter too. The
# adapter owns the Goose process group and must terminate and wait for it.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s %s\n' "$$" "$(ps -o pgid= -p $$ | tr -d ' ')" >"${GOOSE_PID_FILE:?}"
exec sleep 1000
EOF
chmod +x "$scratch/bin/goose"
PATH="$scratch/bin:$PATH" GOOSE_PID_FILE="$scratch/local-term-pid" \
  "$review" main...HEAD >"$scratch/local-term-output" 2>&1 &
local_term_launcher=$!
for _ in {1..50}; do
  [[ -s "$scratch/local-term-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/local-term-pid" ]]
read -r local_term_goose local_term_pgid <"$scratch/local-term-pid"
kill -TERM "$local_term_launcher"
set +e
wait "$local_term_launcher"
local_term_exit=$?
set -e
((local_term_exit != 0))
for _ in {1..20}; do
  kill -0 -- "-$local_term_pgid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 -- "-$local_term_pgid" 2>/dev/null; then
  echo "local-range Goose process group survived TERM: $local_term_pgid" >&2
  exit 1
fi

# INT exercises the same shipped local-range path and preserves its distinct
# interrupt status while still requiring the adapter-owned group to be gone.
PATH="$scratch/bin:$PATH" GOOSE_PID_FILE="$scratch/local-int-pid" \
  env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/local-int-output" 2>&1 &
local_int_launcher=$!
for _ in {1..50}; do
  [[ -s "$scratch/local-int-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/local-int-pid" ]]
read -r local_int_goose local_int_pgid <"$scratch/local-int-pid"
kill -INT "$local_int_launcher"
set +e
wait "$local_int_launcher"
local_int_exit=$?
set -e
((local_int_exit != 0))
for _ in {1..20}; do
  kill -0 -- "-$local_int_pgid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 -- "-$local_int_pgid" 2>/dev/null; then
  echo "local-range Goose process group survived INT: $local_int_pgid" >&2
  exit 1
fi

# Completion-boundary signals must preserve the exact status without
# cancelling a completed adapter or abandoning its temporary review scope.
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s\n' "$$" >"${GOOSE_PID_FILE:?}"
printf '%s\n' "goose review: check 'main' completed: 0 finding(s)"
printf '%s\n' 'goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))'
exit 0
EOF
chmod +x "$scratch/bin/goose"
cat >"$scratch/debug-boundary.env" <<'EOF'
kill() {
  if [[ "${1-}" == "-TERM" && -n "${KILL_LOG-}" ]]; then
    printf '%s\n' "${*:2}" >>"$KILL_LOG"
  fi
  builtin kill "$@"
}
export -f kill
set -T
boundary_debug() {
  [[ -n "${BOUNDARY_TRACE-}" ]] && printf '%s\n' "${BASH_COMMAND-}" >>"$BOUNDARY_TRACE"
  case "${BASH_COMMAND-}" in
    *REVIEW_CHILD_PID*)
      if [[ "${BOUNDARY_PHASE-}" == adapter ]]; then
        : >"${BOUNDARY_MARKER:?}"
        sleep 1
      fi
      ;;
    *'rm -rf "$scope"'*)
      if [[ "${BOUNDARY_PHASE-}" == scope ]]; then
        : >"${BOUNDARY_MARKER:?}"
        sleep 1
      fi
      ;;
  esac
}
trap boundary_debug DEBUG
EOF

run_boundary_signal() {
  local signal="$1" expected="$2" label="$3"
  rm -f "$scratch/boundary-marker" "$scratch/boundary-kills" "$scratch/boundary-pid"
  if [[ "$signal" == INT ]]; then
    BASH_ENV="$scratch/debug-boundary.env" PATH="$scratch/bin:$PATH" \
      GOOSE_PID_FILE="$scratch/boundary-pid" KILL_LOG="$scratch/boundary-kills" \
      BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=adapter BOUNDARY_TRACE="/tmp/issue-239-boundary-trace" \
      env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/boundary-$label-output" 2>&1 &
  else
    BASH_ENV="$scratch/debug-boundary.env" PATH="$scratch/bin:$PATH" \
      GOOSE_PID_FILE="$scratch/boundary-pid" KILL_LOG="$scratch/boundary-kills" \
      BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=adapter BOUNDARY_TRACE="/tmp/issue-239-boundary-trace" "$review" main...HEAD \
      >"$scratch/boundary-$label-output" 2>&1 &
  fi
  local launcher=$!
  for _ in {1..50}; do
    [[ -e "$scratch/boundary-marker" ]] && break
    sleep 0.1
  done
  [[ -e "$scratch/boundary-marker" ]]
  if [[ "$signal" == INT ]]; then
    builtin kill -"$signal" -- "-$launcher"
  else
    builtin kill -"$signal" "$launcher"
  fi
  set +e
  wait "$launcher"
  local actual=$?
  set -e
  [[ "$actual" -eq "$expected" ]]
  [[ ! -s "$scratch/boundary-kills" ]]
}

run_boundary_signal TERM 143 adapter-complete
run_boundary_signal INT 130 adapter-complete-int

for signal in TERM INT; do
  expected=143
  [[ "$signal" == INT ]] && expected=130
  rm -rf "$scratch/overlay" "$scratch/boundary-marker" "$scratch/boundary-kills" "$scratch/boundary-pid"
  mkdir -p "$scratch/tmp"
  mkdir -p "$scratch/overlay/.agents/checks"
  printf 'SCOPED REVIEW PROMPT\n' >"$scratch/overlay/.agents/REVIEW.md"
  printf '%s\n' '---' 'name: bluefin-doctrine' '---' >"$scratch/overlay/.agents/checks/bluefin-doctrine.md"
  if [[ "$signal" == INT ]]; then
    BASH_ENV="$scratch/debug-boundary.env" BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
      TMPDIR="$scratch/tmp" PATH="$scratch/bin:$PATH" GOOSE_PID_FILE="$scratch/boundary-pid" \
      KILL_LOG="$scratch/boundary-kills" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=scope BOUNDARY_TRACE="/tmp/issue-239-boundary-trace" \
      env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/scope-$signal-output" 2>&1 &
  else
    BASH_ENV="$scratch/debug-boundary.env" BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
      TMPDIR="$scratch/tmp" PATH="$scratch/bin:$PATH" GOOSE_PID_FILE="$scratch/boundary-pid" \
      KILL_LOG="$scratch/boundary-kills" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=scope BOUNDARY_TRACE="/tmp/issue-239-boundary-trace" \
      "$review" main...HEAD >"$scratch/scope-$signal-output" 2>&1 &
  fi
  launcher=$!
  for _ in {1..50}; do
    [[ -e "$scratch/boundary-marker" ]] && break
    sleep 0.1
  done
  [[ -e "$scratch/boundary-marker" ]]
  if [[ "$signal" == INT ]]; then
    builtin kill -"$signal" -- "-$launcher"
  else
    builtin kill -"$signal" "$launcher"
  fi
  set +e
  wait "$launcher"
  actual=$?
  set -e
  [[ "$actual" -eq "$expected" ]]
  [[ ! -s "$scratch/boundary-kills" ]]
  if compgen -G "$scratch/tmp/bluefin-review-scope.*" >/dev/null; then
    echo "scope survived completion-boundary $signal" >&2
    exit 1
  fi
done

# restore the exit-code stub for the assertions that follow
cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
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
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
printf '%s\0' "$@" >"${GOOSE_ARGV:?}"
printf '%s\n' "goose review: check 'main' completed: 0 finding(s)"
printf '%s\n' 'goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))'
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

# --- repository-owned context is named before shared doctrine -----------------
mkdir -p "$scratch/repository/docs/skills"
printf 'REPOSITORY AGENTS\n' >"$scratch/repository/AGENTS.md"
printf '# Repository skill router\n' >"$scratch/repository/docs/SKILL.md"
cat >"$scratch/repository/docs/skills/index.json" <<'EOF'
{"skills":[{"id":"repo-skill","description":"Repository review guidance","entry_point":"docs/skills/repo-skill.md","status":"active"}]}
EOF
printf '# Repository skill\n' >"$scratch/repository/docs/skills/repo-skill.md"

BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/repository" \
  BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-repository" \
  "$review" main...HEAD >/dev/null
repository_argv="$(tr '\0' '\n' <"$scratch/argv-repository")"
[[ "$repository_argv" == *"repository-owned context"* ]]
[[ "$repository_argv" == *"$scratch/repository/AGENTS.md"* ]]
[[ "$repository_argv" == *"$scratch/repository/docs/SKILL.md"* ]]
[[ "$repository_argv" == *"$scratch/repository/docs/skills/repo-skill.md"* ]]
[[ "$repository_argv" == *"before shared Bluefin doctrine"* ]]
[[ "$repository_argv" != *'Repository skill router'* ]]

# Invalid optional catalog entries are degraded and never named as trusted.
mkdir -p "$scratch/invalid/docs/skills"
cat >"$scratch/invalid/docs/skills/index.json" <<'EOF'
{"skills":[
  {"id":"duplicate","description":"one","entry_point":"docs/skills/one.md","status":"active"},
  {"id":"duplicate","description":"two","entry_point":"docs/skills/two.md","status":"active"},
  {"id":"inactive","description":"no","entry_point":"docs/skills/inactive.md","status":"inactive"},
  {"id":"absolute","description":"no","entry_point":"/tmp/absolute.md","status":"active"},
  {"id":"traversal","description":"no","entry_point":"../outside.md","status":"active"}
]}
EOF
BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/invalid" \
  BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-invalid" \
  "$review" main...HEAD >/dev/null
invalid_argv="$(tr '\0' '\n' <"$scratch/argv-invalid")"
[[ "$invalid_argv" == *'catalog unavailable'* ]]
[[ "$invalid_argv" != *'one.md'* && "$invalid_argv" != *'inactive.md'* ]]
[[ "$invalid_argv" != *'absolute.md'* && "$invalid_argv" != *'outside.md'* ]]

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
cp "$repo_root/image/review-scope/checks/bluefin-doctrine.md" \
  "$scratch/overlay/.agents/checks/bluefin-doctrine.md"

cat >"$scratch/bin/goose" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
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
  cat "$scope/.agents/checks/bluefin-doctrine.md" >"${SCOPE_DOCTRINE:?}"
  while IFS= read -r check; do
    filename="${check##*/}"
    expected="${filename%.md}"
    actual="$(sed -n 's/^name: //p' "$scope/.agents/checks/$check" | head -n 1)"
    [[ "$actual" == "$expected" ]] || {
      printf "name '%s' must match filename '%s'\n" "$actual" "$expected" >&2
      exit 1
    }
  done < <(find "$scope/.agents/checks" -type f -name '*.md' -printf '%P\n')
  cat "$scope/.agents/checks/cluster-resolution.md" >"${SCOPE_CLUSTER:?}" 2>/dev/null || : >"${SCOPE_CLUSTER:?}"
fi
printf '%s\n' "goose review: check 'main' completed: 0 finding(s)"
printf '%s\n' 'goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))'
EOF
chmod +x "$scratch/bin/goose"

BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/repository" \
  PATH="$scratch/bin:$PATH" GOOSE_ARGV="$scratch/argv-scope" \
  SCOPE_LISTING="$scratch/scope-listing" SCOPE_DOCTRINE="$scratch/scope-doctrine" \
  SCOPE_CLUSTER="$scratch/scope-cluster" \
  "$review" main...HEAD >/dev/null

argv_scope="$(tr '\0' '\n' <"$scratch/argv-scope")"
[[ "$(sed -n 1p <<<"$argv_scope")" == 'review' ]]
[[ "$(sed -n 2p <<<"$argv_scope")" == '--check-scope' ]]
[[ "$argv_scope" != *'--instructions'* ]]
grep -q '^\.agents/REVIEW\.md$' "$scratch/scope-listing"
grep -q '^\.agents/checks/bluefin-doctrine\.md$' "$scratch/scope-listing"
grep -q '^\.agents/checks/00-repository-context\.md$' "$scratch/scope-listing"
repository_context_line="$(grep -n '^\.agents/checks/00-repository-context\.md$' "$scratch/scope-listing" | cut -d: -f1)"
bluefin_doctrine_line="$(grep -n '^\.agents/checks/bluefin-doctrine\.md$' "$scratch/scope-listing" | cut -d: -f1)"
((repository_context_line < bluefin_doctrine_line))
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
if [[ "$*" == "info --check" ]]; then printf '%s\n' 'provider ready'; exit 0; fi
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
printf '%s\n' "goose review: check 'main' completed: 0 finding(s)"
printf '%s\n' 'goose review: orchestrator emitted 0 finding(s) from 1 check(s) (main: ran, 0 finding(s))'
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

# --- the shipped doctrine states current-model alignment ----------------------
doctrine="$scratch/scope-doctrine"
grep -q 'implementation, tests, and applicable durable documentation remain' "$doctrine"
grep -q 'mutually consistent' "$doctrine"
grep -q 'concrete contradictory evidence' "$doctrine"
grep -q 'file and line' "$doctrine"
grep -q 'no documentation change is needed' "$doctrine"
grep -q 'insufficient evidence' "$doctrine"
grep -q 'uncertainty, not a finding' "$doctrine"
grep -q 'changed-file patterns' "$doctrine"
grep -q 'documentation absence' "$doctrine"
grep -q 'alone are not proof' "$doctrine"

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
