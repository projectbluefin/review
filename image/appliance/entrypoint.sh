#!/usr/bin/bash
# The image is an immutable appliance: omp configuration that makes sense on a
# developer workstation must not silently become appliance startup policy.
set -eu

export COPILOT_INTEGRATION_ID="${COPILOT_INTEGRATION_ID:-copilot-developer-cli}"
export COPILOT_GITHUB_TOKEN="${COPILOT_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
export GITHUB_COPILOT_TOKEN="${GITHUB_COPILOT_TOKEN:-${COPILOT_GITHUB_TOKEN:-}}"
profile="bluefin-review-appliance"
if [ "${BLUEFIN_REVIEW_INHERIT_OMP_CONFIG:-0}" = 1 ]; then
  profile="review"
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
routing_profile="${BLUEFIN_REVIEW_ROUTING_PROFILE:-copilot-mixed}"
case "$routing_profile" in
copilot-mixed | codex-subscription)
  ;;
*)
  echo "unknown Bluefin Review routing profile '$routing_profile'; expected copilot-mixed or codex-subscription" >&2
  exit 2
  ;;
esac
routing_dir="/usr/share/bluefin/review/profiles"
if [ ! -d "$routing_dir" ]; then
  routing_dir="$script_dir/profiles"
fi
routing_config="$routing_dir/${routing_profile}.yml"
if [ ! -f "$routing_config" ]; then
  echo "Bluefin Review routing profile '$routing_profile' is missing: $routing_config" >&2
  exit 2
fi
if [ -n "${PI_CONFIG_FILES:-}" ]; then
  export PI_CONFIG_FILES="${PI_CONFIG_FILES}:$routing_config"
else
  export PI_CONFIG_FILES="$routing_config"
fi

# Set up default git identity from the authenticated GitHub user if git identity is unset.
# This ensures fixers and automated merges do not fail with 'Committer identity unknown'
# or commit with unverified/unattributed emails that trip branch protection rulesets.
if [ -z "$(git config --global user.email 2>/dev/null || true)" ]; then
  if command -v gh >/dev/null 2>&1; then
    gh_user_json="$(gh api user 2>/dev/null || true)"
    if [ -n "$gh_user_json" ]; then
      user_login="$(echo "$gh_user_json" | jq -r '.login // empty' 2>/dev/null || true)"
      user_id="$(echo "$gh_user_json" | jq -r '.id // empty' 2>/dev/null || true)"
      user_name="$(echo "$gh_user_json" | jq -r '.name // .login // empty' 2>/dev/null || true)"
      if [ -n "$user_login" ] && [ -n "$user_id" ]; then
        git config --global user.email "${user_id}+${user_login}@users.noreply.github.com"
        git config --global user.name "${user_name:-$user_login}"
      fi
    fi
  fi
fi

case "${1:-}" in
update)
  cat >&2 <<'EOF'
Bluefin Review is an immutable appliance and cannot update itself.
Pull a newer container image and launch it to update.
EOF
  exit 2
  ;;
--help | -h | help)
  # OMP owns the rest of the help text. Remove its mutable-install update
  # command and replace it with the appliance contract below.
  omp --profile "$profile" \
    --extension /usr/share/bluefin/review/extension "$@" |
    sed '/^[[:space:]]*update[[:space:]]/d'
  cat <<'EOF'

Appliance lifecycle:
  This image is immutable. Replace it to update; `omp update` is disabled.
  Host OMP profiles and their MCP servers are isolated by default. Set
  BLUEFIN_REVIEW_INHERIT_OMP_CONFIG=1 to explicitly use the host `review` profile.
  BLUEFIN_REVIEW_ROUTING_PROFILE=copilot-mixed is the default; set it to
  codex-subscription for the explicit Codex subscription route. OMP may fall
  back to the parent model when child authentication is unavailable.
EOF
  exit 0
  ;;
esac
args=("$@")
advisor=false
for arg in "${args[@]}"; do
  [ "$arg" = --advisor ] && advisor=true
done
if [ "$advisor" = false ]; then
  args+=(--advisor)
fi

exec omp --profile "$profile" \
  --extension /usr/share/bluefin/review/extension "${args[@]}"
