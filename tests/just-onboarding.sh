#!/usr/bin/env bash
# Hermetic behavior contract for the root launcher. External tools are faked;
# this test never contacts GitHub, a registry, Podman, or Kubernetes.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
real_just="$(command -v just)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
home="$scratch/home"
fake_bin="$scratch/bin"
podman_log="$scratch/podman.log"
kubectl_log="$scratch/kubectl.log"
apptainer_log="$scratch/apptainer.log"
kvm="$scratch/kvm"
mkdir -p "$home/.config/hive" "$fake_bin"
host_fixture="$scratch/host"
mkdir -p "$host_fixture/etc"
touch "$host_fixture/etc/localtime" "$host_fixture/etc/hosts"
filesystem_hook="$scratch/filesystem.sh"
cat >"$filesystem_hook" <<'EOF'
test() {
  if [[ "$#" == 2 && "$1" == -e && ( "$2" == /etc/localtime || "$2" == /etc/hosts ) ]]; then
    builtin test -e "$HOST_FIXTURE$2"
  else
    builtin test "$@"
  fi
}
command() {
  if [[ "${FAKE_NO_SKOPEO:-0}" == 1 && "${1:-}" == -v && "${2:-}" == skopeo ]]; then
    return 1
  fi
  builtin command "$@"
}
EOF
export BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture"
touch "$kvm"
chmod 0666 "$kvm"
cat >"$home/.config/hive/contributor.env" <<'EOF'
HIVE_REGISTRATION_TOKEN=test-registration
HIVE_HUB=https://hive.example.test
CONTRIBUTOR_USERNAME=test-user
EOF
chmod 0600 "$home/.config/hive/contributor.env"
cat >"$fake_bin/gh" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-} ${2:-}" == "auth status" ]] && exit 0
exit 1
EOF
cat >"$fake_bin/podman" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "${1:-}" == info ]] && {
  [[ "${FAKE_PODMAN_INFO_FAIL:-0}" != 1 ]] || exit 1
  # Report the runtime Podman registers as 'krun' (issue #610). Resolving it
  # via PATH lets a test point the registration at crun-krun with no PATH 'krun'.
  [[ "$*" == *OCIRuntimes* ]] && {
    if [[ "${REVIEW_FAKE_KRUN_RUNTIME:-krun}" == crun-krun ]]; then command -v crun-krun; else command -v krun; fi
  }
  exit 0
}
printf '%s\n' "$*" >>"${PODMAN_LOG:?}"
if [[ "${1:-}" == run && -n "${EXPECT_EXTENSION:-}" ]]; then
  previous=""
  found=0
  for arg in "$@"; do
    if [[ "$previous" == --extension ]]; then
      [[ "$arg" == "$EXPECT_EXTENSION" ]] || exit 19
      found=1
    fi
    previous="$arg"
  done
  [[ "$found" == 1 ]] || exit 19
fi
if [[ "${1:-}" == run && "${EXPECT_EMPTY_SCOPE:-}" == 1 ]]; then
  args=("$@")
  count="${#args[@]}"
  [[ "$count" -ge 2 && "${args[count - 2]}" == ghcr.io/projectbluefin/review:stable && "${args[count - 1]}" == --advisor ]] || exit 19
fi
case "${1:-} ${2:-} ${3:-}" in
  "system connection list")
    [[ "${FAKE_REMOTE_DEFAULT:-}" != 1 ]] || printf 'remote\tssh://engine.example.test/run/podman.sock\tidentity\ttrue\n'
    exit 0 ;;
  "image exists "*) [[ "${FAKE_IMAGE_MISSING:-0}" != 1 ]]; exit ;;
  "pull "*) [[ "${FAKE_PULL_FAIL:-0}" != 1 ]]; exit ;;
  "inspect --format "*) printf 'false\n'; exit 0 ;;
  "image inspect --format") printf '26.08.07|0123456789abcdef|sha256:deadbeef\n'; exit 0 ;;
  "container exists "*) exit 1 ;;
  "run "*) exit 17 ;;
esac
exit 0
EOF
cat >"$fake_bin/apptainer" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${APPTAINER_LOG:?}"
previous=""
for arg in "$@"; do
  [[ "$previous" != --home ]] || runtime_home="${arg%%:*}"
  if [[ "$previous" == --pwd && "$arg" == /home/bluefin/workspace ]]; then
    [[ -d "$runtime_home/workspace" ]] || exit 19
  fi
  previous="$arg"
done
if [[ "${EXPECT_APPTAINER_CREDENTIALS:-}" == 1 ]]; then
  injected=()
  for name in GH_TOKEN OPENAI_API_KEY CONTEXT7_API_KEY HIVE_HUB AWS_BEARER_TOKEN_BEDROCK AWS_REGION AWS_DEFAULT_REGION; do
    source_name="APPTAINERENV_${name}"
    [[ -v "$source_name" ]] && injected+=("$name=${!source_name}")
  done
  injected+=("EXPECT_CONTEXT7_CREDENTIAL=${EXPECT_CONTEXT7_CREDENTIAL:-0}")
  env -i "${injected[@]}" /bin/bash -c '
    [[ "$GH_TOKEN" == test-gh-token &&
       "$OPENAI_API_KEY" == "test-provider-token" &&
       "$HIVE_HUB" == https://hive.example.test &&
       ( -z "$AWS_BEARER_TOKEN_BEDROCK" || "$AWS_BEARER_TOKEN_BEDROCK" == "test-bedrock-token" ) &&
       ( -z "$AWS_REGION" || "$AWS_REGION" == "us-west-2" ) &&
       ( -z "$AWS_DEFAULT_REGION" || "$AWS_DEFAULT_REGION" == "us-west-2" ) &&
       ( "$EXPECT_CONTEXT7_CREDENTIAL" != 1 || "$CONTEXT7_API_KEY" == "test-context7-token" ) ]]
  ' || exit 19
fi
exit 18
EOF
cat >"$fake_bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"${KUBECTL_LOG:?}"
case "$*" in
  "create namespace bluefin-system --dry-run=client -o yaml") printf 'apiVersion: v1\nkind: Namespace\n' ;;
  "create secret generic contribute-secret "*) printf 'apiVersion: v1\nkind: Secret\n' ;;
  "apply "*) cat >/dev/null ;;
  "get secret contribute-secret "*) : ;;
  "get deployment contribute -n bluefin-system") : ;;
esac
exit 0
EOF
cat >"$fake_bin/krun" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$fake_bin/crun-krun" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$fake_bin/squashfuse_ll" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$fake_bin/squashfuse_ll"
export REVIEW_TEST_FUSE_DEVICE=/dev/null
cat >"$fake_bin/skopeo" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$fake_bin/skopeo"
chmod +x "$fake_bin/gh" "$fake_bin/podman" "$fake_bin/kubectl" "$fake_bin/krun" "$fake_bin/apptainer"

failures=0
scenario=startup
fail() {
  printf 'FAIL [%s]: %s\n' "$scenario" "$1" >&2
  failures=$((failures + 1))
}
contains() { grep -Fq -- "$1" <<<"$2" || fail "expected output to contain: $1"; }
not_contains() {
  grep -Fq -- "$1" <<<"$2" && fail "expected output not to contain: $1"
  return 0
}
log_contains() { grep -Fq -- "$1" "$2" || fail "expected $2 to contain: $1"; }
log_not_contains() {
  grep -Fq -- "$1" "$2" && fail "expected $2 not to contain: $1"
  return 0
}
configure_host_files() {
  local mask="$1"
  rm -f "$host_fixture/etc/localtime" "$host_fixture/etc/hosts"
  ((mask & 1)) && touch "$host_fixture/etc/localtime"
  ((mask & 2)) && touch "$host_fixture/etc/hosts"
  return 0
}
assert_apptainer_host_files() {
  local call="$1" mask="$2" path bit
  for path in /etc/localtime /etc/hosts; do
    [[ "$path" == /etc/localtime ]] && bit=1 || bit=2
    if ((mask & bit)); then
      [[ "$call" != *"--no-mount $path"* ]] || fail "present host file $path was suppressed: $call"
    else
      [[ "$call" == *"--no-mount $path"* ]] || fail "missing host file $path was not suppressed: $call"
    fi
  done
}
run_just() {
  : >"$podman_log"
  : >"$kubectl_log"
  set +e
  output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_TEST_FUSE_DEVICE="${REVIEW_TEST_FUSE_DEVICE:-/dev/null}" FAKE_PODMAN_INFO_FAIL="${FAKE_PODMAN_INFO_FAIL:-0}" FAKE_NO_SKOPEO="${FAKE_NO_SKOPEO:-0}" FAKE_PULL_FAIL="${FAKE_PULL_FAIL:-0}" FAKE_IMAGE_MISSING="${FAKE_IMAGE_MISSING:-0}" REVIEW_GH_TOKEN=test-gh-token TERM=xterm-256color COLORTERM=truecolor "$real_just" --justfile "$root/justfile" "$@" 2>&1)"
  status=$?
  set -e
}

scenario="public recipes"
recipes="$($real_just --justfile "$root/justfile" --list)"
for recipe in contribute review-container review-queue review-appliance review-appliance-build review-stop review-doctor; do
  contains "$recipe" "$recipes"
done

scenario="doctor verifies the KVM runtime"
run_just review-doctor
[[ "$status" -eq 0 ]] || fail "review-doctor failed: $output"
contains 'Podman krun KVM runtime ready' "$output"
contains '=== Review image ===' "$output"
contains 'ghcr.io/projectbluefin/review:stable is resolvable' "$output"
contains '=== Contributor image ===' "$output"
contains 'ghcr.io/projectbluefin/contribute:stable is resolvable' "$output"

scenario="krun registered through crun-krun selects Podman"
mv "$fake_bin/krun" "$scratch/krun"
export REVIEW_FAKE_KRUN_RUNTIME=crun-krun
run_just review-queue owner/repo
[[ "$status" -eq 17 ]] || fail "crun-krun registration did not select Podman (status $status): $output"
log_contains 'run --runtime=krun --rm --interactive --tty --name bluefin-review-' "$podman_log"
unset REVIEW_FAKE_KRUN_RUNTIME
mv "$scratch/krun" "$fake_bin/krun"

scenario="doctor diagnoses missing squashfuse"
mv "$fake_bin/squashfuse_ll" "$scratch/squashfuse_ll"
FAKE_PODMAN_INFO_FAIL=1 run_just review-doctor
[[ "$status" -ne 0 ]] || fail "doctor accepted an Apptainer fallback without squashfuse"
contains 'squashfuse userland is unavailable' "$output"
mv "$scratch/squashfuse_ll" "$fake_bin/squashfuse_ll"

scenario="doctor diagnoses missing FUSE device"
REVIEW_TEST_FUSE_DEVICE="$scratch/missing-fuse" FAKE_PODMAN_INFO_FAIL=1 run_just review-doctor
[[ "$status" -ne 0 ]] || fail "doctor accepted an Apptainer fallback without a FUSE device"
contains 'FUSE device' "$output"

scenario="doctor defers remote image resolution without registry tooling"
FAKE_NO_SKOPEO=1 FAKE_PODMAN_INFO_FAIL=1 run_just review-doctor
[[ "$status" -eq 0 ]] || fail "doctor treated unavailable non-mutating registry probes as launch failure: $output"
contains '=== Review image ===' "$output"
contains 'ghcr.io/projectbluefin/review:stable resolution deferred to Apptainer launch' "$output"
contains '=== Contributor image ===' "$output"
contains 'ghcr.io/projectbluefin/contribute:stable resolution deferred to Apptainer launch' "$output"
scenario="contribute launches the OMP worker"
run_just contribute
[[ "$status" -eq 17 ]] || fail "expected fake container exit 17, got $status"
contains 'contributor image ghcr.io/projectbluefin/contribute:stable: version=26.08.07 revision=0123456789abcdef digest=sha256:deadbeef' "$output"
log_contains 'run --runtime=krun --rm --interactive --tty --name bluefin-contribute-' "$podman_log"
log_contains '--userns keep-id:uid=65532,gid=65532' "$podman_log"
log_contains "$home/.config/hive/contributor.env:/home/bluefin/.config/hive/contributor.env:ro,z" "$podman_log"
log_contains ':/home/bluefin:rw' "$podman_log"
log_contains '--env AGENT_BACKEND=omp' "$podman_log"
log_contains '--env TERM=xterm-256color' "$podman_log"
log_contains '--env COLORTERM=truecolor' "$podman_log"
log_contains 'ghcr.io/projectbluefin/contribute:stable' "$podman_log"
log_not_contains 'AGENT_MODEL' "$podman_log"
log_not_contains 'AGENT_REASONING_EFFORT' "$podman_log"
log_not_contains 'test-gh-token' "$podman_log"

scenario="review-container is the same worker"
run_just review-container
[[ "$status" -eq 17 ]] || fail "expected fake container exit 17, got $status"
log_contains 'run --runtime=krun --rm --interactive --tty --name bluefin-contribute-' "$podman_log"
log_contains 'ghcr.io/projectbluefin/contribute:stable' "$podman_log"

scenario="contributor instance names select independent state"
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.org-one.env"
cp "$home/.config/hive/contributor.env" "$home/.config/hive/contributor.org-two.env"
run_just contribute org/one
one_call="$(cat "$podman_log")"
run_just contribute org/two
two_call="$(cat "$podman_log")"
[[ "$one_call" != "$two_call" ]] || fail "different contributor instances must not collide"
[[ "$one_call" == *"contributor.org-one.env:/home/bluefin/.config/hive/contributor.env:ro,z"* ]] || fail "first contributor used the wrong registration"
[[ "$two_call" == *"contributor.org-two.env:/home/bluefin/.config/hive/contributor.env:ro,z"* ]] || fail "second contributor used the wrong registration"

scenario="contributor instance input cannot execute shell syntax"
run_just contribute "\$(touch $scratch/injected)"
[[ "$status" -ne 0 ]] || fail "invalid instance must be rejected"
[[ ! -e "$scratch/injected" ]] || fail "instance argument executed shell code"

scenario="detached workers are rejected"
set +e
output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token REVIEW_DETACH=1 "$real_just" --justfile "$root/justfile" contribute 2>&1)"
status=$?
set -e
[[ "$status" -ne 0 ]] || fail "detached launch must fail"
contains 'detached contributor containers are not supported' "$output"

scenario="KVM preflight failure falls back to Apptainer"
: >"$apptainer_log"
set +e
output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" REVIEW_TEST_KVM_DEVICE="$kvm" GH_TOKEN=test-gh-token OPENAI_API_KEY=test-provider-token CONTEXT7_API_KEY=test-context7-token HIVE_HUB= EXPECT_APPTAINER_CREDENTIALS=1 EXPECT_CONTEXT7_CREDENTIAL=1 FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" review-queue owner/repo 2>&1)"
status=$?
set -e
[[ "$status" -eq 18 ]] || fail "expected fake Apptainer exit 18, got $status"
contains 'using the isolated Apptainer fallback' "$output"
log_contains 'run --containall' "$apptainer_log"
log_contains ':/workspace,' "$apptainer_log"
log_contains ':/tmp' "$apptainer_log"
log_not_contains 'test-gh-token' "$apptainer_log"
log_not_contains 'test-provider-token' "$apptainer_log"
log_not_contains 'test-context7-token' "$apptainer_log"

scenario="contributor fallback preserves credentials under containment"
set +e
output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token OPENAI_API_KEY=test-provider-token HIVE_HUB=https://hive.example.test EXPECT_APPTAINER_CREDENTIALS=1 FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" contribute 2>&1)"
status=$?
set -e
[[ "$status" -eq 18 ]] || fail "contributor credentials did not reach contained process: $output"

for mask in 0 1 2 3; do
  scenario="Apptainer host-file mask $mask"
  configure_host_files "$mask"
  : >"$apptainer_log"
  set +e
  output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" review-queue owner/repo 2>&1)"
  status=$?
  set -e
  [[ "$status" -eq 18 ]] || fail "review fallback failed for host-file mask $mask: $output"
  assert_apptainer_host_files "$(cat "$apptainer_log")" "$mask"

  : >"$apptainer_log"
  set +e
  output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" contribute 2>&1)"
  status=$?
  set -e
  [[ "$status" -eq 18 ]] || fail "contributor fallback failed for host-file mask $mask: $output"
  assert_apptainer_host_files "$(cat "$apptainer_log")" "$mask"
done
configure_host_files 2
ln -s missing-zoneinfo "$host_fixture/etc/localtime"
scenario="review fallback suppresses dangling localtime"
: >"$apptainer_log"
set +e
output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" review-queue owner/repo 2>&1)"
status=$?
set -e
[[ "$status" -eq 18 ]] || fail "review fallback failed with dangling localtime: $output"
assert_apptainer_host_files "$(cat "$apptainer_log")" 2

scenario="contributor fallback suppresses dangling localtime"
: >"$apptainer_log"
set +e
output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_GH_TOKEN=test-gh-token FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" contribute 2>&1)"
status=$?
set -e
[[ "$status" -eq 18 ]] || fail "contributor fallback failed with dangling localtime: $output"
assert_apptainer_host_files "$(cat "$apptainer_log")" 2

scenario="review alias preserves argument boundaries"
EXPECT_EXTENSION="/tmp/review extension" run_just review-queue --extension "/tmp/review extension"
[[ "$status" -eq 17 ]] || fail "extension argument was split: $output"
EXPECT_EMPTY_SCOPE=1 run_just review-queue
[[ "$status" -eq 17 ]] || fail "zero review scope did not add only the advisor flag: $output"

log_contains 'pull ghcr.io/projectbluefin/review:stable' "$podman_log"
contains 'review appliance image ghcr.io/projectbluefin/review:stable: version=26.08.07 revision=0123456789abcdef digest=sha256:deadbeef' "$output"
log_contains ':/tmp:rw' "$podman_log"

scenario="offline review launch reports stale moving tag"
FAKE_PULL_FAIL=1 run_just review-queue owner/repo
[[ "$status" -eq 17 ]] || fail "cached review image did not start after refresh failure: $output"
contains 'using the local copy, which may be out of date' "$output"

scenario="missing review image fails after refresh failure"
FAKE_PULL_FAIL=1 FAKE_IMAGE_MISSING=1 run_just review-queue owner/repo
[[ "$status" -ne 0 ]] || fail "missing review image reached podman run"
contains 'cannot obtain review appliance image' "$output"
scenario="review-queue delegates to the OMP appliance"
run_just review-queue --issues
[[ "$status" -eq 17 ]] || fail "expected fake container exit 17, got $status"
log_contains 'run --runtime=krun --rm --interactive --tty --name bluefin-review-' "$podman_log"
log_contains 'ghcr.io/projectbluefin/review:stable --issues --advisor' "$podman_log"
log_contains '--env CONTEXT7_API_KEY' "$podman_log"
run_just review-queue autoslay
[[ "$status" -eq 17 ]] || fail "expected fake container exit 17, got $status"
log_contains 'ghcr.io/projectbluefin/review:stable --autoslay --advisor' "$podman_log"

scenario="Bedrock bearer-token credentials forward through the Podman/krun path"
: >"$podman_log"
set +e
bedrock_output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" REVIEW_TEST_KVM_DEVICE="$kvm" REVIEW_TEST_FUSE_DEVICE="/dev/null" FAKE_PODMAN_INFO_FAIL=0 FAKE_NO_SKOPEO=0 FAKE_PULL_FAIL=0 FAKE_IMAGE_MISSING=0 REVIEW_GH_TOKEN=test-gh-token TERM=xterm-256color COLORTERM=truecolor AWS_BEARER_TOKEN_BEDROCK=test-bedrock-token AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 "$real_just" --justfile "$root/justfile" review-queue owner/repo 2>&1)"
bedrock_status=$?
set -e
[[ "$bedrock_status" -eq 17 ]] || fail "expected fake container exit 17 with Bedrock credentials, got $bedrock_status"
bedrock_podman_call="$(cat "$podman_log")"
for bedrock_var in AWS_BEARER_TOKEN_BEDROCK AWS_REGION AWS_DEFAULT_REGION; do
  [[ "$bedrock_podman_call" == *"--env $bedrock_var"* ]] || fail "review Podman/krun did not forward $bedrock_var: $bedrock_podman_call"
done
log_not_contains 'test-bedrock-token' "$podman_log"
log_not_contains 'test-bedrock-token' "$bedrock_output"

scenario="Bedrock bearer-token credentials reach the contained Apptainer process"
: >"$apptainer_log"
set +e
bedrock_apptainer_output="$(env HOME="$home" PATH="$fake_bin:/usr/bin:/bin" PODMAN_LOG="$podman_log" KUBECTL_LOG="$kubectl_log" APPTAINER_LOG="$apptainer_log" REVIEW_TEST_KVM_DEVICE="$kvm" GH_TOKEN=test-gh-token OPENAI_API_KEY=test-provider-token HIVE_HUB=https://hive.example.test AWS_BEARER_TOKEN_BEDROCK=test-bedrock-token AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 EXPECT_APPTAINER_CREDENTIALS=1 FAKE_PODMAN_INFO_FAIL=1 "$real_just" --justfile "$root/justfile" review-queue owner/repo 2>&1)"
bedrock_apptainer_status=$?
set -e
[[ "$bedrock_apptainer_status" -eq 18 ]] || fail "expected fake Apptainer exit 18 with Bedrock credentials, got $bedrock_apptainer_status"
log_contains 'run --containall' "$apptainer_log"
log_not_contains 'test-bedrock-token' "$apptainer_log"
log_not_contains 'test-bedrock-token' "$bedrock_apptainer_output"

scenario="review repositories use independent microVM state"
run_just review-queue owner/repo
one_review_call="$(cat "$podman_log")"
[[ "$one_review_call" == *"ghcr.io/projectbluefin/review:stable --repo owner/repo --advisor"* ]] || fail "scoped review did not enable the advisor"
run_just review-queue owner/repo2
two_review_call="$(cat "$podman_log")"
[[ "$one_review_call" == *"bluefin-review-review-owner-repo-"* ]] || fail "first review instance was not scope-named"
[[ "$two_review_call" == *"bluefin-review-review-owner-repo2-"* ]] || fail "second review instance was not scope-named"

scenario="default remote engine uses engine-owned review volumes"
saved_kvm="$kvm"
kvm="$scratch/missing-kvm"
FAKE_REMOTE_DEFAULT=1 run_just review-queue owner/repo
kvm="$saved_kvm"
[[ "$status" -eq 17 ]] || fail "default remote review did not launch: $output"
log_contains '-home:/home/bluefin:rw' "$podman_log"
log_contains '-workspace:/workspace:rw' "$podman_log"
log_not_contains "$home/" "$podman_log"
[[ "$one_review_call" != "$two_review_call" ]] || fail "different review targets must not collide"
log_not_contains 'ghcr.io/projectbluefin/contribute' "$podman_log"

scenario="cluster scale uses the one contributor deployment"
run_just contribute cluster 3
[[ "$status" -eq 0 ]] || fail "cluster scale failed: $output"
log_contains 'apply -f deploy/contribute.yaml' "$kubectl_log"
log_contains 'set env deployment/contribute -n bluefin-system AGENT_BACKEND=omp HIVE_HUB=https://hive.example.test' "$kubectl_log"
log_contains 'scale deployment/contribute -n bluefin-system --replicas=3' "$kubectl_log"
log_contains 'rollout status deployment/contribute -n bluefin-system --timeout=15s' "$kubectl_log"
log_not_contains 'test-gh-token' "$kubectl_log"

scenario="review-container cluster delegates to the same deployment"
run_just review-container cluster 2
[[ "$status" -eq 0 ]] || fail "cluster alias failed: $output"
log_contains 'scale deployment/contribute -n bluefin-system --replicas=2' "$kubectl_log"

scenario="review-stop stops only cluster workers"
run_just review-stop cluster
[[ "$status" -eq 0 ]] || fail "cluster stop failed: $output"
log_contains 'get deployment contribute -n bluefin-system' "$kubectl_log"
log_contains 'scale deployment/contribute -n bluefin-system --replicas=0' "$kubectl_log"

if ((failures)); then
  printf 'just-onboarding: %d failure(s)\n' "$failures" >&2
  exit 1
fi
printf 'just-onboarding: OMP-only launcher contract holds\n'
