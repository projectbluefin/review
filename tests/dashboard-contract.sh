#!/usr/bin/env bash
# Contract checks for the maintainer dashboard (image/tui/bluefin_review_tui.py).
#
# Two layers, and the order matters. The pilot below drives the real Textual
# app: it presses keys, waits for the review screen to reach a terminal state,
# and asserts what the maintainer is actually told. That is the layer that
# catches a binding pointing at nothing, or a failed review reported as a clean
# one — both of which a source-text grep passes happily.
#
# The static assertions that remain are the ones about absence: a power the
# dashboard must never have cannot be proven missing by exercising it.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tui="$repo_root/image/tui/bluefin_review_tui.py"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# --- absence: powers the dashboard must not have -----------------------------
# No protection bypass, no branch deletion, no force.
grep -q -- '--admin' "$tui" && fail "the dashboard must never bypass branch protections with --admin"
grep -q -- '--delete-branch' "$tui" && fail "the dashboard must never delete branches"
grep -qE '"push"|git push' "$tui" && fail "the dashboard must never push"

# A maintainer may merge without arming automation: `lgtm` is an opt-in to
# Hive's sweep, not a toll on merging. That power is exactly one gated call
# site, it squashes like the sweep does, and it is asked of GitHub — the
# 'push' permission — rather than assumed from having the dashboard open.
[[ "$(grep -c '"pr", "merge"' "$tui")" -eq 1 ]] ||
  fail "exactly one merge site: the maintainer's gated direct merge"
merge_now="$(sed -n '/def action_merge_now/,/def action_reject/p' "$tui")"
grep -q -- '"--squash"' <<<"$merge_now" ||
  fail "the direct merge must squash, like the sweep it stands beside"
grep -q 'merge_rights\[stop.repository\]' <<<"$merge_now" ||
  fail "the direct merge must check the maintainer permission before running"
grep -q 'self.mutate_all' <<<"$merge_now" ||
  fail "the direct merge must go through the typed-number gate"
grep -q 'permissions.push' "$tui" ||
  fail "maintainer permission must be read from GitHub, not assumed"
# The lgtm path stays what it is: an opt-in, never a precondition for merging.
grep -q 'isDraft' <<<"$merge_now" ||
  fail "the direct merge must refuse drafts"

# Every mutating verb must be an argument of self.mutate(), never of the
# read-only gh() helper.
if grep -nE 'gh\("pr", "(merge|close|comment|edit|review)"' "$tui"; then
  fail "mutating gh verbs must go through self.mutate(), not the gh() reader"
fi

# Process-execution sites: the read-only gh() reader, the gated executor
# inside mutate(), the review engine the review screen streams, and the
# batch landing agent the queue drains one at a time.
[[ "$(grep -c 'subprocess.run' "$tui")" -eq 2 ]] ||
  fail "expected exactly two subprocess.run sites (gh() reader and mutate() executor)"
[[ "$(grep -c 'subprocess.Popen(' "$tui")" -eq 2 ]] ||
  fail "expected exactly two subprocess.Popen sites (the streamed review and the landing agent)"

# The batch path: a selected batch opens the proportionate plan gate (Enter,
# no typed count) and dispatches one agent for the whole selection.
grep -q 'class BatchPlanScreen' "$tui" ||
  fail "the batch plan gate must exist"
grep -q 'class LandingScreen' "$tui" ||
  fail "the live batch queue screen must exist"
grep -q 'landing.new_task(batch, self.self_login)' "$tui" ||
  fail "a selected batch must become one landing task"
grep -q 'self.enqueue_landing(task)' "$tui" ||
  fail "a confirmed batch must enter the landing queue"
grep -q 'def drain_landings' "$tui" ||
  fail "the landing queue must drain one agent at a time"

# The landing agent's brief keeps the mutation rules: no drafts, no failing
# required checks, no branch-protection bypass, per-PR JSONL status the
# screen polls instead of scraped prose.
landing_py="$repo_root/image/tui/landing.py"
grep -q 'never pass' "$landing_py" ||
  fail "the landing brief must state what the agent may not do"
grep -q -- '--admin' "$landing_py" ||
  fail "the landing brief must forbid branch-protection bypass"
grep -q 'Never merge a draft' "$landing_py" ||
  fail "the landing brief must forbid merging drafts"
grep -q 'def parse_status' "$landing_py" ||
  fail "the landing module must parse the agent's JSONL status report"
grep -q 'BLUEFIN_REVIEW_LANDING_COMMAND' "$landing_py" ||
  fail "the landing command must be overridable for tests"
grep -q 'awaiting-stable' "$landing_py" ||
  fail "a merge is not done until :stable carries the change"
grep -q ':stable' "$landing_py" ||
  fail "the landing brief must define done as :stable published"

# The gate is the typed pull request number: no y/yes, no timeout.
grep -q 'class ConfirmMutation' "$tui" || fail "the ConfirmMutation gate must exist"
grep -q 'ConfirmMutation(commands, str(stop.number))' "$tui" ||
  fail "mutate_all() must confirm with the pull request number"
# One decision, one gate: a multi-command sequence must never be assembled by
# chaining gated mutations through their completion callback.
grep -qE 'then=lambda: self\.mutate' "$tui" &&
  fail "a mutation sequence must be one gated sequence, not chained gates"
grep -qiE '\(y/n\)|yes/no' "$tui" && fail "no y/yes confirmation shortcut"
grep -q 'Binding("l", "labels"' "$tui" &&
  fail "the dashboard must not bind a label overlay"
grep -q 'Binding("p", "priority"' "$tui" &&
  fail "the dashboard must not bind priority cycling"
grep -q '\[b\]l\[/b\]' "$tui" &&
  fail "the acting key line must not advertise label mutation"
grep -q '\[b\]p\[/b\]' "$tui" &&
  fail "the acting key line must not advertise priority mutation"

# Queueing goes through Hive's authenticated mutation endpoint. Hive owns the
# App-authored exact-head approval and queue label; a human gh review cannot
# satisfy the governor's authorship contract (#247).
grep -q '/api/prs/{owner}/{repository}/{stop.number}/queue-automerge' "$tui" ||
  fail "queueing must call Hive's queue-automerge endpoint"
grep -q 'QUEUE_LABEL = "lgtm"' "$tui" ||
  fail "the sweep's label must still be lgtm"
queue_body="$(sed -n '/def _queue_automerge/,/def action_merge/p' "$tui")"
grep -q '"gh", "pr", "review"' <<<"$queue_body" &&
  fail "queueing must never submit a human-authored approval"
# The maintainer's ordinary review path remains separate: approve, request
# changes, or comment, and it neither merges nor arms automation.
leave_review="$(sed -n '/def leave_review/,/def action_leave_review/p' "$tui")"
grep -q 'self.mutate_all' <<<"$leave_review" ||
  fail "leaving a review must go through the typed-number gate"
grep -q '"request-changes"' "$tui" ||
  fail "a reviewer must be able to request changes, not only approve"
grep -q 'f"--{verdict}"' "$tui" ||
  fail "the chosen verdict must be what gh is told to submit"
grep -q -- '"--add-label"' <<<"$leave_review" &&
  fail "leaving a review must not apply the lgtm automation opt-in"
grep -q 'authorAssociation' "$tui" ||
  fail "reviewer standing must come from GitHub's author association"

# Drafts are refused from live evidence, and every mutation invalidates cache.
grep -q 'isDraft' "$tui" || fail "merge must refuse drafts from live evidence"
grep -q 'pulls_cache.pop' "$tui" || fail "mutations must invalidate the pull cache"

# Tracked gaps are named as issues, not silent stubs.
grep -q 'GHOST_BUILD_ISSUE = "projectbluefin/review#' "$tui" ||
  fail "the ghost-build stub must name its tracking issue"
grep -q 'DOCS_UPDATE_ISSUE = "projectbluefin/review#' "$tui" ||
  fail "the docs-update stub must name its tracking issue"

# The handoff key is read-only: it copies through Textual's clipboard API
# (OSC 52) and never mutates.
grep -q 'def action_handoff' "$tui" || fail "the handoff action must exist"
handoff_body="$(sed -n '/def action_handoff/,/def action_resolve_cluster/p' "$tui")"
grep -q 'copy_to_clipboard' <<<"$handoff_body" ||
  fail "handoff must copy through the app clipboard (OSC 52)"
if grep -qE 'self\.mutate|subprocess' <<<"$handoff_body"; then
  fail "handoff must stay read-only: no mutation gate, no process execution"
fi

# --- behaviour: drive the real app -------------------------------------------
# Textual at the version the image installs, from the same hash-locked file the
# image build uses, so the pilot exercises the runtime that ships.
venv="${BLUEFIN_REVIEW_TUI_VENV:-${repo_root}/.cache/tui-venv}"
lock="$repo_root/image/tui/requirements.lock"
stamp="${venv}/.lock-sha256"
want="$(sha256sum "$lock" | cut -d' ' -f1)"

if [[ ! -x "${venv}/bin/python" ]] || [[ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]]; then
  echo "dashboard contract: building the pinned Textual venv at ${venv}"
  rm -rf "$venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet "$venv"
    uv pip install --quiet --python "${venv}/bin/python" \
      --require-hashes --no-deps -r "$lock"
  else
    python3 -m venv "$venv"
    "${venv}/bin/python" -m pip install --quiet --upgrade pip
    "${venv}/bin/python" -m pip install --quiet --require-hashes --no-deps -r "$lock"
  fi
  printf '%s' "$want" >"$stamp"
fi

"${venv}/bin/python" -m py_compile "$tui"
"${venv}/bin/python" "$repo_root/tests/review_result_contract.py"
"${venv}/bin/python" "$repo_root/tests/review_evidence_manifest_contract.py"
"${venv}/bin/python" "$repo_root/tests/review_evidence_manifest_unit.py"
"${venv}/bin/python" "$repo_root/tests/action_plan_contract.py"
"${venv}/bin/python" "$repo_root/tests/re_review_contract.py"
"${venv}/bin/python" "$repo_root/tests/semantic_view_contract.py"
"${venv}/bin/python" "$repo_root/tests/dashboard_pilot.py"

printf 'dashboard contract OK\n'
