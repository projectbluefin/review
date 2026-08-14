#!/usr/bin/env bash
# Hermetic regression harness for the root justfile.
#
# Everything the launcher can shell out to (gh, goose, gum, podman, git,
# secret-tool) is faked on PATH, so this test never touches the network,
# never starts a real container, and never
# depends on what happens to be installed on the developer's machine.
#
# Host preflight remains Goose/Pi-specific. Codex discovery runs inside the
# maintainer image, with only its subscription login cache handed through.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

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
home="$scratch/home"
cfg_dir="$home/.config/review"
state_dir="$home/.local/state/review"
gum_log="$scratch/gum.log"
runner_log="$scratch/runner.log"
image_log="$scratch/image.log"
credential_log="$scratch/credentials.log"

default_hive_backup=""
cleanup() {
  if [[ -n "$default_hive_backup" && -f "$default_hive_backup" ]]; then
    cp "$default_hive_backup" "$home/.config/hive/contributor.env"
  fi
  rm -rf "$scratch" "$tmp_root"
}
trap cleanup EXIT

mkdir -p "$fake_bin" "$tmp_root" \
  "$home/.config/goose" "$home/.config/hive" "$cfg_dir" "$state_dir"

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
# launcher's repo-derived hive registration name can be exercised. Everything
# else stays hermetic.
if [[ "${1:-}" == "rev-parse" && "${2:-}" == "--show-toplevel" ]]; then
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
  image | manifest | pull)
    printf '%s\n' "$*" >>"${IMAGE_LOG:?}"
    [[ "${FAKE_PODMAN_IMAGE_MISSING:-0}" == 1 ]] && exit 1
    exit 0
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
    printf '%s\n' "$*" >>"${IMAGE_LOG:?}"
    case "$*" in
      *review.owner*)
        # FAKE_PODMAN_OWNER_LABEL is the raw marker the launcher would have
        # written: '<boot-id>:<pid>'. Empty means an unmarked container, which
        # can only ever be an orphan.
        printf '%s\n' "${FAKE_PODMAN_OWNER_LABEL:-}"
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
    *) shift ;;
  esac
done
exit 97
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
      -u GITHUB_COPILOT_TOKEN -u FAKE_KEYRING_COPILOT_TOKEN \
      -u GH_TOKEN -u GITHUB_TOKEN \
      -u REVIEW_GH_TOKEN -u FAKE_GH_TOKEN -u FAKE_GH_SCOPES \
      -u CODEX_HOME \
      -u BLUEFIN_REVIEW_BACKEND \
      -u GOOSE_THINKING_EFFORT -u GOOSE_CONTEXT_LIMIT \
      -u REVIEW_NON_INTERACTIVE -u GOOSE_INSTALLED \
      -u REVIEW_CONTAINER_NAME -u REVIEW_DETACH \
      -u REVIEW_QUEUE_NAME \
      HOME="$home" PATH="$fake_bin:/usr/bin:/bin" TMPDIR="$tmp_root" \
      XDG_RUNTIME_DIR="$tmp_root" \
      GUM_LOG="$gum_log" RUNNER_LOG="$runner_log" \
      IMAGE_LOG="$image_log" \
      CREDENTIAL_LOG="$credential_log" \
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

# ══ 2. TOOL handling: Goose only ══════════════════════════════════════════
begin "TOOL=claude is rejected with a Goose-only error"
run_recipe review-container GH_READY=1 TOOL=claude
assert_nonzero_status "$STATUS" "a non-Goose TOOL must be a hard error"
assert_contains "TOOL=claude is not supported" "$OUT"
assert_contains "review supports Goose and Pi" "$OUT"
assert_not_contains "auto-detected" "$OUT"
assert_not_contains "Multiple AI CLIs" "$OUT"

begin "TOOL=goose is accepted"
# A passing TOOL check reaches the fake runner, which always exits non-zero.
run_recipe review-container GH_READY=1 TOOL=goose
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_not_contains "is not supported" "$OUT"
assert_not_contains "Unset TOOL" "$OUT"

begin "TOOL=pi is accepted only with its executable backend credential"
rm -f "$home/.config/goose/config.yaml"
run_recipe review-container GH_READY=1 TOOL=pi PI_API_KEY=pi-test-key
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_not_contains "is not supported" "$OUT"
assert_file_contains "--env AGENT_BACKEND=pi" "$runner_log"
assert_file_contains "--env ANTHROPIC_API_KEY" "$runner_log"
assert_file_not_contains "pi-test-key" "$runner_log"
write_goose_config

begin "TOOL=pi without its credential is rejected before container launch"
reset_logs
run_recipe review-container GH_READY=1 TOOL=pi
assert_nonzero_status "$STATUS" "Pi without a credential must fail preflight"
assert_contains "Pi requires PI_API_KEY" "$OUT"
assert_file_not_contains "run --rm" "$runner_log"

begin "selection: default Copilot model is noninteractive"
reset_logs
run_recipe review-container GH_READY=1
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_file_contains "--env GOOSE_PROVIDER=github_copilot" "$runner_log"
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-luna" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_not_exists "$cfg_dir/last-selections.env"
assert_file_not_exists "$cfg_dir/secrets.env"
assert_eq "$(wc -c <"$gum_log")" 0 "gum must not be invoked"

begin "review-container: thinking-effort overrides are passed through"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test \
  GOOSE_THINKING_EFFORT=medium
assert_file_contains "--env GOOSE_THINKING_EFFORT=medium" "$runner_log"

begin "review-container: no profile is luna at max with the provider's own context"
reset_logs
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-luna" "$runner_log"
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

begin "review-container: the kimi profile is max effort with a clamped context"
reset_logs
RECIPE_ARGS=(kimi)
run_recipe review-container GH_READY=1
assert_file_contains "--env GOOSE_MODEL=kimi-k3" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"
assert_file_contains "--env GOOSE_CONTEXT_LIMIT=264000" "$runner_log"

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

begin "review-container: an unknown thinking effort is one actionable error"
reset_logs
RECIPE_ARGS=(luna ludicrous)
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
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-luna" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=high" "$runner_log"

begin "review-container: maintainer backend choice never changes Hive selection"
reset_logs
run_recipe review-container GH_READY=1 BLUEFIN_REVIEW_BACKEND=codex
assert_file_contains "--env AGENT_BACKEND=goose" "$runner_log"
assert_file_not_contains "BLUEFIN_REVIEW_BACKEND" "$runner_log"

# ══ 2b. Dashboard: no Hive, GH_TOKEN required, args pass through ═════════
begin "review-queue: launches the dashboard with no Hive config at all"
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
assert_file_contains "GH_TOKEN:present" "$credential_log"
assert_not_contains "contributor.env" "$OUT"
assert_file_not_contains "/home/dev/.codex/auth.json" "$runner_log"
assert_contains "starting the maintainer review dashboard (no Hive)" "$OUT"

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
assert_contains "${tmp_root}/" "$codex_auth_mount"
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
RECIPE_ARGS=(kimi high --repo bluefin)
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
RECIPE_ARGS=(luna max acme/widgets)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "queue --live-repo acme/widgets" "$runner_log"
assert_file_contains "--env GOOSE_THINKING_EFFORT=max" "$runner_log"

begin "review-queue: an unknown profile is one actionable error, nothing launches"
reset_logs
RECIPE_ARGS=(gpt-9)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_nonzero_status "$STATUS" "an unknown profile must not launch anything"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "unknown model profile 'gpt-9'" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "no container may start on a bad profile"

begin "review-queue: flags first means no profile, everything passes through"
reset_logs
RECIPE_ARGS=(--all)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
assert_file_contains "--env GOOSE_MODEL=gpt-5.6-luna" "$runner_log"
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
assert_file_not_contains ":/config" "$runner_log"
assert_file_not_contains "/workspace" "$runner_log"
assert_file_not_contains "qemu" "$runner_log"
assert_file_not_contains "super-secret-registration-token" "$runner_log"

# A moving tag must be refreshed on every launch, or a contributor silently
# keeps running whatever copy they first pulled.
assert_file_contains "pull ghcr.io/projectbluefin/review:stable" "$image_log"

begin "review-container: REVIEW_DETACH=1 launches the marked worker"
reset_logs
run_recipe review-container GH_READY=1 REVIEW_DETACH=1
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_file_contains "run --rm --detach --replace --name review-container" "$runner_log"
assert_file_contains "--label review.owner=detached" "$runner_log"
assert_file_not_contains "--interactive" "$runner_log"
assert_file_not_contains "--tty" "$runner_log"
assert_contains "just review-stop review-container" "$OUT"
assert_contains "podman logs -f review-container" "$OUT"

begin "review-container: a running detached worker is never reclaimed"
reset_logs
run_recipe review-container GH_READY=1 \
  FAKE_PODMAN_RUNNING=1 FAKE_PODMAN_OWNER_LABEL=detached
assert_nonzero_status "$STATUS" "a live detached worker must refuse a second launch"
assert_contains "already running as a detached worker" "$OUT"
assert_contains "just review-stop review-container" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "nothing may launch over a live detached worker"

begin "review-stop: stops a detached worker politely"
reset_logs
run_recipe review-stop FAKE_PODMAN_RUNNING=1 FAKE_PODMAN_OWNER_LABEL=detached
assert_zero_status "$STATUS" "stopping a detached worker must succeed"
assert_file_contains "stop review-container" "$runner_log"
assert_contains "stopped the detached worker" "$OUT"

begin "review-stop: refuses an attended run and names Ctrl-C"
reset_logs
run_recipe review-stop FAKE_PODMAN_RUNNING=1 \
  FAKE_PODMAN_OWNER_LABEL="boot-id:12345"
assert_nonzero_status "$STATUS" "an attended run is not review-stop's to end"
assert_contains "Ctrl-C" "$OUT"
assert_eq "$(wc -c <"$runner_log")" 0 "review-stop must not stop an attended run"

begin "review-stop: an absent container is a clean no-op"
reset_logs
run_recipe review-stop
assert_zero_status "$STATUS" "nothing to stop is success, not an error"
assert_contains "no container named review-container" "$OUT"

begin "hive selection: the current repository's registration wins when it exists"
reset_logs
default_hive_backup="$scratch/contributor.default.env"
cp "$home/.config/hive/contributor.env" "$default_hive_backup"
rm "$home/.config/hive/contributor.env"
cat >"$home/.config/hive/contributor.review.env" <<'EOF'
HIVE_REGISTRATION_TOKEN=named-secret-token
HIVE_HUB=wss://named-hive.invalid/contribute
CONTRIBUTOR_ID=test-contributor-named
CONTRIBUTOR_USERNAME=test-user
AGENT_BACKEND=goose
EOF
chmod 600 "$home/.config/hive/contributor.review.env"
named_hive_hash="$(sha256sum "$home/.config/hive/contributor.review.env")"
named_hive_mode="$(stat -c '%a' "$home/.config/hive/contributor.review.env")"
named_hive_uid="$(stat -c '%u' "$home/.config/hive/contributor.review.env")"
named_hive_gid="$(stat -c '%g' "$home/.config/hive/contributor.review.env")"
# The tests run with the review repository as cwd, so the repo-derived
# registration name is 'review'.
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.review.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_not_exists "$home/.config/hive/contributor.env"
assert_eq "$(sha256sum "$home/.config/hive/contributor.review.env")" "$named_hive_hash" "selected Hive registration content changed during launch construction"
assert_eq "$(stat -c '%a' "$home/.config/hive/contributor.review.env")" "$named_hive_mode" "selected Hive registration mode changed during launch construction"
assert_eq "$(stat -c '%u' "$home/.config/hive/contributor.review.env")" "$named_hive_uid" "selected Hive registration uid changed during launch construction"
assert_eq "$(stat -c '%g' "$home/.config/hive/contributor.review.env")" "$named_hive_gid" "selected Hive registration gid changed during launch construction"
# The named launch must not require, create, or mutate the default (#143).
assert_file_not_contains "--volume ${home}/.config/hive:/home/dev/.config/hive" "$runner_log"
assert_contains "hive: wss://named-hive.invalid/contribute (registration 'review')" "$OUT"
assert_not_contains "super-secret-registration-token" "$OUT"
assert_not_contains "named-secret-token" "$OUT"
assert_file_not_contains "named-secret-token" "$runner_log"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_CONTAINER_NAME=review-container-2
assert_file_contains "--replace --name review-container-2 " "$runner_log"
assert_file_contains "--volume ${home}/.config/hive/contributor.review.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_file_not_exists "$home/.config/hive/contributor.env"
assert_eq "$(sha256sum "$home/.config/hive/contributor.review.env")" "$named_hive_hash" "selected Hive registration content changed during concurrent launch construction"
assert_eq "$(stat -c '%a' "$home/.config/hive/contributor.review.env")" "$named_hive_mode" "selected Hive registration mode changed during concurrent launch construction"
assert_eq "$(stat -c '%u' "$home/.config/hive/contributor.review.env")" "$named_hive_uid" "selected Hive registration uid changed during concurrent launch construction"
assert_eq "$(stat -c '%g' "$home/.config/hive/contributor.review.env")" "$named_hive_gid" "selected Hive registration gid changed during concurrent launch construction"
rm -f "$home/.config/hive/contributor.review.env"
cp "$default_hive_backup" "$home/.config/hive/contributor.env"

begin "hive selection: no repo registration falls back to the default and says so"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.env:/home/dev/.config/hive/contributor.env:ro,z" "$runner_log"
assert_contains "hive: wss://example.invalid/contribute (default registration)" "$OUT"
assert_contains "REVIEW_HIVE=review" "$OUT"

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

begin "review-container: no credential says so plainly and names the fix"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-4o
assert_contains "no Copilot credential found" "$OUT"
assert_contains "gh auth token' is NOT a substitute" "$OUT"
assert_contains "goose configure" "$OUT"
assert_file_not_contains "GITHUB_COPILOT_TOKEN=" "$runner_log"

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

begin "review-doctor: Pi diagnostics appear only when Pi is selected"
reset_logs
run_recipe review-doctor GH_READY=1 TOOL=pi PI_API_KEY=pi-test-key
assert_contains "Agent backend (Pi)" "$OUT"
assert_contains "pi: selected" "$OUT"
assert_not_contains "Copilot credential" "$OUT"
assert_not_contains "Agent backend (Goose)" "$OUT"
assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: a saved Pi backend matches selected Pi"
reset_logs
backend_backup="$scratch/contributor.env.pi-bak"
cp "$home/.config/hive/contributor.env" "$backend_backup"
sed -i 's/^AGENT_BACKEND=.*/AGENT_BACKEND=pi/' "$home/.config/hive/contributor.env"
run_recipe review-doctor GH_READY=1 TOOL=pi PI_API_KEY=pi-test-key
assert_not_contains "selected backend is" "$OUT"
cp "$backend_backup" "$home/.config/hive/contributor.env"

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
run_recipe review-doctor GH_READY=1
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

# Exactly one sanctioned detach site exists: the deliberate worker launch,
# which pairs --detach with the 'detached' owner label so a later launch
# refuses to reclaim it and review-stop can stop it. Any other detach is a
# hole.
assert_eq "$(grep -c 'podman run --rm --detach --replace --name' "$code")" 1 \
  "expected exactly one detached launch site (the marked worker)"
assert_eq "$(grep -c 'review.owner=detached' "$code")" 1 \
  "the detached label is stamped at exactly one launch site"
assert_eq "$(grep -c '"detached"' "$code")" 2 \
  "both the ownership check and review-stop must honor the detached marker"
if grep -nE 'podman run' "$code" | grep -vE -- '--detach|--interactive --tty'; then
  fail "every podman run is either the marked detached worker or interactive"
fi
# A lone trailing '&' backgrounds the launch; '&&' and '2>&1' must not match.
if grep -nE '(podman run).*[^&>]&[[:space:]]*$' "$code"; then
  fail "a launch line must never end in a background '&'"
fi
if grep -nE '(^|[^[:alnum:]_])(nohup|setsid)([^[:alnum:]_]|$)' "$code"; then
  fail "nohup/setsid must never appear on a launch path"
fi
assert_eq "$(grep -c 'podman run --rm --interactive --tty' "$code")" 2 \
  "expected exactly two foreground podman run sites (contributor container and queue walk)"
# A stale container from a hard-killed terminal must never block a relaunch.
assert_eq "$(grep -c 'podman run --rm --interactive --tty --replace --name' "$code")" 2 \
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
# any bare 'podman run'/'podman create'. The one sanctioned detach line —
# the marked worker launch asserted above — is excluded so everything else
# stays under the strict scan.
launch_args="$scratch/justfile-launch-args"
awk '
  /CONTAINER_ARGS\+?=\(/           { inargs = 1 }
  inargs                           { print; if ($0 ~ /\)[[:space:]]*$/) inargs = 0; next }
  /podman[[:space:]]+(run|create)/ { print }
' "$joined" | grep -v 'podman run --rm --detach --replace --name' >"$launch_args"
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

begin "static: the lifecycle verb is scoped to detached workers"
# review-stop exists for exactly one thing: stopping a deliberately detached
# worker. It must refuse attended runs (Ctrl-C owns those), refuse containers
# this launcher did not label, and never force anything.
grep -qE '^review-stop' "$code" ||
  fail "review-stop must exist as the detached worker's lifecycle verb"
stop_body="$(sed -n '/^review-stop/,/^[a-z]/p' "$code")"
grep -q 'review.owner' <<<"$stop_body" ||
  fail "review-stop must check the owner label before touching anything"
grep -q 'Ctrl-C' <<<"$stop_body" ||
  fail "review-stop must route attended runs back to Ctrl-C"
if grep -nE 'podman (rm|kill)|--force|stop -f' <<<"$stop_body"; then
  fail "review-stop must stop politely, never force-remove"
fi
if grep -nE '^review-(start|restart|kill|clean|down|up)[ :]' "$code"; then
  fail "no resurrection or force verbs: stop is the only lifecycle command"
fi
# The recipe list is exactly: launch the container, stop a detached worker,
# diagnose, walk the PR queue.
assert_eq "$(grep -cE '^review[a-z-]*[ :]' "$code")" 4 \
  "expected exactly four recipes (review-container, -stop, -doctor, -queue)"

begin "static: upstream contribute-setup runs with upstream's own version-check opt-out"
# Our Hive checkout is a pinned detached SHA on purpose. Upstream's private
# 'check-version' recipe is a prerequisite of 'contribute-setup' and aborts
# whenever HEAD != origin/v2, telling the user to
# "export HIVE_SKIP_VERSION_CHECK=true". Without that flag, first-run
# onboarding is guaranteed to fail the moment v2 moves past the pin.
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

# ══ result ════════════════════════════════════════════════════════════════
if [[ "$failures" -gt 0 ]]; then
  printf '\n%d assertion(s) FAILED.\n' "$failures" >&2
  exit 1
fi
printf '\nAll review onboarding assertions passed.\n'
