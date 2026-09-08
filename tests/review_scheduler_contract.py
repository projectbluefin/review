# tests/review_scheduler_contract.py
import concurrent.futures
import re
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.capacity import CapacityGovernor
from tui.review_cache import ReviewCache
from tui.review_engine import ReviewEngine
from tui.review_receipt import ReviewReceipt
from tui.review_result import ReviewResult
from tui.review_snapshot import BatchReviewItem, BatchSnapshot


BASE = "a" * 40


def item(number: int) -> BatchReviewItem:
    head = f"{number:040x}"
    return BatchReviewItem(
        f"projectbluefin/review#{number}",
        "projectbluefin/review",
        number,
        f"PR {number}",
        BASE,
        head,
        {"baseRefOid": BASE, "headRefOid": head},
        [],
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def run(
        self,
        review_item,
        run,
        workdir,
        check_scope_version,
        check_scope,
        headroom_route,
        headroom_telemetry,
    ):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(review_item.key)
        try:
            time.sleep(0.05)
            return ReviewReceipt.from_result(
                run,
                ReviewResult(
                    1,
                    "complete",
                    {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    [],
                    [],
                    {"backend": run.backend, "model": run.model},
                    {},
                    {},
                    [],
                ),
                ["compact"],
                check_scope_version,
            )
        finally:
            with self.lock:
                self.active -= 1


class MutableGovernor:
    def __init__(self, slots: int) -> None:
        self._slots = slots
        self.calls = 0
        self.lock = threading.Lock()

    def total_slots(self) -> int:
        with self.lock:
            self.calls += 1
            return self._slots

    def set_slots(self, slots: int) -> None:
        with self.lock:
            self._slots = slots


class ReviewSchedulerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1] / ".cache" / "review-scheduler-contract"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self._reset_scheduler()

    def tearDown(self) -> None:
        self._reset_scheduler()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _reset_scheduler() -> None:
        try:
            from tui.scheduler import reset_scheduler_for_tests
        except ModuleNotFoundError:
            return
        reset_scheduler_for_tests()

    def prepared_worktree(self, review_item, root):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", review_item.key)
        path = self.root / "worktrees" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_independent_single_item_batches_share_process_cap(self):
        count = 10
        governor = CapacityGovernor(
            cap=2,
            per_review_budget_mb=1,
            reserve_mb=1,
            mem_available_mb=lambda: 100,
            cpu_count=lambda: 4,
        )
        executor = RecordingExecutor()
        start = threading.Barrier(count + 1)
        errors = []

        def run_one(number: int):
            selected = item(number)
            engine = ReviewEngine(
                state_root=self.root / f"state-{number}",
                cache=ReviewCache(self.root / f"cache-{number}"),
                governor=governor,
                local_executor=executor,
            )
            start.wait(timeout=5)
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                return engine.run_sync(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(run_one, number) for number in range(1, count + 1)]
            start.wait(timeout=5)
            for future in futures:
                try:
                    future.result(timeout=5)
                except Exception as error:
                    errors.append(error)

        self.assertEqual(errors, [])
        self.assertEqual(len(executor.calls), count)
        self.assertLessEqual(executor.max_active, governor.total_slots())

    def test_thread_pool_executor_only_lives_in_scheduler_module(self):
        tui_dir = Path(__file__).parents[1] / "image" / "tui"
        offenders = []
        for path in tui_dir.glob("*.py"):
            if path.name == "scheduler.py":
                continue
            if "ThreadPoolExecutor" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])
        self.assertIn(
            "ThreadPoolExecutor",
            (tui_dir / "scheduler.py").read_text(encoding="utf-8"),
        )

    def test_effective_cap_is_queryable_and_reported(self):
        from tui.scheduler import scheduler

        governor = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1,
            reserve_mb=1,
            mem_available_mb=lambda: 100,
            cpu_count=lambda: 4,
        )
        engine = ReviewEngine(
            state_root=self.root / "state",
            cache=ReviewCache(self.root / "cache"),
            governor=governor,
            local_executor=RecordingExecutor(),
        )
        self.assertEqual(scheduler().effective_cap(governor), 2)
        self.assertEqual(engine.effective_review_cap(), 2)
        dashboard = (
            Path(__file__).parents[1]
            / "image"
            / "tui"
            / "bluefin_review_tui.py"
        ).read_text(encoding="utf-8")
        self.assertIn("review slots:", dashboard)
        self.assertIn("effective_review_cap", dashboard)

    def test_zero_capacity_blocks_without_spinning_until_notified(self):
        from tui.scheduler import scheduler

        governor = MutableGovernor(0)
        started = threading.Event()

        def work() -> str:
            started.set()
            return "done"

        shared = scheduler()
        future = shared.submit(governor, work)
        time.sleep(0.1)
        self.assertFalse(started.is_set())
        self.assertLess(governor.calls, 10)

        governor.set_slots(1)
        shared.notify_capacity_changed()
        self.assertEqual(future.result(timeout=1), "done")
        self.assertTrue(started.is_set())


if __name__ == "__main__":
    unittest.main()
