# justfile — the review appliance launcher entrypoint.
#
# The system image install path is still out of scope here; this root justfile
# is the launcher a checkout exposes directly.
#
# This is the ONLY file that ships/installs. Everything review needs
# (host preflight, Goose selection, container lifecycle) is
# embedded below as private ('_'-prefixed variables and shared shell
# functions) on purpose: a user browsing the image or this repo should find
# one just-recipe file and the commands it exposes, not a scattered bin/ of
# standalone scripts they might stumble into and run directly out of context.
#
# Public commands:
#   review-container  Run the contributor container: the Hive queue
#                     worker that receives assigned tasks and donates
#                     inference. Takes an optional model profile and
#                     thinking effort, e.g. 'just review-container sol
#                     medium'. Foreground when attended; REVIEW_DETACH=1
#                     runs it as a labeled detached worker.
#   review-stop       Stop a detached worker. Refuses attended runs and
#                     containers this launcher did not start.
#   review-doctor     Preflight diagnostics. Starts no agent and mounts no
#                     credential.
#   review-queue      The interactive maintainer review surface: a
#                     full-screen dashboard over the Bluefin PR queue,
#                     running in the contributor container — no Hive
#                     registration required. q or Ctrl-C stops.
#                     Takes the same model profile and effort as
#                     review-container, then passes the rest through to
#                     the dashboard, e.g.
#                     'just review-queue k3 high --repo bluefin'.
#   turbo-review      Scale three cluster contributor workers, then launch
#                     review-queue in the foreground with all arguments
#                     forwarded.
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
# ─────────────────────────────────────────────────────────────────────────
#
# Bluefin's root Justfile (/usr/share/ublue-os/just/00-entry.just) imports a
# fixed list of files, NOT a glob. Making these recipes work system-wide from
# the image still means baking this launcher into a custom image build (out of
# scope here — see README "Scope").
#
# In this checkout, run 'just review-container' (or another recipe below)
# from the repository root. Persistent state is limited to launcher
# configuration; the container receives credentials by environment and the
# read-only ~/.config/hive mount, never a workspace or host home mount.
# Goose is the default agent backend; Pi is the explicitly selected executable
# backend. Hive remains the sole assignment authority and there is no local
# inference, model catalogue, or multi-CLI auto-detection.
#
# TOOL is read from the environment so 'TOOL=goose just review-container'
# works as documented — 'just' recipe parameters are positional, not
# KEY=VALUE, so it cannot be a plain recipe parameter. Unsupported values are
# hard errors rather than silent fallbacks.
tool_env := env("TOOL", "")
hive_repo_url := "https://github.com/hivecommons/hive"
# origin/v4 via `git ls-remote --heads https://github.com/hivecommons/hive v4`
# on 2026-09-06.
hive_commit := "c7a88b8518abf1163e13803b2094f2262605490b"
gemini_model := "gemini-3.8-flash"
# Contributor runs are automated in practice — Hive keeps feeding the session —
# so a large window is money spent on context nobody reads. Opus and Kimi are
# the models whose default windows are worth clamping.
opus_model := "claude-opus-5"
opus_context_limit := "264000"
# Kimi K3's default window is ~1M tokens, so the same clamp applies.
sol_model := "gpt-5.6-sol"
k3_model := "kimi-k3"
k3_context_limit := "264000"
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

# Shared bash, 'eval''d at the top of every recipe script that needs it:
# host preflight, Goose selection, and the pinned Hive checkout. Keeping
# this in one place instead of duplicating it per-recipe is the only
# concession to DRY here — it never leaves the Justfile as a file of its own.
shared_functions := '''
GITHUB_LOGIN_COMMAND="gh auth login --web --hostname github.com --scopes repo,read:org"

github_auth_ready() {
  command -v gh &>/dev/null && gh auth status --hostname github.com &>/dev/null
}
can_run_attended_hive_setup() {
  [[ "${REVIEW_TEST_ATTACH_TTY:-}" == "1" ]] || { [[ -t 0 ]] && [[ -t 1 ]] && [[ -t 2 ]]; }
}
print_missing_hive_setup_guidance() {
  local path="$1" reason="$2" tool="$3" commit="$4"
  echo "ERROR: missing Hive setup at ${path}; ${reason}." >&2
  echo "  Re-run review from an interactive terminal, or pre-seed it yourself from hivecommons/hive @ ${commit} by running \`just contribute-setup ${tool}\` in an interactive checkout (set REVIEW_HIVE_COMMIT to another full commit if needed)" >&2
}
GOOSE_INSTALL_HINT="Install: https://github.com/block/goose/releases"
GOOSE_FIXIT_HINT="Run: goose configure, select GitHub Copilot, and complete the device flow."

goose_configured() {
  # An explicit GOOSE_PROVIDER counts as configured because the launcher
  # passes it straight through to the container; otherwise Goose's own config
  # must name a provider. Current Goose records the selection as
  # 'active_provider:' beside a 'providers:' map; older releases wrote a
  # bare 'provider:' — accept either. A GitHub login alone is deliberately
  # NOT enough: Goose still needs a provider selected before it can talk
  # to a model.
  [[ -n "${GOOSE_PROVIDER:-}" ]] && return 0
  local cfg="${HOME}/.config/goose/config.yaml"
  [[ -s "$cfg" ]] && grep -Eq '^[[:space:]]*(GOOSE_PROVIDER|provider|active_provider):[[:space:]]*[^[:space:]#]' "$cfg"
}
require_copilot_provider() {
  local provider="${GOOSE_PROVIDER:-}"
  [[ -z "$provider" || "$provider" == "github_copilot" ]] && return 0
  echo "ERROR: GOOSE_PROVIDER=${provider} is not supported — review supports GitHub Copilot only." >&2
  echo "  Unset GOOSE_PROVIDER or set GOOSE_PROVIDER=github_copilot." >&2
  return 1
}
require_goose_backend() {
  local requested="${1:-}"
  [[ -z "$requested" || "$requested" == goose || "$requested" == pi || "$requested" == codex ]] && return 0
  echo "ERROR: TOOL=${requested} is not supported — review supports Goose, Codex, and Pi." >&2
  echo "  Unset TOOL, or pass TOOL=goose, TOOL=codex, or TOOL=pi." >&2
  return 1
}
codex_auth_configured() {
  local codex_home="${CODEX_HOME:-${HOME}/.codex}"
  local auth_file="${codex_home%/}/auth.json"
  [[ -s "$auth_file" && -r "$auth_file" ]]
}
preflight_agent() {
  local backend="${1:-goose}"
  # Exactly one ERROR line per failure, each with the command that fixes it.
  if [[ "$backend" == pi ]]; then
    [[ -n "${PI_API_KEY:-}" ]] || {
      echo "ERROR: Pi requires PI_API_KEY for the selected Anthropic provider." >&2
      echo "  Export PI_API_KEY before running TOOL=pi just review-container." >&2
      return 1
    }
  elif [[ "$backend" == codex ]]; then
    codex_auth_configured || {
      echo "ERROR: Codex subscription login is unavailable for the selected backend." >&2
      echo "  Run 'codex login' with file credential storage, then re-run TOOL=codex just review-container." >&2
      return 1
    }
  else
    require_copilot_provider || return 1
    command -v goose &>/dev/null || {
      echo "ERROR: goose is not installed." >&2
      echo "  ${GOOSE_INSTALL_HINT}" >&2
      return 1
    }
    goose_configured || {
      echo "ERROR: Goose has no usable provider configuration." >&2
      echo "  ${GOOSE_FIXIT_HINT}" >&2
      return 1
    }
  fi
  preflight_github
}
preflight_github() {
  github_auth_ready || {
    echo "ERROR: GitHub CLI is not authenticated against github.com." >&2
    echo "  Run: ${GITHUB_LOGIN_COMMAND}" >&2
    return 1
  }
}
contributor_image_available() {
  # Locally present is enough; otherwise the tag has to exist in the
  # registry. Both probes are read-only, so review-doctor can call
  # this without starting anything.
  local ref="$1"
  podman image exists "$ref" && return 0
  # A 'localhost/' ref has no registry behind it, so a manifest probe can only
  # dial localhost and fail slowly. Absent from local storage is the answer.
  case "$ref" in localhost/*) return 1 ;; esac
  podman manifest inspect "$ref" &>/dev/null
}
launcher_boot_id() {
  # PIDs are only meaningful within a boot. Recording the boot alongside the
  # owner PID keeps a recycled number from ever reading as a live owner.
  cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown
}
container_owner_pid() {
  # The owning client PID, printed only when that process is genuinely still
  # the launcher run that started this container. Nothing otherwise.
  #
  # This has to be authoritative, because '--rm --interactive --tty' does NOT
  # bind the container's lifetime to the client: conmon supervises the
  # container, survives the client, and reparents to the user manager. A
  # terminal that died hard -- or a 'podman start'/'podman restart' typed by
  # hand -- therefore leaves a container that is fully RUNNING with no client
  # and no terminal behind it. Inferring ownership from 'pgrep' for a 'podman
  # run' command line cannot tell that apart from a live session, so the
  # launcher records the answer itself instead of guessing.
  local name="$1" marker owner_boot owner_pid
  marker="$(podman inspect --format '{{index .Config.Labels "review.owner"}}' "$name" 2>/dev/null || true)"
  [[ "$marker" == *:* ]] || return 0
  owner_boot="${marker%%:*}"
  owner_pid="${marker##*:}"
  # A marker from a previous boot can only describe a process that no longer
  # exists, whatever occupies that PID now.
  [[ "$owner_boot" == "$(launcher_boot_id)" ]] || return 0
  [[ "$owner_pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$owner_pid" 2>/dev/null || return 0
  # PID reuse within one boot is rare but not impossible, and reclaiming a
  # stranger's container would be unforgivable. Confirm the process still
  # names this container.
  tr '\0' ' ' <"/proc/${owner_pid}/cmdline" 2>/dev/null | grep -Fq -- "--name ${name}" || return 0
  printf '%s\n' "$owner_pid"
}
container_owner_tty() {
  # 'ps' prints '?' when a process has no controlling terminal -- which is
  # exactly the case where telling somebody to press Ctrl-C is nonsense.
  local pid="$1" tty
  tty="$(ps -o tty= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$tty" && "$tty" != "?" ]] || return 1
  printf '%s\n' "$tty"
}
require_valid_container_name() {
  # A name supplied through REVIEW_CONTAINER_NAME reaches 'podman run --name'
  # and the ownership probe, so it is checked against podman's own rule
  # rather than handed to podman as-is and left to fail late and cryptically.
  local name="$1"
  [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] && return 0
  echo "ERROR: REVIEW_CONTAINER_NAME='${name}' is not a valid container name." >&2
  echo "  Use [a-zA-Z0-9][a-zA-Z0-9_.-]*, e.g. REVIEW_CONTAINER_NAME=review-container-2." >&2
  return 1
}
owner_run_label() {
  # Stamped onto every launch so the NEXT launch can answer 'is anyone
  # actually holding this?' without guessing. '--rm' clears it with the
  # container, and a hand-typed 'podman start' cannot forge a live one: it
  # reuses the original creation label, whose PID is long dead.
  printf 'review.owner=%s:%s\n' "$(launcher_boot_id)" "$$"
}
require_no_running_instance() {
  # 'Running' alone does not mean 'in use'. Distinguish three cases,
  # because they deserve different treatment:
  #
  #   owned    -- somebody is working in that terminal right now. Never touch
  #               it; hand over the attach command instead.
  #   detached -- a deliberate background worker. Never reclaim it silently;
  #               'just review-stop' is its lifecycle verb.
  #   orphan   -- still running, but its terminal is gone, so no one can ever
  #               reach it or Ctrl-C it again. Reclaim it silently.
  #
  # Telling a user to run 'podman rm -f' for the orphan case would smuggle
  # an undocumented stop command back in; the launcher cleans up after itself
  # instead, and the detached case has its own explicit verb.
  local name="$1" marker owner_pid owner_tty
  [[ "$(podman inspect --format '{{.State.Running}}' "$name" 2>/dev/null || echo false)" == "true" ]] || return 0
  marker="$(podman inspect --format '{{index .Config.Labels "review.owner"}}' "$name" 2>/dev/null || true)"
  if [[ "$marker" == "detached" ]]; then
    echo "ERROR: ${name} is already running as a detached worker." >&2
    echo "  Follow it:  podman logs -f ${name}" >&2
    echo "  Stop it:    just review-stop ${name}" >&2
    return 1
  fi
  owner_pid="$(container_owner_pid "$name")"
  if [[ -z "$owner_pid" ]]; then
    cleanup_codex_auth_staging_dir "$(podman inspect --format '{{index .Config.Labels "review.codex-auth"}}' "$name" 2>/dev/null || true)"
    echo "✓ reclaiming ${name} from a run whose terminal is gone."
    return 0
  fi
  echo "ERROR: ${name} is already running in another terminal." >&2
  echo "  Attach to the live session: podman exec -it ${name} tmux attach -t contributor" >&2
  if owner_tty="$(container_owner_tty "$owner_pid")"; then
    # Only ever name Ctrl-C when a terminal to press it in demonstrably
    # exists. The old message advised it unconditionally, so an ownerless
    # container told the user to act in a terminal that was already gone.
    echo "  Or press Ctrl-C in the terminal that owns it (pid ${owner_pid} on ${owner_tty})." >&2
  else
    echo "  Its launcher (pid ${owner_pid}) has no terminal; end that process to stop it." >&2
  fi
  return 1
}
image_ref_is_moving() {
  # A digest is immutable and an 'sha-<commit>' tag is minted once per build,
  # so both always name exactly one image. That makes the tag CI mints the
  # right name for a local build too: it is honest about which commit is in
  # the image, and it is never re-pulled over. A locally built image has no
  # registry behind it either, so refreshing it only produces a failed pull
  # and a misleading "may be out of date" warning; podman stores a bare
  # 'podman build -t <name>:<tag>' under 'localhost/', so accept the bare
  # name a user is likely to type as well as the stored form. Anything else
  # can be repointed at a newer build under the same name.
  case "$1" in
    *@sha256:*|*:sha-*|localhost/*) return 1 ;;
    */*)                            return 0 ;;
  esac
  ! podman image exists "localhost/$1"
}
ensure_contributor_image() {
  # A missing tag otherwise surfaces as a bare 'manifest unknown' from
  # podman at launch time, which says nothing about what to do next.
  #
  # A moving tag is re-pulled every launch. Treating 'present locally' as
  # good enough is what silently pinned contributors to whatever copy they
  # first pulled while the tag moved on underneath them -- the launcher
  # looked healthy and ran stale code. Best-effort by design: if the registry
  # is unreachable, an existing local copy still starts, so being offline
  # degrades to 'possibly stale' rather than 'cannot work'.
  local ref="$1"
  if image_ref_is_moving "$ref"; then
    podman pull "$ref" && return 0
    if podman image exists "$ref"; then
      echo "! could not refresh ${ref}; using the local copy, which may be out of date." >&2
      return 0
    fi
  fi
  # Local presence is the postcondition every caller depends on: the isolation
  # probe runs with --pull=never, so 'resolvable in the registry' is not good
  # enough. Accepting a registry-only image here made a missing pull surface
  # as a false isolation failure.
  podman image exists "$ref" && return 0
  # 'localhost/' is podman's local-storage namespace, never a registry host.
  # Pulling it dials https://localhost/v2/ and fails three times with a
  # connection-refused error that reads like a network fault, so a deleted
  # local build looked like a broken registry. Say the real thing instead.
  case "$ref" in
    localhost/*)
      echo "ERROR: ${ref} is a locally built image and it is not in local storage." >&2
      echo "  Nothing can pull it: 'localhost/' is podman's local namespace, not a registry." >&2
      echo "  Build it: podman build -f image/Containerfile -t ${ref#localhost/} ." >&2
      echo "  Or drop the override to use the published default: unset REVIEW_CONTRIBUTOR_IMAGE" >&2
      return 1
      ;;
  esac
  podman pull "$ref" && return 0
  echo "ERROR: cannot obtain the contributor image ${ref}." >&2
  echo "  Published tags are 'stable', the version tags and 'sha-<commit>' — there is no ':latest'." >&2
  echo "  Pick a published tag with REVIEW_CONTRIBUTOR_IMAGE=ghcr.io/projectbluefin/review:stable," >&2
  echo "  or build the commit you have: ref=ghcr.io/projectbluefin/review:sha-\$(git rev-parse HEAD)" >&2
  echo "    podman build -f image/Containerfile -t \"\$ref\" . && REVIEW_CONTRIBUTOR_IMAGE=\"\$ref\" just review-container" >&2
  return 1
}

review_queue_kubernetes_available() {
  command -v kubectl &>/dev/null || return 1
  kubectl config current-context >/dev/null 2>&1 || return 1
  kubectl get --raw='/readyz?verbose' --request-timeout=5s >/dev/null 2>&1 || return 1
  kubectl get pvc review-queue-state -n bluefin-system --request-timeout=5s >/dev/null 2>&1 || {
    echo "ERROR: Kubernetes dashboard state claim 'review-queue-state' cannot be read; it may be absent or access may be denied." >&2
    return 2
  }
}
cleanup_kubernetes_dashboard() {
  [[ -n "${K8S_DASHBOARD_POD:-}" ]] ||
    [[ -n "${K8S_DASHBOARD_SECRET:-}" ]] || return 0
  kubectl delete pod "${K8S_DASHBOARD_POD:-}" -n bluefin-system \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete secret "${K8S_DASHBOARD_SECRET:-}" -n bluefin-system \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
review_queue_kubernetes() {
  local image="$1"
  shift
  local session_id env_names
  local -a otlp_secret_args=()
  session_id="$(date +%s)-$$"
  K8S_DASHBOARD_POD="review-queue-${session_id}"
  K8S_DASHBOARD_SECRET="review-session-${session_id}"
  K8S_ENV_NAMES=(
    GH_TOKEN GITHUB_COPILOT_TOKEN HIVE_HUB BLUEFIN_REVIEW_INSTANCE
    GOOSE_PROVIDER GOOSE_MODEL GOOSE_THINKING_EFFORT GOOSE_CONTEXT_LIMIT
    BLUEFIN_REVIEW_BACKEND
  )
  if [[ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]]; then
    K8S_ENV_NAMES+=(OTEL_EXPORTER_OTLP_ENDPOINT)
    otlp_secret_args+=(
      --from-file=OTEL_EXPORTER_OTLP_ENDPOINT=<(printf '%s' "$OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    if [[ -n "${OTEL_EXPORTER_OTLP_HEADERS:-}" ]]; then
      K8S_ENV_NAMES+=(OTEL_EXPORTER_OTLP_HEADERS)
      otlp_secret_args+=(
        --from-file=OTEL_EXPORTER_OTLP_HEADERS=<(printf '%s' "$OTEL_EXPORTER_OTLP_HEADERS")
      )
    fi
  fi
  env_names="$(IFS=,; echo "${K8S_ENV_NAMES[*]}")"

  kubectl create secret generic "$K8S_DASHBOARD_SECRET" -n bluefin-system \
    --from-file=GH_TOKEN=<(printf '%s' "$GH_TOKEN_VALUE") \
    --from-file=GITHUB_COPILOT_TOKEN=<(printf '%s' "${COPILOT_TOKEN:-}") \
    --from-file=HIVE_HUB=<(printf '%s' "$DASHBOARD_HIVE_HUB") \
    --from-file=BLUEFIN_REVIEW_INSTANCE=<(printf '%s' "$K8S_DASHBOARD_POD") \
    --from-file=GOOSE_PROVIDER=<(printf '%s' "${GOOSE_PROVIDER:-}") \
    --from-file=GOOSE_MODEL=<(printf '%s' "${GOOSE_MODEL:-}") \
    --from-file=GOOSE_THINKING_EFFORT=<(printf '%s' "${GOOSE_THINKING_EFFORT:-}") \
    --from-file=GOOSE_CONTEXT_LIMIT=<(printf '%s' "${GOOSE_CONTEXT_LIMIT:-}") \
    --from-file=BLUEFIN_REVIEW_BACKEND=<(printf '%s' "$REVIEW_BACKEND") \
    "${otlp_secret_args[@]}" >/dev/null

  python3 scripts/review-session-runtime.py "$session_id" "$image" \
    "$K8S_DASHBOARD_SECRET" review-queue-state "$env_names" queue "$@" |
    kubectl create -f -
  kubectl wait --for=condition=Ready "pod/${K8S_DASHBOARD_POD}" -n bluefin-system --timeout=5m
  kubectl attach --stdin --tty "$K8S_DASHBOARD_POD" -n bluefin-system
}

resolve_copilot_token() {
  # Goose's github_copilot provider needs the long-lived OAuth token minted by
  # the Copilot editor device flow (a "ghu_" user-to-server token). Without it
  # the container starts a fresh device flow on every launch and the pane sits
  # on "enter code XXXX-XXXX" until a human types one in.
  #
  # A `gh auth token` ("gho_") is NOT a substitute -- it is a different client
  # with different scopes, and Goose fails with "failed to get api info" when
  # handed one. Verified against the contributor image.
  #
  # On a desktop Goose keeps the real token in the login keyring, so read it
  # from there. This is best-effort by design: no keyring, no secret-tool, or
  # a locked session just means the device flow happens as before.
  COPILOT_TOKEN="${GITHUB_COPILOT_TOKEN:-}"
  [[ -n "$COPILOT_TOKEN" ]] && return 0
  command -v secret-tool &>/dev/null || return 0
  # Extracted with sed rather than a JSON parser so this stays a single line:
  # CI lifts recipe bodies out of this file and runs 'bash -n' over them, which
  # a multi-line embedded script breaks. The trailing '|| true' matters under
  # 'set -euo pipefail': a locked or empty keyring makes secret-tool exit
  # non-zero, and pipefail would otherwise abort the whole launch over a lookup
  # that is meant to be optional.
  COPILOT_TOKEN="$(secret-tool lookup service goose username secrets 2>/dev/null | sed -nE 's/.*"GITHUB_COPILOT_TOKEN"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1 || true)"
  return 0
}
report_missing_copilot_credential() {
  # Named so every caller tells the same story. A `gh auth token` is the
  # tempting substitute and the reason this message exists: it looks like a
  # GitHub credential, so a contributor reasonably assumes their gh login is
  # enough, and then the agent dies on "failed to get api info" inside a
  # container they were not watching.
  echo "! no Copilot credential found; the agent will ask for a device code." >&2
  echo "  A 'gh auth token' is NOT a substitute — Copilot inference rejects it." >&2
  echo "  Log in once on this host with: goose configure" >&2
  echo "  Or export GITHUB_COPILOT_TOKEN before launching." >&2
  return 0
}
hive_contributor_backend() {
  # Reads AGENT_BACKEND out of Hive's own contributor.env. Read-only: the file
  # belongs to upstream setup, so it is reported on, never rewritten.
  local path="$1"
  [[ -f "$path" ]] || return 0
  awk -F= '$1 == "AGENT_BACKEND" {sub(/^[^=]*=/, ""); gsub(/["'"'"']/, ""); print; exit}' "$path"
}
resolve_gh_token() {
  # Hive's contributor model is fork + pull request under the contributor's
  # OWN GitHub identity: /usr/local/bin/gh injects the hub's App token only
  # when HIVE_CONTRIBUTOR_MODE is not "true", and we always run with it set.
  # Upstream's own `just contribute-run` therefore passes -e GH_TOKEN from
  # `gh auth token`; without it the agent picks up a task, runs `gh`, is told
  # to `gh auth login` -- which the wrapper also blocks in contributor mode --
  # and stops. Every assigned task dies on arrival.
  #
  # By value, never by mounting ~/.config/gh: the container gets exactly one
  # credential for exactly one host, and no view of any other account, of
  # ~/.config/gh/hosts.yml, or of an enterprise login that happens to sit
  # beside it. Same reasoning as the Copilot token above.
  #
  # REVIEW_GH_TOKEN comes first so a contributor can hand the agent a
  # purpose-made, narrowly scoped PAT instead of their desktop login, which
  # typically carries admin:org, workflow and delete:packages.
  GH_TOKEN_VALUE="${REVIEW_GH_TOKEN:-${GH_TOKEN:-}}"
  GH_TOKEN_SOURCE="environment"
  if [[ -z "$GH_TOKEN_VALUE" ]]; then
    GH_TOKEN_SOURCE="gh auth token"
    command -v gh &>/dev/null || { GH_TOKEN_SOURCE=""; return 0; }
    GH_TOKEN_VALUE="$(gh auth token --hostname github.com 2>/dev/null || true)"
  fi
  [[ -n "$GH_TOKEN_VALUE" ]] || GH_TOKEN_SOURCE=""
  return 0
}
gh_token_scopes() {
  # Scopes, never the token. A contributor is about to hand these powers to an
  # autonomous agent, so the launcher says out loud what it is handing over.
  command -v gh &>/dev/null || return 0
  gh auth status --hostname github.com 2>&1 | sed -nE "s/.*[Tt]oken scopes:[[:space:]]*(.+)/\1/p" | head -1 || true
  return 0
}
report_gh_token_blast_radius() {
  local source="$1" scopes
  echo "✓ GitHub identity passed to the agent as GH_TOKEN (from ${source}; value not shown)."
  scopes="$(gh_token_scopes)"
  [[ -n "$scopes" ]] && echo "  The agent can do anything this token can: ${scopes}"
  echo "  Narrow that with: REVIEW_GH_TOKEN=<scoped PAT> (public_repo or repo is enough to fork and open a PR)."
  return 0
}
report_missing_gh_token() {
  echo "! no GitHub token found; the agent has no GitHub identity." >&2
  echo "  It cannot fork, clone, push or open a pull request, and will stop on" >&2
  echo "  'To get started with GitHub CLI, please run: gh auth login' — which it" >&2
  echo "  is not allowed to run. Every assigned task will die on arrival." >&2
  echo "  Fix it with: gh auth login --web --hostname github.com --scopes repo,read:org" >&2
  echo "  Or export REVIEW_GH_TOKEN with a scoped PAT." >&2
  return 0
}
resolve_codex_auth_file() {
  local codex_home="${CODEX_HOME:-${HOME}/.codex}"
  CODEX_AUTH_SOURCE_FILE="${codex_home%/}/auth.json"
  if [[ "$CODEX_AUTH_SOURCE_FILE" != /* || ! -f "$CODEX_AUTH_SOURCE_FILE" || ! -r "$CODEX_AUTH_SOURCE_FILE" ]]; then
    CODEX_AUTH_SOURCE_FILE=""
  fi
  return 0
}
stage_codex_auth_file() {
  CODEX_AUTH_FILE=""
  CODEX_AUTH_STAGING_DIR=""
  resolve_codex_auth_file
  [[ -n "$CODEX_AUTH_SOURCE_FILE" ]] || return 0
  umask 077
  CODEX_AUTH_STAGING_DIR="$(mktemp -d /tmp/review-codex-auth.XXXXXX)"
  CODEX_AUTH_FILE="${CODEX_AUTH_STAGING_DIR}/auth.json"
  cp -- "$CODEX_AUTH_SOURCE_FILE" "$CODEX_AUTH_FILE"
  chmod 0600 "$CODEX_AUTH_FILE"
  return 0
}
cleanup_codex_auth_file() {
  local staging_dir="${CODEX_AUTH_STAGING_DIR:-}"
  [[ -n "$staging_dir" && "$staging_dir" == /* ]] || return 0
  rm -f -- "${staging_dir}/auth.json"
  rmdir -- "$staging_dir"
  CODEX_AUTH_FILE=""
  CODEX_AUTH_STAGING_DIR=""
  return 0
}
cleanup_codex_auth_staging_dir() {
  local staging_dir="${1:-}" invoking_uid
  local stage_root=/tmp
  invoking_uid="$(id -u)"
  [[ "$staging_dir" =~ ^${stage_root%/}/review-codex-auth\.[[:alnum:]]{6}$ ]] || return 0
  [[ -d "$staging_dir" && ! -L "$staging_dir" ]] || return 0
  [[ -f "$staging_dir/auth.json" && ! -L "$staging_dir/auth.json" ]] || return 0
  [[ "$(stat -c %u "$staging_dir")" == "$invoking_uid" ]] || return 0
  [[ "$(stat -c %a "$staging_dir")" == 700 ]] || return 0
  [[ "$(stat -c %u "$staging_dir/auth.json")" == "$invoking_uid" ]] || return 0
  [[ "$(stat -c %a "$staging_dir/auth.json")" == 600 ]] || return 0
  [[ "$(find "$staging_dir" -mindepth 1 -maxdepth 1 -print | wc -l)" == 1 ]] || return 0
  rm -f -- "${staging_dir}/auth.json"
  rmdir -- "$staging_dir" 2>/dev/null || true
}
podman_default_connection_uri() {
  # Podman resolves its target engine in this order: CONTAINER_HOST wins
  # outright, CONTAINER_CONNECTION names a saved connection, and otherwise
  # whichever connection is marked default applies. Mirror that order so the
  # check below sees exactly the engine 'podman run' itself would use.
  if [[ -n "${CONTAINER_HOST:-}" ]]; then
    printf '%s\n' "$CONTAINER_HOST"
    return 0
  fi
  local list
  if ! list="$(podman system connection list --format '{{.Name}}\t{{.URI}}\t{{.Default}}' 2>/dev/null)"; then
    echo "ERROR: could not resolve Podman connections." >&2
    return 1
  fi
  if [[ -n "${CONTAINER_CONNECTION:-}" ]]; then
    local selected
    selected="$(awk -F'\t' -v n="$CONTAINER_CONNECTION" '$1==n{print $2; exit}' <<<"$list")"
    if [[ -z "$selected" ]]; then
      echo "ERROR: could not resolve selected Podman connection '${CONTAINER_CONNECTION}'." >&2
      return 1
    fi
    printf '%s\n' "$selected"
    return 0
  fi
  awk -F'\t' '$3=="true"{print $2; exit}' <<<"$list"
  return 0
}
podman_redacted_uri() {
  local uri="$1" scheme rest
  case "$uri" in
    *://*)
      scheme="${uri%%://*}"
      rest="${uri#*://}"
      rest="${rest#*@}"
      printf '%s://%s\n' "$scheme" "$rest"
      ;;
    *) printf '%s\n' "$uri" ;;
  esac
}
require_local_podman_engine() {
  # review-queue binds its dashboard state (#281) from the client-side
  # ${XDG_STATE_HOME:-$HOME/.local/state}/bluefin-review path and presents it
  # as this host's durable record of landing batches. When podman's default
  # connection is an ssh:// remote, that same-looking bind resolves on the
  # ENGINE host instead: the dashboard silently reads and writes a directory
  # that never existed on the launcher's filesystem, and landing batches
  # appear to vanish while they are live somewhere nobody is looking (#400).
  local engine_uri display_uri
  if ! engine_uri="$(podman_default_connection_uri)"; then
    return 1
  fi
  case "$engine_uri" in
    ""|unix://*) return 0 ;;
    *)
      display_uri="$(podman_redacted_uri "$engine_uri")"
      case "${REVIEW_QUEUE_ALLOW_REMOTE_STATE:-}" in
        1)
          echo "! podman's selected engine is remote (${display_uri}); the dashboard state directory binds on that engine host, not $(hostname)." >&2
          return 0
          ;;
        ""|0)
          echo "ERROR: podman's selected engine is remote (${display_uri})." >&2
          ;;
        *)
          echo "ERROR: REVIEW_QUEUE_ALLOW_REMOTE_STATE must be exactly 1 for a remote engine (got '${REVIEW_QUEUE_ALLOW_REMOTE_STATE}')." >&2
          ;;
      esac
      echo "  review-queue binds its dashboard state from \${XDG_STATE_HOME:-\$HOME/.local/state}/bluefin-review on THIS host, but podman would resolve that same-looking bind on the engine host instead -- landing batches would be written where nothing local, including the next 'just review-queue', can find them." >&2
      echo "  Switch to a local connection:  podman system connection default <local-name>" >&2
      echo "  Or, once you have confirmed the remote engine host's directory is the one you actually want, acknowledge it explicitly:  REVIEW_QUEUE_ALLOW_REMOTE_STATE=1 just review-queue" >&2
      return 1
      ;;
  esac
}
resolve_review_backend() {
  REVIEW_BACKEND="${BLUEFIN_REVIEW_BACKEND:-}"
  case "$REVIEW_BACKEND" in
    ""|goose|codex) return 0 ;;
    *)
      echo "ERROR: unsupported review backend '${REVIEW_BACKEND}'; expected goose or codex." >&2
      return 1
      ;;
  esac
}
resolve_goose_selection() {
  # Goose is fixed to GitHub Copilot. The model stays noninteractive and the
  # environment still overrides it for automation.
  GOOSE_PROVIDER="github_copilot"
  GOOSE_MODEL="${GOOSE_MODEL:-${GEMINI_MODEL}}"
  return 0
}
# Turn a short profile name plus an optional thinking effort into GOOSE_MODEL /
# GOOSE_THINKING_EFFORT / GOOSE_CONTEXT_LIMIT. Four profiles, no picker:
# an empty profile is the default one. Profiles are defaults, never overrides —
# an explicit GOOSE_* value in the environment still wins.
resolve_model_profile() {
  local profile="${1:-}" effort="${2:-}"
  case "${profile,,}" in
    ""|gemini|gemini-3.8|gemini38)
      PROFILE_MODEL="${GEMINI_MODEL}"
      PROFILE_EFFORT="max"
      PROFILE_CONTEXT_LIMIT=""
      ;;
    opus5)
      PROFILE_MODEL="${OPUS_MODEL}"
      PROFILE_EFFORT="high"
      PROFILE_CONTEXT_LIMIT="${OPUS_CONTEXT_LIMIT}"
      ;;
    sol|gpt-sol|gptsol)
      PROFILE_MODEL="${SOL_MODEL}"
      PROFILE_EFFORT="medium"
      PROFILE_CONTEXT_LIMIT=""
      ;;
    k3|kimi)
      PROFILE_MODEL="${K3_MODEL}"
      PROFILE_EFFORT="max"
      PROFILE_CONTEXT_LIMIT="${K3_CONTEXT_LIMIT}"
      ;;
    *)
      echo "ERROR: unknown model profile '${profile}'." >&2
      echo "  Known profiles: gemini (${GEMINI_MODEL}), sol (${SOL_MODEL}), opus5 (${OPUS_MODEL}), k3 (${K3_MODEL})." >&2
      return 1
      ;;
  esac
  case "${effort,,}" in
    "") ;;
    low|medium|high|max) PROFILE_EFFORT="${effort,,}" ;;
    *)
      echo "ERROR: unknown thinking effort '${effort}'; expected low, medium, high, or max." >&2
      return 1
      ;;
  esac
  GOOSE_MODEL="${GOOSE_MODEL:-${PROFILE_MODEL}}"
  GOOSE_THINKING_EFFORT="${GOOSE_THINKING_EFFORT:-${PROFILE_EFFORT}}"
  GOOSE_CONTEXT_LIMIT="${GOOSE_CONTEXT_LIMIT:-${PROFILE_CONTEXT_LIMIT}}"
  echo "✓ model ${GOOSE_MODEL}, thinking effort ${GOOSE_THINKING_EFFORT}, context ${GOOSE_CONTEXT_LIMIT:-provider default}."
  return 0
}
normalize_git_remote() {
  local value="$1"
  value="${value#ssh://}"
  value="${value%.git}"
  value="${value%/}"
  if [[ "$value" =~ ^git@github\.com:(.+)$ ]]; then
    printf 'https://github.com/%s\n' "${BASH_REMATCH[1]}"
  else
    printf '%s\n' "$value"
  fi
}
prepare_pinned_hive_checkout() {
  local existing_origin actual_commit
  [[ "$HIVE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
    echo "ERROR: REVIEW_HIVE_COMMIT must be a full 40-character commit SHA; branch names like v2 are not allowed." >&2
    return 1
  }
  if [[ -d "${HIVE_SRC_DIR}/.git" ]]; then
    existing_origin="$(git -C "$HIVE_SRC_DIR" remote get-url origin 2>/dev/null || true)"
    [[ -n "$existing_origin" ]] || {
      echo "ERROR: ${HIVE_SRC_DIR} is missing an origin remote; move it aside or delete it so review can recreate the pinned checkout." >&2
      return 1
    }
    if [[ "$(normalize_git_remote "$existing_origin")" != "$(normalize_git_remote "$HIVE_REPO_URL")" ]]; then
      echo "ERROR: ${HIVE_SRC_DIR} points at ${existing_origin}, expected ${HIVE_REPO_URL}." >&2
      echo "  Move it aside or delete it so review can recreate the pinned checkout." >&2
      return 1
    fi
    [[ -z "$(git -C "$HIVE_SRC_DIR" status --porcelain 2>/dev/null)" ]] || {
      echo "ERROR: ${HIVE_SRC_DIR} has local changes; refusing to execute an unverified Hive checkout." >&2
      echo "  Use a clean checkout or delete it so review can recreate the pinned source." >&2
      return 1
    }
  else
    if [[ -e "$HIVE_SRC_DIR" && ! -d "$HIVE_SRC_DIR" ]]; then
      echo "ERROR: ${HIVE_SRC_DIR} exists and is not a directory." >&2
      return 1
    fi
    if [[ -d "$HIVE_SRC_DIR" && -n "$(ls -A "$HIVE_SRC_DIR" 2>/dev/null)" ]]; then
      echo "ERROR: ${HIVE_SRC_DIR} exists but is not a managed git checkout." >&2
      echo "  Move it aside or choose an empty directory before continuing." >&2
      return 1
    fi
    mkdir -p "$HIVE_SRC_DIR"
    git init --quiet "$HIVE_SRC_DIR"
    git -C "$HIVE_SRC_DIR" remote add origin "$HIVE_REPO_URL"
  fi

  echo "Preparing hivecommons/hive @ ${HIVE_COMMIT:0:12} -> ${HIVE_SRC_DIR}..."
  git -C "$HIVE_SRC_DIR" fetch --depth 1 origin "$HIVE_COMMIT"
  git -C "$HIVE_SRC_DIR" checkout --detach -f FETCH_HEAD
  actual_commit="$(git -C "$HIVE_SRC_DIR" rev-parse HEAD)"
  [[ "$actual_commit" == "$HIVE_COMMIT" ]] || {
    echo "ERROR: expected Hive commit ${HIVE_COMMIT}, got ${actual_commit}." >&2
    return 1
  }
}
hive_registration_name() {
  # Which hive registration this launch uses. REVIEW_HIVE names one
  # explicitly; otherwise the current repository's directory names it, so
  # running from another checkout contributes to that project's hive once
  # it is registered. Empty means the default registration.
  HIVE_REGISTRATION_NAME=""
  if [[ -n "${REVIEW_HIVE:-}" ]]; then
    [[ "$REVIEW_HIVE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
      echo "ERROR: REVIEW_HIVE='${REVIEW_HIVE}' is not a valid registration name." >&2
      echo "  Use [a-zA-Z0-9][a-zA-Z0-9_.-]*, e.g. REVIEW_HIVE=endusers." >&2
      return 1
    }
    HIVE_REGISTRATION_NAME="$REVIEW_HIVE"
    return 0
  fi
  command -v git &>/dev/null || return 0
  local top base
  top="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
  base="${top##*/}"
  [[ "$base" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || return 0
  HIVE_REGISTRATION_NAME="$base"
}
register_named_hive() {
  # Register a dedicated hive under this name WITHOUT touching the default
  # registration: upstream contribute-setup writes into a throwaway
  # config_dir and the result is installed as contributor.<name>.env.
  # With HIVE_HUB unset upstream lists the caller's hives and asks which
  # one; an exported HIVE_HUB is honored as-is.
  local target="$1" tmp
  if [[ "${REVIEW_NON_INTERACTIVE:-}" == "true" ]]; then
    print_missing_hive_setup_guidance "$target" "non-interactive mode cannot answer the upstream prompts" goose "$HIVE_COMMIT"
    return 1
  fi
  if ! can_run_attended_hive_setup; then
    echo "ERROR: no hive registration named '${HIVE_REGISTRATION_NAME}' at ${target}." >&2
    echo "  Register one from an interactive terminal: REVIEW_HIVE=${HIVE_REGISTRATION_NAME} just ${REVIEW_RECIPE:-review-container}" >&2
    return 1
  fi
  for cmd in just gh git; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: '${cmd}' is required to run contribute-setup." >&2; return 1; }
  done
  prepare_pinned_hive_checkout || return 1
  echo "Registering hive '${HIVE_REGISTRATION_NAME}': upstream contribute-setup with an isolated config_dir."
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/review-hive-setup.XXXXXX")"
  HIVE_SKIP_VERSION_CHECK=true just --working-directory "$HIVE_SRC_DIR" --justfile "$HIVE_SRC_DIR/Justfile" config_dir="$tmp" contribute-setup goose || {
    rm -rf "$tmp"
    echo "ERROR: upstream contribute-setup did not complete; nothing was registered." >&2
    return 1
  }
  [[ -f "$tmp/contributor.env" ]] || {
    rm -rf "$tmp"
    echo "ERROR: contribute-setup ran but produced no contributor.env." >&2
    return 1
  }
  mkdir -p "${HOME}/.config/hive"
  cp "$tmp/contributor.env" "$target"
  chmod 600 "$target"
  rm -rf "$tmp"
  echo "✓ hive '${HIVE_REGISTRATION_NAME}' registered: ${target}"
}
ensure_hive_contributor_env() {
  # Upstream 'just contribute-setup goose' writes these files. They are the
  # only host state the container genuinely needs, and Hive owns their format.
  # Selection: an explicit REVIEW_HIVE name, then the current repository's
  # name, then the default registration.
  local hive_dir="${HOME}/.config/hive"
  HIVE_CONTRIBUTOR_ENV="${hive_dir}/contributor.env"
  hive_registration_name || return 1
  if [[ -n "$HIVE_REGISTRATION_NAME" ]]; then
    local named="${hive_dir}/contributor.${HIVE_REGISTRATION_NAME}.env"
    if [[ -f "$named" ]]; then
      HIVE_CONTRIBUTOR_ENV="$named"
    elif [[ -n "${REVIEW_HIVE:-}" ]]; then
      register_named_hive "$named" || return 1
      HIVE_CONTRIBUTOR_ENV="$named"
    fi
  fi
  [[ -f "$HIVE_CONTRIBUTOR_ENV" ]] && return 0
  if [[ "${REVIEW_NON_INTERACTIVE:-}" == "true" ]]; then
    print_missing_hive_setup_guidance "$HIVE_CONTRIBUTOR_ENV" "non-interactive mode cannot answer the upstream prompts" goose "$HIVE_COMMIT"
    return 1
  fi
  if ! can_run_attended_hive_setup; then
    print_missing_hive_setup_guidance "$HIVE_CONTRIBUTOR_ENV" "stdin/stdout/stderr are not attached to a terminal" goose "$HIVE_COMMIT"
    return 1
  fi
  echo "Upstream contribute-setup hasn't run yet (no ${HIVE_CONTRIBUTOR_ENV})."
  for cmd in just gh git; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: '${cmd}' is required to run contribute-setup." >&2; return 1; }
  done
  prepare_pinned_hive_checkout || return 1
  echo "Running upstream pinned setup: just contribute-setup goose"
  # HIVE_SKIP_VERSION_CHECK=true is upstream's own documented opt-out, not a
  # local workaround. Upstream's private 'check-version' recipe — a prerequisite
  # of 'contribute-setup' — compares HEAD against origin/v4 and aborts when they
  # differ, printing "Or skip: export HIVE_SKIP_VERSION_CHECK=true". That check
  # assumes a tracking checkout of v4. We deliberately run a pinned, detached
  # SHA (see prepare_pinned_hive_checkout), so the comparison can only ever
  # fail once v4 moves past the pin, and it would abort first-run onboarding on
  # every clean machine. Taking upstream's flag for exactly the case it
  # documents keeps Hive the authority; removing it would break setup without
  # unpinning, and unpinning would mean executing unreviewed upstream code.
  # Scoped to this one invocation so nothing else in the run inherits it.
  HIVE_SKIP_VERSION_CHECK=true just --working-directory "$HIVE_SRC_DIR" --justfile "$HIVE_SRC_DIR/Justfile" contribute-setup goose
  [[ -f "$HIVE_CONTRIBUTOR_ENV" ]] || { echo "ERROR: contribute-setup ran but ${HIVE_CONTRIBUTOR_ENV} still missing." >&2; return 1; }
  echo "✓ Upstream contribute-setup complete."
}
report_hive_selection() {
  # Say out loud which hive this launch contributes to. A silent default is
  # how a contributor ends up watching one hub's dashboard while their agent
  # asks another for work. The token is never printed — the hub only.
  local hub="${1:-}"
  [[ -n "$hub" ]] || hub="$(read_hive_value HIVE_HUB)"
  if [[ -n "$HIVE_REGISTRATION_NAME" && "$HIVE_CONTRIBUTOR_ENV" == *"contributor.${HIVE_REGISTRATION_NAME}.env" ]]; then
    echo "✓ hive: ${hub:-unknown} (registration '${HIVE_REGISTRATION_NAME}')"
  else
    echo "✓ hive: ${hub:-unknown} (default registration)"
    if [[ -n "$HIVE_REGISTRATION_NAME" ]]; then
      echo "  '${HIVE_REGISTRATION_NAME}' has no registration of its own; register one with: REVIEW_HIVE=${HIVE_REGISTRATION_NAME} just ${REVIEW_RECIPE:-review-container}"
    fi
  fi
}
read_hive_value() {
  local key="$1"
  awk -F= -v wanted="$key" '
    {
      name = $1
      sub(/^[[:space:]]*export[[:space:]]+/, "", name)
      gsub(/[[:space:]]/, "", name)
      if (name != wanted) next
      sub(/^[^=]*=/, "")
      sub(/^[[:space:]]+/, "")
      sub(/[[:space:]]+$/, "")
      if (($0 ~ /^".*"$/) || ($0 ~ /^\047.*\047$/)) {
        $0 = substr($0, 2, length($0) - 2)
      }
      print
      exit
    }
  ' "$HIVE_CONTRIBUTOR_ENV"
}
valid_hive_hub() {
  local hub="$1"
  [[ -n "$hub" ]] &&
    [[ "$hub" != *,* ]] &&
    [[ "$hub" =~ ^(wss|https)://[^/@?\#[:space:]]+([/?\#][^[:space:]]*)?$ ]]
}

# ── the optional lab (#379) ────────────────────────────────────────────────
# This appliance owns no lab and depends on none, and none of that changes
# here: what follows is a capability a maintainer can hand ONE dashboard
# session, on their own machine, with their own cluster. Nothing requires it,
# nothing waits for it, and nothing about a pull request's verdict may need
# it — a missing lab only means the deliverable is verified from published
# registry evidence instead.
#
# What crosses into the container is one Unix socket. Not the kubeconfig, not
# a Kubernetes credential, not the host home, not host networking, not the
# Podman socket, not a host binary. The broker on the other end holds all of
# that and answers exactly three typed questions.
REVIEW_LAB_SKILLS=(lab-test k3s-cluster-ops kubernetes-specialist live-dev-common)

lab_broker_script() {
  printf '%s' "${REVIEW_LAB_BROKER:-${PWD}/scripts/review-lab-broker.py}"
}

lab_probe_context() {
  # Host capability, decided before anything is offered and without printing
  # a credential, a kubeconfig path, or a server URL: the probe answers with
  # the current context's NAME and whether nodes can be listed at all.
  LAB_CONTEXT=""
  local broker probe_json
  broker="$(lab_broker_script)"
  [[ -f "$broker" ]] || return 1
  command -v python3 &>/dev/null || return 1
  command -v kubectl &>/dev/null || return 1
  probe_json="$(python3 "$broker" probe 2>/dev/null)" || return 1
  LAB_CONTEXT="$(printf '%s' "$probe_json" | sed -n 's/.*"context":"\([^"]*\)".*/\1/p')"
  [[ -n "$LAB_CONTEXT" ]]
}

lab_runtime_flags() {
  # gVisor blocks host Unix domain sockets by default, so the one socket this
  # session mounts needs runsc's host-uds=open — and ONLY under runsc: crun
  # and runc reject the flag outright, so probing the configured runtime is
  # what keeps the flag from breaking every other machine. review-container
  # never reaches this function: it receives no lab capability at all.
  local runtime
  runtime="$(podman info --format '{{.Host.OCIRuntime.Name}}' 2>/dev/null || true)"
  if [[ "$runtime" == runsc ]]; then
    printf '%s' "--runtime-flag=host-uds=open"
  fi
  return 0
}

start_lab_broker() {
  # The broker lives exactly as long as this foreground session: it is
  # started here, killed by the EXIT trap, and its socket directory removed
  # with it. Nothing survives the terminal that launched it.
  local broker runtime_dir waited
  broker="$(lab_broker_script)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [[ ! -d "$runtime_dir" || ! -w "$runtime_dir" ]]; then
    echo "! no writable XDG_RUNTIME_DIR; the dashboard starts without the lab." >&2
    return 1
  fi
  LAB_SESSION="$(python3 -c 'import secrets; print(secrets.token_hex(8))')" || return 1
  LAB_SOCKET_DIR="$(mktemp -d "${runtime_dir}/bluefin-review-lab.XXXXXX")" || return 1
  chmod 700 "$LAB_SOCKET_DIR"
  LAB_SOCKET="${LAB_SOCKET_DIR}/broker.sock"
  python3 "$broker" serve --socket "$LAB_SOCKET" --session "$LAB_SESSION" \
    >"${LAB_SOCKET_DIR}/broker.log" 2>&1 &
  LAB_BROKER_PID=$!
  for waited in $(seq 1 50); do
    [[ -S "$LAB_SOCKET" ]] && break
    kill -0 "$LAB_BROKER_PID" 2>/dev/null || break
    sleep 0.1
  done
  if [[ ! -S "$LAB_SOCKET" ]]; then
    echo "! the lab broker did not come up; the dashboard starts without the lab." >&2
    cleanup_lab_broker
    return 1
  fi
  return 0
}

cleanup_lab_broker() {
  if [[ -n "${LAB_BROKER_PID:-}" ]]; then
    kill "$LAB_BROKER_PID" 2>/dev/null || true
    wait "$LAB_BROKER_PID" 2>/dev/null || true
    LAB_BROKER_PID=""
  fi
  if [[ -n "${LAB_SOCKET_DIR:-}" && -d "$LAB_SOCKET_DIR" ]]; then
    rm -rf "$LAB_SOCKET_DIR"
    LAB_SOCKET_DIR=""
  fi
  LAB_SOCKET=""
}

offer_lab_session() {
  # Asked once, per dashboard process, on the terminal that launched it.
  # REVIEW_LAB=1/0 answers it without a prompt for an unattended launch;
  # anything else, including no terminal at all, leaves the lab off.
  LAB_SOCKET="" LAB_SESSION="" LAB_SOCKET_DIR="" LAB_BROKER_PID=""
  local answer=""
  [[ "${REVIEW_LAB:-}" == "0" ]] && return 0
  lab_probe_context || return 0
  if [[ "${REVIEW_LAB:-}" == "1" ]]; then
    answer="y"
  elif ( : </dev/tty && : >/dev/tty ) 2>/dev/null; then
    printf '?  Kubernetes context %s is reachable. Use it for this session only? [y/N] ' \
      "$LAB_CONTEXT" >/dev/tty
    read -r answer </dev/tty || answer=""
  else
    return 0
  fi
  [[ "$answer" == [Yy]* ]] || return 0
  start_lab_broker || return 0
  echo "✓ lab enabled for this session (context ${LAB_CONTEXT}); one socket, no credentials."
}

add_lab_container_args() {
  # Only the socket directory and two non-secret strings. The personal lab
  # skills are read-only documents the review agent already knows how to
  # read; common owns the shared lab skill, so only the maintainer's own
  # ids are mounted here, never a second copy of the org inventory.
  [[ -n "${LAB_SOCKET:-}" ]] || return 0
  local flag skill_root skill
  flag="$(lab_runtime_flags)"
  if [[ -n "$flag" ]]; then
    CONTAINER_ARGS+=("$flag")
  fi
  CONTAINER_ARGS+=(--volume "${LAB_SOCKET_DIR}:/run/bluefin-review-lab:rw,z")
  CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_LAB_SOCKET=/run/bluefin-review-lab/broker.sock")
  CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_LAB_SESSION=${LAB_SESSION}")
  skill_root="${REVIEW_PERSONAL_SKILLS:-${HOME}/.copilot/skills}"
  for skill in "${REVIEW_LAB_SKILLS[@]}"; do
    [[ -d "${skill_root}/${skill}" ]] || continue
    CONTAINER_ARGS+=(--volume "${skill_root}/${skill}:/home/dev/.agents/skills/${skill}:ro,z")
  done
}

review_exec_broker_script() {
  printf '%s' "${REVIEW_EXEC_BROKER:-${PWD}/scripts/review-exec-broker.py}"
}

review_exec_probe_context() {
  REVIEW_EXEC_CONTEXT=""
  local broker probe_json
  broker="$(review_exec_broker_script)"
  [[ -f "$broker" ]] || return 1
  command -v python3 &>/dev/null || return 1
  command -v kubectl &>/dev/null || return 1
  probe_json="$(python3 "$broker" probe 2>/dev/null)" || return 1
  REVIEW_EXEC_CONTEXT="$(printf '%s' "$probe_json" | sed -n 's/.*"context":"\([^"]*\)".*/\1/p')"
  [[ -n "$REVIEW_EXEC_CONTEXT" ]]
}

review_exec_runtime_flags() {
  local runtime
  runtime="$(podman info --format '{{.Host.OCIRuntime.Name}}' 2>/dev/null || true)"
  if [[ "$runtime" == runsc ]]; then
    printf '%s' "--runtime-flag=host-uds=open"
  fi
  return 0
}

start_review_exec_broker() {
  local broker runtime_dir
  broker="$(review_exec_broker_script)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [[ ! -d "$runtime_dir" || ! -w "$runtime_dir" ]]; then
    return 1
  fi
  REVIEW_EXEC_SESSION="$(python3 -c 'import secrets; print(secrets.token_hex(8))')" || return 1
  REVIEW_EXEC_SOCKET_DIR="$(mktemp -d "${runtime_dir}/bluefin-review-exec.XXXXXX")" || return 1
  chmod 700 "$REVIEW_EXEC_SOCKET_DIR"
  REVIEW_EXEC_SOCKET="${REVIEW_EXEC_SOCKET_DIR}/broker.sock"
  python3 "$broker" serve \
    --socket "$REVIEW_EXEC_SOCKET" \
    --session "$REVIEW_EXEC_SESSION" \
    --image "${CONTRIBUTOR_IMAGE:-ghcr.io/projectbluefin/review:stable}" \
    >"${REVIEW_EXEC_SOCKET_DIR}/broker.log" 2>&1 &
  REVIEW_EXEC_BROKER_PID=$!
  for _ in $(seq 1 50); do
    [[ -S "$REVIEW_EXEC_SOCKET" ]] && return 0
    kill -0 "$REVIEW_EXEC_BROKER_PID" 2>/dev/null || break
    sleep 0.1
  done
  cleanup_review_exec_broker
  return 1
}

cleanup_review_exec_broker() {
  if [[ -n "${REVIEW_EXEC_BROKER_PID:-}" ]]; then
    kill "$REVIEW_EXEC_BROKER_PID" 2>/dev/null || true
    wait "$REVIEW_EXEC_BROKER_PID" 2>/dev/null || true
    REVIEW_EXEC_BROKER_PID=""
  fi
  if [[ -n "${REVIEW_EXEC_SOCKET_DIR:-}" && -d "$REVIEW_EXEC_SOCKET_DIR" ]]; then
    rm -rf "$REVIEW_EXEC_SOCKET_DIR"
    REVIEW_EXEC_SOCKET_DIR=""
  fi
  REVIEW_EXEC_SOCKET=""
  REVIEW_EXEC_SESSION=""
}

offer_review_exec_session() {
  REVIEW_EXEC_SOCKET="" REVIEW_EXEC_SESSION="" REVIEW_EXEC_SOCKET_DIR="" REVIEW_EXEC_BROKER_PID=""
  [[ "${REVIEW_EXEC:-}" == "0" ]] && return 0
  review_exec_probe_context || return 0
  local answer=""
  if [[ "${REVIEW_EXEC:-}" == "1" ]]; then
    answer="y"
  elif ( : </dev/tty && : >/dev/tty ) 2>/dev/null; then
    printf '?  Kubernetes context %s is reachable. Offload batch reviews to ghost cluster for this session only? [y/N] ' \
      "$REVIEW_EXEC_CONTEXT" >/dev/tty
    read -r answer </dev/tty || answer=""
  else
    return 0
  fi
  [[ "$answer" == [Yy]* ]] || return 0
  start_review_exec_broker || {
    echo "! review-exec broker did not start; reviews remain local." >&2
    return 0
  }
  echo "✓ review-exec enabled for this session (context ${REVIEW_EXEC_CONTEXT}); one socket, no credentials."
}

add_review_exec_container_args() {
  if [[ -n "${REVIEW_EXEC_SOCKET:-}" ]]; then
    local flag
    flag="$(review_exec_runtime_flags)"
    [[ -n "$flag" ]] && CONTAINER_ARGS+=("$flag")
    CONTAINER_ARGS+=(--volume "${REVIEW_EXEC_SOCKET_DIR}:/run/bluefin-review-exec:rw,z")
    CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_EXEC_SOCKET=/run/bluefin-review-exec/broker.sock")
    CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_EXEC_SESSION=${REVIEW_EXEC_SESSION}")
    CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_EXEC_AVAILABLE=1")
  fi
}

scale_cluster_contributors() {
  local replicas="$1" profile="${2:-gemini}" effort="${3:-}"
  command -v kubectl &>/dev/null || {
    echo "ERROR: kubectl is required for cluster contributor scale-out." >&2
    return 1
  }
  local context
  context="$(kubectl config current-context 2>/dev/null || true)"
  if [[ -z "$context" ]]; then
    echo "ERROR: no active Kubernetes context found." >&2
    return 1
  fi

  resolve_model_profile "$profile" "$effort"

  ensure_hive_contributor_env
  local hub
  hub="$(read_hive_value HIVE_HUB)"
  if ! valid_hive_hub "$hub"; then
    echo "ERROR: HIVE_HUB is not set in ${HIVE_CONTRIBUTOR_ENV}." >&2
    return 1
  fi
  CLUSTER_HIVE_HUB="$hub"

  resolve_gh_token
  if [[ -z "${GH_TOKEN_VALUE:-}" ]]; then
    report_missing_gh_token
    echo "ERROR: refusing to update the cluster Secret without a GitHub token." >&2
    return 1
  fi
  resolve_copilot_token
  if [[ -z "${COPILOT_TOKEN:-}" ]]; then
    report_missing_copilot_credential
    echo "ERROR: refusing to update the cluster Secret without a Copilot credential." >&2
    return 1
  fi

  kubectl create namespace bluefin-system --dry-run=client -o yaml |
    kubectl apply -f - >/dev/null || return 1

  kubectl create secret generic review-contributor-secret -n bluefin-system \
    --from-file=contributor.env="${HIVE_CONTRIBUTOR_ENV}" \
    --from-file=GH_TOKEN=<(printf '%s' "$GH_TOKEN_VALUE") \
    --from-file=GITHUB_COPILOT_TOKEN=<(printf '%s' "$COPILOT_TOKEN") \
    --dry-run=client -o yaml |
    kubectl apply --server-side --force-conflicts -f - >/dev/null || return 1
  local legacy_annot
  legacy_annot="$(kubectl get secret review-contributor-secret -n bluefin-system -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}')" || {
    echo "ERROR: failed to read secret annotations." >&2
    return 1
  }
  if [[ -n "$legacy_annot" ]]; then
    kubectl annotate secret review-contributor-secret -n bluefin-system \
      kubectl.kubernetes.io/last-applied-configuration- >/dev/null || {
        echo "ERROR: failed to remove legacy plaintext secret annotation." >&2
        return 1
      }
  fi

  local deploy_file="deploy/review-contributor.yaml"
  if [[ ! -f "$deploy_file" ]]; then
    echo "ERROR: ${deploy_file} not found." >&2
    return 1
  fi
  kubectl apply -f "$deploy_file" >/dev/null || return 1
  kubectl set env deployment/review-contributor -n bluefin-system \
    GOOSE_MODEL="$PROFILE_MODEL" \
    GOOSE_THINKING_EFFORT="$PROFILE_EFFORT" \
    HIVE_HUB="$hub" >/dev/null || return 1
  kubectl scale deployment/review-contributor -n bluefin-system --replicas="$replicas" >/dev/null || return 1
  if [[ "$replicas" -gt 0 ]]; then
    echo "✓ scaled cluster contributor workers to ${replicas} (context ${context}, model ${PROFILE_MODEL} at ${PROFILE_EFFORT} effort)."
    if ! kubectl rollout status deployment/review-contributor -n bluefin-system --timeout=15s 2>/dev/null; then
      echo "! rollout still progressing after 15s; workers will continue pulling/starting in background." >&2
    fi
    kubectl get pods -n bluefin-system -o wide
  else
    echo "✓ scaled cluster contributor workers to 0."
  fi
}

stop_cluster_contributors() {
  if command -v kubectl &>/dev/null && kubectl get deployment review-contributor -n bluefin-system &>/dev/null; then
    kubectl scale deployment/review-contributor -n bluefin-system --replicas=0 >/dev/null
    echo "✓ stopped all cluster contributor workers (scaled to 0 in bluefin-system)."
    return 0
  else
    echo "✓ no cluster contributor deployment found."
    return 0
  fi
}
'''

# Run the contributor container: the Hive queue worker.
# Receives Hive-assigned tasks and donates inference through the
# maintainer's credentials.
#
#   just review-container              # gemini: gemini-3.8-flash at max effort
#   just review-container gemini       # the same, named explicitly
#   just review-container sol          # gpt-5.6-sol, medium effort
#   just review-container opus5 high   # claude-opus-5, high effort, 264k context
#   just review-container k3           # kimi-k3, max effort, 264k context
#
# One instance owns the 'review-container' name, so a second concurrent agent
# needs a name of its own:
#
#   REVIEW_CONTAINER_NAME=review-container-2 just review-container opus5 high
#
# Usage: just review-container [gemini|sol|gpt-sol|opus5|k3|kimi] [low|medium|high|max]
# Env:   REVIEW_CONTAINER_NAME=<name>  run a concurrent second instance
#        (default 'review-container'; must match [a-zA-Z0-9][a-zA-Z0-9_.-]*)
#        REVIEW_HIVE=<name>  use ~/.config/hive/contributor.<name>.env; when
#        missing, register a hive under that name. Without it, the current
#        repository's directory name is tried, then the default
#        ~/.config/hive/contributor.env.
[doc("Run the Hive contributor worker: receive assigned tasks and donate inference.")]
review-container profile="" effort="":
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    GEMINI_MODEL="{{gemini_model}}"
    OPUS_MODEL="{{opus_model}}"
    OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"
    SOL_MODEL="{{sol_model}}"
    K3_MODEL="{{k3_model}}"
    K3_CONTEXT_LIMIT="{{k3_context_limit}}"

    command -v podman &>/dev/null || {
      echo "ERROR: Podman is required to run the contributor container." >&2
      echo "  Install Podman, then re-run review-container." >&2
      exit 1
    }

    STATE_DIR="${HOME}/.local/state/review"
    HIVE_SRC_DIR="${STATE_DIR}/hive-src"
    HIVE_REPO_URL="{{hive_repo_url}}"
    HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"
    HIVE_COMMIT="${HIVE_COMMIT,,}"
    mkdir -p "${STATE_DIR}"

    require_goose_backend "$TOOL"
    BACKEND="${TOOL:-goose}"
    preflight_agent "$BACKEND"

    raw_profile="{{profile}}"
    raw_effort="{{effort}}"

    if [[ "$raw_profile" == "cluster" || -n "${REVIEW_SCALE:-}" ]]; then
      replicas="${REVIEW_SCALE:-2}"
      model_profile="gemini"
      model_effort="max"
      if [[ "$raw_profile" == "cluster" ]]; then
        if [[ "$raw_effort" =~ ^[0-9]+$ ]]; then
          replicas="$raw_effort"
        elif [[ -n "$raw_effort" ]]; then
          model_profile="$raw_effort"
        fi
      elif [[ -n "$raw_profile" ]]; then
        model_profile="$raw_profile"
        model_effort="${raw_effort:-max}"
      fi
      scale_cluster_contributors "$replicas" "$model_profile" "$model_effort"
      exit 0
    fi

    # Resolved before anything interactive so a typo fails immediately rather
    # than after the model picker and the Hive setup.
    CONTAINER_NAME="${REVIEW_CONTAINER_NAME:-review-container}"
    require_valid_container_name "$CONTAINER_NAME"

    resolve_model_profile "{{profile}}" "{{effort}}"
    if [[ "$BACKEND" == pi ]]; then
      export ANTHROPIC_API_KEY="$PI_API_KEY"
    elif [[ "$BACKEND" == goose ]]; then
      resolve_goose_selection
    fi
    REVIEW_RECIPE=review-container
    ensure_hive_contributor_env
    report_hive_selection

    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    require_no_running_instance "$CONTAINER_NAME"
    ensure_contributor_image "$CONTRIBUTOR_IMAGE"

    # REVIEW_DETACH=1 runs the worker as a deliberate background container:
    # no terminal, entrypoint follows the agent without attaching, logs
    # through podman, and 'just review-stop' is the explicit lifecycle verb.
    # The 'detached' owner label is what separates this from an orphan, so a
    # later launch refuses to reclaim it silently.
    DETACH="${REVIEW_DETACH:-0}"
    if [[ "$DETACH" == 1 ]]; then
      CONTAINER_ARGS=(
        podman run --rm --detach --replace --name "$CONTAINER_NAME"
        --label "review.owner=detached"
      )
    else
      CONTAINER_ARGS=(
        podman run --rm --interactive --tty --replace --name "$CONTAINER_NAME"
        --label "$(owner_run_label)"
      )
    fi
    CONTAINER_ARGS+=(
      # Rootless podman maps the host user to container root by default, so a
      # 0600 host file bind-mounts in as root-owned and the 'dev' user the
      # image runs as cannot read it -- contributor.env holds Hive's own
      # settings and is exactly that. Mapping the host user onto dev's uid
      # instead makes the mount readable without loosening the host mode.
      --userns "keep-id:uid=1000,gid=1000"
      # The selected registration, and nothing else from ~/.config/hive.
      #
      # This used to also bind-mount the whole directory, with the selected
      # file overlaid on top. Rootless Podman prepares the nested target
      # through the already-mounted host directory, so with a named
      # registration (REVIEW_HIVE=<name>) the target creation escaped back to
      # the host: it created a zero-byte ~/.config/hive/contributor.env owned
      # by a subordinate uid, and the container then failed on the file it had
      # just caused to exist. Nothing in the image reads anything else from
      # that directory, so one file mount is both the fix and the smaller
      # exposure -- a named worker can no longer see other registrations.
      #
      # ':z' is the shared SELinux relabel. ':Z' would give each container a
      # private MCS category, and review supports concurrent named workers
      # sharing one registration: the second launch would revoke the first
      # live container's access to it.
      --volume "${HIVE_CONTRIBUTOR_ENV}:/home/dev/.config/hive/contributor.env:ro,z"
      --env "AGENT_BACKEND=${BACKEND}"
      # Podman does not pass COLORTERM through on its own; the entrypoint
      # needs it to pick the direct-color attach fallback for a host TERM
      # the image's narrow terminfo set does not know (e.g. xterm-ghostty).
      --env COLORTERM
    )
    [[ "$BACKEND" == goose && -n "$GOOSE_PROVIDER" ]] && CONTAINER_ARGS+=(--env "GOOSE_PROVIDER=${GOOSE_PROVIDER}")
    if [[ "$BACKEND" == goose ]]; then
      [[ -n "$GOOSE_MODEL" ]] && CONTAINER_ARGS+=(--env "GOOSE_MODEL=${GOOSE_MODEL}")
      [[ -n "${GOOSE_THINKING_EFFORT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_THINKING_EFFORT=${GOOSE_THINKING_EFFORT}")
      [[ -n "${GOOSE_CONTEXT_LIMIT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_CONTEXT_LIMIT=${GOOSE_CONTEXT_LIMIT}")
    fi
    if [[ "${GOOSE_PROVIDER:-}" == "github_copilot" ]]; then
      resolve_copilot_token
      if [[ -n "${COPILOT_TOKEN:-}" ]]; then
        export GITHUB_COPILOT_TOKEN="$COPILOT_TOKEN"
        CONTAINER_ARGS+=(--env GITHUB_COPILOT_TOKEN)
        echo "✓ Copilot credential passed to the agent."
      else
        report_missing_copilot_credential
      fi
    fi
    if [[ "$BACKEND" == pi ]]; then
      CONTAINER_ARGS+=(--env ANTHROPIC_API_KEY)
      echo "✓ Pi credential passed to the agent (value not shown)."
    fi
    CODEX_AUTH_STAGING_DIR=""
    trap cleanup_codex_auth_file EXIT
    if [[ "$BACKEND" == codex ]]; then
      stage_codex_auth_file
      if [[ "$DETACH" == 1 ]]; then
        CONTAINER_ARGS+=(--label "review.codex-auth=${CODEX_AUTH_STAGING_DIR}")
      fi
      CONTAINER_ARGS+=(--volume "$CODEX_AUTH_FILE:/home/dev/.codex/auth.json:rw,z")
      echo "✓ Codex subscription login staged as one private file (contents not shown; host cache not mounted)."
    fi
    resolve_gh_token
    if [[ -n "${GH_TOKEN_VALUE:-}" ]]; then
      export GH_TOKEN="$GH_TOKEN_VALUE"
      CONTAINER_ARGS+=(--env GH_TOKEN)
      report_gh_token_blast_radius "${GH_TOKEN_SOURCE}"
    else
      report_missing_gh_token
    fi
    CONTAINER_ARGS+=("$CONTRIBUTOR_IMAGE")

    if [[ "$DETACH" == 1 ]]; then
      echo "✓ starting the review contributor worker (detached)."
      echo "  Follow it:  podman logs -f ${CONTAINER_NAME}"
      echo "  Stop it:    just review-stop ${CONTAINER_NAME}"
    else
      echo "✓ starting the review contributor container."
      echo "  The entrypoint attaches to the 'contributor' tmux session for you."
      echo "  From a second terminal: podman exec -it ${CONTAINER_NAME} tmux attach -t contributor"
      echo "  Stop any time with Ctrl-C."
    fi
    if [[ "$BACKEND" == codex ]]; then
      if "${CONTAINER_ARGS[@]}"; then
        status=0
      else
        status=$?
        cleanup_codex_auth_file
      fi
      if [[ "$DETACH" == 1 && "$status" == 0 ]]; then
        trap - EXIT
      else
        cleanup_codex_auth_file
      fi
      exit "$status"
    fi
    exec "${CONTAINER_ARGS[@]}"

# Start an unattended contributor worker. The existing launcher owns all
# credential checks and lifecycle behavior; this selects its explicit
# detached path so the worker survives the launching terminal.
[doc("Start an unattended Hive contributor worker; local runs detach by default.")]
contribute profile="" effort="":
    #!/usr/bin/env bash
    set -euo pipefail
    REVIEW_DETACH=1 just review-container "{{profile}}" "{{effort}}"

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
    if [[ "$NAME" == "cluster" ]]; then
      stop_cluster_contributors
      exit 0
    fi
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
    codex_auth_staging_dir="$(podman inspect --format '{{{{index .Config.Labels "review.codex-auth"}}' "$NAME" 2>/dev/null || true)"
    podman stop "$NAME" >/dev/null
    cleanup_codex_auth_staging_dir "$codex_auth_staging_dir"
    echo "✓ stopped the detached worker ${NAME}."

# The maintainer review dashboard over the Bluefin PR queue.
# The container runs the dashboard instead of the contributor agent, so no
# Hive registration is mounted or required. When one exists, only its HIVE_HUB
# URL is passed so the dashboard can consult the selected deployment.
# Foreground: q or Ctrl-C stops.
# Arguments pass straight through to the dashboard:
#
#   just review-queue                      # gemini: gemini-3.8-flash at max effort
#   just review-queue sol                  # gpt-5.6-sol at medium effort
#   just review-queue k3 high              # pick the model profile and effort
#   just review-queue owner/repo            # live open PRs for one repository
#   just review-queue --repo bluefin       # narrow the org queue to one repository
#   just review-queue opus5 --all          # profile, then dashboard flags
#
# One instance owns the 'review-queue' name; REVIEW_QUEUE_NAME overrides it
# for a concurrent second dashboard, exactly as REVIEW_CONTAINER_NAME does for
# review-container.
[doc("Open the maintainer review dashboard over the Bluefin PR queue.")]
review-queue *queue_args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    GEMINI_MODEL="{{gemini_model}}"
    OPUS_MODEL="{{opus_model}}"
    OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"
    SOL_MODEL="{{sol_model}}"
    K3_MODEL="{{k3_model}}"
    K3_CONTEXT_LIMIT="{{k3_context_limit}}"

    resolve_review_backend
    K8S_DASHBOARD=0
    if [[ "${REVIEW_RUNTIME:-}" == k8s ]]; then
      if review_queue_kubernetes_available; then
        K8S_DASHBOARD=1
      else
        runtime_status=$?
        if [[ "$runtime_status" == 2 ]]; then
          exit 1
        fi
        echo "! Kubernetes is unavailable; using the local Podman dashboard." >&2
      fi
    fi
    if [[ "$K8S_DASHBOARD" != 1 ]]; then
      command -v podman &>/dev/null || {
        echo "ERROR: Podman is required to run the contributor container." >&2
        echo "  Install Podman, then re-run review-queue." >&2
        exit 1
      }
      require_local_podman_engine
    fi

    require_goose_backend "$TOOL"
    if [[ "$REVIEW_BACKEND" == codex ]]; then
      preflight_github
    else
      preflight_agent
    fi

    CONTAINER_NAME="${REVIEW_QUEUE_NAME:-review-queue}"
    require_valid_container_name "$CONTAINER_NAME"

    # Leading non-flag arguments are the model profile and thinking effort,
    # exactly as review-container takes them; everything from the first '-'
    # flag onward belongs to the dashboard. Word-splitting {{queue_args}} is
    # the point: it arrives as one string of separate flags.
    # shellcheck disable=SC2086
    set -- {{queue_args}}
    profile="" effort=""
    if [[ $# -gt 0 && "$1" != -* && "$1" != */* ]]; then profile="$1"; shift; fi
    if [[ $# -gt 0 && "$1" != -* && "$1" != */* ]]; then effort="$1"; shift; fi
    resolve_model_profile "${profile:-gemini}" "$effort"
    # The unambiguous repository form follows the existing profile/effort
    # pair. Keep all flag forms byte-for-byte available to the dashboard.
    if [[ $# -gt 0 && "$1" != -* ]]; then
      set -- --live-repo "$1" "${@:2}"
    fi
    [[ "$REVIEW_BACKEND" == codex ]] || resolve_goose_selection

    # The dashboard needs only the selected hub URL. Resolve the same named
    # registration as review-container, but never mount contributor.env or pass
    # its registration token into the maintainer surface.
    HIVE_CONTRIBUTOR_ENV="${HOME}/.config/hive/contributor.env"
    hive_registration_name
    if [[ -n "$HIVE_REGISTRATION_NAME" ]]; then
      named_hive_env="${HOME}/.config/hive/contributor.${HIVE_REGISTRATION_NAME}.env"
      if [[ -f "$named_hive_env" ]]; then
        HIVE_CONTRIBUTOR_ENV="$named_hive_env"
      elif [[ -n "${REVIEW_HIVE:-}" ]]; then
        echo "ERROR: no hive registration named '${HIVE_REGISTRATION_NAME}' at ${named_hive_env}." >&2
        echo "  Register it first: REVIEW_HIVE=${HIVE_REGISTRATION_NAME} just review-container" >&2
        exit 1
      fi
    fi
    DASHBOARD_HIVE_HUB="${HIVE_HUB:-}"
    if [[ -n "$DASHBOARD_HIVE_HUB" ]]; then
      if ! valid_hive_hub "$DASHBOARD_HIVE_HUB"; then
        echo "! inherited HIVE_HUB is unsupported; the dashboard requires one wss:// or https:// URL and will continue without Hive." >&2
        DASHBOARD_HIVE_HUB=""
      fi
    elif [[ -f "$HIVE_CONTRIBUTOR_ENV" ]]; then
      DASHBOARD_HIVE_HUB="$(read_hive_value HIVE_HUB)"
      if [[ -z "$DASHBOARD_HIVE_HUB" ]]; then
        echo "! ${HIVE_CONTRIBUTOR_ENV} has no usable HIVE_HUB; the dashboard will continue without Hive." >&2
      elif ! valid_hive_hub "$DASHBOARD_HIVE_HUB"; then
        echo "! ${HIVE_CONTRIBUTOR_ENV} has an unsupported HIVE_HUB; the dashboard requires one wss:// or https:// URL and will continue without Hive." >&2
        DASHBOARD_HIVE_HUB=""
      fi
    fi

    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    if [[ "$K8S_DASHBOARD" != 1 ]]; then
      require_no_running_instance "$CONTAINER_NAME"
      ensure_contributor_image "$CONTRIBUTOR_IMAGE"
    fi

    # The lab offer happens before anything starts and answers in one
    # question. Declining, no terminal, no kubectl, or an unreachable
    # cluster all leave LAB_SOCKET empty and the dashboard fully usable.
    if [[ "$K8S_DASHBOARD" != 1 ]]; then
      offer_lab_session
      offer_review_exec_session
    fi

    CONTAINER_ARGS=(
      podman run --rm --interactive --tty --replace --name "$CONTAINER_NAME"
      --label "$(owner_run_label)"
      --userns "keep-id:uid=1000,gid=1000"
      # Podman does not pass COLORTERM through on its own.
      --env COLORTERM
    )
    add_lab_container_args
    add_review_exec_container_args
    # The dashboard's record — dispatched landing batches, their failure
    # reasons, the action trace — lives under the container's XDG state
    # directory, and a reclaim-by-replace relaunch must not lose it (#281).
    # One shared host directory under the host XDG state root: the queue it
    # records is the same whichever instance name runs, and :z keeps it
    # writable for concurrent named dashboards.
    if [[ "$K8S_DASHBOARD" != 1 ]]; then
      QUEUE_STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/bluefin-review"
      mkdir -p "$QUEUE_STATE_DIR"
      CONTAINER_ARGS+=(--volume "${QUEUE_STATE_DIR}:/home/dev/.local/state/bluefin-review:rw,z")
    fi
    # The instance name qualifies each landing batch id: two named dashboards
    # share the state directory, and a bare timestamp id would let their
    # batches overwrite each other's prompt, status, and log.
    export BLUEFIN_REVIEW_INSTANCE="$CONTAINER_NAME"
    CONTAINER_ARGS+=(--env BLUEFIN_REVIEW_INSTANCE)
    if [[ -n "$DASHBOARD_HIVE_HUB" ]]; then
      CONTAINER_ARGS+=(--env "HIVE_HUB=${DASHBOARD_HIVE_HUB}")
      report_hive_selection "$DASHBOARD_HIVE_HUB"
    fi
    if [[ "$REVIEW_BACKEND" != codex ]]; then
      [[ -n "$GOOSE_PROVIDER" ]] && CONTAINER_ARGS+=(--env "GOOSE_PROVIDER=${GOOSE_PROVIDER}")
      [[ -n "$GOOSE_MODEL" ]] && CONTAINER_ARGS+=(--env "GOOSE_MODEL=${GOOSE_MODEL}")
      [[ -n "${GOOSE_THINKING_EFFORT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_THINKING_EFFORT=${GOOSE_THINKING_EFFORT}")
      [[ -n "${GOOSE_CONTEXT_LIMIT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_CONTEXT_LIMIT=${GOOSE_CONTEXT_LIMIT}")
    fi
    if [[ -n "$REVIEW_BACKEND" ]]; then
      CONTAINER_ARGS+=(--env "BLUEFIN_REVIEW_BACKEND=${REVIEW_BACKEND}")
      echo "✓ review backend preselected: ${REVIEW_BACKEND}; Start still requires confirmation."
    fi
    # The Copilot credential is what powers 'r' (the Goose review of a pull
    # request); the dashboard itself only reads GitHub, so a missing credential
    # is a warning, not a stop.
    if [[ "$REVIEW_BACKEND" != codex ]]; then
      resolve_copilot_token
      if [[ -n "${COPILOT_TOKEN:-}" ]]; then
        export GITHUB_COPILOT_TOKEN="$COPILOT_TOKEN"
        CONTAINER_ARGS+=(--env GITHUB_COPILOT_TOKEN)
        echo "✓ Copilot credential passed to the agent."
      else
        report_missing_copilot_credential
      fi
    fi
    # Codex subscription OAuth is staged into one private file, not mounted
    # from the host login or configuration directory. The official CLI may
    # refresh only the disposable copy, which is removed when this run exits.
    CODEX_AUTH_STAGING_DIR=""
    # One trap owns this session's teardown: the staged Codex credential,
    # the lab broker, and the review-exec broker die with the terminal.
    trap 'cleanup_kubernetes_dashboard; cleanup_codex_auth_file; cleanup_lab_broker; cleanup_review_exec_broker' EXIT
    if [[ "$REVIEW_BACKEND" == codex ]]; then
      if [[ "$K8S_DASHBOARD" != 1 ]]; then
        stage_codex_auth_file
        if [[ -n "$CODEX_AUTH_FILE" ]]; then
          CONTAINER_ARGS+=(--volume "$CODEX_AUTH_FILE:/home/dev/.codex/auth.json:rw,z")
          echo "✓ Codex subscription login staged as one private file (contents not shown; host cache not mounted)."
        else
          echo "! Codex subscription login unavailable; run 'codex login' with file credential storage." >&2
          echo "  Review stays open, reports NEEDS SIGN-IN, and never silently selects Codex." >&2
        fi
      else
        echo "! Kubernetes dashboard sessions do not stage a Codex subscription login." >&2
      fi
    fi
    # The dashboard is a GitHub reader from the first keystroke to the last, so
    # an identity is load-bearing here, not advisory.
    resolve_gh_token
    if [[ -z "${GH_TOKEN_VALUE:-}" ]]; then
      report_missing_gh_token
      echo "ERROR: the dashboard reads live pull-request state from GitHub and cannot run without a token." >&2
      exit 1
    fi
    export GH_TOKEN="$GH_TOKEN_VALUE"
    CONTAINER_ARGS+=(--env GH_TOKEN)
    report_gh_token_blast_radius "${GH_TOKEN_SOURCE}"

    if [[ "$K8S_DASHBOARD" == 1 ]]; then
      echo "✓ starting the maintainer review dashboard in Kubernetes."
      echo "  q or Ctrl-C stops; the dashboard is the only thing running."
      review_queue_kubernetes "$CONTRIBUTOR_IMAGE" "$@"
      exit $?
    fi

    # Whatever survived the profile/effort shift belongs to the dashboard.
    CONTAINER_ARGS+=("$CONTRIBUTOR_IMAGE" queue "$@")

    if [[ -n "$DASHBOARD_HIVE_HUB" ]]; then
      echo "✓ starting the maintainer review dashboard (Hive configured)."
    else
      echo "✓ starting the maintainer review dashboard (Hive not configured)."
    fi
    echo "  q or Ctrl-C stops; the dashboard is the only thing running."
    "${CONTAINER_ARGS[@]}"

# Scale 3 cluster workers and open the maintainer review dashboard in the foreground.
# Workers continue running in the cluster after the dashboard exits.
#
#   just turbo-review                      # default gemini profile, 3 cluster workers
#   just turbo-review sol                  # sol profile, 3 cluster workers
#   just turbo-review projectbluefin/review # live review of one repository
[doc("Scale 3 cluster workers and open the maintainer review dashboard.")]
turbo-review *args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    GEMINI_MODEL="{{gemini_model}}"
    OPUS_MODEL="{{opus_model}}"
    OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"
    SOL_MODEL="{{sol_model}}"
    K3_MODEL="{{k3_model}}"
    K3_CONTEXT_LIMIT="{{k3_context_limit}}"

    replicas="${REVIEW_SCALE:-3}"
    profile="gemini"
    effort=""
    CLUSTER_HIVE_HUB=""
    # Leading non-flag arguments are the model profile and thinking effort,
    # exactly as review-queue takes them. Keep "$@" intact for the dashboard.
    # shellcheck disable=SC2086
    set -- {{args}}
    if [[ $# -gt 0 && "$1" != -* && "$1" != */* ]]; then
      profile="$1"
      if [[ $# -gt 1 && "$2" != -* && "$2" != */* ]]; then effort="$2"; fi
    fi

    echo "=== Launching review turbo ==="
    if command -v kubectl &>/dev/null && [[ -n "$(kubectl config current-context 2>/dev/null || true)" ]]; then
      echo "✓ scaling ${replicas} cluster contributor workers in bluefin-system..."
      scale_cluster_contributors "$replicas" "$profile" "$effort" || {
        echo "! cluster worker scale-out failed; continuing with local review dashboard." >&2
      }
      if [[ -n "$CLUSTER_HIVE_HUB" ]]; then
        export HIVE_HUB="$CLUSTER_HIVE_HUB"
      fi
    else
      echo "! no active Kubernetes context found; continuing with local review dashboard only." >&2
    fi

    report_cluster_exit_status() {
      echo ""
      echo "=== Cluster contributor status ==="
      if ! command -v kubectl &>/dev/null; then
        echo "! kubectl is unavailable; cluster contributor status was not checked."
      elif ! kubectl get deployment review-contributor -n bluefin-system &>/dev/null; then
        echo "! unable to read cluster contributor status in bluefin-system."
      else
        ready="$(kubectl get deployment review-contributor -n bluefin-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
        total="$(kubectl get deployment review-contributor -n bluefin-system -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
        echo "✓ ${ready:-0}/${total:-0} cluster contributor workers active in bluefin-system."
      fi
      echo "  Stop workers: just review-stop cluster"
      echo "  Check health: just review-doctor"
    }
    trap report_cluster_exit_status EXIT

    echo "✓ starting maintainer review dashboard in foreground..."
    # Execute review-queue as a child process to isolate environment variables and traps.
    just review-queue "$@"

# Preflight check: is this machine actually ready for 'just review-container'?
# Starts no agent and mounts no credential.
[doc("Preflight diagnostics for this machine. Starts no agent.")]
review-doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    {{shared_functions}}
    HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"
    HIVE_COMMIT="${HIVE_COMMIT,,}"
    pass=0; fail=0
    check() {
      local label="$1"; shift
      if "$@" &>/dev/null; then echo "  ✓ ${label}"; pass=$((pass+1));
      else echo "  ✗ ${label}"; fail=$((fail+1)); fi
    }
    echo "=== Host ==="
    check "Podman installed" command -v podman
    echo ""

    echo "=== GitHub ==="
    if github_auth_ready; then
      echo "  ✓ gh is authenticated against github.com"
      pass=$((pass+1))
    else
      echo "  ✗ gh is not authenticated against github.com"
      echo "    Run: ${GITHUB_LOGIN_COMMAND}"
      fail=$((fail+1))
    fi
    resolve_gh_token
    if [[ -n "${GH_TOKEN_VALUE:-}" ]]; then
      echo "  ✓ a GitHub token is available for the container-only agent (from ${GH_TOKEN_SOURCE}; not shown)"
      DOCTOR_GH_SCOPES="$(gh_token_scopes)"
      [[ -n "$DOCTOR_GH_SCOPES" ]] && echo "    The agent will be able to do anything this token can: ${DOCTOR_GH_SCOPES}"
      echo "    Narrow that with REVIEW_GH_TOKEN=<scoped PAT> if that is wider than you want."
      pass=$((pass+1))
    else
      echo "  ✗ no GitHub token is available for the container-only agent"
      echo "    It could not fork, push, or open a pull request, and would stop at 'gh auth login'."
      echo "    For container-only mode, run: ${GITHUB_LOGIN_COMMAND}, or export REVIEW_GH_TOKEN."
      fail=$((fail+1))
    fi
    unset GH_TOKEN_VALUE
    echo ""

    BACKEND="${TOOL:-goose}"
    require_goose_backend "$BACKEND" || fail=$((fail+1))
    if [[ "$BACKEND" == pi ]]; then
      echo "=== Agent backend (Pi) ==="
      if [[ -n "${PI_API_KEY:-}" ]]; then
        echo "  ✓ pi: selected; image verifies the installed binary and the credential is available (not shown)"
        pass=$((pass+1))
      else
        echo "  ✗ pi: selected, but PI_API_KEY is missing"
        echo "    Export PI_API_KEY before running TOOL=pi just review-container."
        fail=$((fail+1))
      fi
    else
      echo "=== Agent backend (Goose) ==="
      if ! require_copilot_provider; then
        fail=$((fail+1))
      elif command -v goose &>/dev/null; then
        if goose_configured; then
          echo "  ✓ goose: installed + configured"
          pass=$((pass+1))
        else
          echo "  ✗ goose: installed, NOT configured — ${GOOSE_FIXIT_HINT}"
          fail=$((fail+1))
        fi
      else
        echo "  ✗ goose: not installed — ${GOOSE_INSTALL_HINT}"
        fail=$((fail+1))
      fi
      echo ""

      echo "=== Copilot credential ==="
      resolve_copilot_token
      if [[ -n "${COPILOT_TOKEN:-}" ]]; then
        echo "  ✓ a Copilot credential is available (not shown)"
        pass=$((pass+1))
      else
        echo "  ✗ no Copilot credential is available"
        echo "    The agent will stop at 'enter code XXXX-XXXX' and wait for a human."
        echo "    A 'gh auth token' is NOT a substitute — Copilot inference rejects it."
        echo "    Run: goose configure (pick GitHub Copilot), or export GITHUB_COPILOT_TOKEN."
        fail=$((fail+1))
      fi
      unset COPILOT_TOKEN
    fi
    echo ""

    echo "=== Contributor image ==="
    DOCTOR_CONTRIBUTOR_IMAGE="{{contributor_image}}"
    if contributor_image_available "$DOCTOR_CONTRIBUTOR_IMAGE"; then
      echo "  ✓ ${DOCTOR_CONTRIBUTOR_IMAGE} is resolvable"
      pass=$((pass+1))
    else
      echo "  ✗ ${DOCTOR_CONTRIBUTOR_IMAGE} cannot be resolved"
      echo "    Published tags are 'stable', the version tags and 'sha-<commit>'; ':latest' does not exist."
      echo "    Override with REVIEW_CONTRIBUTOR_IMAGE (a 'sha-' tag or digest pins a build), or build image/Containerfile locally."
      fail=$((fail+1))
    fi
    echo ""

    echo "=== Hive contributor setup ==="
    hive_registration_name || true
    HIVE_CONTRIBUTOR_ENV="${HOME}/.config/hive/contributor.env"
    if [[ -n "${HIVE_REGISTRATION_NAME:-}" ]] &&
      [[ -f "${HOME}/.config/hive/contributor.${HIVE_REGISTRATION_NAME}.env" ]]; then
      HIVE_CONTRIBUTOR_ENV="${HOME}/.config/hive/contributor.${HIVE_REGISTRATION_NAME}.env"
    fi
    if [[ -f "$HIVE_CONTRIBUTOR_ENV" ]]; then
      echo "  ✓ ${HIVE_CONTRIBUTOR_ENV} exists"
      pass=$((pass+1))
      DOCTOR_BACKEND="$(hive_contributor_backend "$HIVE_CONTRIBUTOR_ENV")"
      if [[ -n "$DOCTOR_BACKEND" && "$DOCTOR_BACKEND" != "$BACKEND" ]]; then
        echo "  ! ${HIVE_CONTRIBUTOR_ENV} says AGENT_BACKEND=${DOCTOR_BACKEND}, but the selected backend is ${BACKEND}."
        echo "    The launcher will not rewrite Hive's saved backend selection."
        echo "    Edit that line yourself if you want the file to match."
      fi
    else
      echo "  ✗ ${HIVE_CONTRIBUTOR_ENV} is missing"
      echo "    review runs upstream 'just contribute-setup goose' from"
      echo "    hivecommons/hive @ ${HIVE_COMMIT:0:12} on first attended launch."
      echo "    That runs with upstream's documented HIVE_SKIP_VERSION_CHECK=true,"
      echo "    because the pinned checkout is detached and cannot match origin/v4."
      fail=$((fail+1))
    fi
    echo ""

    echo "=== Cluster scale-out ==="
    if command -v kubectl &>/dev/null; then
      k8s_ctx="$(kubectl config current-context 2>/dev/null || true)"
      if [[ -n "$k8s_ctx" ]]; then
        echo "  ✓ Kubernetes context: ${k8s_ctx}"
        if kubectl get deployment review-contributor -n bluefin-system &>/dev/null; then
          ready_rep="$(kubectl get deployment review-contributor -n bluefin-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
          spec_rep="$(kubectl get deployment review-contributor -n bluefin-system -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
          echo "  ✓ review-contributor: ${ready_rep:-0}/${spec_rep:-0} ready replicas in bluefin-system"
        else
          echo "  - review-contributor: not deployed (scale with 'just review-container cluster [N]')"
        fi
      else
        echo "  - kubectl installed, no active context"
      fi
    else
      echo "  - kubectl not installed (optional; for cluster scale-out)"
    fi
    echo ""

    echo "=== Workspace model ==="
    echo "  ✓ assigned repositories are cloned inside the disposable container"
    echo ""
    echo "${pass} checks passed, ${fail} failed."
    [[ "$fail" -eq 0 ]] || exit 1
