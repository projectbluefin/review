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
|_| \_\_____|\___/|___|_____|_/  \_|
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
pi)
  command -v pi >/dev/null 2>&1 || {
    note 'ERROR: Pi backend selected but pi is not installed.'
    exit 1
  }
  pi --version >/dev/null 2>&1 || {
    note 'ERROR: Pi backend selected but pi is not executable.'
    exit 1
  }
  [ -n "${ANTHROPIC_API_KEY:-}" ] || {
    note 'ERROR: Pi backend selected but its Anthropic credential is missing.'
    exit 1
  }
  export PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
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
# needs GH_TOKEN and Goose but no Hive registration, so it skips the
# contributor.env gate and the Hive handover below.
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
  note 'Bluefin Operations | review dashboard starting (no Hive)'
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
# over anything in the controlled config. Goose is Copilot-only; Pi gets its
# own selected provider credential below.
if [ "$selected_backend" = goose ]; then
  export GOOSE_PROVIDER=github_copilot
fi

# Goose refuses to start without a model. Keep the direct-image fallback in
# sync with the launcher's default for users who invoke this image directly.
if [ -z "${GOOSE_MODEL:-}" ]; then
  GOOSE_MODEL="gpt-5.6-luna"
  note "GOOSE_MODEL not set; defaulting to ${GOOSE_MODEL} for GitHub Copilot"
fi
export GOOSE_MODEL

export GOOSE_THINKING_EFFORT="${GOOSE_THINKING_EFFORT:-high}"

if [ "$review_dashboard" = true ]; then
  banner 'PR queue dashboard (no Hive)'
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

if [ "$review_dashboard" = true ]; then
  # The dashboard gets its context the way a Hive session does, minus Hive:
  # source the pinned runtime's extension seam (/etc/hive/entrypoint.d), whose
  # hook owns the hosted hub URL and the curl rewrite to its authenticated
  # endpoint, then fetch the knowledge export with upstream's own expression.
  # The hook gates on HIVE_HUB, so it is sourced twice: once to learn the hub
  # it owns, once with HIVE_HUB exported so the rewrite installs. The hub URL
  # is defined once in this image, in the hook — never here.
  if [ -n "${GH_TOKEN:-}" ]; then
    shopt -s nullglob
    for hook in /etc/hive/entrypoint.d/*.sh; do
      # shellcheck disable=SC1090
      [ -r "$hook" ] && . "$hook"
    done
    if [ -n "${hosted_hub:-}" ]; then
      export HIVE_HUB="$hosted_hub"
      for hook in /etc/hive/entrypoint.d/*.sh; do
        # shellcheck disable=SC1090
        [ -r "$hook" ] && . "$hook"
      done
      hub_http="${HIVE_HUB/wss:\/\//https://}"
      curl -sf --max-time 30 "${hub_http%/contribute}/api/knowledge/export" \
        -o "${HOME}/agent.md" || rm -f "${HOME}/agent.md"
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
  note 'Bluefin Operations | maintainer review dashboard (no Hive)'
  exec /opt/bluefin/tui/.venv/bin/python /opt/bluefin/tui/bluefin_review_tui.py "$@"
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
