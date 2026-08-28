#!/usr/bin/env bash
# Behavioral contract for the launcher-owned gVisor/runsc boundary (#348).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
justfile="$repo_root/justfile"
real_just="$(command -v just)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

fake_bin="$scratch/bin"
system_bin="$scratch/system-bin"
state_dir="$scratch/podman-state"
podman_log="$scratch/podman.log"
mkdir -p "$fake_bin" "$system_bin" "$state_dir"
for system_tool in bash basename cat grep mktemp ps rm rmdir sleep tr \
  mkdir dirname awk sed head tail sort wc cut env chmod touch ln date id uname; do
  ln -s "$(command -v "$system_tool")" "$system_bin/$system_tool"
done

# The launcher this contract exercises is the restored review runtime, which
# performs a backend preflight and a Hive registration lookup before it
# launches. Those are not what this suite is testing, so they are satisfied by
# a sandboxed HOME and stub commands -- never by the developer's real login,
# real Goose configuration, or real Hive registration.
fake_home="$scratch/home"
mkdir -p "$fake_home/.config/goose" "$fake_home/.config/hive"
cat >"$fake_home/.config/goose/config.yaml" <<'EOF'
active_provider: github_copilot
providers:
  github_copilot: {}
EOF
cat >"$fake_home/.config/hive/contributor.env" <<'EOF'
HIVE_HUB=wss://hive.example.invalid/contribute
AGENT_BACKEND=goose
EOF
chmod 600 "$fake_home/.config/hive/contributor.env"
cat >"$fake_bin/gh" <<'EOF'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "auth token") printf 'gho_isolation_contract_token\n' ;;
esac
exit 0
EOF
cat >"$fake_bin/goose" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$fake_bin/git" <<'EOF'
#!/usr/bin/env bash
# The pinned Hive checkout is pre-seeded, so nothing here should clone.
exit 0
EOF
chmod 0755 "$fake_bin/gh" "$fake_bin/goose" "$fake_bin/git"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

write_fake_runsc() {
  local behavior="${1:-ready}"
  rm -f "$fake_bin/runsc"
  if [[ "$behavior" == missing ]]; then
    return
  fi
  cat >"$fake_bin/runsc" <<'EOF'
#!/usr/bin/env bash
if [[ "${RUNSC_BEHAVIOR:-ready}" == unusable ]]; then
  echo 'runsc version probe failed' >&2
  exit 23
fi
printf 'runsc version test\n'
EOF
  if [[ "$behavior" == nonexecutable ]]; then
    chmod 0644 "$fake_bin/runsc"
  else
    chmod 0755 "$fake_bin/runsc"
  fi
}

cat >"$fake_bin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${PODMAN_LOG:?}"

runtime_selected=false
if [[ "${1:-}" == --runtime=runsc ]]; then
  runtime_selected=true
  shift
fi

command="${1:-}"
shift || true
case "$command" in
image)
  exit 1
  ;;
info)
  printf '%s\n' "${PODMAN_ROOTLESS:-true}"
  ;;
manifest | pull | stop)
  exit 0
  ;;
container)
  if [[ "${1:-}" == exists && -f "${PODMAN_STATE_DIR:?}/${2:-}.exists" ]]; then
    exit 0
  fi
  [[ "${PODMAN_CONTAINER_EXISTS:-0}" == 1 ]]
  ;;
inspect)
  if [[ "$*" == *'review.owner'* ]]; then
    printf '%s\n' "${PODMAN_OWNER_LABEL:-}"
    exit 0
  fi
  if [[ "$*" == *'.OCIRuntime'* ]]; then
    if [[ "$*" == *'.State.Running'* ]]; then
      printf '%s %s\n' "${PODMAN_PROBE_RUNNING:-true}" "${PODMAN_PROBE_RUNTIME:-runsc}"
    else
      printf '%s\n' "${PODMAN_PROBE_RUNTIME:-runsc}"
    fi
    exit 0
  fi
  if [[ "$*" == *'review.probe'* ]]; then
    target="${*: -1}"
    cat "${PODMAN_STATE_DIR:?}/${target}.label"
    exit 0
  fi
  printf 'false\n'
  exit 1
  ;;
run)
  if [[ "$*" == *'--name review-runtime-probe-'* ]]; then
    [[ "$runtime_selected" == true ]] || exit 91
    case "${PODMAN_PROBE_BEHAVIOR:-ready}" in
    reject | collision)
      echo 'configured OCI runtime runsc is unavailable' >&2
      exit 125
      ;;
    nonzero)
      echo 'probe command failed under runsc' >&2
      exit 42
      ;;
    esac
    cidfile=
    probe_label=
    while (($#)); do
      case "$1" in
      --cidfile)
        cidfile="$2"
        shift 2
        ;;
      --label)
        case "$2" in review.probe=*) probe_label="${2#review.probe=}" ;; esac
        shift 2
        ;;
      *) shift ;;
      esac
    done
    [[ -n "$cidfile" && -n "$probe_label" ]] || exit 92
    probe_id="probe-id-${probe_label}"
    printf '%s\n' "$probe_id" >"$cidfile"
    printf '%s\n' "$probe_label" >"${PODMAN_STATE_DIR:?}/${probe_id}.label"
    : >"${PODMAN_STATE_DIR:?}/${probe_id}.exists"
    printf '%s\n' "$probe_id" >"${PODMAN_STATE_DIR:?}/last-created-id"
    if [[ "${PODMAN_PROBE_BEHAVIOR:-ready}" == signal ]]; then
      recipe_pid="$(ps -o ppid= -p "$PPID" | tr -d '[:space:]')"
      kill -TERM "$recipe_pid"
    fi
    exit 0
  fi
  exit 0
  ;;
rm)
  target="${*: -1}"
  if [[ "${PODMAN_CLEANUP_BEHAVIOR:-ready}" == fail ]]; then
    exit 1
  fi
  rm -f "${PODMAN_STATE_DIR:?}/${target}.exists" "${PODMAN_STATE_DIR:?}/${target}.label"
  ;;
esac
exit 0
EOF
chmod 0755 "$fake_bin/podman"

cat >"$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'timeout %s\n' "$*" >>"${PODMAN_LOG:?}"
if [[ "${1:-}" == --kill-after=* ]]; then
  shift
fi
shift
if [[ "$*" == *'--runtime=runsc run'* ]]; then
  case "${TIMEOUT_BEHAVIOR:-ready}" in
  expire) exit 124 ;;
  interrupt) exit 130 ;;
  esac
fi
if [[ "${TIMEOUT_BEHAVIOR:-ready}" == cleanup-timeout && "$*" == *'container exists'* ]]; then
  exit 124
fi
exec "$@"
EOF
chmod 0755 "$fake_bin/timeout"

run_recipe() {
  local recipe="$1"
  shift
  : >"$podman_log"
  rm -f "$state_dir"/*
  set +e
  output="$(
    env PATH="$fake_bin:$system_bin" PODMAN_LOG="$podman_log" \
      PODMAN_STATE_DIR="$state_dir" \
      HOME="$fake_home" \
      GITHUB_COPILOT_TOKEN=isolation-contract-copilot-token \
      REVIEW_GH_TOKEN=isolation-contract-gh-token \
      "$@" \
      "$real_just" --justfile "$justfile" "$recipe" 2>&1
  )"
  status=$?
  set -e
}

assert_no_agent_launch() {
  if grep -Eq -- '--name (review-container|review-queue)( |$)' "$podman_log"; then
    fail "isolation failure must happen before an agent container starts"
  fi
  if grep -Eq -- '--env |--volume ' "$podman_log"; then
    fail "isolation failure must happen before credentials are mounted"
  fi
}

assert_probe_absent() {
  if find "$state_dir" -name '*.exists' -print -quit | grep -q .; then
    fail "the disposable runsc probe must not survive its cleanup trap"
  fi
  if [[ -s "$state_dir/last-created-id" ]]; then
    probe_id="$(cat "$state_dir/last-created-id")"
    grep -Eq -- "^rm --force ${probe_id}$" "$podman_log" ||
      fail "cleanup must target the cidfile-owned probe ID"
    ! grep -Eq -- '^rm --force review-runtime-probe-' "$podman_log" ||
      fail "cleanup must never force-remove a probe by its public name"
  fi
}

assert_failure_copy() {
  local classification="$1"
  [[ "$status" -ne 0 ]] || fail "isolation failure must exit nonzero"
  grep -Fq "isolation runtime: gVisor/runsc (${classification})" <<<"$output" ||
    fail "isolation failure must report classification: ${classification}"
  grep -Fq 'did not start an agent or mount credentials' <<<"$output" ||
    fail "isolation failure must confirm credentials were not mounted"
  grep -Fq 'projectbluefin/bluefin/issues/1139' <<<"$output" ||
    fail "isolation failure must link the Bluefin provisioning issue"
  assert_no_agent_launch
  assert_probe_absent
}

case_missing() {
  write_fake_runsc missing
  run_recipe review-container
  assert_failure_copy missing
  ! grep -Eq '^(pull|manifest) ' "$podman_log" ||
    fail "runtime resolution must fail before image retrieval"
}

case_nonexecutable() {
  write_fake_runsc nonexecutable
  run_recipe review-container
  assert_failure_copy missing
  ! grep -Eq '^(pull|manifest) ' "$podman_log" ||
    fail "the executable check must fail before image retrieval"
}

case_unusable() {
  write_fake_runsc ready
  run_recipe review-container RUNSC_BEHAVIOR=unusable
  assert_failure_copy 'installed but unusable'
  ! grep -Eq '^(pull|manifest) ' "$podman_log" ||
    fail "the runsc version check must fail before image retrieval"
}

case_podman_rejects() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_BEHAVIOR=reject
  assert_failure_copy 'incompatible with this host/Podman configuration'
}

case_probe_nonzero() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_BEHAVIOR=nonzero
  assert_failure_copy 'installed but unusable'
}

case_false_positive() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_RUNTIME=crun
  assert_failure_copy 'incompatible with this host/Podman configuration'
}

case_probe_stopped() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_RUNNING=false
  assert_failure_copy 'installed but unusable'
}

case_probe_timeout() {
  write_fake_runsc ready
  run_recipe review-container TIMEOUT_BEHAVIOR=expire
  assert_failure_copy 'installed but unusable'
}

case_probe_interrupt() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_BEHAVIOR=signal
  assert_failure_copy 'installed but unusable'
  [[ -s "$state_dir/last-created-id" ]] ||
    fail "the interruption test must signal a live, created probe"
}

case_name_collision() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_PROBE_BEHAVIOR=collision
  assert_failure_copy 'incompatible with this host/Podman configuration'
  ! grep -Eq -- '^rm --force review-runtime-probe-' "$podman_log" ||
    fail "a failed create must never remove a colliding container name"
}

case_cleanup_failure() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_CLEANUP_BEHAVIOR=fail
  [[ "$status" -ne 0 ]] || fail "cleanup failure must fail closed"
  ! grep -Fq '✓ isolation runtime: gVisor/runsc' <<<"$output" ||
    fail "cleanup failure must never report runtime readiness"
  [[ -s "$state_dir/last-created-id" ]] ||
    fail "the cleanup-failure test must reach a live, created probe"
  grep -Fq 'probe cleanup failed' <<<"$output" ||
    fail "cleanup failure must be reported as the failed security check"
  assert_no_agent_launch
}

case_cleanup_timeout() {
  write_fake_runsc ready
  run_recipe review-container TIMEOUT_BEHAVIOR=cleanup-timeout
  [[ "$status" -ne 0 ]] || fail "cleanup status timeout must fail closed"
  grep -Fq 'probe cleanup failed' <<<"$output" ||
    fail "cleanup status timeout must be reported as the failed security check"
  assert_no_agent_launch
}

case_rootful() {
  write_fake_runsc ready
  run_recipe review-container PODMAN_ROOTLESS=false
  assert_failure_copy 'incompatible with this host/Podman configuration'
  ! grep -Eq '^(pull|manifest) ' "$podman_log" ||
    fail "the rootless check must fail before image retrieval"
}

assert_successful_agent_launch() {
  local name="$1"
  [[ "$status" -eq 0 ]] || fail "${name} must launch after the runsc probe passes"
  grep -Fq '✓ isolation runtime: gVisor/runsc' <<<"$output" ||
    fail "${name} must report the active isolation runtime"
  grep -Eq -- "^--runtime=runsc run .*--name ${name}( |$)" "$podman_log" ||
    fail "${name} must explicitly select runsc"
  grep -Eq -- '^--runtime=runsc run .*--name review-runtime-probe-' "$podman_log" ||
    fail "${name} must execute the rootless runsc probe"
  grep -Eq -- '^inspect --format .*OCIRuntime.*probe-id-' "$podman_log" ||
    fail "${name} must verify the probe container runtime identity"
  grep -Eq -- '^inspect --format .*State.Running.*OCIRuntime.*probe-id-' "$podman_log" ||
    fail "${name} must verify runtime identity while the probe is active"
  grep -Eq -- '^inspect --format .*review.probe.*probe-id-' "$podman_log" ||
    fail "${name} must bind authoritative evidence and cleanup to the ownership label"
  assert_probe_absent
  grep -Eq -- '^--runtime=runsc run .*--rm .*--cidfile .*--label review.probe=' "$podman_log" ||
    fail "the probe must carry --rm plus cidfile and ownership-label defenses"
  grep -Eq -- '^timeout --kill-after=2s (5s|20s) ' "$podman_log" ||
    fail "probe operations must escalate from TERM to KILL within a fixed bound"
}

case_attended() {
  write_fake_runsc ready
  run_recipe review-container
  assert_successful_agent_launch review-container
}

case_detached() {
  write_fake_runsc ready
  run_recipe review-container REVIEW_DETACH=1
  assert_successful_agent_launch review-container
  grep -Eq -- '^--runtime=runsc run .*--detach .*review.owner=detached' "$podman_log" ||
    fail "the detached worker must preserve its explicit lifecycle and owner label"
}

case_queue() {
  write_fake_runsc ready
  run_recipe review-queue
  assert_successful_agent_launch review-queue
}

case_doctor() {
  write_fake_runsc ready
  run_recipe review-doctor
  [[ "$status" -eq 0 ]] || fail "review-doctor must pass after the runsc probe"
  grep -Fq 'isolation runtime: gVisor/runsc (ready; rootless Podman probe passed)' <<<"$output" ||
    fail "review-doctor must report runsc readiness"
  grep -Fq 'disposable credential-free agent-free probe removed' <<<"$output" ||
    fail "review-doctor must describe its non-persistent diagnostic probe honestly"
  grep -Eq -- '^--runtime=runsc run .*--name review-runtime-probe-' "$podman_log" ||
    fail "review-doctor must execute the rootless runsc probe"
}

run_case() {
  case "$1" in
  missing) case_missing ;;
  nonexecutable) case_nonexecutable ;;
  unusable) case_unusable ;;
  podman-rejects) case_podman_rejects ;;
  probe-nonzero) case_probe_nonzero ;;
  false-positive) case_false_positive ;;
  probe-stopped) case_probe_stopped ;;
  probe-timeout) case_probe_timeout ;;
  probe-interrupt) case_probe_interrupt ;;
  name-collision) case_name_collision ;;
  cleanup-failure) case_cleanup_failure ;;
  cleanup-timeout) case_cleanup_timeout ;;
  rootful) case_rootful ;;
  attended) case_attended ;;
  detached) case_detached ;;
  queue) case_queue ;;
  doctor) case_doctor ;;
  *) fail "unknown runsc contract case: $1" ;;
  esac
}

if (($#)); then
  for requested_case in "$@"; do
    run_case "$requested_case"
  done
else
  for requested_case in \
    missing nonexecutable unusable podman-rejects probe-nonzero false-positive \
    probe-stopped probe-timeout probe-interrupt name-collision cleanup-failure \
    cleanup-timeout rootful \
    attended detached queue doctor; do
    run_case "$requested_case"
  done
fi

printf 'runsc isolation launcher contract OK\n'
