# tests/capacity_contract.py
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.capacity import (
    BLUEFIN_REVIEW_CONCURRENT_REVIEWS,
    BLUEFIN_REVIEW_MEM_BUDGET_MB,
    BLUEFIN_REVIEW_MEM_RESERVE_MB,
    CapacityError,
    CapacityGovernor,
    get_descendant_pids,
    measure_tree_rss_kb,
    measure_tree_rss_mb,
    read_mem_available_mb,
    read_proc_rss_kb,
)


class CapacityContractTests(unittest.TestCase):
    def test_meminfo_reader_parses_memavailable_kib(self):
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "capacity-meminfo-test"
            path.write_text("MemTotal: 8000000 kB\nMemAvailable: 4096000 kB\n")
            self.assertEqual(read_mem_available_mb(str(path)), 4000)

    def test_default_meminfo_reader_reads_host_meminfo_if_present(self):
        if Path("/proc/meminfo").exists():
            mb = read_mem_available_mb()
            self.assertGreater(mb, 0)


    def test_zero_slot_when_reserve_exceeds_available_memory(self):
        governor = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1536,
            reserve_mb=2048,
            mem_available_mb=lambda: 1024,
            cpu_count=lambda: 16,
        )
        self.assertEqual(governor.total_slots(), 0)
        self.assertFalse(governor.can_start(0))

    def test_formula_uses_memory_cpu_and_configured_cap(self):
        governor = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1500,
            reserve_mb=2000,
            mem_available_mb=lambda: 11000,
            cpu_count=lambda: 6,
        )
        self.assertEqual(governor.total_slots(), 3)
        self.assertEqual(governor.runnable_slots(2), 1)
        self.assertEqual(governor.runnable_slots(3), 0)

    def test_cap_strictly_limits_when_memory_and_cpu_are_higher(self):
        # memory gives (11000 - 2000) // 1500 = 6 slots
        # cpu gives 8 // 2 = 4 slots
        # cap = 2 strictly limits capacity to 2 slots
        governor = CapacityGovernor(
            cap=2,
            per_review_budget_mb=1500,
            reserve_mb=2000,
            mem_available_mb=lambda: 11000,
            cpu_count=lambda: 8,
        )
        self.assertEqual(governor.total_slots(), 2)
        self.assertEqual(governor.runnable_slots(1), 1)
        self.assertEqual(governor.runnable_slots(2), 0)
        self.assertTrue(governor.can_start(1))
        self.assertFalse(governor.can_start(2))

    def test_meminfo_reader_error_conditions(self):
        with self.assertRaises(CapacityError):
            read_mem_available_mb("/nonexistent/meminfo")

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", dir=".") as temp:
            temp.write("MemTotal: 8000000 kB\nMemFree: 1000000 kB\n")
            temp.flush()
            with self.assertRaises(CapacityError):
                read_mem_available_mb(temp.name)

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", dir=".") as temp:
            temp.write("MemAvailable: not_a_number kB\n")
            temp.flush()
            with self.assertRaises(CapacityError):
                read_mem_available_mb(temp.name)

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", dir=".") as temp:
            temp.write("MemAvailable: 4000000 mB\n")
            temp.flush()
            with self.assertRaises(CapacityError):
                read_mem_available_mb(temp.name)

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", dir=".") as temp:
            temp.write("MemAvailable: -1024 kB\n")
            temp.flush()
            with self.assertRaises(CapacityError):
                read_mem_available_mb(temp.name)

    def test_governor_environment_defaults_and_validation(self):
        env = {
            BLUEFIN_REVIEW_CONCURRENT_REVIEWS: "6",
            BLUEFIN_REVIEW_MEM_BUDGET_MB: "2048",
            BLUEFIN_REVIEW_MEM_RESERVE_MB: "1024",
        }
        original = os.environ.copy()
        try:
            os.environ.update(env)
            governor = CapacityGovernor()
            self.assertEqual(governor.cap, 6)
            self.assertEqual(governor.per_review_budget_mb, 2048)
            self.assertEqual(governor.reserve_mb, 1024)

            os.environ[BLUEFIN_REVIEW_CONCURRENT_REVIEWS] = "invalid"
            with self.assertRaises(CapacityError):
                CapacityGovernor()

            os.environ[BLUEFIN_REVIEW_CONCURRENT_REVIEWS] = "0"
            with self.assertRaises(CapacityError):
                CapacityGovernor()
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_governor_invalid_arguments(self):
        with self.assertRaises(CapacityError):
            CapacityGovernor(cap=-1)

        with self.assertRaises(CapacityError):
            CapacityGovernor(per_review_budget_mb=0)

        with self.assertRaises(CapacityError):
            CapacityGovernor(reserve_mb=0)

        with self.assertRaises(CapacityError):
            CapacityGovernor(cap=True)

        governor = CapacityGovernor(cap=4, per_review_budget_mb=1000, reserve_mb=1000)
        with self.assertRaises(CapacityError):
            governor.runnable_slots(-1)

        with self.assertRaises(CapacityError):
            governor.runnable_slots(True)

        with self.assertRaises(CapacityError):
            governor.can_start(-1)

        with self.assertRaises(TypeError):
            governor.runnable_slots()  # type: ignore[call-arg]

        with self.assertRaises(TypeError):
            governor.can_start()  # type: ignore[call-arg]

    def test_governor_running_count_and_zero_flooring(self):
        governor = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1000,
            reserve_mb=1000,
            mem_available_mb=lambda: 5000,
            cpu_count=lambda: 8,
        )
        # available = 4000, memory_slots = 4, cpu_slots = 4, cap = 4 => total_slots = 4
        self.assertEqual(governor.total_slots(), 4)
        self.assertEqual(governor.runnable_slots(2), 2)
        self.assertTrue(governor.can_start(2))
        self.assertEqual(governor.runnable_slots(4), 0)
        self.assertFalse(governor.can_start(4))
        # When running exceeds total slots, floored at 0, running reviews never killed
        self.assertEqual(governor.runnable_slots(10), 0)
        self.assertFalse(governor.can_start(10))

    def test_cpu_count_none_or_limited(self):
        # cpu_count None -> cpu_slots = 0 -> total_slots = 0
        governor_none = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1000,
            reserve_mb=1000,
            mem_available_mb=lambda: 10000,
            cpu_count=lambda: None,
        )
        self.assertEqual(governor_none.total_slots(), 0)

        # 1 core -> 1 // 2 = 0 slots
        governor_single = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1000,
            reserve_mb=1000,
            mem_available_mb=lambda: 10000,
            cpu_count=lambda: 1,
        )
        self.assertEqual(governor_single.total_slots(), 0)

        # 2 cores -> 2 // 2 = 1 slot
        governor_dual = CapacityGovernor(
            cap=4,
            per_review_budget_mb=1000,
            reserve_mb=1000,
            mem_available_mb=lambda: 10000,
            cpu_count=lambda: 2,
        )
        self.assertEqual(governor_dual.total_slots(), 1)

    def test_proc_rss_and_tree_measurement_with_mock_proc(self):
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            proc_root = Path(temp_dir)
            # Create process tree:
            # 100 (parent) -> 101 (child1), 102 (child2)
            # 101 -> 103 (grandchild)
            # 200 (unrelated process)
            procs = [
                (100, 1, 10000, 8000),   # pid, ppid, hwm, rss
                (101, 100, 20000, 15000),
                (102, 100, 5000, 4000),
                (103, 101, 8000, 7000),
                (200, 1, 50000, 40000),
            ]
            for pid, ppid, hwm, rss in procs:
                pdir = proc_root / str(pid)
                pdir.mkdir()
                (pdir / "stat").write_text(f"{pid} (testproc) S {ppid} {pid} 0 0\n")
                (pdir / "status").write_text(f"PPid:\t{ppid}\nVmHWM:\t{hwm} kB\nVmRSS:\t{rss} kB\n")

            descendants = get_descendant_pids(100, proc_root=str(proc_root))
            self.assertEqual(descendants, {100, 101, 102, 103})

            # Check single process RSS
            self.assertEqual(read_proc_rss_kb(100, proc_root=str(proc_root), peak=False), 8000)
            self.assertEqual(read_proc_rss_kb(100, proc_root=str(proc_root), peak=True), 10000)

            # Check tree RSS (sum of 100, 101, 102, 103)
            # rss: 8000 + 15000 + 4000 + 7000 = 34000 kB
            # hwm: 10000 + 20000 + 5000 + 8000 = 43000 kB
            self.assertEqual(measure_tree_rss_kb(100, proc_root=str(proc_root), peak=False), 34000)
            self.assertEqual(measure_tree_rss_kb(100, proc_root=str(proc_root), peak=True), 43000)
            self.assertEqual(measure_tree_rss_mb(100, proc_root=str(proc_root), peak=True), 43000 // 1024)

    def test_read_process_tree_rss_current_process(self):
        rss = measure_tree_rss_kb(os.getpid())
        self.assertGreater(rss, 0)


if __name__ == "__main__":
    unittest.main()
