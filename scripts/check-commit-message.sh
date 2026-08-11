#!/usr/bin/env bash
# Refuse a commit message that silently disables CI.
#
# GitHub skips every push-triggered workflow when the head commit message
# contains one of its skip directives, anywhere in the message. That is a
# feature when it is meant, and a trap when it is not: a commit that merely
# *writes about* one -- a fix for titles carrying "[skip ci]", a changelog
# entry, a test fixture quoted in the body -- lands on main with no validate
# run, no image published, and a green tick beside it because nothing ran.
#
# That happened here. The commit that taught the dashboard to escape bracketed
# titles quoted the directive in its body and skipped its own publish, so the
# fix sat on main while ':stable' stayed on the parent commit.
#
# Quote the directive with a zero-width-safe form instead: write it as
# "skip-ci" or "[skip⁠ ci]" prose, or refer to it as "GitHub's CI-skip
# directive". If a commit genuinely means to skip CI, pass --allow-skip-ci.
set -euo pipefail

message_file="${1:?usage: check-commit-message.sh <file>}"
message="$(cat "$message_file")"

if [[ "${ALLOW_SKIP_CI:-}" == "1" ]]; then
  exit 0
fi

# GitHub's documented set, matched case-insensitively as GitHub matches it.
directives=(
  "skip ci" "ci skip" "skip actions" "actions skip"
)

for directive in "${directives[@]}"; do
  if grep -qiE "\[[[:space:]]*${directive}[[:space:]]*\]" <<<"$message"; then
    cat >&2 <<EOF
commit message contains GitHub's CI-skip directive: [${directive}]

GitHub reads that anywhere in the message and skips every push-triggered
workflow, so this commit would land with no validation and no published
image -- and nothing on the commit would say so.

If you are writing *about* the directive, rephrase it: "GitHub's CI-skip
directive", or "skip-ci" without the brackets.

If you mean it, commit with ALLOW_SKIP_CI=1.
EOF
    exit 1
  fi
done
