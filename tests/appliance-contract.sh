#!/usr/bin/env bash
# Contract for the Bluefin Review appliance image.
#
# Two halves. The static half reads the Containerfile and the version machinery
# and needs no container engine, so it runs on every commit. The runtime half
# runs the built image and needs one; CI always passes --image, and locally it
# is skipped unless you pass one too.
#
#   bash tests/appliance-contract.sh
#   bash tests/appliance-contract.sh --image localhost/projectbluefin/review:dev
#
# The runtime half exists because this image is assembled rather than installed:
# a missing shared library or a pruned file does not fail the build, it fails in
# a maintainer's terminal three days later.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

containerfile="image/appliance/Containerfile"
image=""
expect_arch=""
expect_version=""
# The appliance carries OMP and review tools, not an alternate agent runtime.
size_ceiling_bytes=$((500 * 1024 * 1024))

while (($#)); do
  case "$1" in
  --image)
    image="${2:?--image needs a reference}"
    shift 2
    ;;
  --expect-arch)
    expect_arch="${2:?--expect-arch needs a value}"
    shift 2
    ;;
  --expect-version)
    expect_version="${2:?--expect-version needs a value}"
    shift 2
    ;;
  *)
    echo "appliance-contract: unknown argument $1" >&2
    exit 2
    ;;
  esac
done

fail() {
  echo "appliance-contract: $*" >&2
  exit 1
}

require() {
  local path="$1"
  shift
  local needle
  for needle in "$@"; do
    grep -qF -- "$needle" "$path" || fail "${path} must contain: ${needle}"
  done
}

forbid() {
  local path="$1"
  shift
  local needle
  for needle in "$@"; do
    grep -qF -- "$needle" "$path" && fail "${path} must not contain: ${needle}"
  done
  return 0
}

# ------------------------------------------------------------------- static

# Both FSDK images are pinned as tag *and* digest: the digest is what builds,
# the tag is what Renovate can compare against.
grep -qE '^ARG FSDK_BASE_IMAGE=ghcr\.io/projectbluefin/base:[^@[:space:]]+@sha256:[0-9a-f]{64}$' "$containerfile" ||
  fail "FSDK_BASE_IMAGE must be pinned as tag@sha256 digest"
grep -qE '^ARG FSDK_BUILDER_IMAGE=ghcr\.io/projectbluefin/lab-runner:[^@[:space:]]+@sha256:[0-9a-f]{64}$' "$containerfile" ||
  fail "FSDK_BUILDER_IMAGE must be pinned as tag@sha256 digest"

# Every fetched artifact carries a per-architecture digest. A download this
# build cannot verify is a download it must not execute.
for pin in OMP_X86_64_SHA256 OMP_AARCH64_SHA256 GH_X86_64_SHA256 GH_AARCH64_SHA256; do
  grep -qE "^ARG ${pin}=[0-9a-f]{64}$" "$containerfile" ||
    fail "ARG ${pin} must be a lowercase sha256 digest"
done

# shellcheck disable=SC2016 # Literal Containerfile text, not shell expansions.
require "$containerfile" \
  'FROM ${FSDK_BUILDER_IMAGE} AS build' \
  'FROM ${FSDK_BASE_IMAGE}' \
  'sha256sum --check --status' \
  'USER 65532:65532' \
  'WORKDIR /workspace' \
  'ENTRYPOINT ["/usr/bin/bluefin-review-appliance"]' \
  'COPY --chmod=0755 image/appliance/entrypoint.sh /out/usr/bin/bluefin-review-appliance' \
  'COPY image/appliance/config.yml /out/usr/share/bluefin/review/appliance-config.yml' \
  'io.projectbluefin.review.appliance="true"' \
  'org.opencontainers.image.version="${REVIEW_VERSION}"' \
  'org.opencontainers.image.revision="${REVIEW_REVISION}"'
require image/appliance/config.yml 'advisor: "@default"' 'syncBacklog: 1'
python3 - image/extension/bluefin-review/.mcp.json <<'PY' || fail "bundled MCP configuration is invalid"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    servers = json.load(stream)["mcpServers"]

expected = {
    "github": "https://api.githubcopilot.com/mcp/",
    "bluefin": "https://mcp.projectbluefin.io/mcp",
    "context7": "https://mcp.context7.com/mcp",
}
assert set(servers) == set(expected)
for name, url in expected.items():
    assert servers[name]["type"] == "http"
    assert servers[name]["url"] == url
    assert servers[name]["enabled"] is True
assert "GH_TOKEN" in servers["github"]["headers"]["Authorization"]
assert "CONTEXT7_API_KEY" in servers["context7"]["headers"]["Authorization"]
PY
require image/appliance/stage-runtime.sh '/usr/bin/gzip.bin'
require "$containerfile" \
  'GIT_CONFIG_KEY_0=credential.https://github.com.helper' \
  'GIT_CONFIG_VALUE_0="!/usr/bin/gh auth git-credential"'
for tool in actionlint shellcheck yq jq just openssl; do
  grep -qF "/usr/sbin/${tool}" "$containerfile" ||
    fail "${tool} must be staged from the pinned FSDK builder"
done

# The point of a distroless appliance is that nothing inside it can install
# anything. Not one of these may appear, in any stage that reaches the image.
forbid "$containerfile" \
  'dnf install' \
  'apt-get' \
  'apk add' \
  'pip install' \
  'RUN curl | ' \
  'curl -sL |'
forbid "$containerfile" 'PI_VERSION' 'NODE_VERSION' 'pi-coding-agent' '/usr/bin/pi' '/usr/bin/node'

# `SHELL` is silently ignored for the OCI image format; a RUN that relies on it
# for `set -e` is a RUN whose failures are invisible.
grep -qE '^SHELL ' "$containerfile" && fail "SHELL is ignored under --format oci; set options inside each RUN"

version="$(bash scripts/review-appliance-version.sh)"
[[ "$version" =~ ^[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]] ||
  fail "version must be <fsdk-series>.<MM>, got '${version}'"

# The FSDK series is read from the pinned base, never written down twice.
base_series="$(sed -nE 's/^ARG FSDK_BASE_IMAGE=.*base:([0-9]{2}\.[0-9]{2}).*/\1/p' "$containerfile")"
[[ "$version" == "${base_series}."* ]] ||
  fail "version '${version}' does not track the pinned FSDK series '${base_series}'"

[[ -f image/appliance/REVISION ]] || fail "image/appliance/REVISION is missing"
grep -qE '^[0-9]+$' image/appliance/REVISION || fail "image/appliance/REVISION must hold a single integer"

# The mode the image exists to run has to be in the build context.
[[ -f image/extension/bluefin-review/index.ts ]] || fail "the review mode entry point is missing"
[[ -d image/extension/bluefin-review/agents ]] || fail "the companion agents are missing"
grep -qF '!scripts/generate-appliance-sbom.py' .dockerignore ||
  fail ".dockerignore must let the appliance SBOM generator into the build context"

# The generator that fills that SBOM. Its own contract runs here rather than as
# a separate validate.yml step: the document it writes is part of this image's
# contract, and the runtime half below only reads it when --image is given.
python3_contract_output="$(python3 tests/appliance_sbom_contract.py 2>&1)" || {
  printf '%s\n' "$python3_contract_output" >&2
  fail "tests/appliance_sbom_contract.py failed"
}

require .github/workflows/publish-appliance.yml \
  'scripts/review-appliance-version.sh' \
  'tests/appliance-contract.sh' \
  'IMAGE: ghcr.io/projectbluefin/review'

# Exercise the launcher independently of a container engine. A fake omp makes
# the profile boundary observable and proves that update never reaches omp.
entrypoint_tmp="$(mktemp -d)"
trap 'rm -rf "$entrypoint_tmp"' EXIT
cat >"$entrypoint_tmp/omp" <<'EOF'
#!/usr/bin/bash
printf '%s\n' "$@"
EOF
chmod +x "$entrypoint_tmp/omp"
default_args="$(PATH="$entrypoint_tmp:$PATH" image/appliance/entrypoint.sh --version)"
grep -qx 'bluefin-review-appliance' <<<"$default_args" ||
  fail "the appliance entrypoint did not select its isolated profile"
[[ "$(grep -cx -- '--advisor' <<<"$default_args")" -eq 1 ]] ||
  fail "the appliance did not enable exactly one OMP advisor"
inherited_args="$(BLUEFIN_REVIEW_INHERIT_OMP_CONFIG=1 PATH="$entrypoint_tmp:$PATH" image/appliance/entrypoint.sh --version)"
grep -qx 'review' <<<"$inherited_args" ||
  fail "the explicit host omp configuration opt-in did not select the review profile"
autoslay_args="$(PATH="$entrypoint_tmp:$PATH" image/appliance/entrypoint.sh --autoslay)"
[[ "$(grep -cx -- '--advisor' <<<"$autoslay_args")" -eq 1 ]] ||
  fail "autoslay duplicated the always-on OMP advisor"
grep -qx -- '--autoslay' <<<"$autoslay_args" ||
  fail "autoslay flag did not reach the review extension"
explicit_advisor_args="$(PATH="$entrypoint_tmp:$PATH" image/appliance/entrypoint.sh --advisor)"
[[ "$(grep -cx -- '--advisor' <<<"$explicit_advisor_args")" -eq 1 ]] ||
  fail "the appliance duplicated an explicit OMP advisor flag"
if PATH="$entrypoint_tmp:$PATH" image/appliance/entrypoint.sh update >"$entrypoint_tmp/update.out" 2>&1; then
  fail "the immutable appliance accepted an in-place update"
fi
grep -q 'immutable appliance' "$entrypoint_tmp/update.out" ||
  fail "the rejected update did not explain appliance replacement"
rm -rf "$entrypoint_tmp"
trap - EXIT

if [[ -n "$expect_version" && "$expect_version" != "$version" ]]; then
  fail "expected version ${expect_version}, derived ${version}"
fi

echo "appliance-contract: static contract holds (version ${version})"

# ------------------------------------------------------------------ runtime

if [[ -z "$image" ]]; then
  echo "appliance-contract: no --image given; runtime checks skipped"
  exit 0
fi

engine="${CONTAINER_ENGINE:-podman}"
command -v "$engine" >/dev/null 2>&1 || fail "${engine} is required for runtime checks"

inspect() {
  "$engine" image inspect "$image" --format "$1"
}

# The body runs in the container's shell, so its expansions must survive this
# one untouched. Every caller below quotes accordingly.
# shellcheck disable=SC2016
run() {
  "$engine" run --rm --entrypoint /usr/bin/bash "$image" -c "$1"
}

test "$(inspect '{{.Config.User}}')" = "65532:65532" ||
  fail "image must run as the numeric nonroot uid; kubelet rejects named users under runAsNonRoot"
test "$(inspect '{{.Config.WorkingDir}}')" = "/workspace"
test "$(inspect '{{json .Config.Entrypoint}}')" = \
  '["/usr/bin/bluefin-review-appliance"]'
test "$(inspect '{{.ManifestType}}')" = "application/vnd.oci.image.manifest.v1+json" ||
  fail "the shipped format is OCI, not Docker schema 2"

image_version="$(inspect '{{index .Labels "org.opencontainers.image.version"}}')"
test "$image_version" = "$version" ||
  fail "image label version '${image_version}' does not match derived '${version}'"
test "$(inspect '{{index .Labels "io.projectbluefin.review.appliance"}}')" = "true"

# Sum the layer sizes rather than reading `.Size`: podman's inspect field
# double-counts files a later layer replaces, and the number a maintainer sees
# in `podman images` is the sum of the diffs.
size="$("$engine" history --format json "$image" | python3 -c '
import json, sys
print(sum(int(layer.get("size") or 0) for layer in json.load(sys.stdin)))
')"
[[ "$size" =~ ^[0-9]+$ ]] || fail "could not measure the image size"
if ((size > size_ceiling_bytes)); then
  fail "image is $((size / 1024 / 1024)) MiB, over the $((size_ceiling_bytes / 1024 / 1024)) MiB ceiling"
fi

if [[ -n "$expect_arch" ]]; then
  container_arch="$("$engine" run --rm --entrypoint /usr/bin/uname "$image" -m)"
  test "$container_arch" = "$expect_arch" ||
    fail "expected a native ${expect_arch} container, got ${container_arch}"
fi

# Every bundled binary is executed, because "the file exists" says nothing about
# whether its library closure came along.
# omp reports itself as "omp/<version>"; the label carries the bare version.
omp_label="$(inspect '{{index .Labels "io.projectbluefin.review.omp.version"}}')"
omp_version="$(run 'omp --version')"
test "$omp_version" = "omp/${omp_label}" ||
  fail "the omp binary reports '${omp_version}', but this image claims to ship ${omp_label}"

# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  gh --version >/dev/null
  git --version >/dev/null
  python3 --version >/dev/null
  python --version >/dev/null
  actionlint -version >/dev/null
  shellcheck --version >/dev/null
  yq --version >/dev/null
  jq --version >/dev/null
  just --version >/dev/null
  gzip --version >/dev/null
  test "$(git config --get credential.https://github.com.helper)" = "!/usr/bin/gh auth git-credential"
  test "$(readlink -f /bin/sh)" = /usr/bin/bash
' >/dev/null || fail "a bundled binary failed to execute"

# git is here to land fixes, which means it has to be able to commit and to
# reach GitHub over https — the remote helper and its TLS closure included.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  export HOME=/tmp/gitcheck GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com
  export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
  mkdir -p "$HOME/repo" && cd "$HOME/repo"
  git init -q .
  echo hello > file
  git add file
  git commit -qm "smoke"
  git log --oneline | grep -q smoke
  test -x /usr/libexec/git-core/git-remote-https
  ldd /usr/libexec/git-core/git-remote-https | grep -q "not found" && exit 1
  exit 0
' >/dev/null || fail "git cannot commit, or its https remote helper is missing libraries"

# The agent writes sessions, logs and caches under HOME on every run.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run 'set -eu; test -w "$HOME"; test "$HOME" = /home/bluefin' >/dev/null ||
  fail "HOME must exist and be writable by the nonroot user"

# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  test -x /usr/bin/bluefin-review-appliance
  grep -q "checkUpdate: false" /usr/share/bluefin/review/appliance-config.yml
  test -f /usr/share/bluefin/review/extension/index.ts
  test -d /usr/share/bluefin/review/extension/agents
  test -f /usr/share/bluefin/review/extension/.mcp.json
  test -f /usr/share/bluefin/review/sbom.spdx.json
' >/dev/null || fail "the review mode or its SBOM is missing from the image"

# Nothing inside may install anything.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  for forbidden in dnf apt apt-get apk rpm yum pip pip3 npm; do
    if command -v "$forbidden" >/dev/null 2>&1; then
      echo "found package manager: $forbidden" >&2
      exit 1
    fi
  done
' >/dev/null || fail "a package manager reached the appliance"

sbom_packages="$(run 'cat /usr/share/bluefin/review/sbom.spdx.json' | python3 -c '
import json,sys
document = json.load(sys.stdin)
print(" ".join(sorted(package["name"] for package in document["packages"])))
')"
for component in omp gh bluefin-review-mode; do
  grep -qF -- "$component" <<<"$sbom_packages" ||
    fail "the in-image SBOM does not record ${component}"
done

# The deliverable itself: the shipped entrypoint, with its profile and extension
# arguments, starting under the nonroot user. Extension *behavior* is covered by
# tests/omp-review-mode.sh; what this proves is that the image's own command line
# is well formed and the binary runs as shipped.
entry_version="$("$engine" run --rm "$image" --version)"
test "$entry_version" = "$omp_label" ||
  fail "the entrypoint printed '${entry_version}', expected ${omp_label}"

update_output="$("$engine" run --rm "$image" update 2>&1 || true)"
grep -q 'immutable appliance' <<<"$update_output" ||
  fail "the appliance update command did not explain replacement semantics"

help_output="$("$engine" run --rm "$image" --help)"
grep -q 'Replace it to update' <<<"$help_output" ||
  fail "appliance help does not explain replacement semantics"
grep -q 'BLUEFIN_REVIEW_INHERIT_OMP_CONFIG=1' <<<"$help_output" ||
  fail "appliance help does not expose the explicit host-config opt-in"

# An actual packaged autoslay launch with only modelRoles.default persisted
# must start an advisor resolved through the role alias to that default model.
# The advisor must be observable in its normal transcript without reporting inactive.
# shellcheck disable=SC2016 # Expanded by the container's shell, not this one.
run '
  set -eu
  python3 - <<"PY"
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        data = json.loads(body.decode("utf-8"))
        resp = {
            "id": "chatcmpl-contract",
            "object": "chat.completion",
            "created": 1677652288,
            "model": data.get("model", "mock-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13}
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode("utf-8"))

    def log_message(self, format, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = os.path.join(tmpdir, ".omp/profiles/bluefin-review-appliance/agent")
        os.makedirs(agent_dir, exist_ok=True)

        with open(os.path.join(agent_dir, "models.yml"), "w", encoding="utf-8") as f:
            f.write(f"""providers:
  contract-mock:
    baseUrl: http://127.0.0.1:{port}/v1
    api: openai-completions
    apiKey: dummy
    models:
      - id: mock-model
        name: Mock Model
        contextWindow: 100000
        maxTokens: 32000
""")

        with open(os.path.join(agent_dir, "config.yml"), "w", encoding="utf-8") as f:
            f.write("""modelRoles:
  default: contract-mock/mock-model
""")

        cmd = [
            "/usr/bin/bluefin-review-appliance",
            "--autoslay",
            "-p",
            "--max-time", "15s",
            "Reply exactly: ok"
        ]
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["GH_TOKEN"] = "dummy"

        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            print("Appliance autoslay run failed:", res.returncode, file=sys.stderr)
            print("STDOUT:", res.stdout, file=sys.stderr)
            print("STDERR:", res.stderr, file=sys.stderr)
            sys.exit(1)

        advisor_transcripts = []
        for root, dirs, files in os.walk(tmpdir):
            for file in files:
                path = os.path.join(root, file)
                if file == "__advisor.jsonl":
                    advisor_transcripts.append(path)
                elif file.endswith(".log") and file.startswith("omp."):
                    with open(path, encoding="utf-8") as fp:
                        for line in fp:
                            lower = line.lower()
                            if "advisor inactive" in lower or "no model assigned" in lower:
                                print(f"Log reported inactive advisor: {line}", file=sys.stderr)
                                sys.exit(1)

        if not advisor_transcripts:
            print("No __advisor.jsonl found in session", file=sys.stderr)
            sys.exit(1)

        found_resolved_model = False
        for transcript in advisor_transcripts:
            with open(transcript, encoding="utf-8") as fp:
                for line in fp:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if msg.get("role") == "assistant":
                        if msg.get("provider") == "contract-mock" and msg.get("model") == "mock-model":
                            found_resolved_model = True
                            break

        if not found_resolved_model:
            print("Advisor did not record assistant message with resolved default model", file=sys.stderr)
            sys.exit(1)
finally:
    server.shutdown()
PY
' >/dev/null || fail "autoslay advisor failed to resolve role alias to user default model"

echo "appliance-contract: runtime contract holds ($((size / 1024 / 1024)) MiB, omp ${omp_version})"
