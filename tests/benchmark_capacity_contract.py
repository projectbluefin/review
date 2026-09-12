# tests/benchmark_capacity_contract.py
"""Contract for scripts/benchmark-capacity.py.

The capacity benchmark is the only evidence behind the worker-count choice in
``tui.capacity``: a maintainer runs it, reads the table, and picks a cap. It is
opt-in, so CI never executes it, and until now no test loaded it either — the
whole 235-line script (``_get_cpu_model``, ``make_subagent_code``,
``run_benchmark``, ``main``) was the single uncovered file under ``scripts/``.
That means its statistics, its speedup arithmetic and its table could drift
into producing confident, wrong numbers and nothing would say so.

These tests drive the real code, not a copy of it: ``run_benchmark`` and
``main`` are executed end to end at the smallest possible workload (one worker
level, one unit, one run, one subagent, a 1 MB buffer, one hash round), so the
scheduler, the governor, the subprocess fan-out and the RSS monitor all
actually run. Standard library only, matching tests/capacity_contract.py.
"""

import importlib.util
import io
import statistics
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_REPO_ROOT / "image"))


def _load_benchmark_module():
    """Import scripts/benchmark-capacity.py despite the hyphen in its name."""
    path = _REPO_ROOT / "scripts" / "benchmark-capacity.py"
    spec = importlib.util.spec_from_file_location("benchmark_capacity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark_module()

# The smallest workload that still exercises every moving part: governor,
# scheduler, subprocess fan-out, RSS monitor thread.
_TINY = {
    "units": 1,
    "runs": 1,
    "subagents_per_slot": 1,
    "buffer_mb": 1,
    "rounds": 1,
}


class CpuModelTests(unittest.TestCase):
    def test_reads_the_model_name_field_from_cpuinfo(self):
        cpuinfo = (
            "processor\t: 0\n"
            "vendor_id\t: GenuineIntel\n"
            "model name\t: Fictional CPU @ 3.00GHz\n"
            "cache size\t: 8192 KB\n"
        )
        with mock.patch("builtins.open", mock.mock_open(read_data=cpuinfo)):
            self.assertEqual(benchmark._get_cpu_model(), "Fictional CPU @ 3.00GHz")

    def test_returns_first_model_name_when_many_cores_are_listed(self):
        cpuinfo = "model name\t: Core A\nprocessor\t: 1\nmodel name\t: Core B\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=cpuinfo)):
            self.assertEqual(benchmark._get_cpu_model(), "Core A")

    def test_unknown_when_cpuinfo_is_unreadable(self):
        """A benchmark must still report its numbers off /proc-less hosts."""
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertEqual(benchmark._get_cpu_model(), "unknown")

    def test_unknown_when_cpuinfo_has_no_model_name(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="processor\t: 0\n")):
            self.assertEqual(benchmark._get_cpu_model(), "unknown")


class SubagentCodeTests(unittest.TestCase):
    def test_generated_code_compiles(self):
        compile(benchmark.make_subagent_code(25, 500), "<subagent>", "exec")

    def test_parameters_are_baked_into_the_generated_source(self):
        code = benchmark.make_subagent_code(7, 13)
        self.assertIn("bytearray(7 * 1024 * 1024)", code)
        self.assertIn("range(13)", code)

    def test_generated_code_actually_runs(self):
        """The workload is the benchmark. If it raises, every number is noise."""
        proc = subprocess.run(
            [sys.executable, "-c", benchmark.make_subagent_code(1, 1)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class RunBenchmarkTests(unittest.TestCase):
    def test_reports_one_entry_per_requested_worker_level(self):
        results = benchmark.run_benchmark(workers_list=[1, 2], **_TINY)
        self.assertEqual(sorted(results), [1, 2])

    def test_statistics_are_derived_from_the_recorded_samples(self):
        results = benchmark.run_benchmark(
            workers_list=[1],
            units=2,
            runs=3,
            subagents_per_slot=1,
            buffer_mb=1,
            rounds=1,
        )
        data = results[1]

        self.assertEqual(len(data["times"]), 3)
        self.assertEqual(len(data["peaks"]), 3)
        self.assertEqual(data["med_time"], statistics.median(data["times"]))
        self.assertEqual(data["min_time"], min(data["times"]))
        self.assertEqual(data["max_time"], max(data["times"]))
        self.assertEqual(data["med_peak"], statistics.median(data["peaks"]))
        self.assertEqual(data["max_peak"], max(data["peaks"]))

    def test_throughput_is_units_over_median_time(self):
        units = 2
        results = benchmark.run_benchmark(
            workers_list=[1],
            units=units,
            runs=2,
            subagents_per_slot=1,
            buffer_mb=1,
            rounds=1,
        )
        data = results[1]
        self.assertAlmostEqual(data["throughput"], units / data["med_time"], places=9)
        self.assertGreater(data["throughput"], 0.0)

    def test_every_run_is_timed_and_measured(self):
        results = benchmark.run_benchmark(workers_list=[1], **_TINY)
        data = results[1]
        self.assertGreater(data["times"][0], 0.0)
        # Peak tree RSS is sampled from a live monitor thread; a zero here
        # means the monitor never observed the process tree.
        self.assertGreater(data["peaks"][0], 0.0)

    def test_no_worker_levels_produces_no_rows_rather_than_failing(self):
        self.assertEqual(benchmark.run_benchmark(workers_list=[], **_TINY), {})


class MainTests(unittest.TestCase):
    _ARGV = [
        "benchmark-capacity.py",
        "--workers", "1",
        "--units", "1",
        "--runs", "1",
        "--subagents", "1",
        "--buffer-mb", "1",
        "--rounds", "1",
    ]

    def _run_main(self, argv=None):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv or self._ARGV):
            with redirect_stdout(buf):
                benchmark.main()
        return buf.getvalue()

    def test_prints_a_header_a_row_per_level_and_a_completion_line(self):
        argv = list(self._ARGV)
        argv[2] = "1"
        argv.insert(3, "2")  # --workers 1 2
        out = self._run_main(argv)

        self.assertIn("=== Bluefin Review Capacity Benchmark ===", out)
        self.assertIn("Throughput", out)
        self.assertIn("Benchmark completed successfully.", out)

        rows = [
            line for line in out.splitlines()
            if "u/s" in line and "MB" in line
        ]
        self.assertEqual(len(rows), 2, out)

    def test_first_level_is_the_speedup_baseline(self):
        out = self._run_main()
        row = next(line for line in out.splitlines() if "u/s" in line)
        # Speedup is measured against the first requested level, so that level
        # is 1.00x with no marginal gain by construction.
        self.assertIn("1.00x", row)
        self.assertIn("+0.0%", row)

    def test_reports_the_workload_it_actually_ran(self):
        out = self._run_main()
        self.assertIn("1 units, 1 subagents/slot, 1 MB/subagent, 1 hash rounds", out)
        self.assertIn("1 runs per level", out)

    def test_unavailable_meminfo_does_not_abort_the_benchmark(self):
        """A host without MemAvailable still gets its table."""
        with mock.patch.object(
            benchmark, "read_mem_available_mb", side_effect=OSError("no /proc/meminfo")
        ):
            out = self._run_main()
        self.assertIn("MemAvailable:   -1 MB", out)
        self.assertIn("Benchmark completed successfully.", out)

    def test_speedup_and_marginal_columns_are_computed_from_the_results(self):
        """Pin the two numbers a maintainer actually reads off the table.

        Speedup is against the *first* requested level; marginal gain is
        against the *previous* level. Measured throughput cannot pin that
        distinction, so the arithmetic is fed fixed results here.
        """
        fixed = {
            cap: {
                "times": [1.0], "peaks": [100.0],
                "med_time": 1.0, "min_time": 1.0, "max_time": 1.0,
                "throughput": tp, "med_peak": 100.0, "max_peak": 100.0,
            }
            for cap, tp in ((1, 10.0), (2, 15.0), (4, 12.0))
        }
        argv = [
            "benchmark-capacity.py", "--workers", "1", "2", "4",
            "--units", "1", "--runs", "1", "--subagents", "1",
            "--buffer-mb", "1", "--rounds", "1",
        ]
        with mock.patch.object(benchmark, "run_benchmark", return_value=fixed):
            out = self._run_main(argv)

        rows = [line for line in out.splitlines() if "u/s" in line]
        self.assertEqual(len(rows), 3, out)
        # cap 1: baseline. cap 2: 15/10. cap 4: 12/10, and 12/15 marginally.
        self.assertIn("1.00x", rows[0])
        self.assertIn("+0.0%", rows[0])
        self.assertIn("1.50x", rows[1])
        self.assertIn("+50.0%", rows[1])
        self.assertIn("1.20x", rows[2])
        self.assertIn("-20.0%", rows[2])

    def test_cpu_model_is_reported_from_the_host(self):
        with mock.patch.object(benchmark, "_get_cpu_model", return_value="Fictional CPU"):
            out = self._run_main()
        self.assertIn("CPU Model:      Fictional CPU", out)


if __name__ == "__main__":
    unittest.main()
