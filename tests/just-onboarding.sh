#!/usr/bin/env bash
# Contract for launching the direct lab-runner fork through just.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
justfile="$repo_root/justfile"
real_just="$(command -v just)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

fake_bin="$scratch/bin"
podman_log="$scratch/podman.log"
mkdir -p "$fake_bin"
cat >"$fake_bin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${PODMAN_LOG:?}"
if [[ "${1:-}" == --runtime=runsc ]]; then
  shift
fi
case "${1:-}" in
  info)
    printf 'true\n'
    ;;
  image)
    exit 1
    ;;
  manifest)
    exit 0
    ;;
  pull)
    exit 0
    ;;
  inspect)
    if [[ "$*" == *'review.probe'* ]]; then
      target="${*: -1}"
      cat "${PODMAN_LOG%/*}/${target}.label"
      exit 0
    fi
    if [[ "$*" == *'.OCIRuntime'* ]]; then
      if [[ "$*" == *'.State.Running'* ]]; then
        printf 'true runsc\n'
      else
        printf 'runsc\n'
      fi
      exit 0
    fi
    if [[ "$*" == *'review.owner'* ]]; then
      printf '%s\n' "${PODMAN_OWNER_LABEL:-}"
      exit 0
    fi
    printf 'false\n'
    exit 1
    ;;
  container)
    if [[ "${3:-}" == probe-id-* && -f "${PODMAN_LOG%/*}/${3}.exists" ]]; then
      exit 0
    fi
    [[ "${PODMAN_CONTAINER_EXISTS:-0}" == 1 ]]
    ;;
  stop)
    exit 0
    ;;
  run)
    if [[ "$*" == *'--name review-runtime-probe-'* ]]; then
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
      probe_id="probe-id-${probe_label}"
      printf '%s\n' "$probe_id" >"$cidfile"
      printf '%s\n' "$probe_label" >"${PODMAN_LOG%/*}/${probe_id}.label"
      : >"${PODMAN_LOG%/*}/${probe_id}.exists"
    fi
    exit 0
    ;;
  rm)
    target="${*: -1}"
    rm -f "${PODMAN_LOG%/*}/${target}.label" "${PODMAN_LOG%/*}/${target}.exists"
    ;;
esac
exit 0
EOF
chmod 0755 "$fake_bin/podman"
cat >"$fake_bin/runsc" <<'EOF'
#!/usr/bin/env bash
printf 'runsc version test\n'
EOF
chmod 0755 "$fake_bin/runsc"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

run_recipe() {
  local recipe="$1"
  shift
  set +e
  output="$(
    env PATH="$fake_bin:$PATH" PODMAN_LOG="$podman_log" "$@" \
      "$real_just" --justfile "$justfile" "$recipe" 2>&1
  )"
  status=$?
  set -e
}

: >"$podman_log"
run_recipe review-queue
[[ "$status" -eq 0 ]] || fail "review-queue must launch the direct image"
grep -q -- 'run --rm --interactive --tty --replace --name review-queue' "$podman_log" ||
  fail "review-queue must run an interactive container"
grep -q -- 'ghcr.io/projectbluefin/review:stable$' "$podman_log" ||
  fail "review-queue must pass the direct image without an obsolete queue command"

: >"$podman_log"
run_recipe review-container
[[ "$status" -eq 0 ]] || fail "review-container must launch the direct image"
grep -q -- 'run --rm --interactive --tty --replace --name review-container' "$podman_log" ||
  fail "review-container must run an interactive container"
grep -q -- 'ghcr.io/projectbluefin/review:stable$' "$podman_log" ||
  fail "review-container must pass the direct image without an obsolete command"

: >"$podman_log"
run_recipe review-container REVIEW_DETACH=1
[[ "$status" -eq 0 ]] || fail "detached review-container must launch the direct image"
grep -q -- 'run --rm --detach --replace --name review-container' "$podman_log" ||
  fail "detached review-container must use Podman's detached mode"
grep -q -- 'review.owner=detached' "$podman_log" ||
  fail "detached review-container must retain its ownership label"

: >"$podman_log"
run_recipe review-stop PODMAN_CONTAINER_EXISTS=1
[[ "$status" -ne 0 ]] || fail "review-stop must refuse an unlabelled container"
grep -q 'not started by this launcher' <<<"$output" ||
  fail "review-stop must explain an unlabelled container"
! grep -q '^stop ' "$podman_log" ||
  fail "review-stop must not stop an unlabelled container"

: >"$podman_log"
run_recipe review-stop PODMAN_CONTAINER_EXISTS=1 PODMAN_OWNER_LABEL=detached
[[ "$status" -eq 0 ]] || fail "review-stop must stop a detached worker"
grep -q '^stop ' "$podman_log" ||
  fail "review-stop must stop a detached worker politely"

: >"$podman_log"
run_recipe review-stop PODMAN_CONTAINER_EXISTS=1 PODMAN_OWNER_LABEL=attended
[[ "$status" -ne 0 ]] || fail "review-stop must refuse an attended run"
grep -q 'attended run' <<<"$output" ||
  fail "review-stop must direct attended runs back to their terminal"
! grep -q '^stop ' "$podman_log" ||
  fail "review-stop must not stop an attended run"

run_recipe review-doctor
[[ "$status" -eq 0 ]] || fail "review-doctor must report direct image readiness"
grep -q 'direct lab-runner fork' <<<"$output" ||
  fail "review-doctor must identify the direct image"

list="$("$real_just" --justfile "$justfile" --list)"
for recipe in review-container review-stop review-doctor review-queue; do
  grep -qE "^[[:space:]]+${recipe}([[:space:]]|$)" <<<"$list" ||
    fail "public recipe missing from just --list: ${recipe}"
done
[[ "$(grep -cE '^[[:space:]]+review-(container|stop|doctor|queue)([[:space:]]|$)' <<<"$list")" -eq 4 ]] ||
  fail "just --list must expose exactly four review recipes"

code="$(cat "$justfile")"
grep -q 'ghcr.io/projectbluefin/review:stable' <<<"$code" ||
  fail "the launcher must use the published review image"
if grep -q 'require_review_runtime' <<<"$code"; then
  fail "the launcher must not block the direct image with a compatibility guard"
fi
stop_body="$(sed -n '/^review-stop/,/^[a-z]/p' "$justfile")"
grep -q 'review.owner' <<<"$stop_body" ||
  fail "review-stop must remain scoped to launcher-owned detached workers"
if grep -qE 'podman (rm|kill)|--force|stop -f' <<<"$stop_body"; then
  fail "review-stop must stop politely and never force-remove"
fi

printf 'direct-copy launcher contract OK\n'

bash "$repo_root/tests/runsc-isolation.sh"
