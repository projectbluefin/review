# tests/review_deadline_contract.py
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.capacity import (
    CapacityGovernor,
    measure_tree_rss_kb,
    read_proc_rss_kb,
)
from tui.headroom import HeadroomRoute
from tui.review_cache import ReviewCache
from tui.review_engine import (
    BLUEFIN_REVIEW_DEADLINE_SECONDS,
    DEFAULT_REVIEW_DEADLINE_SECONDS,
    LocalExecutor,
    ReviewDeadlineExceeded,
    ReviewEngine,
    ReviewEvent,
)
from tui.review_receipt import ReviewReceipt
from tui.review_result import ReviewResult
from tui.review_run import ReviewRun
from tui.review_snapshot import BatchReviewItem, BatchSnapshot

BASE = "a" * 40
HEADS = ("b" * 40, "c" * 40)


def make_item(number, head):
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


class FakeHeadroomSession:
    def refresh(self, backend):
        return HeadroomRoute("DIRECT", backend, None, "ready")

    def route_for_call(self, backend):
        return HeadroomRoute("DIRECT", backend, None, "ready")

    def telemetry(self, backend):
        return {
            "state": "DIRECT",
            "route": None,
            "status_line": f"[DIRECT] {backend}",
            "output_reduction_percent": 0.0,
            "output_reduction_method": "none",
            "output_tokens_saved": 0,
        }


class ReviewDeadlineContractTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path.cwd()

    def test_deadline_default_and_env_configuration(self):
        original = os.environ.get(BLUEFIN_REVIEW_DEADLINE_SECONDS)
        try:
            os.environ.pop(BLUEFIN_REVIEW_DEADLINE_SECONDS, None)
            executor = LocalExecutor()
            self.assertEqual(executor.timeout, DEFAULT_REVIEW_DEADLINE_SECONDS)
            self.assertGreaterEqual(executor.timeout, 600.0)

            os.environ[BLUEFIN_REVIEW_DEADLINE_SECONDS] = "42.5"
            executor_env = LocalExecutor()
            self.assertEqual(executor_env.timeout, 42.5)

            explicit = LocalExecutor(timeout=15.0)
            self.assertEqual(explicit.timeout, 15.0)

            with self.assertRaises(ValueError):
                LocalExecutor(timeout=-1)

            with self.assertRaises(ValueError):
                LocalExecutor(timeout=0)

            os.environ[BLUEFIN_REVIEW_DEADLINE_SECONDS] = "not_a_number"
            with self.assertRaises(ValueError):
                LocalExecutor()
        finally:
            if original is not None:
                os.environ[BLUEFIN_REVIEW_DEADLINE_SECONDS] = original
            else:
                os.environ.pop(BLUEFIN_REVIEW_DEADLINE_SECONDS, None)

    def test_executor_subprocess_exceeding_deadline_kills_whole_process_group(self):
        """Spawn a child that forks a grandchild; assert grandchild dies on deadline expiry."""
        with tempfile.TemporaryDirectory(dir=str(self.cwd)) as root:
            root_path = Path(root)
            workdir = root_path / "workdir"
            workdir.mkdir()
            grandchild_pid_file = root_path / "grandchild.pid"

            # Receipt command: forks a grandchild that sleeps, then parent sleeps
            command_path = root_path / "hanging_command"
            command_code = (
                "#!/usr/bin/env python3\n"
                "import os, sys, time, subprocess\n"
                f"grandchild_pid_file = {str(grandchild_pid_file)!r}\n"
                "# Spawn grandchild that would live for 60 seconds if not killed\n"
                "proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "with open(grandchild_pid_file, 'w') as f:\n"
                "    f.write(str(proc.pid))\n"
                "    f.flush()\n"
                "# Parent sleeps longer than the deadline\n"
                "time.sleep(30)\n"
            )
            command_path.write_text(command_code)
            command_path.chmod(0o755)

            executor = LocalExecutor(command=str(command_path), timeout=0.5)
            selected = make_item(1, HEADS[0])
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )

            with self.assertRaises(ReviewDeadlineExceeded) as ctx:
                executor.run(
                    selected,
                    run,
                    workdir,
                    "scope-v1",
                    "",
                    HeadroomRoute("DIRECT", "goose", None, "ready"),
                    {},
                )

            self.assertIn("deadline exceeded", str(ctx.exception))
            self.assertIn("0.5", str(ctx.exception))

            # Grandchild PID must have been recorded
            self.assertTrue(grandchild_pid_file.exists())
            grandchild_pid = int(grandchild_pid_file.read_text().strip())

            # Verify the grandchild process is dead
            deadline = time.monotonic() + 2.0
            grandchild_dead = False
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    grandchild_dead = True
                    break

            self.assertTrue(
                grandchild_dead,
                f"grandchild process {grandchild_pid} was not killed by process group kill",
            )

    def test_deadline_expiry_produces_distinct_failure_and_emits_failed_event(self):
        """Deadline expiry produces distinct failure message and emits ReviewEvent 'failed'."""
        with tempfile.TemporaryDirectory(dir=str(self.cwd)) as root:
            root_path = Path(root)
            workdir = root_path / "workdir"
            workdir.mkdir()

            command_path = root_path / "hanging_review"
            command_code = (
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(30)\n"
            )
            command_path.write_text(command_code)
            command_path.chmod(0o755)

            executor = LocalExecutor(command=str(command_path), timeout=0.4)
            cache = ReviewCache(root_path / "reviews")
            engine = ReviewEngine(
                state_root=str(root_path),
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 1000,
                    cpu_count=lambda: 4,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )

            events: list[ReviewEvent] = []
            selected = make_item(1, HEADS[0])
            snapshot = BatchSnapshot((selected,), {})

            def mock_prepare_worktree(item, root):
                return workdir

            from unittest.mock import patch
            with patch("tui.review_engine._prepare_worktree", side_effect=mock_prepare_worktree):
                result = engine.run_sync(
                    snapshot,
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v1",
                    on_event=events.append,
                )

            self.assertIn(selected.key, result.failures)
            failure_message = result.failures[selected.key]
            self.assertIn("deadline exceeded", failure_message)
            self.assertIn("0.4", failure_message)

            failed_events = [e for e in events if e.state == "failed" and e.key == selected.key]
            self.assertTrue(failed_events, "failed ReviewEvent was not emitted")
            self.assertIn("deadline exceeded", failed_events[0].note)

    def test_review_finishing_inside_deadline_is_unaffected(self):
        """A review completing within deadline succeeds normally."""
        with tempfile.TemporaryDirectory(dir=str(self.cwd)) as root:
            root_path = Path(root)
            workdir = root_path / "workdir"
            workdir.mkdir()

            selected = make_item(1, HEADS[0])
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            receipt = ReviewReceipt.from_result(
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
                ["within deadline"],
                "scope-v1",
            )

            command_path = root_path / "fast_command"
            command_code = (
                "#!/usr/bin/env python3\n"
                f"print({receipt.to_json()!r})\n"
            )
            command_path.write_text(command_code)
            command_path.chmod(0o755)

            executor = LocalExecutor(command=str(command_path), timeout=5.0)
            actual_receipt = executor.run(
                selected,
                run,
                workdir,
                "scope-v1",
                "",
                HeadroomRoute("DIRECT", "goose", None, "ready"),
                FakeHeadroomSession().telemetry("goose"),
            )
            self.assertEqual(actual_receipt.identity, receipt.identity)
            self.assertEqual(actual_receipt.analysis.state, "complete")

    def test_descendant_tree_measurement_counts_child_memory(self):
        """measure_tree_rss_kb counts memory of descendant processes, not just parent."""
        script = (
            "import os, sys, time\n"
            "# Parent allocates 15 MB\n"
            "p_buf = b'x' * (15 * 1024 * 1024)\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    # Child allocates 25 MB\n"
            "    c_buf = b'y' * (25 * 1024 * 1024)\n"
            "    time.sleep(2)\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    time.sleep(2)\n"
            "    os.waitpid(pid, 0)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            # Give both processes time to allocate memory
            time.sleep(0.4)
            parent_rss = read_proc_rss_kb(proc.pid)
            tree_rss = measure_tree_rss_kb(proc.pid)

            self.assertGreater(parent_rss, 10 * 1024)
            # The tree RSS must be substantially greater than parent alone (child added ~25MB)
            self.assertGreater(
                tree_rss,
                parent_rss + (15 * 1024),
                f"tree_rss ({tree_rss} kB) does not reflect child memory (parent_rss={parent_rss} kB)",
            )
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
