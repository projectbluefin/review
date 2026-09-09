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

python3 "$repo_root/tests/hive_api_contract.py"
python3 "$repo_root/tests/review_scheduler_contract.py"
python3 "$repo_root/tests/review_engine_contract.py"
python3 "$repo_root/tests/review_transport_contract.py"
python3 "$repo_root/tests/review_deadline_contract.py"
python3 "$repo_root/tests/capacity_contract.py"
python3 "$repo_root/tests/model_profiles_contract.py"
python3 "$repo_root/tests/run_state_contract.py"
python3 "$repo_root/tests/gh_client_contract.py"
python3 "$repo_root/tests/landing-probe-contract.py"
python3 "$repo_root/tests/import_root_contract.py"

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

# Batch review is read-only orchestration. It may hydrate live GitHub evidence
# and dispatch ReviewEngine, but it must not reach any mutation gate.
batch_review="$(sed -n '/def start_review_batch/,/def on_key/p' "$tui")"
grep -q 'self\.mutate' <<<"$batch_review" &&
  fail "batch review must not invoke a GitHub mutation gate"

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
  fail "the landing queue must have a repository-aware dispatcher"

# The landing agent's brief keeps the mutation rules: no drafts, no failing
# required checks, no branch-protection bypass, per-PR JSONL status the
# screen polls instead of scraped prose.
landing_py="$repo_root/image/tui/landing.py"
model_profiles_py="$repo_root/image/tui/model_profiles.py"
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
# The image ships no registry inspector until fsdk-containers#164 lands in
# the base, so neither the brief nor the skills may instruct one.
for doc in "$landing_py" \
  "$repo_root/docs/skills/review-dashboard.md" \
  "$repo_root/docs/skills/image-build.md"; do
  grep -q 'skopeo inspect' "$doc" &&
    fail "$(basename "$doc") must not instruct skopeo; the image does not ship it (fsdk-containers#164)"
done
# The brief's verification is the anonymous ghcr flow through the module's
# probe command: the mint, pagination, and content negotiation live in code
# so a denied token mint can never be masked by a shell pipeline (#375).
grep -q 'def probe_package' "$landing_py" ||
  fail "the landing module must ship the anonymous ghcr probe"
grep -q '/token?scope=repository:' "$landing_py" ||
  fail "the probe must use the anonymous ghcr token flow"
grep -q 'probe --package' "$landing_py" ||
  fail "the landing brief must instruct the probe command"
# This appliance owns no lab: the brief must treat an unreachable external
# check service as infrastructure unavailability and substitute ghcr
# evidence — never a blocked pull request.
grep -q 'infrastructure' "$landing_py" ||
  fail "the landing brief must treat an unreachable check service as infrastructure unavailability"
grep -q 'substitute evidence' "$landing_py" ||
  fail "the landing brief must verify an unreachable check's deliverable in ghcr"
grep -q 'owns no lab and depends on none' "$landing_py" ||
  fail "the landing brief must state the appliance depends on no lab"
grep -q 'owns no lab' "$repo_root/AGENTS.md" ||
  fail "AGENTS.md must codify that nothing gates on maintainer-local infrastructure"
grep -qiE 'ghost' "$landing_py" &&
  fail "the landing brief must not carry ghost-lab special cases"
# A path-filtered, scheduled, or manual publish workflow owes no publication
# for a merge outside its triggers: the merge itself is the deliverable.
grep -q 'path-filtered' "$landing_py" ||
  fail "the landing brief must cover conditional publish workflows"
grep -q 'no publication of it exists' "$landing_py" ||
  fail "the landing brief must not fail a merge that owes no publication"

# The status record has exactly one writer: the landing module's report CLI.
# It serializes under flock, writes a terminal state once, and closes the
# batch only when every selected pull request has a terminal outcome (#377).
grep -q 'fcntl.flock' "$landing_py" ||
  fail "the landing reporter must serialize status writes under flock"
grep -q 'add_parser("report"' "$landing_py" ||
  fail "the landing module must ship the report CLI the brief instructs"
grep -q 'report --status' "$landing_py" ||
  fail "the landing brief must route status writes through the report CLI"
grep -q 'no printf' "$landing_py" ||
  fail "the landing brief must forbid direct status-file writes"
grep -q 'written once' "$landing_py" ||
  fail "the landing brief must define a terminal state as written once"
# The token probe must not pipe curl into jq: without pipefail the pipeline
# reports jq's status, and a denied mint reads as a successful one (#375).
grep -qE 'curl[^|]*\| *jq' "$landing_py" &&
  fail "the token mint must not pipe curl into jq — jq masks a denied mint (#375)"
grep -q 'never evidence of absence' "$landing_py" ||
  fail "a probe that cannot answer must never read as a missing package"
# A ghcr.io mention is not a publish signal: the brief must require a real
# publication path targeting the repository's own package, the wait must end
# when the identified workflow's runs are terminal, and an empty run list is
# never evidence — runs can lag the merge (#376).
grep -q 'on.push' "$landing_py" ||
  fail "the publish signal must be an on.push publication path"
grep -q 'workflow_call' "$landing_py" ||
  fail "reusable workflows must not count as a publication path"
grep -q 'workflow_run' "$landing_py" ||
  fail "workflow_run-triggered publishes must be covered"
grep -q 'release' "$landing_py" ||
  fail "release-triggered publishes must be covered"
grep -q 'an empty run list is never evidence' "$landing_py" ||
  fail "an empty run list must not read as 'no publication'"
grep -q 'publish-verdict' "$landing_py" ||
  fail "the wait/stop decision must route through the publish-verdict command"
grep -q 'stop polling' "$landing_py" ||
  fail "the publish wait must stop once terminal runs prove no publication is owed"

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

# ── the optional lab is optional, and holds no credential (#379) ─────────
# The container half must never reach for a cluster directly: it has no
# kubeconfig, no kubectl, and no argo, by design.
lab_client="$repo_root/image/tui/lab_client.py"
broker="$repo_root/scripts/review-lab-broker.py"
grep -qE '\b(kubectl|kubeconfig|argo|k8sgpt)\b' "$lab_client" &&
  fail "the container-side lab client must not name a host cluster tool"
grep -qE '\bsubprocess\b|\bos\.system\b' "$lab_client" &&
  fail "the lab client speaks the socket protocol, never a local command"
grep -q 'LAB_DEGRADED' "$lab_client" ||
  fail "an unreachable broker must degrade rather than answer cleanly"
grep -q "usb4-link-observed-at" "$broker" ||
  fail "the USB4 predicate must read the observation timestamp"
grep -q '45' "$broker" ||
  fail "the USB4 freshness window must be the documented 45 seconds"
# The dashboard renders the word; the bolt is decoration on top of it.
grep -q 'LAB ⚡ ACTIVE' "$tui" ||
  fail "the status area must carry the lab state as text plus the glyph"
grep -qE 'lab_client\.(status|lab_state)' "$tui" ||
  fail "the dashboard must read lab state through the client"
grep -q 'set_interval(30.0, self.poll_lab)' "$tui" ||
  fail "the lab must be polled coarsely, not on the dashboard's own pace"
# Filing is a narrow machine authority with fixed destinations.
grep -q 'projectbluefin/lab' "$broker" ||
  fail "cluster-platform findings must route to projectbluefin/lab"
grep -q 'projectbluefin/server' "$broker" ||
  fail "server-product findings must route to projectbluefin/server"
grep -q 'unroutable' "$broker" ||
  fail "an ambiguous finding must file nothing"

# ── the final review runs in the existing lane (#378) ───────────────────
grep -q 'class FinalPolicyScreen' "$tui" ||
  fail "the session's final-review policy must be one explicit gate"
grep -q 'landing_draining' "$tui" ||
  fail "one drainer must own the landing lane, or a round runs twice"
grep -qE 'self\.[a-z_]*queue: list\[landing\.LandingTask\]' "$tui" ||
  fail "the final review must reuse the landing queue, not add a second one"
grep -cE '^\s+self\.[a-z_]+_queue: list' "$tui" | grep -qx 1 ||
  fail "the dashboard must own exactly one agent queue"
grep -q 'FINAL_ROUND_LIMIT = 5' "$landing_py" ||
  fail "the five-round breaker must be a constant in the record's writer"
grep -q 'is outside 1\.\.' "$landing_py" ||
  fail "the record itself must refuse a round past the limit"
grep -q 'already review-blocked\|already {rounds\[-1\]' "$landing_py" ||
  fail "nothing may be written after the final phase closes"
grep -q 'GOOSE_MODEL' "$landing_py" "$model_profiles_py" 2>/dev/null ||
  fail "a Goose round must carry its model explicitly"
grep -q 'BLUEFIN_REVIEW_FINAL_MODEL' "$landing_py" "$model_profiles_py" 2>/dev/null ||
  fail "a Codex round must not be handed Goose variables that do nothing"
# shellcheck disable=SC2016 # single quotes are intentional for literal markdown backticks
grep -q 'never force-push, never remove a hold' "$landing_py" ||
  fail "a fix round must be told never to bypass branch protection"

# Queueing goes through Hive's authenticated mutation endpoint. Hive owns the
# App-authored exact-head approval and queue label; a human gh review cannot
# satisfy the governor's authorship contract (#247).
grep -q '/api/v1/prs/{owner}/{repository}/{stop.number}/queue-automerge' "$tui" ||
  fail "queueing must call Hive's queue-automerge endpoint"
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
grep -q 'GHOST_BUILD_ISSUE' "$tui" &&
  fail "ghost build dispatch must not exist — this appliance owns no lab"
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

# --- batch engine and broker contracts ---------------------------------------
# These remaining suites import image/tui modules that never reach Textual, so
# they run on the system interpreter, before the venv is built. The capacity
# and review-engine contracts already run at the top of this script; keep each
# suite to one invocation.
python3 "$repo_root/tests/review_cache_contract.py"
python3 "$repo_root/tests/review_receipt_contract.py"
python3 "$repo_root/tests/lab-broker-contract.py"
python3 "$repo_root/tests/review-exec-broker-contract.py"

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
"${venv}/bin/python" "$repo_root/tests/review_run_contract.py"
"${venv}/bin/python" "$repo_root/tests/review_evidence_manifest_contract.py"
"${venv}/bin/python" "$repo_root/tests/action_plan_contract.py"
"${venv}/bin/python" "$repo_root/tests/re_review_contract.py"
"${venv}/bin/python" "$repo_root/tests/semantic_view_contract.py"
# These current-main suites remain in the pinned Textual environment.
"${venv}/bin/python" "$repo_root/tests/slay_state_contract.py"
"${venv}/bin/python" "$repo_root/tests/soak_contract.py"
"${venv}/bin/python" "$repo_root/tests/observability_contract.py"
"${venv}/bin/python" "$repo_root/tests/review_session_runtime_contract.py"
# review_snapshot_contract.py imports bluefin_review_tui for Stop, so it needs
# the Textual venv rather than the stdlib-only step in validate.yml.
"${venv}/bin/python" "$repo_root/tests/review_snapshot_contract.py"
"${venv}/bin/python" "$repo_root/tests/mixed_workboard_contract.py"
"${venv}/bin/python" "$repo_root/tests/dashboard_pilot.py"

# Fails when a file under tests/ is not reachable from validate.yml, so a new
# suite cannot land and then sit unexecuted the way seven of these did.
bash "$repo_root/tests/test-registry.sh"

printf 'dashboard contract OK\n'
