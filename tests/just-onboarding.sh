#!/usr/bin/env bash
# Hermetic regression harness for the root justfile.
#
# Everything the launcher can shell out to (gh, goose, gum, podman, git,
# secret-tool, kubectl) is faked on PATH, so this test never touches the network,
# never starts a real container, and never
# depends on what happens to be installed on the developer's machine.
#
# Host preflight is backend-specific. Codex contributor runs use only their
# subscription login cache; Goose configuration and Copilot are not required.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 "$repo_root/tests/worker_status_contract.py"

# REVIEW_TEST_JUSTFILE exists so the harness itself can be negative-
# tested against a deliberately broken copy of the launcher.
justfile="${REVIEW_TEST_JUSTFILE:-$repo_root/justfile}"
real_just="$(command -v just)"

# Absolute scratch root: a relative TMPDIR used to leave stray
# .just-onboarding-tmp-* directories behind in the repository.
scratch="${repo_root}/.just-onboarding-scratch-$$-$(date +%s%N)"

# The launcher opens a UNIX socket under TMPDIR, and AF_UNIX paths are capped
# at ~107 bytes, so TMPDIR has to be both ABSOLUTE (a relative value used to
# leave stray directories in the repository) and short. Pick the first short
# writable base; everything picked here is removed by the EXIT trap.
tmp_root=""
for base in "${XDG_RUNTIME_DIR:-}" "/run/user/$(id -u)" "${HOME:-}/.cache"; do
  [[ -n "$base" && "$base" == /* && -d "$base" && -w "$base" ]] || continue
  candidate="${base}/.just-onboarding-$$"
  ((${#candidate} <= 45)) || continue
  tmp_root="$candidate"
  break
done
[[ -n "$tmp_root" ]] || tmp_root="${scratch}/tmp"
fake_bin="$scratch/bin"
system_bin="$scratch/system-bin"
home="$scratch/home"
cfg_dir="$home/.config/review"
state_dir="$home/.local/state/review"
gum_log="$scratch/gum.log"
runner_log="$scratch/runner.log"
image_log="$scratch/image.log"
credential_log="$scratch/credentials.log"
remote_log="$scratch/remote.log"
fake_remote_root="$scratch/remote"
kubectl_log="$scratch/kubectl.log"
kubernetes_manifest_log="$scratch/kubernetes-manifest.json"

default_hive_backup=""
cleanup() {
  if [[ -n "$default_hive_backup" && -f "$default_hive_backup" ]]; then
    cp "$default_hive_backup" "$home/.config/hive/contributor.env"
  fi
  rm -rf "$scratch" "$tmp_root"
}
trap cleanup EXIT

mkdir -p "$fake_bin" "$system_bin" "$tmp_root" "$fake_remote_root" \
  "$home/.config/goose" "$home/.config/hive" "$cfg_dir" "$state_dir"

# Preserve the launcher's normal system tools without allowing a host kubectl
# to appear after the fake is removed for missing-command scenarios.
for executable in /usr/bin/* /bin/*; do
  [[ -x "$executable" && ! -d "$executable" ]] || continue
  name="${executable##*/}"
  [[ "$name" == "kubectl" || -e "$system_bin/$name" || -L "$system_bin/$name" ]] && continue
  ln -s "$executable" "$system_bin/$name"
done
[[ -n "$real_just" && -x "$real_just" ]] && ln -sf "$real_just" "$system_bin/just"

# ── failure reporting ─────────────────────────────────────────────────────
scenario="<startup>"
failures=0

begin() {
  scenario="$1"
  printf '• %s\n' "$scenario"
}
fail() {
  printf 'FAIL [%s]: %s\n' "$scenario" "$1" >&2
  failures=$((failures + 1))
  return 0
}
assert_contains() {
  grep -Fq -- "$1" <<<"$2" || fail "expected output to contain: $1
--- output ---
$2
--------------"
}
assert_not_contains() {
  grep -Fq -- "$1" <<<"$2" && fail "expected output NOT to contain: $1
--- output ---
$2
--------------"
  return 0
}
assert_file_contains() {
  grep -Fq -- "$1" "$2" || fail "expected $2 to contain: $1
--- $2 ---
$(cat "$2" 2>/dev/null)
--------------"
}
assert_file_not_contains() {
  grep -Fq -- "$1" "$2" && fail "expected $2 NOT to contain: $1
--- $2 ---
$(cat "$2" 2>/dev/null)
--------------"
  return 0
}
assert_file_before() {
  local earlier later file earlier_line later_line
  earlier="$1"
  later="$2"
  file="$3"
  earlier_line="$(grep -nF -- "$earlier" "$file" | head -1 | cut -d: -f1)"
  later_line="$(grep -nF -- "$later" "$file" | head -1 | cut -d: -f1)"
  [[ -n "$earlier_line" && -n "$later_line" && "$earlier_line" -lt "$later_line" ]] ||
    fail "expected '$earlier' before '$later' in $file"
}
assert_file_exists() { [[ -e "$1" ]] || fail "expected file to exist: $1"; }
assert_file_not_exists() { [[ ! -e "$1" ]] || fail "expected file to be absent: $1"; }
assert_eq() { [[ "$1" == "$2" ]] || fail "${3:-value mismatch}: expected '$2', got '$1'"; }
assert_nonzero_status() { [[ "$1" -ne 0 ]] || fail "${2:-expected a non-zero exit status}"; }
assert_zero_status() { [[ "$1" -eq 0 ]] || fail "${2:-expected exit status 0, got $1}"; }

# ── fake PATH ─────────────────────────────────────────────────────────────
cat >"$fake_bin/gh" <<'EOF'
#!/usr/bin/env bash
[[ "${GH_READY:-}" == "1" ]] || exit 1
# 'auth token' and 'auth status' are faked so a scenario can say whether the
# agent gets a GitHub identity, and with which scopes, without ever reading the
# developer's real gh login.
case "${1:-} ${2:-}" in
  "auth token")
    [[ -n "${FAKE_GH_TOKEN:-}" ]] || exit 1
    printf '%s\n' "$FAKE_GH_TOKEN"
    ;;
  "auth status")
    printf '  - Token scopes: %s\n' "${FAKE_GH_SCOPES:-'repo', 'read:org'}" >&2
    ;;
esac
exit 0
EOF
cat >"$fake_bin/goose" <<'EOF'
#!/usr/bin/env bash
[[ "${GOOSE_INSTALLED:-1}" == "1" ]] || exit 127
exit 0
EOF
cat >"$fake_bin/gum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GUM_LOG:?}"
case "${1:-}" in
  input) printf '%s\n' "${GUM_INPUT_RESPONSE:-}" ;;
  choose) printf '%s\n' "${GUM_CHOOSE_RESPONSE:-}" ;;
  *) exit 1 ;;
esac
EOF
cat >"$fake_bin/git" <<'EOF'
#!/usr/bin/env bash
# rev-parse --show-toplevel is read-only and local; answer it honestly so the
# launcher's repo-derived hive registration name can be exercised.
# FAKE_GIT_TOPLEVEL replaces the honest answer so a scenario can pretend the
# checkout lives under any directory name without renaming this one.
# Everything else stays hermetic.
if [[ "${1:-}" == "rev-parse" && "${2:-}" == "--show-toplevel" ]]; then
  if [[ -n "${FAKE_GIT_TOPLEVEL:-}" ]]; then
    printf '%s\n' "$FAKE_GIT_TOPLEVEL"
    exit 0
  fi
  exec /usr/bin/git "$@"
fi
exit 97
EOF
# secret-tool is faked so the harness can never read the developer's real login
# keyring, and so a scenario can say whether a Copilot credential exists.
cat >"$fake_bin/secret-tool" <<'EOF'
#!/usr/bin/env bash
[[ -n "${FAKE_KEYRING_COPILOT_TOKEN:-}" ]] || exit 1
printf '{"GITHUB_COPILOT_TOKEN":"%s","OTHER":"ignored"}\n' "$FAKE_KEYRING_COPILOT_TOKEN"
EOF
cat >"$fake_bin/podman" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# Image resolution is a separate concern from launching: it gets its own log
# so the 'exactly one foreground run' assertions stay meaningful, and it
# fails on demand so the missing-tag path can be exercised.
case "${1:-}" in
  system)
    if [[ "${2:-}" == "connection" && "${3:-}" == "list" ]]; then
      if [[ " $* " == *"json"* ]]; then
        printf '%s\n' "${FAKE_PODMAN_CONNECTIONS:-[]}"
        exit 0
      fi
      if [[ " $* " == *'.Name'* ]]; then
        if [[ "${FAKE_PODMAN_CONNECTIONS:-}" == \[* ]]; then
          /usr/bin/jq -r '.[] | [.Name, .URI, .Identity, (.Default | tostring)] | @tsv' \
            <<<"$FAKE_PODMAN_CONNECTIONS"
        else
          awk -F'\t' '{printf "%s\t%s\t\t%s\n", $1, $2, $3}' \
            <<<"${FAKE_PODMAN_CONNECTIONS:-}"
        fi
        exit 0
      fi
      if [[ -n "${FAKE_PODMAN_CONNECTIONS:-}" && "${FAKE_PODMAN_CONNECTIONS:-}" != "[]" ]]; then
        uri=""
        identity=""
        if [[ "${FAKE_PODMAN_CONNECTIONS}" =~ \"URI\":\"([^\"]+)\" ]]; then
          uri="${BASH_REMATCH[1]}"
        fi
        if [[ "${FAKE_PODMAN_CONNECTIONS}" =~ \"Identity\":\"([^\"]+)\" ]]; then
          identity="${BASH_REMATCH[1]}"
        fi
        printf '%s\t%s\n' "$uri" "$identity"
        exit 0
      fi
      exit 0
    fi
    ;;
  info)
    # The launcher asks which OCI runtime podman is configured with, because
    # runsc is the only one that takes --runtime-flag=host-uds=open.
    printf '%s\n' "${FAKE_PODMAN_RUNTIME:-crun}"
    exit 0
    ;;
  image | manifest | pull)
    printf '%s\n' "$*" >>"${IMAGE_LOG:?}"
    [[ "${FAKE_PODMAN_IMAGE_MISSING:-0}" == 1 ]] && exit 1
    exit 0
    ;;
  system)
    # Only 'system connection list' is consulted, to resolve the engine
    # podman run would actually use (#400). FAKE_PODMAN_CONNECTIONS holds
    # tab-separated 'name\turi\tdefault' rows, one per line; empty means no
    # connections are configured, i.e. a purely local podman.
    if [[ "${2:-}" == "connection" && "${3:-}" == "list" ]]; then
      [[ -n "${FAKE_PODMAN_CONNECTIONS:-}" ]] && printf '%s\n' "${FAKE_PODMAN_CONNECTIONS}"
      exit 0
    fi
    exit 97
    ;;
  stop)
    printf '%s\n' "$*" >>"${RUNNER_LOG:?}"
    exit 0
    ;;
  container)
    # 'container exists' — only review-stop asks.
    [[ "${FAKE_PODMAN_RUNNING:-0}" == 1 ]] && exit 0
    exit 1
    ;;
  inspect)
    # Only the liveness and ownership probes use 'podman inspect'; nothing is
    # running unless a scenario asks for it.
    case "$*" in
    esac
    printf '%s\n' "$*" >>"${IMAGE_LOG:?}"
    case "$*" in
      *review.owner*)
        # FAKE_PODMAN_OWNER_LABEL is the raw marker the launcher would have
        # written: '<boot-id>:<pid>'. Empty means an unmarked container, which
        # can only ever be an orphan.
        printf '%s\n' "${FAKE_PODMAN_OWNER_LABEL:-}"
        exit 0
        ;;
      *review.codex-auth*)
        printf '%s\n' "${FAKE_PODMAN_CODEX_AUTH_LABEL:-}"
        exit 0
        ;;
    esac
    [[ "${FAKE_PODMAN_RUNNING:-0}" == 1 ]] || { echo false; exit 1; }
    echo true
    exit 0
    ;;
esac
printf '%s\n' "$*" >> "${RUNNER_LOG:?}"
mounted_hive_dir=false
while (($#)); do
  case "$1" in
    --volume)
      volume_arg="${2:-}"
      case "$volume_arg" in
        *:/home/dev/.codex/auth.json:rw,z)
          codex_auth_source="${volume_arg%:/home/dev/.codex/auth.json:rw,z}"
          printf 'CODEX_AUTH_MOUNT:%s\n' "$codex_auth_source" >> "${CREDENTIAL_LOG:?}"
          if [[ "$codex_auth_source" == "${HOME}/.codex/auth.json" ]]; then
            printf 'CODEX_AUTH_DIRECT:yes\n' >> "${CREDENTIAL_LOG:?}"
          else
            printf 'CODEX_AUTH_DIRECT:no\n' >> "${CREDENTIAL_LOG:?}"
          fi
          [[ -f "$codex_auth_source" ]] || exit 98
          printf '{"tokens":{"access_token":"refreshed-test-secret"}}\n' >"$codex_auth_source"
          ;;
        "${HOME}/.config/hive:/home/dev/.config/hive:"*)
          mounted_hive_dir=true
          ;;
        *:/home/dev/.config/hive/contributor.env:*)
          if [[ "$mounted_hive_dir" == true && ! -e "${HOME}/.config/hive/contributor.env" ]]; then
            : >"${HOME}/.config/hive/contributor.env"
          fi
          ;;
      esac
      shift 2
      ;;
    --env)
      env_arg="${2:-}"
      case "$env_arg" in
        GITHUB_COPILOT_TOKEN|GH_TOKEN)
          if [[ -n "${!env_arg-}" ]]; then
            printf '%s:present\n' "$env_arg" >> "${CREDENTIAL_LOG:?}"
          else
            printf '%s:absent\n' "$env_arg" >> "${CREDENTIAL_LOG:?}"
          fi
          ;;
        GITHUB_COPILOT_TOKEN=*|GH_TOKEN=*)
          printf '%s:value-in-argument\n' "${env_arg%%=*}" >> "${CREDENTIAL_LOG:?}"
          ;;
      esac
      shift 2
      ;;
    --detach) detached=true; shift ;;
    *) shift ;;
  esac
done
if [[ "${FAKE_PODMAN_DETACH_SUCCESS:-0}" == 1 && "${detached:-false}" == true ]]; then
  exit 0
fi
exit 97
EOF
cat >"$fake_bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ssh %s\n' "$*" >> "${REMOTE_LOG:?}"

target=""
cmd=""
while (($#)); do
  case "$1" in
    -o|-i|-p) shift 2 ;;
    -*) shift ;;
    *)
      if [[ -z "$target" ]]; then
        target="$1"
      else
        cmd="${cmd:+$cmd }$1"
      fi
      shift
      ;;
  esac
done

remote_root="${FAKE_REMOTE_ROOT:-}"
if [[ -n "$remote_root" ]]; then
  if [[ "${FAKE_SSH_FAIL:-0}" == 1 ]]; then
    exit 1
  fi
  if [[ "${FAKE_SSH_CHMOD_FAIL:-0}" == 1 && "$cmd" == *"chmod 0600"* ]]; then
    exit 1
  fi
  # If the launcher targets the old canonical path:
  if [[ "$cmd" == *"mkdir -p \"\$HOME/.config/hive\""* || "$cmd" == *'mkdir -p "$HOME/.config/hive"'* || "$cmd" == *"mkdir -p \$HOME/.config/hive"* ]]; then
    mkdir -p "$remote_root/home/dev/.config/hive"
    chmod 0700 "$remote_root/home/dev/.config/hive"
    printf '%s\n' "$remote_root/home/dev/.config/hive"
    exit 0
  fi
  # If the launcher creates a unique private staging directory:
  if [[ "$cmd" == *"review-hive-registration"* && "$cmd" == *"mktemp -d"* ]]; then
    stage_id="${FAKE_STAGE_ID:-a1b2c3}"
    stage_path="/tmp/review-hive-registration.${stage_id}"
    mkdir -m 0700 -p "${remote_root}${stage_path}"
    printf '%s\n' "$stage_path"
    exit 0
  fi
  if [[ "$cmd" == *"chmod 0600"* ]]; then
    for part in $cmd; do
      if [[ "$part" == /tmp/review-hive-registration* || "$part" == /home/dev/.config/hive* ]]; then
        if [[ -e "${remote_root}${part}" ]]; then
          chmod 0600 "${remote_root}${part}"
        fi
      fi
    done
    exit 0
  fi
  if [[ "$cmd" == *"rm "* || "$cmd" == *"rmdir "* ]]; then
    for part in $cmd; do
      part="${part#\"}"
      part="${part%\"}"
      part="${part#\'}"
      part="${part%\'}"
      part="${part%;}"
      if [[ "$part" == /tmp/review-hive-registration* || "$part" == /home/dev/.config/hive* ]]; then
        if [[ -e "${remote_root}${part}" ]]; then
          rm -rf "${remote_root}${part}" 2>/dev/null || true
        fi
      fi
    done
    exit 0
  fi
fi

printf '%s\n' "/remote/home/.config/hive"
EOF
cat >"$fake_bin/scp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'scp %s\n' "$*" >> "${REMOTE_LOG:?}"

if [[ "${FAKE_SCP_FAIL:-0}" == 1 ]]; then
  exit 1
fi

remote_root="${FAKE_REMOTE_ROOT:-}"
if [[ -n "$remote_root" ]]; then
  src=""
  dest=""
  while (($#)); do
    case "$1" in
      -o|-i|-P) shift 2 ;;
      -*) shift ;;
      *)
        if [[ -z "$src" ]]; then
          src="$1"
        else
          dest="$1"
        fi
        shift
        ;;
    esac
  done
  if [[ -n "$src" && -n "$dest" && "$dest" == *:* ]]; then
    remote_path="${dest#*:}"
    if [[ "$remote_path" == /tmp/review-hive-registration* || "$remote_path" == /home/dev/.config/hive* || "$remote_path" == /remote/home/* ]]; then
      if [[ "$remote_path" == "/remote/home/"* ]]; then
        local_target="${remote_root}/home/dev/${remote_path#/remote/home/}"
      else
        local_target="${remote_root}${remote_path}"
      fi
      mkdir -p "$(dirname "$local_target")"
      cp -p "$src" "$local_target"
    fi
  fi
fi
EOF
cat >"$fake_bin/jq" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_JQ_UNAVAILABLE:-0}" == 1 ]]; then
  echo "jq: command not found" >&2
  exit 127
fi
exec /usr/bin/jq "$@"
EOF
chmod +x "$fake_bin"/*

# ── fixtures ──────────────────────────────────────────────────────────────
write_goose_config() {
  cat >"$home/.config/goose/config.yaml" <<'EOF'
provider: openai
base_url: http://127.0.0.1:11434/v1
api_key: local-test-key
model: llama3.1
EOF
}
cat >"$home/.config/hive/contributor.env" <<'EOF'
HIVE_REGISTRATION_TOKEN=super-secret-registration-token
HIVE_HUB=wss://example.invalid/contribute
CONTRIBUTOR_ID=test-contributor
CONTRIBUTOR_USERNAME=test-user
AGENT_BACKEND=goose
EOF
write_goose_config

reset_logs() {
  : >"$gum_log"
  : >"$runner_log"
  : >"$image_log"
  : >"$credential_log"
  : >"$remote_log"
  : >"$kubectl_log"
  : >"$kubernetes_manifest_log"
  RECIPE_ARGS=()
}
reset_logs

# ── runner ────────────────────────────────────────────────────────────────
# run_recipe <recipe> [KEY=VALUE ...] — runs the launcher with a hermetic
# environment; sets OUT and STATUS. Positional recipe arguments go in the
# RECIPE_ARGS array, since everything after <recipe> is read as environment.
run_recipe() {
  local recipe="$1"
  shift
  set +e
  OUT="$(
    env \
      -u TOOL -u REVIEW_HIVE_COMMIT \
      -u AGENT_MODEL -u GOOSE_PROVIDER -u GOOSE_MODEL -u GH_READY \
      -u GITHUB_COPILOT_TOKEN \
      -u GH_TOKEN -u GITHUB_TOKEN \
      -u REVIEW_GH_TOKEN -u FAKE_GH_TOKEN -u FAKE_GH_SCOPES \
      -u CODEX_HOME \
      -u BLUEFIN_REVIEW_BACKEND \
      -u GOOSE_THINKING_EFFORT -u GOOSE_CONTEXT_LIMIT \
      -u REVIEW_NON_INTERACTIVE -u GOOSE_INSTALLED \
      -u REVIEW_CONTAINER_NAME -u REVIEW_DETACH \
      -u REVIEW_HIVE -u REVIEW_CONTRIBUTOR_IMAGE \
      -u REVIEW_QUEUE_NAME -u REVIEW_SCALE -u XDG_STATE_HOME -u FAKE_GIT_TOPLEVEL \
      -u FAKE_PODMAN_CONNECTIONS \
      -u FAKE_JQ_UNAVAILABLE \
      -u FAKE_SCP_FAIL -u FAKE_SSH_CHMOD_FAIL -u FAKE_STAGE_ID \
      -u REVIEW_RUNTIME -u FAKE_KUBECTL_DASHBOARD_API_UNAVAILABLE \
      -u FAKE_KUBECTL_DASHBOARD_PVC_MISSING -u FAKE_KUBECTL_DASHBOARD_PVC_FORBIDDEN \
      -u OTEL_EXPORTER_OTLP_ENDPOINT -u OTEL_EXPORTER_OTLP_HEADERS \
      -u REVIEW_LAB -u REVIEW_LAB_BROKER -u REVIEW_PERSONAL_SKILLS \
      -u FAKE_PODMAN_CONNECTIONS -u REVIEW_QUEUE_ALLOW_REMOTE_STATE \
      -u CONTAINER_HOST -u CONTAINER_CONNECTION \
      -u HIVE_HUB \
      -u FAKE_KUBECTL_ANNOTATION_GET_FAIL -u FAKE_KUBECTL_ANNOTATE_FAIL \
      -u FAKE_KUBECTL_DEPLOYMENT_GET_FAIL -u FAKE_KUBECTL_HAS_LAST_APPLIED \
      -u FAKE_KUBECTL_NAMESPACE_APPLY_FAIL -u FAKE_KUBECTL_SECRET_APPLY_FAIL \
      -u FAKE_KUBECTL_DEPLOY_APPLY_FAIL -u FAKE_KUBECTL_SET_ENV_FAIL \
      -u FAKE_KUBECTL_SCALE_FAIL -u FAKE_KUBECTL_ROLLOUT_FAIL \
      -u FAKE_KUBECTL_REWRITE_HIVE_HUB \
      HOME="$home" PATH="$fake_bin:$system_bin" TMPDIR="$tmp_root" \
      XDG_RUNTIME_DIR="$tmp_root" \
      FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token \
      GUM_LOG="$gum_log" RUNNER_LOG="$runner_log" \
      IMAGE_LOG="$image_log" \
      CREDENTIAL_LOG="$credential_log" \
      REMOTE_LOG="$remote_log" \
      FAKE_REMOTE_ROOT="$fake_remote_root" \
      KUBECTL_LOG="$kubectl_log" \
      KUBERNETES_MANIFEST_LOG="$kubernetes_manifest_log" \
      "$@" \
      "$real_just" --justfile "$justfile" "$recipe" "${RECIPE_ARGS[@]}" 2>&1
  )"
  STATUS=$?
  set -e
}

error_line_count() { grep -c '^ERROR:' <<<"$1" || true; }

# ══ 1. Preflight: exactly one actionable ERROR per failure ════════════════
begin "preflight: missing GitHub auth yields one actionable error"
run_recipe review-container
assert_nonzero_status "$STATUS" "unauthenticated gh must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "gh auth login" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "copilot" "$OUT"
assert_not_contains "codex" "$OUT"

begin "preflight: missing Goose provider configuration yields one actionable error"
rm -f "$home/.config/goose/config.yaml"
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "an unconfigured goose must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "goose configure" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "codex" "$OUT"
write_goose_config

begin "preflight: an invalid Goose config (no provider) is treated as unconfigured"
printf 'model: llama3.1\n' >"$home/.config/goose/config.yaml"
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "a provider-less goose config must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "goose configure" "$OUT"
write_goose_config

begin "preflight: Goose's current active_provider config counts as configured"
# Goose >= 1.45 records the selection as 'active_provider:' beside a
# 'providers:' map; the launcher must accept it or every launch dies on
# "Goose has no usable provider configuration" after Goose migrates the
# host config. A passing preflight reaches the fake runner, which always
# exits non-zero.
cat >"$home/.config/goose/config.yaml" <<'EOF'
providers:
  github_copilot:
    enabled: true
    model: kimi-k3
    configured: true
active_provider: github_copilot
EOF
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_not_contains "Goose has no usable provider configuration" "$OUT"
write_goose_config

begin "preflight: unsupported GOOSE_PROVIDER yields one actionable Copilot-only error"
run_recipe review-container GH_READY=1 GOOSE_PROVIDER=openai
assert_nonzero_status "$STATUS" "an unsupported provider must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "GOOSE_PROVIDER=openai is not supported" "$OUT"
assert_contains "GOOSE_PROVIDER=github_copilot" "$OUT"

# ══ 2. TOOL handling: selected backends ════════════════════════════════════
begin "TOOL=claude is rejected with a Goose-only error"
run_recipe review-container GH_READY=1 TOOL=claude
assert_nonzero_status "$STATUS" "a non-Goose TOOL must be a hard error"
assert_contains "TOOL=claude is not supported" "$OUT"
assert_contains "review supports Goose and Codex" "$OUT"
assert_not_contains "auto-detected" "$OUT"
assert_not_contains "Multiple AI CLIs" "$OUT"

begin "TOOL=goose is accepted"
# A passing TOOL check reaches the fake runner, which always exits non-zero.
run_recipe review-container GH_READY=1 TOOL=goose
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_not_contains "is not supported" "$OUT"
assert_not_contains "Unset TOOL" "$OUT"

begin "TOOL=pi is rejected before container launch"
reset_logs
run_recipe review-container GH_READY=1 TOOL=pi PI_API_KEY=pi-test-key
assert_nonzero_status "$STATUS" "Pi must be unsupported"
assert_contains "is not supported" "$OUT"
assert_file_not_contains "run --rm" "$runner_log"

begin "TOOL=codex uses subscription auth without Goose or Copilot"
reset_logs
rm -f "$home/.config/goose/config.yaml"
mkdir -p "$home/.codex"
printf '{"tokens":{"access_token":"codex-test-secret"}}\n' >"$home/.codex/auth.json"
chmod 0400 "$home/.codex/auth.json"
run_recipe review-container GH_READY=1 TOOL=codex
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_not_contains "Goose has no usable provider configuration" "$OUT"
assert_not_contains "Copilot" "$OUT"
assert_file_contains "--env AGENT_BACKEND=codex" "$runner_log"
codex_auth_mount="$(sed -n 's/^CODEX_AUTH_MOUNT://p' "$credential_log")"
assert_contains "/tmp/review-codex-auth." "$codex_auth_mount"
[[ "$codex_auth_mount" != "$home/.codex/auth.json" ]] || fail "host Codex auth must not be mounted directly"
assert_file_not_contains "codex-test-secret" "$runner_log"
assert_file_not_contains "codex-test-secret" "$OUT"
assert_file_not_exists "$codex_auth_mount"
assert_file_contains "codex-test-secret" "$home/.codex/auth.json"
rm -f "$home/.codex/auth.json"
rmdir "$home/.codex"
write_goose_config

begin "selection: default Gemini model is noninteractive"
reset_logs
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_file_contains "--env GOOSE_PROVIDER=github_copilot" "$runner_log"
assert_file_contains "--env GOOSE_MODEL=gemini-3.8-flash" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_not_exists "$cfg_dir/last-selections.env"
assert_file_not_exists "$cfg_dir/secrets.env"
assert_eq "$(wc -c <"$gum_log")" 0 "gum must not be invoked"

begin "review-container: thinking-effort overrides are passed through"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  GOOSE_THINKING_EFFORT=medium
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"

begin "review-container: no profile is gemini at max with the provider's own context"
reset_logs
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=gemini-3.8-flash" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_not_contains "GOOSE_CONTEXT_LIMIT" "$runner_log"
assert_eq "$(wc -c <"$gum_log")" 0 "a headless run must not invoke gum"

begin "review-container: the opus profile clamps the context window"
reset_logs
RECIPE_ARGS=(opus5 high)
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=claude-opus-5" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=high" "$runner_log"
assert_file_contains "--env GOOSE_CONTEXT_LIMIT=264000" "$runner_log"

begin "review-container: the k3 profile is max effort with a clamped context"
reset_logs
RECIPE_ARGS=(k3)
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=kimi-k3" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_contains "--env GOOSE_CONTEXT_LIMIT=264000" "$runner_log"

begin "review-container: the sol profile is medium effort with provider context"
reset_logs
RECIPE_ARGS=(gpt-sol)
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-sol" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"
assert_file_not_contains "GOOSE_CONTEXT_LIMIT" "$runner_log"

begin "review-container: an effort argument overrides the profile default"reset_logs
RECIPE_ARGS=(opus5 max)
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"

begin "review-container: an unknown profile is one actionable error"
reset_logs
RECIPE_ARGS=(gpt-9)
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "an unknown profile must not launch anything"
assert_contains "unknown model profile 'gpt-9'" "$OUT"
assert_contains "Known profiles" "$OUT"
assert_contains "gemini (gemini-3.8-flash), sol (gpt-5.6-sol), opus5 (claude-opus-5), k3 (kimi-k3)" "$OUT"

begin "review-container: an unknown thinking effort is one actionable error"
reset_logs
RECIPE_ARGS=(gemini ludicrous)
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "an unknown effort must not launch anything"
assert_contains "unknown thinking effort 'ludicrous'" "$OUT"

begin "review-container: an empty profile never prompts"
# A short fixed profile list does not need a picker. An empty profile is the
# default one, and gum sitting on PATH with a canned answer must not change
# that.
reset_logs
RECIPE_ARGS=("" high)
run_recipe review-container GH_READY=1 \
  GUM_CHOOSE_RESPONSE=opus5
assert_eq "$(wc -c <"$gum_log")" 0 "the launcher must never prompt for a model"
assert_file_contains "--env GOOSE_MODEL=gemini-3.8-flash" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=high" "$runner_log"

begin "review-container: maintainer backend choice never changes Hive selection"
reset_logs
run_recipe review-container GH_READY=1 BLUEFIN_REVIEW_BACKEND=codex
assert_file_contains "--env AGENT_BACKEND=goose" "$runner_log"
assert_file_not_contains "BLUEFIN_REVIEW_BACKEND" "$runner_log"

# ══ 2b. Dashboard: optional Hive URL, GH_TOKEN required, args pass through ═
begin "review-queue: launches the dashboard without Hive when none is configured"
reset_logs
mv "$home/.config/hive" "$home/.config/hive.saved"
RECIPE_ARGS=(--repo bluefin)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
mv "$home/.config/hive.saved" "$home/.config/hive"
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_file_contains "--name review-queue" "$runner_log"
assert_file_contains "queue --repo bluefin" "$runner_log"
assert_file_contains "--env GOOSE_PROVIDER=github_copilot" "$runner_log"
assert_file_not_contains "BLUEFIN_REVIEW_BACKEND" "$runner_log"
assert_file_not_contains ".config/hive" "$runner_log"
assert_file_not_contains "HIVE_HUB" "$runner_log"
assert_file_contains "GH_TOKEN:present" "$credential_log"
assert_not_contains "contributor.env" "$OUT"
assert_file_not_contains "/home/dev/.codex/auth.json" "$runner_log"
assert_contains "starting the maintainer review dashboard (Hive not configured)" "$OUT"

begin "review-queue: passes the default Hive URL without mounting its registration"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--env HIVE_HUB=wss://example.invalid/contribute" "$runner_log"
assert_file_not_contains ".config/hive" "$runner_log"
assert_file_not_contains "super-secret-registration-token" "$runner_log"
assert_contains "hive: wss://example.invalid/contribute (default registration)" "$OUT"
assert_contains "starting the maintainer review dashboard (Hive configured)" "$OUT"

begin "review-queue: REVIEW_HIVE selects another hosted Hive"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.endusers.env"
sed -i 's|^HIVE_HUB=.*|export HIVE_HUB="wss://endusers.invalid/contribute"|' \
  "$home/.config/hive/contributor.endusers.env"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_HIVE=endusers
assert_file_contains "--env HIVE_HUB=wss://endusers.invalid/contribute" "$runner_log"
assert_file_not_contains ".config/hive" "$runner_log"
assert_file_not_contains "super-secret-registration-token" "$runner_log"
assert_contains "hive: wss://endusers.invalid/contribute (registration 'endusers')" "$OUT"
rm -f "$home/.config/hive/contributor.endusers.env"

begin "review-queue: an unusable Hive file does not block GitHub review"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.broken.env"
sed -i '/^HIVE_HUB=/d' "$home/.config/hive/contributor.broken.env"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_HIVE=broken
assert_file_not_contains "HIVE_HUB" "$runner_log"
assert_contains "has no usable HIVE_HUB; the dashboard will continue without Hive" "$OUT"
assert_contains "starting the maintainer review dashboard (Hive not configured)" "$OUT"
rm -f "$home/.config/hive/contributor.broken.env"

begin "review-queue: a plaintext Hive cannot receive the maintainer token"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.plaintext.env"
sed -i 's|^HIVE_HUB=.*|HIVE_HUB=ws://plaintext.invalid/contribute|' \
  "$home/.config/hive/contributor.plaintext.env"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_HIVE=plaintext
assert_file_not_contains "HIVE_HUB" "$runner_log"
assert_contains "has an unsupported HIVE_HUB; the dashboard requires one wss:// or https:// URL" "$OUT"
assert_contains "starting the maintainer review dashboard (Hive not configured)" "$OUT"
rm -f "$home/.config/hive/contributor.plaintext.env"

begin "review-queue: a multi-hub worker registration is not an API target"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.multi.env"
sed -i 's|^HIVE_HUB=.*|HIVE_HUB=wss://one.invalid/contribute,wss://two.invalid/contribute|' \
  "$home/.config/hive/contributor.multi.env"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_HIVE=multi
assert_file_not_contains "HIVE_HUB" "$runner_log"
assert_contains "has an unsupported HIVE_HUB; the dashboard requires one wss:// or https:// URL" "$OUT"
assert_contains "starting the maintainer review dashboard (Hive not configured)" "$OUT"
rm -f "$home/.config/hive/contributor.multi.env"

begin "review-queue: a remote podman connection fails closed before launching (#400)"
reset_logs
# A remote default connection resolves the dashboard state bind on the
# ENGINE host, not this one; landing batches would be written where no
# local tool can ever find them again. Refuse to launch instead of quietly
# binding the wrong filesystem.
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://jorge@ghost:22/run/user/1000/podman/podman.sock\ttrue')"
assert_nonzero_status "$STATUS" "a remote default connection must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "podman's selected engine is remote (ssh://ghost:22/run/user/1000/podman/podman.sock)" "$OUT"
assert_eq "$(wc -l <"$runner_log")" 0 "no podman run may happen once the engine is rejected"
assert_eq "$(wc -c <"$credential_log")" 0 "remote rejection must precede credential transfer"

begin "review-queue: explicit SSH selection is resolved and redacted"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://alice:s3cr3t@ghost:22/run/user/1000/podman/podman.sock\tfalse')" \
  CONTAINER_CONNECTION=ghost
assert_nonzero_status "$STATUS" "an explicitly selected SSH engine must fail the launch"
assert_eq "$(wc -l <"$runner_log")" 0 "explicit SSH rejection must prevent podman run"
assert_eq "$(wc -c <"$credential_log")" 0 "explicit SSH rejection must precede credentials"
assert_not_contains "s3cr3t" "$OUT"
assert_contains "ghost" "$OUT"

begin "review-queue: TCP engine selection fails closed"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'tcp\ttcp://alice:s3cr3t@remote.example:1234/run/podman.sock\ttrue')"
assert_nonzero_status "$STATUS" "a TCP engine must fail the launch"
assert_eq "$(wc -l <"$runner_log")" 0 "TCP rejection must prevent podman run"
assert_eq "$(wc -c <"$credential_log")" 0 "TCP rejection must precede credentials"
assert_not_contains "s3cr3t" "$OUT"

begin "review-queue: unresolved explicit connection fails closed"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://ghost:22/run/user/1000/podman/podman.sock\tfalse')" \
  CONTAINER_CONNECTION=missing
assert_nonzero_status "$STATUS" "an unresolved explicit connection must fail the launch"
assert_contains "could not resolve selected Podman connection 'missing'" "$OUT"
assert_eq "$(wc -l <"$runner_log")" 0 "unresolved selection must prevent podman run"
assert_eq "$(wc -c <"$credential_log")" 0 "unresolved selection must precede credentials"

begin "review-queue: CONTAINER_HOST overrides a remote saved default"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://ghost:22/run/user/1000/podman/podman.sock\ttrue')" \
  CONTAINER_HOST=unix:///run/user/1000/podman/podman.sock
assert_file_contains "--name review-queue" "$runner_log"
assert_not_contains "podman's default connection is remote" "$OUT"

begin "review-queue: local Unix socket and local mode remain allowed"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'local\tunix:///run/user/1000/podman/podman.sock\ttrue')"
assert_file_contains "--name review-queue" "$runner_log"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--name review-queue" "$runner_log"

begin "review-queue: remote opt-in is strict and redacted"
for opt_in in 0 invalid; do
  reset_logs
  run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
    FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://alice:s3cr3t@ghost:22/run/user/1000/podman/podman.sock\ttrue')" \
    REVIEW_QUEUE_ALLOW_REMOTE_STATE="$opt_in"
  assert_nonzero_status "$STATUS" "remote opt-in ${opt_in} must fail closed"
  assert_eq "$(wc -l <"$runner_log")" 0 "remote opt-in ${opt_in} must prevent podman run"
  assert_eq "$(wc -c <"$credential_log")" 0 "remote opt-in ${opt_in} must precede credentials"
  assert_not_contains "s3cr3t" "$OUT"
done
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://alice:s3cr3t@ghost:22/run/user/1000/podman/podman.sock\ttrue')" \
  REVIEW_QUEUE_ALLOW_REMOTE_STATE=1
assert_file_contains "--name review-queue" "$runner_log"
assert_not_contains "s3cr3t" "$OUT"

begin "review-queue: REVIEW_QUEUE_ALLOW_REMOTE_STATE acknowledges a remote connection"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://jorge@ghost:22/run/user/1000/podman/podman.sock\ttrue')" \
  REVIEW_QUEUE_ALLOW_REMOTE_STATE=1
assert_file_contains "--name review-queue" "$runner_log"
assert_contains "podman's selected engine is remote (ssh://ghost:22/run/user/1000/podman/podman.sock); the dashboard state directory binds on that engine host" "$OUT"

begin "review-queue: a non-default or absent podman connection stays local"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_PODMAN_CONNECTIONS="$(printf 'ghost\tssh://jorge@ghost:22/run/user/1000/podman/podman.sock\tfalse')"
assert_file_contains "--name review-queue" "$runner_log"
assert_not_contains "podman's default connection is remote" "$OUT"

begin "review-queue: the dashboard state directory persists on the host"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
# Landing batches, their failure reasons, and the action trace are the only
# durable record of what the landing agent did; the reclaim-by-replace
# relaunch must not wipe them (#281).
assert_file_contains "--volume ${home}/.local/state/bluefin-review:/home/dev/.local/state/bluefin-review:rw,z" "$runner_log"
assert_file_exists "${home}/.local/state/bluefin-review"
# The instance name rides into the container so two named dashboards sharing
# the state directory mint distinct batch ids.
assert_file_contains "--env BLUEFIN_REVIEW_INSTANCE" "$runner_log"

begin "review-queue: XDG_STATE_HOME relocates the dashboard state mount"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  XDG_STATE_HOME="$scratch/xdg"
assert_file_contains "--volume ${scratch}/xdg/bluefin-review:/home/dev/.local/state/bluefin-review:rw,z" "$runner_log"
assert_file_exists "${scratch}/xdg/bluefin-review"

# ══ 2c. The optional lab: one socket, one session, nothing else (#379) ════
# The launcher offers the lab only when the host can actually reach a
# cluster, asks once, and hands the container exactly one Unix socket. None
# of it may become a dependency: every negative path here still launches a
# fully usable dashboard.
lab_broker="$repo_root/scripts/review-lab-broker.py"
lab_skills="$scratch/personal-skills"
mkdir -p "$lab_skills/lab-test" "$lab_skills/k3s-cluster-ops"
printf 'personal lab skill\n' >"$lab_skills/lab-test/SKILL.md"
printf 'personal lab skill\n' >"$lab_skills/k3s-cluster-ops/SKILL.md"

install_fake_kubectl() {
  cat >"$fake_bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${KUBECTL_LOG:?}"
case "$*" in
  "config current-context") printf 'ghost-lab\n' ;;
  "get --raw=/readyz?verbose --request-timeout=5s")
    [[ "${FAKE_KUBECTL_DASHBOARD_API_UNAVAILABLE:-0}" == 1 ]] && exit 52
    ;;
  "get nodes -o name") printf 'node/ghost\nnode/exo-0\n' ;;
  "get pvc review-queue-state -n bluefin-system"*)
    [[ "${FAKE_KUBECTL_DASHBOARD_PVC_MISSING:-0}" == 1 ]] && exit 51
    [[ "${FAKE_KUBECTL_DASHBOARD_PVC_FORBIDDEN:-0}" == 1 ]] && exit 53
    ;;
  "create -f -") cat >"${KUBERNETES_MANIFEST_LOG:?}" ;;
  "attach --stdin --tty review-queue-"*"-n bluefin-system") ;;
  "delete pod review-queue-"*"-n bluefin-system --ignore-not-found --wait=false") ;;
  "delete secret review-session-"*"-n bluefin-system --ignore-not-found --wait=false") ;;
  "apply -f -")
    cat >/dev/null
    [[ "${FAKE_KUBECTL_NAMESPACE_APPLY_FAIL:-0}" == 1 ]] && exit 45
    ;;
  "apply --server-side --force-conflicts -f -")
    cat >/dev/null
    [[ "${FAKE_KUBECTL_SECRET_APPLY_FAIL:-0}" == 1 ]] && exit 46
    ;;
  "apply -f deploy/review-contributor.yaml")
    [[ "${FAKE_KUBECTL_DEPLOY_APPLY_FAIL:-0}" == 1 ]] && exit 47
    ;;
  "set env deployment/review-contributor -n bluefin-system "*)
    [[ "${FAKE_KUBECTL_SET_ENV_FAIL:-0}" == 1 ]] && exit 48
    ;;
  "scale deployment/review-contributor -n bluefin-system --replicas="*)
    if [[ -n "${FAKE_KUBECTL_REWRITE_HIVE_HUB:-}" ]]; then
      sed -i "s|^HIVE_HUB=.*|HIVE_HUB=${FAKE_KUBECTL_REWRITE_HIVE_HUB}|" \
        "$HOME/.config/hive/contributor.env"
    fi
    [[ "${FAKE_KUBECTL_SCALE_FAIL:-0}" == 1 ]] && exit 49
    ;;
  "rollout status deployment/review-contributor -n bluefin-system --timeout=15s")
    [[ "${FAKE_KUBECTL_ROLLOUT_FAIL:-0}" == 1 ]] && exit 50
    ;;
  "get deployment review-contributor -n bluefin-system")
    [[ "${FAKE_KUBECTL_DEPLOYMENT_GET_FAIL:-0}" == 1 ]] && exit 44
    ;;
  "get deployment review-contributor -n bluefin-system -o jsonpath={.status.readyReplicas}")
    printf '3'
    ;;
  "get deployment review-contributor -n bluefin-system -o jsonpath={.spec.replicas}")
    printf '3'
    ;;
  "get secret review-contributor-secret -n bluefin-system -o jsonpath={.metadata.annotations.kubectl\\.kubernetes\\.io/last-applied-configuration}")
    [[ "${FAKE_KUBECTL_ANNOTATION_GET_FAIL:-0}" == 1 ]] && exit 43
    [[ "${FAKE_KUBECTL_HAS_LAST_APPLIED:-0}" == 1 ]] &&
      printf 'legacy-configuration\n'
    ;;
  "annotate secret review-contributor-secret -n bluefin-system kubectl.kubernetes.io/last-applied-configuration-")
    [[ "${FAKE_KUBECTL_ANNOTATE_FAIL:-0}" == 1 ]] && exit 42
    ;;
  "create secret generic "*)
    if [[ "$*" == *"--from-env-file="* && ("$*" == *"--from-file="* || "$*" == *"--from-literal="*) ]]; then
      echo "error: from-env-file cannot be combined with from-file or from-literal" >&2
      exit 1
    fi
    printf '{"items":[]}\n'
    ;;
  *) printf '{"items":[]}\n' ;;
esac
exit 0
EOF
  chmod +x "$fake_bin/kubectl"
}
remove_fake_kubectl() { rm -f "$fake_bin/kubectl"; }

assert_no_lab_handoff() {
  assert_file_not_contains "/run/bluefin-review-lab" "$runner_log"
  assert_file_not_contains "BLUEFIN_REVIEW_LAB_SOCKET" "$runner_log"
  assert_file_not_contains "BLUEFIN_REVIEW_LAB_SESSION" "$runner_log"
  assert_file_not_contains "host-uds" "$runner_log"
}

assert_kubernetes_dashboard_manifest() {
  python3 - "$kubernetes_manifest_log" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    pod = json.load(stream)

container = pod["spec"]["containers"][0]
assert pod["metadata"]["namespace"] == "bluefin-system"
assert pod["spec"]["automountServiceAccountToken"] is False
assert pod["spec"]["restartPolicy"] == "Never"
assert container["stdin"] is True
assert container["stdinOnce"] is True
assert container["tty"] is True
assert container["imagePullPolicy"] == "Always"
assert all("value" not in item and "secretKeyRef" in item["valueFrom"] for item in container["env"])
assert container["securityContext"]["allowPrivilegeEscalation"] is False
assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
assert any(volume["name"] == "workspace" and volume["emptyDir"] == {} for volume in pod["spec"]["volumes"])
assert any(
    volume["name"] == "state"
    and volume["persistentVolumeClaim"]["claimName"] == "review-queue-state"
    for volume in pod["spec"]["volumes"]
)
assert "hostPath" not in json.dumps(pod)
PY
}

assert_review_queue_state_manifest() {
  local manifest="$repo_root/deploy/review-queue-state.yaml"
  assert_file_exists "$manifest"
  assert_file_contains "kind: PersistentVolumeClaim" "$manifest"
  assert_file_contains "name: review-queue-state" "$manifest"
  assert_file_contains "namespace: bluefin-system" "$manifest"
  assert_file_contains "- ReadWriteOnce" "$manifest"
  assert_file_contains "storage: 1Gi" "$manifest"
  assert_file_not_contains "hostPath" "$manifest"
  assert_file_not_contains "OTEL_" "$manifest"
}

begin "review-queue: Kubernetes state claim is a dedicated durable PVC"
assert_review_queue_state_manifest

begin "review-queue: no host kubectl means no lab and no prompt"
reset_logs
remove_fake_kubectl
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_no_lab_handoff
assert_not_contains "Use it for this session only?" "$OUT"
assert_contains "starting the maintainer review dashboard" "$OUT"

begin "review-queue: an unavailable Kubernetes runtime falls back to Podman"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s
assert_nonzero_status "$STATUS" "the Podman fallback runner exits non-zero"
assert_file_contains "run --rm --interactive --tty --replace --name review-queue" "$runner_log"
assert_contains "Kubernetes is unavailable; using the local Podman dashboard" "$OUT"

install_fake_kubectl
begin "review-queue: an unreachable Kubernetes API falls back before state-claim lookup"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s \
  FAKE_KUBECTL_DASHBOARD_API_UNAVAILABLE=1
assert_nonzero_status "$STATUS" "the Podman fallback runner exits non-zero"
assert_contains "Kubernetes is unavailable; using the local Podman dashboard" "$OUT"
assert_file_contains "get --raw=/readyz?verbose --request-timeout=5s" "$kubectl_log"
assert_file_not_contains "get pvc review-queue-state -n bluefin-system" "$kubectl_log"
assert_file_contains "run --rm --interactive --tty --replace --name review-queue" "$runner_log"

begin "review-queue: Kubernetes runtime uses an ephemeral restricted dashboard Pod"
reset_logs
RECIPE_ARGS=(--repo bluefin)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s
assert_zero_status "$STATUS" "the fake Kubernetes dashboard session must succeed"
assert_file_contains "config current-context" "$kubectl_log"
assert_file_contains "get --raw=/readyz?verbose --request-timeout=5s" "$kubectl_log"
assert_file_contains "get pvc review-queue-state -n bluefin-system" "$kubectl_log"
assert_file_contains "create secret generic review-session-" "$kubectl_log"
assert_file_contains "create -f -" "$kubectl_log"
assert_file_contains "wait --for=condition=Ready pod/review-queue-" "$kubectl_log"
assert_file_contains "-n bluefin-system --timeout=5m" "$kubectl_log"
assert_file_contains "attach --stdin --tty review-queue-" "$kubectl_log"
assert_file_before "wait --for=condition=Ready pod/review-queue-" \
  "attach --stdin --tty review-queue-" "$kubectl_log"
assert_file_contains "delete pod review-queue-" "$kubectl_log"
assert_file_contains "delete secret review-session-" "$kubectl_log"
assert_file_before "attach --stdin --tty review-queue-" \
  "delete pod review-queue-" "$kubectl_log"
assert_file_before "delete pod review-queue-" \
  "delete secret review-session-" "$kubectl_log"
assert_eq "$(wc -c <"$runner_log")" 0 "Kubernetes runtime must not launch Podman"
assert_file_not_contains "gho-test-token" "$kubectl_log"
assert_file_not_contains "gho-test-token" "$kubernetes_manifest_log"
assert_kubernetes_dashboard_manifest || fail "Kubernetes dashboard manifest violates its runtime contract"

begin "review-queue: Kubernetes countme values remain in the session Secret"
reset_logs
otlp_endpoint="https://countme.example.invalid/v1/metrics"
otlp_headers="x-session-key=otlp-test-secret"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s \
  OTEL_EXPORTER_OTLP_ENDPOINT="$otlp_endpoint" \
  OTEL_EXPORTER_OTLP_HEADERS="$otlp_headers"
assert_zero_status "$STATUS" "the fake Kubernetes dashboard session must succeed"
assert_file_contains "--from-file=OTEL_EXPORTER_OTLP_ENDPOINT=" "$kubectl_log"
assert_file_contains "--from-file=OTEL_EXPORTER_OTLP_HEADERS=" "$kubectl_log"
assert_not_contains "$otlp_endpoint" "$OUT"
assert_file_not_contains "$otlp_endpoint" "$kubectl_log"
assert_file_not_contains "$otlp_endpoint" "$kubernetes_manifest_log"
assert_not_contains "$otlp_headers" "$OUT"
assert_file_not_contains "$otlp_headers" "$kubectl_log"
assert_file_not_contains "$otlp_headers" "$kubernetes_manifest_log"

begin "review-queue: a missing dashboard state claim fails before starting a Pod"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s \
  FAKE_KUBECTL_DASHBOARD_PVC_MISSING=1
assert_nonzero_status "$STATUS" "a Kubernetes dashboard needs its persistent state claim"
assert_contains "Kubernetes dashboard state claim 'review-queue-state' cannot be read" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "a missing state claim must not fall back to a Podman session"
assert_file_not_contains "create secret generic review-session-" "$kubectl_log"
assert_file_not_contains "create -f -" "$kubectl_log"

begin "review-queue: an unreadable dashboard state claim fails before starting a Pod"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s \
  FAKE_KUBECTL_DASHBOARD_PVC_FORBIDDEN=1
assert_nonzero_status "$STATUS" "an unreadable state claim must stop the Kubernetes dashboard"
assert_contains "Kubernetes dashboard state claim 'review-queue-state' cannot be read" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "an unreadable state claim must not fall back to a Podman session"
assert_file_not_contains "create secret generic review-session-" "$kubectl_log"
assert_file_not_contains "create -f -" "$kubectl_log"

begin "review-queue: Kubernetes Codex sessions explain unstaged subscription login"
reset_logs
run_recipe review-queue BLUEFIN_REVIEW_BACKEND=codex GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_RUNTIME=k8s
assert_zero_status "$STATUS" "the fake Kubernetes Codex dashboard session must succeed"
assert_contains "Kubernetes dashboard sessions do not stage a Codex subscription login" "$OUT"
assert_file_not_contains "CODEX_AUTH_MOUNT:" "$credential_log"

begin "review-queue: a declined lab starts no broker and mounts nothing"
reset_logs
install_fake_kubectl
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_LAB=0 REVIEW_LAB_BROKER="$lab_broker"
assert_no_lab_handoff
assert_not_contains "lab enabled for this session" "$OUT"

begin "review-queue: an accepted lab hands over one socket and nothing else"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_LAB=1 REVIEW_LAB_BROKER="$lab_broker" REVIEW_PERSONAL_SKILLS="$lab_skills"
assert_contains "lab enabled for this session (context ghost-lab)" "$OUT"
assert_file_contains ":/run/bluefin-review-lab:rw,z" "$runner_log"
assert_file_contains "--env BLUEFIN_REVIEW_LAB_SOCKET=/run/bluefin-review-lab/broker.sock" "$runner_log"
assert_file_contains "--env BLUEFIN_REVIEW_LAB_SESSION=" "$runner_log"
# The credential boundary: the container gets the socket, never the cluster.
assert_file_not_contains "kubeconfig" "$runner_log"
assert_file_not_contains ".kube" "$runner_log"
assert_file_not_contains "--network host" "$runner_log"
assert_file_not_contains "podman.sock" "$runner_log"
assert_file_not_contains "docker.sock" "$runner_log"
assert_file_not_contains "/var/run" "$runner_log"
assert_file_not_contains "$(command -v kubectl 2>/dev/null || echo /nonexistent-kubectl)" "$runner_log"
# Exactly one host socket crosses the boundary, and it is the broker's.
lab_socket_mounts="$(tr ' ' '\n' <"$runner_log" | grep -c '/run/bluefin-review-lab' || true)"
assert_eq "$lab_socket_mounts" 2 "expected exactly the socket mount and its env"
# Personal lab skills ride read-only, and only the ones that exist.
assert_file_contains "${lab_skills}/lab-test:/home/dev/.agents/skills/lab-test:ro,z" "$runner_log"
assert_file_contains "${lab_skills}/k3s-cluster-ops:/home/dev/.agents/skills/k3s-cluster-ops:ro,z" "$runner_log"
assert_file_not_contains "kubernetes-specialist" "$runner_log"
assert_file_not_contains "lab-testing" "$runner_log"

begin "review-queue: the broker and its socket die with the session"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_LAB=1 REVIEW_LAB_BROKER="$lab_broker" REVIEW_PERSONAL_SKILLS="$lab_skills"
socket_dir="$(tr ' ' '\n' <"$runner_log" | sed -n 's|^\(.*bluefin-review-lab\.[^:]*\):/run/bluefin-review-lab:rw,z$|\1|p' | head -1)"
[[ -n "$socket_dir" ]] || fail "the accepted lab must name its socket directory"
assert_file_not_exists "$socket_dir"
pgrep -f "review-lab-broker.py serve --socket ${socket_dir}" >/dev/null 2>&1 &&
  fail "the broker must not outlive the foreground session"

begin "review-queue: gVisor gets host-uds=open, other runtimes never do"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_LAB=1 REVIEW_LAB_BROKER="$lab_broker" FAKE_PODMAN_RUNTIME=runsc
assert_file_contains "--runtime-flag=host-uds=open" "$runner_log"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_LAB=1 REVIEW_LAB_BROKER="$lab_broker" FAKE_PODMAN_RUNTIME=crun
assert_file_contains "/run/bluefin-review-lab" "$runner_log"
assert_file_not_contains "host-uds" "$runner_log"

begin "review-container: the contributor worker receives no lab capability"
reset_logs
run_recipe review-container GH_READY=1 \
  REVIEW_LAB=1 REVIEW_LAB_BROKER="$lab_broker" REVIEW_PERSONAL_SKILLS="$lab_skills"
assert_no_lab_handoff
assert_file_not_contains "/home/dev/.agents/skills/lab-test" "$runner_log"
assert_not_contains "lab enabled for this session" "$OUT"

begin "review-container cluster: absent legacy annotation needs no removal"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token
assert_zero_status "$STATUS" "cluster scale-out must succeed without the legacy annotation"
assert_file_contains "get secret review-contributor-secret -n bluefin-system" "$kubectl_log"
assert_file_not_contains "annotate secret review-contributor-secret" "$kubectl_log"
assert_file_contains "--from-file=GH_TOKEN=" "$kubectl_log"
assert_file_contains "--from-file=GITHUB_COPILOT_TOKEN=" "$kubectl_log"
assert_file_not_contains "--from-literal=" "$kubectl_log"
assert_file_not_contains "gho-test-token" "$kubectl_log"
assert_file_not_contains "copilot-test-token" "$kubectl_log"

begin "review-container cluster: missing GitHub token leaves the Secret unchanged"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token
assert_nonzero_status "$STATUS" "cluster scale-out without a GitHub token must fail"
assert_contains "cluster Secret without a GitHub token" "$OUT"
assert_file_not_contains "create namespace bluefin-system" "$kubectl_log"
assert_file_not_contains "create secret generic review-contributor-secret" "$kubectl_log"

begin "review-container cluster: missing Copilot token leaves the Secret unchanged"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=
assert_nonzero_status "$STATUS" "cluster scale-out without a Copilot token must fail"
assert_contains "cluster Secret without a Copilot credential" "$OUT"
assert_file_not_contains "create namespace bluefin-system" "$kubectl_log"
assert_file_not_contains "create secret generic review-contributor-secret" "$kubectl_log"

begin "review-container cluster: annotation read errors stop deployment"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token \
  FAKE_KUBECTL_ANNOTATION_GET_FAIL=1
assert_nonzero_status "$STATUS" "a failed annotation read must fail cluster scale-out"
assert_contains "ERROR: failed to read secret annotations." "$OUT"
assert_file_contains "get secret review-contributor-secret -n bluefin-system -o jsonpath=" "$kubectl_log"
assert_file_not_contains "apply -f deploy/review-contributor.yaml" "$kubectl_log"

begin "review-container cluster: annotation removal errors stop deployment"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token \
  FAKE_KUBECTL_HAS_LAST_APPLIED=1 FAKE_KUBECTL_ANNOTATE_FAIL=1
assert_nonzero_status "$STATUS" "a failed annotation removal must fail cluster scale-out"
assert_contains "ERROR: failed to remove legacy plaintext secret annotation." "$OUT"
assert_file_contains "annotate secret review-contributor-secret -n bluefin-system" "$kubectl_log"
assert_file_not_contains "apply -f deploy/review-contributor.yaml" "$kubectl_log"

for failure_spec in \
  "namespace apply|FAKE_KUBECTL_NAMESPACE_APPLY_FAIL=1|create secret generic review-contributor-secret" \
  "Secret apply|FAKE_KUBECTL_SECRET_APPLY_FAIL=1|get secret review-contributor-secret" \
  "deployment apply|FAKE_KUBECTL_DEPLOY_APPLY_FAIL=1|set env deployment/review-contributor" \
  "deployment env update|FAKE_KUBECTL_SET_ENV_FAIL=1|scale deployment/review-contributor" \
  "deployment scale|FAKE_KUBECTL_SCALE_FAIL=1|rollout status deployment/review-contributor"; do
  IFS='|' read -r mutation_label failure_flag blocked_command <<<"$failure_spec"
  begin "turbo-review: ${mutation_label} failure aborts cluster mutation sequence"
  reset_logs
  RECIPE_ARGS=(--all)
  run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
    FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0 \
    "$failure_flag"
  assert_contains "cluster worker scale-out failed; continuing with local review dashboard" "$OUT"
  assert_file_not_contains "$blocked_command" "$kubectl_log"
  assert_file_contains "run --rm --interactive --tty --replace --name review-queue" "$runner_log"
done

begin "review-container cluster: rollout timeout warns after 15 seconds"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-container GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token FAKE_KUBECTL_ROLLOUT_FAIL=1
assert_zero_status "$STATUS" "rollout observation timeout must not fail cluster scale-out"
assert_file_contains "rollout status deployment/review-contributor -n bluefin-system --timeout=15s" "$kubectl_log"
assert_contains "! rollout still progressing after 15s; workers will continue pulling/starting in background." "$OUT"

begin "turbo-review: a leading profile configures cluster and dashboard"
reset_logs
RECIPE_ARGS=(sol)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0
assert_nonzero_status "$STATUS" "the fake dashboard runner always exits non-zero"
assert_file_contains "scale deployment/review-contributor -n bluefin-system --replicas=3" "$kubectl_log"
assert_file_contains "GOOSE_MODEL=gpt-5.6-sol" "$kubectl_log"
assert_file_contains "GOOSE_THINKING_EFFORT=medium" "$kubectl_log"
assert_file_contains "run --rm --interactive --tty --replace --name review-queue" "$runner_log"
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-sol" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"
assert_file_contains " queue" "$runner_log"
assert_contains "3/3 cluster contributor workers active in bluefin-system" "$OUT"
assert_contains "Stop workers: just review-stop cluster" "$OUT"
assert_contains "Check health: just review-doctor" "$OUT"

begin "turbo-review: explicit effort and dashboard flags stay intact"
reset_logs
RECIPE_ARGS=(k3 low --repo bluefin)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0
assert_file_contains "GOOSE_MODEL=kimi-k3" "$kubectl_log"
assert_file_contains "GOOSE_THINKING_EFFORT=low" "$kubectl_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=low" "$runner_log"
assert_file_contains "queue --repo bluefin" "$runner_log"

begin "turbo-review: a repository argument keeps the cluster default"
reset_logs
RECIPE_ARGS=(projectbluefin/review)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0
assert_file_contains "GOOSE_MODEL=gemini-3.8-flash" "$kubectl_log"
assert_file_contains "GOOSE_THINKING_EFFORT=max" "$kubectl_log"
assert_file_contains "queue --live-repo projectbluefin/review" "$runner_log"

begin "turbo-review: flags first keep the cluster default"
reset_logs
RECIPE_ARGS=(--repo bluefin)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0
assert_file_contains "GOOSE_MODEL=gemini-3.8-flash" "$kubectl_log"
assert_file_contains "GOOSE_THINKING_EFFORT=max" "$kubectl_log"
assert_file_contains "queue --repo bluefin" "$runner_log"

begin "turbo-review: dashboard inherits the hub resolved for cluster workers"
reset_logs
RECIPE_ARGS=(--all)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0 \
  FAKE_KUBECTL_REWRITE_HIVE_HUB=wss://changed.invalid/contribute
assert_file_contains "HIVE_HUB=wss://example.invalid/contribute" "$kubectl_log"
assert_file_contains "--env HIVE_HUB=wss://example.invalid/contribute" "$runner_log"
assert_file_not_contains "HIVE_HUB=wss://changed.invalid/contribute" "$runner_log"
sed -i 's|^HIVE_HUB=.*|HIVE_HUB=wss://example.invalid/contribute|' \
  "$home/.config/hive/contributor.env"

begin "turbo-review: failed exit status check is reported"
reset_logs
RECIPE_ARGS=(--all)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  FAKE_KEYRING_COPILOT_TOKEN=copilot-test-token REVIEW_LAB=0 \
  FAKE_KUBECTL_DEPLOYMENT_GET_FAIL=1
assert_contains "unable to read cluster contributor status in bluefin-system" "$OUT"
assert_contains "Stop workers: just review-stop cluster" "$OUT"
assert_contains "Check health: just review-doctor" "$OUT"

begin "turbo-review: missing kubectl warns and still launches the dashboard"
reset_logs
remove_fake_kubectl
RECIPE_ARGS=(--all)
run_recipe turbo-review GH_READY=1 FAKE_GH_TOKEN=gho-test-token REVIEW_LAB=0
assert_nonzero_status "$STATUS" "the fake dashboard runner always exits non-zero"
assert_contains "no active Kubernetes context found" "$OUT"
assert_contains "kubectl is unavailable; cluster contributor status was not checked" "$OUT"
assert_contains "Stop workers: just review-stop cluster" "$OUT"
assert_contains "Check health: just review-doctor" "$OUT"
assert_file_contains "run --rm --interactive --tty --replace --name review-queue" "$runner_log"
assert_file_contains "queue --all" "$runner_log"

begin "review-queue: explicit Codex selection reaches the shipped dashboard"
reset_logs
mv "$home/.config/goose/config.yaml" "$home/.config/goose/config.yaml.saved"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  BLUEFIN_REVIEW_BACKEND=codex
mv "$home/.config/goose/config.yaml.saved" "$home/.config/goose/config.yaml"
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_file_contains "--env BLUEFIN_REVIEW_BACKEND=codex" "$runner_log"
assert_file_not_contains "GOOSE_PROVIDER" "$runner_log"
assert_file_not_contains "GITHUB_COPILOT_TOKEN" "$runner_log"
assert_not_contains "Goose has no usable provider configuration" "$OUT"
assert_not_contains "Copilot credential" "$OUT"

begin "review-queue: an invalid review backend starts nothing"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  BLUEFIN_REVIEW_BACKEND=not-a-harness
assert_nonzero_status "$STATUS" "an invalid review backend must not launch"
assert_contains "unsupported review backend 'not-a-harness'" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "no container may start for an invalid review backend"

begin "review-queue: stages only an ephemeral Codex subscription login cache"
reset_logs
mkdir -p "$home/.codex"
printf '{"tokens":{"access_token":"codex-test-secret"}}\n' >"$home/.codex/auth.json"
chmod 0400 "$home/.codex/auth.json"
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  BLUEFIN_REVIEW_BACKEND=codex
codex_auth_mount="$(sed -n 's/^CODEX_AUTH_MOUNT://p' "$credential_log")"
assert_contains "/tmp/review-codex-auth." "$codex_auth_mount"
[[ "$codex_auth_mount" != "$home/.codex/auth.json" ]] || fail "host Codex auth must not be mounted directly"
assert_file_contains "CODEX_AUTH_DIRECT:no" "$credential_log"
assert_file_not_exists "$codex_auth_mount"
assert_file_contains "codex-test-secret" "$home/.codex/auth.json"
assert_file_not_contains "refreshed-test-secret" "$home/.codex/auth.json"
assert_file_not_contains "--volume ${home}/.codex:/home/dev/.codex" "$runner_log"
assert_file_not_contains "codex-test-secret" "$runner_log"
assert_not_contains "codex-test-secret" "$OUT"
rm -f "$home/.codex/auth.json"
rmdir "$home/.codex"

begin "review-queue: missing Codex login is explicit and mounts nothing"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  BLUEFIN_REVIEW_BACKEND=codex
assert_file_not_contains "/home/dev/.codex" "$runner_log"
assert_contains "Codex subscription login unavailable" "$OUT"

begin "review-queue: no GitHub token is one actionable error"
reset_logs
run_recipe review-queue GH_READY=1
assert_nonzero_status "$STATUS" "the dashboard without a token must not launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "cannot run without a token" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "no container may start without a token"

begin "review-queue: REVIEW_QUEUE_NAME scopes a second walk"
reset_logs
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token \
  REVIEW_QUEUE_NAME=review-queue-2
assert_file_contains "--name review-queue-2" "$runner_log"
assert_file_not_contains "--name review-queue " "$runner_log"

begin "review-queue: a leading profile and effort set the model, flags pass through"
reset_logs
RECIPE_ARGS=(k3 high --repo bluefin)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--env GOOSE_MODEL=kimi-k3" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=high" "$runner_log"
assert_file_contains "--env GOOSE_CONTEXT_LIMIT=264000" "$runner_log"
assert_file_contains "queue --repo bluefin" "$runner_log"
assert_file_not_contains "queue kimi" "$runner_log"

begin "review-queue: owner/repo is forwarded as the live repository"
reset_logs
RECIPE_ARGS=(acme/widgets)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "queue --live-repo acme/widgets" "$runner_log"

begin "review-queue: profile effort owner/repo preserves live grammar"
reset_logs
RECIPE_ARGS=(gpt-sol medium acme/widgets)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "queue --live-repo acme/widgets" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"

begin "review-queue: an unknown profile is one actionable error, nothing launches"
reset_logs
RECIPE_ARGS=(gpt-9)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_nonzero_status "$STATUS" "an unknown profile must not launch anything"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "unknown model profile 'gpt-9'" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "no container may start on a bad profile"

begin "review-queue: flags first means no profile, defaults to gemini at max effort"
reset_logs
RECIPE_ARGS=(--all)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--env GOOSE_MODEL=gemini-3.8-flash" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_contains "queue --all" "$runner_log"

begin "review-queue: explicit sol profile selects structured triage"
reset_logs
RECIPE_ARGS=(sol --all)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-sol" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"
assert_file_contains "queue --all" "$runner_log"

# ══ 3. Doctor: no failure on a fully provisioned host ═════════════════════
begin "review-doctor: fully provisioned host exits 0"
reset_logs
run_recipe review-doctor GH_READY=1 \
  FAKE_GH_TOKEN=gho-test-token FAKE_GH_SCOPES="'repo', 'read:org'" \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_zero_status "$STATUS" "a fully provisioned doctor run must exit 0"
assert_contains "a GitHub token is available for the container-only agent" "$OUT"
assert_contains "a Copilot credential is available" "$OUT"
assert_contains "0 failed." "$OUT"

# ══ 4. Container recipe ═══════════════════════════════════════════════════
begin "review-container: exactly one foreground podman run, hive mounts only"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_eq "$(wc -l <"$runner_log")" 1 "expected exactly one podman invocation"
assert_file_contains "run --rm --interactive --tty --replace --name review-container" "$runner_log"
# Only the selected registration is mounted. The directory mount it used to
# sit on top of made rootless Podman create a bogus host contributor.env when
# a named registration was selected (#143), and nothing in the image reads
# anything else from that directory.
assert_file_not_contains "--volume ${home}/.config/hive:/home/dev/.config/hive" "$runner_log"
assert_file_contains "--volume ${home}/.config/hive/contributor.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_contains "--env AGENT_BACKEND=goose" "$runner_log"
assert_file_contains "--env GOOSE_PROVIDER=github_copilot" "$runner_log"
assert_file_contains "--env GOOSE_MODEL=gpt-test" "$runner_log"
assert_file_contains "ghcr.io/projectbluefin/review" "$runner_log"
assert_file_not_contains " -d " "$runner_log"
assert_file_not_contains "--detach" "$runner_log"
assert_file_not_contains "--env-file" "$runner_log"
# The dashboard state mount belongs to review-queue alone; the contributor
# worker's record flows through Hive, so no bluefin-review state persists.
assert_file_not_contains "bluefin-review" "$runner_log"
assert_file_not_contains ":/config" "$runner_log"
# Check the container mount destination, not arbitrary source-path segments.
# A checkout may itself live under a directory named "workspace".
assert_file_not_contains ":/workspace" "$runner_log"
assert_file_not_contains "qemu" "$runner_log"
assert_file_not_contains "super-secret-registration-token" "$runner_log"

# A moving tag must be refreshed on every launch, or a contributor silently
# keeps running whatever copy they first pulled.
assert_file_contains "pull ghcr.io/projectbluefin/review:stable" "$image_log"

begin "contribute: launches the worker in the foreground"
reset_logs
run_recipe contribute GH_READY=1
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_file_contains "run --rm --interactive --tty --replace --name review-container" "$runner_log"
assert_file_not_contains "--detach" "$runner_log"

begin "review-container: REVIEW_DETACH=1 is rejected"
reset_logs
run_recipe review-container GH_READY=1 REVIEW_DETACH=1
assert_nonzero_status "$STATUS" "detached launches must fail"
assert_contains "detached contributor containers are not supported" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "a detached launch must not reach Podman"

begin "review-container: missing jq does not affect a local connection"
reset_logs
run_recipe review-container GH_READY=1 FAKE_JQ_UNAVAILABLE=1
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_file_contains "run --rm --interactive --tty --replace --name review-container" "$runner_log"

begin "review-container: a remote Podman engine receives the selected Hive registration"
reset_logs
remote_canonical_dir="$fake_remote_root/home/dev/.config/hive"
mkdir -p "$remote_canonical_dir"
printf 'HIVE_REGISTRATION_TOKEN=remote-canonical-token-preserve-me\n' >"$remote_canonical_dir/contributor.env"
chmod 0755 "$remote_canonical_dir"
chmod 0600 "$remote_canonical_dir/contributor.env"

run_recipe review-container GH_READY=1 \
  'FAKE_PODMAN_CONNECTIONS=[{"Name":"engine","URI":"ssh://dev@engine:2222/run/user/1000/podman/podman.sock","Identity":"/fake/key","Default":true,"ReadWrite":true}]'
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
# Canonical remote registration must be preserved: neither overwritten nor deleted!
assert_file_exists "$remote_canonical_dir/contributor.env"
assert_file_contains "remote-canonical-token-preserve-me" "$remote_canonical_dir/contributor.env"
assert_eq "755" "$(stat -c '%a' "$remote_canonical_dir")" "canonical remote directory mode must be preserved"
# It must NOT target canonical .config/hive
assert_file_not_contains "mkdir -p \"\$HOME/.config/hive\"" "$remote_log"
assert_file_not_contains "chmod 0700 \"\$HOME/.config/hive\"" "$remote_log"
assert_file_contains "ssh -o BatchMode=yes -i /fake/key -p 2222 dev@engine" "$remote_log"
assert_file_contains "mktemp -d /tmp/review-hive-registration.XXXXXX" "$remote_log"
assert_file_contains "scp -o BatchMode=yes -i /fake/key -P 2222 -p ${home}/.config/hive/contributor.env dev@engine:/tmp/review-hive-registration.a1b2c3/contributor.env" "$remote_log"
assert_file_contains "--volume /tmp/review-hive-registration.a1b2c3/contributor.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_contains "Hive contributor registration staged on remote Podman engine (0600, removed on exit; endpoint and secret not shown)." "$OUT"
assert_not_contains "dev@engine" "$OUT"
assert_not_contains "super-secret-registration-token" "$OUT"
assert_file_contains "ssh -o BatchMode=yes -i /fake/key -p 2222 dev@engine chmod 0600 /tmp/review-hive-registration.a1b2c3/contributor.env" "$remote_log"
assert_file_contains "rm -f -- /tmp/review-hive-registration.a1b2c3/contributor.env; rmdir -- /tmp/review-hive-registration.a1b2c3" "$remote_log"
assert_file_not_exists "$fake_remote_root/tmp/review-hive-registration.a1b2c3"

begin "review-container: explicitly selected remote engine stages Hive registration"
reset_logs
run_recipe review-container GH_READY=1 CONTAINER_CONNECTION=remote \
  'FAKE_PODMAN_CONNECTIONS=[{"Name":"local","URI":"unix:///run/user/1000/podman/podman.sock","Identity":"","Default":true},{"Name":"remote","URI":"ssh://dev@engine:2222/run/user/1000/podman/podman.sock","Identity":"/fake/key","Default":false}]'
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_file_contains "ssh -o BatchMode=yes -i /fake/key -p 2222 dev@engine" "$remote_log"
assert_file_contains "--volume /tmp/review-hive-registration.a1b2c3/contributor.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_not_exists "$fake_remote_root/tmp/review-hive-registration.a1b2c3"

begin "review-container: remote Podman cleans up private staging directory when scp fails"
reset_logs
remote_canonical_dir="$fake_remote_root/home/dev/.config/hive"
mkdir -p "$remote_canonical_dir"
printf 'HIVE_REGISTRATION_TOKEN=remote-canonical-token-preserve-me\n' >"$remote_canonical_dir/contributor.env"
chmod 0755 "$remote_canonical_dir"
chmod 0600 "$remote_canonical_dir/contributor.env"

run_recipe review-container GH_READY=1 FAKE_SCP_FAIL=1 \
  'FAKE_PODMAN_CONNECTIONS=[{"Name":"engine","URI":"ssh://dev@engine:2222/run/user/1000/podman/podman.sock","Identity":"/fake/key","Default":true,"ReadWrite":true}]'
assert_nonzero_status "$STATUS" "scp failure must fail the recipe"
assert_file_exists "$remote_canonical_dir/contributor.env"
assert_file_contains "remote-canonical-token-preserve-me" "$remote_canonical_dir/contributor.env"
assert_file_not_exists "$fake_remote_root/tmp/review-hive-registration.a1b2c3"

begin "review-container: remote Podman cleans up private staging directory when chmod fails"
reset_logs
remote_canonical_dir="$fake_remote_root/home/dev/.config/hive"
mkdir -p "$remote_canonical_dir"
printf 'HIVE_REGISTRATION_TOKEN=remote-canonical-token-preserve-me\n' >"$remote_canonical_dir/contributor.env"
chmod 0755 "$remote_canonical_dir"
chmod 0600 "$remote_canonical_dir/contributor.env"

run_recipe review-container GH_READY=1 FAKE_SSH_CHMOD_FAIL=1 \
  'FAKE_PODMAN_CONNECTIONS=[{"Name":"engine","URI":"ssh://dev@engine:2222/run/user/1000/podman/podman.sock","Identity":"/fake/key","Default":true,"ReadWrite":true}]'
assert_nonzero_status "$STATUS" "chmod failure must fail the recipe"
assert_file_exists "$remote_canonical_dir/contributor.env"
assert_file_contains "remote-canonical-token-preserve-me" "$remote_canonical_dir/contributor.env"
assert_file_not_exists "$fake_remote_root/tmp/review-hive-registration.a1b2c3"

begin "review-container: failed foreground Codex launch removes staged auth"
reset_logs
mkdir -p "$home/.codex"
printf '{"tokens":{"access_token":"codex-test-secret"}}\n' >"$home/.codex/auth.json"
chmod 0400 "$home/.codex/auth.json"
run_recipe review-container GH_READY=1 TOOL=codex
codex_auth_mount="$(sed -n 's/^CODEX_AUTH_MOUNT://p' "$credential_log")"
assert_nonzero_status "$STATUS" "failed foreground launch must fail"
assert_file_not_exists "$codex_auth_mount"
rm -f "$home/.codex/auth.json"
rmdir "$home/.codex"

begin "review-stop: stops cluster workers by default"
reset_logs
install_fake_kubectl
run_recipe review-stop
assert_zero_status "$STATUS" "stopping cluster workers must succeed"
assert_file_contains "scale deployment/review-contributor -n bluefin-system --replicas=0" "$kubectl_log"
assert_contains "stopped all cluster contributor workers (scaled to 0 in bluefin-system)." "$OUT"

begin "review-stop: stops cluster workers when explicitly named"
reset_logs
RECIPE_ARGS=(cluster)
run_recipe review-stop
assert_zero_status "$STATUS" "stopping cluster workers must succeed"
assert_file_contains "scale deployment/review-contributor -n bluefin-system --replicas=0" "$kubectl_log"
assert_contains "stopped all cluster contributor workers (scaled to 0 in bluefin-system)." "$OUT"
remove_fake_kubectl

begin "review-stop: refuses an attended run and names Ctrl-C"
reset_logs
RECIPE_ARGS=(review-container)
run_recipe review-stop FAKE_PODMAN_RUNNING=1 \
  FAKE_PODMAN_OWNER_LABEL="boot-id:12345"
assert_nonzero_status "$STATUS" "an attended run is not review-stop's to end"
assert_contains "Ctrl-C" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "review-stop must not stop an attended run"

begin "review-stop: an absent container is a clean no-op"
reset_logs
RECIPE_ARGS=(review-container)
run_recipe review-stop
assert_zero_status "$STATUS" "nothing to stop is success, not an error"
assert_contains "no container named review-container is running" "$OUT"

begin "review-stop: refuses a container not started by this launcher"
reset_logs
RECIPE_ARGS=(foreign-container)
run_recipe review-stop FAKE_PODMAN_RUNNING=1
assert_nonzero_status "$STATUS" "foreign container must be refused"
assert_contains "foreign-container was not started by this launcher" "$OUT"

begin "hive selection: the current repository's registration wins when it exists"
reset_logs
default_hive_backup="$scratch/contributor.default.env"
cp "$home/.config/hive/contributor.env" "$default_hive_backup"
rm "$home/.config/hive/contributor.env"
# The launcher derives the registration name from the checkout's directory
# basename (git rev-parse --show-toplevel), so this scenario must too: a
# worktree named anything but 'review' selects contributor.<basename>.env.
repo_registration="${repo_root##*/}"
cat >"$home/.config/hive/contributor.${repo_registration}.env" <<'EOF'
HIVE_REGISTRATION_TOKEN=named-secret-token
HIVE_HUB=wss://named-hive.invalid/contribute
CONTRIBUTOR_ID=test-contributor-named
CONTRIBUTOR_USERNAME=test-user
AGENT_BACKEND=goose
EOF
chmod 600 "$home/.config/hive/contributor.${repo_registration}.env"
named_hive_hash="$(sha256sum "$home/.config/hive/contributor.${repo_registration}.env")"
named_hive_mode="$(stat -c '%a' "$home/.config/hive/contributor.${repo_registration}.env")"
named_hive_uid="$(stat -c '%u' "$home/.config/hive/contributor.${repo_registration}.env")"
named_hive_gid="$(stat -c '%g' "$home/.config/hive/contributor.${repo_registration}.env")"
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.${repo_registration}.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_not_exists "$home/.config/hive/contributor.env"
assert_eq "$(sha256sum "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_hash" "selected Hive registration content changed during launch construction"
assert_eq "$(stat -c '%a' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_mode" "selected Hive registration mode changed during launch construction"
assert_eq "$(stat -c '%u' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_uid" "selected Hive registration uid changed during launch construction"
assert_eq "$(stat -c '%g' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_gid" "selected Hive registration gid changed during launch construction"
# The named launch must not require, create, or mutate the default (#143).
assert_file_not_contains "--volume ${home}/.config/hive:/home/dev/.config/hive" "$runner_log"
assert_contains "hive: wss://named-hive.invalid/contribute (registration '${repo_registration}')" "$OUT"
assert_not_contains "super-secret-registration-token" "$OUT"
assert_not_contains "named-secret-token" "$OUT"
assert_file_not_contains "named-secret-token" "$runner_log"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_CONTAINER_NAME=review-container-2
assert_file_contains "--replace --name review-container-2 " "$runner_log"
assert_file_contains "--volume ${home}/.config/hive/contributor.${repo_registration}.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_not_exists "$home/.config/hive/contributor.env"
assert_eq "$(sha256sum "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_hash" "selected Hive registration content changed during concurrent launch construction"
assert_eq "$(stat -c '%a' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_mode" "selected Hive registration mode changed during concurrent launch construction"
assert_eq "$(stat -c '%u' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_uid" "selected Hive registration uid changed during concurrent launch construction"
assert_eq "$(stat -c '%g' "$home/.config/hive/contributor.${repo_registration}.env")" "$named_hive_gid" "selected Hive registration gid changed during concurrent launch construction"
rm -f "$home/.config/hive/contributor.${repo_registration}.env"
cp "$default_hive_backup" "$home/.config/hive/contributor.env"

begin "hive selection: no repo registration falls back to the default and says so"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_contains "hive: wss://example.invalid/contribute (default registration)" "$OUT"
assert_contains "REVIEW_HIVE=${repo_root##*/}" "$OUT"

begin "hive selection: fallback guidance names a checkout not called review"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  FAKE_GIT_TOPLEVEL=/home/maintainer/checkouts/not-review
assert_contains "hive: wss://example.invalid/contribute (default registration)" "$OUT"
assert_contains "REVIEW_HIVE=not-review" "$OUT"
assert_not_contains "REVIEW_HIVE=review " "$OUT"

begin "hive selection: REVIEW_HIVE overrides the repository-derived name"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.otherhive.env"
sed -i 's|wss://example.invalid/contribute|wss://other-hive.invalid/contribute|' \
  "$home/.config/hive/contributor.otherhive.env"
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_HIVE=otherhive
assert_file_contains "--volume ${home}/.config/hive/contributor.otherhive.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_contains "hive: wss://other-hive.invalid/contribute (registration 'otherhive')" "$OUT"
rm -f "$home/.config/hive/contributor.otherhive.env"

begin "hive selection: an invalid REVIEW_HIVE is one actionable error"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_HIVE='bad;name'
assert_nonzero_status "$STATUS" "an invalid REVIEW_HIVE must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "REVIEW_HIVE='bad;name' is not a valid registration name" "$OUT"

begin "hive selection: an unregistered REVIEW_HIVE names the fix when unattended"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_HIVE=unregistered
assert_nonzero_status "$STATUS" "an unregistered REVIEW_HIVE cannot register without a terminal"
assert_contains "no hive registration named 'unregistered'" "$OUT"
assert_contains "REVIEW_HIVE=unregistered just review-container" "$OUT"
assert_file_not_exists "$home/.config/hive/contributor.unregistered.env"

begin "review-container: the Copilot credential is passed, never a gh token"
# Without this the agent starts a fresh device flow on every launch and the
# pane sits on "enter code XXXX-XXXX" until a human types one in.
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  GITHUB_COPILOT_TOKEN=ghu-test-token
assert_contains "Copilot credential passed" "$OUT"
assert_file_contains "--env GITHUB_COPILOT_TOKEN" "$runner_log"
assert_file_not_contains "GITHUB_COPILOT_TOKEN=ghu-test-token" "$runner_log"
assert_file_contains "GITHUB_COPILOT_TOKEN:present" "$credential_log"

begin "review-container: the credential is read from the login keyring when unexported"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_contains "Copilot credential passed" "$OUT"
assert_file_contains "--env GITHUB_COPILOT_TOKEN" "$runner_log"
assert_file_not_contains "GITHUB_COPILOT_TOKEN=ghu-keyring-token" "$runner_log"
assert_file_contains "GITHUB_COPILOT_TOKEN:present" "$credential_log"
assert_not_contains "ghu-keyring-token" "$OUT"

begin "review-container: no credential refuses the launch and names the fix"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  FAKE_KEYRING_COPILOT_TOKEN=
assert_nonzero_status "$STATUS" "a credential-less contributor launch must refuse"
assert_contains "no Copilot credential found" "$OUT"
assert_contains "Provider is not configured" "$OUT"
assert_contains "gh auth token' is NOT a substitute" "$OUT"
assert_contains "goose configure" "$OUT"
assert_file_not_contains "GITHUB_COPILOT_TOKEN=" "$runner_log"
# Refusal means refusal: no contributor container may start only to have
# every claimed Hive task die on an unconfigured provider.
assert_file_not_contains "run" "$runner_log"

begin "review-container: a GitHub identity is inherited, never mounted"
# Without GH_TOKEN the agent picks up a task, runs gh, is told to 'gh auth
# login' (which the Hive wrapper blocks in contributor mode) and stops.
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  FAKE_GH_TOKEN=gho-test-token
assert_contains "GitHub identity passed to the agent" "$OUT"
assert_file_contains "--env GH_TOKEN" "$runner_log"
assert_file_not_contains "GH_TOKEN=gho-test-token" "$runner_log"
assert_file_contains "GH_TOKEN:present" "$credential_log"
# By inherited environment only: ~/.config/gh must never be mounted, and the
# value must never reach this terminal.
assert_file_not_contains ".config/gh" "$runner_log"
assert_not_contains "gho-test-token" "$OUT"

begin "review-container: the blast radius is named, the token is not"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  FAKE_GH_TOKEN=gho-test-token FAKE_GH_SCOPES="'admin:org', 'repo', 'workflow'"
assert_contains "admin:org" "$OUT"
assert_contains "REVIEW_GH_TOKEN" "$OUT"
assert_not_contains "gho-test-token" "$OUT"

begin "review-container: an explicit scoped PAT beats the desktop login"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o \
  FAKE_GH_TOKEN=gho-desktop-token REVIEW_GH_TOKEN=gho-scoped-pat
assert_file_contains "--env GH_TOKEN" "$runner_log"
assert_file_not_contains "GH_TOKEN=gho-scoped-pat" "$runner_log"
assert_file_contains "GH_TOKEN:present" "$credential_log"
assert_file_not_contains "gho-desktop-token" "$runner_log"

begin "review-container: no GitHub token says so plainly and names the fix"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o
assert_contains "no GitHub token found" "$OUT"
assert_contains "gh auth login" "$OUT"
assert_file_not_contains "GH_TOKEN=" "$runner_log"

begin "review-container: an unobtainable image is one actionable error"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_IMAGE_MISSING=1
assert_nonzero_status "$STATUS" "an unobtainable contributor image must fail the run"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR line"
assert_contains "cannot obtain the contributor image" "$OUT"
assert_contains "there is no ':latest'" "$OUT"
assert_file_contains "pull" "$image_log"
assert_eq "$(wc -c <"$runner_log")" 0 "no container may start when the image is unobtainable"

# ══ 8. Stop recipe ════════════════════════════════════════════════════════
begin "review-container: an immutable reference is not re-pulled"
# sha- tags and digests name exactly one image, so refreshing them is wasted
# work on every launch; only moving tags need the pull.
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  REVIEW_CONTRIBUTOR_IMAGE=ghcr.io/projectbluefin/review:sha-deadbeef
assert_file_contains "image exists" "$image_log"
assert_file_not_contains "pull" "$image_log"

begin "review-container: an orphaned run is reclaimed without a second command"
# Still running, but its terminal is gone: nobody can reach or Ctrl-C it, so
# the launch takes the name back instead of demanding manual cleanup.
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_RUNNING=1
assert_contains "reclaiming" "$OUT"
assert_not_contains "ERROR:" "$OUT"
assert_not_contains "podman rm -f" "$OUT"
assert_file_contains "--replace --name review-container" "$runner_log"

# ── ownership marking ─────────────────────────────────────────────────────
# '--rm --interactive --tty' does not bind the container's lifetime to the
# client: conmon supervises the container and outlives it, so a hard-killed
# terminal (or a hand-typed 'podman start'/'podman restart') leaves a fully
# RUNNING container with no client and no terminal. These scenarios pin the
# classification that tells that apart from a live session.
boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"

begin "review-container: every launch records an ownership marker"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--label review.owner=${boot_id}:" "$runner_log"

begin "review-container: a marked run with a live owner is never replaced"
# A process whose command line names the container stands in for the owning
# foreground 'podman run' client.
reset_logs
# The trap stops bash from exec-replacing itself with 'sleep', which would
# drop the '--name review-container' argv this scenario depends on.
bash -c 'trap "exit 0" TERM; sleep 30' --name review-container &
owner_pid=$!
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_RUNNING=1 \
  "FAKE_PODMAN_OWNER_LABEL=${boot_id}:${owner_pid}"
kill "$owner_pid" 2>/dev/null || true
wait "$owner_pid" 2>/dev/null || true
assert_nonzero_status "$STATUS" "a marked, live owner must stop the relaunch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR line"
assert_contains "is already running in another terminal" "$OUT"
assert_contains "tmux attach -t contributor" "$OUT"
assert_not_contains "podman rm -f" "$OUT"
assert_contains "pid ${owner_pid}" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "a live session must never be replaced"

begin "review-container: a marked run whose owner is gone is reclaimed"
# The reproduced incident: RestartPolicy=no, AutoRemove=true, container Up,
# and no owning client process anywhere. 'podman restart' lands here too --
# it resurrects the container with the original creation label, whose PID is
# long dead -- so the launcher reclaims instead of refusing.
reset_logs
dead_owner="$(bash -c 'echo $$')"
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_RUNNING=1 \
  "FAKE_PODMAN_OWNER_LABEL=${boot_id}:${dead_owner}"
assert_contains "reclaiming" "$OUT"
assert_not_contains "ERROR:" "$OUT"
assert_file_contains "--replace --name review-container" "$runner_log"

begin "review-container: a marker from a previous boot is never trusted"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_RUNNING=1 \
  "FAKE_PODMAN_OWNER_LABEL=00000000-0000-0000-0000-000000000000:1"
assert_contains "reclaiming" "$OUT"
assert_not_contains "ERROR:" "$OUT"

begin "review-container: an unmarked running container is an orphan, never a live session"
# The user-facing half of the incident: an ownerless container answered with
# 'press Ctrl-C in the terminal that owns it' when no such terminal existed.
# The launcher holds no state and stamps an owner label on every launch, so an
# unmarked container cannot have survived this boot with an owner.
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test \
  FAKE_PODMAN_RUNNING=1
assert_contains "reclaiming" "$OUT"
assert_not_contains "press Ctrl-C in the terminal that owns it." "$OUT"

# ── concurrent instances ──────────────────────────────────────────────────
# One name can only be held by one agent, so a second concurrent contributor
# asks for a name of its own. That is the whole feature: one validated
# environment override, no instance registry and no state.

begin "review-container: the default name is unchanged when the override is unset"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--replace --name review-container " "$runner_log"
assert_contains "podman exec -it review-container tmux attach" "$OUT"

begin "review-container: REVIEW_CONTAINER_NAME runs a second, differently-named instance"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  REVIEW_CONTAINER_NAME=review-container-2
assert_eq "$(wc -l <"$runner_log")" 1 "expected exactly one podman invocation"
assert_file_contains "--replace --name review-container-2 " "$runner_log"
assert_file_contains "--label review.owner=${boot_id}:" "$runner_log"
assert_file_not_contains "--detach" "$runner_log"
# Every hint has to name the container the user actually started, or a second
# agent is told to attach to the first one's session.
assert_contains "podman exec -it review-container-2 tmux attach" "$OUT"

begin "review-container: an invalid REVIEW_CONTAINER_NAME is one actionable error"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  'REVIEW_CONTAINER_NAME=-bad name; rm -rf /'
assert_nonzero_status "$STATUS" "an invalid container name must stop the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "is not a valid container name" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "an invalid name must never reach podman"

begin "review-container: orphan reclaim is per-name"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  REVIEW_CONTAINER_NAME=review-container-2 \
  FAKE_PODMAN_RUNNING=1
assert_contains "reclaiming review-container-2" "$OUT"
assert_not_contains "ERROR:" "$OUT"
assert_file_contains "--replace --name review-container-2 " "$runner_log"

begin "review-container: a named instance with a live owner is never replaced"
# The ownership marker is confirmed against the owner's own '/proc' cmdline,
# so the confirmation has to follow the custom name too.
reset_logs
bash -c 'trap "exit 0" TERM; sleep 30' --name review-container-2 &
named_owner_pid=$!
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  REVIEW_CONTAINER_NAME=review-container-2 \
  FAKE_PODMAN_RUNNING=1 \
  "FAKE_PODMAN_OWNER_LABEL=${boot_id}:${named_owner_pid}"
kill "$named_owner_pid" 2>/dev/null || true
wait "$named_owner_pid" 2>/dev/null || true
assert_nonzero_status "$STATUS" "a marked, live owner must stop the relaunch"
assert_contains "review-container-2 is already running in another terminal" "$OUT"
assert_contains "podman exec -it review-container-2 tmux attach" "$OUT"
assert_not_contains "podman exec -it review-container tmux attach" "$OUT"
assert_contains "pid ${named_owner_pid}" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "a live session must never be replaced"

# ══ Doctor is read-only ═══════════════════════════════════════════════════
begin "review-doctor: read-only, Goose-only diagnostics"
reset_logs
run_recipe review-doctor GH_READY=1
assert_contains "Agent backend (Goose)" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "Agent backend (Codex)" "$OUT"

assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: rejects an unsupported provider"
reset_logs
run_recipe review-doctor GH_READY=1 GOOSE_PROVIDER=ollama
assert_nonzero_status "$STATUS" "an unsupported provider must fail doctor"
assert_contains "GOOSE_PROVIDER=ollama is not supported" "$OUT"
begin "review-doctor: reports a usable Copilot credential without printing it"
reset_logs
run_recipe review-doctor GH_READY=1 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_contains "Copilot credential" "$OUT"
assert_contains "a Copilot credential is available" "$OUT"
assert_not_contains "ghu-keyring-token" "$OUT"

assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: a missing Copilot credential is a failed check with the fix"
reset_logs
run_recipe review-doctor GH_READY=1 FAKE_KEYRING_COPILOT_TOKEN=
assert_nonzero_status "$STATUS" "a missing Copilot credential must fail the doctor"
assert_contains "no Copilot credential is available" "$OUT"
assert_contains "gh auth token' is NOT a substitute" "$OUT"
assert_contains "goose configure" "$OUT"

begin "review-doctor: a stale AGENT_BACKEND is a warning, and the file is left alone"
# Harmless (the launcher passes AGENT_BACKEND=goose itself) but misleading to
# anyone who reads contributor.env, so it is reported, never rewritten.
reset_logs
backend_backup="$scratch/contributor.env.bak"
cp "$home/.config/hive/contributor.env" "$backend_backup"
sed -i 's/^AGENT_BACKEND=.*/AGENT_BACKEND=copilot/' "$home/.config/hive/contributor.env"
run_recipe review-doctor GH_READY=1 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_contains "AGENT_BACKEND=copilot" "$OUT"
assert_contains "selected backend is goose" "$OUT"
assert_contains "will not rewrite Hive's saved backend selection" "$OUT"
assert_file_contains "AGENT_BACKEND=copilot" "$home/.config/hive/contributor.env"
cp "$backend_backup" "$home/.config/hive/contributor.env"

begin "review-doctor: a matching AGENT_BACKEND raises no warning"
reset_logs
run_recipe review-doctor GH_READY=1 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_not_contains "selected backend is" "$OUT"

begin "review-doctor: reports the agent's GitHub token and its scopes, not its value"
reset_logs
run_recipe review-doctor GH_READY=1 \
  FAKE_GH_TOKEN=gho-test-token FAKE_GH_SCOPES="'admin:org', 'repo'"
assert_contains "a GitHub token is available for the container-only agent" "$OUT"
assert_contains "admin:org" "$OUT"
assert_not_contains "gho-test-token" "$OUT"

assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: a missing GitHub token is a failed check with the fix"
reset_logs
run_recipe review-doctor GH_READY=1
assert_nonzero_status "$STATUS" "a missing GitHub token must fail the doctor"
assert_contains "no GitHub token is available for the container-only agent" "$OUT"
assert_contains "REVIEW_GH_TOKEN" "$OUT"

# ══ 5/6. Static guarantees read straight off the justfile ════════════════
begin "static: an interactive launch can never background the container"
# Comments in this file legitimately discuss --detach/nohup/setsid, so they
# are stripped before any of these greps run.
# Only whole-line comments are stripped, deliberately. A trailing '# --detach'
# on a code line is still scanned and would fail this test, which is a false
# positive — but the alternative is worse: the justfile contains '#' inside
# quoted strings and inside ${...} expansions, and no line-level rule can tell
# those apart from a comment. An over-eager strip would silently truncate a
# real launch line and turn a false positive into a hole in the guarantee.
# Move the comment to its own line instead.
code="$scratch/justfile-code"
sed -E 's/^[[:space:]]*#.*$//' "$justfile" >"$code"

# Every launch is attached to the invoking terminal.
assert_eq "$(grep -cE 'podman run --rm --detach --replace --name' "$code")" 0 \
  "no launch may detach"
if grep -nE 'podman run' "$code" | grep -vE -- '--interactive --tty'; then
  fail "every podman run must be interactive"
fi
# A lone trailing '&' backgrounds the launch; '&&' and '2>&1' must not match.
if grep -nE '(podman run).*[^&>]&[[:space:]]*$' "$code"; then
  fail "a launch line must never end in a background '&'"
fi
if grep -nE '(^|[^[:alnum:]_])(nohup|setsid)([^[:alnum:]_]|$)' "$code"; then
  fail "nohup/setsid must never appear on a launch path"
fi
assert_eq "$(grep -cE 'podman run --rm --interactive --tty' "$code")" 2 \
  "expected exactly two foreground podman run sites (contributor container and queue walk)"
# A stale container from a hard-killed terminal must never block a relaunch.
assert_eq "$(grep -cE 'podman run --rm --interactive --tty --replace --name' "$code")" 2 \
  "every named foreground run must reclaim its name with --replace"
begin "static: a launch cannot detach through an option form or a second line"
# The greps above read one physical line at a time and only recognise a
# space-delimited '-d'/'--detach' sitting on the same line as 'podman run'.
# The container launch is actually built as a multi-line CONTAINER_ARGS array, so
# '--detach' on a continuation line of the array — or '-itd', '--detach=true',
# or a '\'-continued launch — would sail straight past them. Rebuild the
# scan around the argument region instead of the single launch line.
#
# Line continuations are joined first so a launch split with '\' is scanned
# as the one command it becomes.
joined="$scratch/justfile-code-joined"
sed -e :a -e '/\\$/N; s/\\\n//; ta' "$code" >"$joined"
# Everything that contributes arguments to a real launch: both podman
# argument arrays (opened as CONTAINER_ARGS=( and appended to with +=), and
# any bare 'podman run'/'podman create'.
launch_args="$scratch/justfile-launch-args"
awk '
  /CONTAINER_ARGS\+?=\(/           { inargs = 1 }
  inargs                           { print; if ($0 ~ /\)[[:space:]]*$/) inargs = 0; next }
  /podman[[:space:]]+(run|create)/ { print }
' "$joined" >"$launch_args"
# 'podman run --detach-keys' is a foreground detach *sequence*, not
# backgrounding, so the character after '--detach' has to be checked.
if grep -nE -- '--detach([^-]|$)' "$launch_args"; then
  fail "no other launch argument may detach the run (--detach/--detach=true)"
fi
if grep -nE -- '(^|[[:space:]])-d([[:space:]=]|$)' "$launch_args"; then
  fail "no launch argument may detach the run (-d/-d=true)"
fi
# '-itd' and '-dit' bundle the detach flag into the short-flag cluster the
# foreground launches already use. Only clusters built from podman's own
# bundleable short flags are matched, so the shell's '-rf'/'-euo' cannot
# trip this.
if grep -nE -- '(^|[[:space:]])-([aditq]+d[aditq]*|d[aditq]+)([[:space:]]|$)' "$launch_args"; then
  fail "no launch argument may bundle the detach flag into a short-flag cluster"
fi
# A background '&' anywhere in the launch region, not only at end of line:
# 'podman run ... & wait' backgrounds the launch just as effectively while
# still ending the line in 'wait'. '&&', '&>' and '2>&1' must not match.
if grep -nE '[^&>]&([^&>]|$)' "$launch_args"; then
  fail "a launch must never be backgrounded with '&'"
fi

begin "static: the launcher cannot daemonize through a second command"
# Every one of these hands the run to something that outlives the terminal
# without ever writing '-d' on a 'podman run' line. 'podman create' is the
# subtlest: it never detaches by itself, but it exists only to be handed to
# 'podman start', which does.
if grep -nE 'podman[[:space:]]+(create|start|restart)([[:space:]]|$)' "$joined"; then
  fail "podman create/start/restart would resurrect a run outside its terminal"
fi
if grep -nE '(^|[^[:alnum:]_-])(systemd-run|disown|daemonize)([^[:alnum:]_-]|$)' "$joined"; then
  fail "systemd-run/disown/daemonize must never appear on a launch path"
fi
if grep -nE '(screen[[:space:]]+-[A-Za-z]*d|tmux[[:space:]]+new(-session)?[[:space:]]+.*-[A-Za-z]*d)' "$joined"; then
  fail "a detached screen/tmux session is a daemon wearing a multiplexer"
fi
# 'at'/'batch' only in command position: the scheduler runs the job under a
# daemon, detached from this terminal by construction.
if grep -nE '(^|[;&|])[[:space:]]*(at|batch)[[:space:]]+' "$joined"; then
  fail "a launch must never be handed to the at/batch scheduler"
fi

begin "static: the launcher ships no systemd unit, quadlet or otherwise"
# A quadlet unit ('.container', '.kube', '.pod', '.volume', '.network',
# '.build') is a systemd service in disguise: podman-system-generator turns
# it into a unit, and the run then belongs to systemd rather than to the
# terminal. It would bypass every regex above, because none of the words
# those match ever appear. So the check is the absence of the file, plus the
# absence of any reference that could install or start one.
tracked_units="$(git -C "$repo_root" ls-files \
  '*.container' '*.kube' '*.pod' '*.volume' '*.network' '*.build' 2>/dev/null || true)"
[[ -z "$tracked_units" ]] ||
  fail "this repository must ship no quadlet unit: $tracked_units"
if grep -nE '(quadlet|containers/systemd|systemctl|systemd-analyze)' "$joined"; then
  fail "the launcher must never install, generate or drive a systemd unit"
fi
begin "static: the container never defaults to an unpublished ':latest' tag"
# publish-compat-image.yml only pushes sha-<commit>, the version tags and
# 'stable', so a ':latest' default is guaranteed 'manifest unknown'.
if grep -n 'review:latest' "$code"; then
  fail "the default contributor image must be a tag the publish workflow actually pushes"
fi
grep -q 'ghcr.io/projectbluefin/review:stable' "$code" ||
  fail "the default contributor image must be the published ':stable' tag"

begin "static: the lifecycle verb stops cluster workers and refuses attended runs"
# review-stop stops cluster contributor workers. It refuses attended runs
# (Ctrl-C owns those), refuses containers this launcher did not label, and
# never force-removes anything.
grep -qE '^review-stop' "$code" ||
  fail "review-stop must exist as the cluster workers' lifecycle verb"
stop_body="$(sed -n '/^review-stop/,/^[a-z]/p' "$code")"
grep -q 'stop_cluster_contributors' <<<"$stop_body" ||
  fail "review-stop must stop cluster contributors"
grep -q 'review.owner' <<<"$stop_body" ||
  fail "review-stop must check the owner label before touching anything"
grep -q 'Ctrl-C' <<<"$stop_body" ||
  fail "review-stop must route attended runs back to Ctrl-C"
if grep -nE 'podman (rm|kill)|--force|stop -f' <<<"$stop_body"; then
  fail "review-stop must stop politely, never force-remove"
fi
if grep -nE 'review\.owner=detached' "$code"; then
  fail "stale detached owner label found in justfile code"
fi
if grep -nE '^review-(start|restart|kill|clean|down|up)[ :]' "$code"; then
  fail "no resurrection or force verbs: stop is the only lifecycle command"
fi
# The recipe list is exactly: launch foreground or unattended contributors,
# stop a detached worker, diagnose, walk the PR queue, and scale workers.
grep -qE '^turbo-review[ :]' "$code" ||
  fail "turbo-review must exist as the worker scale-out plus dashboard recipe"
assert_eq "$(grep -cE '^(contribute|review[a-z-]*|turbo-review)[ :]' "$code")" 6 \
  "expected exactly six recipes (contribute, review-container, -stop, -doctor, -queue, turbo-review)"

begin "static: upstream contribute-setup runs with upstream's own version-check opt-out"
# Our Hive checkout is a pinned detached SHA on purpose. Upstream's private
# 'check-version' recipe is a prerequisite of 'contribute-setup' and aborts
# whenever HEAD != origin/v4, telling the user to
# "export HIVE_SKIP_VERSION_CHECK=true". Without that flag, first-run
# onboarding is guaranteed to fail the moment v4 moves past the pin.
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q 'HIVE_SKIP_VERSION_CHECK=true just --working-directory "\$HIVE_SRC_DIR"' "$code" ||
  fail "upstream contribute-setup must run with HIVE_SKIP_VERSION_CHECK=true"
if grep -nE '^[[:space:]]*export HIVE_SKIP_VERSION_CHECK' "$code"; then
  fail "the version-check opt-out must be scoped to the one upstream invocation"
fi
# The pin itself stays load-bearing: no branch name may be executed.
grep -q 'must be a full 40-character commit SHA' "$code" ||
  fail "the Hive checkout must remain pinned to a full commit SHA"
begin "static: no legacy backends survive in the launcher"
for legacy in copilot_live_models 'Multiple AI CLIs' LAST_TOOL AGENT_MODEL=; do
  if grep -Fn -- "$legacy" "$code"; then
    fail "legacy backend leftover found: $legacy"
  fi
done
# One backend means one dispatch: a 'case' over tool names would be the first
# step back to multi-CLI detection.
if grep -nE '^(tool_order|tool_installed|tool_authenticated|tool_fixit_hint|tool_install_hint)' "$code"; then
  fail "the launcher must not reintroduce a per-tool dispatch table"
fi

begin "static: ownership is proven from the label, never guessed"
# A 'pgrep' for a 'podman run' command line cannot tell a live session from a
# container conmon outlived, which is the whole reason the owner label exists.
if grep -n 'pgrep' "$code"; then
  fail "container ownership must come from the owner label, not a pgrep heuristic"
fi

begin "static: Codex cleanup requires invoking ownership and private modes"
cleanup_body="$(sed -n '/^cleanup_codex_auth_staging_dir()/,/^}/p' "$code")"
grep -Fq "stat -c %u \"\$staging_dir\"" <<<"$cleanup_body" ||
  fail "Codex cleanup must inspect staging-directory ownership"
grep -Fq "stat -c %a \"\$staging_dir\"" <<<"$cleanup_body" ||
  fail "Codex cleanup must inspect staging-directory mode"
grep -Fq "stat -c %u \"\$staging_dir/auth.json\"" <<<"$cleanup_body" ||
  fail "Codex cleanup must inspect auth-file ownership"
grep -Fq "stat -c %a \"\$staging_dir/auth.json\"" <<<"$cleanup_body" ||
  fail "Codex cleanup must inspect auth-file mode"
grep -Fq 'id -u' <<<"$cleanup_body" ||
  fail "Codex cleanup must compare ownership with the invoking UID"

begin "static: cluster scale-out validates Hive before mutation and scrubs secret metadata"
cluster_body="$(sed -n '/^scale_cluster_contributors()/,/^stop_cluster_contributors()/p' "$code")"
hub_guard_line="$(grep -nF "if ! valid_hive_hub \"\$hub\"; then" <<<"$cluster_body" | cut -d: -f1)"
namespace_line="$(grep -nF 'kubectl create namespace bluefin-system' <<<"$cluster_body" | cut -d: -f1)"
if [[ -z "$hub_guard_line" || -z "$namespace_line" || "$hub_guard_line" -ge "$namespace_line" ]]; then
  fail "cluster scale-out must validate HIVE_HUB before its first cluster mutation"
fi
grep -Fq "echo \"ERROR: HIVE_HUB is not set in \${HIVE_CONTRIBUTOR_ENV}.\" >&2" <<<"$cluster_body" ||
  fail "cluster scale-out must report the selected Hive registration when HIVE_HUB is invalid"
grep -Fq 'kubectl annotate secret review-contributor-secret -n bluefin-system' <<<"$cluster_body" ||
  fail "cluster scale-out must remove stale client-side apply metadata from the Secret"
grep -Fq 'kubectl.kubernetes.io/last-applied-configuration-' <<<"$cluster_body" ||
  fail "cluster scale-out must remove the last-applied-configuration annotation"
grep -Fq -- '--from-file=GH_TOKEN=' <<<"$cluster_body" ||
  fail "cluster scale-out must feed token values via file descriptors"
grep -Fq -- '--from-file=GITHUB_COPILOT_TOKEN=' <<<"$cluster_body" ||
  fail "cluster scale-out must feed copilot token via file descriptors"
if grep -Fq -- '--from-literal=' <<<"$cluster_body"; then
  fail "cluster scale-out must not place token values in kubectl arguments"
fi
grep -Fq -- '--timeout=15s' <<<"$cluster_body" ||
  fail "cluster scale-out must cap rollout observation at 15 seconds"
if grep -q '^[[:space:]]*- name: HIVE_HUB$' "$repo_root/deploy/review-contributor.yaml"; then
  fail "the deployment manifest must leave HIVE_HUB to the launcher"
fi
if ! grep -A1 '^          image: ghcr.io/projectbluefin/review:stable$' \
  "$repo_root/deploy/review-contributor.yaml" |
  grep -Fxq '          imagePullPolicy: Always'; then
  fail "the stable contributor deployment must always pull the published image"
fi

begin "static: remote Hive staging never alters canonical paths and validates cleanup"
stage_func="$(sed -n '/^stage_hive_registration_for_remote_podman()/,/^}/p' "$code")"
cleanup_func="$(sed -n '/^cleanup_remote_hive_registration()/,/^}/p' "$code")"
# shellcheck disable=SC2016 # the single-quoted $HOME is the literal being searched for
if grep -q '\$HOME/\.config/hive' <<<"$stage_func"; then
  fail "remote Hive staging must not target remote canonical \$HOME/.config/hive"
fi
grep -q 'mktemp -d /tmp/review-hive-registration' <<<"$stage_func" ||
  fail "remote Hive staging must use a unique private directory under /tmp/review-hive-registration.XXXXXX"
grep -q 'review-hive-registration' <<<"$cleanup_func" ||
  fail "remote Hive cleanup must validate the private staging path before removal"
if grep -nE 'rm -rf|rm -r' <<<"$cleanup_func"; then
  fail "remote Hive cleanup must not use broad recursive deletion"
fi
grep -q 'rmdir' <<<"$cleanup_func" ||
  fail "remote Hive cleanup must use rmdir for the private directory"

begin "static: turbo-review initializes models and forwards arguments through positional parameters"
turbo_body="$(sed -n '/^turbo-review \*args:/,/^# Preflight check:/p' "$code")"
for assignment in \
  'TOOL="{{tool_env}}"' \
  'GEMINI_MODEL="{{gemini_model}}"' \
  'OPUS_MODEL="{{opus_model}}"' \
  'OPUS_CONTEXT_LIMIT="{{opus_context_limit}}"' \
  'SOL_MODEL="{{sol_model}}"' \
  'K3_MODEL="{{k3_model}}"' \
  'K3_CONTEXT_LIMIT="{{k3_context_limit}}"'; do
  grep -Fq "$assignment" <<<"$turbo_body" ||
    fail "turbo-review must initialize ${assignment%%=*}"
done
grep -Fq 'set -- {{args}}' <<<"$turbo_body" ||
  fail "turbo-review must establish positional arguments before parsing"
grep -Fq 'just review-queue "$@"' <<<"$turbo_body" ||
  fail "turbo-review must forward dashboard arguments through the positional array"
grep -Fq "export HIVE_HUB=\"\$CLUSTER_HIVE_HUB\"" <<<"$turbo_body" ||
  fail "turbo-review must export the cluster-resolved Hive hub to review-queue"
if grep -Fq 'just review-queue {{args}}' <<<"$turbo_body"; then
  fail "turbo-review must not render arguments directly into the review-queue command"
fi

begin "static: nothing here filters the work Hive assigns"
# Hive's selectTask is the sole authority on what gets worked on: the hub's
# config decides the repository pool, the deny-lists and the cooldown. Filter
# any of that here and this repository shadows a lifecycle Hive owns, diverges
# from the hub's admission policy the moment either side changes, and silently
# hides work the maintainers deliberately admitted.
#
# Filtering needs one of two footholds: speaking the contributor protocol, or
# gating on issue metadata. Both are checked, on the launcher and on the
# entrypoint that hands over to Hive. Comments in both legitimately discuss
# task selection, so they are stripped first, exactly as above.
entry_code="$scratch/entrypoint-code"
policy_code="$scratch/policy-text"
sed -E 's/^[[:space:]]*#.*$//' "$repo_root/image/entrypoint.sh" >"$entry_code"
sed -E 's/^[[:space:]]*#.*$//' "$repo_root/image/config/local-agent-policy.md" >"$policy_code"

# Declining an assignment means answering Hive's contributor protocol, so the
# message names may not appear here at all. The relay is Hive's too.
if grep -nEi '(task_assignment|task_completed|task_failed|request_task|contributor-relay)' \
  "$code" "$entry_code" "$policy_code"; then
  fail "nothing here may speak Hive's contributor protocol — selection is Hive's alone"
fi
# The other foothold: an allow/deny list or a conditional keyed on the
# repository, label, title, author or issue number of an assignment.
if grep -nEi \
  '(allow|deny|skip|exclude|ignore|block|reject|decline|only)[_-](repo|repos|issue|issues|label|labels|title|titles|author|authors|task|tasks)|(repo|repos|issue|issues|label|labels|title|author|task|tasks)[_-](filter|filters|allowlist|denylist|whitelist|blacklist|pattern|patterns|mode)|(allow|deny|white|black)list|clanker-queue' \
  "$code" "$entry_code" "$policy_code"; then
  fail "nothing here may filter Hive-selected work by repo, label, title, author or issue"
fi
# An env passthrough is the quietest way to smuggle a selector into the guest.
if grep -nE '[A-Z0-9_]*(FILTER|ALLOWLIST|DENYLIST|WHITELIST|BLACKLIST)[A-Z0-9_]*' \
  "$code" "$entry_code"; then
  fail "no selector may be passed into the guest through the environment"
fi
# Positive control: the handover stays a bare launch of Hive's own agent. A
# wrapper, pipe or redirection around it is precisely where an interception
# filter would land, and would not trip the greps above.
# shellcheck disable=SC2016 # the entrypoint source is matched literally, not expanded
grep -q '^/usr/local/bin/contributor-agent.sh "\$@" &$' "$entry_code" ||
  fail "the entrypoint must hand straight over to Hive's contributor-agent.sh, unwrapped"
grep -Fq '/opt/bluefin/tui/worker_status.py' "$entry_code" ||
  fail "the attended contributor path must launch the passive worker-status companion"
grep -Fq 'status_pid=' "$entry_code" ||
  fail "the companion must have explicit PID-1 cleanup ownership"
grep -Fq 'tmux attach -t contributor' "$repo_root/image/tui/worker_status.py" ||
  fail "the companion must show Hive's named contributor tmux attach command"

# ══ result ════════════════════════════════════════════════════════════════
if [[ "$failures" -gt 0 ]]; then
  printf '\n%d assertion(s) FAILED.\n' "$failures" >&2
  exit 1
fi
printf '\nAll review onboarding assertions passed.\n'
