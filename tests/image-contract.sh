#!/usr/bin/env bash
# Static contract for the image-owned review runtime.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

fail=0
require() {
  local file="$1" want
  shift
  for want in "$@"; do
    grep -qF -- "$want" "$file" || {
      echo "::error file=${file}::missing required: ${want}"
      fail=1
    }
  done
}

forbid() {
  local file="$1" unwanted
  shift
  for unwanted in "$@"; do
    grep -qF -- "$unwanted" "$file" && {
      echo "::error file=${file}::must not contain: ${unwanted}"
      fail=1
    }
  done
  return 0
}

grep -qE '^ARG FSDK_RUNNER_IMAGE=ghcr\.io/projectbluefin/lab-runner(:[^@[:space:]]+)?@sha256:[0-9a-f]{64}$' image/Containerfile ||
  {
    echo "::error file=image/Containerfile::FSDK_RUNNER_IMAGE must be digest-pinned"
    fail=1
  }
# shellcheck disable=SC2016
require image/Containerfile \
  'FROM ${FSDK_RUNNER_IMAGE}' \
  'ARG HIVE_COMMIT=' \
  'ARG NODE_VERSION=' \
  'ARG GH_VERSION=' \
  'ARG TMUX_VERSION=' \
  'ARG CODEX_VERSION=' \
  'ARG GOOSE_CHANNEL=canary' \
  'ARG PI_VERSION=' \
  'COPY package.json package-lock.json /opt/hive/' \
  'COPY --chmod=0755 image/bin/bluefin-review /usr/local/bin/bluefin-review' \
  'COPY --chmod=0755 image/entrypoint.sh /usr/local/bin/review-entrypoint' \
  'PYTHONPATH=/opt/bluefin/tui:/opt/bluefin' \
  'COPY image/tmux.conf /etc/tmux.conf' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/bin/contributor-agent.sh' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/bin/contributor-relay.sh' \
  'https://raw.githubusercontent.com/kubestellar/hive/${HIVE_COMMIT}/config/backends.conf' \
  '/usr/local/bin/goose --version' \
  'tmux -V' \
  'codex --version' \
  'pi --version' \
  'ARG REVIEW_REVISION=unknown' \
  'COPY --chmod=0755 scripts/generate-sbom-manifest.py /usr/local/libexec/review-sbom-manifest' \
  'rm -f /usr/local/libexec/review-sbom-manifest;' \
  '--out /opt/bluefin/sbom/review-components.spdx.json' \
  'USER dev' \
  'WORKDIR /home/dev' \
  'ENTRYPOINT ["/usr/local/bin/review-entrypoint"]'

forbid image/Containerfile \
  'LAB_SKILLS_COMMIT' \
  'projectbluefin/lab/' \
  'apt-get' \
  'dnf install' \
  'apk add'

require tests/image-audit.sh \
  'required_sbom_components' \
  'check_sbom_components' \
  'fetch_spdx_predicate' \
  '/opt/bluefin/sbom/review-components.spdx.json'

# One platform must have one SPDX decision and one optional component fetch:
# duplicate conflict branches can otherwise pass static manifest tests without
# exercising the published-attestation path.
# shellcheck disable=SC2016
spdx_decision_count="$(grep -F -c -- \
  'if verify_predicate "${derived_repository}@${digest}" "$spdx_predicate"; then' \
  tests/image-audit.sh || true)"
if [[ "$spdx_decision_count" -ne 1 ]]; then
  echo "::error file=tests/image-audit.sh::expected one platform SPDX decision, found ${spdx_decision_count}"
  fail=1
fi
# shellcheck disable=SC2016
spdx_fetch_count="$(grep -F -c -- \
  'if spdx_document="$(fetch_spdx_predicate "${derived_repository}@${digest}")"; then' \
  tests/image-audit.sh || true)"
if [[ "$spdx_fetch_count" -ne 1 ]]; then
  echo "::error file=tests/image-audit.sh::expected one platform SPDX fetch, found ${spdx_fetch_count}"
  fail=1
fi
# shellcheck disable=SC2016
require tests/image-audit.sh 'if ! "$direct_copy"; then'

# The publish path must ingest the in-image manifest and require it in the
# per-platform attestation audit.
# shellcheck disable=SC2016
require .github/workflows/publish-compat-image.yml \
  'SYFT_SELECT_CATALOGERS: "+sbom-cataloger"' \
  'sbom-path: review-sbom-${{ matrix.arch }}.spdx.json' \
  '--require-attestations'

for path in \
  image/entrypoint.sh \
  image/bin/bluefin-review \
  image/config/goose.yaml \
  image/tmux.conf \
  image/tui/bluefin_review_tui.py \
  image/harness/goose.py \
  package.json \
  package-lock.json; do
  [[ -e "$path" ]] || {
    echo "::error file=${path}::required review runtime input is missing"
    fail=1
  }
done

grep -qF '!package.json' .dockerignore ||
  {
    echo "::error file=.dockerignore::package.json is not allowed into the build context"
    fail=1
  }
grep -qF '!package-lock.json' .dockerignore ||
  {
    echo "::error file=.dockerignore::package-lock.json is not allowed into the build context"
    fail=1
  }
grep -qF '!scripts/generate-sbom-manifest.py' .dockerignore ||
  {
    echo "::error file=.dockerignore::SBOM generator is not allowed into the build context"
    fail=1
  }

[[ "$fail" -eq 0 ]] && echo "✓ review runtime image contract holds."
exit "$fail"
