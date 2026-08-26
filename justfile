# justfile — the review appliance launcher entrypoint.
#
# The system image install path is still out of scope here; this root justfile
# is the launcher a checkout exposes directly.
#
# This is the ONLY file that ships/installs. The direct image launch and
# container lifecycle are
# embedded below as private ('_'-prefixed variables and shared shell
# functions) on purpose: a user browsing the image or this repo should find
# one just-recipe file and the commands it exposes, not a scattered bin/ of
# standalone scripts they might stumble into and run directly out of context.
#
# Public commands:
#   review-container  Run the direct lab-runner fork as a container.
#   review-stop       Stop a detached worker. Refuses attended runs and
#                     containers this launcher did not start.
#   review-doctor     Run disposable isolation diagnostics and image checks.
#   review-queue      Run the direct lab-runner fork as a container.
#
# ─────────────────────────────────────────────────────────────────────────
# LIFECYCLE
#
# The interactive recipes run in the foreground of the terminal that
# launched them: a maintainer steers the session, and Ctrl-C stops it.
# Cleanup of interactive runs is a startup concern: a launch reclaims
# whatever a previous run left behind, so there is no lifecycle verb for
# them.
#
# The detached worker is the one permitted background launch. REVIEW_DETACH=1
# stamps the container with the 'review.owner=detached' label; a later launch
# refuses to reclaim it, and 'just review-stop' — a polite podman stop, never
# a force flag — is its only lifecycle verb.
#
# '--replace' is how interactive reclaim works: --rm removes the container
# when it exits cleanly, but a hard-killed terminal, an OOM kill or a podman
# restart can leave the fixed name behind, and the next launch would
# otherwise die with 'the container name ... is already in use'. --replace
# takes the name back at launch time instead of asking the user to run a
# lifecycle command.
#
# Every interactive launch path ends in an 'exec' or a final foreground
# command whose exit status propagates verbatim; the detached path is an
# explicit, labeled podman run -d. tests/just-onboarding.sh pins all of it.
# Agent-capable launches additionally select runsc explicitly after a
# credential-free rootless probe; there is no default-runtime fallback (#348).
# ─────────────────────────────────────────────────────────────────────────
#
# Bluefin's root Justfile (/usr/share/ublue-os/just/00-entry.just) imports a
# fixed list of files, NOT a glob. Making these recipes work system-wide from
# the image still means baking this launcher into a custom image build (out of
# scope here — see README "Scope").
#
# In this checkout, launch recipes run the direct lab-runner fork using only
# the minimal image lifecycle helpers below.
# The fsdk-derived contributor image, used by every recipe that starts a
# container.
#
# ':stable' moves on every merge to main, so the default is always what the
# repository currently says. That is the point: the people running this are
# the people changing it, and nobody should be debugging a bug that was fixed
# yesterday.
#
# ':latest' is deliberately absent from the registry — the workflow publishes
# 'sha-<commit>' on every build, 'stable' on each main or release build, and
# version tags on a 'v*.*.*' release. Asking for ':latest' dies on 'manifest
# unknown'.
# REVIEW_CONTRIBUTOR_IMAGE overrides this when you need a specific
# 'sha-' tag or digest.
contributor_image := env("REVIEW_CONTRIBUTOR_IMAGE", "ghcr.io/projectbluefin/review:stable")

# Shared bash evaluated at the top of every recipe that needs image lifecycle
# helpers. Keeping it here avoids shipping another launcher artifact.
shared_functions := '''
contributor_image_available() {
  local ref="$1"
  podman image exists "$ref" && return 0
  case "$ref" in localhost/*) return 1 ;; esac
  podman manifest inspect "$ref" &>/dev/null
}

launcher_boot_id() {
  cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown
}

container_owner_pid() {
  local name="$1" marker owner_boot owner_pid
  marker="$(podman inspect --format '{{index .Config.Labels "review.owner"}}' "$name" 2>/dev/null || true)"
  [[ "$marker" == *:* ]] || return 0
  owner_boot="${marker%%:*}"
  owner_pid="${marker##*:}"
  [[ "$owner_boot" == "$(launcher_boot_id)" ]] || return 0
  [[ "$owner_pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$owner_pid" 2>/dev/null || return 0
  tr '\0' ' ' <"/proc/${owner_pid}/cmdline" 2>/dev/null |
    grep -Fq -- "--name ${name}" || return 0
  printf '%s\n' "$owner_pid"
}

container_owner_tty() {
  local pid="$1" tty
  tty="$(ps -o tty= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$tty" && "$tty" != "?" ]] || return 1
  printf '%s\n' "$tty"
}

require_valid_container_name() {
  local name="$1"
  [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] && return 0
  echo "ERROR: container name '${name}' is invalid." >&2
  echo "  Use [a-zA-Z0-9][a-zA-Z0-9_.-]*." >&2
  return 1
}

owner_run_label() {
  printf 'review.owner=%s:%s\n' "$(launcher_boot_id)" "$$"
}

require_no_running_instance() {
  local name="$1" marker owner_pid owner_tty
  [[ "$(podman inspect --format '{{.State.Running}}' "$name" 2>/dev/null || echo false)" == true ]] || return 0
  marker="$(podman inspect --format '{{index .Config.Labels "review.owner"}}' "$name" 2>/dev/null || true)"
  if [[ "$marker" == detached ]]; then
    echo "ERROR: ${name} is already running as a detached worker." >&2
    echo "  Follow it: podman logs -f ${name}" >&2
    echo "  Stop it:   just review-stop ${name}" >&2
    return 1
  fi
  owner_pid="$(container_owner_pid "$name")"
  if [[ -z "$owner_pid" ]]; then
    echo "✓ reclaiming ${name} from a run whose terminal is gone."
    return 0
  fi
  echo "ERROR: ${name} is already running in another terminal." >&2
  echo "  Attach to it or stop it from its owning terminal." >&2
  if owner_tty="$(container_owner_tty "$owner_pid")"; then
    echo "  Owner pid ${owner_pid} is attached to ${owner_tty}." >&2
  else
    echo "  Owner pid ${owner_pid} has no terminal." >&2
  fi
  return 1
}

image_ref_is_moving() {
  case "$1" in
    *@sha256:*|*:sha-*|localhost/*) return 1 ;;
    */*) return 0 ;;
  esac
  ! podman image exists "localhost/$1"
}

ensure_contributor_image() {
  local ref="$1"
  if image_ref_is_moving "$ref"; then
    podman pull "$ref" && return 0
    if podman image exists "$ref"; then
      echo "! could not refresh ${ref}; using the local copy, which may be out of date." >&2
      return 0
    fi
  fi
  contributor_image_available "$ref" && return 0
  case "$ref" in
    localhost/*)
      echo "ERROR: ${ref} is not in local storage." >&2
      echo "  Build it with: podman build -f image/Containerfile -t ${ref#localhost/} ." >&2
      return 1
      ;;
  esac
  podman pull "$ref" && return 0
  echo "ERROR: cannot obtain the contributor image ${ref}." >&2
  echo "  Use a published stable/SHA tag or build image/Containerfile locally." >&2
  return 1
}

isolation_runtime_failure() {
  local classification="$1" detail="$2"
  echo "ERROR: isolation runtime: gVisor/runsc (${classification})" >&2
  echo "  ${detail}" >&2
  echo "  Review did not start an agent or mount credentials." >&2
  echo "  Bluefin provisioning: https://github.com/projectbluefin/bluefin/issues/1139" >&2
  return 1
}

runsc_runtime_probe() (
  set -euo pipefail
  local image="$1" runtime_root="${XDG_RUNTIME_DIR:-/tmp}"
  local probe_dir probe_name probe_token cidfile probe_id= probe_status=0
  local probe_evidence probe_owner cleanup_status=0
  probe_dir="$(mktemp -d "${runtime_root%/}/review-runtime-probe-XXXXXX")"
  probe_name="$(basename "$probe_dir")"
  probe_token="${probe_name#review-runtime-probe-}"
  cidfile="$probe_dir/cid"

  cleanup_runtime_probe() {
    local owned_probe= exists_status=0 verification_status=0
    if [[ -s "$cidfile" ]]; then
      probe_id="$(cat "$cidfile")"
      timeout --kill-after=2s 5s podman container exists "$probe_id" || exists_status=$?
      if [[ "$exists_status" -eq 0 ]]; then
        owned_probe="$(
          timeout --kill-after=2s 5s podman inspect \
            --format '{{index .Config.Labels "review.probe"}}' "$probe_id"
        )" || cleanup_status=70
        if [[ "$owned_probe" != "$probe_token" ]]; then
          cleanup_status=70
        elif ! timeout --kill-after=2s 5s podman rm --force "$probe_id" >/dev/null; then
          cleanup_status=70
        else
          timeout --kill-after=2s 5s podman container exists "$probe_id" ||
            verification_status=$?
          [[ "$verification_status" -eq 1 ]] || cleanup_status=70
        fi
      elif [[ "$exists_status" -ne 1 ]]; then
        cleanup_status=70
      fi
    fi
    rm -f "$cidfile"
    rmdir "$probe_dir" 2>/dev/null || cleanup_status=70
    return "$cleanup_status"
  }
  finish_runtime_probe() {
    local original_status=$?
    trap - EXIT HUP INT TERM
    cleanup_runtime_probe || cleanup_status=$?
    if [[ "$cleanup_status" -ne 0 ]]; then
      echo 'runsc probe cleanup failed; the diagnostic may still exist.' >&2
      exit "$cleanup_status"
    fi
    exit "$original_status"
  }
  trap finish_runtime_probe EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  timeout --kill-after=2s 20s podman --runtime=runsc run --rm --detach \
    --name "$probe_name" --cidfile "$cidfile" --label "review.probe=$probe_token" \
    --network=none --pull=never --entrypoint /usr/bin/sleep "$image" 30 \
    >/dev/null || probe_status=$?
  [[ "$probe_status" -eq 0 ]] || return "$probe_status"
  [[ -s "$cidfile" ]] || return 70
  probe_id="$(cat "$cidfile")"
  probe_evidence="$(
    timeout --kill-after=2s 5s podman inspect \
      --format '{{.State.Running}} {{.OCIRuntime}}' "$probe_id"
  )" || probe_status=$?
  [[ "$probe_status" -eq 0 ]] || return "$probe_status"
  probe_owner="$(
    timeout --kill-after=2s 5s podman inspect \
      --format '{{index .Config.Labels "review.probe"}}' "$probe_id"
  )" || probe_status=$?
  [[ "$probe_status" -eq 0 ]] || return "$probe_status"
  printf '%s %s\n' "$probe_evidence" "$probe_owner"
)

require_runsc_host() {
  local runsc_path rootless
  runsc_path="$(command -v runsc 2>/dev/null || true)"
  if [[ -z "$runsc_path" || ! -f "$runsc_path" || ! -x "$runsc_path" ]]; then
    isolation_runtime_failure missing 'runsc is not installed as an executable host command.'
    return
  fi
  if ! "$runsc_path" --version >/dev/null 2>&1; then
    isolation_runtime_failure 'installed but unusable' 'runsc --version failed.'
    return
  fi
  rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)"
  if [[ "$rootless" != true ]]; then
    isolation_runtime_failure 'incompatible with this host/Podman configuration' \
      'Podman is not operating rootless for this user.'
    return
  fi
}

require_runsc_runtime() {
  local image="$1" probe_status=0 probe_evidence running probe_runtime probe_owner
  probe_evidence="$(runsc_runtime_probe "$image")" || probe_status=$?
  if [[ "$probe_status" -ne 0 ]]; then
    if [[ "$probe_status" -eq 125 ]]; then
      isolation_runtime_failure 'incompatible with this host/Podman configuration' \
        'Rootless Podman rejected runsc or could not start it.'
    else
      isolation_runtime_failure 'installed but unusable' \
        "The disposable runsc probe exited with status ${probe_status}."
    fi
    return
  fi

  read -r running probe_runtime probe_owner <<<"$probe_evidence"
  if [[ "$running" != true ]]; then
    isolation_runtime_failure 'installed but unusable' \
      'The disposable runsc probe was not running during runtime inspection.'
    return
  fi
  if [[ "$probe_runtime" != runsc ]]; then
    isolation_runtime_failure 'incompatible with this host/Podman configuration' \
      "Podman reported the probe runtime as ${probe_runtime:-unknown}, not runsc."
    return
  fi
  if [[ -z "$probe_owner" ]]; then
    isolation_runtime_failure 'incompatible with this host/Podman configuration' \
      'Podman did not preserve the probe ownership label.'
    return
  fi
}
'''

# Run the direct lab-runner fork. REVIEW_DETACH=1 runs it detached.
[doc("Run the direct lab-runner fork as the contributor container.")]
review-container:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    CONTAINER_NAME="${REVIEW_CONTAINER_NAME:-review-container}"
    require_valid_container_name "$CONTAINER_NAME"
    require_runsc_host
    require_no_running_instance "$CONTAINER_NAME"
    ensure_contributor_image "$CONTRIBUTOR_IMAGE"
    require_runsc_runtime "$CONTRIBUTOR_IMAGE"
    echo "✓ isolation runtime: gVisor/runsc"

    if [[ "${REVIEW_DETACH:-0}" == 1 ]]; then
      echo "✓ starting ${CONTRIBUTOR_IMAGE} as a detached container."
      echo "  Follow it:  podman logs -f ${CONTAINER_NAME}"
      echo "  Stop it:    just review-stop ${CONTAINER_NAME}"
      exec podman --runtime=runsc run --rm --detach --replace --name "$CONTAINER_NAME" \
        --label "review.owner=detached" "$CONTRIBUTOR_IMAGE"
    fi

    echo "✓ starting ${CONTRIBUTOR_IMAGE} in the foreground."
    echo "  Stop any time with Ctrl-C."
    exec podman --runtime=runsc run --rm --interactive --tty --replace --name "$CONTAINER_NAME" \
      --label "$(owner_run_label)" "$CONTRIBUTOR_IMAGE"

# Stop a detached review worker. This is the explicit lifecycle verb for
# containers started with REVIEW_DETACH=1; it refuses to touch anything this
# launcher did not start (no review.owner label) and never force-removes.
# Interactive runs still end with Ctrl-C, not with this.
[doc("Stop a detached contributor worker. Refuses attended runs and foreign containers.")]
review-stop name="review-container":
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    NAME="{{name}}"
    [[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
      echo "ERROR: '${NAME}' is not a valid container name." >&2
      exit 1
    }
    marker="$(podman inspect --format '{{{{index .Config.Labels "review.owner"}}' "$NAME" 2>/dev/null || true)"
    if [[ -z "$marker" ]]; then
      if podman container exists "$NAME" 2>/dev/null; then
        echo "ERROR: ${NAME} was not started by this launcher; not touching it." >&2
        exit 1
      fi
      echo "✓ no container named ${NAME} is running."
      exit 0
    fi
    if [[ "$marker" != "detached" ]]; then
      echo "ERROR: ${NAME} is an attended run; press Ctrl-C in its terminal instead." >&2
      exit 1
    fi
    podman stop "$NAME" >/dev/null
    echo "✓ stopped the detached worker ${NAME}."

# Run the direct lab-runner fork as the interactive review container.
# The first image slice has no dashboard entrypoint, so no queue arguments are
# accepted. Foreground: Ctrl-C stops.
#
#   just review-queue
# REVIEW_QUEUE_NAME overrides the container name.
[doc("Run the direct lab-runner fork as the interactive review container.")]
review-queue:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    CONTAINER_NAME="${REVIEW_QUEUE_NAME:-review-queue}"
    require_valid_container_name "$CONTAINER_NAME"
    require_runsc_host
    require_no_running_instance "$CONTAINER_NAME"
    ensure_contributor_image "$CONTRIBUTOR_IMAGE"
    require_runsc_runtime "$CONTRIBUTOR_IMAGE"
    echo "✓ isolation runtime: gVisor/runsc"
    echo "✓ starting ${CONTRIBUTOR_IMAGE} in the foreground."
    echo "  Stop any time with Ctrl-C."
    exec podman --runtime=runsc run --rm --interactive --tty --replace --name "$CONTAINER_NAME" \
      --label "$(owner_run_label)" "$CONTRIBUTOR_IMAGE"

[doc("Run disposable isolation diagnostics and check the direct image runtime.")]
review-doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    command -v podman &>/dev/null || {
      echo "ERROR: Podman is required to inspect the review image." >&2
      exit 1
    }
    require_runsc_host
    ensure_contributor_image "$CONTRIBUTOR_IMAGE" || {
      echo "ERROR: cannot obtain the contributor image ${CONTRIBUTOR_IMAGE}." >&2
      exit 1
    }
    echo "✓ ${CONTRIBUTOR_IMAGE} is resolvable (direct lab-runner fork)"
    require_runsc_runtime "$CONTRIBUTOR_IMAGE"
    echo "✓ isolation runtime: gVisor/runsc (ready; rootless Podman probe passed)"
    echo "✓ disposable credential-free agent-free probe removed"
    podman run --rm --entrypoint /usr/bin/bash "$CONTRIBUTOR_IMAGE" -lc '
      set -eu
      for tool in bash curl git jq python3 kubectl argo just which xargs awk ps tar diff patch less file; do
        command -v "$tool" >/dev/null
      done
    '
    echo "✓ direct lab-runner runtime is runnable"
