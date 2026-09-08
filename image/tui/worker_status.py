"""Passive, attended status view for the Hive contributor worker."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import Footer, Header, Static
except ModuleNotFoundError:  # importable by socket-free contract tests
    App = object
    ComposeResult = object
    Vertical = Header = Footer = Static = None

from tui import hive_api

UNKNOWN = "unknown"


def _value(mapping, *keys):
    if not isinstance(mapping, dict):
        return UNKNOWN
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return str(value)
    return UNKNOWN


@dataclass(frozen=True)
class Projection:
    connection: str = UNKNOWN
    identity: str = UNKNOWN
    state: str = UNKNOWN
    repository: str = UNKNOWN
    issue: str = UNKNOWN
    title: str = UNKNOWN
    actionable: str = UNKNOWN
    contributors: str = UNKNOWN
    freshness: str = UNKNOWN
    attach: str = ""


def _freshness(value) -> str:
    if value in (None, "", UNKNOWN):
        return UNKNOWN
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
        return "<1m ago" if seconds < 60 else f"{seconds // 60}m ago"
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN


def project_status(payload: dict, *, identity: str = "") -> Projection:
    status = payload.get("status", {}) if isinstance(payload, dict) else {}
    fleet = payload.get("fleet", []) if isinstance(payload, dict) else []
    member = {}
    if isinstance(fleet, list):
        for candidate in fleet:
            if isinstance(candidate, dict) and candidate.get("identity") == identity:
                member = candidate
                break
    task = member.get("current_task") if isinstance(member.get("current_task"), dict) else {}
    return Projection(
        connection="online" if payload.get("ok") is True else UNKNOWN,
        identity=_value(member, "identity") if member else _value(status, "identity") if status else identity or UNKNOWN,
        state=_value(member, "state", "status"),
        repository=_value(task, "repository"),
        issue=_value(task, "issue", "number"),
        title=_value(task, "title"),
        actionable=_value(status, "actionable_items"),
        contributors=_value(status, "active_contributors"),
        freshness=_freshness(_value(status, "updated_at", "generated_at", "timestamp")),
    )


def attach_command() -> str:
    name = os.environ.get("REVIEW_CONTAINER_NAME", "review-container")
    return f"podman exec -it {shlex.quote(name)} tmux attach -t contributor"


def render_text(projection: Projection) -> str:
    return "\n".join((
        f"Connection: {projection.connection}    Identity: {projection.identity}",
        f"Worker: {projection.state}    Freshness: {projection.freshness}",
        f"Assignment repository: {projection.repository}",
        f"Assignment issue: {projection.issue}    Title: {projection.title}",
        f"Actionable items: {projection.actionable}    Contributors: {projection.contributors}",
        f"Attach: {projection.attach}",
    ))


class RefreshController:
    def __init__(self, reader, *, clock=time.monotonic, base_delay=5, max_delay=60):
        self.reader = reader
        self.clock = clock
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.next_allowed = 0.0
        self.delay = base_delay
        self._task = None

    def refresh(self):
        if self._task and not self._task.done():
            return self._task
        if self.clock() < self.next_allowed:
            return asyncio.create_task(asyncio.sleep(0, result=False))
        self._task = asyncio.create_task(self._read())
        return self._task

    async def _read(self):
        try:
            result = await self.reader()
        except Exception:
            self.next_allowed = self.clock() + self.delay
            self.delay = min(self.max_delay, self.delay * 2)
            return False
        self.delay = self.base_delay
        self.next_allowed = 0.0
        return result


if Static is not None:
    class WorkerStatusApp(App):
        CSS = "Screen { align: center middle; } #status { width: 90%; height: auto; padding: 1 2; }"
        BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

        def __init__(self, reader, *, identity="", **kwargs):
            super().__init__(**kwargs)
            self.reader = reader
            self.identity = identity
            self.controller = RefreshController(reader)
            self.projection = Projection(connection="starting", state="starting", attach=attach_command())

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical():
                yield Static(render_text(self.projection), id="status")
            yield Footer()

        def on_mount(self) -> None:
            self.set_interval(5, self.action_refresh)
            self.action_refresh()

        def action_refresh(self) -> None:
            task = self.controller.refresh()
            task.add_done_callback(lambda completed: self._show(completed.result()))

        def _show(self, result) -> None:
            if result is False:
                return
            payload = result if isinstance(result, dict) else {}
            if not payload.get("ok", True):
                self.projection = Projection(connection="disconnected", identity=self.identity,
                                             attach=attach_command())
            else:
                self.projection = project_status(payload.get("data", payload), identity=self.identity)
                self.projection = Projection(**{**self.projection.__dict__, "attach": attach_command()})
            self.query_one("#status", Static).update(render_text(self.projection))


def main() -> int:
    if Static is None:
        return 1
    identity = os.environ.get("CONTRIBUTOR_ID", "")
    base = os.environ.get("HIVE_HUB", "")
    if base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    if base.endswith("/contribute"):
        base = base[:-10]

    async def reader():
        return await asyncio.to_thread(hive_api.read_projections, base, os.environ.get("GH_TOKEN", ""))

    WorkerStatusApp(reader, identity=identity).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
