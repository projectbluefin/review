#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image=""
smaller_than=""
while (($#)); do
  case "$1" in
  --image)
    image="$2"
    shift 2
    ;;
  --smaller-than)
    smaller_than="$2"
    shift 2
    ;;
  *)
    echo "unknown argument: $1" >&2
    exit 2
    ;;
  esac
done
containerfile=image/contribute/Containerfile
fail() {
  echo "contribute-contract: $*" >&2
  exit 1
}
grep -qE '^ARG FSDK_BASE_IMAGE=ghcr\.io/projectbluefin/base:[^@]+@sha256:[0-9a-f]{64}$' "$containerfile" || fail "base must be tag@digest pinned"
grep -qE '^ARG FSDK_BUILDER_IMAGE=ghcr\.io/projectbluefin/lab-runner:[^@]+@sha256:[0-9a-f]{64}$' "$containerfile" || fail "builder must be tag@digest pinned"
for pin in OMP_X86_64_SHA256 OMP_AARCH64_SHA256 NODE_X86_64_SHA256 NODE_AARCH64_SHA256 GH_X86_64_SHA256 GH_AARCH64_SHA256 TMUX_X86_64_SHA256 TMUX_AARCH64_SHA256; do grep -qE "^ARG ${pin}=[0-9a-f]{64}$" "$containerfile" || fail "missing ${pin}"; done
for path in contributor-agent.sh contributor-relay.js pi-backend.js lib/pane-classifier.js; do grep -q "${path}" "$containerfile" || fail "missing Hive runtime ${path}"; done
grep -qF 'ENTRYPOINT ["/usr/local/bin/contribute-entrypoint"]' "$containerfile" || fail "wrong entrypoint"
grep -qF 'WORKDIR /home/bluefin/workspace' "$containerfile" || fail "wrong workdir"
grep -qF 'USER 65532:65532' "$containerfile" || fail "wrong user"
grep -qF 'NODE_PATH=/usr/lib/bluefin/hive/node_modules' "$containerfile" || fail "missing NODE_PATH"
grep -qF 'io.projectbluefin.contribute="true"' "$containerfile" || fail "missing contribute label"
grep -qF 'AGENT_BACKEND=omp' "$containerfile" || fail "OMP must be the image default backend"
grep -qF 'supports only AGENT_BACKEND=omp' image/contribute/entrypoint.sh || fail "entrypoint must reject alternate backends"
grep -qF 'COPY image/tmux.conf /etc/tmux.conf' "$containerfile" || fail "missing shared tmux.conf (mouse, truecolor, history-limit)"
# Positive control: the attended path must actually show the OMP session in
# the launching terminal instead of leaving the operator staring at relay
# logs with no way to see the agent (the entrypoint used to `exec` straight
# into contributor-agent.sh, unwrapped, with nothing waiting for or attaching
# to the tmux session Hive creates).
entry=image/contribute/entrypoint.sh
# shellcheck disable=SC2016 # the entrypoint source is matched literally, not expanded
grep -q '^/usr/local/bin/contributor-agent.sh "\$@" &$' "$entry" || fail "entrypoint must background contributor-agent.sh so it can wait for and attach to its tmux session"
grep -qF 'tmux has-session -t contributor' "$entry" || fail "entrypoint must wait for the contributor tmux session before attaching"
grep -qF 'tmux attach-session -t contributor' "$entry" || fail "entrypoint must attach the attended terminal to the contributor tmux session"
grep -qF 'attach_pid=' "$entry" || fail "the attach must have explicit PID-1 cleanup ownership"
grep -qF 'IMAGE: ghcr.io/projectbluefin/contribute' .github/workflows/publish-contribute.yml || fail "wrong contributor OCI name"
grep -qF 'staging="ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/contribute"' .github/workflows/publish-contribute.yml || fail "platform attestations and index must share the contributor namespace"
grep -qF 'bluefin-contribute-SHA256SUMS.txt' .github/workflows/publish-contribute.yml || fail "contributor SIF checksum manifest missing"
grep -qF 'bluefin-review-SHA256SUMS.txt' .github/workflows/publish-appliance.yml || fail "review SIF checksum manifest missing"
grep -qF 'exec "${repo_root}/bin/bluefin-contribute" "$@"' bin/bluefin || fail "bluefin must forward contributor profile arguments"
grep -qF '"$sif" "$@"' bin/bluefin-contribute || fail "SIF launcher must forward contributor profile arguments"
grep -qF 'gh release download --repo projectbluefin/review' bin/bluefin bin/bluefin-contribute || fail "missing canonical SIF acquisition command"

# --- bin/bluefin-contribute: GH_TOKEN resolution and preflight -----------------
# A missing GitHub identity used to reach apptainer anyway: the contributor
# picked up a Hive task, its first git/gh call failed against a stale or
# absent credential, and it retried through an interactive 'gh auth login'
# device-code prompt that nobody headless could ever answer, hanging forever.
# These assertions are hermetic (stubbed gh/apptainer, no real container) and
# cover both the fail-fast preflight and the token handoff into the SIF.
launcher_scratch="$(mktemp -d)"
trap 'rm -rf "$launcher_scratch"' EXIT
mkdir -p "$launcher_scratch/bin"
touch "$launcher_scratch/sif" && chmod +x "$launcher_scratch/sif"
touch "$launcher_scratch/contributor.env" && chmod 0644 "$launcher_scratch/contributor.env"
mkdir -p "$launcher_scratch/omp-state"

cat >"$launcher_scratch/bin/apptainer" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$APPTAINER_CALL_ARGV"
printf 'GH_TOKEN=%s\nGITHUB_TOKEN=%s\n' "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" >"$APPTAINER_CALL_ENV"
exit 0
EOF
chmod +x "$launcher_scratch/bin/apptainer"

launcher_env=(
  BLUEFIN_CONTRIBUTE_SIF="$launcher_scratch/sif"
  HIVE_CONTRIBUTOR_ENV="$launcher_scratch/contributor.env"
  BLUEFIN_OMP_STATE="$launcher_scratch/omp-state"
  XDG_STATE_HOME="$launcher_scratch/state"
  APPTAINER_CALL_ARGV="$launcher_scratch/argv"
  APPTAINER_CALL_ENV="$launcher_scratch/env"
)

# No GitHub identity available anywhere: must fail fast and never reach
# apptainer, rather than launching into the unanswerable device-code hang.
cat >"$launcher_scratch/bin/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$launcher_scratch/bin/gh"
rm -f "$launcher_scratch/argv" "$launcher_scratch/env"
set +e
no_token_out="$(env -i PATH="$launcher_scratch/bin:/usr/bin:/bin" "${launcher_env[@]}" \
  "$root/bin/bluefin-contribute" 2>&1)"
no_token_status=$?
set -e
((no_token_status != 0)) || fail "bin/bluefin-contribute must refuse to launch without a GitHub token"
[[ "$no_token_out" == *'no GitHub token available'* ]] || fail "missing-token error must be actionable"
[[ ! -e "$launcher_scratch/argv" ]] || fail "bin/bluefin-contribute must never invoke apptainer without a GitHub token"

# gh resolves a token: the launch must proceed and hand the token to the SIF
# as an inherited environment variable, never as a literal --env argument.
cat >"$launcher_scratch/bin/gh" <<'EOF'
#!/usr/bin/env bash
[[ "$1 $2" == "auth token" ]] && { echo "faketoken1234567890faketoken1234567890"; exit 0; }
exit 1
EOF
chmod +x "$launcher_scratch/bin/gh"
rm -f "$launcher_scratch/argv" "$launcher_scratch/env"
set +e
resolved_out="$(env -i PATH="$launcher_scratch/bin:/usr/bin:/bin" "${launcher_env[@]}" \
  "$root/bin/bluefin-contribute" luna 2>&1)"
resolved_status=$?
set -e
((resolved_status == 0)) || fail "bin/bluefin-contribute must launch once a token resolves: $resolved_out"
[[ -e "$launcher_scratch/argv" ]] || fail "bin/bluefin-contribute must invoke apptainer once a token resolves"
grep -q '^GH_TOKEN=faketoken1234567890faketoken1234567890$' "$launcher_scratch/env" || fail "resolved GH_TOKEN must reach the contained process"
grep -q '^GITHUB_TOKEN=faketoken1234567890faketoken1234567890$' "$launcher_scratch/env" || fail "resolved GITHUB_TOKEN must reach the contained process"
grep -q -- '--env' "$launcher_scratch/argv" && fail "credential values must never be passed as --env arguments"
grep -qx 'luna' "$launcher_scratch/argv" || fail "contributor profile argument did not reach the SIF"
echo "contribute-contract: bin/bluefin-contribute GH_TOKEN handling holds"
if [[ -z "$image" ]]; then
  echo "contribute-contract: static contract holds"
  exit 0
fi
engine="${CONTAINER_ENGINE:-podman}"
inspect() { "$engine" image inspect "$image" --format "$1"; }
test "$(inspect '{{.Config.User}}')" = 65532:65532 || fail "image user"
test "$(inspect '{{.Config.WorkingDir}}')" = /home/bluefin/workspace || fail "image workdir"
test "$(inspect '{{json .Config.Entrypoint}}')" = '["/usr/local/bin/contribute-entrypoint"]' || fail "image entrypoint"
# shellcheck disable=SC2016 # the single-quoted $HOME expands inside the container, not this shell
"$engine" run --rm --entrypoint /usr/bin/bash "$image" -c 'set -eu; omp --version; node -e "require.resolve(\"ws\")"; python3 --version >/dev/null; gh --version >/dev/null; tmux -V; git --version >/dev/null; curl --version >/dev/null; find --version >/dev/null; grep --version >/dev/null; sed --version >/dev/null; cmp --version >/dev/null; test -w "$HOME"; test -w "$HOME/workspace"; test -f /usr/local/bin/contributor-relay.js; test -f /usr/local/bin/pi-backend.js; test -f /usr/local/bin/lib/pane-classifier.js; test ! -e /usr/bin/npm; test ! -e /usr/bin/corepack' >/dev/null || fail "runtime closure"
if "$engine" run --rm --env AGENT_BACKEND=goose "$image" >/dev/null 2>&1; then
  fail "alternate agent backends must be rejected"
fi
if [[ -n "$smaller_than" ]]; then
  size() { "$engine" history --format json "$1" | python3 -c 'import json,sys; print(sum(int(x.get("size") or 0) for x in json.load(sys.stdin)))'; }
  test "$(size "$image")" -lt "$(size "$smaller_than")" || fail "contribute image is not smaller than ${smaller_than}"
fi
echo "contribute-contract: runtime contract holds"
