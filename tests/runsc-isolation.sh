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
podman_log="$scratch/podman.log"
mkdir -p "$fake_bin" "$system_bin"
for system_tool in bash cat grep ps sleep tr; do
  ln -s "$(command -v "$system_tool")" "$system_bin/$system_tool"
done

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
manifest | pull | rm | stop)
  exit 0
  ;;
container)
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
  printf 'false\n'
  exit 1
  ;;
run)
  if [[ "$*" == *'--name review-runtime-probe-'* ]]; then
    [[ "$runtime_selected" == true ]] || exit 91
    case "${PODMAN_PROBE_BEHAVIOR:-ready}" in
    reject)
      echo 'configured OCI runtime runsc is unavailable' >&2
      exit 125
      ;;
    nonzero)
      echo 'probe command failed under runsc' >&2
      exit 42
      ;;
    esac
    exit 0
  fi
  exit 0
  ;;
esac
exit 0
EOF
chmod 0755 "$fake_bin/podman"

cat >"$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
shift
if [[ "$*" == *'--runtime=runsc run'* ]]; then
  case "${TIMEOUT_BEHAVIOR:-ready}" in
  expire) exit 124 ;;
  interrupt) exit 130 ;;
  esac
fi
exec "$@"
EOF
chmod 0755 "$fake_bin/timeout"

run_recipe() {
  local recipe="$1"
  shift
  : >"$podman_log"
  set +e
  output="$(
    env PATH="$fake_bin:$system_bin" PODMAN_LOG="$podman_log" "$@" \
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

assert_probe_removed() {
  if ! grep -Eq -- '^--runtime=runsc run .*--name review-runtime-probe-' "$podman_log"; then
    return
  fi
  grep -Eq -- '^rm --force review-runtime-probe-[0-9]+$' "$podman_log" ||
    fail "the disposable runsc probe must be force-removed by its cleanup trap"
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
  assert_probe_removed
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
  run_recipe review-container TIMEOUT_BEHAVIOR=interrupt
  assert_failure_copy 'installed but unusable'
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
  grep -Eq -- '^inspect --format .*OCIRuntime.*review-runtime-probe-' "$podman_log" ||
    fail "${name} must verify the probe container runtime identity"
  grep -Eq -- '^inspect --format .*State.Running.*OCIRuntime.*review-runtime-probe-' "$podman_log" ||
    fail "${name} must verify runtime identity while the probe is active"
  assert_probe_removed
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
    probe-stopped probe-timeout probe-interrupt rootful \
    attended detached queue doctor; do
    run_case "$requested_case"
  done
fi

printf 'runsc isolation launcher contract OK\n'
