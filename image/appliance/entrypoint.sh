#!/usr/bin/bash
# The SIF is an immutable appliance: omp configuration that makes sense on a
# developer workstation must not silently become appliance startup policy.
set -eu

profile="bluefin-review-appliance"
if [ "${BLUEFIN_REVIEW_INHERIT_OMP_CONFIG:-0}" = 1 ]; then
  profile="review"
fi

case "${1:-}" in
update)
  cat >&2 <<'EOF'
Bluefin Review is an immutable appliance and cannot update itself.
Replace the SIF with the newer release asset, or pull a newer container image.
EOF
  exit 2
  ;;
--help|-h|help)
  # OMP owns the rest of the help text. Remove its mutable-install update
  # command and replace it with the appliance contract below.
  omp --profile "$profile" --config /usr/share/bluefin/review/appliance-config.yml \
    --extension /usr/share/bluefin/review/extension "$@" |
    sed '/^[[:space:]]*update[[:space:]]/d'
  cat <<'EOF'

Appliance lifecycle:
  This image/SIF is immutable. Replace it to update; `omp update` is disabled.
  Host OMP profiles and their MCP servers are isolated by default. Set
  BLUEFIN_REVIEW_INHERIT_OMP_CONFIG=1 to explicitly use the host `review` profile.
EOF
  exit 0
  ;;
esac

exec omp --profile "$profile" \
  --config /usr/share/bluefin/review/appliance-config.yml \
  --extension /usr/share/bluefin/review/extension "$@"
