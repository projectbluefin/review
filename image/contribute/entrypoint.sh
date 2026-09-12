#!/usr/bin/env bash
# Bluefin Contribute entrypoint: Hive's pinned contributor runtime owns the
# tmux session, prompt injection, and task selection; this only waits for
# that session to exist and attaches this attended terminal to it, so the
# OMP agent is visible immediately with no second terminal required.
set -euo pipefail

note() { printf 'contribute: %s\n' "$1" >&2; }

if [[ -n "${AGENT_BACKEND:-}" && "${AGENT_BACKEND}" != omp ]]; then
  echo "ERROR: this contribute image supports only AGENT_BACKEND=omp." >&2
  exit 64
fi
export AGENT_BACKEND=omp

# The attach client must describe the terminal that actually renders tmux.
# The base ships the full terminfo database, so the caller's TERM normally
# resolves; the fallback covers terminals newer than the base's ncurses
# (e.g. xterm-ghostty). A truecolor caller (COLORTERM) gets the direct-color
# fallback; without it tmux downsamples every pane color to 256 and OMP
# renders the wrong colors.
tmux_fallback_term=xterm-256color
if command -v infocmp >/dev/null 2>&1 && ! infocmp "${TERM:-}" >/dev/null 2>&1; then
  case "${COLORTERM:-}" in
  truecolor | 24bit) tmux_fallback_term=xterm-direct ;;
  esac
  note "TERM=${TERM:-<unset>} has no terminfo; using ${tmux_fallback_term}"
  export TERM="$tmux_fallback_term"
fi

agent_pid=
attach_pid=
status_pid=
# Podman/Apptainer send SIGTERM and wait before SIGKILL, so teardown has to be
# BOUNDED: an unbounded wait on a stuck agent stalls until that deadline and
# dies by SIGKILL, which is the "Ctrl-C stops it" promise failing in the only
# way a user can see. Two short steps, three seconds worst case.
#
# Nothing downstream depends on the agent exiting cleanly. Hive's hub releases
# the task itself when the socket drops.
shutdown_grace_deciseconds=20

wait_for_exit() {
  local pid="$1" limit="$2" waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$limit" ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
}

cleanup() {
  status=$?
  # A second signal during teardown would re-enter this handler and restart
  # the escalation, stretching a bounded teardown past the runtime's deadline.
  trap '' HUP INT TERM
  if [ -n "$status_pid" ] && kill -0 "$status_pid" 2>/dev/null; then
    kill "$status_pid" 2>/dev/null || true
  fi
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

# Lower third live status updater (blue styling: tracks Hive governor, issue/PR counts, and task state)
(
  update_status() {
    # shellcheck disable=SC2016
    node -e '
const fs = require("fs");
const https = require("https");
const http = require("http");

let token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN || "";
let hub = process.env.HIVE_HUB || "";
let taskFile = process.env.HIVE_TASK_FILE || "/tmp/contributor-task.json";

let activeTaskStr = "";
let activeTaskTitle = "";
if (fs.existsSync(taskFile)) {
  try {
    const t = JSON.parse(fs.readFileSync(taskFile, "utf8"));
    if (t && (t.number || t.title)) {
      const repo = t.repo ? t.repo.split("/").pop() : "";
      const kind = t.kind === "pull_request" ? "PR" : (t.kind ? t.kind.toUpperCase() : "TASK");
      activeTaskStr = `#[fg=#60a5fa]Task: #[bold,fg=#ffffff]${kind} #${t.number}#[nobold,fg=#93c5fd] (${repo}) #[fg=#3b82f6]| `;
      activeTaskTitle = `${kind} #${t.number} (${repo})${t.title ? " " + String(t.title).replace(/["`$\\]/g, "") : ""}`;
    }
  } catch (_) {}
}

let statusUrl = "";
if (hub) {
  const clean = hub.replace(/^wss:\/\//, "https://").replace(/^ws:\/\//, "http://").replace(/\/contribute$/, "");
  statusUrl = `${clean}/api/status`;
}

if (!statusUrl) {
  process.exit(0);
}

const reqMod = statusUrl.startsWith("https") ? https : http;
const headers = token ? { "Authorization": `Bearer ${token}` } : {};

const req = reqMod.get(statusUrl, { headers, timeout: 5000 }, (res) => {
  let body = "";
  res.on("data", (chunk) => body += chunk);
  res.on("end", () => {
    try {
      const data = JSON.parse(body);
      const gov = data.governor || {};
      const mode = (gov.mode || "active").toUpperCase();
      const issues = gov.issues !== undefined ? gov.issues : "-";
      const prs = gov.prs !== undefined ? gov.prs : "-";
      const agents = data.agents || [];
      const busyCount = agents.filter(a => a.busy).length;
      const pool = data.contributorPool || {};
      const workers = pool.active !== undefined ? `${pool.active}/${pool.registered || 0}` : "";

      const left = `#[bg=#1d4ed8,fg=#ffffff,bold] 🦖 BLUEFIN #[bg=#2563eb,fg=#ffffff,nobold] contribute #[bg=#1e40af,fg=#bfdbfe] 🐝 ${mode} #[default] `;
      let right = `${activeTaskStr}#[fg=#93c5fd]Issues: #[bold,fg=#ffffff]${issues}#[nobold,fg=#93c5fd] #[fg=#3b82f6]| #[fg=#93c5fd]PRs: #[bold,fg=#ffffff]${prs}#[nobold,fg=#93c5fd]`;
      if (workers) {
        right += ` #[fg=#3b82f6]| #[fg=#93c5fd]Workers: #[bold,fg=#ffffff]${workers}#[nobold,fg=#93c5fd]`;
        const reviewers = `${pool.active !== undefined ? pool.active : 0}/${pool.registered || 0}`;
        right += ` #[fg=#3b82f6]| #[fg=#93c5fd]Reviewers: #[bold,fg=#ffffff]${reviewers}#[nobold,fg=#93c5fd]`;
      }
      right += ` #[fg=#3b82f6]| #[fg=#bfdbfe]%H:%M #[default]`;

      const { execSync } = require("child_process");
      const termTitle = activeTaskTitle ? `contribute · ${activeTaskTitle}` : "contribute · idle";
      execSync(`tmux set-option -t contributor status on && tmux set-option -t contributor status-style "bg=#1e293b,fg=#93c5fd" && tmux set-option -t contributor status-left-length 70 && tmux set-option -t contributor status-left "${left}" && tmux set-option -t contributor status-right-length 140 && tmux set-option -t contributor status-right "${right}" && tmux set-option -t contributor set-titles on && tmux set-option -t contributor set-titles-string "${termTitle}"`, { stdio: "ignore" });
    } catch (_) {}
  });
});
req.on("error", () => {});
req.on("timeout", () => req.destroy());
' 2>/dev/null || true
  }

  while tmux has-session -t contributor 2>/dev/null; do
    update_status
    sleep 10
  done
) &
status_pid=$!

# Attach only when there is a terminal. Without this an unattended run would
# fail on `tmux attach`, which refuses to run without a tty.
#
# The attach runs as a background job and is waited on rather than run in the
# foreground: bash defers a trap handler until the foreground child returns,
# so a foreground `tmux attach-session` swallows SIGTERM/SIGINT for as long
# as the session is attached, and the runtime would then force the container
# closed by its own kill deadline instead of stopping cleanly on Ctrl-C.
# `wait` is interruptible, so this keeps PID 1 responsive to signals for the
# whole session.
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
