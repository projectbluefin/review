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
                    await screen.app.workers.wait_for_complete()
                    await pilot.pause()
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
            app.enqueue_landing = lambda task: app.landing_queue.append(task)
            notices = []

            async def exercise():
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.notify = lambda message, *args, **kwargs: notices.append(
                        str(message)
                    )
                    app.plan_landing([stop])
                    await pilot.pause()
                    self.assertIsInstance(app.screen, tui.BatchPlanScreen)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(app.landing_queue)
                    self.assertFalse(
                        any("dispatched" in message for message in notices)
                    )
                    self.assertIn(
                        "last dispatched",
                        str(app.query_one("#status-bar", tui.Static).render()),
                    )

            asyncio.run(exercise())

    def test_dashboard_mounts_at_compact_and_desktop_sizes_with_queue_focus(self):
        async def exercise(size):
            app = tui.ReviewDashboard()
            app.load_queue = lambda: None
            app.load_hive = lambda: None
            app.discover_harness = lambda: None
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
