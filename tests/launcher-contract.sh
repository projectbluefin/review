#!/usr/bin/env bash
# Contract for review argument parsing and OCI/source launcher parity.
#
# Verifies that:
#   1. scripts/parse-review-args.sh parses all shorthand forms and mixed combinations.
#   2. bin/bluefin review runs the OCI appliance through libkrun.
#   3. bin/omp-review forwards identical parsed flags to OMP.
#   4. Container and source launchers preserve argument parity.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# shellcheck source=scripts/parse-review-args.sh
source "${repo_root}/scripts/parse-review-args.sh"

fail() {
  echo "launcher-contract: $*" >&2
  exit 1
}

assert_eq() {
  local actual="$1"
  local expected="$2"
  local label="${3:-}"
  if [[ "$actual" != "$expected" ]]; then
    fail "${label}: expected '${expected}', got '${actual}'"
  fi
}
arg_after() {
  local line="$1" wanted="$2" previous="" part
  for part in $line; do
    if [[ "$previous" == "$wanted" ]]; then
      printf '%s\n' "$part"
      return 0
    fi
    previous="$part"
  done
  return 1
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

# --- 1. Parser unit tests across all forms and mixed combinations -------------

test_cases=(
  "projectbluefin/review|--repo projectbluefin/review"
  "projectbluefin/review #463|--repo projectbluefin/review --pr 463"
  "projectbluefin/review 463|--repo projectbluefin/review --pr 463"
  "projectbluefin/review#463|--repo projectbluefin/review --pr 463"
  "https://github.com/projectbluefin/review|--repo projectbluefin/review"
  "https://github.com/projectbluefin/review#463|--repo projectbluefin/review --pr 463"
  "https://github.com/projectbluefin/review/pull/463|--repo projectbluefin/review --pr 463"
  "https://github.com/projectbluefin/review/issues/463|--repo projectbluefin/review --pr 463"
  "org:projectbluefin|--repo org:projectbluefin"
  "#463|--pr 463"
  "463|--pr 463"
  "--repo projectbluefin/review|--repo projectbluefin/review"
  "--repo projectbluefin/review #463|--repo projectbluefin/review --pr 463"
  "--repo projectbluefin/review 463|--repo projectbluefin/review --pr 463"
  "--repo projectbluefin/review#463|--repo projectbluefin/review --pr 463"
  "--repo=projectbluefin/review#463|--repo projectbluefin/review --pr 463"
  "--pr 463|--pr 463"
  "--pr #463|--pr 463"
  "--pr=463|--pr 463"
  "--pr=#463|--pr 463"
  "issues|--issues"
  "--issues|--issues"
  "all|--all"
  "--all|--all"
  "autoslay|--autoslay --advisor"
  "--autoslay|--autoslay --advisor"
  "projectbluefin/review autoslay|--repo projectbluefin/review --autoslay --advisor"
  "projectbluefin/review --autoslay|--repo projectbluefin/review --autoslay --advisor"
  "autoslay projectbluefin/review|--autoslay --repo projectbluefin/review --advisor"
  "--autoslay projectbluefin/review|--autoslay --repo projectbluefin/review --advisor"
  "--advisor|--advisor"
  "bluefin|--repo bluefin"
  "bluefin #123|--repo bluefin --pr 123"
  "bluefin#123|--repo bluefin --pr 123"
  "projectbluefin/review issues|--repo projectbluefin/review --issues"
  "projectbluefin/review --issues|--repo projectbluefin/review --issues"
  "issues projectbluefin/review|--issues --repo projectbluefin/review"
  "--issues projectbluefin/review|--issues --repo projectbluefin/review"
  "projectbluefin/review #463 --issues|--repo projectbluefin/review --pr 463 --issues"
  "projectbluefin/review#463 --issues|--repo projectbluefin/review --pr 463 --issues"
  "--issues projectbluefin/review#463|--issues --repo projectbluefin/review --pr 463"
  "--issues projectbluefin/review #463|--issues --repo projectbluefin/review --pr 463"
  "projectbluefin/review issues #463|--repo projectbluefin/review --issues --pr 463"
  "--repo projectbluefin/review --issues|--repo projectbluefin/review --issues"
  "--issues --repo projectbluefin/review|--issues --repo projectbluefin/review"
  "projectbluefin/review --skip-repo lab|--repo projectbluefin/review --skip-repo lab"
  "--skip-repo lab projectbluefin/review|--skip-repo lab --repo projectbluefin/review"
)

for case in "${test_cases[@]}"; do
  input="${case%%|*}"
  expected="${case#*|}"
  [[ "$expected" == *--advisor* ]] || expected="$expected --advisor"
  # shellcheck disable=SC2086
  parse_review_args $input
  actual="${PARSED_REVIEW_ARGS[*]:-}"
  assert_eq "$actual" "$expected" "parse_review_args '$input'"
done
parse_review_args "--extension=/tmp/review extension"
assert_eq "${#PARSED_REVIEW_ARGS[@]}" "2" "single argument with whitespace plus advisor"
assert_eq "${PARSED_REVIEW_ARGS[0]}" "--extension=/tmp/review extension" "literal extension path"
assert_eq "${PARSED_REVIEW_ARGS[1]}" "--advisor" "advisor is always enabled"

# Verify standalone execution of parse-review-args.sh
standalone_out="$("${repo_root}/scripts/parse-review-args.sh" projectbluefin/review#463 --issues | tr '\n' ' ' | sed 's/ $//')"
assert_eq "$standalone_out" "--repo projectbluefin/review --pr 463 --issues --advisor" "standalone parse-review-args.sh"

# --- 2. Hermetic test of bin/bluefin review (KVM OCI launcher) ----------------

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

mkdir -p "$scratch/bin" "$scratch/home/.config/hive"
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
EOF
export BASH_ENV="$filesystem_hook" HOST_FIXTURE="$host_fixture"
cat >"$scratch/home/.config/hive/contributor.env" <<'EOF'
HIVE_HUB=https://hive.example.test
EOF
mock_podman_log="$scratch/podman.log"
mock_attestation_log="$scratch/attestation.log"
kvm="$scratch/kvm"
touch "$kvm"
chmod 0666 "$kvm"
cat >"$scratch/bin/podman" <<EOF
#!/usr/bin/env bash
[[ "\${1:-}" == info ]] && exit 0
if [[ "\${1:-} \${2:-} \${3:-}" == "system connection list" ]]; then
  [[ "\${FAKE_REMOTE_DEFAULT:-}" != 1 ]] || printf 'remote\tssh://engine.example.test/run/podman.sock\tidentity\ttrue\n'
  exit 0
fi
printf '%s\n' "\$*" >>"$mock_podman_log"
[[ -z "\${FAKE_PODMAN_DELAY:-}" ]] || sleep "\$FAKE_PODMAN_DELAY"
case "\${1:-} \${2:-}" in
  "pull "*) [[ "\${FAKE_PULL_FAIL:-0}" != 1 ]]; exit ;;
  "image exists") [[ "\${FAKE_IMAGE_MISSING:-0}" != 1 ]]; exit ;;
  "image inspect")
    if [[ "\$*" == *'{{.Digest}}'* ]]; then
      printf 'sha256:deadbeef\n'
    else
      printf '%s|0123456789abcdef|sha256:deadbeef\n' "\${FAKE_INSPECT_VERSION:-26.08.07}"
    fi
    exit 0 ;;
esac
exit 0
EOF
chmod +x "$scratch/bin/podman"
cat >"$scratch/bin/krun" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
mock_apptainer_log="$scratch/apptainer.log"
cat >"$scratch/bin/apptainer" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$mock_apptainer_log"
previous=""
for arg in "\$@"; do
  [[ "\$previous" != --home ]] || runtime_home="\${arg%%:*}"
  if [[ "\$previous" == --pwd && "\$arg" == /home/bluefin/workspace ]]; then
    [[ -d "\$runtime_home/workspace" ]] || exit 19
  fi
  previous="\$arg"
done
if [[ "\${EXPECT_APPTAINER_CREDENTIALS:-}" == 1 ]]; then
  injected=()
  for name in GH_TOKEN OPENAI_API_KEY AWS_BEARER_TOKEN_BEDROCK AWS_REGION AWS_DEFAULT_REGION; do
    source_name="APPTAINERENV_\${name}"
    [[ -v "\$source_name" ]] && injected+=("\$name=\${!source_name}")
  done
  if [[ "\${EXPECT_APPTAINER_HIVE:-}" == 1 ]]; then
    [[ "\${APPTAINERENV_HIVE_HUB:-}" == https://hive.example.test ]] || exit 19
  fi
  env -i "\${injected[@]}" /bin/bash -c '
    [[ "\$GH_TOKEN" == mock-token && "\$OPENAI_API_KEY" == test-provider-token ]] || exit 19
    [[ -z "\$AWS_BEARER_TOKEN_BEDROCK" || "\$AWS_BEARER_TOKEN_BEDROCK" == test-bedrock-token ]] || exit 19
    [[ -z "\$AWS_REGION" || "\$AWS_REGION" == us-west-2 ]] || exit 19
    [[ -z "\$AWS_DEFAULT_REGION" || "\$AWS_DEFAULT_REGION" == us-west-2 ]] || exit 19
  ' || exit 19
fi
exit 0
EOF
cat >"$scratch/bin/squashfuse_ll" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$scratch/bin/squashfuse_ll"
export REVIEW_TEST_FUSE_DEVICE=/dev/null
chmod +x "$scratch/bin/apptainer"
# Keep fallback identity checks hermetic even when the host provides skopeo.
cat >"$scratch/bin/skopeo" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$scratch/bin/skopeo"
chmod +x "$scratch/bin/krun"

cat >"$scratch/bin/gh" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == "auth token"* ]]; then
  echo "mock-token"
  exit 0
fi
if [[ "\${1:-} \${2:-}" == "attestation verify" ]]; then
  printf '%s\n' "\$*" >>"$mock_attestation_log"
  [[ "\${FAKE_ATTESTATION_FAIL:-0}" != 1 ]]
  exit
fi
exit 1
EOF
chmod +x "$scratch/bin/gh"

export PATH="$scratch/bin:$PATH"
export HOME="$scratch/home"
export XDG_STATE_HOME="$scratch/home/.local/state"
export REVIEW_TEST_KVM_DEVICE="$kvm"
export GH_TOKEN=mock-token GITHUB_TOKEN=mock-token
unset HIVE_HUB

assert_bluefin_review() {
  local input="$1"
  local expected_flags="$2"
  [[ "$expected_flags" == *--advisor* ]] || expected_flags="$expected_flags --advisor"
  : >"$mock_podman_log"

  # shellcheck disable=SC2086
  "${repo_root}/bin/bluefin" review $input >/dev/null 2>&1 || fail "bin/bluefin review failed for: $input"

  [[ -f "$mock_podman_log" ]] || fail "bin/bluefin review did not invoke podman for: $input"
  local podman_call
  podman_call="$(grep '^run ' "$mock_podman_log")"
  [[ "$podman_call" == *"run --runtime=krun --rm --interactive --tty"* ]] || fail "review did not use the krun OCI runtime: $podman_call"
  [[ "$podman_call" == *"--name bluefin-review-"* ]] || fail "review did not use an isolated instance name: $podman_call"
  [[ "$podman_call" == *":/home/bluefin:rw"* ]] || fail "review did not use target-specific state: $podman_call"
  [[ "$podman_call" == *":/tmp:rw,z"* ]] || fail "review did not use instance-backed scratch storage: $podman_call"

  grep -qFx "pull ghcr.io/projectbluefin/review:stable" "$mock_podman_log" ||
    fail "bin/bluefin review did not refresh the moving stable tag"
  grep -q '^image inspect --format ' "$mock_podman_log" ||
    fail "bin/bluefin review did not inspect the resolved image identity"
  local image="ghcr.io/projectbluefin/review:stable" passed_flags
  passed_flags="${podman_call#*"$image"}"
  passed_flags="$(echo "$passed_flags" | xargs)"
  assert_eq "$passed_flags" "$expected_flags" "bin/bluefin review $input flags"
  if [[ "$passed_flags" == *"projectbluefin/review"* && "$passed_flags" != *"--repo projectbluefin/review"* ]]; then
    fail "shorthand reached the appliance as prompt text: $passed_flags"
  fi
}

: >"$mock_podman_log"
offline_output="$(FAKE_PULL_FAIL=1 "${repo_root}/bin/bluefin" review owner/repo 2>&1)" ||
  fail "packaged review did not use its cached image after refresh failure"
[[ "$offline_output" == *"using the local copy, which may be out of date"* ]] ||
  fail "packaged review did not report its stale cached image"
grep -q '^run ' "$mock_podman_log" || fail "packaged review did not launch its cached image"

: >"$mock_podman_log"
set +e
offline_output="$(FAKE_PULL_FAIL=1 FAKE_IMAGE_MISSING=1 "${repo_root}/bin/bluefin" review owner/repo 2>&1)"
offline_status=$?
set -e
[[ "$offline_status" -ne 0 ]] || fail "packaged review launched without an obtainable image"
[[ "$offline_output" == *"cannot obtain review appliance image"* ]] ||
  fail "packaged review missing-image diagnostic was not actionable: $offline_output"
! grep -q '^run ' "$mock_podman_log" || fail "packaged review ran after image acquisition failed"

# Incompatible review appliance image rejection
: >"$mock_podman_log"
set +e
incompat_output="$(FAKE_INSPECT_VERSION="26.08.05" "${repo_root}/bin/bluefin" review owner/repo 2>&1)"
incompat_status=$?
set -e
[[ "$incompat_status" -ne 0 ]] || fail "packaged review accepted incompatible image version 26.08.05"
[[ "$incompat_output" == *"is incompatible with this launcher (requires >= 26.08.06)"* ]] ||
  fail "incompatible image diagnostic was not actionable: $incompat_output"
! grep -q '^run ' "$mock_podman_log" || fail "packaged review ran after image compatibility check failed"
# Documented review alias selects the same packaged launcher override path.
: >"$mock_podman_log"
REVIEW_APPLIANCE_IMAGE="custom/review:alias" FAKE_INSPECT_VERSION="26.08.07" "${repo_root}/bin/bluefin" review owner/repo >/dev/null 2>&1 ||
  fail "REVIEW_APPLIANCE_IMAGE alias failed to start"
grep -qFx "pull custom/review:alias" "$mock_podman_log" || fail "review alias was not refreshed"
grep -q '^run .* custom/review:alias ' "$mock_podman_log" || fail "review alias was not passed to the container"

# Non-unknown malformed OCI version labels must fail before execution.
for malformed_version in 26.08.foo 26.08.06-rc; do
  : >"$mock_podman_log"
  set +e
  malformed_output="$(FAKE_INSPECT_VERSION="$malformed_version" "${repo_root}/bin/bluefin" review owner/repo 2>&1)"
  malformed_status=$?
  set -e
  [[ "$malformed_status" -ne 0 ]] || fail "packaged review accepted malformed image version $malformed_version"
  [[ "$malformed_output" == *"malformed version label '$malformed_version'"* ]] ||
    fail "malformed review version diagnostic was not actionable: $malformed_output"
  ! grep -q '^run ' "$mock_podman_log" || fail "packaged review ran after malformed version check failed"
done
# Explicit image override warning
: >"$mock_podman_log"
override_output="$(BLUEFIN_REVIEW_IMAGE="custom/review:old" FAKE_INSPECT_VERSION="26.08.05" "${repo_root}/bin/bluefin" review owner/repo 2>&1)" ||
  fail "explicit review image override failed to start"
[[ "$override_output" == *"older than recommended minimum (26.08.06); proceeding with explicit override"* ]] ||
  fail "explicit override warning missing: $override_output"
grep -q '^run ' "$mock_podman_log" || fail "explicit override did not launch container"

# Legacy review state migration from v26.08.05
legacy_review_dir="$scratch/home/.local/state/bluefin-review"
mkdir -p "$legacy_review_dir/.config/review"
touch "$legacy_review_dir/bluefin-review.sif" "$legacy_review_dir/user_session.json" "$legacy_review_dir/.config/review/config.env"
chmod +x "$legacy_review_dir/bluefin-review.sif"
migrate_output="$(HOME="$scratch/home" "${repo_root}/bin/bluefin" review owner/repo 2>&1)" ||
  fail "review with legacy cache failed"
[[ "$migrate_output" == *"migrated user configuration from"* ]] ||
  fail "legacy migration notice was not reported: $migrate_output"
review_instance_home="$(find "$scratch/home/.local/state/bluefin/instances" -type d -path "*review*/home" | head -1)"
[[ -f "$review_instance_home/user_session.json" ]] || fail "user session was not migrated to instance home"
[[ -f "$review_instance_home/.config/review/config.env" ]] || fail "nested configuration was not migrated"
[[ ! -e "$review_instance_home/bluefin-review.sif" ]] || fail "legacy SIF was copied into instance home"
[[ -d "$legacy_review_dir" ]] || fail "legacy review state directory was broadly deleted"
[[ -f "$legacy_review_dir/bluefin-review.sif" ]] || fail "legacy SIF was deleted from legacy directory"
[[ -f "$legacy_review_dir/user_session.json" ]] || fail "original session was deleted from legacy directory"
# A failed copy must abort migration and must not report success.
legacy_failure_item="$legacy_review_dir/migration-failure.json"
touch "$legacy_failure_item"
cat >"$scratch/bin/cp" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == -a ]]; then
  exit 1
fi
exec /bin/cp "$@"
EOF
chmod +x "$scratch/bin/cp"
set +e
migration_failure_output="$(PATH="$scratch/bin:$PATH" BLUEFIN_INSTANCE=migration-failure "${repo_root}/bin/bluefin" review owner/repo 2>&1)"
migration_failure_status=$?
set -e
[[ "$migration_failure_status" -ne 0 ]] || fail "legacy migration continued after cp failure"
[[ "$migration_failure_output" == *"failed to migrate legacy state item"* ]] ||
  fail "legacy migration failure was not actionable: $migration_failure_output"
[[ "$migration_failure_output" != *"migrated user configuration from"* ]] ||
  fail "legacy migration claimed success after cp failure: $migration_failure_output"
rm -f "$scratch/bin/cp"
mv "$scratch/bin/krun" "$scratch/krun"
: >"$mock_apptainer_log"
fallback_output="$(EXPECT_APPTAINER_CREDENTIALS=1 EXPECT_APPTAINER_HIVE=1 OPENAI_API_KEY=test-provider-token REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review projectbluefin/review 2>&1)" || fail "review Apptainer fallback lost credentials"
[[ "$fallback_output" == *"using the isolated Apptainer fallback"* ]] || fail "review fallback warning is missing"
[[ "$fallback_output" == *"✓ bluefin launcher revision:"* ]] || fail "review fallback missing launcher revision: $fallback_output"
[[ "$fallback_output" == *"! review appliance image identity unavailable for ghcr.io/projectbluefin/review:stable."* ]] || fail "review fallback missing identity report without registry probe: $fallback_output"
fallback_call="$(cat "$mock_apptainer_log")"
[[ "$fallback_call" == *"run --containall"* ]] || fail "review fallback did not use Apptainer containment"
[[ "$fallback_call" == *"docker://ghcr.io/projectbluefin/review:stable --repo projectbluefin/review"* ]] || fail "review fallback used the wrong image or scope"
[[ "$fallback_call" == *":/workspace,"*":/tmp"* ]] || fail "review fallback did not bind workspace and instance-backed scratch together"
[[ "$fallback_call" != *mock-token* && "$fallback_call" != *test-provider-token* ]] || fail "fallback leaked credentials into argv"
mv "$scratch/krun" "$scratch/bin/krun"
: >"$mock_podman_log"

# --- Bedrock bearer-token forwarding (regression coverage for #593) ---------
# The Amazon Bedrock provider credential must reach both Review appliance
# execution paths through the environment only, never in argv or launcher output.
BEDROCK_TOKEN="test-bedrock-token"
BEDROCK_REGION="us-west-2"

# Podman/krun path: the named --env entries forward each Bedrock variable.
: >"$mock_podman_log"
bedrock_podman_output="$(AWS_BEARER_TOKEN_BEDROCK="$BEDROCK_TOKEN" AWS_REGION="$BEDROCK_REGION" AWS_DEFAULT_REGION="$BEDROCK_REGION" OPENAI_API_KEY=test-provider-token REVIEW_TEST_KVM_DEVICE="$kvm" "${repo_root}/bin/bluefin" review owner/repo 2>&1)" ||
  fail "review did not launch under KVM with Bedrock credentials set"
bedrock_podman_call="$(grep '^run ' "$mock_podman_log")"
for bedrock_var in AWS_BEARER_TOKEN_BEDROCK AWS_REGION AWS_DEFAULT_REGION; do
  [[ "$bedrock_podman_call" == *"--env $bedrock_var"* ]] || fail "review Podman/krun did not forward $bedrock_var: $bedrock_podman_call"
done
[[ "$bedrock_podman_call" != *"$BEDROCK_TOKEN"* ]] || fail "Bedrock bearer token reached argv in the Podman path"
[[ "$bedrock_podman_output" != *"$BEDROCK_TOKEN"* ]] || fail "Bedrock bearer token leaked into launcher output (Podman path)"

# Apptainer fallback path: APPTAINERENV_ prefixed variables reach the process.
mv "$scratch/bin/krun" "$scratch/krun"
: >"$mock_apptainer_log"
bedrock_apptainer_output="$(AWS_BEARER_TOKEN_BEDROCK="$BEDROCK_TOKEN" AWS_REGION="$BEDROCK_REGION" AWS_DEFAULT_REGION="$BEDROCK_REGION" OPENAI_API_KEY=test-provider-token REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" EXPECT_APPTAINER_CREDENTIALS=1 "${repo_root}/bin/bluefin" review owner/repo 2>&1)" ||
  fail "review Apptainer fallback lost Bedrock credentials"
bedrock_apptainer_call="$(cat "$mock_apptainer_log")"
[[ "$bedrock_apptainer_output" == *"using the isolated Apptainer fallback"* ]] || fail "review fallback warning is missing (Bedrock)"
[[ "$bedrock_apptainer_call" == *"run --containall"* ]] || fail "review fallback did not use Apptainer containment (Bedrock)"
[[ "$bedrock_apptainer_output" != *"$BEDROCK_TOKEN"* ]] || fail "Bedrock bearer token leaked into launcher output (Apptainer path)"
mv "$scratch/krun" "$scratch/bin/krun"
: >"$mock_podman_log"
: >"$mock_apptainer_log"
fallback_output="$(FAKE_REMOTE_DEFAULT=1 "${repo_root}/bin/bluefin" review projectbluefin/review 2>&1)" || fail "default remote connection fallback failed"
[[ "$fallback_output" == *"remote Podman engines are unsupported"* ]] || fail "default remote engine was not diagnosed"
[[ ! -s "$mock_podman_log" ]] || fail "packaged launcher sent host bind mounts to a remote engine"
[[ -s "$mock_apptainer_log" ]] || fail "default remote connection did not use local fallback"

: >"$mock_podman_log"
FAKE_PODMAN_DELAY=0.1 "${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1 &
review_one_pid=$!
FAKE_PODMAN_DELAY=0.1 "${repo_root}/bin/bluefin" review projectbluefin/repo2 >/dev/null 2>&1 &
review_two_pid=$!
wait "$review_one_pid" "$review_two_pid"
mapfile -t concurrent_review_calls < <(grep '^run ' "$mock_podman_log")
assert_eq "${#concurrent_review_calls[@]}" "2" "concurrent review launch count"
assert_eq "$(grep -cFx 'pull ghcr.io/projectbluefin/review:stable' "$mock_podman_log")" "2" "concurrent review refresh count"
first_repo_call="${concurrent_review_calls[0]}"
second_repo_call="${concurrent_review_calls[1]}"
[[ "$(arg_after "$first_repo_call" --name)" != "$(arg_after "$second_repo_call" --name)" ]] || fail "concurrent reviews collided on container name"
[[ "$(arg_after "$first_repo_call" --volume)" != "$(arg_after "$second_repo_call" --volume)" ]] || fail "concurrent reviews collided on state volume"
assert_bluefin_review "projectbluefin/review #463" "--repo projectbluefin/review --pr 463"
assert_bluefin_review "projectbluefin/review#463" "--repo projectbluefin/review --pr 463"
assert_bluefin_review "--issues projectbluefin/review" "--issues --repo projectbluefin/review"
assert_bluefin_review "projectbluefin/review#463 --issues" "--repo projectbluefin/review --pr 463 --issues"
assert_bluefin_review "projectbluefin/review autoslay" "--repo projectbluefin/review --autoslay --advisor"

# --- 2b. Provenance verification gates the published image -------------------

: >"$mock_attestation_log"
"${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1 || fail "bin/bluefin review failed with attestation passing"
grep -qFx 'attestation verify oci://ghcr.io/projectbluefin/review:stable@sha256:deadbeef --repo projectbluefin/review' "$mock_attestation_log" ||
  fail "review launcher did not verify the pulled image digest attestation"

: >"$mock_podman_log"
if FAKE_ATTESTATION_FAIL=1 "${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1; then
  fail "bin/bluefin review ran despite attestation verification failure"
fi
if grep -q '^run ' "$mock_podman_log"; then
  fail "container launched despite attestation verification failure"
fi

: >"$mock_attestation_log"
"${repo_root}/bin/bluefin" contribute >/dev/null 2>&1 || fail "bin/bluefin contribute failed with attestation passing"
grep -qFx 'attestation verify oci://ghcr.io/projectbluefin/contribute:stable@sha256:deadbeef --repo projectbluefin/review' "$mock_attestation_log" ||
  fail "contribute launcher did not verify the pulled image digest attestation"

: >"$mock_attestation_log"
REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1 ||
  fail "apptainer fallback review failed with attestation passing"
grep -qFx 'attestation verify oci://ghcr.io/projectbluefin/review:stable --repo projectbluefin/review' "$mock_attestation_log" ||
  fail "apptainer fallback did not verify the registry ref before pulling"

: >"$mock_apptainer_log"
if FAKE_ATTESTATION_FAIL=1 REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1; then
  fail "apptainer fallback ran despite attestation verification failure"
fi
[[ ! -s "$mock_apptainer_log" ]] || fail "apptainer launched despite attestation verification failure"

: >"$mock_attestation_log"
BLUEFIN_REVIEW_IMAGE="localhost/review:dev" "${repo_root}/bin/bluefin" review projectbluefin/review >/dev/null 2>&1 ||
  fail "localhost override image launch failed"
[[ ! -s "$mock_attestation_log" ]] || fail "localhost override image was sent to attestation verification"

# --- 3. Hermetic test of bin/omp-review (Source launcher) ----------------------

mock_omp_log="$scratch/omp.log"
cat >"$scratch/bin/omp" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$mock_omp_log"
exit 0
EOF
chmod +x "$scratch/bin/omp"

assert_omp_review() {
  local input="$1"
  local expected_flags="$2"
  [[ "$expected_flags" == *--advisor* ]] || expected_flags="$expected_flags --advisor"
  rm -f "$mock_omp_log"

  # shellcheck disable=SC2086
  "${repo_root}/bin/omp-review" $input >/dev/null 2>&1 || fail "bin/omp-review failed for: $input"

  [[ -f "$mock_omp_log" ]] || fail "bin/omp-review did not invoke omp for: $input"
  local omp_call
  omp_call="$(cat "$mock_omp_log")"

  # omp --profile review --extension <path> [FLAGS...]
  local passed_flags
  passed_flags="$(echo "$omp_call" | sed -E 's/^--profile review --extension [^ ]+ ?//' | xargs)"

  assert_eq "$passed_flags" "$expected_flags" "bin/omp-review $input flags"
  if [[ "$passed_flags" == *"projectbluefin/review"* && "$passed_flags" != *"--repo projectbluefin/review"* ]]; then
    fail "shorthand reached omp as prompt text: $passed_flags"
  fi
}

assert_omp_review "projectbluefin/review" "--repo projectbluefin/review"
assert_omp_review "projectbluefin/review #463" "--repo projectbluefin/review --pr 463"
assert_omp_review "projectbluefin/review#463" "--repo projectbluefin/review --pr 463"
assert_omp_review "--issues projectbluefin/review" "--issues --repo projectbluefin/review"
assert_omp_review "projectbluefin/review#463 --issues" "--repo projectbluefin/review --pr 463 --issues"
assert_omp_review "projectbluefin/review autoslay" "--repo projectbluefin/review --autoslay --advisor"

# --- 4. Contributor aliases launch independent KVM appliances -----------------
mkdir -p "$HOME/.config/hive"
printf 'HIVE_REGISTRATION_TOKEN=test\nHIVE_HUB=https://hive.example.test\n' >"$HOME/.config/hive/contributor.env"
printf 'HIVE_REGISTRATION_TOKEN=one\nHIVE_HUB=https://hive.example.test\n' >"$HOME/.config/hive/contributor.owner-repo.env"
printf 'HIVE_REGISTRATION_TOKEN=two\nHIVE_HUB=https://hive.example.test\n' >"$HOME/.config/hive/contributor.owner-repo2.env"
chmod 0600 "$HOME/.config/hive/"contributor*.env
export GH_TOKEN=mock-token

: >"$mock_podman_log"
"${repo_root}/bin/bluefin-contribute" >/dev/null 2>&1 || fail "bluefin-contribute failed"
contribute_alias_call="$(cat "$mock_podman_log")"
[[ "$contribute_alias_call" == *"run --runtime=krun --rm --interactive --tty"* ]] || fail "contribute alias did not use krun"
[[ "$contribute_alias_call" == *"ghcr.io/projectbluefin/contribute:stable"* ]] || fail "contribute alias used the wrong image"
grep -qFx "pull ghcr.io/projectbluefin/contribute:stable" "$mock_podman_log" ||
  fail "bin/bluefin contribute did not refresh the moving stable tag"
grep -q '^image inspect --format ' "$mock_podman_log" ||
  fail "bin/bluefin contribute did not inspect the resolved image identity"

: >"$mock_podman_log"
FAKE_PODMAN_DELAY=0.1 "${repo_root}/bin/bluefin" contribute owner/repo >/dev/null 2>&1 &
contribute_one_pid=$!
FAKE_PODMAN_DELAY=0.1 "${repo_root}/bin/bluefin" contribute owner/repo2 >/dev/null 2>&1 &
contribute_two_pid=$!
wait "$contribute_one_pid" "$contribute_two_pid"
mapfile -t concurrent_contribute_calls < <(grep '^run ' "$mock_podman_log")
assert_eq "${#concurrent_contribute_calls[@]}" "2" "concurrent contribute launch count"
assert_eq "$(grep -cFx 'pull ghcr.io/projectbluefin/contribute:stable' "$mock_podman_log")" "2" "concurrent contributor refresh count"
first_contribute_call="${concurrent_contribute_calls[0]}"
second_contribute_call="${concurrent_contribute_calls[1]}"
[[ "$(arg_after "$first_contribute_call" --name)" != "$(arg_after "$second_contribute_call" --name)" ]] || fail "contributor appliances must have unique container names"
[[ "$(arg_after "$first_contribute_call" --volume)" != "$(arg_after "$second_contribute_call" --volume)" ]] || fail "contributor appliances must have isolated state volumes"
[[ "$first_contribute_call$second_contribute_call" == *"contributor.owner-repo.env:/home/bluefin/.config/hive/contributor.env:ro,z"* ]] || fail "repo contributor did not select its Hive registration"
[[ "$first_contribute_call$second_contribute_call" == *"contributor.owner-repo2.env:/home/bluefin/.config/hive/contributor.env:ro,z"* ]] || fail "repo2 contributor used the wrong registration"

# Incompatible contributor image rejection
: >"$mock_podman_log"
set +e
incompat_contribute_output="$(FAKE_INSPECT_VERSION="26.08.01" "${repo_root}/bin/bluefin" contribute owner/repo 2>&1)"
incompat_contribute_status=$?
set -e
[[ "$incompat_contribute_status" -ne 0 ]] || fail "packaged contribute accepted incompatible image version 26.08.01"
[[ "$incompat_contribute_output" == *"is incompatible with this launcher (requires >= 26.08.02)"* ]] ||
  fail "incompatible contributor image diagnostic was not actionable: $incompat_contribute_output"
! grep -q '^run ' "$mock_podman_log" || fail "packaged contribute ran after image compatibility check failed"
# Documented contributor alias selects the same packaged launcher override path.
: >"$mock_podman_log"
CONTRIBUTE_IMAGE="custom/contribute:alias" FAKE_INSPECT_VERSION="26.08.07" "${repo_root}/bin/bluefin" contribute owner/repo >/dev/null 2>&1 ||
  fail "CONTRIBUTE_IMAGE alias failed to start"
grep -qFx "pull custom/contribute:alias" "$mock_podman_log" || fail "contributor alias was not refreshed"
grep -q '^run .* custom/contribute:alias$' "$mock_podman_log" || fail "contributor alias was not passed to the container"

# Malformed contributor labels must fail before execution as well.
for malformed_version in 26.08.foo 26.08.06-rc; do
  : >"$mock_podman_log"
  set +e
  malformed_contribute_output="$(FAKE_INSPECT_VERSION="$malformed_version" "${repo_root}/bin/bluefin" contribute owner/repo 2>&1)"
  malformed_contribute_status=$?
  set -e
  [[ "$malformed_contribute_status" -ne 0 ]] || fail "packaged contribute accepted malformed image version $malformed_version"
  [[ "$malformed_contribute_output" == *"malformed version label '$malformed_version'"* ]] ||
    fail "malformed contributor version diagnostic was not actionable: $malformed_contribute_output"
  ! grep -q '^run ' "$mock_podman_log" || fail "packaged contribute ran after malformed version check failed"
done
# Explicit contributor image override warning
: >"$mock_podman_log"
override_contribute_output="$(BLUEFIN_CONTRIBUTE_IMAGE="custom/contribute:old" FAKE_INSPECT_VERSION="26.08.01" "${repo_root}/bin/bluefin" contribute owner/repo 2>&1)" ||
  fail "explicit contributor image override failed to start"
[[ "$override_contribute_output" == *"older than recommended minimum (26.08.02); proceeding with explicit override"* ]] ||
  fail "explicit contributor override warning missing: $override_contribute_output"
grep -q '^run ' "$mock_podman_log" || fail "explicit contributor override did not launch container"

# Legacy contribute state migration from v26.08.05
legacy_contribute_dir="$scratch/home/.local/state/bluefin-contribute"
mkdir -p "$legacy_contribute_dir/.config/contribute"
touch "$legacy_contribute_dir/bluefin-contribute.sif" "$legacy_contribute_dir/contribute_session.json" "$legacy_contribute_dir/.config/contribute/config.env"
chmod +x "$legacy_contribute_dir/bluefin-contribute.sif"
migrate_contribute_output="$(HOME="$scratch/home" "${repo_root}/bin/bluefin" contribute owner/repo 2>&1)" ||
  fail "contribute with legacy cache failed"
[[ "$migrate_contribute_output" == *"migrated user configuration from"* ]] ||
  fail "legacy contribute migration notice was not reported: $migrate_contribute_output"
contribute_instance_home="$(find "$scratch/home/.local/state/bluefin/instances" -type d -path "*contribute-owner-repo-[0-9a-f]*/home" | head -1)"
[[ -f "$contribute_instance_home/contribute_session.json" ]] || fail "contribute user session was not migrated to instance home"
[[ -f "$contribute_instance_home/.config/contribute/config.env" ]] || fail "nested contribute configuration was not migrated"
[[ ! -e "$contribute_instance_home/bluefin-contribute.sif" ]] || fail "legacy contribute SIF was copied into instance home"
[[ -d "$legacy_contribute_dir" ]] || fail "legacy contribute state directory was broadly deleted"
# A contributor does the work of whichever hive its registration names, and that
# choice used to be invisible: a default registration written by another
# project's contribute-setup routed every bare launch to that project's queue.
printf 'HIVE_REGISTRATION_TOKEN=two\nHIVE_HUB=wss://other.example.test/contribute\n' >"$HOME/.config/hive/contributor.owner-repo2.env"
chmod 0600 "$HOME/.config/hive/contributor.owner-repo2.env"
: >"$mock_podman_log"
hive_notice="$("${repo_root}/bin/bluefin" contribute owner/repo2 2>&1 >/dev/null)" || fail "contributor launch failed"
[[ "$hive_notice" == *"hive: wss://other.example.test/contribute (contributor.owner-repo2.env)"* ]] ||
  fail "contributor launch did not name the hive it joins: ${hive_notice}"

printf 'HIVE_REGISTRATION_TOKEN=two\n' >"$HOME/.config/hive/contributor.owner-repo2.env"
chmod 0600 "$HOME/.config/hive/contributor.owner-repo2.env"
: >"$mock_podman_log"
set +e
hubless_output="$("${repo_root}/bin/bluefin" contribute owner/repo2 2>&1)"
hubless_status=$?
set -e
[[ "$hubless_status" -ne 0 ]] || fail "contributor launched from a registration with no HIVE_HUB"
[[ "$hubless_output" == *"has no usable HIVE_HUB"* ]] || fail "hub-less registration error is unexplained"
! grep -q '^run ' "$mock_podman_log" || fail "hub-less registration still started a container"
printf 'HIVE_REGISTRATION_TOKEN=two\nHIVE_HUB=https://hive.example.test\n' >"$HOME/.config/hive/contributor.owner-repo2.env"
chmod 0600 "$HOME/.config/hive/contributor.owner-repo2.env"

set +e
setup_output="$(REVIEW_NON_INTERACTIVE=true "${repo_root}/bin/bluefin" setup owner/repo 2>&1)"
setup_status=$?
set -e
[[ "$setup_status" -ne 0 ]] || fail "non-interactive setup unexpectedly replaced a registration"
[[ "$setup_output" == *"non-interactive mode cannot answer"* ]] || fail "setup did not explain its attended registration requirement"
grep -qF 'HIVE_REGISTRATION_TOKEN=one' "$HOME/.config/hive/contributor.owner-repo.env" || fail "failed setup did not restore the prior registration"
[[ ! -e "$HOME/.config/hive/contributor.owner-repo.env.bak" ]] || fail "failed setup left a registration backup behind"

mv "$scratch/bin/krun" "$scratch/krun"
: >"$mock_apptainer_log"
fallback_output="$(EXPECT_APPTAINER_CREDENTIALS=1 OPENAI_API_KEY=test-provider-token "${repo_root}/bin/bluefin" contribute owner/repo 2>&1)" || fail "contributor Apptainer fallback lost credentials"
[[ "$fallback_output" == *"using the isolated Apptainer fallback"* ]] || fail "contributor fallback warning is missing"
[[ "$fallback_output" == *"✓ bluefin launcher revision:"* ]] || fail "contributor fallback missing launcher revision: $fallback_output"
[[ "$fallback_output" == *"! contributor image identity unavailable for ghcr.io/projectbluefin/contribute:stable."* ]] || fail "contributor fallback missing identity report without registry probe: $fallback_output"
fallback_call="$(cat "$mock_apptainer_log")"
[[ "$fallback_call" == *"run --containall"* ]] || fail "contributor fallback did not use Apptainer containment"
[[ "$fallback_call" == *"docker://ghcr.io/projectbluefin/contribute:stable"* ]] || fail "contributor fallback used the wrong image"

for mask in 0 1 2 3; do
  configure_host_files "$mask"
  : >"$mock_apptainer_log"
  REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review owner/repo >/dev/null 2>&1 ||
    fail "review fallback failed for host-file mask $mask"
  assert_apptainer_host_files "$(cat "$mock_apptainer_log")" "$mask"

  : >"$mock_apptainer_log"
  REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" contribute >/dev/null 2>&1 ||
    fail "contributor fallback failed for host-file mask $mask"
  assert_apptainer_host_files "$(cat "$mock_apptainer_log")" "$mask"
done
configure_host_files 2
ln -s missing-zoneinfo "$host_fixture/etc/localtime"
: >"$mock_apptainer_log"
REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review owner/repo >/dev/null 2>&1 ||
  fail "review fallback failed with dangling localtime"
assert_apptainer_host_files "$(cat "$mock_apptainer_log")" 2
: >"$mock_apptainer_log"
REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" contribute >/dev/null 2>&1 ||
  fail "contributor fallback failed with dangling localtime"
assert_apptainer_host_files "$(cat "$mock_apptainer_log")" 2
: >"$mock_apptainer_log"
REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin-contribute" >/dev/null 2>&1 ||
  fail "contributor wrapper fallback failed with dangling localtime"
assert_apptainer_host_files "$(cat "$mock_apptainer_log")" 2

mv "$scratch/bin/squashfuse_ll" "$scratch/squashfuse_ll"
set +e
fallback_output="$(env PATH="$scratch/bin:/usr/bin:/bin" HOME="$HOME" GH_TOKEN="$GH_TOKEN" GITHUB_TOKEN="$GITHUB_TOKEN" BASH_ENV="$BASH_ENV" HOST_FIXTURE="$HOST_FIXTURE" REVIEW_TEST_FUSE_DEVICE="$REVIEW_TEST_FUSE_DEVICE" REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" review owner/repo 2>&1)"
fallback_status=$?
set -e
[[ "$fallback_status" -ne 0 ]] || fail "review fallback accepted missing squashfuse"
[[ "$fallback_output" == *"squashfuse"* ]] || fail "missing squashfuse diagnostic was not actionable: $fallback_output"
mv "$scratch/squashfuse_ll" "$scratch/bin/squashfuse_ll"

set +e
fallback_output="$(REVIEW_TEST_FUSE_DEVICE="$scratch/missing-fuse" REVIEW_TEST_KVM_DEVICE="$scratch/missing-kvm" "${repo_root}/bin/bluefin" contribute 2>&1)"
fallback_status=$?
set -e
[[ "$fallback_status" -ne 0 ]] || fail "contributor fallback accepted a missing FUSE device"
[[ "$fallback_output" == *"FUSE device"* ]] || fail "missing FUSE diagnostic was not actionable: $fallback_output"
mv "$scratch/krun" "$scratch/bin/krun"

# --- 5. Parity test: KVM container and source launchers use identical flags ---

for case in "${test_cases[@]}"; do
  input="${case%%|*}"
  expected="${case#*|}"
  assert_bluefin_review "$input" "$expected"
  assert_omp_review "$input" "$expected"
done

# --- 6. Credential-resolution parity across launchers -------------------------

launchers=(bluefin omp-review)

extract_keyring_reader() {
  awk '/state_dir = os.environ.get\("BLUEFIN_OMP_STATE"\)/,/^'"'"' 2>\/dev\/null/' "$1" |
    sed -e '$d' -e 's/[[:space:]]*$//'
}

for name in "${launchers[@]}"; do
  block="$(extract_keyring_reader "${repo_root}/bin/${name}")"
  [[ -n "$block" ]] || fail "bin/${name}: no omp-keyring reader found"
  grep -qF 'BLUEFIN_OMP_STATE' <<<"$block" ||
    fail "bin/${name}: omp-keyring reader ignores the BLUEFIN_OMP_STATE override"
done

# Functional test: verify BLUEFIN_OMP_STATE is honored when GH_TOKEN is unset
custom_state="$scratch/custom-omp-state"
mkdir -p "$custom_state/agent"
python3 -c "
import sqlite3, json
conn = sqlite3.connect('$custom_state/agent/agent.db')
conn.execute('CREATE TABLE auth_credentials (provider TEXT, data TEXT)')
conn.execute('INSERT INTO auth_credentials VALUES (?, ?)', ('github-copilot', json.dumps({'access_token': 'custom-omp-token'})))
conn.commit()
conn.close()
"

mock_cred_bin="$scratch/cred-bin"
mkdir -p "$mock_cred_bin"
cat >"$mock_cred_bin/podman" <<'EOF'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "info "|"pull "*|"image exists") exit 0 ;;
  "run "*) echo "$GH_TOKEN $COPILOT_INTEGRATION_ID"; exit 0 ;;
esac
exit 0
EOF
chmod +x "$mock_cred_bin/podman"

cat >"$mock_cred_bin/krun" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$mock_cred_bin/krun"

cat >"$mock_cred_bin/omp" <<'EOF'
#!/usr/bin/env bash
echo "$GH_TOKEN $COPILOT_INTEGRATION_ID"
exit 0
EOF
chmod +x "$mock_cred_bin/omp"

# This credential PATH deliberately keeps /usr/bin so bin/bluefin can reach
# coreutils, but CI runners also ship a real gh there. Without a stub ahead of
# it, verify_image_provenance would run a real `gh attestation verify` against
# the network and abort a test that is only about credential resolution.
cat >"$mock_cred_bin/gh" <<'EOF'
#!/usr/bin/env bash
# Only the provenance check is neutralized. `gh auth token` must still fail so
# the credential precedence under test (BLUEFIN_OMP_STATE wins) is unchanged.
case "${1:-} ${2:-}" in
  "attestation verify") exit 0 ;;
esac
exit 1
EOF
chmod +x "$mock_cred_bin/gh"

bluefin_cred_out="$(env -i PATH="$mock_cred_bin:/usr/bin:/bin" HOME="$scratch/home" BLUEFIN_OMP_STATE="$custom_state" REVIEW_TEST_KVM_DEVICE="$kvm" "${repo_root}/bin/bluefin" review projectbluefin/review 2>/dev/null)" || fail "bin/bluefin credential test failed"
assert_eq "$bluefin_cred_out" "custom-omp-token copilot-developer-cli" "bin/bluefin resolves BLUEFIN_OMP_STATE and COPILOT_INTEGRATION_ID"

omp_cred_out="$(env -i PATH="$mock_cred_bin:/usr/bin:/bin" HOME="$scratch/home" BLUEFIN_OMP_STATE="$custom_state" "${repo_root}/bin/omp-review" projectbluefin/review 2>/dev/null)" || fail "bin/omp-review credential test failed"
assert_eq "$omp_cred_out" "custom-omp-token copilot-developer-cli" "bin/omp-review resolves BLUEFIN_OMP_STATE and COPILOT_INTEGRATION_ID"

echo "launcher-contract: all shorthand forms and launcher parity assertions passed"
