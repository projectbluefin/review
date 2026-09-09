"""Focused Pilot contracts for the responsive dashboard surfaces."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual.app import App, ComposeResult
from textual.widgets import Static

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "image"))

from tui import bluefin_review_tui as tui  # noqa: E402
from tui import worker_status  # noqa: E402
from harness.autopilot import Discovery, HarnessOption  # noqa: E402
from harness.codex import CodexHarness  # noqa: E402
from harness.goose import GooseHarness  # noqa: E402
from tui.display_brand import display_brand, display_title  # noqa: E402
import tui.display_brand as display_brand_module  # noqa: E402


class ScreenHost(App):
    CSS = tui.ReviewDashboard.CSS

    def __init__(self, screen) -> None:
        super().__init__()
        self.target = screen

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(self.target)


def review_stop() -> tui.Stop:
    base = "a" * 40
    head = "b" * 40
    result = tui.ReviewResult(
        1,
        "complete",
        {"critical": 0, "high": 0, "medium": 0, "low": 0},
        [],
        [],
        {
            "repository": "projectbluefin/review",
            "pull_request": 7,
            "base_sha": base,
            "head_sha": head,
            "backend": "goose",
            "model": "gemini-3.8-flash",
        },
    )
    return tui.Stop(
        "projectbluefin/review",
        7,
        "review",
        "review body",
        live={"baseRefOid": base, "headRefOid": head},
        head_sha=head,
        review_result=result,
        review_status="complete",
    )


class ResponsiveTuiContractTests(unittest.TestCase):
    def test_pilot_notification_recorders_keep_their_original_sinks(self):
        import ast

        source = Path(__file__).with_name("dashboard_pilot.py").read_text()
        recorders = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name in {"record", "record_notice"}
            and any(isinstance(item, ast.Name) and item.id in {"notices", "_notices"}
                    for item in ast.walk(node))
        ]
        self.assertTrue(recorders)
        for recorder in recorders:
            with self.subTest(line=recorder.lineno):
                original, later = [], []
                forwarded = []
                namespace = {"notices": original,
                             "real_notify": lambda *args, **kwargs: forwarded.append(args)}
                exec(compile(ast.Module(body=[recorder], type_ignores=[]), "dashboard_pilot.py", "exec"), namespace)
                callback = namespace[recorder.name]
                namespace["notices"] = later
                namespace["real_notify"] = lambda *args, **kwargs: self.fail("late notification reached another app")
                callback("late notification")
                self.assertTrue(original)
                self.assertEqual(later, [])
                self.assertEqual(forwarded, [("late notification",)])

    def test_conflict_row_does_not_color_passing_ci_red(self):
        app = tui.ReviewDashboard()
        stop = tui.Stop("projectbluefin/review", 42, "resolve-conflicts", "conflicted PR",
                        mergeable_state="dirty", check_state="success")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            rendered = app.row_markup(stop)
        self.assertFalse(rendered.startswith("[red]"))
        self.assertIn("[bold red]⚑ CONFLICTS[/bold red]", rendered)
        self.assertIn("[green]✓ CI GREEN[/green]", rendered)

    def test_harness_banner_uses_selected_backend_model_and_effort(self):
        app = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        label = mock.MagicMock()
        app.query_one = lambda *_args, **_kwargs: label
        app.refresh_rows = lambda: None
        goose = HarnessOption(
            GooseHarness(model="gpt-5.6-sol", effort="high"),
            Discovery("goose", "ready", "ready", "ready", "gpt-5.6-sol", "high", tui.Availability.READY),
        )
        codex = HarnessOption(
            CodexHarness(model="claude-opus-5", effort="medium", availability=tui.Availability.READY),
            Discovery("codex", "ready", "ready", "ready", "claude-opus-5", "medium", tui.Availability.READY),
        )
        with mock.patch.object(tui, "ACTIVE_BACKEND", "goose"), mock.patch.dict(
            os.environ,
            {"GOOSE_MODEL": "gpt-5.6-sol", "GOOSE_THINKING_EFFORT": "high"},
            clear=False,
        ):
            app.harness_loaded([goose, codex])
        self.assertIn("Goose / gpt-5.6-sol", label.update.call_args.args[0])
        self.assertIn("effort: high", label.update.call_args.args[0])

        label.reset_mock()
        with mock.patch.object(tui, "ACTIVE_BACKEND", "codex"):
            app.harness_loaded([goose, codex])
        self.assertIn("Codex / claude-opus-5", label.update.call_args.args[0])
        self.assertIn("effort: medium", label.update.call_args.args[0])

    def test_harness_banner_reports_unknown_selection_without_fallback(self):
        app = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        label = mock.MagicMock()
        app.query_one = lambda *_args, **_kwargs: label
        app.refresh_rows = lambda: None
        unavailable = HarnessOption(
            CodexHarness(model="", effort="", availability=tui.Availability.UNAVAILABLE_BINARY),
            Discovery("codex", "missing", "missing", "unavailable", "", "", tui.Availability.UNAVAILABLE_BINARY),
        )
        with mock.patch.object(tui, "ACTIVE_BACKEND", "codex"):
            app.harness_loaded([unavailable])
        text = label.update.call_args.args[0]
        self.assertIn("Codex / model unknown", text)
        self.assertIn("effort: unknown", text)
        self.assertNotIn("gemini-3.8-flash", text)

        label.reset_mock()
        app.harness_loaded([])
        text = label.update.call_args.args[0]
        self.assertIn("backend unknown / model unknown", text)
        self.assertIn("effort: unknown", text)

    def test_display_brand_is_shared_configurable_and_markup_safe(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing-brand"
            self.assertEqual(display_brand(missing), "Review")
            brand_file = Path(root) / "display-brand"
            brand_file.write_text("# custom image brand\n[ORBIT]\x1b[31m\n")
            configured = display_brand(brand_file)
            self.assertEqual(configured, "[ORBIT][31m")
            self.assertEqual(
                display_title("DASHBOARD", brand_file),
                "[ORBIT][31m · DASHBOARD",
            )
            with mock.patch.object(
                display_brand_module,
                "display_brand",
                return_value=configured,
            ), mock.patch.object(
                worker_status,
                "display_brand",
                return_value=configured,
            ):
                self.assertEqual(
                    tui.ReviewDashboard().title,
                    "[ORBIT][31m · DASHBOARD",
                )
                self.assertEqual(
                    worker_status.WorkerStatusApp(lambda: None).title,
                    "[ORBIT][31m · WORKER STATUS",
                )
                from rich.console import Console

                rendered = Console().render_str(
                    worker_status.render_brand(color=True)
                )
                self.assertIn("[ORBIT][31m  / WORKER STATUS", rendered.plain)

    def test_preferences_have_explicit_no_color_ascii_and_reduced_motion_modes(self):
        with mock.patch.dict(
            os.environ,
            {
                "NO_COLOR": "",
                "BLUEFIN_REVIEW_ASCII": "1",
                "BLUEFIN_REVIEW_REDUCED_MOTION": "1",
            },
            clear=False,
        ):
            self.assertTrue(tui.no_color_requested())
            self.assertTrue(tui.ascii_ui_requested())
            self.assertTrue(tui.reduced_motion_requested())
            self.assertEqual(tui.ui_glyph("▶", ">"), ">")
            self.assertEqual(tui.ui_style("bold cyan"), "")

    def test_dashboard_no_color_removes_theme_colors_from_rendered_widgets(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}, clear=False):
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None

            async def exercise():
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    self.assertIn("no-color", app.classes)
                    for selector in ("#status-bar", "#activity", "#keys-acting"):
                        widget = app.query_one(selector)
                        self.assertEqual(widget.styles.color.ansi, -1)
                        self.assertEqual(widget.styles.background.ansi, -1)

            asyncio.run(exercise())

    def test_slow_draft_accepts_editor_input_and_rejects_changed_head(self):
        started = threading.Event()
        release = threading.Event()

        class SlowAdapter:
            capabilities = SimpleNamespace(body_drafting=True)

            def draft(self, _request):
                started.set()
                release.wait(timeout=5)
                return SimpleNamespace(
                    state=tui.DraftState.COMPLETE,
                    markdown="late generated body",
                    provenance={"source": "pilot"},
                )

        class Registry:
            def register(self, _harness):
                return None

            def require_ready(self, _backend):
                return SlowAdapter()

        stop = review_stop()
        screen = tui.ReviewBody(stop, "comment")
        with mock.patch.object(tui, "HarnessRegistry", Registry):
            async def exercise():
                async with ScreenHost(screen).run_test(size=(80, 24)) as pilot:
                    await pilot.click("#review-body-editor")
                    screen.query_one(tui.TextArea).text = "keep this"
                    screen.action_generate()
                    for _ in range(100):
                        if started.is_set():
                            break
                        await pilot.pause(0.01)
                    self.assertTrue(started.is_set())
                    await pilot.press("z")
                    self.assertEqual(screen.query_one(tui.TextArea).text, "zkeep this")
                    stop.head_sha = "c" * 40
                    stop.live["headRefOid"] = "c" * 40
                    release.set()
                    await screen.app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertEqual(
                        screen.query_one(tui.TextArea).text,
                        "zkeep this",
                    )

            asyncio.run(exercise())

    def test_draft_applies_when_preexisting_editor_text_is_unchanged(self):
        class FastAdapter:
            capabilities = SimpleNamespace(body_drafting=True)

            def draft(self, _request):
                return SimpleNamespace(
                    state=tui.DraftState.COMPLETE,
                    markdown="generated body",
                    provenance={"source": "pilot"},
                )

        class Registry:
            def register(self, _harness):
                return None

            def require_ready(self, _backend):
                return FastAdapter()

        screen = tui.ReviewBody(review_stop(), "comment")
        with mock.patch.object(tui, "HarnessRegistry", Registry):
            async def exercise():
                async with ScreenHost(screen).run_test(size=(80, 24)) as pilot:
                    editor = screen.query_one(tui.TextArea)
                    editor.text = "preexisting body"
                    screen.action_generate()
                    for _ in range(200):
                        if editor.text == "generated body":
                            break
                        await pilot.pause(0.01)
                    self.assertEqual(editor.text, "generated body")

            asyncio.run(exercise())

    def test_landing_target_and_log_following_stay_consistent_across_batches(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)

            def task(name: str, number: int) -> tui.landing.LandingTask:
                status = root_path / f"{name}.jsonl"
                log = root_path / f"{name}.log"
                key = f"projectbluefin/review#{number}"
                status.write_text(
                    json.dumps({"expect": [key]})
                    + "\n"
                    + json.dumps({"pr": key, "state": "merged", "note": name})
                    + "\n"
                    + json.dumps({"state": "done", "note": name})
                    + "\n"
                )
                log.write_text("\n".join(f"{name}-{line}" for line in range(240)))
                return tui.landing.LandingTask(
                    name,
                    [tui.Stop("projectbluefin/review", number, "review", name)],
                    "reviewer",
                    status_path=str(status),
                    log_path=str(log),
                )

            first = task("batch-one", 1)
            second = task("batch-two", 2)
            dashboard = SimpleNamespace(
                landing_queue=[first, second],
                hive_state="ready",
                _landing_task_active=lambda _task: False,
            )
            screen = tui.LandingScreen(dashboard)

            async def exercise():
                async with ScreenHost(screen).run_test(size=(80, 24)) as pilot:
                    screen.select_task(first.task_id)
                    await pilot.pause()
                    self.assertEqual(screen._selected_task(), first)
                    self.assertIn("target batch-one", str(screen.query_one("#landing-status").render()))
                    log = screen.query_one("#landing-log", tui.RichLog)
                    log_text = "\n".join(line.text for line in log.lines)
                    self.assertIn("batch-one-239", log_text)
                    self.assertNotIn("batch-two-239", log_text)
                    self.assertEqual(log.max_lines, 200)
                    screen.select_task(second.task_id)
                    await pilot.pause()
                    self.assertEqual(screen._selected_task(), second)
                    self.assertIn("target batch-two", str(screen.query_one("#landing-status").render()))
                    log_text = "\n".join(line.text for line in log.lines)
                    self.assertIn("batch-two-239", log_text)
                    self.assertNotIn("batch-one-239", log_text)

            asyncio.run(exercise())

    def test_landing_progress_counts_only_explicit_external_waits(self):
        stop = tui.Stop(
            "projectbluefin/review",
            425,
            "review",
            "waiting fixture",
        )
        with tempfile.TemporaryDirectory() as root:
            status = Path(root) / "waiting.jsonl"
            status.write_text(json.dumps({"expect": [stop.key]}) + "\n")
            task = tui.landing.LandingTask(
                "waiting",
                [stop],
                "reviewer",
                status_path=str(status),
                started=10,
                process=object(),
            )
            progress = tui.landing.progress_snapshot(task, now=10)
            self.assertEqual(progress.waiting, 0)
            self.assertEqual(progress.stage, "starting")
            status.write_text(
                json.dumps({"pr": stop.key, "state": "waiting-ci", "ts": 1})
                + "\n"
            )
            progress = tui.landing.progress_snapshot(task, now=10)
            self.assertEqual(progress.waiting, 1)
            self.assertEqual(progress.stage, "waiting-ci")

    def test_landing_viewer_uses_a_scoped_footer_at_120_columns(self):
        stop = tui.Stop(
            "projectbluefin/review",
            426,
            "review",
            "footer fixture",
        )
        with tempfile.TemporaryDirectory() as root:
            status = Path(root) / "footer-one.jsonl"
            status.write_text(json.dumps({"pr": stop.key, "state": "waiting"}) + "\n")
            first = tui.landing.LandingTask(
                "footer",
                [stop],
                "reviewer",
                status_path=str(status),
            )
            second_stop = tui.Stop(
                "projectbluefin/review",
                428,
                "review",
                "second footer fixture",
            )
            second_status = Path(root) / "footer-two.jsonl"
            second_status.write_text(
                json.dumps({"pr": second_stop.key, "state": "waiting"}) + "\n"
            )
            second = tui.landing.LandingTask(
                "footer-two",
                [second_stop],
                "reviewer",
                status_path=str(second_status),
            )
            dashboard = SimpleNamespace(
                landing_queue=[first, second],
                hive_state="ready",
                _landing_task_active=lambda _task: False,
            )
            screen = tui.LandingScreen(dashboard)

            async def exercise():
                async with ScreenHost(screen).run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    footer = screen.query_one("#landing-keys", tui.LandingFooter)
                    footer_keys = list(footer.children)
                    visible = " ".join(
                        str(child.render()) for child in footer_keys
                    )
                    self.assertIn("j next", visible)
                    self.assertIn("k previous", visible)
                    self.assertIn("x stop", visible)
                    self.assertIn("^p palette", visible)
                    self.assertNotIn("last item", visible)
                    previous = next(
                        child
                        for child in footer_keys
                        if child.action == "previous_batch"
                    )
                    self.assertTrue(await pilot.click(previous))
                    await pilot.pause()
                    self.assertIs(screen._selected_task(), first)
                    status = str(screen.query_one("#landing-status").render())
                    self.assertIn("[x] stop", status)
                    self.assertIn("[esc] back", status)
                    self.assertIn("evidence age unknown", status)

            asyncio.run(exercise())

    def test_ci_failure_hint_keeps_literal_control_labels(self):
        evidence = {
            "repository": "projectbluefin/review",
            "pull_request": 7,
            "head_sha": "b" * 40,
            "check": "unit",
            "run_id": 7,
            "conclusion": "FAILURE",
        }
        screen = tui.CIFailureScreen(review_stop(), evidence)

        async def exercise():
            async with ScreenHost(screen).run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                state = str(screen.query_one("#ci-log-state").render())
                self.assertIn("[i] load", state)
                self.assertIn("[esc] back", state)

        asyncio.run(exercise())

    def test_dispatch_feedback_stays_in_status_instead_of_covering_context(self):
        stop = tui.Stop(
            "projectbluefin/review",
            427,
            "review",
            "dispatch fixture",
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": root,
                "BLUEFIN_REVIEW_INSTANCE": "dispatch-feedback",
                "BLUEFIN_REVIEW_PARTITION_BATCH": "0",
            },
        ):
            app = tui.ReviewDashboard()
            app.self_login = "reviewer"
            app.final_policy = "automatic"
            # This contract supplies its own landing fixture. Keep the
            # dashboard's mount-time live queue and auxiliary probes from
            # replacing that identity with the host's authentication result.
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            app.enqueue_landing = lambda task: app.landing_queue.append(task)
            notices = []

            async def exercise():
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.notify = lambda message, *args, **kwargs: notices.append(
                        str(message)
                    )
                    app.plan_landing([stop])
                    for _ in range(100):
                        if isinstance(app.screen, tui.BatchPlanScreen):
                            break
                        await pilot.pause(0.01)
                    self.assertIsInstance(app.screen, tui.BatchPlanScreen)
                    await pilot.pause()
                    self.assertIsNotNone(
                        app.screen.query_one("#batch-dispatch", tui.Button)
                    )
                    await pilot.click("#batch-dispatch")
                    for _ in range(100):
                        if app.landing_queue:
                            break
                        await pilot.pause(0.01)
                    self.assertTrue(app.landing_queue)
                    self.assertFalse(
                        any("dispatched" in message for message in notices)
                    )
                    status_bar = app.query_one("#status-bar", tui.Static)
                    visible_status = ""
                    for _ in range(100):
                        visible_status = "".join(
                            segment.text for segment in status_bar.render_line(0)
                        )
                        if "last dispatched" in visible_status:
                            break
                        await pilot.pause(0.01)
                    self.assertIn("last dispatched", str(status_bar.render()))
                    self.assertIn("last dispatched", visible_status)
                    self.assertIn("review queue remains open | [I]", visible_status)

            asyncio.run(exercise())

    def test_dashboard_mounts_at_compact_and_desktop_sizes_with_queue_focus(self):
        async def exercise(size):
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                self.assertIs(app.focused, app.query_one("#queue"))
                self.assertLessEqual(app.query_one("#queue-pane").size.width, size[0])
                self.assertLessEqual(app.query_one("#right-pane").size.width, size[0])
                self.assertIsNotNone(app.query_one("#main-content"))

        asyncio.run(exercise((80, 24)))
        asyncio.run(exercise((120, 40)))
        asyncio.run(exercise((160, 50)))

    def test_compact_focus_skips_hidden_context_pane(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                queue = app.query_one("#queue")
                details = app.query_one("#details-pane")
                context = app.query_one("#context-pane")
                details.focus()
                await pilot.pause()
                app.action_pane_next()
                await pilot.pause()
                self.assertIs(app.focused, details)
                self.assertEqual(context.styles.display, "none")
                queue.focus()
                await pilot.pause()
                app.action_pane_next()
                await pilot.pause()
                self.assertIs(app.focused, details)

        asyncio.run(exercise())

    def test_compact_layout_keeps_both_key_rows_on_screen(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                for ident in ("#keys-reading", "#keys-acting"):
                    widget = app.query_one(ident)
                    self.assertGreater(widget.region.height, 0)
                    self.assertLessEqual(
                        widget.region.y + widget.region.height,
                        app.size.height,
                    )
                self.assertIn("A", str(app.query_one("#keys-acting").render()))
                self.assertIn("i", str(app.query_one("#keys-reading").render()))

        asyncio.run(exercise())

    def test_compact_notification_stays_above_steering_and_key_controls(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(80, 24), notifications=True) as pilot:
                await pilot.pause()
                app.notify("dispatch test message", timeout=5)
                racks = []
                for _ in range(20):
                    await pilot.pause(0.05)
                    racks = list(app.query("ToastRack"))
                    if any(rack.children for rack in racks):
                        break
                self.assertTrue(racks)
                self.assertEqual(racks[0].styles.margin.bottom, 9)
                toast_bottom = max(
                    child.region.y + child.region.height
                    for rack in racks
                    for child in rack.children
                )
                steer = app.query_one("#steer")
                reading = app.query_one("#keys-reading")
                self.assertLessEqual(toast_bottom, steer.region.y)
                self.assertLessEqual(toast_bottom, reading.region.y)

        asyncio.run(exercise())

    def test_desktop_queue_allocates_room_for_identity_and_action(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(180, 52)) as pilot:
                stop = tui.Stop(
                    "projectbluefin/review",
                    101,
                    "review",
                    "fix: desktop identity and action",
                    check_state="success",
                    live={"headRefOid": "1" * 40},
                )
                app.stops = [stop]
                app.populate(app.stops)
                await pilot.pause()
                self.assertGreaterEqual(app.query_one("#queue-pane").size.width, 92)

        asyncio.run(exercise())

    def test_standard_queue_keeps_ci_and_action_suffix_visible(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(120, 40)) as pilot:
                stop = tui.Stop(
                    "projectbluefin/review",
                    101,
                    "review",
                    "fix: " + ("long queue title " * 6),
                    check_state="success",
                    live={"headRefOid": "1" * 40},
                )
                app.stops = [stop]
                app.populate(app.stops)
                await pilot.pause()
                self.assertGreaterEqual(app.query_one("#queue-pane").size.width, 60)
                label = app.query_one("#queue Label")
                visible = "".join(segment.text for segment in label.render_line(0))
                self.assertIn("CI GREEN", visible)
                self.assertIn("[review]", visible)

        asyncio.run(exercise())

    def test_compact_queue_keeps_ci_and_action_suffix_visible(self):
        async def exercise():
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            async with app.run_test(size=(80, 24)) as pilot:
                stop = tui.Stop(
                    "projectbluefin/review",
                    102,
                    "fix-ci",
                    "fix: failed CI fixture",
                    check_state="failure",
                    live={"headRefOid": "2" * 40},
                )
                app.stops = [stop]
                app.populate(app.stops)
                await pilot.pause()
                label = app.query_one("#queue Label")
                visible = "".join(segment.text for segment in label.render_line(0))
                self.assertLess(
                    len(visible),
                    app.query_one("#queue").content_region.width,
                    "queue rows must reserve the ListView edge gutter",
                )
                self.assertIn("CI FAILED", visible)
                self.assertIn("[fix-ci]", visible)
                self.assertEqual(visible.count("FAILED"), 1)

        asyncio.run(exercise())

    def test_compact_activity_and_controls_keep_active_landing_context(self):
        with tempfile.TemporaryDirectory() as root:
            task = tui.landing.LandingTask(
                "compact-landing",
                [tui.Stop("projectbluefin/review", 151, "review", "landing")],
                "reviewer",
                status_path=str(Path(root) / "status.jsonl"),
                process=object(),
            )
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.landing_queue.append(task)

            async def exercise():
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    activity = app.query_one("#activity", tui.Static)
                    visible_activity = "\n".join(
                        "".join(segment.text for segment in activity.render_line(row))
                        for row in range(activity.region.height)
                    )
                    self.assertIn("projectbluefin/review#151", visible_activity)
                    steer = app.query_one("#steer", tui.Input)
                    self.assertIn("Enter", steer.placeholder)
                    self.assertIn("Esc", steer.placeholder)
                    self.assertLessEqual(
                        len(steer.placeholder), steer.content_region.width
                    )
                    reading = app.query_one("#keys-reading", tui.Static)
                    acting = app.query_one("#keys-acting", tui.Static)
                    self.assertEqual(reading.region.height, 1)
                    self.assertEqual(acting.region.height, 1)
                    reading_text = "".join(
                        segment.text for segment in reading.render_line(0)
                    )
                    acting_text = "".join(
                        segment.text for segment in acting.render_line(0)
                    )
                    self.assertIn("i:CI", reading_text)
                    self.assertIn("A:land", acting_text)

        asyncio.run(exercise())

    def test_active_landing_counts_as_queue_progress(self):
        with tempfile.TemporaryDirectory() as root:
            stop = tui.Stop(
                "projectbluefin/review",
                101,
                "review",
                "landing fixture",
                check_state="pending",
                live={
                    "headRefOid": "1" * 40,
                    "statusCheckRollup": [{
                        "name": "fixture-ci",
                        "state": "IN_PROGRESS",
                        "headSha": "1" * 40,
                    }],
                },
            )
            task = tui.landing.LandingTask(
                "active-landing",
                [stop],
                "reviewer",
                status_path=str(Path(root) / "status.jsonl"),
                process=object(),
            )
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
            app.load_issues = lambda: None
            app.stops = [stop]
            app.landing_queue.append(task)

            async def exercise():
                async with app.run_test(size=(120, 40)) as pilot:
                    app.populate(app.stops)
                    await pilot.pause()
                    self.assertEqual(app._queue_state(stop), "in progress")
                    status = str(app.query_one("#status-bar").render())
                    self.assertIn("1 in progress", status)
                    row = "".join(
                        segment.text
                        for segment in app.query_one("#queue Label").render_line(0)
                    )
                    self.assertIn("[review]", row)
                    self.assertNotIn("IN PROGRESS", row)

            asyncio.run(exercise())

    def test_ci_evidence_fetch_does_not_leave_a_covering_toast(self):
        stop = review_stop()
        app = tui.ReviewDashboard()
        app.load_ci_failure_evidence = lambda *_args: None
        notices = []
        app.notify = lambda message, *args, **kwargs: notices.append(str(message))
        app.open_ci_failure_logs(stop)
        self.assertEqual(notices, [])

    def test_editor_and_confirmation_keep_back_keys_and_input_isolated(self):
        async def exercise():
            root = ScreenHost(tui.ReviewBody(review_stop(), "comment"))
            async with root.run_test(size=(80, 24)) as pilot:
                await pilot.press("q")
                self.assertEqual(root.screen.query_one(tui.TextArea).text, "q")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(root.screen, root.screen_stack[0])
                root.push_screen(tui.ConfirmMutation([["gh", "pr", "merge"]], "7"))
                await pilot.pause()
                await pilot.press("q")
                self.assertEqual(root.screen.query_one(tui.Input).value, "q")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(root.screen, root.screen_stack[0])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
