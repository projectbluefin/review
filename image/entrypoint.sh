#!/usr/bin/env bash
# review container entrypoint.
#
# This wraps Hive's contributor runtime instead of replacing it. Hive owns the
# contributor WebSocket protocol, task selection, the tmux session, prompt
# injection and output capture; everything below is context setup that happens
# before handing control over.
set -euo pipefail

note() { printf 'review: %s\n' "$1" >&2; }

# Validate the caller's requested provider before any other startup work so an
# unsupported setting always gets the same actionable answer.
if [ -n "${GOOSE_PROVIDER:-}" ] && [ "$GOOSE_PROVIDER" != github_copilot ]; then
  note "ERROR: GOOSE_PROVIDER=${GOOSE_PROVIDER} is not supported — review supports GitHub Copilot only."
  note "  Unset GOOSE_PROVIDER or set GOOSE_PROVIDER=github_copilot."
  exit 1
fi

# A queue walk is the PR-review launch path: `bluefin-review queue` needs
# GH_TOKEN and Goose but no Hive registration, so it skips the contributor.env
# gate and the Hive handover below.
queue_walk=false
if [ "${1:-}" = queue ]; then
  queue_walk=true
  shift
fi

hive_config="${HOME}/.config/hive"
if [ "$queue_walk" = false ] && [ ! -f "${hive_config}/contributor.env" ]; then
  note "missing ${hive_config}/contributor.env"
  note "  mount your Hive config, or run: just contribute-setup goose"
  note "  walking the PR queue needs no Hive: run the image with 'queue'"
  exit 1
fi

if [ "$queue_walk" = true ]; then
  note 'Bluefin Operations | PR queue walk starting (no Hive)'
else
  note 'Bluefin Operations | contributor runtime starting'
fi
# --- Goose configuration -----------------------------------------------------
#
# GOOSE_PATH_ROOT is the image-owned policy, data, and state seam. The pinned
# Hive runtime preserves an existing ~/.config/goose/config.yaml, but its
# runtime-owned file and the image's controlled policy must remain separate.
export GOOSE_PATH_ROOT="${REVIEW_GOOSE_ROOT:-/opt/bluefin/goose}"

# Goose resolves environment before file, so the launcher's passthrough wins
# over anything in the controlled config. This image is intentionally
# Copilot-only; accepting another value would leave Hive to start a provider
# whose credential path this launcher does not support.
export GOOSE_PROVIDER=github_copilot

# Goose refuses to start without a model. Keep the direct-image fallback in
# sync with the launcher's default for users who invoke this image directly.
if [ -z "${GOOSE_MODEL:-}" ]; then
  GOOSE_MODEL="gpt-5.6-luna"
  note "GOOSE_MODEL not set; defaulting to ${GOOSE_MODEL} for GitHub Copilot"
fi
export GOOSE_MODEL

export GOOSE_THINKING_EFFORT="${GOOSE_THINKING_EFFORT:-high}"

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

skills_root="${HOME}/.agents/skills"
if [ -d "$skills_root" ]; then
  shopt -s nullglob
  skills=("$skills_root"/*/SKILL.md)
  note "${#skills[@]} org skills available (load one with /<skill-name>)"
fi

# Contributor work is usually lint-gated, and the base ships no linter and no
# package manager to obtain one (fsdk-containers#89). Naming them at startup
# stops an agent from discovering it mid-task and reaching for a slow ad-hoc
# `npx --yes` download.
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

if [ "$queue_walk" = true ]; then
  # The Hive knowledge base normally reaches the agent through the Hive
  # runtime's ten-minute refresh. A queue walk has no Hive, so fetch the same
  # export once, with the walker's own token, onto the path bluefin-review
  # already names in its review instructions. Best-effort: the hub's auth
  # redirects an expired token, and a walk without the export still works.
  if [ -n "${GH_TOKEN:-}" ]; then
    curl --fail --silent --show-error --max-time 30 \
      --header "Authorization: Bearer ${GH_TOKEN}" \
      "https://hosted-projectbluefin-knuckle-gjvq.hive.kubestellar.io/api/v1/knowledge" \
      -o "${HOME}/agent.md" || rm -f "${HOME}/agent.md"
  fi
  note 'Bluefin Operations | PR queue walk (no Hive)'
  exec bluefin-review queue "$@"
fi

# --- Hand over to Hive -------------------------------------------------------
#
# contributor-agent.sh creates the tmux session named "contributor", starts the
# relay, and launches Goose by keystroke injection. Attaching to that session is
# Hive's own documented flow. Running it in the foreground is deliberate: the
# launcher never backgrounds or detaches the agent.
# The attach client must describe the terminal that actually renders tmux.
# The base ships the full terminfo database, so the caller's TERM normally
# resolves; the fallback covers terminals newer than the base's ncurses
# (e.g. xterm-ghostty). A truecolor caller (COLORTERM) gets the direct-color
# fallback; without it tmux downsamples every pane color to 256 and Goose
# renders the wrong colors.
tmux_fallback_term=xterm-256color
if ! infocmp "${TERM:-}" >/dev/null 2>&1; then
  case "${COLORTERM:-}" in
  truecolor | 24bit) tmux_fallback_term=xterm-direct ;;
  esac
  note "TERM=${TERM:-<unset>} has no terminfo; using ${tmux_fallback_term}"
  export TERM="$tmux_fallback_term"
fi
agent_pid=
attach_pid=
# Podman sends SIGTERM and waits ten seconds before SIGKILL, so teardown has
# to be BOUNDED: an unbounded wait on a stuck agent stalls until that deadline
# and dies by SIGKILL, which is the "Ctrl-C stops it" promise failing in the
# only way a user can see. Two short steps, three seconds worst case, leave
# the deadline untouched.
#
# Nothing downstream depends on the agent exiting cleanly. Hive's hub releases
# the task itself when the socket drops -- its disconnect defer nils
# currentTask, logs 'task released on disconnect' and books a cooldown, and
# heartbeatLoop closes a half-open socket on a stale pong. A polite window for
# the agent's own exit trap is worth two seconds; it is not load-bearing, and
# must not grow into a shutdown protocol this repository does not owe anyone.
shutdown_grace_deciseconds=20

wait_for_exit() {
  # Poll rather than 'wait' so this is reusable from inside a trap handler,
  # where the child has usually already been reaped.
  local pid="$1" limit="$2" waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$limit" ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
}

cleanup() {
  status=$?
  # A second signal during teardown would re-enter this handler and restart
  # the escalation, stretching a bounded teardown past podman's deadline.
  trap '' HUP INT TERM
  if [ -n "$attach_pid" ] && kill -0 "$attach_pid" 2>/dev/null; then
    kill "$attach_pid" 2>/dev/null || true
  fi
  if [ -n "$agent_pid" ] && kill -0 "$agent_pid" 2>/dev/null; then
    kill -TERM "$agent_pid" 2>/dev/null || true
    wait_for_exit "$agent_pid" "$shutdown_grace_deciseconds"
    # Hive's agent script blocks on its own tmux session, so dropping the
    # session is what lets a stuck shutdown finish.
    tmux kill-session -t contributor 2>/dev/null || true
    wait_for_exit "$agent_pid" 10
    kill -KILL "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
  fi
  tmux kill-session -t contributor 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

/usr/local/bin/contributor-agent.sh "$@" &
agent_pid=$!

attempts=0
while ! tmux has-session -t contributor 2>/dev/null; do
  if ! kill -0 "$agent_pid" 2>/dev/null; then
    wait "$agent_pid"
    exit $?
  fi
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 600 ]; then
    note 'contributor session did not start'
    note "tmux readiness diagnostics: TMUX=${TMUX:-<unset>} TMUX_TMPDIR=${TMUX_TMPDIR:-<unset>}"
    tmux_state="$(tmux ls 2>&1 || true)"
    note "tmux readiness diagnostics: ${tmux_state//$'\n'/; }"
    exit 1
  fi
  sleep 0.1
done

# Attach only when there is a terminal. Without this an unattended run would
# fail on `tmux attach`, which refuses to run without a tty.
#
# The attach runs as a background job and is waited on rather than run in the
# foreground. Bash defers a trap handler until the foreground child returns, so
# a foreground `tmux attach-session` swallows SIGTERM/SIGINT for as long as the
# session is attached -- which is the entire run. Podman then hits its ten
# second deadline and SIGKILLs the container: Ctrl-C and `podman stop` both
# stall for ten seconds and the run ends by force, which is exactly the
# foreground guarantee in AGENTS.md failing where a user can see it. `wait` is
# interruptible, so this keeps PID 1 responsive to signals for the whole
# session. Job control is off here, so the background attach shares this
# shell's process group and still owns the terminal normally -- no
# SIGTTIN/SIGTTOU, no visible behaviour change.
#
# The explicit `<&3` matters: with job control off, bash redirects an
# asynchronous command's stdin from /dev/null unless the command carries a
# redirection of its own, and `tmux attach` dies with "open terminal failed:
# not a terminal" the moment it loses the tty.
if [ -t 0 ] && [ -t 1 ]; then
  exec 3<&0
  tmux attach-session -t contributor <&3 &
  attach_pid=$!
  wait "$attach_pid" || true
  attach_pid=
  exec 3<&-
  note 'tmux detached; the agent remains foreground in this terminal. Press Ctrl-C or close this terminal to stop it.'
  wait "$agent_pid"
else
  note 'no tty; following the agent without attaching'
  wait "$agent_pid"
fi
