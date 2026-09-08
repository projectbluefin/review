# image/tui/capacity.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

BLUEFIN_REVIEW_CONCURRENT_REVIEWS = "BLUEFIN_REVIEW_CONCURRENT_REVIEWS"
BLUEFIN_REVIEW_MEM_BUDGET_MB = "BLUEFIN_REVIEW_MEM_BUDGET_MB"
BLUEFIN_REVIEW_MEM_RESERVE_MB = "BLUEFIN_REVIEW_MEM_RESERVE_MB"
DEFAULT_REVIEW_CAP = 4
DEFAULT_REVIEW_BUDGET_MB = 1536
DEFAULT_REVIEW_RESERVE_MB = 2048


class CapacityError(RuntimeError):
    pass


def _positive_int(value: str | int, name: str) -> int:
    if isinstance(value, bool):
        raise CapacityError(f"{name} must be an integer")
    try:
        result = int(value)
    except (ValueError, TypeError) as error:
        raise CapacityError(f"{name} must be an integer") from error
    if result < 1:
        raise CapacityError(f"{name} must be positive")
    return result


def read_mem_available_mb(path: str = "/proc/meminfo") -> int:
    try:
        lines = open(path, encoding="utf-8")
    except OSError as error:
        raise CapacityError(f"cannot read {path}: {error}") from error
    with lines:
        for line in lines:
            name, separator, value = line.partition(":")
            if name != "MemAvailable" or not separator:
                continue
            fields = value.split()
            if len(fields) != 2 or fields[1] != "kB":
                raise CapacityError("MemAvailable is not expressed in kB")
            try:
                kib = int(fields[0])
            except ValueError as error:
                raise CapacityError("MemAvailable is not an integer") from error
            if kib < 0:
                raise CapacityError("MemAvailable is negative")
            return kib // 1024
    raise CapacityError("MemAvailable is missing")


def _read_ppid(pid_dir: str) -> int | None:
    stat_file = os.path.join(pid_dir, "stat")
    try:
        with open(stat_file, "r", encoding="utf-8") as f:
            content = f.read()
        idx = content.rfind(")")
        if idx != -1:
            return int(content[idx + 2:].split()[1])
    except (OSError, IndexError, ValueError):
        pass
    status_file = os.path.join(pid_dir, "status")
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return None


def get_descendant_pids(
    root_pid: int,
    proc_root: str = "/proc",
) -> set[int]:
    """Find root_pid and all its descendant process IDs by scanning proc_root."""
    parent_map: dict[int, int] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return {root_pid}

    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
        except ValueError:
            continue
        pid_dir = os.path.join(proc_root, entry)
        ppid = _read_ppid(pid_dir)
        if ppid is not None:
            parent_map[pid] = ppid

    children: dict[int, list[int]] = {}
    for pid, ppid in parent_map.items():
        children.setdefault(ppid, []).append(pid)

    descendants: set[int] = set()
    queue = [root_pid]
    while queue:
        curr = queue.pop()
        descendants.add(curr)
        for child in children.get(curr, ()):
            if child not in descendants:
                queue.append(child)

    return descendants


def read_proc_rss_kb(
    pid: int,
    proc_root: str = "/proc",
    peak: bool = False,
) -> int:
    status_file = os.path.join(proc_root, str(pid), "status")
    hwm = 0
    rss = 0
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            hwm = int(parts[1])
                        except ValueError:
                            pass
                elif line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            rss = int(parts[1])
                        except ValueError:
                            pass
    except (OSError, ValueError):
        return 0
    if peak:
        return hwm if hwm > 0 else rss
    return rss if rss > 0 else hwm


def measure_tree_rss_kb(
    root_pid: int,
    proc_root: str = "/proc",
    peak: bool = False,
) -> int:
    """Measure resident memory in kB across root_pid and all its descendant processes."""
    pids = get_descendant_pids(root_pid, proc_root=proc_root)
    total_kb = 0
    for pid in pids:
        total_kb += read_proc_rss_kb(pid, proc_root=proc_root, peak=peak)
    return total_kb


def measure_tree_rss_mb(
    root_pid: int,
    proc_root: str = "/proc",
    peak: bool = False,
) -> int:
    """Measure resident memory in MB across root_pid and all its descendant processes."""
    return measure_tree_rss_kb(root_pid, proc_root=proc_root, peak=peak) // 1024


read_process_tree_rss_kb = measure_tree_rss_kb
read_process_tree_rss_mb = measure_tree_rss_mb


@dataclass(frozen=True)
class CapacityGovernor:
    cap: int | None = None
    per_review_budget_mb: int | None = None
    reserve_mb: int | None = None
    mem_available_mb: Callable[[], int] = read_mem_available_mb
    cpu_count: Callable[[], int | None] = os.cpu_count

    def __post_init__(self) -> None:
        raw_cap = self.cap
        raw_budget = self.per_review_budget_mb
        raw_reserve = self.reserve_mb

        cap = (
            _positive_int(raw_cap, "cap")
            if raw_cap is not None
            else _positive_int(
                os.environ.get(BLUEFIN_REVIEW_CONCURRENT_REVIEWS, str(DEFAULT_REVIEW_CAP)),
                BLUEFIN_REVIEW_CONCURRENT_REVIEWS,
            )
        )
        budget = (
            _positive_int(raw_budget, "per_review_budget_mb")
            if raw_budget is not None
            else _positive_int(
                os.environ.get(BLUEFIN_REVIEW_MEM_BUDGET_MB, str(DEFAULT_REVIEW_BUDGET_MB)),
                BLUEFIN_REVIEW_MEM_BUDGET_MB,
            )
        )
        reserve = (
            _positive_int(raw_reserve, "reserve_mb")
            if raw_reserve is not None
            else _positive_int(
                os.environ.get(BLUEFIN_REVIEW_MEM_RESERVE_MB, str(DEFAULT_REVIEW_RESERVE_MB)),
                BLUEFIN_REVIEW_MEM_RESERVE_MB,
            )
        )

        mem_reader = self.mem_available_mb if self.mem_available_mb is not None else read_mem_available_mb
        cpu_reader = self.cpu_count if self.cpu_count is not None else os.cpu_count

        object.__setattr__(self, "cap", cap)
        object.__setattr__(self, "per_review_budget_mb", budget)
        object.__setattr__(self, "reserve_mb", reserve)
        object.__setattr__(self, "mem_available_mb", mem_reader)
        object.__setattr__(self, "cpu_count", cpu_reader)

    def total_slots(self) -> int:
        available = max(0, int(self.mem_available_mb()) - self.reserve_mb)
        memory_slots = available // self.per_review_budget_mb
        cores = self.cpu_count()
        cpu_slots = max(0, int(cores or 0) // 2)
        return max(0, min(memory_slots, cpu_slots, self.cap))

    def runnable_slots(self, running: int) -> int:
        if isinstance(running, bool) or not isinstance(running, int) or running < 0:
            raise CapacityError("running must be a non-negative integer")
        return max(0, self.total_slots() - running)

    def can_start(self, running: int) -> bool:
        return self.runnable_slots(running) > 0
