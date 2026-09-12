#!/usr/bin/env python
"""Drive the shipped dashboard entry point behind a real pseudo-terminal."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get("BLUEFIN_REVIEW_TUI_PYTHON", os.sys.executable)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9])")

GH_FIXTURE = r'''#!/bin/sh
printf '%s\n' "$*" >>"$GH_CALLS"
case "$*" in
  "api user"*) printf '%s\n' fixture-maintainer ;;
  *graphql*)
    printf '%s\n' '[{"data":{"search":{"pageInfo":{"hasNextPage":false},"nodes":[{"number":168,"title":"test: terminal smoke fixture","updatedAt":"2026-09-12T00:00:00Z","author":{"login":"fixture-contributor"},"repository":{"nameWithOwner":"projectbluefin/review"},"labels":{"nodes":[]},"reviewDecision":"REVIEW_REQUIRED","baseRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","headRefOid":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","mergeable":"MERGEABLE","commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"SUCCESS"}}}]},"reviews":{"nodes":[]}}]}}}]'
    ;;
  "pr list"*) printf '%s\n' '[]' ;;
  "pr view"*) printf '%s\n' '{}' ;;
  *) printf '%s\n' '[]' ;;
esac
'''


def resize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def visible(raw: bytes) -> str:
    return ANSI.sub("", raw.decode("utf-8", "replace")).replace("\x1b", "")


class TerminalSession:
    def __init__(self, width: int, height: int, preferences: dict[str, str] | None = None):
        self.temporary = tempfile.TemporaryDirectory(prefix="dashboard-pty-")
        root = Path(self.temporary.name)
        gh = root / "gh"
        gh.write_text(GH_FIXTURE, encoding="utf-8")
        gh.chmod(0o755)
        self.calls = root / "gh-calls"
        master, slave = pty.openpty()
        resize(slave, width, height)
        environment = {
            **os.environ,
            "PATH": f"{root}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(ROOT / "image"),
            "XDG_STATE_HOME": str(root / "state"),
            "GH_CALLS": str(self.calls),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }
        environment.pop("GH_TOKEN", None)
        environment.pop("GITHUB_TOKEN", None)
        environment.update(preferences or {})
        self.master = master
        self.output = bytearray()
        self.process = subprocess.Popen(
            [PYTHON, "-m", "tui.bluefin_review_tui"], cwd=ROOT, env=environment,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            close_fds=True,
        )
        os.close(slave)

    def read_until(self, text: str, timeout: float = 8.0) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if text in visible(self.output):
                return time.monotonic() - started
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    break
            if self.process.poll() is not None:
                break
        raise AssertionError(
            f"terminal never rendered {text!r}; tail={visible(self.output)[-1000:]!r}"
        )

    def press(self, keys: str) -> None:
        os.write(self.master, keys.encode())

    def clear_capture(self) -> None:
        self.output.clear()

    def resize(self, width: int, height: int) -> None:
        resize(self.master, width, height)

    def close(self) -> None:
        if self.process.poll() is None:
            self.press("q")
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=4)
        os.close(self.master)
        self.temporary.cleanup()


class DashboardTerminalSmoke(unittest.TestCase):
    def test_keyboard_journey_uses_a_real_pty_and_never_opens_a_browser(self) -> None:
        session = TerminalSession(80, 24)
        try:
            session.read_until("terminal smoke fixture")
            session.clear_capture()
            session.press("?")
            feedback = session.read_until("MUTATIONS & DECISIONS")
            self.assertLess(feedback, 1.0, "local key feedback blocked the UI thread")
            session.clear_capture()
            session.press("\x1b")
            session.read_until("terminal smoke fixture")
            session.clear_capture()
            session.press("I")
            session.read_until("Issues view")
            session.read_until("terminal smoke fixture")
            session.resize(72, 20)
            session.clear_capture()
            session.press("?")
            session.read_until("MUTATIONS & DECISIONS")
            session.clear_capture()
            session.press("q")
            session.read_until("Issues view")
            session.press("q")
            self.assertEqual(session.process.wait(timeout=4), 0)
            calls = session.calls.read_text(encoding="utf-8")
            self.assertNotRegex(
                calls, r"(^|\s)(browse|pr (merge|close|comment|edit|review))(\s|$)"
            )
        finally:
            session.close()

    def test_terminal_capability_matrix_retains_textual_meaning(self) -> None:
        cases = {
            "minimum": (80, 24, {}),
            "narrow-degraded": (72, 20, {"BLUEFIN_REVIEW_ASCII": "1"}),
            "true-color-reduced-motion": (
                120, 40, {"BLUEFIN_REVIEW_REDUCED_MOTION": "1"}
            ),
            "no-color": (160, 48, {"NO_COLOR": "", "COLORTERM": ""}),
        }
        for name, (width, height, preferences) in cases.items():
            with self.subTest(name=name):
                session = TerminalSession(width, height, preferences)
                try:
                    hydration = session.read_until("Queue: 1 PRs")
                    self.assertLess(hydration, 6.0, "cached fixture hydration stalled")
                    screen = visible(session.output)
                    self.assertIn("Queue: 1 PRs", screen)
                    self.assertIn("review", screen.lower())
                finally:
                    session.close()


if __name__ == "__main__":
    unittest.main()
