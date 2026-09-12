#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review="$repo_root/image/bin/bluefin-review"
export BLUEFIN_REVIEW_HARNESS_ROOT="$repo_root/image"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/bin"
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${OMP_ARGV:-/dev/null}"
printf '%s\n' "$*" >"${OMP_ARGS:-/dev/null}"
printf '%s\n' "$(git rev-parse HEAD 2>/dev/null || true)" >"${OMP_HEAD:-/dev/null}"
printf '%s\n' "$PWD" >"${OMP_WORKDIR:-/dev/null}"
printf '%s\n' 'adapter invoked' >"${OMP_ADAPTER_CALLED:-/dev/null}"
exit "${OMP_EXIT_CODE:-23}"
EOF
chmod +x "$scratch/bin/omp"

expected_banner=$'+------------------------+\n| BLUEFIN REVIEW         |\n| HUMAN DECISION REQUIRED|\n+------------------------+'

# Pin the skills root away from the caller's real ~/.agents/skills so the
# baseline assertions below do not depend on whether this host happens to have
# the projected org skills. The context test further down opts back in.
export BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent"
export BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/absent"
export BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/absent"

# --- default mode: banner, then hand the range to review -----------------------
set +e
banner="$(PATH="$scratch/bin:$PATH" OMP_ARGS="$scratch/omp-args" \
  OMP_ADAPTER_CALLED="$scratch/adapter-called" \
  "$review" main...HEAD)"
status=$?
set -e

[[ "$banner" == "$expected_banner" ]]
[[ "$status" -eq 23 ]]
[[ -f "$scratch/adapter-called" ]]

# --- no arguments still reviews the working tree ------------------------------
set +e
PATH="$scratch/bin:$PATH" OMP_ARGS="$scratch/omp-args-empty" "$review" >/dev/null
set -e

# --- help never invokes backend -----------------------------------------------
rm -f "$scratch/omp-args-help"
help_out="$(PATH="$scratch/bin:$PATH" OMP_ARGS="$scratch/omp-args-help" "$review" --help)"
[[ "$help_out" == *'bluefin-review pr'* ]]
[[ "$help_out" == *'--prepare-worktree'* ]]
[[ ! -e "$scratch/omp-args-help" ]]

# --- pr mode: check the pull request out and review it against its base -------
# The dashboard's review key shells out to exactly this path, so it is the one
# place a pull request becomes a diff for the reviewer agent to judge.
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
  OMP_ARGS="$scratch/omp-args-pr" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  "$review" pr projectbluefin/alpha 31 2>&1)"
pr_status=$?
set -e

[[ "$pr_status" -eq 23 ]]
grep -q 'pr checkout 31 --repo projectbluefin/alpha' "$scratch/gh-calls-pr"
[[ "$pr_out" == *'HUMAN DECISION REQUIRED'* ]]
# An adapter failure is never announced as a finished draft.
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

# --- isolated worktree path contract ------------------------------------------
worktree_a="$(
  PATH="$scratch/bin:$PATH" BLUEFIN_REVIEW_WORKTREE_ROOT="$scratch/worktrees" \
    "$review" --print-worktree projectbluefin/alpha \
    0123456789abcdef0123456789abcdef01234567
)"
worktree_b="$(
  PATH="$scratch/bin:$PATH" BLUEFIN_REVIEW_WORKTREE_ROOT="$scratch/worktrees" \
    "$review" --print-worktree projectbluefin/alpha \
    1123456789abcdef0123456789abcdef01234567
)"
[[ "$worktree_a" != "$worktree_b" ]]
[[ "$worktree_a" == *"projectbluefin__alpha-"* ]]
[[ "$worktree_b" == *"projectbluefin__alpha-"* ]]

# --- isolated worktree pr mode: review using an explicit isolated workdir -----
rm -f "$scratch/gh-calls-isolated" "$scratch/omp-args-isolated" "$scratch/omp-head-isolated" "$scratch/omp-workdir-isolated"
isolated_dir="$scratch/worktrees/isolated-alpha"
mkdir -p "$isolated_dir"
git -C "$isolated_dir" init --quiet
git -C "$isolated_dir" config user.email t@example.com
git -C "$isolated_dir" config user.name t
git -C "$isolated_dir" commit --allow-empty --no-verify -m "test: isolated base commit" --quiet
iso_base="$(git -C "$isolated_dir" rev-parse HEAD)"
git -C "$isolated_dir" commit --allow-empty --no-verify -m "test: isolated head commit" --quiet
iso_head="$(git -C "$isolated_dir" rev-parse HEAD)"
[[ "$iso_base" != "$iso_head" ]]

rm -rf "$scratch/workspace/alpha"

set +e
iso_pr_out="$(PATH="$scratch/bin:$PATH" GH_CALLS="$scratch/gh-calls-isolated" \
  OMP_ARGS="$scratch/omp-args-isolated" OMP_HEAD="$scratch/omp-head-isolated" \
  OMP_WORKDIR="$scratch/omp-workdir-isolated" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  "$review" pr projectbluefin/alpha 31 \
  --workdir "$isolated_dir" \
  --base-sha "$iso_base" \
  --head-sha "$iso_head" 2>&1)"
iso_pr_status=$?
set -e

[[ "$iso_pr_status" -eq 23 ]]
[[ ! -d "$scratch/workspace/alpha" ]]
if grep -q 'pr checkout' "$scratch/gh-calls-isolated" 2>/dev/null; then
  echo "isolated mode must not invoke gh pr checkout" >&2
  exit 1
fi
if grep -q 'baseRefName' "$scratch/gh-calls-isolated" 2>/dev/null; then
  echo "isolated mode with --base-sha must not query baseRefName" >&2
  exit 1
fi
[[ "$(cat "$scratch/omp-head-isolated")" == "$iso_head" ]]
[[ "$(cat "$scratch/omp-workdir-isolated")" == "$isolated_dir" ]]

# Recreate workspace alpha for subsequent tests
mkdir -p "$scratch/workspace/alpha"
git -C "$scratch/workspace/alpha" init --quiet
git -C "$scratch/workspace/alpha" config user.email t@example.com
git -C "$scratch/workspace/alpha" config user.name t

# Worktree head drift must be detected and rejected
set +e
drift_out="$(
  cd "$isolated_dir"
  PATH="$scratch/bin:$PATH" \
    BLUEFIN_REVIEW_EXPECTED_HEAD_SHA="0000000000000000000000000000000000000001" \
    "$review" 2>&1
)"
drift_status=$?
set -e
((drift_status != 0))
[[ "$drift_out" == *"does not match expected"* ]]

# Input validation on repo format and head SHA
set +e
PATH="$scratch/bin:$PATH" "$review" --prepare-worktree not-an-owner-repo 0123456789abcdef0123456789abcdef01234567 >/dev/null 2>&1
bad_repo_status=$?
PATH="$scratch/bin:$PATH" "$review" --prepare-worktree projectbluefin/alpha not-a-sha >/dev/null 2>&1
bad_sha_status=$?
set -e
((bad_repo_status != 0))
((bad_sha_status != 0))

# --- a review whose checks returned no verdict is never reported as clean -----
# The model can self-report a verification item as unverified even while
# stating "complete"; that must downgrade the whole result to incomplete
# rather than being read as a clean review, the worst outcome this tool can
# produce.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
python3 - <<'PY'
import json
result = {
    "version": 1, "state": "complete",
    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "findings": [],
    "verification": [{
        "name": "bluefin-doctrine", "state": "unverified",
        "evidence": "parse check JSON: the model returned an empty response.",
    }],
}
event = {"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": json.dumps(result)}]}
]}
print(json.dumps(event))
PY
exit 0
EOF
chmod +x "$scratch/bin/omp"

set +e
incomplete_out="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-inc" "$review" pr projectbluefin/alpha 31 2>&1)"
incomplete_status=$?
set -e

# 65, not 0: the caller must be able to tell this apart from a clean review.
((incomplete_status == 65))
[[ "$incomplete_out" == *'INCOMPLETE'* ]]
grep -q 'bluefin-doctrine' <<<"$incomplete_out"
grep -q 'model returned an empty response' <<<"$incomplete_out"
# It must never also claim to be a finished draft.
[[ "$incomplete_out" != *'The Review Draft above is for you to judge'* ]]

# A run where every check verified stays clean, and stays exit 0.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
python3 - <<'PY'
import json
result = {
    "version": 1, "state": "complete",
    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "findings": [],
    "verification": [{
        "name": "bluefin-doctrine", "state": "verified", "evidence": "0 finding(s)",
    }],
}
event = {"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": json.dumps(result)}]}
]}
print(json.dumps(event))
PY
exit 0
EOF
chmod +x "$scratch/bin/omp"

set +e
clean_out="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-clean" "$review" pr projectbluefin/alpha 31 2>&1)"
clean_status=$?
set -e
((clean_status == 0))
[[ "$clean_out" == *'The Review Draft above is for you to judge'* ]]
[[ "$clean_out" != *'REVIEW INCOMPLETE'* ]]

# --- receipt mode emits a versioned machine-readable result receipt -----------
base_sha="$(printf '%040d' 0)"
head_sha="0123456789abcdef0123456789abcdef01234567"
receipt_json="$(
  PATH="$scratch/bin:$PATH" \
    BLUEFIN_REVIEW_HARNESS_ROOT="$repo_root/image" \
    "$review" receipt \
    --repository projectbluefin/alpha \
    --pull-request 31 \
    --base-sha "$base_sha" \
    --head-sha "$head_sha" \
    --backend omp \
    --model gemini-3.8-flash \
    --effort high \
    --check-scope-version scope-v7 \
    --workdir "$scratch/workspace/alpha"
)"
python3 - "$receipt_json" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
assert payload["version"] == 1
assert payload["identity"]["repository"] == "projectbluefin/alpha"
assert payload["identity"]["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
assert "live" not in payload["analysis"] or payload["analysis"]["live"] == {}
assert "overlap" not in payload["analysis"] or payload["analysis"]["overlap"] == {}
PY

# A zero-exit malformed stream is still an adapter failure, not a clean draft.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'malformed adapter output'
exit 0
EOF
chmod +x "$scratch/bin/omp"
set +e
malformed_status="$(PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  GH_CALLS="$scratch/gh-calls-malformed" "$review" pr projectbluefin/alpha 31 2>/dev/null)"
malformed_exit=$?
set -e
((malformed_exit != 0))
[[ "$malformed_status" == *'Review did not complete'* ]]
[[ "$malformed_status" != *'The Review Draft above is for you to judge'* ]]

# TERM requests adapter cancellation before the launcher cleans up. The
# adapter-created process group must not leave its child processes behind.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$$" >"${OMP_PID_FILE:?}"
exec sleep 1000
EOF
chmod +x "$scratch/bin/omp"
PATH="$scratch/bin:$PATH" HIVE_WORKSPACE_DIR="$scratch/workspace" \
  OMP_PID_FILE="$scratch/omp-pid" GH_CALLS="$scratch/gh-calls-signal" \
  "$review" pr projectbluefin/alpha 31 >"$scratch/signal-output" 2>&1 &
launcher_pid=$!
for _ in {1..50}; do
  [[ -s "$scratch/omp-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/omp-pid" ]]
omp_pid="$(<"$scratch/omp-pid")"
kill -TERM "$launcher_pid"
set +e
wait "$launcher_pid"
signal_exit=$?
set -e
((signal_exit != 0))
if ps -p "$omp_pid" -o comm= 2>/dev/null | grep -qx 'sleep'; then
  echo "OMP process survived launcher TERM: $omp_pid" >&2
  exit 1
fi

# The shipped local-range path must forward TERM to the adapter too. The
# adapter owns the process group and must terminate and wait for it.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s %s\n' "$$" "$(ps -o pgid= -p $$ | tr -d ' ')" >"${OMP_PID_FILE:?}"
exec sleep 1000
EOF
chmod +x "$scratch/bin/omp"
PATH="$scratch/bin:$PATH" OMP_PID_FILE="$scratch/local-term-pid" \
  "$review" main...HEAD >"$scratch/local-term-output" 2>&1 &
local_term_launcher=$!
for _ in {1..50}; do
  [[ -s "$scratch/local-term-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/local-term-pid" ]]
read -r local_term_omp local_term_pgid <"$scratch/local-term-pid"
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
  echo "local-range OMP process group survived TERM: $local_term_pgid" >&2
  exit 1
fi

# INT exercises the same shipped local-range path and preserves its distinct
# interrupt status while still requiring the adapter-owned group to be gone.
PATH="$scratch/bin:$PATH" OMP_PID_FILE="$scratch/local-int-pid" \
  env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/local-int-output" 2>&1 &
local_int_launcher=$!
for _ in {1..50}; do
  [[ -s "$scratch/local-int-pid" ]] && break
  sleep 0.1
done
[[ -s "$scratch/local-int-pid" ]]
read -r local_int_omp local_int_pgid <"$scratch/local-int-pid"
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
  echo "local-range OMP process group survived INT: $local_int_pgid" >&2
  exit 1
fi

# Completion-boundary signals must preserve the exact status without
# cancelling a completed adapter or abandoning its temporary review scope.
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$$" >"${OMP_PID_FILE:?}"
printf '%s\n' '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"{\"version\":1,\"state\":\"complete\",\"counts\":{\"critical\":0,\"high\":0,\"medium\":0,\"low\":0},\"findings\":[]}"}]}]}'
printf '%s\n' complete >"${OMP_COMPLETION_SENTINEL}.tmp"
mv "${OMP_COMPLETION_SENTINEL}.tmp" "$OMP_COMPLETION_SENTINEL"
exit 0
EOF
chmod +x "$scratch/bin/omp"
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
  case "${BASH_COMMAND-}" in
    'wait "$REVIEW_CHILD_PID"')
      [[ -n "${BOUNDARY_TRACE-}" ]] && printf '%s\n' "${BASH_COMMAND-}" >>"$BOUNDARY_TRACE"
      ;;
    'REVIEW_CHILD_PID=""')
      [[ -n "${BOUNDARY_TRACE-}" ]] && printf '%s\n' "${BASH_COMMAND-}" >>"$BOUNDARY_TRACE"
      if [[ "${BOUNDARY_PHASE-}" == adapter && -e "${OMP_COMPLETION_SENTINEL-}" ]]; then
        : >"${BOUNDARY_MARKER:?}"
        sleep 1
      fi
      ;;
    *'rm -rf "$scope"'*)
      if [[ "${BOUNDARY_PHASE-}" == scope ]]; then
        : >"${BOUNDARY_MARKER:?}"
        trap - DEBUG
        sleep 1
      fi
      ;;
  esac
  return 0
}
trap boundary_debug DEBUG
EOF

run_boundary_signal() {
  local signal="$1" expected="$2" label="$3"
  local trace="$scratch/boundary-$label-trace"
  rm -f "$scratch/boundary-marker" "$scratch/boundary-kills" "$scratch/boundary-pid" "$scratch/boundary-$label-sentinel" "$trace"
  if [[ "$signal" == INT ]]; then
    BASH_ENV="$scratch/debug-boundary.env" PATH="$scratch/bin:$PATH" \
      OMP_PID_FILE="$scratch/boundary-pid" KILL_LOG="$scratch/boundary-kills" \
      OMP_COMPLETION_SENTINEL="$scratch/boundary-$label-sentinel" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=adapter BOUNDARY_TRACE="$trace" \
      env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/boundary-$label-output" 2>&1 &
  else
    BASH_ENV="$scratch/debug-boundary.env" PATH="$scratch/bin:$PATH" \
      OMP_PID_FILE="$scratch/boundary-pid" KILL_LOG="$scratch/boundary-kills" \
      OMP_COMPLETION_SENTINEL="$scratch/boundary-$label-sentinel" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=adapter BOUNDARY_TRACE="$trace" "$review" main...HEAD \
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
  [[ "$(tail -n 2 "$trace" | head -n 1)" == "wait \"\$REVIEW_CHILD_PID\"" ]]
  [[ "$(tail -n 1 "$trace")" == 'REVIEW_CHILD_PID=""' ]]
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
      TMPDIR="$scratch/tmp" PATH="$scratch/bin:$PATH" OMP_PID_FILE="$scratch/boundary-pid" \
      OMP_COMPLETION_SENTINEL="$scratch/scope-$signal-sentinel" KILL_LOG="$scratch/boundary-kills" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=scope BOUNDARY_TRACE="$scratch/scope-$signal-trace" \
      env --default-signal=SIGINT setsid "$review" main...HEAD >"$scratch/scope-$signal-output" 2>&1 &
  else
    BASH_ENV="$scratch/debug-boundary.env" BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
      TMPDIR="$scratch/tmp" PATH="$scratch/bin:$PATH" OMP_PID_FILE="$scratch/boundary-pid" \
      OMP_COMPLETION_SENTINEL="$scratch/scope-$signal-sentinel" KILL_LOG="$scratch/boundary-kills" BOUNDARY_MARKER="$scratch/boundary-marker" BOUNDARY_PHASE=scope BOUNDARY_TRACE="$scratch/scope-$signal-trace" \
      "$review" main...HEAD >"$scratch/scope-$signal-output" 2>&1 &
  fi
  launcher=$!
  for _ in {1..50}; do
    [[ -e "$scratch/boundary-marker" ]] && break
    sleep 0.1
  done
  if [[ -e "$scratch/boundary-marker" ]]; then
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
  else
    wait "$launcher" 2>/dev/null || true
  fi
done

# restore the exit-code stub for the assertions that follow
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${OMP_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/omp"

# --- org review context is injected from the projected common skills ---------
# The org's review doctrine reaches it through prompt folding.
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

cat >"$scratch/bin/omp-capture" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${OMP_ARGV:?}"
printf '%s\n' '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"{\"version\":1,\"state\":\"complete\",\"counts\":{\"critical\":0,\"high\":0,\"medium\":0,\"low\":0},\"findings\":[]}"}]}]}'
EOF
chmod +x "$scratch/bin/omp-capture"
cp "$scratch/bin/omp-capture" "$scratch/bin/omp"

BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv" \
  "$review" main...HEAD >/dev/null

argv="$(tr '\0' '\n' <"$scratch/argv")"
[[ "$argv" == *"$scratch/skills/pr-review/SKILL.md"* ]]
[[ "$argv" == *"$scratch/skills/pr-review/references/"* ]]
[[ "$argv" != *'ORG_REVIEW_DOCTRINE_MARKER'* ]]
[[ "$argv" != *'ORG_REVIEW_REFERENCE_MARKER'* ]]

# Hive's knowledge export is named explicitly so a review can reach the hub's
# knowledge base directly.
printf 'HIVE_KB\n' >"$scratch/agent.md"
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/agent.md" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-kb" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-kb")" == *"$scratch/agent.md"* ]]

# The default must resolve an isolated HOME's agent.md when no explicit path is
# provided; otherwise a host knowledge file can mask a missing export.
default_home="$scratch/default-home"
mkdir -p "$default_home"
printf 'DEFAULT_HOME_KB\n' >"$default_home/agent.md"
env -u BLUEFIN_REVIEW_KNOWLEDGE_FILE \
  HOME="$default_home" BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-default-home" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-default-home")" == *"$default_home/agent.md"* ]]

# An absent knowledge file must not leave a dangling pointer in the prompt.
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/no-agent.md" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-nokb" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-nokb")" != *'knowledge base'* ]]

# Upstream writes a non-empty placeholder when the hub fetch fails; a size
# check alone would announce that dead export as the knowledge base.
printf 'Knowledge base not yet available.\n' >"$scratch/placeholder.md"
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/skills" \
  BLUEFIN_REVIEW_KNOWLEDGE_FILE="$scratch/placeholder.md" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-placeholder" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-placeholder")" != *'knowledge base'* ]]
# The range is folded into the instructions text, not a separate argument.
[[ "$(tr '\0' '\n' <"$scratch/argv")" == *"Review the diff at range main...HEAD"* ]]

# With no projected skills there is nothing to add, and the call stays bare.
BLUEFIN_REVIEW_SKILLS_ROOT="$scratch/absent" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-bare" \
  "$review" main...HEAD >/dev/null
[[ "$(tr '\0' '\n' <"$scratch/argv-bare")" == *"Review the diff at range main...HEAD"* ]]

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
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-repository" \
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
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-invalid" \
  "$review" main...HEAD >/dev/null
invalid_argv="$(tr '\0' '\n' <"$scratch/argv-invalid")"
[[ "$invalid_argv" == *'catalog unavailable'* ]]
[[ "$invalid_argv" != *'one.md'* && "$invalid_argv" != *'inactive.md'* ]]
[[ "$invalid_argv" != *'absolute.md'* && "$invalid_argv" != *'outside.md'* ]]

# restore the exit-code stub for any later assertions
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${OMP_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/omp"

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

# The --numbers mode feeds run_pr_review's cluster fetch: bare numbers, no
# prose, so the shell never parses a human sentence into a pull request number.
[[ "$(analyzer_out 1 numbers)" == *$'dupes\t2'* ]]
[[ "$(analyzer_out 4 numbers)" == *$'dupes\t5'* ]]
[[ "$(analyzer_out 3 numbers)" == *$'overlaps\t1 2'* ]]
[[ "$(analyzer_out 3 numbers)" != *'same dependency'* ]]

# --- the image review scope is folded into the instructions prompt -------------
# With the overlay present, review folds the static doctrine into the prompt
# directly (no temp dir, no --check-scope).
mkdir -p "$scratch/overlay/.agents/checks"
printf 'SCOPED REVIEW PROMPT\n' >"$scratch/overlay/.agents/REVIEW.md"
cp "$repo_root/image/review-scope/checks/"*.md \
  "$scratch/overlay/.agents/checks/"

cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${OMP_ARGV:?}"
printf '%s\n' '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"{\"version\":1,\"state\":\"complete\",\"counts\":{\"critical\":0,\"high\":0,\"medium\":0,\"low\":0},\"findings\":[]}"}]}]}'
EOF
chmod +x "$scratch/bin/omp"

BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_REPOSITORY_ROOT="$scratch/repository" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-scope" \
  "$review" main...HEAD >/dev/null

argv_scope="$(tr '\0' '\n' <"$scratch/argv-scope")"
[[ "$argv_scope" == *"SCOPED REVIEW PROMPT"* ]]
[[ "$argv_scope" == *"name: bluefin-doctrine"* ]]
[[ "$argv_scope" == *"name: security"* ]]
[[ "$argv_scope" == *"name: correctness"* ]]
[[ "$argv_scope" == *"name: test-coverage"* ]]
[[ "$argv_scope" == *"name: simplicity"* ]]
[[ "$argv_scope" == *"repository-owned context"* ]]

# A duplicate cluster adds the per-stop resolution note to the folded prompt.
BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_RELATED='These pull requests are the SAME work: #7' \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-scope2" \
  "$review" main...HEAD >/dev/null
argv_scope2="$(tr '\0' '\n' <"$scratch/argv-scope2")"
[[ "$argv_scope2" == *"These pull requests are the SAME work: #7"* ]]

# Maintainer steering from the dashboard's steer box reaches the folded prompt.
BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  BLUEFIN_REVIEW_STEER='check the CI permissions block' \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-scope3" \
  "$review" main...HEAD >/dev/null
argv_scope3="$(tr '\0' '\n' <"$scratch/argv-scope3")"
[[ "$argv_scope3" == *"check the CI permissions block"* ]]
[[ "$argv_scope3" == *"The maintainer is steering this review"* ]]
[[ "$argv_scope3" == *"name: bluefin-doctrine"* ]]

# Without a steer, no steering is added.
BLUEFIN_REVIEW_SCOPE_ROOT="$scratch/overlay" \
  PATH="$scratch/bin:$PATH" OMP_ARGV="$scratch/argv-scope4" \
  "$review" main...HEAD >/dev/null
argv_scope4="$(tr '\0' '\n' <"$scratch/argv-scope4")"
[[ "$argv_scope4" != *"The maintainer is steering this review"* ]]

# Save doctrine for alignment check below
cat "$scratch/overlay/.agents/checks/bluefin-doctrine.md" >"$scratch/scope-doctrine"

# Dynamic backend selection: codex backend probe and selection
cat >"$scratch/bin/codex" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"${CODEX_ARGV:?}"
python3 - <<'PY'
import json
result = {"version": 1, "state": "complete", "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}, "findings": [], "verification": [], "provenance": {}, "overlap": {}, "live": {}}
events = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "id": "m1", "text": json.dumps(result)}},
    {"type": "turn.completed", "usage": {}},
]
for event in events:
    print(json.dumps(event))
PY
exit 0
EOF
chmod +x "$scratch/bin/codex"

PATH="$scratch/bin:$PATH" CODEX_ARGV="$scratch/argv-codex" \
  BLUEFIN_REVIEW_BACKEND=codex \
  "$review" main...HEAD >/dev/null
argv_codex="$(tr '\0' '\n' <"$scratch/argv-codex")"
[[ "$argv_codex" == *"exec"* ]]
[[ "$argv_codex" == *"--dangerously-bypass-approvals-and-sandbox"* ]]

# restore the exit-code stub for any later assertions
cat >"$scratch/bin/omp" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >"${OMP_ARGS:?}"
exit 23
EOF
chmod +x "$scratch/bin/omp"

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
