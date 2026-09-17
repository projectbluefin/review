# justfile — the review appliance launcher entrypoint.
#
# The system image install path is still out of scope here; this root justfile
# is the launcher a checkout exposes directly.
#
# This is the ONLY file that ships/installs. Everything review needs
# (host preflight, backend selection, container lifecycle) is
# embedded below as private ('_'-prefixed variables and shared shell
# functions) on purpose: a user browsing the image or this repo should find
# one just-recipe file and the commands it exposes, not a scattered bin/ of
# standalone scripts they might stumble into and run directly out of context.
#
# Public commands:
#   review-container  Run the contributor container: the Hive queue
#                     OMP worker that receives Hive-assigned tasks.
#                     Model and effort are chosen inside OMP. Contributor
#                     containers run in the foreground; Ctrl-C stops them.
#   review-stop       Stop cluster contributor workers. Local appliances stop
#                     with Ctrl-C in their owning terminal.
#   review-doctor     Preflight diagnostics. Starts no agent and mounts no
#                     credential.
#   review-queue      Convenience alias for the OMP review appliance. It opens
#                     the same single-screen workbench as review-appliance and
#                     forwards repository, issue, and pull-request arguments.
#
# ─────────────────────────────────────────────────────────────────────────
# LIFECYCLE
#
# Contributor and maintainer runs stay in the foreground. The preferred krun
# path gives each invocation a unique container name; target-specific state
# also keeps Apptainer fallback sessions independent. Ctrl-C stops only the
# calling terminal's appliance.
#
# Every interactive launch path ends in an 'exec' or a final foreground
# command whose exit status propagates verbatim; tests/just-onboarding.sh
# pins all of it.
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
# Hive remains the sole assignment authority. OMP owns provider, model, and
# effort selection from the user's active configuration.
hive_repo_url := "https://github.com/hivecommons/hive"
# origin/v4 via `git ls-remote --heads https://github.com/hivecommons/hive v4`
# on 2026-09-11, after kubestellar/hive#6637 (fix: key OMP readiness/busy/idle
# off real captured chrome instead of a hand-written fixture that never
# exercised OMP's actual welcome/idle/busy chrome at real dimensions),
# kubestellar/hive#6639 (fix: stop OMP's rotating "Log in to several
# accounts..." startup tip from faking a needs-login verdict), and
# kubestellar/hive#6670 (fix: scope OMP's login/onboarding checks to the
# pane's last 3 lines instead of a 15-line tail a tip or a finished turn's
# own prose could still land in).
hive_commit := "67530919a135cbc466d1e0961770028842c80876"
contribute_image := env("CONTRIBUTE_IMAGE", "ghcr.io/projectbluefin/contribute:stable")

# Shared bash, 'eval''d at the top of every recipe script that needs it:
# host preflight, backend selection, and the pinned Hive checkout. Keeping
# this in one place instead of duplicating it per-recipe is the only
# concession to DRY here — it never leaves the Justfile as a file of its own.
shared_functions := '''
GITHUB_LOGIN_COMMAND="gh auth login --web --hostname github.com --scopes repo,read:org,workflow"

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
kvm_device_ready() {
  local device="${REVIEW_TEST_KVM_DEVICE:-/dev/kvm}"
  [[ -r "$device" && -w "$device" ]]
}
kvm_runtime_path() {
  # The 'krun' OCI runtime name Podman launches need not match an executable
  # on PATH: a host can register 'krun' -> '/usr/bin/crun-krun' (crun-krun), so
  # 'command -v krun' alone misses a working libkrun microVM. Podman is the
  # authoritative source for what '--runtime=krun' runs, so resolve the
  # configured path from 'podman info' and validate it exists.
  podman info --format '{{.Host.OCIRuntimes.krun.path}}' 2>/dev/null
}
kvm_runtime_ready() {
  local device="${REVIEW_TEST_KVM_DEVICE:-/dev/kvm}"
  command -v podman &>/dev/null || { KVM_FAILURE="Podman is unavailable"; return 1; }
  podman info &>/dev/null || { KVM_FAILURE="Podman is not reachable"; return 1; }
  local selected uri
  selected="$(podman_selected_connection)" || { KVM_FAILURE="Podman connections could not be resolved"; return 1; }
  IFS=$'\t' read -r uri _ <<<"$selected"
  if [[ -z "$uri" || "$uri" == unix://* ]]; then
    local runtime_path=""
    runtime_path="$(kvm_runtime_path)"
    [[ -n "$runtime_path" ]] || { KVM_FAILURE="the krun OCI runtime is not registered with Podman"; return 1; }
    [[ -x "$runtime_path" ]] || { KVM_FAILURE="the krun OCI runtime '${runtime_path}' is not executable"; return 1; }
    kvm_device_ready || { KVM_FAILURE="${device} is not readable and writable"; return 1; }
  fi
  return 0
}
apptainer_fallback_ready() {
  local fuse_device="${REVIEW_TEST_FUSE_DEVICE:-/dev/fuse}"
  command -v apptainer &>/dev/null || { APPTAINER_FAILURE="Apptainer fallback is unavailable; install Apptainer"; return 1; }
  { command -v squashfuse_ll &>/dev/null || command -v squashfuse &>/dev/null; } || {
    APPTAINER_FAILURE="squashfuse userland is unavailable; install squashfuse"
    return 1
  }
  test -e "$fuse_device" || { APPTAINER_FAILURE="FUSE device ${fuse_device} is missing"; return 1; }
  test -c "$fuse_device" || { APPTAINER_FAILURE="FUSE device ${fuse_device} is not a character device"; return 1; }
  if ! test -r "$fuse_device" || ! test -w "$fuse_device"; then
    APPTAINER_FAILURE="FUSE device ${fuse_device} is not readable and writable"
    return 1
  fi
  return 0
}
require_apptainer_fallback() {
  apptainer_fallback_ready || { echo "ERROR: ${KVM_FAILURE}; ${APPTAINER_FAILURE}." >&2; return 1; }
  echo "WARNING: ${KVM_FAILURE}; using the isolated Apptainer fallback without a KVM boundary." >&2
}
prepare_apptainer_environment() {
  local name host_file
  for name in GH_TOKEN GITHUB_TOKEN COPILOT_GITHUB_TOKEN GITHUB_COPILOT_TOKEN ANTHROPIC_API_KEY ANTHROPIC_OAUTH_TOKEN OPENAI_API_KEY GEMINI_API_KEY CONTEXT7_API_KEY AWS_BEARER_TOKEN_BEDROCK AWS_REGION AWS_DEFAULT_REGION HIVE_HUB BLUEFIN_REVIEW_ORG TERM COLORTERM; do
    [[ -v "$name" ]] && export "APPTAINERENV_${name}=${!name}"
  done
  APPTAINER_HOST_ARGS=()
  for host_file in /etc/localtime /etc/hosts; do
    test -e "$host_file" || APPTAINER_HOST_ARGS+=(--no-mount "$host_file")
  done
  return 0
}
instance_key() {
  local value="$1" slug digest
  slug="$(printf '%s' "$value" | tr '[:upper:]/:' '[:lower:]--' | tr -cd 'a-z0-9_.-')"
  slug="${slug:0:28}"
  [[ -n "$slug" ]] || slug=default
  digest="$(printf '%s' "$value" | sha256sum | cut -c1-8)"
  printf '%s-%s\n' "$slug" "$digest"
}
preflight_github() {
  github_auth_ready || {
    echo "ERROR: GitHub CLI is not authenticated against github.com." >&2
    echo "  Run: ${GITHUB_LOGIN_COMMAND}" >&2
    return 1
  }
}
image_available() {
  local ref="$1"
  if command -v podman &>/dev/null && podman info &>/dev/null; then
    podman image exists "$ref" &>/dev/null && return 0
    case "$ref" in localhost/*) return 1 ;; esac
    podman manifest inspect "$ref" &>/dev/null
    return
  fi
  case "$ref" in localhost/*) return 1 ;; esac
  if command -v skopeo &>/dev/null; then
    [[ "$ref" == *://* ]] || ref="docker://${ref}"
    skopeo inspect "$ref" &>/dev/null
    return
  fi
  command -v apptainer &>/dev/null && return 2
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
ensure_image() {
  # Moving tags are refreshed on every launch. If the registry is unavailable,
  # an existing local copy remains usable but the launcher says it may be stale.
  local ref="$1" product="$2" containerfile="$3" override="$4"
  if image_ref_is_moving "$ref"; then
    podman pull "$ref" && return 0
    if podman image exists "$ref"; then
      echo "! could not refresh ${ref}; using the local copy, which may be out of date." >&2
      return 0
    fi
  fi
  podman image exists "$ref" && return 0
  case "$ref" in
    localhost/*)
      echo "ERROR: ${ref} is a locally built image and it is not in local storage." >&2
      echo "  Build it: podman build -f ${containerfile} -t ${ref#localhost/} ." >&2
      echo "  Or drop the override to use the published default: unset ${override}" >&2
      return 1
      ;;
  esac
  podman pull "$ref" && return 0
  echo "ERROR: cannot obtain ${product} image ${ref}." >&2
  echo "  Set ${override} to a published tag or digest, or build ${containerfile}." >&2
  return 1
}
MIN_REVIEW_APPLIANCE_VERSION="26.08.06"
MIN_CONTRIBUTOR_VERSION="26.08.02"
EXPECTED_IMAGE_SERIES="26.08"

launcher_revision() {
  local rev=""
  if command -v git >/dev/null 2>&1 && [[ -d ".git" ]]; then
    rev="$(git rev-parse --short HEAD 2>/dev/null || true)"
  fi
  if [[ -z "$rev" && -f "image/appliance/REVISION" ]]; then
    local tool_rev
    tool_rev="$(tr -d '[:space:]' <"image/appliance/REVISION" 2>/dev/null || true)"
    if [[ "$tool_rev" =~ ^[0-9]+$ ]]; then
      rev=$(printf '%s.%02d' "$EXPECTED_IMAGE_SERIES" "$((10#$tool_rev))")
    fi
  fi
  printf '%s\n' "${rev:-${BLUEFIN_LAUNCHER_VERSION:-26.08.08}}"
}

report_launcher_identity() {
  local rev
  rev="$(launcher_revision)"
  echo "✓ bluefin launcher revision: ${rev}" >&2
}

check_image_compatibility() {
  local ref="$1" product="$2" version="$3" min_version="$4" is_override="${5:-0}"
  [[ -n "$version" && "$version" != "unknown" ]] || {
    if [[ "$is_override" -eq 1 ]]; then
      echo "! ${product} image ${ref} has unknown version; proceeding with explicit override." >&2
    fi
    return 0
  }
  if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: ${product} image ${ref} has malformed version label '${version}'; expected numeric MAJOR.MINOR.PATCH." >&2
    return 1
  fi
  local ver_series="${version%.*}"
  if [[ "$ver_series" != "$EXPECTED_IMAGE_SERIES" ]]; then
    if [[ "$is_override" -eq 1 ]]; then
      echo "WARNING: ${product} image ${ref} series (${ver_series}) differs from expected (${EXPECTED_IMAGE_SERIES}); proceeding with explicit override." >&2
      return 0
    fi
    echo "ERROR: ${product} image ${ref} (version ${version}) is incompatible with this launcher (expected series ${EXPECTED_IMAGE_SERIES})." >&2
    return 1
  fi
  local ver_num="${version##*.}" min_num="${min_version##*.}"
  if [[ "$ver_num" =~ ^[0-9]+$ && "$min_num" =~ ^[0-9]+$ ]]; then
    if (( 10#$ver_num < 10#$min_num )); then
      if [[ "$is_override" -eq 1 ]]; then
        echo "WARNING: ${product} image ${ref} (version ${version}) is older than recommended minimum (${min_version}); proceeding with explicit override." >&2
        return 0
      fi
      echo "ERROR: ${product} image ${ref} (version ${version}) is incompatible with this launcher (requires >= ${min_version})." >&2
      return 1
    fi
  fi
  return 0
}
migrate_legacy_state() {
  local legacy_dir="$1" target_home="$2" sif_name="$3"
  [[ -d "$legacy_dir" ]] || return 0
  [[ -d "$target_home" ]] || return 0
  local item base migrated=0
  for item in "$legacy_dir"/* "$legacy_dir"/.*; do
    [[ -e "$item" ]] || continue
    base="${item##*/}"
    [[ "$base" == "." || "$base" == ".." || "$base" == "$sif_name" ]] && continue
    if [[ ! -e "$target_home/$base" ]]; then
      if ! cp -a "$item" "$target_home/" 2>/dev/null; then
        echo "ERROR: failed to migrate legacy state item ${item} to ${target_home}; check permissions and available space." >&2
        return 1
      fi
      migrated=1
    fi
  done
  if [[ "$migrated" -eq 1 ]]; then
    echo "✓ migrated user configuration from ${legacy_dir} to ${target_home}" >&2
  fi
}

report_podman_image_identity() {
  local ref="$1" product="$2" is_override="${3:-0}" min_version="${4:-$MIN_REVIEW_APPLIANCE_VERSION}"
  local identity version revision digest
  identity="$(podman image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}|{{ index .Config.Labels "org.opencontainers.image.revision" }}|{{ .Digest }}' "$ref" 2>/dev/null)" || {
    echo "! ${product} image identity unavailable for ${ref}." >&2
    return 0
  }
  IFS='|' read -r version revision digest <<<"$identity"
  [[ -n "$version" && "$version" != "<no value>" ]] || version=unknown
  [[ -n "$revision" && "$revision" != "<no value>" ]] || revision=unknown
  [[ -n "$digest" && "$digest" != "<no value>" ]] || digest=unknown
  echo "✓ ${product} image ${ref}: version=${version} revision=${revision} digest=${digest}" >&2
  check_image_compatibility "$ref" "$product" "$version" "$min_version" "$is_override"
}

inspect_apptainer_image() {
  local ref="$1"
  local identity=""
  if [[ -f "$ref" ]] && command -v apptainer >/dev/null 2>&1; then
    local json
    json="$(apptainer inspect --json "$ref" 2>/dev/null || true)"
    if [[ -n "$json" ]] && command -v python3 >/dev/null 2>&1; then
      identity="$(python3 -c '
import json, sys
def parse():
    try:
        d = json.loads(sys.argv[1])
        labels = d.get("data", {}).get("attributes", {}).get("labels", {})
        v = labels.get("org.opencontainers.image.version", "")
        r = labels.get("org.opencontainers.image.revision", "")
        return f"{v}|{r}|unknown"
    except Exception:
        return ""
res = parse()
if res:
    print(res)
' "$json" 2>/dev/null || true)"
    fi
  elif command -v skopeo >/dev/null 2>&1; then
    local skopeo_ref="$ref"
    [[ "$skopeo_ref" == *://* ]] || skopeo_ref="docker://${skopeo_ref}"
    identity="$(skopeo inspect --format '{{ index .Labels "org.opencontainers.image.version" }}|{{ index .Labels "org.opencontainers.image.revision" }}|{{ .Digest }}' "$skopeo_ref" 2>/dev/null || true)"
  fi
  printf '%s\n' "$identity"
}

report_apptainer_image_identity() {
  local ref="$1" product="$2" is_override="${3:-0}" min_version="${4:-$MIN_REVIEW_APPLIANCE_VERSION}"
  local identity version revision digest
  identity="$(inspect_apptainer_image "$ref")"
  if [[ -z "$identity" ]]; then
    echo "! ${product} image identity unavailable for ${ref}." >&2
    return 0
  fi
  IFS='|' read -r version revision digest <<<"$identity"
  [[ -n "$version" && "$version" != "<no value>" ]] || version=unknown
  [[ -n "$revision" && "$revision" != "<no value>" ]] || revision=unknown
  [[ -n "$digest" && "$digest" != "<no value>" ]] || digest=unknown
  echo "✓ ${product} image ${ref}: version=${version} revision=${revision} digest=${digest}" >&2
  check_image_compatibility "$ref" "$product" "$version" "$min_version" "$is_override"
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
  # beside it.
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
  if [[ -n "$scopes" ]]; then
    echo "  The agent can do anything this token can: ${scopes}"
    if [[ ",${scopes//[[:space:]]/}," != *",workflow,"* && ",${scopes//[[:space:]]/}," != *"'workflow'"* ]]; then
      echo "  ! Note: Token lacks 'workflow' scope; pushing tasks that modify .github/workflows/* will fail."
    fi
  fi
  echo "  Narrow that with: REVIEW_GH_TOKEN=<scoped PAT> (public_repo or repo is enough to fork and open a PR)."
  return 0
}
report_missing_gh_token() {
  echo "! no GitHub token found; the agent has no GitHub identity." >&2
  echo "  It cannot fork, clone, push or open a pull request, and will stop on" >&2
  echo "  'To get started with GitHub CLI, please run: gh auth login' — which it" >&2
  echo "  is not allowed to run. Every assigned task will die on arrival." >&2
  echo "  Fix it with: gh auth login --web --hostname github.com --scopes repo,read:org,workflow" >&2
  echo "  Or export REVIEW_GH_TOKEN with a scoped PAT." >&2
  return 0
}
podman_selected_connection() {
  # Podman resolves its target engine in this order: CONTAINER_HOST wins
  # outright, CONTAINER_CONNECTION names a saved connection, and otherwise
  # whichever connection is marked default applies. Mirror that order so the
  # queue guard and remote credential staging see the engine 'podman run' uses.
  if [[ -n "${CONTAINER_HOST:-}" ]]; then
    printf '%s\t\n' "$CONTAINER_HOST"
    return 0
  fi
  local list
  if ! list="$(podman system connection list --format '{{.Name}}\t{{.URI}}\t{{.Identity}}\t{{.Default}}' 2>/dev/null)"; then
    echo "ERROR: could not resolve Podman connections." >&2
    return 1
  fi
  if [[ -n "${CONTAINER_CONNECTION:-}" ]]; then
    local selected
    selected="$(awk -F'\t' -v n="$CONTAINER_CONNECTION" '$1==n{printf "%s\t%s\n", $2, $3; exit}' <<<"$list")"
    if [[ -z "$selected" ]]; then
      echo "ERROR: could not resolve selected Podman connection '${CONTAINER_CONNECTION}'." >&2
      return 1
    fi
    printf '%s\n' "$selected"
    return 0
  fi
  awk -F'\t' '$4=="true"{printf "%s\t%s\n", $2, $3; exit}' <<<"$list"
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
    print_missing_hive_setup_guidance "$target" "non-interactive mode cannot answer the upstream prompts" "${HIVE_SETUP_BACKEND:-omp}" "$HIVE_COMMIT"
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
  HIVE_SKIP_VERSION_CHECK=true just --working-directory "$HIVE_SRC_DIR" --justfile "$HIVE_SRC_DIR/Justfile" config_dir="$tmp" contribute-setup "${HIVE_SETUP_BACKEND:-omp}" || {
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
  # Upstream 'contribute-setup' writes these files. They are the only host
  # state the container genuinely needs, and Hive owns their format.
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
    print_missing_hive_setup_guidance "$HIVE_CONTRIBUTOR_ENV" "non-interactive mode cannot answer the upstream prompts" "${HIVE_SETUP_BACKEND:-omp}" "$HIVE_COMMIT"
    return 1
  fi
  if ! can_run_attended_hive_setup; then
    print_missing_hive_setup_guidance "$HIVE_CONTRIBUTOR_ENV" "stdin/stdout/stderr are not attached to a terminal" "${HIVE_SETUP_BACKEND:-omp}" "$HIVE_COMMIT"
    return 1
  fi
  echo "Upstream contribute-setup hasn't run yet (no ${HIVE_CONTRIBUTOR_ENV})."
  for cmd in just gh git; do
    command -v "$cmd" &>/dev/null || { echo "ERROR: '${cmd}' is required to run contribute-setup." >&2; return 1; }
  done
  prepare_pinned_hive_checkout || return 1
  echo "Running upstream pinned setup: just contribute-setup ${HIVE_SETUP_BACKEND:-omp}"
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
  HIVE_SKIP_VERSION_CHECK=true just --working-directory "$HIVE_SRC_DIR" --justfile "$HIVE_SRC_DIR/Justfile" contribute-setup "${HIVE_SETUP_BACKEND:-omp}"
  [[ -f "$HIVE_CONTRIBUTOR_ENV" ]] || { echo "ERROR: contribute-setup ran but ${HIVE_CONTRIBUTOR_ENV} still missing." >&2; return 1; }
  echo "✓ Upstream contribute-setup complete."
}
stage_hive_registration_for_remote_podman() {
  # Podman remote resolves bind mounts on its engine host, not the client.
  # Mirror only the selected 0600 Hive registration when Podman targets an
  # SSH engine, staging to an isolated private 0700
  # directory and removing only that path on exit.
  local selected_connection uri identity authority target port remote_dir remote_env
  selected_connection="$(podman_selected_connection)" || return 1
  [[ -n "$selected_connection" ]] || return 0
  IFS=$'\t' read -r uri identity <<<"$selected_connection"
  [[ "$uri" == ssh://* ]] || return 0
  authority="${uri#ssh://}"
  authority="${authority%%/*}"
  [[ "$authority" =~ ^(([^@/]+)@)?(\[[^]]+\]|[^:]+)(:([0-9]+))?$ ]] || {
    echo "ERROR: configured Podman SSH connection has an invalid host." >&2
    return 1
  }
  target="${BASH_REMATCH[1]}${BASH_REMATCH[3]}"
  port="${BASH_REMATCH[5]:-22}"
  local -a ssh_args scp_args
  ssh_args=(-o BatchMode=yes)
  scp_args=(-o BatchMode=yes)
  if [[ -n "$identity" ]]; then
    ssh_args+=(-i "$identity")
    scp_args+=(-i "$identity")
  fi
  if [[ "$port" != 22 ]]; then
    ssh_args+=(-p "$port")
    scp_args+=(-P "$port")
  fi
  remote_dir="$(ssh "${ssh_args[@]}" "$target" 'umask 077; mktemp -d /tmp/review-hive-registration.XXXXXX')" || {
    echo "ERROR: cannot prepare the remote Podman Hive registration directory." >&2
    return 1
  }
  [[ "$remote_dir" =~ ^/tmp/review-hive-registration\.[[:alnum:]]{6}$ ]] || {
    echo "ERROR: remote Podman Hive registration directory is invalid." >&2
    return 1
  }
  REMOTE_HIVE_TARGET="$target"
  REMOTE_HIVE_SSH_ARGS=("${ssh_args[@]}")
  REMOTE_HIVE_DIR="$remote_dir"
  remote_env="${remote_dir}/${HIVE_CONTRIBUTOR_ENV##*/}"
  REMOTE_HIVE_ENV="$remote_env"
  scp "${scp_args[@]}" -p "$HIVE_CONTRIBUTOR_ENV" "${target}:${remote_env}" || {
    echo "ERROR: cannot stage the Hive registration on the remote Podman engine." >&2
    return 1
  }
  ssh "${ssh_args[@]}" "$target" "chmod 0600 $remote_env" || {
    echo "ERROR: cannot secure the staged Hive registration on the remote Podman engine." >&2
    return 1
  }
  HIVE_CONTRIBUTOR_ENV="$remote_env"
  echo "✓ Hive contributor registration staged on remote Podman engine (0600, removed on exit; endpoint and secret not shown)."
}
cleanup_remote_hive_registration() {
  local target="${REMOTE_HIVE_TARGET:-}"
  local remote_dir="${REMOTE_HIVE_DIR:-}"
  local remote_env="${REMOTE_HIVE_ENV:-}"
  [[ -n "$target" && -n "$remote_dir" ]] || return 0
  [[ "$remote_dir" =~ ^/tmp/review-hive-registration\.[[:alnum:]]{6}$ ]] || return 0
  local cleanup_cmd
  if [[ -n "$remote_env" ]]; then
    [[ "$remote_env" =~ ^${remote_dir}/[a-zA-Z0-9_.-]+$ ]] || return 0
    cleanup_cmd="rm -f -- $remote_env; rmdir -- $remote_dir"
  else
    cleanup_cmd="rmdir -- $remote_dir"
  fi
  ssh "${REMOTE_HIVE_SSH_ARGS[@]}" "$target" "$cleanup_cmd" 2>/dev/null || true
  REMOTE_HIVE_TARGET=""
  REMOTE_HIVE_DIR=""
  REMOTE_HIVE_ENV=""
  REMOTE_HIVE_SSH_ARGS=()
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



scale_contribute() {
  local replicas="$1" hub
  HIVE_SETUP_BACKEND=omp
  ensure_hive_contributor_env || return 1
  hub="$(read_hive_value HIVE_HUB)"
  valid_hive_hub "$hub" || { echo "ERROR: HIVE_HUB is not set in ${HIVE_CONTRIBUTOR_ENV}." >&2; return 1; }
  resolve_gh_token
  [[ -n "${GH_TOKEN_VALUE:-}" ]] || { report_missing_gh_token; return 1; }
  kubectl create namespace bluefin-system --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1
  kubectl create secret generic contribute-secret -n bluefin-system \
    --from-file=contributor.env="${HIVE_CONTRIBUTOR_ENV}" \
    --from-file=GH_TOKEN=<(printf '%s' "$GH_TOKEN_VALUE") \
    --from-file=GITHUB_COPILOT_TOKEN=<(printf '%s' "${GITHUB_COPILOT_TOKEN:-${COPILOT_GITHUB_TOKEN:-}}") \
    --from-file=ANTHROPIC_API_KEY=<(printf '%s' "${ANTHROPIC_API_KEY:-}") \
    --from-file=ANTHROPIC_OAUTH_TOKEN=<(printf '%s' "${ANTHROPIC_OAUTH_TOKEN:-}") \
    --from-file=OPENAI_API_KEY=<(printf '%s' "${OPENAI_API_KEY:-}") \
    --from-file=GEMINI_API_KEY=<(printf '%s' "${GEMINI_API_KEY:-}") \
    --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f - >/dev/null || return 1
  local prior_annotation
  prior_annotation="$(kubectl get secret contribute-secret -n bluefin-system -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}')" || return 1
  [[ -z "$prior_annotation" ]] || kubectl annotate secret contribute-secret -n bluefin-system kubectl.kubernetes.io/last-applied-configuration- >/dev/null || return 1
  kubectl apply -f deploy/contribute.yaml >/dev/null || return 1
  kubectl set env deployment/contribute -n bluefin-system AGENT_BACKEND=omp HIVE_HUB="$hub" >/dev/null || return 1
  kubectl scale deployment/contribute -n bluefin-system --replicas="$replicas" >/dev/null || return 1
  kubectl rollout status deployment/contribute -n bluefin-system --timeout=15s >/dev/null 2>&1 || echo "! rollout still progressing after 15s; workers will continue pulling/starting in background." >&2
}

stop_cluster_contributors() {
  if command -v kubectl &>/dev/null && kubectl get deployment contribute -n bluefin-system &>/dev/null; then
    kubectl scale deployment/contribute -n bluefin-system --replicas=0 >/dev/null
    echo "✓ stopped all cluster contributor workers (scaled to 0 in bluefin-system)."
  else
    echo "✓ no cluster contributor deployment found."
  fi
}
'''

# Both contributor convenience names enter the same OMP worker. Hive owns task
# assignment; OMP owns the interactive model and effort choice.
[doc("Run the Hive + OMP contributor worker.")]
review-container mode="" count="": (contribute mode count)

[doc("Run the Hive + OMP contributor worker.")]
contribute mode="" count="":
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    if [[ {{quote(mode)}} == cluster ]]; then
      replicas={{quote(count)}}; replicas="${replicas:-2}"
      [[ "$replicas" =~ ^[0-9]+$ ]] || { echo "ERROR: contribute cluster expects a replica count." >&2; exit 1; }
      STATE_DIR="${HOME}/.local/state/review"; HIVE_SRC_DIR="${STATE_DIR}/hive-src"; HIVE_REPO_URL="{{hive_repo_url}}"
      HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"; HIVE_COMMIT="${HIVE_COMMIT,,}"; mkdir -p "$STATE_DIR"
      REVIEW_RECIPE=contribute
      scale_contribute "$replicas"
      exit $?
    fi
    [[ -z {{quote(count)}} ]] || { echo "ERROR: contribute accepts one instance name outside cluster mode." >&2; exit 1; }
    INSTANCE_HINT={{quote(mode)}}
    if [[ -n "$INSTANCE_HINT" ]]; then
      [[ "$INSTANCE_HINT" =~ ^[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?$ ]] || { echo "ERROR: invalid contributor instance '${INSTANCE_HINT}'." >&2; exit 1; }
      export REVIEW_HIVE="${REVIEW_HIVE:-${INSTANCE_HINT//\//-}}"
    fi
    [[ -z "${REVIEW_DETACH:-}" ]] || { echo "ERROR: detached contributor containers are not supported." >&2; exit 1; }
    STATE_DIR="${HOME}/.local/state/review"
    HIVE_SRC_DIR="${STATE_DIR}/hive-src"
    HIVE_REPO_URL="{{hive_repo_url}}"
    HIVE_COMMIT="${REVIEW_HIVE_COMMIT:-{{hive_commit}}}"
    HIVE_COMMIT="${HIVE_COMMIT,,}"
    mkdir -p "$STATE_DIR"
    HIVE_SETUP_BACKEND=omp
    REVIEW_RECIPE=contribute
    ensure_hive_contributor_env
    report_hive_selection
    INSTANCE_KEY="$(instance_key "${BLUEFIN_INSTANCE:-contribute-${INSTANCE_HINT:-${HIVE_REGISTRATION_NAME:-default}}}")"
    INSTANCE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/bluefin/instances/${INSTANCE_KEY}"
    INSTANCE_HOME="${INSTANCE_ROOT}/home"
    mkdir -p "$INSTANCE_HOME/workspace"
    CONTAINER_NAME="bluefin-contribute-${INSTANCE_KEY}-$(date +%s)-$$"
    CONTRIBUTOR_VOLUME="${BLUEFIN_CONTRIBUTE_VOLUME:-bluefin-contribute-${INSTANCE_KEY}-home}"
    IS_OVERRIDE=0
    if [[ -n "${CONTRIBUTE_IMAGE:-}" ]]; then
      CONTRIBUTOR_IMAGE="$CONTRIBUTE_IMAGE"
      IS_OVERRIDE=1
    elif [[ -n "${BLUEFIN_CONTRIBUTE_IMAGE:-}" ]]; then
      CONTRIBUTOR_IMAGE="$BLUEFIN_CONTRIBUTE_IMAGE"
      IS_OVERRIDE=1
    elif [[ -n "${BLUEFIN_CONTRIBUTE_SIF:-}" ]]; then
      CONTRIBUTOR_IMAGE="$BLUEFIN_CONTRIBUTE_SIF"
      IS_OVERRIDE=1
    else
      CONTRIBUTOR_IMAGE="{{contribute_image}}"
    fi
    migrate_legacy_state "${XDG_STATE_HOME:-${HOME}/.local/state}/bluefin-contribute" "$INSTANCE_HOME" "bluefin-contribute.sif"
    report_launcher_identity
    resolve_gh_token
    if [[ -n "${GH_TOKEN_VALUE:-}" ]]; then export GH_TOKEN="$GH_TOKEN_VALUE"; report_gh_token_blast_radius "$GH_TOKEN_SOURCE"; else report_missing_gh_token; fi

    KVM_FAILURE=""
    if kvm_runtime_ready && [[ "$CONTRIBUTOR_IMAGE" != *.sif && ! -f "$CONTRIBUTOR_IMAGE" ]]; then
      REMOTE_HIVE_TARGET=""; REMOTE_HIVE_DIR=""; REMOTE_HIVE_ENV=""; REMOTE_HIVE_SSH_ARGS=()
      trap 'cleanup_remote_hive_registration' EXIT
      stage_hive_registration_for_remote_podman
      ensure_image "$CONTRIBUTOR_IMAGE" "contributor" "image/contribute/Containerfile" "CONTRIBUTE_IMAGE"
      report_podman_image_identity "$CONTRIBUTOR_IMAGE" "contributor" "$IS_OVERRIDE" "$MIN_CONTRIBUTOR_VERSION"
      CONTAINER_ARGS=(podman run --runtime=krun --rm --interactive --tty --name "$CONTAINER_NAME" --userns "keep-id:uid=65532,gid=65532")
      CONTAINER_ARGS+=(--volume "${CONTRIBUTOR_VOLUME}:/home/bluefin:rw" --volume "${HIVE_CONTRIBUTOR_ENV}:/home/bluefin/.config/hive/contributor.env:ro,z" --env AGENT_BACKEND=omp --env "HIVE_CONTAINER_NAME=${CONTAINER_NAME}" --env HIVE_CONTAINER_RUNTIME=podman --env "TERM=${TERM:-xterm-256color}" --env "COLORTERM=${COLORTERM:-truecolor}")
      for name in GITHUB_COPILOT_TOKEN COPILOT_GITHUB_TOKEN GITHUB_TOKEN ANTHROPIC_API_KEY ANTHROPIC_OAUTH_TOKEN OPENAI_API_KEY GEMINI_API_KEY; do
        [[ -n "${!name:-}" ]] && CONTAINER_ARGS+=(--env "$name")
      done
      [[ -n "${GH_TOKEN_VALUE:-}" ]] && CONTAINER_ARGS+=(--env GH_TOKEN)
      CONTAINER_ARGS+=("$CONTRIBUTOR_IMAGE")
      echo "✓ starting isolated KVM contributor ${CONTAINER_NAME}. Choose model and effort in OMP."
      "${CONTAINER_ARGS[@]}"
      exit $?
    fi

    require_apptainer_fallback
    [[ "$CONTRIBUTOR_IMAGE" != localhost/* ]] || { echo "ERROR: Apptainer cannot resolve local Podman image ${CONTRIBUTOR_IMAGE}." >&2; exit 1; }
    APPTAINER_IMAGE="$CONTRIBUTOR_IMAGE"; [[ "$APPTAINER_IMAGE" == *://* || "$APPTAINER_IMAGE" == *.sif || -f "$APPTAINER_IMAGE" ]] || APPTAINER_IMAGE="docker://${APPTAINER_IMAGE}"
    report_apptainer_image_identity "$CONTRIBUTOR_IMAGE" "contributor" "$IS_OVERRIDE" "$MIN_CONTRIBUTOR_VERSION"
    echo "✓ starting isolated Apptainer contributor ${INSTANCE_KEY}. Choose model and effort in OMP."
    prepare_apptainer_environment
    exec apptainer run --containall --no-eval "${APPTAINER_HOST_ARGS[@]}" --home "${INSTANCE_HOME}:/home/bluefin" --pwd /home/bluefin/workspace \
      --bind "${HIVE_CONTRIBUTOR_ENV}:/home/bluefin/.config/hive/contributor.env:ro" "$APPTAINER_IMAGE"

# Stop cluster contributor workers. Local appliances belong to their foreground
# terminals and stop with Ctrl-C.
[doc("Stop cluster contributor workers.")]
review-stop target="cluster":
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    [[ "{{target}}" == cluster ]] || { echo "ERROR: review-stop only accepts 'cluster'; local appliances stop with Ctrl-C." >&2; exit 1; }
    stop_cluster_contributors

# Maintainer convenience name for the OMP appliance. Keep this as delegation,
# not a second launch path: review-queue and review-appliance must execute the
# same image, entrypoint, configuration, and workbench.
alias review-queue := review-appliance

# The review appliance prefers one foreground libkrun microVM per invocation.
# Target-specific state and workspace directories also keep the Apptainer
# fallback independent when KVM is unavailable.
[doc("Run the distroless Bluefin Review appliance container.")]
[positional-arguments]
review-appliance *appliance_args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{shared_functions}}
    IS_OVERRIDE=0
    if [[ -n "${REVIEW_APPLIANCE_IMAGE:-}" ]]; then
      IMAGE="$REVIEW_APPLIANCE_IMAGE"
      IS_OVERRIDE=1
    elif [[ -n "${BLUEFIN_REVIEW_IMAGE:-}" ]]; then
      IMAGE="$BLUEFIN_REVIEW_IMAGE"
      IS_OVERRIDE=1
    elif [[ -n "${BLUEFIN_REVIEW_SIF:-}" ]]; then
      IMAGE="$BLUEFIN_REVIEW_SIF"
      IS_OVERRIDE=1
    else
      IMAGE="ghcr.io/projectbluefin/review:stable"
    fi

    # The token is resolved on the host and inherited by name. It is never an
    # argument, mount payload, image layer, or log value.
    if [[ -z "${GH_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
      GH_TOKEN="$(gh auth token 2>/dev/null || true)"
      export GH_TOKEN
    fi
    if [[ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
      echo "WARNING: no GitHub credential found; the queue will load empty." >&2
      echo "  Run 'gh auth login' or export GH_TOKEN." >&2
    fi
    if [[ -z "${HIVE_HUB:-}" && -f "${HOME}/.config/hive/contributor.env" ]]; then
      HIVE_CONTRIBUTOR_ENV="${HOME}/.config/hive/contributor.env"
      HIVE_HUB="$(read_hive_value HIVE_HUB)"
      if valid_hive_hub "$HIVE_HUB"; then
        export HIVE_HUB
      else
        unset HIVE_HUB
      fi
    fi

    source scripts/parse-review-args.sh
    parse_review_args "$@"
    APPLIANCE_ARGS=("${PARSED_REVIEW_ARGS[@]}")
    SCOPE=projectbluefin
    PREVIOUS=""
    for ARG in "${APPLIANCE_ARGS[@]}"; do
      if [[ "$PREVIOUS" == --repo ]]; then SCOPE="$ARG"; break; fi
      PREVIOUS="$ARG"
    done
    INSTANCE_KEY="$(instance_key "${BLUEFIN_INSTANCE:-review-${SCOPE}}")"
    INSTANCE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/bluefin/instances/${INSTANCE_KEY}"
    INSTANCE_HOME="${INSTANCE_ROOT}/home"
    INSTANCE_WORKSPACE="${INSTANCE_ROOT}/workspace"
    INSTANCE_TMP="${INSTANCE_ROOT}/tmp"
    mkdir -p "$INSTANCE_HOME" "$INSTANCE_WORKSPACE" "$INSTANCE_TMP"
    migrate_legacy_state "${XDG_STATE_HOME:-${HOME}/.local/state}/bluefin-review" "$INSTANCE_HOME" "bluefin-review.sif"
    report_launcher_identity
    CONTAINER_NAME="bluefin-review-${INSTANCE_KEY}-$(date +%s)-$$"
    KVM_FAILURE=""
    if kvm_runtime_ready && [[ "$IMAGE" != *.sif && ! -f "$IMAGE" ]]; then
      ensure_image "$IMAGE" "review appliance" "image/appliance/Containerfile" "REVIEW_APPLIANCE_IMAGE"
      report_podman_image_identity "$IMAGE" "review appliance" "$IS_OVERRIDE" "$MIN_REVIEW_APPLIANCE_VERSION"
      ARGS=(run --runtime=krun --rm --interactive --tty --name "$CONTAINER_NAME")
      ARGS+=(--userns "keep-id:uid=65532,gid=65532")
      ARGS+=(
        --volume "bluefin-review-${INSTANCE_KEY}-home:/home/bluefin:rw"
        --volume "bluefin-review-${INSTANCE_KEY}-workspace:/workspace:rw"
        --volume "bluefin-review-${INSTANCE_KEY}-tmp:/tmp:rw"
        --env GH_TOKEN --env GITHUB_TOKEN --env COPILOT_GITHUB_TOKEN --env GITHUB_COPILOT_TOKEN
        --env ANTHROPIC_API_KEY --env ANTHROPIC_OAUTH_TOKEN --env OPENAI_API_KEY --env GEMINI_API_KEY --env CONTEXT7_API_KEY
        --env AWS_BEARER_TOKEN_BEDROCK --env AWS_REGION --env AWS_DEFAULT_REGION
        --env HIVE_HUB --env BLUEFIN_REVIEW_ORG
        --env "TERM=${TERM:-xterm-256color}" --env "COLORTERM=${COLORTERM:-truecolor}"
      )
      exec podman "${ARGS[@]}" "$IMAGE" ${APPLIANCE_ARGS[@]+"${APPLIANCE_ARGS[@]}"}
    fi

    require_apptainer_fallback
    [[ "$IMAGE" != localhost/* ]] || { echo "ERROR: Apptainer cannot resolve local Podman image ${IMAGE}." >&2; exit 1; }
    APPTAINER_IMAGE="$IMAGE"; [[ "$APPTAINER_IMAGE" == *://* || "$APPTAINER_IMAGE" == *.sif || -f "$APPTAINER_IMAGE" ]] || APPTAINER_IMAGE="docker://${APPTAINER_IMAGE}"
    report_apptainer_image_identity "$IMAGE" "review appliance" "$IS_OVERRIDE" "$MIN_REVIEW_APPLIANCE_VERSION"
    prepare_apptainer_environment
    exec apptainer run --containall --no-eval "${APPTAINER_HOST_ARGS[@]}" --home "${INSTANCE_HOME}:/home/bluefin" --pwd /workspace \
      --bind "${INSTANCE_WORKSPACE}:/workspace,${INSTANCE_TMP}:/tmp" "$APPTAINER_IMAGE" ${APPLIANCE_ARGS[@]+"${APPLIANCE_ARGS[@]}"}

# Build the appliance from this checkout and hold it to its contract. The
# version is derived, never typed: FSDK series from the pinned base, revision
# from image/appliance/REVISION.
[doc("Build the review appliance image locally and verify its contract.")]
review-appliance-build tag="localhost/projectbluefin/review:dev":
    #!/usr/bin/env bash
    set -euo pipefail
    ENGINE="${CONTAINER_ENGINE:-podman}"
    VERSION="$(bash scripts/review-appliance-version.sh)"
    echo "→ building {{tag}} as version ${VERSION}"
    "$ENGINE" build \
      --format oci \
      --build-arg REVIEW_VERSION="$VERSION" \
      --build-arg REVIEW_REVISION="$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
      --file image/appliance/Containerfile \
      --tag "{{tag}}" \
      .
    bash tests/appliance-contract.sh --image "{{tag}}" --expect-arch "$(uname -m)"

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
    echo "=== Isolation runtime ==="
    KVM_FAILURE=""
    if kvm_runtime_ready; then
      echo "  ✓ Podman krun KVM runtime ready"
      pass=$((pass+1))
    elif apptainer_fallback_ready; then
      echo "  ! ${KVM_FAILURE}; isolated Apptainer fallback ready"
      pass=$((pass+1))
    else
      echo "  ✗ ${KVM_FAILURE}; ${APPTAINER_FAILURE}"
      fail=$((fail+1))
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
      if [[ -n "$DOCTOR_GH_SCOPES" ]]; then
        echo "    The agent will be able to do anything this token can: ${DOCTOR_GH_SCOPES}"
        if [[ ",${DOCTOR_GH_SCOPES//[[:space:]]/}," != *",workflow,"* && ",${DOCTOR_GH_SCOPES//[[:space:]]/}," != *"'workflow'"* ]]; then
          echo "    ! Token lacks 'workflow' scope: tasks modifying .github/workflows/* cannot be pushed or merged."
        fi
      fi
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

    doctor_image() {
      local label="$1" ref="$2" status
      echo "=== ${label} image ==="
      if image_available "$ref"; then
        echo "  ✓ ${ref} is resolvable"
        pass=$((pass+1))
      else
        status=$?
        if [[ "$status" -eq 2 ]]; then
          echo "  - ${ref} resolution deferred to Apptainer launch"
          pass=$((pass+1))
        else
          echo "  ✗ ${ref} cannot be resolved"
          fail=$((fail+1))
        fi
      fi
      echo ""
    }
    doctor_image "Review" "${REVIEW_APPLIANCE_IMAGE:-ghcr.io/projectbluefin/review:stable}"
    doctor_image "Contributor" "{{contribute_image}}"

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
    else
      echo "  ✗ ${HIVE_CONTRIBUTOR_ENV} is missing"
      echo "    review runs upstream 'just contribute-setup omp' from"
      echo "    hivecommons/hive @ ${HIVE_COMMIT:0:12} on first attended launch."
      fail=$((fail+1))
    fi
    echo ""

    echo "=== Cluster scale-out ==="
    if command -v kubectl &>/dev/null; then
      k8s_ctx="$(kubectl config current-context 2>/dev/null || true)"
      if [[ -n "$k8s_ctx" ]]; then
        echo "  ✓ Kubernetes context: ${k8s_ctx}"
        if kubectl get deployment contribute -n bluefin-system &>/dev/null; then
          ready_rep="$(kubectl get deployment contribute -n bluefin-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
          spec_rep="$(kubectl get deployment contribute -n bluefin-system -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
          echo "  ✓ contribute: ${ready_rep:-0}/${spec_rep:-0} ready replicas in bluefin-system"
        else
          echo "  - contribute: not deployed (scale with 'just contribute cluster [N]')"
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
