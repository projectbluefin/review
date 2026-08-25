#!/usr/bin/env bash
# Exercise the SBOM manifest generator: the document it bakes into the image
# is what the publish workflow's syft run ingests (#78), so a malformed or
# incomplete manifest must fail here rather than at the audit gate.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

fail=0

GOOSE_X86_64="db1ae20729ac87ebff0e55d161ec88515d92e580ddd1b0b4371a162f16d05180"
GOOSE_AARCH64="187e0564d735cd99678f471c303546df64eafab6db851421781c96b8700e9792"
CODEX_X86_64="0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"
CODEX_AARCH64="eb677c80f666b1ab8b4b1d083b66e8d614b1281d960bb6f9fd8ca98f58b38b90"
CMH_X86_64="0146adfaac8363ec9fcdb5895f7624db5b2e8617a283887938b7fb97a1dd4356"
CMH_AARCH64="dfd4ff98ea4db30ed078af9c31b6f86e3da4836d0573aa87e225e5a5b54d3c7c"
RG_X86_64="33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c"
RG_AARCH64="800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915"
HIVE_COMMIT="5db84c3b2cb7ce48635bba702115a34838690e03"
SKILLS_COMMIT="b6f5c370cca19398fbbbe43a0182dca6783a80cb"
LAB_SKILLS_COMMIT="4673dd5062bcebb26779da76789ccde006ea2366"
REVISION="fd4437560fb87eae4707070b224ab1901ab6f0c6"

generate() {
  # generate <arch> <out> [extra args...]
  local arch="$1" out="$2"
  shift 2
  python3 scripts/generate-sbom-manifest.py \
    --arch "$arch" \
    --revision "$REVISION" \
    --out "$out" \
    --goose-channel canary \
    --goose-sha256-x86-64 "$GOOSE_X86_64" \
    --goose-sha256-aarch64 "$GOOSE_AARCH64" \
    --gh-version 2.97.0 \
    --tmux-version 3.7b \
    --codex-version 0.147.0 \
    --codex-sha256-x86-64 "$CODEX_X86_64" \
    --codex-sha256-aarch64 "$CODEX_AARCH64" \
    --codex-code-mode-host-sha256-x86-64 "$CMH_X86_64" \
    --codex-code-mode-host-sha256-aarch64 "$CMH_AARCH64" \
    --ripgrep-version 15.2.0 \
    --ripgrep-sha256-x86-64 "$RG_X86_64" \
    --ripgrep-sha256-aarch64 "$RG_AARCH64" \
    --hive-commit "$HIVE_COMMIT" \
    --skills-commit "$SKILLS_COMMIT" \
    --lab-skills-commit "$LAB_SKILLS_COMMIT" \
    "$@"
}

# check <file> <jq expression> <description> — the expression must be true.
check() {
  local file="$1" expression="$2" description="$3"
  jq -e "$expression" "$file" >/dev/null || {
    echo "::error::${description}"
    fail=1
  }
}

generate x86_64 "$tmpdir/amd64.spdx.json"
generate aarch64 "$tmpdir/arm64.spdx.json"

for doc in "$tmpdir/amd64.spdx.json" "$tmpdir/arm64.spdx.json"; do
  check "$doc" '.spdxVersion == "SPDX-2.3"' "$doc: not SPDX 2.3"
  check "$doc" '.dataLicense == "CC0-1.0"' "$doc: missing CC0-1.0 data license"
  check "$doc" '.documentNamespace | test("^https://github.com/projectbluefin/review/sbom/")' \
    "$doc: documentNamespace is not under the review repository"
  check "$doc" '.packages | length == 12' "$doc: must describe exactly the 12 archive-installed components"
  check "$doc" '[.packages[].SPDXID] | unique | length == 12' "$doc: SPDXIDs must be unique"
  check "$doc" 'all(.packages[]; .filesAnalyzed == false
    and (.downloadLocation | test("^https://"))
    and (.externalRefs[0].referenceType == "purl"))' \
    "$doc: every package needs a versioned https download location and a purl"
done

# Component identities: name and version are what the audit asserts.
check "$tmpdir/amd64.spdx.json" '
  [.packages[] | {key: .name, value: .versionInfo}] | from_entries ==
  {
    "goose": "canary",
    "gh": "2.97.0",
    "tmux": "3.7b",
    "codex": "0.147.0",
    "codex-code-mode-host": "0.147.0",
    "ripgrep": "15.2.0",
    "contributor-agent.sh": "'"$HIVE_COMMIT"'",
    "contributor-relay.sh": "'"$HIVE_COMMIT"'",
    "backends.conf": "'"$HIVE_COMMIT"'",
    "bluefin-organization-skills": "'"$SKILLS_COMMIT"'",
    "bluefin-lab-skills": "'"$LAB_SKILLS_COMMIT"'",
    "review-git-hooks": "'"$REVISION"'"
  }' "amd64 manifest: component names or versions diverge"

# The per-architecture identity: asset URL, verified digest and the purl
# checksum qualifier (the field that survives syft's merge into the published
# SBOM) all follow --arch.
check "$tmpdir/amd64.spdx.json" '
  .packages[] | select(.name == "goose") |
  .downloadLocation | endswith("goose-x86_64-unknown-linux-musl.tar.gz")' \
  "amd64 manifest: goose download URL is not the x86_64 asset"
check "$tmpdir/amd64.spdx.json" '
  .packages[] | select(.name == "goose") | .externalRefs[0].referenceLocator ==
  "pkg:github/aaif-goose/goose@canary?checksum=sha256:'"$GOOSE_X86_64"'"' \
  "amd64 manifest: goose purl does not carry the verified digest qualifier"
check "$tmpdir/arm64.spdx.json" '
  .packages[] | select(.name == "ripgrep") | .externalRefs[0].referenceLocator ==
  "pkg:github/burntsushi/ripgrep@15.2.0?checksum=sha256:'"$RG_AARCH64"'"' \
  "arm64 manifest: ripgrep purl does not carry the verified digest qualifier"
check "$tmpdir/amd64.spdx.json" '
  .packages[] | select(.name == "goose") | .checksums ==
  [{"algorithm": "SHA256", "checksumValue": "'"$GOOSE_X86_64"'"}]' \
  "amd64 manifest: goose checksum is not the x86_64 asset digest"
check "$tmpdir/arm64.spdx.json" '
  .packages[] | select(.name == "goose") |
  .downloadLocation | endswith("goose-aarch64-unknown-linux-musl.tar.gz")' \
  "arm64 manifest: goose download URL is not the aarch64 asset"
check "$tmpdir/arm64.spdx.json" '
  .packages[] | select(.name == "goose") | .checksums ==
  [{"algorithm": "SHA256", "checksumValue": "'"$GOOSE_AARCH64"'"}]' \
  "arm64 manifest: goose checksum is not the aarch64 asset digest"

# Publisher-specific architecture tokens: gh says arm64, ripgrep says
# aarch64-unknown-linux-musl, codex says aarch64.
check "$tmpdir/arm64.spdx.json" '
  [.packages[] | select(.name == "gh" or .name == "tmux" or .name == "ripgrep" or .name == "codex") |
   .downloadLocation] ==
  ["https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_arm64.tar.gz",
   "https://github.com/tmux/tmux-builds/releases/download/v3.7b/tmux-3.7b-linux-arm64.tar.gz",
   "https://github.com/openai/codex/releases/download/rust-v0.147.0/codex-aarch64-unknown-linux-musl.tar.gz",
   "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-aarch64-unknown-linux-musl.tar.gz"]' \
  "arm64 manifest: per-publisher architecture tokens diverge"

# A pin that is not a digest must fail the generator, not bake a bad record.
if generate x86_64 "$tmpdir/bad.spdx.json" \
  --goose-sha256-x86-64 not-a-digest >/dev/null 2>&1; then
  echo "::error::generator accepted a malformed goose sha256"
  fail=1
fi
if generate s390x "$tmpdir/bad.spdx.json" >/dev/null 2>&1; then
  echo "::error::generator accepted an unsupported architecture"
  fail=1
fi
if generate x86_64 "$tmpdir/bad.spdx.json" \
  --hive-commit deadbeef >/dev/null 2>&1; then
  echo "::error::generator accepted a short Hive commit"
  fail=1
fi

# The wiring that makes the manifest reach the published SBOM: the image
# build bakes it and the publish workflow enables syft's sbom-cataloger.
grep -qF 'COPY --chmod=0755 scripts/generate-sbom-manifest.py /usr/local/libexec/review-sbom-manifest' image/Containerfile || {
  echo "::error file=image/Containerfile::SBOM manifest generator is not copied into the build"
  fail=1
}
grep -qF 'rm -f /usr/local/libexec/review-sbom-manifest;' image/Containerfile || {
  echo "::error file=image/Containerfile::SBOM manifest generator must not be retained in the image"
  fail=1
}
grep -qF -- '--out /opt/bluefin/sbom/review-components.spdx.json' image/Containerfile || {
  echo "::error file=image/Containerfile::SBOM manifest must be written to /opt/bluefin/sbom/"
  fail=1
}
grep -qF '!scripts/generate-sbom-manifest.py' .dockerignore || {
  echo "::error file=.dockerignore::generate-sbom-manifest.py is not allowed into the build context"
  fail=1
}
grep -qF 'SYFT_SELECT_CATALOGERS: "+sbom-cataloger"' .github/workflows/publish-compat-image.yml || {
  echo "::error file=.github/workflows/publish-compat-image.yml::syft sbom-cataloger is not enabled for the SBOM step"
  fail=1
}
# SC2016: the argument is a literal to grep for, never an expansion.
# shellcheck disable=SC2016
grep -qF -- '--build-arg REVIEW_REVISION="${GITHUB_SHA}"' .github/workflows/publish-compat-image.yml || {
  echo "::error file=.github/workflows/publish-compat-image.yml::REVIEW_REVISION build arg is not passed"
  fail=1
}

[[ "$fail" -eq 0 ]] && echo "✓ SBOM manifest contract holds."
exit "$fail"
