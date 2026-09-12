#!/usr/bin/env bash
# review container entrypoint.
#
# This wraps Hive's contributor runtime instead of replacing it. Hive owns the
# contributor WebSocket protocol, task selection, the tmux session, prompt
# injection and output capture; everything below is context setup that happens
# before handing control over.
set -euo pipefail

note() { printf 'review: %s\n' "$1" >&2; }

# Startup banner. Deep cyan, slate blue, and neon magenta, colors only when
# stderr is a terminal so captured logs stay plain text.
banner() {
  local mode="$1" c1='' c2='' c3='' r=''
  if [ -t 2 ]; then
    c1=$'\033[1;36m' c2=$'\033[38;5;68m' c3=$'\033[1;95m' r=$'\033[0m'
  fi
  {
    printf '%s' "$c1"
    cat <<'BANNER'
 ____  _____ _   _ ___ _____ _    _
|  _ \| ____| | | |_ _| ____| |  | |
| |_) |  _| | | | || ||  _| | |/\| |
|  _ <| |___| |_| || || |___|  /\  |
|_| \_\_____\___/|___|_____|_/  \_|
BANNER
    printf '%s      %sBLUEFIN REVIEW APPLIANCE%s\n' "$r" "$c3" "$r"
    printf '%s%s | model %s | effort %s%s\n' \
      "$c2" "$mode" "${GOOSE_MODEL:-provider default}" \
      "${GOOSE_THINKING_EFFORT:-provider default}" "$r"
  } >&2
}

# Validate the selected backend before startup. Hive remains responsible for
# assignment selection; this only proves the selected CLI can run here.
selected_backend="${AGENT_BACKEND:-goose}"
case "$selected_backend" in
goose)
  if [ -n "${GOOSE_PROVIDER:-}" ] && [ "$GOOSE_PROVIDER" != github_copilot ]; then
    note "ERROR: GOOSE_PROVIDER=${GOOSE_PROVIDER} is not supported — review supports GitHub Copilot only."
    note "  Unset GOOSE_PROVIDER or set GOOSE_PROVIDER=github_copilot."
    exit 1
  fi
  ;;
codex)
  command -v codex >/dev/null 2>&1 || {
    note 'ERROR: Codex backend selected but codex is not installed.'
    exit 1
  }
  codex --version >/dev/null 2>&1 || {
    note 'ERROR: Codex backend selected but codex is not executable.'
    exit 1
  }
  [ -r /home/dev/.codex/auth.json ] || {
    note 'ERROR: Codex backend selected but its subscription auth.json is missing.'
    exit 1
  }
  ;;
*)
  note "ERROR: unsupported Hive agent backend: ${selected_backend}."
  exit 1
  ;;
esac

# The maintainer review surface is the PR-review launch path: the dashboard
# needs GH_TOKEN and Goose but no mounted Hive registration, so it skips the
# contributor.env gate and the Hive handover below. The launcher may pass only
# HIVE_HUB so this surface can consult the selected deployment.
review_dashboard=false
if [ "${1:-}" = queue ]; then
  review_dashboard=true
  shift
fi

hive_config="${HOME}/.config/hive"
if [ "$review_dashboard" = false ] && [ ! -f "${hive_config}/contributor.env" ]; then
  note "missing ${hive_config}/contributor.env"
  note "  mount your Hive config, or run: just contribute-setup goose"
  note "  reviewing the PR queue needs no Hive: run the image with 'queue'"
  exit 1
fi

if [ "$review_dashboard" = true ]; then
  note 'Bluefin Operations | review dashboard starting'
else
  note 'Bluefin Operations | contributor runtime starting'
fi
# --- Goose configuration -----------------------------------------------------
#
# GOOSE_PATH_ROOT is the image-owned policy, data, and state seam. The pinned
# Hive runtime preserves an existing ~/.config/goose/config.yaml, but its
# runtime-owned file and the image's controlled policy must remain separate.
export GOOSE_PATH_ROOT="${REVIEW_GOOSE_ROOT:-/opt/bluefin/goose}"

# Prefer the backend selected by Hive's contributor registration if present.
# The launcher may also pass AGENT_BACKEND; prefer the mounted contributor.env
# selection so the image truly consumes Hive's decision rather than enforcing
# a local default.
if [ -f "${hive_config}/contributor.env" ]; then
  # Parse AGENT_BACKEND from the registration file if present.
  parsed_backend="$(awk -F= '$1=="AGENT_BACKEND" {sub(/^[^=]*=/, ""); gsub(/["'"'\ ]/, ""); print; exit}' "${hive_config}/contributor.env" 2>/dev/null || true)"
  if [ -n "${parsed_backend}" ]; then
    selected_backend="${parsed_backend}"
  fi
fi
# Fallback to a sensible default when neither the registration nor env set it.
if [ -z "${selected_backend}" ]; then
  selected_backend="goose"
fi

# Do not force a particular Goose provider here. Hive owns assignment and the
# contributor registration; the image should consume that selection rather
# than enforcing GitHub Copilot unconditionally. The launcher may pass
# GOOSE_PROVIDER explicitly if needed.

# Goose refuses to start without a model. Keep the direct-image fallback in
# sync with the launcher's default for users who invoke this image directly.
if [ -z "${GOOSE_MODEL:-}" ]; then
  GOOSE_MODEL="gemini-3.8-flash"
  note "GOOSE_MODEL not set; defaulting to ${GOOSE_MODEL}"
fi
export GOOSE_MODEL

export GOOSE_THINKING_EFFORT="${GOOSE_THINKING_EFFORT:-max}"

if [ "$review_dashboard" = true ]; then
  if [ -n "${HIVE_HUB:-}" ]; then
    banner 'PR queue dashboard (Hive configured)'
  else
    banner 'PR queue dashboard (Hive not configured)'
  fi
else
  banner 'Hive contributor'
fi

# No desktop keyring exists in a container; without this Goose fails to store or
# read provider secrets and falls back inconsistently.
export GOOSE_DISABLE_KEYRING=1

# Goose asks an interactive telemetry question on first run. Hive drives the
# CLI with simulated keystrokes, so an unanswered prompt hangs the agent.
export GOOSE_TELEMETRY_ENABLED="${GOOSE_TELEMETRY_ENABLED:-false}"

# Native skills advertise their descriptions at session start, but their bodies
# load on demand. Keep this small policy in every turn so the agent routes into
# the global inventory and each cloned repository's own skill catalog.
export GOOSE_MOIM_MESSAGE_FILE="${GOOSE_MOIM_MESSAGE_FILE:-/opt/bluefin/local-agent-policy.md}"

# --- Git hooks ---------------------------------------------------------------
#
# Hive's entrypoint sets user.name, user.email and credential.helper with
# `git config --global`, which writes individual keys and leaves core.hooksPath
# intact. Hooks are ergonomics only: --no-verify bypasses all of them.
if [ -d /opt/bluefin/git-hooks ]; then
  git config --global core.hooksPath /opt/bluefin/git-hooks || true
fi

# Contributor work forks the assigned repository, so `gh repo fork
# --remote=true` leaves both `origin` and `upstream` tracking a `main`. Git
# then refuses `git checkout main` with "matched multiple (2) remote tracking
# branches" and prints this exact setting as the hint. Name the fork's remote
# so the first checkout of a freshly forked repository just works.
git config --global checkout.defaultRemote origin || true

# The dashboard path never runs contributor-agent.sh, which is where Hive sets
# user.name and user.email. Without them `git commit` aborts with "Author
# identity unknown", so every fix, issue, and landing agent this surface
# dispatches dies the moment it tries to commit. Derive the identity from the
# same credential the agent already acts with, so a commit is attributable to
# the human whose token authorised it. The numeric-id noreply form is the one
# GitHub links back to the account; the bare login form does not on accounts
# created after 2017, and an unattributable commit additionally trips the
# require_extra_approval_for_unattributed_changes rule on every projectbluefin
# ruleset. Never overwrite an identity that is already set.
if [ -n "${GH_TOKEN:-}" ] && ! git config --global --get user.email >/dev/null 2>&1; then
  gh_identity="$(gh api user --jq '[.login, .id] | @tsv' 2>/dev/null || true)"
  if [ -n "$gh_identity" ]; then
    gh_login="${gh_identity%%	*}"
    gh_uid="${gh_identity##*	}"
    git config --global user.name "$gh_login" || true
    git config --global user.email "${gh_uid}+${gh_login}@users.noreply.github.com" || true
  else
    note 'GitHub identity lookup failed; git commits would abort with "Author identity unknown".'
  fi
fi

skills_root="${HOME}/.agents/skills"
if [ -d "$skills_root" ]; then
  shopt -s nullglob
  skills=("$skills_root"/*/SKILL.md)
  note "${#skills[@]} org skills available (load one with /<skill-name>)"
fi

# Contributor work is usually lint-gated. Name any unavailable validation
# tools at startup so an agent does not discover the gap mid-task.
validation_tools=(bats shellcheck hadolint systemd-analyze pre-commit just podman actionlint)
missing_validation_tools=()
for validation_tool in "${validation_tools[@]}"; do
  if ! command -v "$validation_tool" >/dev/null 2>&1; then
    missing_validation_tools+=("$validation_tool")
  fi
done
if ((${#missing_validation_tools[@]})); then
  note "validation tools unavailable: ${missing_validation_tools[*]} (fsdk-containers#89)"
fi

if [ "$review_dashboard" = true ]; then
  # The dashboard gets its context the way a Hive session does, minus Hive:
  # source the pinned runtime's extension seam (/etc/hive/entrypoint.d), whose
  # hook installs the exact curl rewrite for the selected hosted endpoint.
  # Then fetch the knowledge export with upstream's own expression. An absent
  # HIVE_HUB stays absent: queue mode never silently chooses a deployment.
  if [ -n "${GH_TOKEN:-}" ]; then
    shopt -s nullglob
    for hook in /etc/hive/entrypoint.d/*.sh; do
      # shellcheck disable=SC1090
      [ -r "$hook" ] && . "$hook"
    done
    if [ -n "${HIVE_HUB:-}" ]; then
      hub_http="${HIVE_HUB/wss:\/\//https://}"
      if ! curl -sf --max-time 30 "${hub_http%/contribute}/api/knowledge/export" \
        -o "${HOME}/agent.md"; then
        rm -f "${HOME}/agent.md"
        note "Hive knowledge export unavailable from ${hub_http%/contribute}; reviews continue without it."
      fi
      # The export stays a file the agent can search, and is deliberately NOT
      # linked to AGENTS.md/.goosehints/.goose-instructions.md. Goose loads
      # those into EVERY subprocess it starts, and 'goose review' starts one
      # per check: linking them spent the live export — 417 KB of scraped
      # documentation — of each check's context window before the diff was
      # read, and checks answered with prose or an empty response instead of
      # a verdict. The review scope's REVIEW.md names the path instead, so
      # the knowledge base is reachable at the cost of one line.
    fi
  fi
  if [ -n "${HIVE_HUB:-}" ]; then
    note 'Bluefin Operations | maintainer review dashboard (Hive configured)'
  else
    note 'Bluefin Operations | maintainer review dashboard (Hive not configured)'
  fi
  # The dashboard runs as a background job this shell waits on; it must NOT be
  # exec'd. PID 1 owes the container one duty the Textual process does not
  # perform: reaping adopted children. Goose review tool calls leave orphaned
  # grandchildren (defunct git/gh) whose intermediate shell exited first, and
  # reparented to an exec'd Python PID 1 — which never waitpid()s a process it
  # did not spawn — they accumulated as zombies for the whole session (#338).
  # Kept alive, this shell reaps them, `wait` stays interruptible so the trap
  # keeps PID 1 signal-responsive, and the status still propagates — the same
  # handover shape as the contributor path below, for the same reason.
  #
  # The explicit `<&3` matters as it does for the tmux attach below: with job
  # control off, bash redirects an asynchronous command's stdin from /dev/null
  # unless the command carries a redirection of its own, and the dashboard
  # which follows is started in the background with FD 3 attached to the
  # container's stdin so that it can prompt the maintainer without blocking
  # the reaper shell.  See the comments in the original upstream Hive entrypoint
  # for more detail.
