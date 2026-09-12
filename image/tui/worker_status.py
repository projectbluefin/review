"""Passive, attended status view for the Hive contributor worker."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Static
except ModuleNotFoundError:  # importable by socket-free contract tests
    App = object
    ComposeResult = object
    Horizontal = Vertical = VerticalScroll = Header = Footer = Static = None

# This file is launched by path from the image entrypoint; keep the image root
# on sys.path so the packaged `tui` siblings resolve exactly like the dashboard.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tui import hive_api
from tui.display_brand import display_brand, display_title

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
    read_state: str = "unknown"
    read_error: str = ""


def _freshness(value) -> str:
    if value in (None, "", UNKNOWN):
        return UNKNOWN
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
        return "<1m ago" if seconds < 60 else f"{seconds // 60}m ago"
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN


def project_status(payload: dict) -> Projection:
    envelope = payload if isinstance(payload, dict) else {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    status = data.get("status", {})
    me = data.get("me", {})
    task = me.get("current_task") if isinstance(me, dict) and isinstance(me.get("current_task"), dict) else {}
    active = me.get("active") if isinstance(me, dict) else None
    if active is True:
        state = "working" if task else "idle"
    elif active is False:
        state = "disconnected"
    else:
        state = UNKNOWN
    connection = _value(status, "hub") if envelope.get("ok", True) is True else UNKNOWN
    return Projection(
        connection=connection,
        identity=_value(me, "github_username"),
        state=state,
        repository=_value(task, "repo"),
        issue=_value(task, "number"),
        title=_value(task, "title"),
        actionable=_value(status, "actionable_items"),
        contributors=_value(status, "active_contributors"),
        freshness=_freshness(_value(status, "updated_at", "generated_at", "timestamp")),
        read_state="current",
    )


def attach_command() -> str:
    name = os.environ.get("REVIEW_CONTAINER_NAME", "review-container")
    return f"podman exec -it {shlex.quote(name)} tmux attach -t contributor"


def unavailable_projection() -> Projection:
    return Projection(
        connection="unavailable",
        state="unknown",
        attach=attach_command(),
        read_state="unavailable",
        read_error="hub read unavailable",
    )


def stale_projection(
    projection: Projection, age_seconds: float | None, error: str = "read failed"
) -> Projection:
    """Keep the last worker facts while making a failed read and its age explicit."""
    if age_seconds is None:
        age = "age unknown"
    else:
        seconds = max(0, int(age_seconds))
        age = f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"
    return Projection(
        **{
            **projection.__dict__,
            "freshness": f"stale · {age}",
            "read_state": "stale",
            "read_error": str(error or "read failed"),
        }
    )


def render_text(projection: Projection) -> str:
    return "\n".join((
        f"Connection: {projection.connection}    Identity: {projection.identity}",
        f"Worker: {projection.state}    Freshness: {projection.freshness}",
        f"Assignment repository: {projection.repository}",
        f"Assignment issue: {projection.issue}    Title: {projection.title}",
        f"Actionable items: {projection.actionable}    Contributors: {projection.contributors}",
        f"Attach: {projection.attach}",
    ))


def _escape(value) -> str:
    from rich.markup import escape

    return escape(str(value))


def _state_presentation(projection: Projection) -> tuple[str, str, str]:
    if projection.read_state == "starting":
        return "…", "STARTING", "starting"
    if projection.read_state == "unknown":
        return "?", "READ UNKNOWN", "warning"
    if projection.read_state == "stale":
        return "⚠", f"LAST KNOWN · {str(projection.state or UNKNOWN).upper()}", "warning"
    if projection.read_state == "unavailable":
        return "✗", "HUB UNAVAILABLE", "error"
    state = str(projection.state or UNKNOWN).lower()
    if state == "working":
        return "●", "WORKING", "active"
    if state == "idle":
        return "○", "IDLE", "idle"
    if state == "starting":
        return "…", "STARTING", "starting"
    if state == "disconnected":
        return "✗", "DISCONNECTED", "error"
    return "?", "UNKNOWN", "warning"


def render_state_badge(projection: Projection, *, color: bool = True) -> str:
    glyph, label, kind = _state_presentation(projection)
    plain = f"{glyph} {label}"
    if not color:
        return plain
    style = {
        "active": "bold cyan",
        "idle": "bold cyan",
        "starting": "bold cyan",
        "warning": "bold yellow",
        "error": "bold red",
    }[kind]
    return f"[{style}]{_escape(plain)}[/]"


def _connection_style(value: str) -> str:
    lowered = str(value).lower()
    if lowered in {"unavailable", "failed", "error", "disconnected"}:
        return "bold red"
    if lowered == UNKNOWN:
        return "bold yellow"
    return "bold cyan"


def _freshness_style(value: str) -> str:
    text = str(value)
    if text == UNKNOWN or text.startswith("stale") or text.endswith("m ago"):
        return "bold yellow"
    return "cyan"


def _read_style(projection: Projection) -> str:
    if projection.read_state == "stale":
        return "bold yellow"
    if projection.read_state == "unavailable":
        return "bold red"
    if projection.read_state in {"starting", "unknown"}:
        return "bold yellow"
    return "cyan"


def _section(title: str, rows: list[tuple[str, str, str]], *, color: bool) -> str:
    label_width = max(14, *(len(label) for label, _, _ in rows))
    if not color:
        return "\n".join(
            [title, *(f"{label:<{label_width}} {value}" for label, value, _ in rows)]
        )
    return "\n".join(
        [
            f"[bold cyan]{title}[/]",
            *(
                f"[dim]{label:<{label_width}}[/] [{style}]{_escape(value)}[/]"
                for label, value, style in rows
            ),
        ]
    )


def render_sections(projection: Projection, *, color: bool = True) -> dict[str, str]:
    """Render bounded, labeled sections without changing the plain projection."""
    _, _, state_kind = _state_presentation(projection)
    state_style = {
        "active": "bold cyan",
        "idle": "bold cyan",
        "starting": "bold cyan",
        "warning": "bold yellow",
        "error": "bold red",
    }[state_kind]
    value_style = "bright_white" if color else ""
    return {
        "connection": _section(
            "CONNECTION",
            [
                ("Hub", str(projection.connection), _connection_style(projection.connection)),
                ("Identity", str(projection.identity), value_style),
                (
                    "Read",
                    " ".join(
                        part
                        for part in (projection.read_state, projection.read_error)
                        if part
                    ),
                    _read_style(projection),
                ),
                ("Freshness", str(projection.freshness), _freshness_style(projection.freshness)),
            ],
            color=color,
        ),
        "worker": _section(
            "WORKER",
            [
                ("State", str(projection.state).upper(), state_style),
                ("Hub actionable", str(projection.actionable), value_style),
                ("Hub contributors", str(projection.contributors), value_style),
            ],
            color=color,
        ),
        "assignment": _section(
            "ASSIGNMENT",
            [
                ("Repository", str(projection.repository), value_style),
                ("Issue", str(projection.issue), value_style),
                ("Title", str(projection.title), value_style),
            ],
            color=color,
        ),
        "attach": _section(
            "ATTACH",
            [("Command", str(projection.attach or UNKNOWN), "cyan")],
            color=color,
        ),
    }


def render_brand(*, color: bool = True) -> str:
    """Render configurable branding with the functional worker label separate."""
    brand = display_brand()
    if not color:
        return f"{brand}  /  WORKER STATUS"
    return f"[bold cyan]{_escape(brand)}[/]  [dim]/ WORKER STATUS[/]"


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
        except Exception as error:
            result = {"ok": False, "category": "read", "message": type(error).__name__}
        if result is False:
            result = {"ok": False, "category": "read"}
        if isinstance(result, dict) and result.get("ok") is False:
            self._record_failure()
            return result
        self.delay = self.base_delay
        self.next_allowed = 0.0
        return result

    def _record_failure(self):
        self.next_allowed = self.clock() + self.delay
        self.delay = min(self.max_delay, self.delay * 2)


if Static is not None:
    class WorkerStatusApp(App):
        TITLE = display_title("WORKER STATUS")
        CSS = """
        Screen { align: center middle; background: $background; }
        Header { background: $primary; color: $text-primary; text-style: bold; }
        #status {
            width: 94%; max-width: 120; height: auto; max-height: 1fr;
            padding: 0 1; border: heavy $primary; background: $surface;
            overflow-y: auto;
        }
        #brand {
            height: 1; text-align: center; color: $text-accent;
            text-style: bold;
        }
        #state-badge {
            width: 100%; height: 1; text-align: center;
        }
        #state-badge.active { color: $text-accent; background: $primary-muted; text-style: bold; }
        #state-badge.idle, #state-badge.starting { color: $text-primary; background: $primary-muted; text-style: bold; }
        #state-badge.warning { color: $text-warning; background: $warning-muted; text-style: bold; }
        #state-badge.error { color: $text-error; background: $error-muted; text-style: bold; }
        #summary-sections { width: 100%; height: auto; }
        .section {
            width: 50%; height: auto; min-height: 4; padding: 0 1;
            border: round $secondary;
        }
        #assignment-section, #attach-section { width: 100%; }
        .section Static { height: auto; }
        .no-color #status, .no-color .section, .no-color #state-badge {
            color: auto; background: transparent; border: none; text-style: none;
        }
        .no-color Header, .no-color Footer { color: auto; background: transparent; }
        """
        BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

        def __init__(self, reader, **kwargs):
            super().__init__(**kwargs)
            self.title = display_title("WORKER STATUS")
            self.reader = reader
            self.controller = RefreshController(reader)
            self.projection = Projection(
                connection="starting",
                state="starting",
                attach=attach_command(),
                read_state="starting",
            )
            self.last_success_at: float | None = None
            self.use_color = "NO_COLOR" not in os.environ
            if not self.use_color:
                self.add_class("no-color")

        def compose(self) -> ComposeResult:
            sections = render_sections(self.projection, color=self.use_color)
            yield Header(show_clock=True)
            with VerticalScroll(id="status"):
                yield Static(render_brand(color=self.use_color), id="brand", markup=self.use_color)
                yield Static(
                    render_state_badge(self.projection, color=self.use_color),
                    id="state-badge",
                    markup=self.use_color,
                )
                with Horizontal(id="summary-sections"):
                    with Vertical(id="connection-section", classes="section"):
                        yield Static(sections["connection"], markup=self.use_color)
                    with Vertical(id="worker-section", classes="section"):
                        yield Static(sections["worker"], markup=self.use_color)
                with Vertical(id="assignment-section", classes="section"):
                    yield Static(sections["assignment"], markup=self.use_color)
                with Vertical(id="attach-section", classes="section"):
                    yield Static(sections["attach"], markup=self.use_color)
            yield Footer(compact=True)

        def on_mount(self) -> None:
            self.set_interval(5, self.action_refresh)
            self.action_refresh()

        def action_refresh(self) -> None:
            task = self.controller.refresh()
            task.add_done_callback(lambda completed: self._show(completed.result()))

        def _show(self, result) -> None:
            if result is False:
                if self.last_success_at is not None:
                    self.projection = stale_projection(
                        self.projection,
                        time.monotonic() - self.last_success_at,
                        "read backoff",
                    )
                    self._render_projection()
                return
            payload = result if isinstance(result, dict) else {}
            if not payload.get("ok", True):
                if self.last_success_at is None:
                    self.projection = unavailable_projection()
                else:
                    self.projection = stale_projection(
                        self.projection,
                        time.monotonic() - self.last_success_at,
                        payload.get("message") or payload.get("category") or "read failed",
                    )
            else:
                self.projection = project_status(payload)
                self.projection = Projection(**{**self.projection.__dict__, "attach": attach_command()})
                self.last_success_at = time.monotonic()
            self._render_projection()

        def _render_projection(self) -> None:
            sections = render_sections(self.projection, color=self.use_color)
            self.query_one("#brand", Static).update(
                render_brand(color=self.use_color)
            )
            badge = self.query_one("#state-badge", Static)
            for class_name in ("active", "idle", "starting", "warning", "error"):
                badge.remove_class(class_name)
            badge.add_class(_state_presentation(self.projection)[2])
            badge.update(render_state_badge(self.projection, color=self.use_color))
            for name, content in sections.items():
                self.query_one(f"#{name}-section Static", Static).update(content)
            if self.projection.issue != UNKNOWN and self.projection.repository != UNKNOWN:
                self.title = display_title(f"WORKER: #{self.projection.issue} ({self.projection.repository})")
            elif self.projection.state != UNKNOWN:
                self.title = display_title(f"WORKER: {self.projection.state.upper()}")


def main() -> int:
    if Static is None:
        return 1
    base = os.environ.get("HIVE_HUB", "")
    if base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    if base.endswith("/contribute"):
        base = base[:-10]

    async def reader():
        return await asyncio.to_thread(hive_api.read_projections, base, os.environ.get("GH_TOKEN", ""))

    WorkerStatusApp(reader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
