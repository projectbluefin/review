from __future__ import annotations

import os
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar

from tui.capacity import CapacityGovernor

T = TypeVar("T")


class CapacitySource(Protocol):
    def total_slots(self) -> int: ...


@dataclass(frozen=True)
class _Job:
    governor: CapacitySource
    future: Future[Any]
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class ReviewScheduler:
    def __init__(
        self,
        governor: CapacitySource | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._governor = governor or CapacityGovernor()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers or os.cpu_count() or 1)
        )
        self._condition = threading.Condition()
        self._queue: deque[_Job] = deque()
        self._running = 0
        self._closed = False
        self._dispatcher = threading.Thread(
            target=self._dispatch,
            name="bluefin-review-scheduler",
            daemon=True,
        )
        self._dispatcher.start()

    def submit(
        self,
        governor: CapacitySource | None,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> Future[T]:
        future: Future[T] = Future()
        future.add_done_callback(lambda _future: self.notify_capacity_changed())
        with self._condition:
            if self._closed:
                raise RuntimeError("review scheduler is closed")
            self._queue.append(
                _Job(
                    governor or self._governor,
                    future,
                    function,
                    args,
                    kwargs,
                )
            )
            self._condition.notify()
        return future

    def effective_cap(self, governor: CapacitySource | None = None) -> int:
        return max(0, int((governor or self._governor).total_slots()))

    def running_count(self) -> int:
        with self._condition:
            return self._running

    def queued_count(self) -> int:
        with self._condition:
            return sum(1 for job in self._queue if not job.future.cancelled())

    def notify_capacity_changed(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def shutdown(self, wait: bool = True) -> None:
        with self._condition:
            self._closed = True
            while self._queue:
                self._queue.popleft().future.cancel()
            self._condition.notify_all()
        if wait:
            self._dispatcher.join(timeout=5)
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                job = self._next_job()
                if job is None:
                    return
                self._running += 1
            try:
                self._pool.submit(self._run, job)
            except BaseException as error:
                with self._condition:
                    self._running -= 1
                    self._condition.notify_all()
                job.future.set_exception(error)

    def _next_job(self) -> _Job | None:
        while True:
            while self._queue and self._queue[0].future.cancelled():
                self._queue.popleft()
            if self._closed and not self._queue and self._running == 0:
                return None
            if not self._queue:
                self._condition.wait()
                continue
            job = self._queue[0]
            if self._running >= self.effective_cap(job.governor):
                self._condition.wait()
                continue
            self._queue.popleft()
            if job.future.set_running_or_notify_cancel():
                return job

    def _run(self, job: _Job) -> None:
        try:
            result = job.function(*job.args, **job.kwargs)
        except BaseException as error:
            job.future.set_exception(error)
        else:
            job.future.set_result(result)
        finally:
            with self._condition:
                self._running -= 1
                self._condition.notify_all()


_scheduler: ReviewScheduler | None = None
_scheduler_lock = threading.Lock()


def scheduler() -> ReviewScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = ReviewScheduler()
        return _scheduler


def reset_scheduler_for_tests() -> None:
    global _scheduler
    with _scheduler_lock:
        current = _scheduler
        _scheduler = None
    if current is not None:
        current.shutdown()


__all__ = [
    "CapacitySource",
    "ReviewScheduler",
    "reset_scheduler_for_tests",
    "scheduler",
]
