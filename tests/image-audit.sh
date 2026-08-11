#!/usr/bin/env bash
# Reproducible evidence for the exact FSDK input and a derived review image.
#
# This intentionally does not build an image or run a foreign architecture.
# Callers build or publish first, then pass the immutable derived reference.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

base_image="$(sed -n 's/^ARG FSDK_RUNNER_IMAGE=\(.*\)$/\1/p' image/Containerfile)"
derived_image=""
report_file=""
require_oci=false
require_attestations=false
require_github_attestation=false
verify_base_evidence=false
attestation_repository=""
expected_source=""
expected_revision=""
expected_version=""
engine="${CONTAINER_ENGINE:-docker}"

usage() {
  cat <<'EOF'
Usage: tests/image-audit.sh --derived IMAGE [options]

Audit the digest-pinned FSDK base and an already-built or published review
image. IMAGE may be a local Docker tag or an immutable registry reference.

Options:
  --base IMAGE             Override the FSDK base parsed from image/Containerfile.
  --derived IMAGE          Built review image to inspect (required).
  --require-oci            Require source, revision, and version OCI labels.
  --require-attestations   Require SPDX SBOM and SLSA provenance attestations.
  --require-github-attestation
                           Verify GitHub artifact provenance for the digest.
  --attestation-repository OWNER/REPO
                           Repository expected to have created the artifact attestation.
  --expected-source URL    Require matching OCI source and URL values.
  --expected-revision SHA  Require matching OCI revision value.
  --expected-version TAG   Require matching OCI version value.
  --verify-base-evidence   Verify FSDK provenance and exactly linux/amd64+linux/arm64.
  --report FILE            Also write the Markdown report to FILE. The report
                           is generated output; keep it out of git.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --base)
    base_image="${2:?--base needs an image reference}"
    shift 2
    ;;
  --derived)
    derived_image="${2:?--derived needs an image reference}"
    shift 2
    ;;
  --require-oci)
    require_oci=true
    shift
    ;;
  --require-attestations)
    require_attestations=true
    shift
    ;;
  --require-github-attestation)
    require_github_attestation=true
    shift
    ;;
  --attestation-repository)
    attestation_repository="${2:?--attestation-repository needs OWNER/REPO}"
    shift 2
    ;;
  --expected-source)
    expected_source="${2:?--expected-source needs a URL}"
    shift 2
    ;;
  --expected-revision)
    expected_revision="${2:?--expected-revision needs a revision}"
    shift 2
    ;;
  --expected-version)
    expected_version="${2:?--expected-version needs a version}"
    shift 2
    ;;
  --verify-base-evidence)
    verify_base_evidence=true
    shift
    ;;
  --report)
    report_file="${2:?--report needs a path}"
    shift 2
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
done

[[ -n "$base_image" ]] || {
  echo "could not read FSDK_RUNNER_IMAGE from image/Containerfile" >&2
  exit 1
}
if ! "$verify_base_evidence"; then
  [[ -n "$derived_image" ]] || {
    echo "--derived is required" >&2
    exit 2
  }
fi
if { "$require_attestations" || "$require_github_attestation"; } &&
  [[ ! "$derived_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "attestation verification needs an immutable derived image digest" >&2
  exit 2
fi
if "$require_github_attestation" && [[ -z "$attestation_repository" ]]; then
  echo "--require-github-attestation needs --attestation-repository" >&2
  exit 2
fi

audit_commands=(jq skopeo)
if ! "$verify_base_evidence"; then
  audit_commands=("$engine" "${audit_commands[@]}")
fi
if "$verify_base_evidence" || "$require_github_attestation"; then
  audit_commands+=(gh)
fi
for command in "${audit_commands[@]}"; do
  command -v "$command" >/dev/null ||
    {
      echo "required audit command is unavailable: $command" >&2
      exit 1
    }
done

fail=0
report=""

append() {
  report+="$1"$'\n'
}

error() {
  echo "::error::$1" >&2
  fail=1
}

require_line() {
  local inventory="$1" kind="$2" name="$3"
  grep -qFx "${kind}:${name}" <<<"$inventory" ||
    error "${kind} command missing from runtime: ${name}"
}

forbid_line() {
  local inventory="$1" kind="$2" name="$3"
  if grep -qFx "${kind}:${name}" <<<"$inventory"; then
    error "forbidden ${kind} command present in runtime: ${name}"
  fi
}

emit_report() {
  printf '%s' "$report"
  if [[ -n "$report_file" ]]; then
    printf '%s' "$report" >"$report_file"
  fi
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s' "$report" >>"$GITHUB_STEP_SUMMARY"
  fi
}

unshadowed_path_line() {
  # The base ships real GNU findutils and diffutils, so review no longer shims
  # find and cmp; the Python shims it once carried are deleted. What Hive's
  # relay depends on now is that nothing shadows them from /usr/local/bin,
  # which the base PATH searches first. Assert the resolved path so a
  # reintroduced shim fails here instead of silently altering relay behaviour.
  local inventory="$1" name="$2" resolved
  resolved="$(grep -F "path:${name}:" <<<"$inventory" | head -n 1)"
  resolved="${resolved#path:"${name}":}"
  if [[ -z "$resolved" ]]; then
    error "base-provided command missing from runtime: ${name}"
  elif [[ "$resolved" == /usr/local/bin/* ]]; then
    error "review shadows the base ${name}: resolves to ${resolved}; use the FSDK seam, not a shim"
  fi
}

normalize_arch() {
  case "$1" in
  x86_64 | amd64) echo amd64 ;;
  aarch64 | arm64) echo arm64 ;;
  *) echo "$1" ;;
  esac
}

engine_arch() {
  case "$(basename "$engine")" in
  podman) "$engine" info --format '{{.Host.Arch}}' ;;
  *) "$engine" info --format '{{.Architecture}}' ;;
  esac
}

# Docker accepts a trackable tag plus an immutable digest. Skopeo requires the
# equivalent digest-only form, so retain the tag for Docker pulls and remove it
# only at the registry-inspection boundary.
registry_repository() {
  local image="${1%%@*}" leaf
  leaf="${image##*/}"
  [[ "$leaf" == *:* ]] && image="${image%:*}"
  printf '%s' "$image"
}

registry_reference() {
  local image="$1"
  if [[ "$image" == *@* ]]; then
    printf '%s@%s' "$(registry_repository "$image")" "${image#*@}"
  else
    printf '%s' "$image"
  fi
}

require_exact_linux_platforms() {
  local manifest_raw="$1" subject="$2"
  local platforms=()
  mapfile -t platforms < <(
    jq -r '
      .manifests[]? |
      select(.platform.os == "linux") |
      .platform.architecture
    ' <<<"$manifest_raw" | sort -u
  )
  if [[ "${#platforms[@]}" -ne 2 ||
    "${platforms[0]:-}" != amd64 ||
    "${platforms[1]:-}" != arm64 ]]; then
    error "${subject} must contain exactly linux/amd64 and linux/arm64 manifests"
  fi
}

base_raw="$(skopeo inspect --raw "docker://$(registry_reference "$base_image")")"
base_repository="$(registry_repository "$base_image")"
derived_repository="$(registry_repository "$derived_image")"
mapfile -t base_platforms < <(
  jq -r '
    .manifests[]? |
    select(.platform.os == "linux") |
    [.platform.architecture, .digest] | @tsv
  ' <<<"$base_raw"
)

[[ "${#base_platforms[@]}" -gt 0 ]] ||
  {
    echo "base image is not a Linux manifest list: ${base_image}" >&2
    exit 1
  }

append "### Review image audit"
append ""
append "- FSDK base: \`${base_image}\`"
if "$verify_base_evidence"; then
  append "- Derived image: _not inspected; verifying the pinned FSDK input._"
else
  append "- Derived image: \`${derived_image}\`"
  append "- Host architecture: \`$(normalize_arch "$(engine_arch)")\`"
fi
append ""
# Every figure in this report is measured from the exact digests above, at
# runtime where the host architecture allows it. Comparisons against retired
# digests are historical and live in issues #70 and #87, never here.
append "All facts below are current exact-digest measurements. Retired-digest comparisons are historical context only and live in #70/#87, not in this report."
append ""
append "#### FSDK manifest and compressed layers"
append ""
append "| Platform | Manifest | Compressed layers |"
append "| --- | --- | ---: |"

found_amd64=false
found_arm64=false
declare -A base_compressed derived_compressed
derived_compressed_count=0
for platform_digest in "${base_platforms[@]}"; do
  platform="${platform_digest%%$'\t'*}"
  digest="${platform_digest#*$'\t'}"
  case "$platform" in
  amd64) found_amd64=true ;;
  arm64) found_arm64=true ;;
  esac
  manifest_raw="$(skopeo inspect --raw "docker://${base_repository}@${digest}")"
  compressed="$(jq '[.layers[]?.size] | add // 0' <<<"$manifest_raw")"
  base_compressed["$platform"]="$compressed"
  append "| linux/${platform} | \`${digest}\` | ${compressed} B |"
done

"$found_amd64" || error "FSDK manifest list is missing linux/amd64"
"$found_arm64" || error "FSDK manifest list is missing linux/arm64"
require_exact_linux_platforms "$base_raw" "FSDK base manifest list"

if "$verify_base_evidence"; then
  gh attestation verify "oci://$(registry_reference "$base_image")" \
    --repo projectbluefin/fsdk-containers
  append ""
  append "- FSDK attestation: verified for \`$(registry_reference "$base_image")\`."
  emit_report
  [[ "$fail" -eq 0 ]] && echo "✓ FSDK input evidence holds."
  exit "$fail"
fi

derived_raw=""
if derived_raw="$(skopeo inspect --raw "docker://$(registry_reference "$derived_image")" 2>/dev/null)"; then
  append ""
  append "#### Derived manifest and compressed layers"
  append ""
  append "| Platform | Manifest | Compressed layers |"
  append "| --- | --- | ---: |"
  while IFS=$'\t' read -r platform digest; do
    [[ -n "$platform" ]] || continue
    manifest_raw="$(skopeo inspect --raw "docker://${derived_repository}@${digest}")"
    compressed="$(jq '[.layers[]?.size] | add // 0' <<<"$manifest_raw")"
    derived_compressed["$platform"]="$compressed"
    derived_compressed_count=$((derived_compressed_count + 1))
    append "| linux/${platform} | \`${digest}\` | ${compressed} B |"
  done < <(
    jq -r '
      .manifests[]? |
      select(.platform.os == "linux") |
      [.platform.architecture, .digest] | @tsv
    ' <<<"$derived_raw"
  )
else
  append ""
  append "- Derived registry manifest: unavailable for local image reference."
fi

if "$require_attestations"; then
  [[ -n "$derived_raw" ]] ||
    {
      echo "published audit needs a registry image reference: ${derived_image}" >&2
      exit 1
    }
  require_exact_linux_platforms "$derived_raw" "published review manifest list"
  attestation_digests="$(
    jq -r '
      .manifests[]? |
      select(.annotations["vnd.docker.reference.type"] == "attestation-manifest") |
      .digest
    ' <<<"$derived_raw"
  )"
  [[ -n "$attestation_digests" ]] ||
    error "published derived image has no OCI attestation manifests"
  attestation_predicates=""
  while IFS= read -r digest; do
    [[ -n "$digest" ]] || continue
    attestation_predicates+="$(
      skopeo inspect --raw "docker://${derived_repository}@${digest}" |
        jq -r '.layers[]?.annotations["in-toto.io/predicate-type"] // empty'
    )"$'\n'
  done <<<"$attestation_digests"
  grep -qi 'spdx' <<<"$attestation_predicates" ||
    error "published derived image is missing an SPDX SBOM attestation"
  grep -qi 'slsa.*provenance' <<<"$attestation_predicates" ||
    error "published derived image is missing a SLSA provenance attestation"
fi

if "$require_github_attestation"; then
  [[ -n "$derived_raw" ]] ||
    {
      echo "GitHub artifact verification needs a registry image reference: ${derived_image}" >&2
      exit 1
    }
  gh attestation verify "oci://$(registry_reference "$derived_image")" \
    --repo "$attestation_repository"
  append "- GitHub artifact attestation: verified for \`$(registry_reference "$derived_image")\`."
fi

# Pull only the host architecture. A runtime check on another architecture
# would use emulation when it happens to work, which is not native evidence.
"$engine" pull "$base_image" >/dev/null
if ! "$engine" image inspect "$derived_image" >/dev/null 2>&1; then
  "$engine" pull "$derived_image" >/dev/null
fi

base_inspect="$("$engine" image inspect "$base_image")"
derived_inspect="$("$engine" image inspect "$derived_image")"
host_arch="$(normalize_arch "$(engine_arch)")"
base_arch="$(normalize_arch "$(jq -r '.[0].Architecture' <<<"$base_inspect")")"
derived_arch="$(normalize_arch "$(jq -r '.[0].Architecture' <<<"$derived_inspect")")"

append ""
append "#### Local image and OCI configuration"
append ""
append "| Image | Architecture | Config user | Unpacked size | Rootfs layers |"
append "| --- | --- | --- | ---: | ---: |"
for image_kind in base derived; do
  if [[ "$image_kind" == base ]]; then
    inspect="$base_inspect"
    arch="$base_arch"
  else
    inspect="$derived_inspect"
    arch="$derived_arch"
  fi
  user="$(jq -r '.[0].Config.User // "<default>"' <<<"$inspect")"
  size="$(jq -r '.[0].Size' <<<"$inspect")"
  layers="$(jq -r '.[0].RootFS.Layers | length' <<<"$inspect")"
  append "| ${image_kind} | linux/${arch} | \`${user}\` | ${size} B | ${layers} |"
done

append ""
append "#### Size deltas"
append ""
if [[ "$derived_compressed_count" -eq 0 ]]; then
  append "- Compressed derived delta: unavailable for a local-only image reference."
else
  append "| Platform | Compressed delta (derived - base) |"
  append "| --- | ---: |"
  for platform in "${!derived_compressed[@]}"; do
    if [[ -n "${base_compressed[$platform]:-}" ]]; then
      append "| linux/${platform} | $((${derived_compressed[$platform]} - ${base_compressed[$platform]})) B |"
    else
      append "| linux/${platform} | unavailable (base platform missing) |"
    fi
  done
fi
append "- Local unpacked delta (derived - base): $(($(jq -r '.[0].Size' <<<"$derived_inspect") - $(jq -r '.[0].Size' <<<"$base_inspect"))) B."

mapfile -t base_layers < <(jq -r '.[0].RootFS.Layers[]' <<<"$base_inspect")
mapfile -t derived_layers < <(jq -r '.[0].RootFS.Layers[]' <<<"$derived_inspect")
if [[ "${#derived_layers[@]}" -le "${#base_layers[@]}" ]]; then
  error "derived image does not add layers to the exact base image"
else
  for index in "${!base_layers[@]}"; do
    [[ "${base_layers[$index]}" == "${derived_layers[$index]}" ]] ||
      error "derived rootfs layer ${index} does not preserve the exact base layer"
  done
fi
append "- Composition: derived rootfs preserves ${#base_layers[@]} base layer(s) and adds $((${#derived_layers[@]} - ${#base_layers[@]})) layer(s)."

base_labels="$(jq -r '.[0].Config.Labels // {} | to_entries[] | "\(.key)=\(.value)"' <<<"$base_inspect")"
derived_labels="$(jq -r '.[0].Config.Labels // {} | to_entries[] | "\(.key)=\(.value)"' <<<"$derived_inspect")"
for image_kind in base derived; do
  if [[ "$image_kind" == base ]]; then
    labels="$base_labels"
  else
    labels="$derived_labels"
  fi
  append "- ${image_kind^} OCI labels:"
  if [[ -n "$labels" ]]; then
    while IFS= read -r label; do
      append "  - \`${label}\`"
    done <<<"$labels"
  else
    append "  - _none_"
  fi
done

if "$require_oci"; then
  [[ -n "$derived_raw" ]] ||
    {
      echo "published OCI verification needs a registry image reference: ${derived_image}" >&2
      exit 1
    }

  expected_oci_value() {
    case "$1" in
    org.opencontainers.image.title) printf '%s' 'Bluefin review contributor' ;;
    org.opencontainers.image.description) printf '%s' 'Foreground contributor runtime for projectbluefin/review.' ;;
    org.opencontainers.image.url | org.opencontainers.image.source) printf '%s' "$expected_source" ;;
    org.opencontainers.image.revision) printf '%s' "$expected_revision" ;;
    org.opencontainers.image.version) printf '%s' "$expected_version" ;;
    org.opencontainers.image.licenses) printf '%s' 'Apache-2.0' ;;
    org.opencontainers.image.base.name) printf '%s' "${base_image%@*}" ;;
    org.opencontainers.image.base.digest) printf '%s' "${base_image#*@}" ;;
    *) printf '%s' '' ;;
    esac
  }

  required_oci_labels=(
    org.opencontainers.image.title
    org.opencontainers.image.description
    org.opencontainers.image.url
    org.opencontainers.image.source
    org.opencontainers.image.revision
    org.opencontainers.image.version
    org.opencontainers.image.created
    org.opencontainers.image.licenses
    org.opencontainers.image.base.name
    org.opencontainers.image.base.digest
  )

  check_oci_metadata() {
    local metadata_json="$1" location="$2" selector="$3" label expected actual
    for label in "${required_oci_labels[@]}"; do
      if [[ "$selector" == annotations ]]; then
        actual="$(jq -r --arg label "$label" '.annotations[$label] // empty' <<<"$metadata_json")"
      else
        actual="$(jq -r --arg label "$label" '.config.Labels[$label] // empty' <<<"$metadata_json")"
      fi
      if [[ -z "$actual" ]]; then
        error "${location} lacks required OCI metadata: ${label}"
        continue
      fi
      if [[ "$label" == org.opencontainers.image.created ]]; then
        [[ "$actual" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
          error "${location} has an invalid OCI created timestamp: ${actual}"
        continue
      fi
      expected="$(expected_oci_value "$label")"
      if [[ -n "$expected" && "$actual" != "$expected" ]]; then
        error "${location} has unexpected ${label}: ${actual}"
      fi
    done
  }

  check_oci_metadata "$derived_raw" "published review manifest index annotations" annotations
  while IFS=$'\t' read -r platform digest; do
    [[ -n "$platform" ]] || continue
    platform_config="$(skopeo inspect --config "docker://${derived_repository}@${digest}")"
    check_oci_metadata "$platform_config" "published review linux/${platform} config labels" config.Labels
  done < <(
    jq -r '
      .manifests[]? |
      select(.platform.os == "linux") |
      [.platform.architecture, .digest] | @tsv
    ' <<<"$derived_raw"
  )
fi

runtime_inventory() {
  local image="$1"
  # shellcheck disable=SC2016 # This script is evaluated inside the image.
  "$engine" run --rm --entrypoint /usr/bin/bash "$image" -ceu '
    required="$1"
    forbidden="$2"
    terminfo="$3"
    for command in $required; do
      command -v "$command" >/dev/null && printf "required:%s\n" "$command"
      resolved="$(command -v "$command" 2>/dev/null)" &&
        printf "path:%s:%s\n" "$command" "$resolved"
    done
    for command in $forbidden; do
      command -v "$command" >/dev/null && printf "forbidden:%s\n" "$command"
    done
    for term in $terminfo; do
      infocmp "$term" >/dev/null 2>&1 && printf "terminfo:%s\n" "$term"
    done
    printf "identity:%s:%s\n" "$(id -u)" "$(id -un)"
    grep "^$(id -un):" /etc/passwd | sed "s/^/passwd:/"
    for loader in /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-aarch64.so.1; do
      [ -e "$loader" ] && printf "loader:%s:%s\n" "$loader" "$(readlink -f "$loader")"
    done
    set -- $(du -sk / 2>/dev/null)
    printf "rootfs-kib:%s\n" "$1"
  ' bash "$2" "$3" "$4"
}

base_required="bash cat chmod cp curl git grep jq ls mkdir mv python3 rm sed sh sort tail tee touch tr uname wc ssh kubectl tic infocmp argo just nginx"
# Two different rules used to be spelled the same way here, which made the
# audit fail for a change that was actually correct.
#
# A package manager is genuinely forbidden: images are built from BST
# elements, so a runtime that can mutate itself is a defect.
#
# Everything review installs itself is forbidden only from the base, because
# a second copy there means two versions of the same tool and no way to tell
# which one an agent ran.
#
# Ordinary userland -- find, cmp, diff, and utilities such as rg, fd, yq and
# ShellCheck -- is deliberately absent from both lists. Contributor agents
# need it, and its absence is what made them fail with 'command not found'.
# For 'find' and 'cmp' the real invariant is not absence but provenance:
# Hive's relay calls both directly, the base carries real GNU implementations,
# and review must not shadow them. That is asserted directly below rather than
# approximated by forbidding the base copy.
package_managers="apt dnf apk"
review_owned="node npm gh tmux goose"
base_forbidden="${review_owned} ${package_managers}"
derived_required="bash node npm corepack gh tmux goose find cmp diff infocmp"
derived_forbidden="$package_managers"
# Base commands Hive's relay calls directly and review must never shim over.
# image/Containerfile proves their semantics at build time against the real
# base; this checks the finished runtime still resolves them from it.
derived_unshadowed="find cmp diff"

append ""
append "#### Native runtime audit"
append ""
for image_kind in base derived; do
  if [[ "$image_kind" == base ]]; then
    image="$base_image"
    arch="$base_arch"
    required="$base_required"
    forbidden="$base_forbidden"
    terms="xterm-256color tmux-256color"
  else
    image="$derived_image"
    arch="$derived_arch"
    required="$derived_required"
    forbidden="$derived_forbidden"
    terms="xterm-256color tmux-256color"
  fi

  if [[ "$arch" != "$host_arch" ]]; then
    append "- ${image_kind}: **unavailable** (linux/${arch} image on native linux/${host_arch} host; not run under QEMU)."
    continue
  fi

  inventory="$(runtime_inventory "$image" "$required" "$forbidden" "$terms")"
  for command in $required; do
    require_line "$inventory" required "$command"
  done
  for command in $forbidden; do
    forbid_line "$inventory" forbidden "$command"
  done
  if [[ "$image_kind" == derived ]]; then
    for command in $derived_unshadowed; do
      unshadowed_path_line "$inventory" "$command"
    done
    require_line "$inventory" terminfo xterm-256color
    require_line "$inventory" terminfo tmux-256color
    "$engine" run --rm --entrypoint /usr/bin/bash "$image" -ceu \
      'node -e "require.resolve(\"ws\")" >/dev/null' ||
      error "derived runtime is missing the ws Node module"
    # shellcheck disable=SC2016 # This Node program is evaluated inside the image.
    "$engine" run --rm --entrypoint /usr/bin/bash "$image" -ceu '
      [ ! -e /opt/node/include ]
      [ ! -e /opt/node/share/doc ]
      [ ! -e /root/.npm ]
      npm --version >/dev/null
      corepack --version >/dev/null
      node - <<'"'"'NODE'"'"'
const WebSocket = require("ws");
const server = new WebSocket.Server({ port: 0 }, () => {
  const { port } = server.address();
  const client = new WebSocket(`ws://127.0.0.1:${port}`);
  client.on("open", () => client.close());
});
server.on("connection", (socket) => {
  socket.on("close", () => server.close(() => process.exit(0)));
});
setTimeout(() => {
  server.close();
  process.exit(1);
}, 5000).unref();
NODE
    ' ||
      error "derived runtime cannot establish a local ws connection"
    "$engine" run --rm --entrypoint /usr/local/bin/goose "$image" run --help >/dev/null ||
      error "derived runtime cannot execute goose run --help"
    grep -q '^identity:1000:dev$' <<<"$inventory" ||
      error "derived runtime must execute as uid 1000 (dev)"
  fi
  rootfs_kib="$(grep '^rootfs-kib:' <<<"$inventory" | cut -d: -f2)"
  identity="$(grep '^identity:' <<<"$inventory" | cut -d: -f2-)"
  loader_facts="$(grep '^loader:' <<<"$inventory" | cut -d: -f2- | paste -sd ';' - || true)"
  command_facts="$(grep '^required:' <<<"$inventory" | cut -d: -f2 | paste -sd ',' -)"
  terminfo_facts="$(grep '^terminfo:' <<<"$inventory" | cut -d: -f2 | paste -sd ',' - || true)"
  append "- ${image_kind}: **native** linux/${arch}; ${rootfs_kib} KiB rootfs."
  append "  - ${identity}; ${loader_facts:-no supported loader path found}"
  append "  - commands: ${command_facts}; terminfo: ${terminfo_facts:-none}; forbidden package managers absent."
done

# Record both platform slots explicitly. A platform with no native host is
# unavailable evidence, never a skipped row and never a QEMU substitute;
# native arm64 runtime measurement is tracked by #77.
append ""
append "#### Runtime evidence by platform"
append ""
append "| Platform | Base | Derived |"
append "| --- | --- | --- |"
for platform in amd64 arm64; do
  for image_kind in base derived; do
    if [[ "$image_kind" == base ]]; then
      arch="$base_arch"
    else
      arch="$derived_arch"
    fi
    if [[ "$arch" == "$platform" && "$arch" == "$host_arch" ]]; then
      slot="**native** (this host)"
    else
      slot="unavailable (no native linux/${platform} host)"
      [[ "$platform" == arm64 ]] && slot+=" — #77"
    fi
    if [[ "$image_kind" == base ]]; then
      base_slot="$slot"
    else
      derived_slot="$slot"
    fi
  done
  append "| linux/${platform} | ${base_slot} | ${derived_slot} |"
done

emit_report

[[ "$fail" -eq 0 ]] && echo "✓ image audit holds."
exit "$fail"
