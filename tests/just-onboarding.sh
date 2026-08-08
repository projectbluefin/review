#!/usr/bin/env bash
# Hermetic regression harness for the root justfile.
#
# Everything the launcher can shell out to (gh, goose, gum, podman, qemu,
# qemu-img, curl, find, brew, uname) is faked on PATH, so this test
# never touches the network, never starts a real VM or container, and never
# depends on what happens to be installed on the developer's machine.
#
# The launcher under test is Goose-only: there is no claude/copilot/codex
# detection and no multi-CLI picker. Assertions here reflect that.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# REVIEW_TEST_JUSTFILE exists so the harness itself can be negative-
# tested against a deliberately broken copy of the launcher.
justfile="${REVIEW_TEST_JUSTFILE:-$repo_root/justfile}"
consumer="$repo_root/tests/guest-bootstrap-consumer.py"
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
firmware_dir="$scratch/firmware"
gum_log="$scratch/gum.log"
runner_log="$scratch/runner.log"
image_log="$scratch/image.log"
qemu_log="$scratch/qemu.log"
qemu_img_log="$scratch/qemu-img.log"
curl_log="$scratch/curl.log"
find_log="$scratch/find.log"
credential_log="$scratch/credentials.log"
consumed_marker="$scratch/runner-consumed"

trap 'rm -rf "$scratch" "$tmp_root"' EXIT

mkdir -p "$fake_bin" "$tmp_root" "$firmware_dir" \
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
# brew is faked so vm_firmware's search roots can never resolve to a real
# Homebrew prefix on the developer's machine.
cat >"$fake_bin/brew" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_BREW_PREFIX:?}"
EOF
# secret-tool is faked so the harness can never read the developer's real login
# keyring, and so a scenario can say whether a Copilot credential exists.
cat >"$fake_bin/secret-tool" <<'EOF'
#!/usr/bin/env bash
[[ -n "${FAKE_KEYRING_COPILOT_TOKEN:-}" ]] || exit 1
printf '{"GITHUB_COPILOT_TOKEN":"%s","OTHER":"ignored"}\n' "$FAKE_KEYRING_COPILOT_TOKEN"
EOF
cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
output=""
url=""
head_request=0
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    -I|--head|-fsIL) head_request=1; shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s -> %s\n' "$url" "$output" >> "${CURL_LOG:?}"
if [[ "$head_request" == 1 ]]; then
  [[ "${TEST_CURL_MODE:-}" == "release-missing" ]] && exit 22
  exit 0
fi
if [[ "${TEST_CURL_MODE:-}" == "checksum-fail" && "$url" == *.sha256 ]]; then
  printf 'partial checksum\n' >"$output"
  exit 98
fi
if [[ -n "${TEST_CURL_MODE:-}" ]]; then
  if [[ "$url" == *.sha256 ]]; then
    checksum="$(printf 'downloaded VM\n' | /usr/bin/sha256sum | awk '{print $1}')"
    printf '%s  %s\n' "$checksum" "$(basename "${url%.sha256}")" >"$output"
  else
    printf 'downloaded VM\n' >"$output"
  fi
  exit 0
fi
echo "unexpected raw VM fetch" >&2
exit 98
EOF
cat >"$fake_bin/zstd" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
input=""
output=""
while (($#)); do
  case "$1" in
    -d|--force) shift ;;
    -o) output="$2"; shift 2 ;;
    *) input="$1"; shift ;;
  esac
done
cp "$input" "$output"
EOF
cat >"$fake_bin/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" && "${2:-}" == *'server=socket.socket'* ]]; then
  case "${REVIEW_TEST_BOOTSTRAP_MODE:-}" in
    delayed) sleep 0.2 ;;
    exit)
      echo "simulated bootstrap bind failure" >&2
      exit 87
      ;;
  esac
fi
exec /usr/bin/python3 "$@"
EOF
# The launcher's vm_firmware() searches several roots for a fixed list of
# firmware names. This fake answers for every supported name itself and never
# falls through to the real find for them, so a developer who genuinely has
# /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd cannot change the
# result. FAKE_FIRMWARE_MODE selects a split pflash pair or a single blob.
cat >"$fake_bin/find" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FIND_LOG:?}"
answer() {
  [[ -f "${FAKE_FIRMWARE_DIR:?}/$1" ]] && printf '%s\n' "${FAKE_FIRMWARE_DIR}/$1"
  exit 0
}
case " $* " in
  *" edk2-x86_64-code.fd "*|*" edk2-aarch64-code.fd "*)
    [[ "${FAKE_FIRMWARE_MODE:-pflash}" == "pflash" ]] || exit 0
    case " $* " in
      *" edk2-x86_64-code.fd "*) answer edk2-x86_64-code.fd ;;
      *) answer edk2-aarch64-code.fd ;;
    esac
    ;;
  *" OVMF_CODE*.fd "*|*" AAVMF_CODE.fd "*)
    # Only the edk2 names are provided in pflash mode; these distro names
    # deliberately resolve to nothing so the name order stays observable.
    exit 0
    ;;
  *" OVMF.fd "*|*" QEMU_EFI.fd "*)
    [[ "${FAKE_FIRMWARE_MODE:-pflash}" == "bios" ]] || exit 0
    case " $* " in
      *" OVMF.fd "*) answer OVMF.fd ;;
      *) answer QEMU_EFI.fd ;;
    esac
    ;;
esac
exec /usr/bin/find "$@"
EOF
cat >"$fake_bin/uname" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-m" && -n "${TEST_UNAME_M:-}" ]]; then
  printf '%s\n' "$TEST_UNAME_M"
else
  exec /usr/bin/uname "$@"
fi
EOF
cat >"$fake_bin/qemu-img" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${QEMU_IMG_LOG:?}"
target="${!#}"
: >"$target"
exit 0
EOF
cat >"$fake_bin/qemu-system-x86_64" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s %s\n' "$(basename "$0")" "$*" >> "${QEMU_LOG:?}"
# Stand in for the guest. The bootstrap socket qemu is handed as a chardev is
# exactly what the guest agent connects to, so the v2 handshake is exercised
# from here.
socket_path=""
while (($#)); do
  case "$1" in
    -chardev) socket_path="$(sed -E 's/.*path=([^,]+).*/\1/' <<<"${2:-}")"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ -n "${RUNNER_CONSUMED:-}" && -n "$socket_path" ]]; then
  if [[ -S "$socket_path" ]]; then
    printf 'bootstrap-socket-before-qemu:present\n' >> "${CREDENTIAL_LOG:?}"
  else
    printf 'bootstrap-socket-before-qemu:absent\n' >> "${CREDENTIAL_LOG:?}"
  fi
  printf 'vm-run-directory:%s\n' \
    "$(/usr/bin/stat -c '%a' "$(dirname "$socket_path")")" >> "${CREDENTIAL_LOG:?}"
  REVIEW_BOOTSTRAP_SOCKET="$socket_path" \
    REVIEW_TEST_CONSUMED="${RUNNER_CONSUMED}" \
    python3 "${REVIEW_TEST_CONSUMER:?}"
fi
exit 97
EOF
cp "$fake_bin/qemu-system-x86_64" "$fake_bin/qemu-system-aarch64"
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
while (($#)); do
  case "$1" in
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

# Split pflash pair (CODE + VARS) plus a single-blob fallback firmware.
: >"$firmware_dir/edk2-x86_64-code.fd"
: >"$firmware_dir/edk2-i386-vars.fd"
: >"$firmware_dir/edk2-aarch64-code.fd"
: >"$firmware_dir/edk2-arm-vars.fd"
: >"$firmware_dir/OVMF.fd"
: >"$firmware_dir/QEMU_EFI.fd"

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
  : >"$qemu_log"
  : >"$qemu_img_log"
  : >"$curl_log"
  : >"$find_log"
  : >"$credential_log"
  rm -f "$consumed_marker"
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
      -u TEST_UNAME_M -u TEST_CURL_MODE \
      -u GOOSE_THINKING_EFFORT -u GOOSE_CONTEXT_LIMIT \
      -u REVIEW_NON_INTERACTIVE -u GOOSE_INSTALLED \
      -u REVIEW_TEST_BOOTSTRAP_MODE -u REVIEW_CONTAINER_NAME \
      -u REVIEW_QUEUE_NAME \
      HOME="$home" PATH="$fake_bin:/usr/bin:/bin" TMPDIR="$tmp_root" \
      GUM_LOG="$gum_log" RUNNER_LOG="$runner_log" QEMU_LOG="$qemu_log" \
      IMAGE_LOG="$image_log" \
      QEMU_IMG_LOG="$qemu_img_log" CURL_LOG="$curl_log" FIND_LOG="$find_log" \
      CREDENTIAL_LOG="$credential_log" \
      RUNNER_CONSUMED="$consumed_marker" \
      REVIEW_TEST_CONSUMER="$consumer" \
      REVIEW_BOOTSTRAP_TIMEOUT=20 \
      REVIEW_TEST_SKIP_VM_FETCH=1 \
      FAKE_BREW_PREFIX="$scratch/no-such-brew-prefix" \
      FAKE_FIRMWARE_DIR="$firmware_dir" \
      FAKE_FIRMWARE_MODE=pflash \
      "$@" \
      "$real_just" --justfile "$justfile" "$recipe" "${RECIPE_ARGS[@]}" 2>&1
  )"
  STATUS=$?
  set -e
}

error_line_count() { grep -c '^ERROR:' <<<"$1" || true; }

# Seed a verified VM cache entry: the launch path refuses to boot anything it
# has not checksum-verified, so scenarios that need a VM put one here first.
seed_vm_cache() {
  local arch="$1"
  local raw="$state_dir/review-vm-25.08.15-${arch}.raw"
  mkdir -p "$state_dir"
  printf '%s guest\n' "$arch" >"$raw"
  (cd "$state_dir" && sha256sum "$(basename "$raw")" >"$(basename "$raw").sha256")
}

kvm_usable() { [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; }

# ══ 1. Preflight: exactly one actionable ERROR per failure ════════════════
begin "preflight: missing GitHub auth yields one actionable error"
run_recipe review
assert_nonzero_status "$STATUS" "unauthenticated gh must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "gh auth login" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "copilot" "$OUT"
assert_not_contains "codex" "$OUT"

begin "preflight: missing Goose provider configuration yields one actionable error"
rm -f "$home/.config/goose/config.yaml"
run_recipe review GH_READY=1
assert_nonzero_status "$STATUS" "an unconfigured goose must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "goose configure" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "codex" "$OUT"
write_goose_config

begin "preflight: an invalid Goose config (no provider) is treated as unconfigured"
printf 'model: llama3.1\n' >"$home/.config/goose/config.yaml"
run_recipe review GH_READY=1
assert_nonzero_status "$STATUS" "a provider-less goose config must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "goose configure" "$OUT"
write_goose_config

begin "preflight: Goose's current active_provider config counts as configured"
# Goose >= 1.45 records the selection as 'active_provider:' beside a
# 'providers:' map; the launcher must accept it or every launch dies on
# "Goose has no usable provider configuration" after Goose migrates the
# host config. No VM disk is seeded, so a passing preflight surfaces as
# the VM-disk error instead.
cat >"$home/.config/goose/config.yaml" <<'EOF'
providers:
  github_copilot:
    enabled: true
    model: kimi-k3
    configured: true
active_provider: github_copilot
EOF
run_recipe review GH_READY=1
assert_nonzero_status "$STATUS" "no VM disk is available in this scenario"
assert_not_contains "Goose has no usable provider configuration" "$OUT"
assert_contains "no review VM disk is available for this host" "$OUT"
write_goose_config

begin "preflight: unsupported GOOSE_PROVIDER yields one actionable Copilot-only error"
run_recipe review GH_READY=1 GOOSE_PROVIDER=openai
assert_nonzero_status "$STATUS" "an unsupported provider must fail the launch"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "GOOSE_PROVIDER=openai is not supported" "$OUT"
assert_contains "GOOSE_PROVIDER=github_copilot" "$OUT"

# ══ 2. TOOL handling: Goose only ══════════════════════════════════════════
begin "TOOL=claude is rejected with a Goose-only error"
run_recipe review GH_READY=1 TOOL=claude
assert_nonzero_status "$STATUS" "a non-Goose TOOL must be a hard error"
assert_contains "TOOL=claude is not supported" "$OUT"
assert_contains "review runs Goose only" "$OUT"
assert_not_contains "auto-detected" "$OUT"
assert_not_contains "Multiple AI CLIs" "$OUT"

begin "TOOL=goose is accepted"
# No VM disk is cached here, so the launch stops on that instead of on TOOL.
run_recipe review GH_READY=1 TOOL=goose
assert_nonzero_status "$STATUS" "no VM disk is available in this scenario"
assert_not_contains "is not supported" "$OUT"
assert_not_contains "Unset TOOL" "$OUT"

begin "review: an unresolvable VM disk is one actionable error, not a silent no-op"
run_recipe review GH_READY=1
assert_nonzero_status "$STATUS" "a launch with no VM disk must fail"
assert_eq "$(error_line_count "$OUT")" 1 "expected exactly one ERROR: line"
assert_contains "no review VM disk is available for this host" "$OUT"
assert_contains "REVIEW_VM_RAW" "$OUT"

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

# ══ 2b. Queue walk: no Hive, GH_TOKEN required, args pass through ════════
begin "review-queue: launches the walk with no Hive config at all"
reset_logs
mv "$home/.config/hive" "$home/.config/hive.saved"
RECIPE_ARGS=(--repo bluefin)
run_recipe review-queue GH_READY=1 FAKE_GH_TOKEN=gho-test-token
mv "$home/.config/hive.saved" "$home/.config/hive"
assert_nonzero_status "$STATUS" "the fake runner always exits non-zero"
assert_file_contains "--name review-queue" "$runner_log"
assert_file_contains "queue --repo bluefin" "$runner_log"
assert_file_contains "--env GOOSE_PROVIDER=github_copilot" "$runner_log"
assert_file_not_contains ".config/hive" "$runner_log"
assert_file_contains "GH_TOKEN:present" "$credential_log"
assert_not_contains "contributor.env" "$OUT"
assert_contains "starting the PR queue walk (no Hive)" "$OUT"

begin "review-queue: no GitHub token is one actionable error"
reset_logs
run_recipe review-queue GH_READY=1
assert_nonzero_status "$STATUS" "a queue walk without a token must not launch"
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

# ══ 3. Doctor: advisory VM limitation, no failure on a fully provisioned host ══
if kvm_usable; then
  begin "review-doctor: fully provisioned x86_64 host exits 0 with VM advisory"
  reset_logs
  run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 \
    FAKE_GH_TOKEN=gho-test-token FAKE_GH_SCOPES="'repo', 'read:org'" \
    FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
  assert_zero_status "$STATUS" "a fully provisioned x86_64 doctor run must exit 0"
  assert_contains "a GitHub token is available for the container-only agent" "$OUT"
  assert_contains "! VM GitHub identity is blocked" "$OUT"
  assert_contains "a Copilot credential is available" "$OUT"
  assert_contains "0 failed." "$OUT"
else
  begin "review-doctor: SKIPPED (/dev/kvm is not usable by this user)"
fi

# ══ 4. VM path: bootstrap handshake into the foreground QEMU guest ════════
seed_vm_cache x86_64
if kvm_usable; then
  begin "VM: foreground qemu, v2 bootstrap handshake, no secrets"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-test REVIEW_TEST_RECV_SIZE=1
  assert_nonzero_status "$STATUS" "the fake qemu always exits non-zero"

  assert_eq "$(wc -c <"$gum_log")" 0 "gum must not be invoked"
  assert_file_not_exists "$cfg_dir/last-selections.env"

  # The guest is a plain foreground qemu with a private control channel and no
  # host secret material anywhere on its command line. No container is
  # involved at all: the VM path never touches podman.
  assert_eq "$(wc -c <"$runner_log")" 0 "the VM path must not start a container"
  assert_file_contains "-chardev socket,id=control,path=${tmp_root}/review." "$qemu_log"
  assert_file_contains "org.projectbluefin.review.bootstrap" "$qemu_log"
  assert_file_not_contains "-daemonize" "$qemu_log"
  assert_file_not_contains "${home}/.config/hive/contributor.env" "$qemu_log"
  assert_file_not_contains "super-secret-registration-token" "$qemu_log"
  assert_file_contains "vm-run-directory:700" "$credential_log"

  # The version-2 envelope reached the guest consumer and was acknowledged.
  assert_file_exists "$consumed_marker"
  assert_file_contains "acknowledged" "$consumed_marker"
  assert_file_not_exists "$cfg_dir/secrets.env"

  # No secret in any log this run, and the per-run directory is cleaned up.
  for log in "$runner_log" "$qemu_log" "$qemu_img_log" "$curl_log" "$find_log" "$gum_log"; do
    assert_file_not_contains "super-secret-registration-token" "$log"
  done
  assert_not_contains "super-secret-registration-token" "$OUT"
  assert_eq "$(/usr/bin/find "$tmp_root" -maxdepth 1 -name 'review.*' -print -quit)" "" \
    "the per-run directory must be removed on exit"

  begin "VM: waits for the bootstrap socket before handing its path to qemu"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-4o REVIEW_TEST_BOOTSTRAP_MODE=delayed
  assert_nonzero_status "$STATUS" "the fake qemu always exits non-zero"
  assert_file_contains "bootstrap-socket-before-qemu:present" "$credential_log"
  assert_file_exists "$consumed_marker"

  begin "VM: a bootstrap process that dies before bind fails before qemu"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-4o REVIEW_TEST_BOOTSTRAP_MODE=exit
  assert_nonzero_status "$STATUS" "a dead bootstrap process must fail the launch"
  assert_contains "VM bootstrap server exited before binding" "$OUT"
  assert_contains "simulated bootstrap bind failure" "$OUT"
  assert_eq "$(wc -c <"$qemu_log")" 0 "qemu must not receive an unbound socket path"

  begin "VM: the Copilot credential rides the bootstrap envelope, not the guest argv"
  # The container path already passes this token; without it here the VM path
  # boots an agent that stalls on a device code nobody is there to type.
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-4o \
    FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token REVIEW_TEST_SPLIT_ACK=1
  assert_contains "Copilot credential passed" "$OUT"
  assert_file_contains "provider_secret:present" "$consumed_marker"
  assert_file_contains "github_token:absent" "$consumed_marker"
  # The secret travels over the one-shot socket only: never on a command line,
  # never in the guest environment, never in this terminal.
  assert_not_contains "ghu-keyring-token" "$OUT"
  for log in "$runner_log" "$qemu_log" "$qemu_img_log" "$curl_log" "$find_log" "$gum_log"; do
    assert_file_not_contains "ghu-keyring-token" "$log"
  done

  begin "VM: the GitHub identity block is reported the same way whatever the host has"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-4o \
    FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token FAKE_GH_TOKEN=gho-test-token
  assert_contains "VM GitHub identity is blocked" "$OUT"
  assert_contains "cannot satisfy this VM prerequisite" "$OUT"
  assert_contains "Use review-container for work that needs fork, push, or PR access" "$OUT"
  assert_not_contains "gh auth login" "$OUT"
  assert_file_contains "github_token:absent" "$consumed_marker"
  assert_not_contains "gho-test-token" "$OUT"
  for log in "$runner_log" "$qemu_log" "$qemu_img_log" "$curl_log" "$find_log" "$gum_log"; do
    assert_file_not_contains "gho-test-token" "$log"
  done

  begin "VM: a missing Copilot credential is named plainly, not discovered in the guest"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 \
    GOOSE_MODEL=gpt-4o
  assert_contains "no Copilot credential found" "$OUT"
  assert_contains "gh auth token' is NOT a substitute" "$OUT"
  assert_contains "goose configure" "$OUT"
  assert_contains "VM GitHub identity is blocked" "$OUT"
  assert_contains "cannot satisfy this VM prerequisite" "$OUT"
  assert_not_contains "gh auth login" "$OUT"
  assert_file_contains "provider_secret:absent" "$consumed_marker"

  begin "VM: an unsupported provider is rejected before the guest starts"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64 GOOSE_PROVIDER=openai \
    FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
  assert_nonzero_status "$STATUS" "an unsupported provider must not start the VM"
  assert_contains "GOOSE_PROVIDER=openai is not supported" "$OUT"
  assert_eq "$(wc -c <"$qemu_log")" 0 "the VM must not start"
  assert_not_contains "ghu-keyring-token" "$OUT"
else
  begin "VM: SKIPPED (/dev/kvm is not usable by this user)"
fi

# ══ Fetch/verify behaviour of the raw disk ════════════════════════════════
begin "fetch: a raw disk is removed when its checksum sidecar download fails"
rm -rf "$home/.local/state"
mkdir -p "$state_dir"
stale_raw="$state_dir/review-vm-25.08.13-x86_64.raw"
printf 'stale guest\n' >"$stale_raw"
(cd "$state_dir" && sha256sum "$(basename "$stale_raw")" >"$(basename "$stale_raw").sha256")
reset_logs
run_recipe review GH_READY=1 \
  REVIEW_TEST_SKIP_VM_FETCH=0 TEST_UNAME_M=x86_64 TEST_CURL_MODE=checksum-fail
assert_nonzero_status "$STATUS" "a failed sidecar download must fail the launch"
assert_contains "VM release checksum sidecar is not published yet" "$OUT"
assert_file_contains "review-vm-25.08.15-x86_64.raw" "$curl_log"
# fsdk-containers still publishes the pre-rename asset name. Fetching
# 'review-vm-...' 404s, so the URL must carry the name the release actually has
# while the local cache keeps our own review-vm-* name.
assert_file_contains "releases/download/v25.08.15/donate-clanker-vm-25.08.15-x86_64.raw.zst" "$curl_log"
assert_file_contains "releases/download/v25.08.15/donate-clanker-vm-25.08.15-x86_64.raw.sha256" "$curl_log"
assert_file_not_contains "releases/download/v25.08.15/review-vm-" "$curl_log"
assert_file_not_contains "25.08.13-x86_64.raw.zst" "$curl_log"
assert_file_exists "$stale_raw"
assert_file_not_exists "$state_dir/review-vm-25.08.15-x86_64.raw"
assert_file_not_exists "$state_dir/review-vm-25.08.15-x86_64.raw.partial"
assert_file_not_exists "$state_dir/review-vm-25.08.15-x86_64.raw.sha256"
assert_file_not_exists "$state_dir/review-vm-25.08.15-x86_64.raw.sha256.partial"

begin "fetch: a successful raw fetch leaves command substitution with only the raw path"
rm -rf "$home/.local/state"
mkdir -p "$state_dir"
reset_logs
run_recipe review GH_READY=1 \
  REVIEW_TEST_SKIP_VM_FETCH=0 TEST_UNAME_M=x86_64 TEST_CURL_MODE=fetch-success
assert_file_exists "$state_dir/review-vm-25.08.15-x86_64.raw"
assert_file_exists "$state_dir/review-vm-25.08.15-x86_64.raw.sha256"
assert_not_contains "VM raw disk not found:" "$OUT"
assert_contains "Fetching review VM 25.08.15 for x86_64..." "$OUT"
assert_contains "Decompressing VM image..." "$OUT"

begin "verify: an incomplete exact cache entry is never reused"
mkdir -p "$state_dir"
raw_x86="$state_dir/review-vm-25.08.15-x86_64.raw"
raw_arm="$state_dir/review-vm-25.08.15-aarch64.raw"
printf 'x86 guest\n' >"$raw_x86"
run_recipe review GH_READY=1 TEST_UNAME_M=x86_64
assert_nonzero_status "$STATUS" "an incomplete cache must not boot"
assert_contains "cached VM 25.08.15 for x86_64 is incomplete or failed verification; refetching it" "$OUT"
assert_file_not_exists "$raw_x86"

printf 'x86 guest\n' >"$raw_x86"
(cd "$state_dir" && sha256sum "$(basename "$raw_x86")" >"$(basename "$raw_x86").sha256")
printf 'arm guest\n' >"$raw_arm"
(cd "$state_dir" && sha256sum "$(basename "$raw_arm")" >"$(basename "$raw_arm").sha256")

begin "verify: a matching cache removes obsolete releases for its architecture"
reset_logs
run_recipe review GH_READY=1 TEST_UNAME_M=x86_64
assert_file_not_exists "$stale_raw"
assert_file_exists "$raw_arm"

# ══ 6/7. Local QEMU: overlay boot, arch selection, firmware detection ═════
if kvm_usable; then
  begin "local QEMU (x86_64): boots a per-run overlay, never the master image"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=x86_64
  assert_nonzero_status "$STATUS" "the fake qemu always exits non-zero"
  assert_contains "booting local review VM" "$OUT"
  assert_file_contains "qemu-system-x86_64" "$qemu_log"
  assert_file_contains "-machine q35" "$qemu_log"
  assert_file_contains "-device virtio-serial-pci" "$qemu_log"
  assert_file_contains "-nographic" "$qemu_log"
  assert_file_contains "org.projectbluefin.review.bootstrap" "$qemu_log"

  # The verified master image is copy-on-write backing only. Booting it
  # directly mutates it and breaks its checksum.
  assert_file_contains "create -q -f qcow2 -F raw -b $raw_x86" "$qemu_img_log"
  assert_file_contains "overlay.qcow2,format=qcow2,if=virtio" "$qemu_log"
  assert_file_not_contains "file=${raw_x86}" "$qemu_log"

  # Split pflash firmware: read-only CODE at unit 0, writable VARS at unit 1.
  assert_file_contains "-name edk2-x86_64-code.fd" "$find_log"
  assert_file_contains "if=pflash,format=raw,unit=0,readonly=on,file=${firmware_dir}/edk2-x86_64-code.fd" "$qemu_log"
  assert_file_contains "if=pflash,format=raw,unit=1,file=" "$qemu_log"
  assert_file_contains "efivars.fd" "$qemu_log"
  assert_file_not_contains "-bios" "$qemu_log"

  begin "local QEMU (aarch64): arch-specific machine, device and firmware"
  reset_logs
  run_recipe review GH_READY=1 TEST_UNAME_M=aarch64
  assert_nonzero_status "$STATUS" "the fake qemu always exits non-zero"
  assert_file_contains "qemu-system-aarch64" "$qemu_log"
  assert_file_contains "-machine virt" "$qemu_log"
  assert_file_contains "-device virtio-serial-device" "$qemu_log"
  assert_file_contains "-name edk2-aarch64-code.fd" "$find_log"
  assert_file_contains "if=pflash,format=raw,unit=0,readonly=on,file=${firmware_dir}/edk2-aarch64-code.fd" "$qemu_log"
  assert_file_contains "create -q -f qcow2 -F raw -b $raw_arm" "$qemu_img_log"
  assert_file_not_contains "file=${raw_arm}" "$qemu_log"

  begin "local QEMU: single-blob firmware falls back to -bios"
  reset_logs
  run_recipe review GH_READY=1 \
    TEST_UNAME_M=x86_64 FAKE_FIRMWARE_MODE=bios
  assert_nonzero_status "$STATUS" "the fake qemu always exits non-zero"
  assert_file_contains "-name edk2-x86_64-code.fd" "$find_log"
  assert_file_contains "-name OVMF.fd" "$find_log"
  assert_file_contains "-bios ${firmware_dir}/OVMF.fd" "$qemu_log"
  assert_file_not_contains "if=pflash" "$qemu_log"

  begin "local QEMU: no firmware anywhere is an actionable error"
  reset_logs
  run_recipe review GH_READY=1 \
    TEST_UNAME_M=x86_64 FAKE_FIRMWARE_MODE=none
  assert_nonzero_status "$STATUS" "missing firmware must fail the launch"
  assert_contains "matching UEFI firmware for x86_64 was not found" "$OUT"
  assert_contains "brew install qemu" "$OUT"
  assert_eq "$(wc -c <"$qemu_log")" 0 "qemu must not be invoked without firmware"
else
  begin "local QEMU: SKIPPED (/dev/kvm is not usable by this user)"
fi

# ══ 4. Container recipe ═══════════════════════════════════════════════════
begin "review-container: exactly one foreground podman run, hive mounts only"
reset_logs
run_recipe review-container GH_READY=1 \
  GOOSE_MODEL=gpt-test
assert_nonzero_status "$STATUS" "the fake podman always exits non-zero"
assert_eq "$(wc -l <"$runner_log")" 1 "expected exactly one podman invocation"
assert_file_contains "run --rm --interactive --tty --replace --name review-container" "$runner_log"
assert_file_contains "--volume ${home}/.config/hive:/home/dev/.config/hive:ro" "$runner_log"
assert_file_contains "--volume ${home}/.config/hive/contributor.env:/home/dev/.config/hive/contributor.env:ro" "$runner_log"
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
assert_eq "$(wc -c <"$qemu_log")" 0 "the container recipe must not start a VM"
# A moving tag must be refreshed on every launch, or a contributor silently
# keeps running whatever copy they first pulled.
assert_file_contains "pull ghcr.io/projectbluefin/review:stable" "$image_log"

begin "hive selection: the current repository's registration wins when it exists"
reset_logs
cat >"$home/.config/hive/contributor.review.env" <<'EOF'
HIVE_REGISTRATION_TOKEN=named-secret-token
HIVE_HUB=wss://named-hive.invalid/contribute
CONTRIBUTOR_ID=test-contributor-named
CONTRIBUTOR_USERNAME=test-user
AGENT_BACKEND=goose
EOF
# The tests run with the review repository as cwd, so the repo-derived
# registration name is 'review'.
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.review.env:/home/dev/.config/hive/contributor.env:ro" "$runner_log"
assert_contains "hive: wss://named-hive.invalid/contribute (registration 'review')" "$OUT"
assert_not_contains "super-secret-registration-token" "$OUT"
assert_not_contains "named-secret-token" "$OUT"
assert_file_not_contains "named-secret-token" "$runner_log"
rm -f "$home/.config/hive/contributor.review.env"

begin "hive selection: no repo registration falls back to the default and says so"
reset_logs
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test
assert_file_contains "--volume ${home}/.config/hive/contributor.env:/home/dev/.config/hive/contributor.env:ro" "$runner_log"
assert_contains "hive: wss://example.invalid/contribute (default registration)" "$OUT"
assert_contains "REVIEW_HIVE=review" "$OUT"

begin "hive selection: REVIEW_HIVE overrides the repository-derived name"
reset_logs
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.otherhive.env"
sed -i 's|wss://example.invalid/contribute|wss://other-hive.invalid/contribute|' \
  "$home/.config/hive/contributor.otherhive.env"
run_recipe review-container GH_READY=1 GOOSE_MODEL=gpt-test REVIEW_HIVE=otherhive
assert_file_contains "--volume ${home}/.config/hive/contributor.otherhive.env:/home/dev/.config/hive/contributor.env:ro" "$runner_log"
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
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64
assert_contains "Agent backend (Goose only)" "$OUT"
assert_not_contains "claude" "$OUT"
assert_not_contains "codex" "$OUT"
assert_eq "$(wc -c <"$qemu_log")" 0 "doctor must not start a VM"
assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: rejects an unsupported provider"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 GOOSE_PROVIDER=ollama
assert_nonzero_status "$STATUS" "an unsupported provider must fail doctor"
assert_contains "GOOSE_PROVIDER=ollama is not supported" "$OUT"

begin "review-doctor: names an unavailable aarch64 raw release before launch"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=aarch64 \
  TEST_CURL_MODE=release-missing
assert_nonzero_status "$STATUS" "an unavailable aarch64 raw asset must fail doctor"
assert_contains "aarch64 VM release artifact is unavailable" "$OUT"
assert_contains "Use review-container until the aarch64 raw asset is released" "$OUT"
assert_file_contains "donate-clanker-vm-25.08.15-aarch64.raw.zst" "$curl_log"
assert_eq "$(wc -c <"$qemu_log")" 0 "doctor must not start a VM"
assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: reports a usable Copilot credential without printing it"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_contains "Copilot credential" "$OUT"
assert_contains "a Copilot credential is available" "$OUT"
assert_not_contains "ghu-keyring-token" "$OUT"
assert_eq "$(wc -c <"$qemu_log")" 0 "doctor must not start a VM"
assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: a missing Copilot credential is a failed check with the fix"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64
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
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_contains "AGENT_BACKEND=copilot" "$OUT"
assert_contains "will not touch it" "$OUT"
assert_file_contains "AGENT_BACKEND=copilot" "$home/.config/hive/contributor.env"
cp "$backend_backup" "$home/.config/hive/contributor.env"

begin "review-doctor: a matching AGENT_BACKEND raises no warning"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 \
  FAKE_KEYRING_COPILOT_TOKEN=ghu-keyring-token
assert_not_contains "but review always launches goose" "$OUT"

begin "review-doctor: reports the agent's GitHub token and its scopes, not its value"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64 \
  FAKE_GH_TOKEN=gho-test-token FAKE_GH_SCOPES="'admin:org', 'repo'"
assert_contains "a GitHub token is available for the container-only agent" "$OUT"
assert_contains "VM GitHub identity is blocked" "$OUT"
assert_contains "host gh login or REVIEW_GH_TOKEN cannot satisfy this VM prerequisite" "$OUT"
assert_contains "admin:org" "$OUT"
assert_not_contains "gho-test-token" "$OUT"
assert_eq "$(wc -c <"$qemu_log")" 0 "doctor must not start a VM"
assert_file_not_contains "run --rm" "$runner_log"

begin "review-doctor: a missing GitHub token is a failed check with the fix"
reset_logs
run_recipe review-doctor GH_READY=1 TEST_UNAME_M=x86_64
assert_nonzero_status "$STATUS" "a missing GitHub token must fail the doctor"
assert_contains "no GitHub token is available for the container-only agent" "$OUT"
assert_contains "REVIEW_GH_TOKEN" "$OUT"
assert_contains "VM GitHub identity is blocked" "$OUT"

# ══ 5/6. Static guarantees read straight off the justfile ════════════════
begin "static: the launcher can never background a VM or a container"
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

if grep -nE 'podman run.*(^| )(-d|--detach)( |$)' "$code"; then
  fail "podman run must never detach"
fi
# A lone trailing '&' backgrounds the launch; '&&' and '2>&1' must not match.
if grep -nE '(podman run|qemu-system-).*[^&>]&[[:space:]]*$' "$code"; then
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
# argument arrays (opened as CONTAINER_ARGS=( and appended to with +=), any
# bare 'podman run'/'podman create', and the qemu invocation.
launch_args="$scratch/justfile-launch-args"
awk '
  /CONTAINER_ARGS\+?=\(/           { inargs = 1 }
  inargs                           { print; if ($0 ~ /\)[[:space:]]*$/) inargs = 0; next }
  /podman[[:space:]]+(run|create)/ { print; next }
  /qemu-system-/                   { print }
' "$joined" >"$launch_args"
# 'podman run --detach-keys' is a foreground detach *sequence*, not
# backgrounding, so the character after '--detach' has to be checked.
if grep -nE -- '--detach([^-]|$)' "$launch_args"; then
  fail "no launch argument may detach the run (--detach/--detach=true)"
fi
if grep -nE -- '(^|[[:space:]])-d([[:space:]=]|$)' "$launch_args"; then
  fail "no launch argument may detach the run (-d/-d=true)"
fi
# '-itd' and '-dit' bundle the detach flag into the short-flag cluster the
# foreground launches already use. Only clusters built from podman's own
# bundleable short flags are matched, so qemu's '-drive'/'-device' and the
# shell's '-rf'/'-euo' cannot trip this.
if grep -nE -- '(^|[[:space:]])-([aditq]+d[aditq]*|d[aditq]+)([[:space:]]|$)' "$launch_args"; then
  fail "no launch argument may bundle the detach flag into a short-flag cluster"
fi
# qemu daemonizes with its own flag, which shares nothing with podman's.
if grep -nE -- '(^|[[:space:]])-{1,2}daemonize([[:space:]]|$)' "$launch_args"; then
  fail "the qemu invocation must never daemonize"
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

begin "static: the verified master image is never booted directly"
# shellcheck disable=SC2016 # the launcher source is matched literally, not expanded
grep -q 'qemu-img create -q -f qcow2 -F raw -b "\$VM_RAW"' "$code" ||
  fail "the launcher must create a qcow2 overlay backed by \$VM_RAW"
# shellcheck disable=SC2016 # the launcher source is matched literally, not expanded
grep -q 'VM_DISK_ARGS=(-drive "file=\${VM_OVERLAY},format=qcow2,if=virtio")' "$code" ||
  fail "the launcher must boot the per-run overlay"
# shellcheck disable=SC2016 # the launcher source is matched literally, not expanded
if grep -n 'file=\${VM_RAW},format=raw' "$code"; then
  fail "the qemu invocation must never attach the master raw image"
fi

begin "static: bootstrap timeout starts only after the exact VM cache is ready"
# A stale raw glob can silently boot a different release. The only cache
# lookup must name the requested version and architecture, and the server
# invocation for local QEMU must follow overlay preparation.
# shellcheck disable=SC2016 # the launcher source is matched literally
if grep -n 'LOCAL_VM_CANDIDATES\|"\${STATE_DIR}"/\*-"' "$code"; then
  fail "VM cache selection must not glob across versions"
fi
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q 'cached_vm_raw "\$STATE_DIR" "{{vm_version}}" "\$VM_ARCH"' "$code" ||
  fail "VM cache lookup must use the requested version and architecture"
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q 'cleanup_obsolete_vm_cache "\$state_dir" "\$version" "\$arch"' "$code" ||
  fail "verified VM cache entries must clean obsolete releases for their architecture"
# shellcheck disable=SC2016 # the launcher source is matched literally
overlay_line="$(grep -n 'qemu-img create -q -f qcow2 -F raw -b "\$VM_RAW"' "$code" | head -n1 | cut -d: -f1)"
bootstrap_line="$(grep -n '^      start_bootstrap_server$' "$code" | head -n1 | cut -d: -f1)"
[[ -n "$overlay_line" && -n "$bootstrap_line" && "$bootstrap_line" -gt "$overlay_line" ]] ||
  fail "bootstrap server must start after local VM overlay preparation"

begin "static: the container never defaults to an unpublished ':latest' tag"
# publish-compat-image.yml only pushes sha-<commit>, the version tags and
# 'stable', so a ':latest' default is guaranteed 'manifest unknown'.
if grep -n 'review:latest' "$code"; then
  fail "the default contributor image must be a tag the publish workflow actually pushes"
fi
grep -q 'ghcr.io/projectbluefin/review:stable' "$code" ||
  fail "the default contributor image must be the published ':stable' tag"

begin "static: the launcher ships no lifecycle command"
# A stop/start/restart verb would mean a run can outlive its terminal. It
# cannot: Ctrl-C is the only way a review run ends, and a stale name
# is reclaimed at launch by --replace, not by a second command.
if grep -nE '^review-(stop|start|restart|kill|clean|down|up)[ :]' "$code"; then
  fail "the launcher must never ship a lifecycle recipe — Ctrl-C is the stop button"
fi
if grep -n 'just review-stop' "$code"; then
  fail "nothing may point a user at a stop command that must not exist"
fi
# The recipe list is exactly: launch the VM, launch the container, diagnose,
# walk the PR queue.
assert_eq "$(grep -cE '^review[a-z-]*[ :]' "$code")" 4 \
  "expected exactly four recipes (review, -container, -doctor, -queue)"

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

begin "static: the VM URL builder names the asset the release actually publishes"
# projectbluefin/fsdk-containers never renamed its assets after this repository
# became 'review', so 'review-vm-<version>-<arch>.raw.zst' 404s on every
# release. Doctor and the fetch path must build the identical URL, or doctor
# can pass while 'just review' 404s.
grep -q "releases/download/v%s/donate-clanker-vm-%s-%s.raw.zst" "$code" ||
  fail "the VM release URL must use the published donate-clanker-vm asset name"
if grep -n 'releases/download/v%s/review-vm-' "$code"; then
  fail "no release URL may use the unpublished review-vm asset name"
fi
assert_eq "$(grep -c 'releases/download/' "$code")" 1 \
  "the release URL must be built in exactly one place"
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q 'VM_RELEASE_URL="$(vm_release_url "{{vm_version}}" "$VM_ARCH")"' "$code" ||
  fail "doctor must report the same URL the fetch path builds"
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q 'vm_release_asset_available "{{vm_version}}" "$VM_ARCH"' "$code" ||
  fail "doctor must probe the same version and architecture it reports"
# Checksum verification stays mandatory whatever the asset is called.
grep -q 'sha256sum -c' "$code" ||
  fail "the VM raw disk must always be checksum-verified"
# shellcheck disable=SC2016 # the launcher source is matched literally
grep -q '\[\[ -f "$raw" && -f "${raw}.sha256" \]\] || return 1' "$code" ||
  fail "verification must require the checksum sidecar"

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
