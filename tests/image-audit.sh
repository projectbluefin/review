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
engine="${CONTAINER_ENGINE:-podman}"

# Predicate URIs written by actions/attest. Provenance is SLSA v1; the SBOM
# predicate is derived from the SPDX version inside the document, so it moves
# with the SBOM format rather than with the action.
slsa_provenance_predicate="https://slsa.dev/provenance/v1"
spdx_predicate="https://spdx.dev/Document/v2.3"

usage() {
  cat <<'EOF'
Usage: tests/image-audit.sh --derived IMAGE [options]

Audit the digest-pinned FSDK base and an already-built or published review
image. IMAGE may be a local image tag or an immutable registry reference.
The engine is podman unless CONTAINER_ENGINE names another one.

Options:
  --base IMAGE             Override the FSDK base parsed from image/Containerfile.
  --derived IMAGE          Built review image to inspect (required).
  --require-oci            Require source, revision, and version OCI labels.
  --require-attestations   Require registry-attached SPDX SBOM and SLSA
                           provenance bundles, verified by signature.
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
if "$require_attestations" && [[ -z "$attestation_repository" ]]; then
  echo "--require-attestations needs --attestation-repository" >&2
  exit 2
fi

audit_commands=(jq skopeo)
if ! "$verify_base_evidence"; then
  audit_commands=("$engine" "${audit_commands[@]}")
fi
if "$verify_base_evidence" || "$require_github_attestation" || "$require_attestations"; then
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

containerfile_arg() {
  sed -n "s/^ARG $1=\\(.*\\)\$/\\1/p" image/Containerfile | head -n 1
}

# The Goose asset digest the build actually verified. CI resolves it from the
# canary release API immediately before building, so the Containerfile
# defaults can be older than what shipped; the image's own labels are the
# record of what was verified. $1: amd64|arm64, $2: labels as key=value lines.
goose_label_digest() {
  local arch="$1" labels="$2" label_arch
  case "$arch" in
  amd64) label_arch=x86_64 ;;
  arm64) label_arch=aarch64 ;;
  *)
    echo "goose_label_digest needs amd64 or arm64, got: ${arch}" >&2
    return 2
    ;;
  esac
  sed -n "s/^io\\.projectbluefin\\.review\\.goose\\.${label_arch}-unknown-linux-musl\\.sha256=\\([0-9a-f]\\{64\\}\\)\$/\\1/p" \
    <<<"$labels"
}

# The components syft cannot see in the image: everything review installs
# from a release archive or fetched source file rather than through a package
# manager (#78). Expected versions come from the same Containerfile pins the
# build consumed, so a pin bump cannot silently drift the audit away from the
# SBOM. The third field is the verified archive digest where the pin is a
# build argument; it reaches the published SBOM as the purl checksum
# qualifier, the one digest field syft's sbom-cataloger merge preserves.
# review-git-hooks is versioned by the review source revision, which only the
# publisher knows; without --expected-revision it is a presence check only.
required_sbom_components() {
  local arch="$1" goose_sha256="$2" suffix
  local hive_commit skills_commit codex_version
  case "$arch" in
  amd64) suffix=X86_64 ;;
  arm64) suffix=AARCH64 ;;
  *)
    echo "required_sbom_components needs amd64 or arm64, got: ${arch}" >&2
    return 2
    ;;
  esac
  hive_commit="$(containerfile_arg HIVE_COMMIT)"
  skills_commit="$(containerfile_arg SKILLS_COMMIT)"
  codex_version="$(containerfile_arg CODEX_VERSION)"
  printf 'goose\t%s\t%s\n' "$(containerfile_arg GOOSE_CHANNEL)" "$goose_sha256"
  printf 'gh\t%s\t\n' "$(containerfile_arg GH_VERSION)"
  printf 'tmux\t%s\t\n' "$(containerfile_arg TMUX_VERSION)"
  printf 'codex\t%s\t%s\n' "$codex_version" "$(containerfile_arg "CODEX_${suffix}_SHA256")"
  printf 'codex-code-mode-host\t%s\t%s\n' "$codex_version" "$(containerfile_arg "CODEX_CODE_MODE_HOST_${suffix}_SHA256")"
  printf 'ripgrep\t%s\t%s\n' "$(containerfile_arg RIPGREP_VERSION)" "$(containerfile_arg "RIPGREP_${suffix}_SHA256")"
  printf 'contributor-agent.sh\t%s\t\n' "$hive_commit"
  printf 'contributor-relay.sh\t%s\t\n' "$hive_commit"
  printf 'backends.conf\t%s\t\n' "$hive_commit"
  printf 'bluefin-organization-skills\t%s\t\n' "$skills_commit"
  printf 'review-git-hooks\t%s\t\n' "$expected_revision"
}

check_sbom_components() {
  local sbom_json="$1" source_desc="$2" arch="$3" goose_sha256="$4"
  local name expected expected_sha actual
  while IFS=$'\t' read -r name expected expected_sha; do
    [[ -n "$name" ]] || continue
    actual="$(jq -r --arg name "$name" \
      '[.packages[]? | select(.name == $name) | .versionInfo][0] // empty' \
      <<<"$sbom_json")"
    if [[ -z "$actual" ]]; then
      error "${source_desc} omits review-owned component: ${name}"
      continue
    fi
    if [[ -z "$expected" ]]; then
      [[ "$name" == review-git-hooks ]] ||
        error "no expected version derivable from image/Containerfile for ${name}"
      continue
    fi
    [[ "$actual" == "$expected" ]] ||
      error "${source_desc} records ${name} at ${actual}, expected ${expected}"
    # The digest rides as the purl checksum qualifier: raw in the in-image
    # manifest, percent-encoded after syft re-encodes the locator.
    if [[ "$name" == goose && -z "$expected_sha" ]]; then
      error "${source_desc}: image does not record the verified Goose ${arch} asset digest"
      continue
    fi
    if [[ -n "$expected_sha" ]] && ! jq -e --arg name "$name" --arg sha "$expected_sha" \
      '[.packages[]? | select(.name == $name) |
        .externalRefs[]? | select(.referenceType == "purl") | .referenceLocator |
        test("checksum=sha256(:|%3A)" + $sha)] | any' \
      <<<"$sbom_json" >/dev/null; then
      error "${source_desc} records ${name} without its verified archive digest ${expected_sha}"
    fi
  done < <(required_sbom_components "$arch" "$goose_sha256")
}

# The SBOM predicate lives in a signed Sigstore bundle in the registry's
# referrers API; the signature is verified separately and the content is read
# from the same attestation. gh attestation download writes one JSONL line per
# bundle, with the in-toto statement base64-encoded in the DSSE envelope.
fetch_spdx_predicate() {
  local reference="$1" download_dir bundle_file statement
  download_dir="$(mktemp -d)"
  if ! (
    cd "$download_dir"
    gh attestation download "oci://${reference}" \
      --repo "$attestation_repository" \
      --predicate-type "$spdx_predicate" >/dev/null
  ); then
    rm -rf "$download_dir"
    return 1
  fi
  bundle_file="$(find "$download_dir" -name '*.jsonl' -print -quit)"
  statement="$(head -n 1 "$bundle_file" | jq -r '.dsseEnvelope.payload // empty' | base64 -d 2>/dev/null || true)"
  rm -rf "$download_dir"
  jq -e --arg predicate_type "$spdx_predicate" \
    'select(.predicateType == $predicate_type) | .predicate' <<<"$statement"
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

# Podman accepts a trackable tag plus an immutable digest. Skopeo requires the
# equivalent digest-only form, so retain the tag for engine pulls and remove it
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

  # The SBOM and the provenance are signed Sigstore bundles attached through
  # the referrers API, not manifests the builder wrote into its own output.
  # Verifying signatures is the point: a builder cannot vouch for itself.
  verify_predicate() {
    local reference="$1" predicate="$2"
    gh attestation verify "oci://${reference}" \
      --repo "$attestation_repository" \
      --bundle-from-oci \
      --predicate-type "$predicate" >/dev/null 2>&1
  }

  if verify_predicate "$(registry_reference "$derived_image")" "$slsa_provenance_predicate"; then
    append ""
    append "- Index attestation: verified \`${slsa_provenance_predicate}\` for the published index."
  else
    error "published index is missing a verifiable SLSA provenance attestation"
  fi

  # An SBOM describes one root filesystem, so each platform carries its own,
  # produced by the job that built that platform. An index-wide SBOM would be
  # one architecture's inventory presented as both.
  while IFS=$'\t' read -r platform digest; do
    [[ -n "$platform" ]] || continue
    platform_verified=true
    # An attached SBOM is not coverage: the document must also name the
    # archive-installed components review owns (#78), on every platform, or
    # publication fails here.
    if verify_predicate "${derived_repository}@${digest}" "$spdx_predicate"; then
      checks_before="$fail"
      if spdx_document="$(fetch_spdx_predicate "${derived_repository}@${digest}")"; then
        platform_labels="$(skopeo inspect --config "docker://${derived_repository}@${digest}" |
          jq -r '.config.Labels // {} | to_entries[] | "\(.key)=\(.value)"')"
        check_sbom_components "$spdx_document" "published linux/${platform} SPDX SBOM" "$platform" \
          "$(goose_label_digest "$platform" "$platform_labels")"
      else
        error "published linux/${platform} SPDX SBOM predicate could not be read"
      fi
      [[ "$fail" == "$checks_before" ]] || platform_verified=false
    else
      error "published linux/${platform} image is missing a verifiable SPDX SBOM attestation"
      platform_verified=false
    fi
    verify_predicate "${derived_repository}@${digest}" "$slsa_provenance_predicate" || {
      error "published linux/${platform} image is missing a verifiable SLSA provenance attestation"
      platform_verified=false
    }
    if "$platform_verified"; then
      append "- linux/${platform} attestations: verified SPDX SBOM and SLSA provenance."
    else
      append "- linux/${platform} attestations: **missing or unverifiable**."
    fi
  done < <(
    jq -r '
      .manifests[]? |
      select(.platform.os == "linux") |
      [.platform.architecture, .digest] | @tsv
    ' <<<"$derived_raw"
  )
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

base_required="bash cat chmod cp curl git grep jq ls mkdir mv python3 rm sed sh sort tail tee touch tr uname wc ssh kubectl tic infocmp argo just nginx find cmp diff"
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
# Ordinary userland -- find, cmp, diff, and utilities such as fd, yq and
# ShellCheck -- is deliberately absent from both lists. Contributor agents
# need it, and its absence is what made them fail with 'command not found'.
# For 'find' and 'cmp' the real invariant is not absence but provenance:
# Hive's relay calls both directly, the base carries real GNU implementations,
# and review must not shadow them. That is asserted directly below rather than
# approximated by forbidding the base copy.
#
# rg sits with the review-owned tools instead, because review installs it
# (#75). The day the base ships rg, this audit fails, and the answer is to
# delete review's layer rather than run two copies.
package_managers="apt dnf apk"
review_owned="node npm gh tmux codex codex-code-mode-host goose rg"
base_forbidden="${review_owned} ${package_managers}"
derived_required="bash node npm corepack gh tmux codex codex-code-mode-host goose rg find cmp diff grep cat ls infocmp"
derived_forbidden="$package_managers"
# Base commands Hive's relay calls directly and review must never shim over.
# image/Containerfile proves their semantics at build time against the real
# base; this checks the finished runtime still resolves them from it. grep,
# cat and ls are here because of rg: a search tool in /usr/local/bin is
# exactly the shape that let the old find/cmp shims shadow the base's GNU
# copies. rg itself must never join this list -- /usr/local/bin is where it
# legitimately lives.
#
# Every name here must also appear in derived_required: runtime_inventory
# only emits a `path:` line for a required command, and a name without one
# reports as missing from the runtime rather than as unshadowed.
derived_unshadowed="find cmp diff grep cat ls"

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
    "$engine" run --rm --entrypoint /usr/local/bin/codex "$image" --version >/dev/null ||
      error "derived runtime cannot execute codex --version"
    "$engine" run --rm --entrypoint /usr/local/bin/codex-code-mode-host "$image" --help >/dev/null ||
      error "derived runtime cannot execute codex-code-mode-host --help"
    # The image carries the SPDX manifest of its archive-installed components
    # (#78) for the publish workflow's syft run to ingest; a build that loses
    # it would publish an incomplete SBOM, so assert it here where a missing
    # manifest is still a local, unpublishable defect.
    if sbom_manifest="$("$engine" run --rm --entrypoint /usr/bin/cat "$image" \
      /opt/bluefin/sbom/review-components.spdx.json 2>/dev/null)"; then
      check_sbom_components "$sbom_manifest" "derived image SBOM manifest" "$arch" \
        "$(goose_label_digest "$arch" "$derived_labels")"
    else
      error "derived image is missing /opt/bluefin/sbom/review-components.spdx.json"
    fi
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
