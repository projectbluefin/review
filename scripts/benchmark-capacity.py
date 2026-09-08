#!/usr/bin/env python3
"""Empirical review capacity benchmark across concurrent worker levels.

Measures wall-clock throughput and peak descendant-tree RSS across 1, 2, 4, 6,
and 8 workers under a synthetic review workload simulating the 5 concurrent
check subagents per review slot.

Drives work through ReviewScheduler and CapacityGovernor. Standard library only.
Opt-in: never runs in CI or pre-commit.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "image"))

from tui.capacity import CapacityGovernor, measure_tree_rss_kb, read_mem_available_mb
from tui.scheduler import ReviewScheduler


def _get_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    _, _, val = line.partition(":")
                    return val.strip()
    except OSError:
        pass
    return "unknown"


def make_subagent_code(buffer_mb: int, rounds: int) -> str:
    return f"""
import hashlib
b = bytearray({buffer_mb} * 1024 * 1024)
for i in range(0, len(b), 4096):
    b[i] = (i & 0xff)
view = memoryview(b)[:1024 * 1024]
for _ in range({rounds}):
    hashlib.sha256(view).digest()
"""


def run_benchmark(
    workers_list: list[int],
    units: int,
    runs: int,
    subagents_per_slot: int,
    buffer_mb: int,
    rounds: int,
) -> dict[int, dict[str, float | list[float]]]:
    subagent_code = make_subagent_code(buffer_mb, rounds)

    def slot_task(item_id: int) -> int:
        procs = [
            subprocess.Popen([sys.executable, "-c", subagent_code])
            for _ in range(subagents_per_slot)
        ]
        for proc in procs:
            proc.wait()
        return item_id

    results: dict[int, dict[str, float | list[float]]] = {}

    for cap in workers_list:
        run_times: list[float] = []
        run_peaks: list[float] = []

        for r in range(runs):
            governor = CapacityGovernor(
                cap=cap,
                per_review_budget_mb=1,
                reserve_mb=1,
                mem_available_mb=lambda: 100_000,
                cpu_count=lambda: max(32, cap * 2),
            )
            sched = ReviewScheduler(
                governor=governor,
                max_workers=max(16, (os.cpu_count() or 1) * 2),
            )
            peak_kb = [0]
            stop = threading.Event()

            def monitor() -> None:
                while not stop.is_set():
                    val = measure_tree_rss_kb(os.getpid(), peak=True)
                    if val > peak_kb[0]:
                        peak_kb[0] = val
                    time.sleep(0.015)

            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()

            start_time = time.perf_counter()
            futures = [sched.submit(governor, slot_task, i) for i in range(units)]
            for fut in futures:
                fut.result()
            elapsed = time.perf_counter() - start_time

            stop.set()
            monitor_thread.join()
            sched.shutdown()

            run_times.append(elapsed)
            run_peaks.append(float(peak_kb[0] // 1024))

        med_time = statistics.median(run_times)
        min_time = min(run_times)
        max_time = max(run_times)
        med_peak = statistics.median(run_peaks)
        max_peak = max(run_peaks)
        throughput = units / med_time

        results[cap] = {
            "times": run_times,
            "peaks": run_peaks,
            "med_time": med_time,
            "min_time": min_time,
            "max_time": max_time,
            "throughput": throughput,
            "med_peak": med_peak,
            "max_peak": max_peak,
        }

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark ReviewScheduler capacity across worker counts."
    )
    parser.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=[1, 2, 4, 6, 8],
        help="Worker concurrency levels to test (default: 1 2 4 6 8)",
    )
    parser.add_argument(
        "--units",
        type=int,
        default=24,
        help="Total work units per benchmark run (default: 24)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Repetitions per worker count (default: 3)",
    )
    parser.add_argument(
        "--subagents",
        type=int,
        default=5,
        help="Subagent child processes per review slot (default: 5)",
    )
    parser.add_argument(
        "--buffer-mb",
        type=int,
        default=25,
        help="Memory buffer in MB per subagent process (default: 25)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=500,
        help="Hash computation rounds per subagent (default: 500)",
    )
    args = parser.parse_args()

    cpu_model = _get_cpu_model()
    cpu_count = os.cpu_count() or 1
    try:
        mem_avail = read_mem_available_mb()
    except Exception:
        mem_avail = -1

    print("=== Bluefin Review Capacity Benchmark ===")
    print(f"CPU Model:      {cpu_model}")
    print(f"Logical Cores:  {cpu_count} (nproc)")
    print(f"MemAvailable:   {mem_avail} MB (~{mem_avail // 1024} GB)")
    print(f"Workload:       {args.units} units, {args.subagents} subagents/slot, {args.buffer_mb} MB/subagent, {args.rounds} hash rounds")
    print(f"Repetitions:    {args.runs} runs per level (reporting median)")
    print("==========================================\n")

    results = run_benchmark(
        workers_list=args.workers,
        units=args.units,
        runs=args.runs,
        subagents_per_slot=args.subagents,
        buffer_mb=args.buffer_mb,
        rounds=args.rounds,
    )

    base_throughput = float(results[args.workers[0]]["throughput"])
    prev_throughput = base_throughput

    header = (
        f"{'Cap':>4} | {'Med Time':>9} | {'Min Time':>9} | {'Max Time':>9} | "
        f"{'Throughput':>11} | {'Peak RSS':>9} | {'Speedup':>8} | {'Marginal':>9}"
    )
    print(header)
    print("-" * len(header))

    for cap in args.workers:
        data = results[cap]
        med_t = float(data["med_time"])
        min_t = float(data["min_time"])
        max_t = float(data["max_time"])
        tp = float(data["throughput"])
        med_rss = float(data["med_peak"])
        speedup = tp / base_throughput
        marginal = ((tp - prev_throughput) / prev_throughput) * 100.0 if prev_throughput else 0.0
        prev_throughput = tp

        print(
            f"{cap:4d} | {med_t:8.3f}s | {min_t:8.3f}s | {max_t:8.3f}s | "
            f"{tp:9.2f} u/s | {med_rss:7.0f} MB | {speedup:7.2f}x | {marginal:+8.1f}%"
        )

    print("-" * len(header))
    print("\nBenchmark completed successfully.")


if __name__ == "__main__":
    main()
