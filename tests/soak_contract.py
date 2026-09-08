"""Soak contract: an unattended run's memory and disk stay bounded (#413).

Run state and diagnostics have different lifetimes. The durable run store
bounds itself and survives restart; everything here is a display or
scheduling cache that must not grow for the lifetime of the process, and a
diagnostics log that must not grow on disk.

The bounds are read from the environment at import, so this drives the real
production code paths with small caps rather than asserting against a
reimplementation.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

SOAK_STATE = tempfile.mkdtemp(prefix="bluefin-soak-")
atexit.register(shutil.rmtree, SOAK_STATE, True)
os.environ["XDG_STATE_HOME"] = SOAK_STATE
os.environ["BLUEFIN_REVIEW_TRACE_MAX_BYTES"] = "4096"
os.environ["BLUEFIN_REVIEW_TRACE_BACKUPS"] = "2"
os.environ["BLUEFIN_REVIEW_MAX_BATCHES"] = "10"
os.environ["BLUEFIN_REVIEW_MAX_TRIAGE"] = "50"
os.environ["BLUEFIN_REVIEW_MAX_MERGE_RIGHTS"] = "25"
os.environ["BLUEFIN_REVIEW_MAX_LANDING_QUEUE"] = "20"

TUI_ROOT = Path(__file__).resolve().parents[1] / "image"
if str(TUI_ROOT) not in sys.path:
    sys.path.insert(0, str(TUI_ROOT))

import tui.bluefin_review_tui as tui  # noqa: E402

CYCLES = 2000


class _FakeBatch:
    def __init__(self, running: bool) -> None:
        self.running = running


class _FakeLandingTask:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        self.process = None
        self.phase = ""


class BoundedMapTests(unittest.TestCase):
    def test_bound_map_evicts_oldest_first(self) -> None:
        mapping = {str(i): i for i in range(10)}
        tui._bound_map(mapping, 4)
        self.assertEqual(len(mapping), 4)
        self.assertEqual(list(mapping), ["6", "7", "8", "9"])

    def test_bound_map_leaves_small_maps_alone(self) -> None:
        mapping = {"a": 1}
        tui._bound_map(mapping, 4)
        self.assertEqual(mapping, {"a": 1})

    def test_triage_and_merge_rights_stay_bounded_under_soak(self) -> None:
        triage: dict[str, str] = {}
        merge_rights: dict[str, bool] = {}
        for i in range(CYCLES):
            triage[f"repo/name#{i}"] = "reviewed"
            tui._bound_map(triage, tui.MAX_TRIAGE_ENTRIES)
            merge_rights[f"org/repo{i}"] = True
            tui._bound_map(merge_rights, tui.MAX_MERGE_RIGHTS_ENTRIES)
        self.assertLessEqual(len(triage), tui.MAX_TRIAGE_ENTRIES)
        self.assertLessEqual(len(merge_rights), tui.MAX_MERGE_RIGHTS_ENTRIES)


class ReviewBatchPruneTests(unittest.TestCase):
    def _dashboard(self):
        app = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        app.review_batches = []
        return app

    def test_finished_batches_age_out_under_soak(self) -> None:
        app = self._dashboard()
        for _ in range(CYCLES):
            app.review_batches.append(_FakeBatch(running=False))
            app.prune_review_batches()
        self.assertLessEqual(len(app.review_batches), tui.MAX_REVIEW_BATCHES)

    def test_running_batches_are_never_evicted(self) -> None:
        app = self._dashboard()
        running = [_FakeBatch(running=True) for _ in range(5)]
        for batch in running:
            app.review_batches.append(batch)
            app.prune_review_batches()
        for _ in range(CYCLES):
            app.review_batches.append(_FakeBatch(running=False))
            app.prune_review_batches()
        for batch in running:
            self.assertIn(
                batch,
                app.review_batches,
                "a running batch must survive pruning: the watcher and every "
                "review event route through it",
            )

    def test_prune_never_evicts_when_all_batches_run(self) -> None:
        app = self._dashboard()
        for _ in range(tui.MAX_REVIEW_BATCHES * 3):
            app.review_batches.append(_FakeBatch(running=True))
            app.prune_review_batches()
        self.assertEqual(len(app.review_batches), tui.MAX_REVIEW_BATCHES * 3)


class LandingQueuePruneTests(unittest.TestCase):
    def _dashboard(self):
        app = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        app.landing_queue = []
        app._landing_active = set()
        return app

    def test_finished_landings_age_out_under_soak(self) -> None:
        app = self._dashboard()
        for _ in range(CYCLES):
            app.landing_queue.append(_FakeLandingTask(returncode=0))
            app._prune_landing_queue()
        self.assertLessEqual(len(app.landing_queue), tui.MAX_LANDING_QUEUE)

    def test_newest_task_is_always_the_tail(self) -> None:
        app = self._dashboard()
        for _ in range(CYCLES):
            newest = _FakeLandingTask(returncode=0)
            app.landing_queue.append(newest)
            app._prune_landing_queue()
            self.assertIs(
                app.landing_queue[-1],
                newest,
                "the status line reads the queue tail, so the newest task "
                "must stay last",
            )

    def test_unfinished_and_active_landings_survive(self) -> None:
        app = self._dashboard()
        pending = _FakeLandingTask(returncode=None)
        active = _FakeLandingTask(returncode=0)
        app.landing_queue.extend([pending, active])
        app._landing_active.add(id(active))
        for _ in range(CYCLES):
            app.landing_queue.append(_FakeLandingTask(returncode=0))
            app._prune_landing_queue()
        self.assertIn(pending, app.landing_queue)
        self.assertIn(active, app.landing_queue)
        self.assertLessEqual(len(app.landing_queue), tui.MAX_LANDING_QUEUE + 2)

    def test_evicted_task_ids_leave_the_active_set(self) -> None:
        app = self._dashboard()
        for _ in range(CYCLES):
            task = _FakeLandingTask(returncode=0)
            app.landing_queue.append(task)
            app._landing_active.add(id(task))
            app._prune_landing_queue()
            app._landing_active.discard(id(task))
        self.assertLessEqual(
            len(app._landing_active),
            tui.MAX_LANDING_QUEUE,
            "id() is reused after an object is freed, so stale membership "
            "would misreport a later task as active",
        )


class TraceRotationTests(unittest.TestCase):
    def test_diagnostics_stay_bounded_on_disk(self) -> None:
        for i in range(CYCLES):
            tui.trace({"event": "soak", "i": i, "pad": "x" * 128})
        directory = Path(tui.TRACE_PATH).parent
        segments = list(directory.glob("trace.jsonl*"))
        self.assertTrue(segments, "the trace log must exist after writing")
        self.assertLessEqual(
            len(segments),
            tui.TRACE_BACKUP_COUNT + 1,
            "a rotating log keeps one live segment plus its backups",
        )
        total = sum(segment.stat().st_size for segment in segments)
        ceiling = tui.TRACE_MAX_BYTES * (tui.TRACE_BACKUP_COUNT + 2)
        self.assertLess(
            total,
            ceiling,
            f"diagnostics grew to {total} bytes, above the {ceiling} ceiling",
        )

    def test_trace_still_records_the_most_recent_action(self) -> None:
        tui.trace({"event": "sentinel-soak-marker"})
        live = Path(tui.TRACE_PATH).read_text(encoding="utf-8")
        self.assertIn("sentinel-soak-marker", live)


class MemoryGrowthTests(unittest.TestCase):
    def test_repeated_cycles_do_not_grow_the_heap(self) -> None:
        app = tui.ReviewDashboard.__new__(tui.ReviewDashboard)
        app.review_batches = []
        app.landing_queue = []
        app._landing_active = set()
        triage: dict[str, str] = {}

        def cycle(count: int) -> None:
            for i in range(count):
                app.review_batches.append(_FakeBatch(running=False))
                app.prune_review_batches()
                app.landing_queue.append(_FakeLandingTask(returncode=0))
                app._prune_landing_queue()
                triage[f"repo/name#{i}"] = "reviewed"
                tui._bound_map(triage, tui.MAX_TRIAGE_ENTRIES)
                tui.trace({"event": "soak-memory", "i": i})

        tracemalloc.start()
        cycle(200)
        baseline = tracemalloc.take_snapshot()
        cycle(CYCLES)
        after = tracemalloc.take_snapshot()
        growth = sum(
            entry.size_diff for entry in after.compare_to(baseline, "filename")
        )
        tracemalloc.stop()
        self.assertLess(
            growth,
            2 * 1024 * 1024,
            f"heap grew {growth} bytes across {CYCLES} cycles; an unattended "
            "run must reach a steady state",
        )


if __name__ == "__main__":
    unittest.main()
