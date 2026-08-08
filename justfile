# justfile — the local review launcher entrypoint.
#
# The system image install path is still out of scope here; this root justfile
# is the foreground launcher a checkout exposes directly.
#
# This is the ONLY file that ships/installs. Everything review needs
# (host preflight, Goose selection, VM lifecycle, container lifecycle) is
# embedded below as private ('_'-prefixed variables and shared shell
# functions) on purpose: a user browsing the image or this repo should find
# one just-recipe file and the commands it exposes, not a scattered bin/ of
# standalone scripts they might stumble into and run directly out of context.
#
# Public commands:
#   review            Boot the pinned QEMU VM in the FOREGROUND and
#                     hand the terminal to the contributor agent.
#   review-container  Run only the contributor container — no VM —
#                     for quick local development. Also foreground.
#                     Takes an optional model profile and thinking effort,
#                     e.g. 'just review-container opus5 high'.
#   review-doctor     Read-only preflight diagnostics. Starts nothing.
#   review-queue      Walk the Bluefin PR queue in the contributor
#                     container — no Hive, no VM. Foreground; q or
#                     Ctrl-C stops. Takes the same model profile and
#                     effort as review-container, then passes the rest
#                     through to `bluefin-review queue`, e.g.
#                     'just review-queue kimi high --repo bluefin'.
#
# ─────────────────────────────────────────────────────────────────────────
# FOREGROUND GUARANTEE
#
# The VM and the container ALWAYS run in the foreground of the terminal that
# launched them. There is no '&', no 'nohup', no 'setsid', no 'podman run
# -d', and no '--detach' on any launch path in this file, and there never
# should be. Two reasons, both non-negotiable:
#
#   1. A human must be able to steer the agent. review is an
#      attended tool: you watch the session, you interrupt it, you type into
#      it. A backgrounded agent is an agent nobody is supervising.
#   2. Ctrl-C must stop it. Signals have to reach the QEMU process or the
#      container directly, so the run dies with the terminal. No daemon, no
#      systemd unit, no orphaned state to reap later.
#
# It follows that there is NO stop command, and there must never be one.
# Shipping 'just review-stop' would be an admission that a run can outlive the
# terminal that started it — the exact property this file exists to prevent.
# Ctrl-C is the stop button. A run that needs a second command to end it is a
# daemon wearing a disguise, and this repository does not ship daemons.
# Cleanup is therefore a startup concern, never a user-facing verb: a launch
# reclaims whatever a previous run left behind.
#
# '--replace' is how that reclaim works, and it is not a hole in the
# guarantee: --rm removes the container when it exits cleanly, but a
# hard-killed terminal, an OOM kill or a podman restart can leave the fixed
# name behind, and the next launch would otherwise die with 'the container
# name ... is already in use'. --replace takes the name back at launch time
# instead of asking the user to run a lifecycle command.
#
# Concretely: the final process of every launch path is either 'exec'd (so
# it replaces this shell and inherits its signals) or is the last foreground
# command whose exit status is propagated verbatim. The only background job
# in this file is the short-lived, host-local bootstrap socket server, which
# is reaped by an EXIT/INT/TERM trap and never outlives the run.
# tests/just-onboarding.sh enforces this by grepping the launch lines.
# ─────────────────────────────────────────────────────────────────────────
#
# Bluefin's root Justfile (/usr/share/ublue-os/just/00-entry.just) imports a
# fixed list of files, NOT a glob. Making 'just review' work system-wide from
# the image still means baking this launcher into a custom image build (out of
# scope here — see README "Scope").
#
# In this checkout, run plain 'just review' (or another recipe below) from the
# repository root. Persistent state is limited to launcher configuration; the
# VM runner receives only its per-run control/overlay directory, never a
# workspace or host home/configuration mount.
# Goose is the only agent backend. There is no local inference, no model
# profile catalogue, and no multi-CLI auto-detection: one backend means one
# readiness check and one fix-it message when it fails.
#
# TOOL is read from the environment so 'TOOL=goose just review'
# works as documented — 'just' recipe parameters are positional, not
# KEY=VALUE, so it cannot be a plain recipe parameter. Any value other than
# 'goose' is a hard error rather than a silent fallback.
tool_env := env("TOOL", "")
hive_repo_url := "https://github.com/kubestellar/hive"
# origin/v2 via `git ls-remote --heads https://github.com/kubestellar/hive v2`
# on 2026-08-04.
hive_commit := "98781c252cefb2f2193832a701abd8d0728ea18b"
copilot_default_model := "gpt-5.6-luna"
# Contributor runs are automated in practice — Hive keeps feeding the session —
# so a large window is money spent on context nobody reads. Opus and Kimi are
# the models whose default windows are worth clamping; luna keeps the provider
# default because it is already the cheap path.
opus_model := "claude-opus-5"
opus_context_limit := "264000"
# Kimi K3's default window is ~1M tokens, so the same clamp applies.
kimi_model := "kimi-k3"
kimi_context_limit := "264000"
vm_raw_image := env("REVIEW_VM_RAW", "")
vm_version := env("REVIEW_VM_VERSION", "25.08.15")
# The fsdk-derived contributor image. Used by
# review-container; the VM path gets the same image from inside the
# guest, so this only matters for local development.
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
  echo "  Re-run review from an interactive terminal, or pre-seed it yourself from kubestellar/hive @ ${commit} by running \`just contribute-setup ${tool}\` in an interactive checkout (set REVIEW_HIVE_COMMIT to another full commit if needed)" >&2
}
GOOSE_INSTALL_HINT="Install: https://github.com/block/goose/releases"
GOOSE_FIXIT_HINT="Run: goose configure, select GitHub Copilot, and complete the device flow."

goose_configured() {
  # An explicit GOOSE_PROVIDER counts as configured because the launcher
  # passes it straight through to the guest; otherwise Goose's own config
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
  # TOOL exists only for compatibility with the documented invocation; the
  # single supported value is the single supported backend.
  local requested="${1:-}"
  [[ -z "$requested" || "$requested" == "goose" ]] && return 0
  echo "ERROR: TOOL=${requested} is not supported — review runs Goose only." >&2
  echo "  Unset TOOL, or pass TOOL=goose." >&2
  return 1
}
preflight_agent() {
  # Exactly one ERROR line per failure, each with the command that fixes it.
  require_copilot_provider || return 1
  command -v goose &>/dev/null || {
    echo "ERROR: goose is not installed." >&2
    echo "  ${GOOSE_INSTALL_HINT}" >&2
    return 1
  }
  github_auth_ready || {
    echo "ERROR: GitHub CLI is not authenticated against github.com." >&2
    echo "  Run: ${GITHUB_LOGIN_COMMAND}" >&2
    return 1
  }
  goose_configured || {
    echo "ERROR: Goose has no usable provider configuration." >&2
    echo "  ${GOOSE_FIXIT_HINT}" >&2
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
  # 'Running' alone does not mean 'in use'. Distinguish the two cases,
  # because they deserve opposite treatment:
  #
  #   owned  -- somebody is working in that terminal right now. Never touch
  #             it; hand over the attach command instead.
  #   orphan -- still running, but its terminal is gone, so no one can ever
  #             reach it or Ctrl-C it again. Reclaim it silently.
  #
  # Telling a user to run 'podman rm -f' for the orphan case would smuggle
  # the stop command back in through the door it was thrown out of. The
  # launcher cleans up after itself instead.
  local name="$1" owner_pid owner_tty
  [[ "$(podman inspect --format '{{.State.Running}}' "$name" 2>/dev/null || echo false)" == "true" ]] || return 0
  owner_pid="$(container_owner_pid "$name")"
  if [[ -z "$owner_pid" ]]; then
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
  # so both always name exactly one image. A locally built image has no
  # registry behind it either, so refreshing it only produces a failed pull
  # and a misleading "may be out of date" warning; podman stores
  # 'podman build -t review:dev' as 'localhost/review:dev', so accept the bare
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
  contributor_image_available "$ref" && return 0
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
  echo "  or build it yourself: podman build -f image/Containerfile -t review:dev . && REVIEW_CONTRIBUTOR_IMAGE=review:dev just review-container" >&2
  return 1
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
  # Named so both launch paths tell the same story. A `gh auth token` is the
  # tempting substitute and the reason this message exists: it looks like a
  # GitHub credential, so a contributor reasonably assumes their gh login is
  # enough, and then the agent dies on "failed to get api info" inside a guest
  # they cannot read.
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
report_vm_github_identity_blocked() {
  # Unconditional on both callers: the current guest has no bootstrap mapping
  # for a host GH_TOKEN, so whether the host happens to have one changes
  # nothing about what the VM can do.
  echo "! VM GitHub identity is blocked: the current guest cannot receive GH_TOKEN." >&2
  echo "  A host gh login or REVIEW_GH_TOKEN cannot satisfy this VM prerequisite." >&2
  echo "  Use review-container for work that needs fork, push, or PR access." >&2
  return 0
}
resolve_goose_selection() {
  # Goose is fixed to GitHub Copilot. The model stays noninteractive and the
  # environment still overrides it for automation.
  GOOSE_PROVIDER="github_copilot"
  GOOSE_MODEL="${GOOSE_MODEL:-${COPILOT_DEFAULT_MODEL}}"
  return 0
}
# Turn a short profile name ('luna', 'opus5') plus an optional thinking effort
# into GOOSE_MODEL / GOOSE_THINKING_EFFORT / GOOSE_CONTEXT_LIMIT. Two profiles,
# no picker: an empty profile is the default one. Profiles are defaults, never
# overrides — an explicit GOOSE_* value in the environment still wins.
resolve_model_profile() {
  local profile="${1:-}" effort="${2:-}"
  case "${profile,,}" in
    ""|luna)
      PROFILE_MODEL="${COPILOT_DEFAULT_MODEL}"
      PROFILE_EFFORT="max"
      PROFILE_CONTEXT_LIMIT=""
      ;;
    opus5)
      PROFILE_MODEL="${OPUS_MODEL}"
      PROFILE_EFFORT="high"
      PROFILE_CONTEXT_LIMIT="${OPUS_CONTEXT_LIMIT}"
      ;;
    kimi)
      PROFILE_MODEL="${KIMI_MODEL}"
      PROFILE_EFFORT="max"
      PROFILE_CONTEXT_LIMIT="${KIMI_CONTEXT_LIMIT}"
      ;;
    *)
      echo "ERROR: unknown model profile '${profile}'." >&2
      echo "  Known profiles: luna (${COPILOT_DEFAULT_MODEL}), opus5 (${OPUS_MODEL}), kimi (${KIMI_MODEL})." >&2
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

  echo "Preparing kubestellar/hive @ ${HIVE_COMMIT:0:12} -> ${HIVE_SRC_DIR}..."
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
    echo "  Register one from an interactive terminal: REVIEW_HIVE=${HIVE_REGISTRATION_NAME} just ${REVIEW_RECIPE:-review}" >&2
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
  # only host state the guest genuinely needs, and Hive owns their format.
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
  # of 'contribute-setup' — compares HEAD against origin/v2 and aborts when they
  # differ, printing "Or skip: export HIVE_SKIP_VERSION_CHECK=true". That check
  # assumes a tracking checkout of v2. We deliberately run a pinned, detached
  # SHA (see prepare_pinned_hive_checkout), so the comparison can only ever
  # fail once v2 moves past the pin, and it would abort first-run onboarding on
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
  local hub
  hub="$(read_hive_value HIVE_HUB)"
  if [[ -n "$HIVE_REGISTRATION_NAME" && "$HIVE_CONTRIBUTOR_ENV" == *"contributor.${HIVE_REGISTRATION_NAME}.env" ]]; then
    echo "✓ hive: ${hub:-unknown} (registration '${HIVE_REGISTRATION_NAME}')"
  else
    echo "✓ hive: ${hub:-unknown} (default registration)"
    if [[ -n "$HIVE_REGISTRATION_NAME" ]]; then
      echo "  '${HIVE_REGISTRATION_NAME}' has no registration of its own; register one with: REVIEW_HIVE=${HIVE_REGISTRATION_NAME} just ${REVIEW_RECIPE:-review}"
    fi
  fi
}
read_hive_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$HIVE_CONTRIBUTOR_ENV"
}
vm_host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'x86_64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    *) echo "ERROR: unsupported host architecture: $(uname -m)" >&2; return 1 ;;
  esac
}
vm_firmware() {
  local arch="$1" name dir firmware
  command -v find &>/dev/null || return 1

  # Search brew's prefix first. On an immutable host (Bluefin, Aurora) the
  # distro's edk2/OVMF package usually is not layered, but Homebrew's `qemu`
  # formula ships firmware alongside the emulator it will actually run --
  # so if qemu came from brew, its matching firmware is already present.
  # Also search next to whichever qemu is on PATH, for the same reason.
  local -a roots=()
  if command -v brew &>/dev/null; then
    roots+=("$(brew --prefix 2>/dev/null)/share/qemu")
  fi
  if command -v "qemu-system-${arch}" &>/dev/null; then
    roots+=("$(dirname "$(dirname "$(command -v "qemu-system-${arch}")")")/share/qemu")
  fi
  roots+=(/usr/share /usr/lib /var/home/"${USER}"/.local/share/review/firmware)

  # Both naming schemes are in the wild: distro packages use OVMF_CODE*.fd,
  # while qemu's own bundled firmware uses edk2-<arch>-code.fd.
  local -a names=()
  case "$arch" in
    x86_64)  names=(edk2-x86_64-code.fd 'OVMF_CODE*.fd' OVMF.fd) ;;
    aarch64) names=(edk2-aarch64-code.fd AAVMF_CODE.fd QEMU_EFI.fd) ;;
    *) return 1 ;;
  esac

  for dir in "${roots[@]}"; do
    [[ -d "$dir" ]] || continue
    for name in "${names[@]}"; do
      # -L is required: Homebrew symlinks share/qemu into ../Cellar/qemu/<v>/,
      # and find does not follow symlinks without it.
      firmware="$(find -L "$dir" -name "$name" -print -quit 2>/dev/null)"
      [[ -n "$firmware" ]] && { printf '%s\n' "$firmware"; return 0; }
    done
  done
  return 1
}
vm_firmware_vars() {
  # Split edk2/OVMF firmware is a pflash PAIR: a read-only CODE image plus a
  # writable VARS image. `-bios` cannot load these; they must be attached as
  # pflash units 0 and 1. The VARS template sits beside the CODE image under
  # the same name with CODE->VARS (so OVMF_CODE_4M.fd pairs with
  # OVMF_VARS_4M.fd), except that edk2's own tree names its variable stores
  # after the 32-bit architecture. A name with no CODE in it is a single-blob
  # firmware and has no pair at all.
  local code="$1" dir base vars
  dir="$(dirname "$code")"
  base="$(basename "$code")"
  vars="${base/CODE/VARS}"
  vars="${vars/code/vars}"
  vars="${vars/edk2-x86_64/edk2-i386}"
  vars="${vars/edk2-aarch64/edk2-arm}"
  [[ "$vars" != "$base" && -f "${dir}/${vars}" ]] || return 1
  printf '%s\n' "${dir}/${vars}"
}
vm_firmware_hint() {
  # brew is the one install path that works without layering or a reboot on an
  # immutable host, and its qemu formula carries the firmware with it.
  echo "  Install QEMU (which bundles UEFI firmware) with: brew install qemu" >&2
  echo "  Already have brew qemu? Re-run 'brew reinstall qemu' to restore its share/qemu firmware." >&2
  echo "  Or layer the distro package: sudo rpm-ostree install edk2-ovmf   (needs a reboot)" >&2
}
ensure_vm_host() {
  local arch qemu
  arch="$(vm_host_arch)" || return 1
  qemu="qemu-system-${arch}"
  command -v "$qemu" &>/dev/null || {
    echo "ERROR: ${qemu} is required for local VM prototyping." >&2
    return 1
  }
  [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]] || {
    echo "ERROR: /dev/kvm is unavailable or not usable by this user." >&2
    return 1
  }
  command -v python3 &>/dev/null || {
    echo "ERROR: python3 is required to open the one-shot VM bootstrap channel." >&2
    return 1
  }
}
vm_raw_cache_path() {
  local state_dir="$1" version="$2" arch="$3"
  printf '%s/review-vm-%s-%s.raw
' "$state_dir" "$version" "$arch"
}
verify_vm_raw() {
  local raw="$1"
  [[ -f "$raw" && -f "${raw}.sha256" ]] || return 1
  (cd "$(dirname "$raw")" && sha256sum -c "$(basename "${raw}.sha256")") &>/dev/null
}
cached_vm_raw() {
  # A cache entry is useful only when it is the requested version and
  # architecture *and* its sidecar still verifies it. Never fall back to a
  # convenient-looking raw from another release.
  local state_dir="$1" version="$2" arch="$3" raw
  raw="$(vm_raw_cache_path "$state_dir" "$version" "$arch")"
  [[ -e "$raw" || -e "${raw}.sha256" ]] || return 1
  if verify_vm_raw "$raw"; then
    cleanup_obsolete_vm_cache "$state_dir" "$version" "$arch"
    printf '%s
' "$raw"
    return 0
  fi
  echo "! cached VM ${version} for ${arch} is incomplete or failed verification; refetching it." >&2
  rm -f "$raw" "${raw}.sha256" "${raw}.zst" "${raw}.zst.partial" "${raw}.partial" "${raw}.sha256.partial"
  return 1
}
cleanup_obsolete_vm_cache() {
  # There is exactly one current raw path per architecture. Anything else
  # under that architecture's cache name — an older release, a dead partial,
  # a leftover sidecar — is obsolete by definition.
  local state_dir="$1" version="$2" arch="$3" current stale
  current="$(vm_raw_cache_path "$state_dir" "$version" "$arch")"
  shopt -s nullglob
  for stale in "$state_dir"/review-vm-*-"${arch}".raw*; do
    [[ "$stale" == "${current}"* ]] || rm -f "$stale"
  done
  shopt -u nullglob
}
vm_release_url() {
  # projectbluefin/fsdk-containers still publishes the VM under its pre-rename
  # asset name. This repository was renamed donate-clanker -> review, but that
  # rename was never carried into the release artifacts: every published asset
  # on v25.08.14 and v25.08.15 is 'donate-clanker-vm-<version>-<arch>...'.
  # Verified with 'gh release view v25.08.15 --repo projectbluefin/fsdk-containers'.
  # Track the name the publisher actually uses, not the name we wish it used;
  # change this constant when fsdk-containers republishes under 'review-vm'.
  # The local cache keeps the review-vm-* name — that is ours, not theirs.
  local version="$1" arch="$2"
  printf 'https://github.com/projectbluefin/fsdk-containers/releases/download/v%s/donate-clanker-vm-%s-%s.raw.zst
'     "$version" "$version" "$arch"
}
vm_release_asset_available() {
  command -v curl &>/dev/null || return 1
  curl -fsIL --max-time 10 "$(vm_release_url "$1" "$2")" &>/dev/null
}
fetch_vm_raw() {
  local state_dir="$1" version="$2" arch raw url checksum_url
  arch="$(vm_host_arch)" || return 1
  command -v curl &>/dev/null || { echo "ERROR: curl is required to fetch the VM artifact." >&2; return 1; }
  command -v zstd &>/dev/null || { echo "ERROR: zstd is required to decompress the VM artifact." >&2; return 1; }
  raw="$(vm_raw_cache_path "$state_dir" "$version" "$arch")"
  local zst="${raw}.zst" checksum="${raw}.sha256"
  url="$(vm_release_url "$version" "$arch")"
  checksum_url="${url%.zst}.sha256"
  mkdir -p "$state_dir"
  echo "Fetching review VM ${version} for ${arch}..." >&2
  curl -fL --retry 3 --output "${zst}.partial" "$url" || {
    rm -f "${zst}.partial"
    if [[ "$arch" == "aarch64" ]]; then
      echo "ERROR: the aarch64 VM raw asset is unavailable for release ${version}: ${url}" >&2
      echo "  Use review-container until that release asset exists." >&2
    else
      echo "ERROR: VM release asset is not published yet: ${url}" >&2
    fi
    return 1
  }
  mv "${zst}.partial" "$zst"
  curl -fL --retry 3 --output "${checksum}.partial" "$checksum_url" || {
    rm -f "$zst" "${zst}.partial" "$checksum" "${checksum}.partial"
    echo "ERROR: VM release checksum sidecar is not published yet: ${checksum_url}" >&2
    return 1
  }
  mv "${checksum}.partial" "$checksum"
  python3 - "$checksum" "$(basename "$raw")" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
if lines:
    checksum, *_rest = lines[0].split(maxsplit=1)
    path.write_text(f"{checksum}  {sys.argv[2]}\n")
PY
  echo "Decompressing VM image..." >&2
  zstd -d "$zst" -o "${raw}.partial" --force || {
    rm -f "$zst" "$checksum" "${raw}.partial"
    echo "ERROR: VM decompression failed." >&2
    return 1
  }
  mv "${raw}.partial" "$raw"
  rm -f "$zst"
  (cd "$state_dir" && sha256sum -c "$(basename "$checksum")" >/dev/null) || {
    rm -f "$raw" "$checksum"
    echo "ERROR: downloaded VM checksum failed." >&2
    return 1
  }
  cleanup_obsolete_vm_cache "$state_dir" "$version" "$arch"
  printf '%s
' "$raw"
}
'''

# Start review cycles on the hive — foreground, Ctrl-C to stop.
# Goose is the only backend; TOOL=goose is accepted, anything else is an
# error. The VM boots in the foreground and the terminal belongs to the
# agent until you stop it.
# Usage: just review
#        TOOL=goose just review
#        REVIEW_HIVE=endusers just review   # use that named hive registration
review:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    COPILOT_DEFAULT_MODEL="{{copilot_default_model}}"

    STATE_DIR="${HOME}/.local/state/review"
    HIVE_SRC_DIR="${STATE_DIR}/hive-src"
    HIVE_REPO_URL="{{hive_repo_url}}"
    HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"
    HIVE_COMMIT="${HIVE_COMMIT,,}"
    mkdir -p "${STATE_DIR}"

    require_goose_backend "$TOOL"
    preflight_agent
    resolve_goose_selection

    COPILOT_TOKEN=""
    if [[ "${GOOSE_PROVIDER:-}" == "github_copilot" ]]; then
      resolve_copilot_token
      if [[ -n "${COPILOT_TOKEN:-}" ]]; then
        echo "✓ Copilot credential passed to the agent."
      else
        report_missing_copilot_credential
      fi
    fi
    report_vm_github_identity_blocked

    REVIEW_RECIPE=review
    ensure_hive_contributor_env
    report_hive_selection

    RUN_ID="$(date +%s)-$$"
    umask 077
    RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/review.XXXXXX")" || {
      echo "ERROR: could not create a private VM run directory." >&2
      exit 1
    }
    chmod 700 "$RUN_DIR" || {
      rm -rf "$RUN_DIR"
      echo "ERROR: could not secure the VM run directory." >&2
      exit 1
    }
    BOOTSTRAP_SOCKET="${RUN_DIR}/bootstrap-${RUN_ID}.sock"
    BOOTSTRAP_PID=""
    cleanup_bootstrap() {
      if [[ -n "$BOOTSTRAP_PID" ]]; then
        kill "$BOOTSTRAP_PID" 2>/dev/null || true
        wait "$BOOTSTRAP_PID" 2>/dev/null || true
      fi
      rm -f "$BOOTSTRAP_SOCKET"
      rm -rf "$RUN_DIR"
    }
    trap cleanup_bootstrap EXIT INT TERM

    HIVE_ENDPOINT="$(read_hive_value HIVE_WS_URL)"
    [[ -n "$HIVE_ENDPOINT" ]] || HIVE_ENDPOINT="$(read_hive_value HIVE_HUB)"
    HIVE_REGISTRATION_TOKEN="$(read_hive_value HIVE_REGISTRATION_TOKEN)"
    [[ -n "$HIVE_ENDPOINT" && -n "$HIVE_REGISTRATION_TOKEN" ]] || {
      echo "ERROR: Hive setup is missing HIVE_HUB/HIVE_WS_URL or HIVE_REGISTRATION_TOKEN." >&2
      exit 1
    }

    start_bootstrap_server() {
      printf '%s\0%s\0%s\0%s\0%s\0%s\0%s\0' \
        "$HIVE_ENDPOINT" "$HIVE_REGISTRATION_TOKEN" "goose" "$RUN_ID" "$GOOSE_PROVIDER" "$GOOSE_MODEL" "${COPILOT_TOKEN:-}" |
        python3 -c 'import json,os,socket,sys; path=sys.argv[1]; values=sys.stdin.buffer.read().split(b"\0"); len(values) < 7 and sys.exit("bootstrap input is incomplete"); payload={"version":2,"hive_endpoint":values[0].decode(),"registration_token":values[1].decode(),"backend":values[2].decode(),"run_id":values[3].decode(), **({"goose_provider":values[4].decode()} if values[4] else {}), **({"goose_model":values[5].decode()} if values[5] else {}), **({"provider_secret":values[6].decode()} if values[6] else {})}; os.path.exists(path) and os.unlink(path); server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); server.bind(path); os.chmod(path,0o600); server.listen(1); server.settimeout(float(os.environ.get("REVIEW_BOOTSTRAP_TIMEOUT","180"))); conn,_=server.accept(); conn.settimeout(30); conn.sendall((json.dumps(payload,separators=(",",":"))+"\n").encode()); ack_line=conn.makefile("rb").readline(65537); ack_line.endswith(b"\n") or sys.exit("bootstrap acknowledgement ended before newline"); len(ack_line) <= 65536 or sys.exit("bootstrap acknowledgement exceeds 65536 bytes"); ack=json.loads(ack_line); ack == {"version":2,"type":"control_ack"} or sys.exit("invalid bootstrap acknowledgement"); conn.close(); server.close(); os.unlink(path)' "$BOOTSTRAP_SOCKET" &
      BOOTSTRAP_PID=$!
      local ready_deadline=$((SECONDS + 5))
      while [[ ! -S "$BOOTSTRAP_SOCKET" ]]; do
        if ! kill -0 "$BOOTSTRAP_PID" 2>/dev/null; then
          wait "$BOOTSTRAP_PID" || true
          echo "ERROR: VM bootstrap server exited before binding ${BOOTSTRAP_SOCKET}." >&2
          echo "  Check the bootstrap diagnostics above, then re-run review." >&2
          exit 1
        fi
        if (( SECONDS >= ready_deadline )); then
          echo "ERROR: VM bootstrap server did not bind ${BOOTSTRAP_SOCKET} within 5 seconds." >&2
          echo "  Check that this host can create Unix sockets in ${RUN_DIR}, then re-run review." >&2
          exit 1
        fi
        sleep 0.05
      done
    }

    VM_RAW="{{vm_raw_image}}"
    if [[ -z "$VM_RAW" ]]; then
      VM_ARCH="$(vm_host_arch)"
      VM_RAW="$(cached_vm_raw "$STATE_DIR" "{{vm_version}}" "$VM_ARCH" || true)"
    fi
    if [[ -z "$VM_RAW" && "${REVIEW_TEST_SKIP_VM_FETCH:-}" != 1 ]]; then
      VM_RAW="$(fetch_vm_raw "$STATE_DIR" "{{vm_version}}")"
    fi
    if [[ -n "$VM_RAW" ]]; then
      VM_ARCH="$(vm_host_arch)"
      [[ -f "$VM_RAW" ]] || { echo "ERROR: VM raw disk not found: $VM_RAW" >&2; exit 1; }
      [[ -f "${VM_RAW}.sha256" ]] || {
        echo "ERROR: VM raw disk checksum sidecar not found: ${VM_RAW}.sha256" >&2
        exit 1
      }
      (cd "$(dirname "$VM_RAW")" && sha256sum -c "$(basename "${VM_RAW}.sha256")") || {
        echo "ERROR: VM raw disk checksum failed: $VM_RAW" >&2
        exit 1
      }
      ensure_vm_host
      FIRMWARE="$(vm_firmware "$VM_ARCH")" || {
        echo "ERROR: matching UEFI firmware for ${VM_ARCH} was not found." >&2
        vm_firmware_hint
        exit 1
      }
      VM_OVERLAY="${RUN_DIR}/overlay.qcow2"
      if command -v qemu-img &>/dev/null; then
        qemu-img create -q -f qcow2 -F raw -b "$VM_RAW" "$VM_OVERLAY" >/dev/null || {
          echo "ERROR: could not create the per-run VM overlay." >&2
          exit 1
        }
        VM_DISK_ARGS=(-drive "file=${VM_OVERLAY},format=qcow2,if=virtio")
      else
        echo "ERROR: qemu-img is required to create a disposable VM overlay." >&2
        echo "  Install QEMU with: brew install qemu" >&2
        exit 1
      fi

      start_bootstrap_server

      case "$VM_ARCH" in
        x86_64) VM_MACHINE=q35; SERIAL_DEVICE=virtio-serial-pci ;;
        aarch64) VM_MACHINE=virt; SERIAL_DEVICE=virtio-serial-device ;;
      esac
      echo "✓ booting local review VM (4 vCPU, 8 GiB RAM)."
      echo "  Foreground by design: Ctrl-C stops it."

      FIRMWARE_ARGS=()
      if VARS_TEMPLATE="$(vm_firmware_vars "$FIRMWARE" 2>/dev/null)"; then
        RUN_VARS="${RUN_DIR}/efivars.fd"
        cp -f "$VARS_TEMPLATE" "$RUN_VARS"
        chmod u+w "$RUN_VARS"
        FIRMWARE_ARGS=(
          -drive "if=pflash,format=raw,unit=0,readonly=on,file=${FIRMWARE}"
          -drive "if=pflash,format=raw,unit=1,file=${RUN_VARS}"
        )
      else
        FIRMWARE_ARGS=(-bios "$FIRMWARE")
      fi

      set +e
      "qemu-system-${VM_ARCH}"         -enable-kvm -machine "$VM_MACHINE" -cpu host -smp 4 -m 8192         "${FIRMWARE_ARGS[@]}"         "${VM_DISK_ARGS[@]}"         -nic user,model=virtio         -chardev "socket,id=control,path=${BOOTSTRAP_SOCKET}"         -device "$SERIAL_DEVICE"         -device "virtserialport,chardev=control,name=org.projectbluefin.review.bootstrap"         -nographic
      status=$?
      set -e
      exit "$status"
    fi
    echo "ERROR: no review VM disk is available for this host." >&2
    echo "  Re-run review to fetch release {{vm_version}}, or point REVIEW_VM_RAW at a" >&2
    echo "  verified raw disk with its .sha256 sidecar beside it." >&2
    exit 1

# Run ONLY the contributor container — no VM — for quick local development.
# Same preflight and same provider/model selection as the VM path, minus the
# hardware isolation, so it is the fast loop while hacking on the image.
#
#   just review-container              # luna: gpt-5.6-luna at max effort
#   just review-container luna         # the same, named explicitly
#   just review-container opus5 high   # claude-opus-5, high effort, 264k context
#   just review-container kimi         # kimi-k3, max effort, 264k context
#
# One instance owns the 'review-container' name, so a second concurrent agent
# needs a name of its own:
#
#   REVIEW_CONTAINER_NAME=review-container-2 just review-container opus5 high
#
# Usage: just review-container [luna|opus5|kimi] [low|medium|high|max]
# Env:   REVIEW_CONTAINER_NAME=<name>  run a concurrent second instance
#        (default 'review-container'; must match [a-zA-Z0-9][a-zA-Z0-9_.-]*)
#        REVIEW_HIVE=<name>  use ~/.config/hive/contributor.<name>.env; when
#        missing, register a hive under that name. Without it, the current
#        repository's directory name is tried, then the default
#        ~/.config/hive/contributor.env.
review-container profile="" effort="":
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    COPILOT_DEFAULT_MODEL="{{copilot_default_model}}"
    OPUS_MODEL="{{opus_model}}"
    OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"
    KIMI_MODEL="{{kimi_model}}"
    KIMI_CONTEXT_LIMIT="{{kimi_context_limit}}"

    STATE_DIR="${HOME}/.local/state/review"
    HIVE_SRC_DIR="${STATE_DIR}/hive-src"
    HIVE_REPO_URL="{{hive_repo_url}}"
    HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"
    HIVE_COMMIT="${HIVE_COMMIT,,}"
    mkdir -p "${STATE_DIR}"

    require_goose_backend "$TOOL"
    preflight_agent
    command -v podman &>/dev/null || {
      echo "ERROR: Podman is required to run the contributor container." >&2
      echo "  Install Podman, then re-run review-container." >&2
      exit 1
    }

    # Resolved before anything interactive so a typo fails immediately rather
    # than after the model picker and the Hive setup.
    CONTAINER_NAME="${REVIEW_CONTAINER_NAME:-review-container}"
    require_valid_container_name "$CONTAINER_NAME"

    resolve_model_profile "{{profile}}" "{{effort}}"
    resolve_goose_selection
    REVIEW_RECIPE=review-container
    ensure_hive_contributor_env
    report_hive_selection

    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    require_no_running_instance "$CONTAINER_NAME"
    ensure_contributor_image "$CONTRIBUTOR_IMAGE"

    CONTAINER_ARGS=(
      podman run --rm --interactive --tty --replace --name "$CONTAINER_NAME"
      --label "$(owner_run_label)"
      # Rootless podman maps the host user to container root by default, so a
      # 0600 host file bind-mounts in as root-owned and the 'dev' user the
      # image runs as cannot read it -- contributor.env holds Hive's own
      # settings and is exactly that. Mapping the host user onto dev's uid
      # instead makes the mount readable without loosening the host mode.
      --userns "keep-id:uid=1000,gid=1000"
      --volume "${HOME}/.config/hive:/home/dev/.config/hive:ro"
      # The selected registration lands on the path the relay reads. When a
      # named registration (contributor.<name>.env) is in play this overlays
      # it on top of the directory mount; with the default it is the same
      # file mounted over itself.
      --volume "${HIVE_CONTRIBUTOR_ENV}:/home/dev/.config/hive/contributor.env:ro"
      --env "AGENT_BACKEND=goose"
      # Podman does not pass COLORTERM through on its own; the entrypoint
      # needs it to pick the direct-color attach fallback for a host TERM
      # the image's narrow terminfo set does not know (e.g. xterm-ghostty).
      --env COLORTERM
    )
    [[ -n "$GOOSE_PROVIDER" ]] && CONTAINER_ARGS+=(--env "GOOSE_PROVIDER=${GOOSE_PROVIDER}")
    [[ -n "$GOOSE_MODEL" ]] && CONTAINER_ARGS+=(--env "GOOSE_MODEL=${GOOSE_MODEL}")
    [[ -n "${GOOSE_THINKING_EFFORT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_THINKING_EFFORT=${GOOSE_THINKING_EFFORT}")
    [[ -n "${GOOSE_CONTEXT_LIMIT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_CONTEXT_LIMIT=${GOOSE_CONTEXT_LIMIT}")
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
    resolve_gh_token
    if [[ -n "${GH_TOKEN_VALUE:-}" ]]; then
      export GH_TOKEN="$GH_TOKEN_VALUE"
      CONTAINER_ARGS+=(--env GH_TOKEN)
      report_gh_token_blast_radius "${GH_TOKEN_SOURCE}"
    else
      report_missing_gh_token
    fi
    CONTAINER_ARGS+=("$CONTRIBUTOR_IMAGE")

    echo "✓ starting the review contributor container (no VM)."
    echo "  The entrypoint attaches to the 'contributor' tmux session for you."
    echo "  From a second terminal: podman exec -it ${CONTAINER_NAME} tmux attach -t contributor"
    echo "  Stop any time with Ctrl-C — that is the only way it ends."
    exec "${CONTAINER_ARGS[@]}"

# Walk the Bluefin PR queue in the contributor container — no Hive, no VM.
# The container runs `bluefin-review queue` instead of the contributor agent,
# so no Hive registration is mounted or required. Foreground: q or Ctrl-C
# stops. Arguments pass straight through to `bluefin-review queue`:
#
#   just review-queue                      # everything the queue marks 'review'
#   just review-queue kimi high            # pick the model profile and effort
#   just review-queue --repo bluefin       # one repository
#   just review-queue opus5 --all          # profile, then bluefin-review flags
#
# One instance owns the 'review-queue' name; REVIEW_QUEUE_NAME overrides it
# for a concurrent second walk, exactly as REVIEW_CONTAINER_NAME does for
# review-container.
review-queue *queue_args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    TOOL="{{tool_env}}"
    COPILOT_DEFAULT_MODEL="{{copilot_default_model}}"
    OPUS_MODEL="{{opus_model}}"
    OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"
    KIMI_MODEL="{{kimi_model}}"
    KIMI_CONTEXT_LIMIT="{{kimi_context_limit}}"

    require_goose_backend "$TOOL"
    preflight_agent
    command -v podman &>/dev/null || {
      echo "ERROR: Podman is required to run the contributor container." >&2
      echo "  Install Podman, then re-run review-queue." >&2
      exit 1
    }

    CONTAINER_NAME="${REVIEW_QUEUE_NAME:-review-queue}"
    require_valid_container_name "$CONTAINER_NAME"

    # Leading non-flag arguments are the model profile and thinking effort,
    # exactly as review-container takes them; everything from the first '-'
    # flag onward belongs to bluefin-review queue. Word-splitting {{queue_args}}
    # is the point: it arrives as one string of separate flags.
    # shellcheck disable=SC2086
    set -- {{queue_args}}
    profile="" effort=""
    if [[ $# -gt 0 && "$1" != -* ]]; then profile="$1"; shift; fi
    if [[ $# -gt 0 && "$1" != -* ]]; then effort="$1"; shift; fi
    resolve_model_profile "$profile" "$effort"
    resolve_goose_selection

    CONTRIBUTOR_IMAGE="{{contributor_image}}"
    require_no_running_instance "$CONTAINER_NAME"
    ensure_contributor_image "$CONTRIBUTOR_IMAGE"

    CONTAINER_ARGS=(
      podman run --rm --interactive --tty --replace --name "$CONTAINER_NAME"
      --label "$(owner_run_label)"
      --userns "keep-id:uid=1000,gid=1000"
      # Podman does not pass COLORTERM through on its own.
      --env COLORTERM
    )
    [[ -n "$GOOSE_PROVIDER" ]] && CONTAINER_ARGS+=(--env "GOOSE_PROVIDER=${GOOSE_PROVIDER}")
    [[ -n "$GOOSE_MODEL" ]] && CONTAINER_ARGS+=(--env "GOOSE_MODEL=${GOOSE_MODEL}")
    [[ -n "${GOOSE_THINKING_EFFORT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_THINKING_EFFORT=${GOOSE_THINKING_EFFORT}")
    [[ -n "${GOOSE_CONTEXT_LIMIT:-}" ]] && CONTAINER_ARGS+=(--env "GOOSE_CONTEXT_LIMIT=${GOOSE_CONTEXT_LIMIT}")
    # The Copilot credential is what powers 'r' (the Goose review of a pull
    # request); the walk itself only reads GitHub, so a missing credential is
    # a warning, not a stop.
    resolve_copilot_token
    if [[ -n "${COPILOT_TOKEN:-}" ]]; then
      export GITHUB_COPILOT_TOKEN="$COPILOT_TOKEN"
      CONTAINER_ARGS+=(--env GITHUB_COPILOT_TOKEN)
      echo "✓ Copilot credential passed to the agent."
    else
      report_missing_copilot_credential
    fi
    # The walk is a GitHub reader from the first keystroke to the last, so an
    # identity is load-bearing here, not advisory.
    resolve_gh_token
    if [[ -z "${GH_TOKEN_VALUE:-}" ]]; then
      report_missing_gh_token
      echo "ERROR: the queue walk reads live pull-request state from GitHub and cannot run without a token." >&2
      exit 1
    fi
    export GH_TOKEN="$GH_TOKEN_VALUE"
    CONTAINER_ARGS+=(--env GH_TOKEN)
    report_gh_token_blast_radius "${GH_TOKEN_SOURCE}"

    # Whatever survived the profile/effort shift belongs to bluefin-review.
    CONTAINER_ARGS+=("$CONTRIBUTOR_IMAGE" queue "$@")

    echo "✓ starting the PR queue walk (no Hive)."
    echo "  q or Ctrl-C stops; the walk is the only thing running."
    exec "${CONTAINER_ARGS[@]}"

# Preflight check: is this machine actually ready for 'just review'?
# Never starts the VM or the container — read-only diagnostics only.
review-doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    {{shared_functions}}
    COPILOT_DEFAULT_MODEL="{{copilot_default_model}}"
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
    if [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
      echo "  ✓ /dev/kvm is available"
      pass=$((pass+1))
    else
      echo "  ✗ /dev/kvm is unavailable or not usable by this user"
      echo "    Enable KVM access before launching the pinned QEMU VM runner."
      echo "    (review-container does not need it.)"
      fail=$((fail+1))
    fi
    echo ""

    echo "=== VM startup ==="
    if VM_ARCH="$(vm_host_arch)"; then
      echo "  ✓ supported VM architecture: ${VM_ARCH}"
      pass=$((pass+1))
      check "python3 installed (VM bootstrap socket)" command -v python3
    else
      echo "  ✗ this host architecture cannot run the local VM"
      fail=$((fail+1))
      VM_ARCH=""
    fi
    if [[ -n "{{vm_raw_image}}" ]]; then
      echo "  ✓ configured VM raw disk: {{vm_raw_image}}"
      pass=$((pass+1))
      check "qemu-system-${VM_ARCH} installed" command -v "qemu-system-${VM_ARCH}"
      check "qemu-img installed (disposable VM overlays)" command -v qemu-img
      if FIRMWARE_PATH="$(vm_firmware "$VM_ARCH" 2>/dev/null)"; then
        echo "  ✓ UEFI firmware found: ${FIRMWARE_PATH}"
        pass=$((pass+1))
      else
        echo "  ✗ no UEFI firmware for ${VM_ARCH}"
        vm_firmware_hint
        fail=$((fail+1))
      fi
      if verify_vm_raw "{{vm_raw_image}}"; then
        echo "  ✓ configured VM raw disk checksum verifies"
        pass=$((pass+1))
      else
        echo "  ✗ configured VM raw disk is missing or its checksum sidecar does not verify"
        echo "    Supply {{vm_raw_image}} and {{vm_raw_image}}.sha256, or unset REVIEW_VM_RAW."
        fail=$((fail+1))
      fi
    elif [[ -n "$VM_ARCH" ]]; then
      check "qemu-system-${VM_ARCH} installed" command -v "qemu-system-${VM_ARCH}"
      check "qemu-img installed (disposable VM overlays)" command -v qemu-img
      check "curl installed (VM artifact download)" command -v curl
      check "zstd installed (VM artifact decompression)" command -v zstd
      if FIRMWARE_PATH="$(vm_firmware "$VM_ARCH" 2>/dev/null)"; then
        echo "  ✓ UEFI firmware found: ${FIRMWARE_PATH}"
        pass=$((pass+1))
      else
        echo "  ✗ no UEFI firmware for ${VM_ARCH}"
        vm_firmware_hint
        fail=$((fail+1))
      fi
      VM_RELEASE_URL="$(vm_release_url "{{vm_version}}" "$VM_ARCH")"
      if vm_release_asset_available "{{vm_version}}" "$VM_ARCH"; then
        echo "  ✓ VM release artifact is published: ${VM_RELEASE_URL}"
        pass=$((pass+1))
      elif [[ "$VM_ARCH" == "aarch64" ]]; then
        echo "  ✗ aarch64 VM release artifact is unavailable: ${VM_RELEASE_URL}"
        echo "    Use review-container until the aarch64 raw asset is released."
        fail=$((fail+1))
      else
        echo "  ✗ VM release artifact is unavailable: ${VM_RELEASE_URL}"
        echo "    Check REVIEW_VM_VERSION, or point REVIEW_VM_RAW at a verified raw disk."
        fail=$((fail+1))
      fi
    fi
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
    report_vm_github_identity_blocked
    unset GH_TOKEN_VALUE
    echo ""

    echo "=== Agent backend (Goose only) ==="
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
      if [[ -n "$DOCTOR_BACKEND" && "$DOCTOR_BACKEND" != "goose" ]]; then
        echo "  ! ${HIVE_CONTRIBUTOR_ENV} says AGENT_BACKEND=${DOCTOR_BACKEND}, but review always launches goose."
        echo "    Harmless — the launcher passes AGENT_BACKEND=goose itself — but stale."
        echo "    Edit that line yourself if you want the file to match; review will not touch it."
      fi
    else
      echo "  ✗ ${HIVE_CONTRIBUTOR_ENV} is missing"
      echo "    review runs upstream 'just contribute-setup goose' from"
      echo "    kubestellar/hive @ ${HIVE_COMMIT:0:12} on first attended launch."
      echo "    That runs with upstream's documented HIVE_SKIP_VERSION_CHECK=true,"
      echo "    because the pinned checkout is detached and cannot match origin/v2."
      fail=$((fail+1))
    fi
    echo ""

    echo "=== Guest repository model ==="
    echo "  ✓ assigned repositories are cloned inside the disposable guest"
    echo ""
    echo "${pass} checks passed, ${fail} failed."
    [[ "$fail" -eq 0 ]] || exit 1
