"""Durable, capacity-aware scheduling for exact-head pull request reviews."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO, cast

from harness.registry import Harness, HarnessRegistry
from tui.capacity import CapacityGovernor
from tui.gh_client import _kill_process_group
from tui.headroom import HeadroomRoute, HeadroomSession
from tui.review_cache import ReviewCache
from tui.review_receipt import ReceiptIdentity, ReviewReceipt
from tui.review_run import ReviewRun, ReviewRunController, ReviewRunState
from tui.scheduler import scheduler
from tui.review_snapshot import BatchReviewItem, BatchSnapshot

BLUEFIN_REVIEW_DEADLINE_SECONDS = "BLUEFIN_REVIEW_DEADLINE_SECONDS"
DEFAULT_REVIEW_DEADLINE_SECONDS = 1800.0


class ReviewDeadlineExceeded(RuntimeError):
    """The review executor exceeded its configured deadline."""
    pass


def _positive_deadline(value: str | int | float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a number") from error
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


TERMINAL_REVIEW_STATES = frozenset(
    {"complete", "findings", "failed", "cancelled"}
)
_INSTANCE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def REVIEW_ENGINE_STATE_DIR() -> str:
    root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(root, "bluefin-review", "review-batches")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass(frozen=True)
class ReviewEvent:
    key: str
    state: str
    note: str
    timestamp: int
    receipt: str = ""
    batch_id: str = ""
    head_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "key": self.key,
            "state": self.state,
            "note": self.note,
            "ts": self.timestamp,
        }
        if self.receipt:
            value["receipt"] = self.receipt
        if self.batch_id:
            value["batch_id"] = self.batch_id
        if self.head_sha:
            value["head_sha"] = self.head_sha
        return value


def _read_events(handle: TextIO) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    handle.seek(0)
    for line in handle:
        value = line.strip()
        if not value:
            continue
        try:
            event = json.loads(value)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


@contextlib.contextmanager
def _locked_events(path: str):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle, _read_events(handle)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _append_locked(handle: TextIO, event: dict[str, Any]) -> None:
    line = json.dumps(event, separators=(",", ":"))
    handle.seek(0)
    content = handle.read()
    if content and not content.endswith("\n"):
        boundary = content.rfind("\n") + 1
        try:
            tail = json.loads(content[boundary:])
        except (ValueError, RecursionError):
            tail = None
        if isinstance(tail, dict):
            handle.seek(0, os.SEEK_END)
            handle.write("\n")
        else:
            handle.seek(boundary)
            handle.truncate()
    handle.seek(0, os.SEEK_END)
    handle.write(line + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def append_review_event(path: str, event: ReviewEvent) -> None:
    with _locked_events(path) as (handle, events):
        previous = next(
            (
                recorded
                for recorded in reversed(events)
                if recorded.get("key") == event.key
            ),
            None,
        )
        if previous is not None and previous.get("state") in TERMINAL_REVIEW_STATES:
            semantic_fields = ("key", "state", "note", "receipt")
            if all(
                previous.get(field, "") == event.to_dict().get(field, "")
                for field in semantic_fields
            ):
                return
            if event.state not in TERMINAL_REVIEW_STATES:
                raise RuntimeError(f"{event.key} is already terminal")
        _append_locked(handle, event.to_dict())


def parse_review_status(path: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for event in _read_events(handle):
                key = event.get("key")
                if key:
                    latest[str(key)] = event
    except OSError:
        return {}
    return latest


class ReviewExecutor(Protocol):
    def run(
        self,
        review_item: BatchReviewItem,
        run: ReviewRun,
        workdir: Path,
        check_scope_version: str,
        check_scope: str,
        headroom_route: HeadroomRoute,
        headroom_telemetry: Mapping[str, Any],
    ) -> ReviewReceipt: ...


class BrokerUnavailable(RuntimeError):
    pass


def _with_headroom_provenance(
    receipt: ReviewReceipt,
    route: HeadroomRoute,
    telemetry: Mapping[str, object],
) -> ReviewReceipt:
    return receipt.with_provenance(
        {
            "headroom_state": route.state,
            "headroom_route": route.base_url or "",
            "headroom_status_line": telemetry["status_line"],
            "headroom_output_reduction_percent": telemetry[
                "output_reduction_percent"
            ],
            "headroom_output_reduction_method": telemetry[
                "output_reduction_method"
            ],
            "headroom_output_tokens_saved": telemetry[
                "output_tokens_saved"
            ],
        }
    )


class BrokerExecutor:
    def __init__(
        self,
        client_module: Any = None,
        poll_seconds: float = 0.2,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self.client = client_module
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            from tui import review_exec_client
            return review_exec_client
        except ImportError:
            import review_exec_client
            return review_exec_client

    def run(
        self,
        review_item: BatchReviewItem,
        run: ReviewRun,
        workdir: Path,
        check_scope_version: str,
        check_scope: str,
        headroom_route: HeadroomRoute,
        headroom_telemetry: Mapping[str, Any],
    ) -> ReviewReceipt:
        client = self._client()
        try:
            submitted = client.submit(
                review_item.repository,
                review_item.number,
                review_item.base_sha,
                review_item.head_sha,
                run.backend,
                run.model,
                run.effort,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            raise BrokerUnavailable(str(error)) from error
        if not submitted.get("ok") or submitted.get("result") != "submitted":
            raise BrokerUnavailable(
                str(submitted.get("detail") or submitted.get("error") or "submit failed")
            )
        job = submitted.get("job")
        if not isinstance(job, str) or not job:
            raise BrokerUnavailable("broker returned no Job name")
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                status = client.status()
            except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                raise BrokerUnavailable(str(error)) from error
            if not status.get("ok"):
                raise BrokerUnavailable(
                    str(status.get("detail") or status.get("error") or "status failed")
                )
            record = next(
                (entry for entry in status.get("jobs", []) if entry.get("job") == job),
                None,
            )
            if record is None:
                raise BrokerUnavailable(f"broker lost Job {job}")
            if int(record.get("succeeded", 0) or 0) or int(record.get("failed", 0) or 0):
                logs = client.logs(job)
                if not logs.get("ok"):
                    raise BrokerUnavailable(
                        str(logs.get("detail") or "log collection failed")
                    )
                return ReviewReceipt.from_json(
                    str(logs.get("logs") or "")
                ).with_provenance({
                    "headroom_state": headroom_route.state,
                    "headroom_route": headroom_route.base_url or "",
                    "headroom_status_line": headroom_telemetry["status_line"],
                    "headroom_output_reduction_percent": headroom_telemetry["output_reduction_percent"],
                    "headroom_output_reduction_method": headroom_telemetry["output_reduction_method"],
                    "headroom_output_tokens_saved": headroom_telemetry["output_tokens_saved"],
                })
            time.sleep(self.poll_seconds)
        raise BrokerUnavailable(f"Job {job} exceeded the broker collection timeout")

    def cancel(self, run: ReviewRun) -> None:
        return None


class LocalExecutor:
    def __init__(
        self,
        command: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.command = command or os.environ.get(
            "BLUEFIN_REVIEW_COMMAND", "bluefin-review"
        )
        if timeout is not None:
            self.timeout = _positive_deadline(timeout, "timeout")
        else:
            raw = os.environ.get(BLUEFIN_REVIEW_DEADLINE_SECONDS)
            self.timeout = (
                _positive_deadline(raw, BLUEFIN_REVIEW_DEADLINE_SECONDS)
                if raw is not None
                else DEFAULT_REVIEW_DEADLINE_SECONDS
            )
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def run(
        self,
        review_item: BatchReviewItem,
        run: ReviewRun,
        workdir: Path,
        check_scope_version: str,
        check_scope: str,
        headroom_route: HeadroomRoute,
        headroom_telemetry: Mapping[str, Any],
    ) -> ReviewReceipt:
        command = [
            *shlex.split(self.command),
            "receipt",
            "--repository",
            review_item.repository,
            "--pull-request",
            str(review_item.number),
            "--base-sha",
            review_item.base_sha,
            "--head-sha",
            review_item.head_sha,
            "--backend",
            run.backend,
            "--model",
            run.model,
            "--effort",
            run.effort,
            "--check-scope-version",
            check_scope_version,
            "--workdir",
            str(workdir),
        ]
        if check_scope:
            command.extend(["--check-scope", check_scope])
        environment = dict(os.environ)
        environment["BLUEFIN_REVIEW_REPOSITORY_ROOT"] = str(workdir)
        with self._lock:
            if run.identity in self._cancelled:
                self._cancelled.discard(run.identity)
                raise RuntimeError("review cancelled")
            process = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            self._processes[run.identity] = process
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                _kill_process_group(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            raise ReviewDeadlineExceeded(
                f"review deadline exceeded after {self.timeout:g}s"
            ) from exc
        finally:
            with self._lock:
                self._processes.pop(run.identity, None)
                self._cancelled.discard(run.identity)
        if process.returncode not in (0, 65):
            detail = (stderr or stdout).strip() or (
                f"receipt exited {process.returncode}"
            )
            raise RuntimeError(detail[:240])
        receipt = ReviewReceipt.from_json(stdout.strip())
        return _with_headroom_provenance(
            receipt, headroom_route, headroom_telemetry
        )

    def cancel(self, run: ReviewRun) -> None:
        with self._lock:
            self._cancelled.add(run.identity)
            process = self._processes.get(run.identity)
        if process is None:
            return
        _kill_process_group(process.pid, signal.SIGTERM)

    def _clear_cancel(self, run: ReviewRun) -> None:
        with self._lock:
            self._cancelled.discard(run.identity)


_REPO_FETCH_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_REPO_FETCH_LOCKS_GUARD = threading.Lock()


def _get_repo_fetch_lock(root: str, repository: str) -> threading.Lock:
    key = (str(Path(root).resolve()), repository)
    with _REPO_FETCH_LOCKS_GUARD:
        lock = _REPO_FETCH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REPO_FETCH_LOCKS[key] = lock
        return lock


def _mirror_path(root: str, repository: str) -> Path:
    return Path(root) / "mirrors" / repository.replace("/", "__")


def _worktree_path(root: str, item: BatchReviewItem) -> Path:
    digest = hashlib.sha256(
        f"{item.repository}\0{item.head_sha}".encode("utf-8")
    ).hexdigest()[:24]
    return Path(root) / f"{item.repository.replace('/', '__')}-{digest}"


def _has_commit(mirror: Path, commit_sha: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(mirror), "cat-file", "-e", commit_sha],
            capture_output=True,
            check=False,
            timeout=30,
        )
        return res.returncode == 0
    except Exception:
        return False


def _ensure_mirror(repository: str, root: str) -> Path:
    mirror = _mirror_path(root, repository)
    if mirror.exists() and (mirror / ".git").exists():
        return mirror
    with _get_repo_fetch_lock(root, repository):
        if mirror.exists() and (mirror / ".git").exists():
            return mirror
        mirror.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "repo", "clone", repository, str(mirror), "--", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return mirror


def _fetch_commits(repository: str, commits: Sequence[str], root: str) -> None:
    mirror = _ensure_mirror(repository, root)
    missing = [c for c in commits if not _has_commit(mirror, c)]
    if not missing:
        return
    with _get_repo_fetch_lock(root, repository):
        still_missing = [c for c in missing if not _has_commit(mirror, c)]
        if not still_missing:
            return
        subprocess.run(
            [
                "git",
                "-C",
                str(mirror),
                "fetch",
                "--quiet",
                "origin",
                *still_missing,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )


def _prepare_transport_batch(
    items: Sequence[BatchReviewItem],
    root: str,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    needed = [it for it in items if not _worktree_path(root, it).exists()]
    if not needed:
        return
    by_repo: dict[str, list[str]] = {}
    for it in needed:
        by_repo.setdefault(it.repository, []).append(it.head_sha)
    for repo, heads in by_repo.items():
        if is_cancelled is not None and is_cancelled():
            break
        try:
            _ensure_mirror(repo, root)
            if is_cancelled is not None and is_cancelled():
                break
            _fetch_commits(repo, heads, root)
        except Exception:
            # Prefetch only. A failure here costs a round trip, not the batch:
            # _prepare_worktree() fetches the head it needs and raises there,
            # where the failure belongs to an identifiable review.
            pass


def _prepare_worktree(item: BatchReviewItem, root: str) -> Path:
    path = _worktree_path(root, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        actual = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            timeout=30,
        ).strip()
        if actual != item.head_sha:
            raise RuntimeError(f"{item.key} worktree head drifted to {actual}")
        dirty = subprocess.check_output(
            [
                "git",
                "-C",
                str(path),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            text=True,
            timeout=30,
        )
        if dirty:
            raise RuntimeError(f"{item.key} worktree has local changes")
        return path

    mirror = _ensure_mirror(item.repository, root)
    if not _has_commit(mirror, item.head_sha):
        _fetch_commits(item.repository, [item.head_sha], root)

    target = str(path.resolve())
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(mirror),
                    "worktree",
                    "add",
                    "--detach",
                    "--quiet",
                    target,
                    item.head_sha,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            break
        except subprocess.CalledProcessError as exc:
            last_err = exc
            if path.exists():
                actual = subprocess.check_output(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    text=True,
                    timeout=30,
                ).strip()
                if actual != item.head_sha:
                    raise RuntimeError(f"{item.key} worktree head drifted to {actual}")
                dirty = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(path),
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ],
                    text=True,
                    timeout=30,
                )
                if dirty:
                    raise RuntimeError(f"{item.key} worktree has local changes")
                return path
            try:
                subprocess.run(
                    ["git", "-C", str(mirror), "worktree", "prune"],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except Exception:
                pass
            time.sleep(0.05 * (2 ** attempt))
    else:
        if last_err is not None:
            raise last_err

    return path


_DEFAULT_PREPARE_WORKTREE = _prepare_worktree


@dataclass
class ReviewBatch:
    batch_id: str
    status_path: str
    items: tuple[BatchReviewItem, ...]
    backend: str
    model: str
    effort: str
    headroom_status_line: str
    headroom_output_reduction: dict[str, object]
    running: bool = True


@dataclass(frozen=True)
class ReviewBatchResult:
    results: dict[str, ReviewReceipt]
    failures: dict[str, str]


@dataclass(frozen=True)
class _ActiveReview:
    item: BatchReviewItem
    run: ReviewRun
    controller: ReviewRunController


class ReviewEngine:
    def __init__(
        self,
        *,
        state_root: str | os.PathLike[str] | None = None,
        cache: ReviewCache | None = None,
        governor: CapacityGovernor | None = None,
        headroom_session: HeadroomSession | None = None,
        headroom_lock: threading.Lock | None = None,
        local_executor: ReviewExecutor | None = None,
        broker_executor: ReviewExecutor | None = None,
        worktree_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.state_root = Path(state_root or REVIEW_ENGINE_STATE_DIR())
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.cache = cache or ReviewCache()
        self.governor = governor or CapacityGovernor()
        self.headroom_session = (
            headroom_session or HeadroomSession.from_environment()
        )
        self.local_executor = local_executor or LocalExecutor()
        self.broker_executor = broker_executor
        if self.broker_executor is None and os.environ.get("BLUEFIN_REVIEW_EXEC_AVAILABLE") == "1":
            self.broker_executor = BrokerExecutor()
        self.worktree_root = str(worktree_root or self.state_root / "worktrees")
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_runs: dict[str, dict[str, ReviewRun]] = {}
        self._state_lock = threading.Lock()
        self._headroom_lock = headroom_lock or threading.Lock()

    def _new_batch(
        self,
        snapshot: BatchSnapshot,
        backend: str,
        model: str,
        effort: str,
        headroom_telemetry: Mapping[str, object],
    ) -> ReviewBatch:
        instance = _INSTANCE_PATTERN.sub(
            "-", os.environ.get("BLUEFIN_REVIEW_INSTANCE", "dashboard")
        ).strip("-.")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{instance}" if instance else stamp
        suffix = 1
        while True:
            batch_id = base if suffix == 1 else f"{base}-{suffix}"
            status_path = self.state_root / f"{batch_id}.jsonl"
            try:
                descriptor = os.open(
                    status_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                suffix += 1
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "version": 1,
                            "batch_id": batch_id,
                            "expect": [item.key for item in snapshot.items],
                            "ts": int(time.time()),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            return ReviewBatch(
                batch_id,
                str(status_path),
                snapshot.items,
                backend,
                model,
                effort,
                str(headroom_telemetry["status_line"]),
                dict(headroom_telemetry),
            )

    def start(
        self,
        snapshot: BatchSnapshot,
        backend: str,
        model: str,
        effort: str,
        check_scope_version: str,
        check_scope: str = "",
        on_event: Callable[[ReviewEvent], None] | None = None,
    ) -> ReviewBatch:
        self._require_ready(snapshot)
        headroom_telemetry = self._refresh_headroom(backend)
        batch = self._new_batch(
            snapshot,
            backend,
            model,
            effort,
            headroom_telemetry,
        )
        with self._state_lock:
            self._cancel_events[batch.batch_id] = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(batch, check_scope_version, check_scope, on_event),
            daemon=True,
        )
        thread.start()
        return batch

    def run_sync(
        self,
        snapshot: BatchSnapshot,
        backend: str,
        model: str,
        effort: str,
        check_scope_version: str,
        check_scope: str = "",
        on_event: Callable[[ReviewEvent], None] | None = None,
    ) -> ReviewBatchResult:
        self._require_ready(snapshot)
        headroom_telemetry = self._refresh_headroom(backend)
        batch = self._new_batch(
            snapshot,
            backend,
            model,
            effort,
            headroom_telemetry,
        )
        with self._state_lock:
            self._cancel_events[batch.batch_id] = threading.Event()
        return self._run(batch, check_scope_version, check_scope, on_event)

    @staticmethod
    def _require_ready(snapshot: BatchSnapshot) -> None:
        if not snapshot.ready:
            raise ValueError(f"batch snapshot is not ready: {snapshot.failures}")

    def _emit(
        self,
        batch: ReviewBatch,
        event: ReviewEvent,
        callback: Callable[[ReviewEvent], None] | None,
    ) -> None:
        item = next(
            (candidate for candidate in batch.items if candidate.key == event.key),
            None,
        )
        if item is not None and (
            event.batch_id != batch.batch_id
            or event.head_sha != item.head_sha
        ):
            event = replace(
                event,
                batch_id=batch.batch_id,
                head_sha=item.head_sha,
            )
        append_review_event(batch.status_path, event)
        if callback is not None:
            callback(event)

    def _is_cancelled(self, batch: ReviewBatch) -> bool:
        with self._state_lock:
            event = self._cancel_events.get(batch.batch_id)
        return event.is_set() if event is not None else False

    def effective_review_cap(self) -> int:
        return scheduler().effective_cap(self.governor)

    def active_review_slots(self) -> int:
        return scheduler().running_count()

    def _run(
        self,
        batch: ReviewBatch,
        check_scope_version: str,
        check_scope: str,
        callback: Callable[[ReviewEvent], None] | None,
    ) -> ReviewBatchResult:
        self.cache.prune()
        results: dict[str, ReviewReceipt] = {}
        failures: dict[str, str] = {}
        pending = list(batch.items)
        active: dict[Future[ReviewReceipt], _ActiveReview] = {}
        with self._state_lock:
            self._active_runs[batch.batch_id] = {}
        try:
            while pending or active:
                if self._is_cancelled(batch):
                    while pending:
                        item = pending.pop(0)
                        failures[item.key] = "cancelled before dispatch"
                        self._emit(
                            batch,
                            ReviewEvent(
                                item.key,
                                "cancelled",
                                failures[item.key],
                                int(time.time()),
                            ),
                            callback,
                        )
                    for future in active:
                        future.cancel()
                remaining: list[BatchReviewItem] = []
                for item in pending:
                    run = ReviewRun.from_request(
                        item.request(),
                        backend=batch.backend,
                        model=batch.model,
                        effort=batch.effort,
                    )
                    cached = self.cache.get(run, check_scope_version)
                    if (
                        cached is None
                        or cached.analysis.state
                        not in {"complete", "findings"}
                    ):
                        remaining.append(item)
                        continue
                    results[item.key] = cached
                    self._emit(
                        batch,
                        ReviewEvent(
                            item.key,
                            "cached",
                            "exact identity hit",
                            int(time.time()),
                            self.cache.path_for(
                                run, check_scope_version
                            ).name,
                        ),
                        callback,
                    )
                pending = remaining
                if (
                    pending
                    and not self._is_cancelled(batch)
                    and _prepare_worktree is _DEFAULT_PREPARE_WORKTREE
                ):
                    _prepare_transport_batch(
                        pending,
                        self.worktree_root,
                        is_cancelled=lambda: self._is_cancelled(batch),
                    )
                while pending and not self._is_cancelled(batch):
                    item = pending.pop(0)
                    run = ReviewRun.from_request(
                        item.request(),
                        backend=batch.backend,
                        model=batch.model,
                        effort=batch.effort,
                    )
                    controller: ReviewRunController | None = None
                    try:
                        workdir = _prepare_worktree(
                            item, self.worktree_root
                        )
                        if self._is_cancelled(batch):
                            failures[item.key] = "cancelled before dispatch"
                            self._emit(
                                batch,
                                ReviewEvent(
                                    item.key,
                                    "cancelled",
                                    failures[item.key],
                                    int(time.time()),
                                ),
                                callback,
                            )
                            continue
                        executor = (
                            self.broker_executor or self.local_executor
                        )
                        route, telemetry = self._headroom_snapshot(
                            batch.backend
                        )
                        controller = ReviewRunController(
                            run,
                            cast(Harness, executor),
                            HarnessRegistry(),
                        )
                        with self._state_lock:
                            cancel_event = self._cancel_events[
                                batch.batch_id
                            ]
                            cancelled_before_dispatch = (
                                cancel_event.is_set()
                            )
                            if not cancelled_before_dispatch:
                                controller.start()
                                self._active_runs[batch.batch_id][
                                    run.identity
                                ] = run
                                future = scheduler().submit(
                                    self.governor,
                                    self._run_one,
                                    cancel_event,
                                    executor,
                                    item,
                                    run,
                                    workdir,
                                    check_scope_version,
                                    check_scope,
                                    route,
                                    telemetry,
                                )
                                active[future] = _ActiveReview(
                                    item,
                                    run,
                                    controller,
                                )
                        if cancelled_before_dispatch:
                            failures[item.key] = (
                                "cancelled before dispatch"
                            )
                            self._emit(
                                batch,
                                ReviewEvent(
                                    item.key,
                                    "cancelled",
                                    failures[item.key],
                                    int(time.time()),
                                ),
                                callback,
                            )
                            continue
                        self._emit(
                            batch,
                            ReviewEvent(
                                item.key,
                                "running",
                                "review dispatched",
                                int(time.time()),
                            ),
                            callback,
                        )
                    except Exception as error:
                        with self._state_lock:
                            self._active_runs[batch.batch_id].pop(
                                run.identity, None
                            )
                        failures[item.key] = self._error_text(error)
                        if (
                            controller is not None
                            and controller.state is ReviewRunState.RUNNING
                        ):
                            controller.fail(failures[item.key])
                        self._emit(
                            batch,
                            ReviewEvent(
                                item.key,
                                "failed",
                                failures[item.key],
                                int(time.time()),
                            ),
                            callback,
                        )
                finished = [
                    future for future in active if future.done()
                ]
                if not finished:
                    if not active:
                        continue
                    done, _ = wait(
                        tuple(active),
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )
                    finished = list(done)
                    if not finished:
                        continue
                for future in finished:
                    current = active.pop(future)
                    if self._is_cancelled(batch):
                        current.controller.cancel()
                        failures[current.item.key] = "cancelled"
                        self._release_active(batch, current.run)
                        self._clear_executor_cancel(current.run)
                        self._emit(
                            batch,
                            ReviewEvent(
                                current.item.key,
                                "cancelled",
                                "cancelled",
                                int(time.time()),
                            ),
                            callback,
                        )
                        continue
                    try:
                        receipt = future.result()
                        expected_identity = ReceiptIdentity.from_run(
                            current.run, check_scope_version
                        )
                        if receipt.identity != expected_identity:
                            raise RuntimeError(
                                "receipt identity mismatch"
                            )
                        if receipt.analysis.state not in {
                            "complete",
                            "findings",
                        }:
                            raise RuntimeError(
                                "review ended "
                                f"{receipt.analysis.state}"
                            )
                        receipt_path = self.cache.put(receipt)
                    except Exception as error:
                        failures[current.item.key] = self._error_text(error)
                        current.controller.fail(failures[current.item.key])
                        self._release_active(batch, current.run)
                        self._emit(
                            batch,
                            ReviewEvent(
                                current.item.key,
                                "failed",
                                failures[current.item.key],
                                int(time.time()),
                            ),
                            callback,
                        )
                        continue
                    with self._state_lock:
                        cancel_event = self._cancel_events[
                            batch.batch_id
                        ]
                        cancelled_after_cache = cancel_event.is_set()
                        if not cancelled_after_cache:
                            current.controller.complete(
                                receipt.analysis_result()
                            )
                            self._active_runs[batch.batch_id].pop(
                                current.run.identity, None
                            )
                            results[current.item.key] = receipt
                    if cancelled_after_cache:
                        try:
                            self.cache.remove_if_matches(receipt)
                        except OSError as error:
                            failures[current.item.key] = self._error_text(
                                RuntimeError(
                                    "cancelled; cache cleanup failed: "
                                    f"{error}"
                                )
                            )
                            current.controller.fail(
                                failures[current.item.key]
                            )
                            self._release_active(batch, current.run)
                            self._clear_executor_cancel(current.run)
                            self._emit(
                                batch,
                                ReviewEvent(
                                    current.item.key,
                                    "failed",
                                    failures[current.item.key],
                                    int(time.time()),
                                ),
                                callback,
                            )
                            continue
                        current.controller.cancel()
                        failures[current.item.key] = "cancelled"
                        self._release_active(batch, current.run)
                        self._clear_executor_cancel(current.run)
                        self._emit(
                            batch,
                            ReviewEvent(
                                current.item.key,
                                "cancelled",
                                "cancelled",
                                int(time.time()),
                            ),
                            callback,
                        )
                        continue
                    self._emit(
                        batch,
                        ReviewEvent(
                            current.item.key,
                            receipt.analysis.state,
                            "review complete",
                            int(time.time()),
                            receipt_path.name,
                        ),
                        callback,
                    )
        finally:
            with self._state_lock:
                self._active_runs.pop(batch.batch_id, None)
                self._cancel_events.pop(batch.batch_id, None)
            telemetry = self._refresh_headroom(batch.backend)
            batch.headroom_status_line = str(telemetry["status_line"])
            batch.headroom_output_reduction = telemetry
            batch.running = False
        return ReviewBatchResult(results, failures)

    def _run_one(
        self,
        cancel_event: threading.Event,
        executor: ReviewExecutor,
        item: BatchReviewItem,
        run: ReviewRun,
        workdir: Path,
        check_scope_version: str,
        check_scope: str,
        headroom_route: HeadroomRoute,
        headroom_telemetry: Mapping[str, Any],
    ) -> ReviewReceipt:
        if cancel_event.is_set():
            raise RuntimeError("review cancelled")
        try:
            receipt = executor.run(
                item,
                run,
                workdir,
                check_scope_version,
                check_scope,
                headroom_route,
                headroom_telemetry,
            )
        except BrokerUnavailable:
            if executor is self.local_executor:
                raise
            if cancel_event.is_set():
                raise RuntimeError("review cancelled")
            headroom_route, headroom_telemetry = self._headroom_snapshot(
                run.backend
            )
            with self._state_lock:
                if cancel_event.is_set():
                    raise RuntimeError("review cancelled")
            receipt = self.local_executor.run(
                item,
                run,
                workdir,
                check_scope_version,
                check_scope,
                headroom_route,
                headroom_telemetry,
            )
        return _with_headroom_provenance(
            receipt,
            headroom_route,
            headroom_telemetry,
        )

    def _headroom_snapshot(
        self, backend: str
    ) -> tuple[HeadroomRoute, dict[str, object]]:
        with self._headroom_lock:
            route = self.headroom_session.route_for_call(backend)
            telemetry = dict(self.headroom_session.telemetry(backend))
        return route, telemetry

    def _refresh_headroom(self, backend: str) -> dict[str, object]:
        with self._headroom_lock:
            self.headroom_session.refresh(backend)
            return dict(self.headroom_session.telemetry(backend))

    @staticmethod
    def _error_text(error: Exception) -> str:
        return (str(error) or type(error).__name__)[:240]

    def _clear_executor_cancel(self, run: ReviewRun) -> None:
        clear = getattr(self.local_executor, "_clear_cancel", None)
        if callable(clear):
            clear(run)

    def _release_active(self, batch: ReviewBatch, run: ReviewRun) -> None:
        with self._state_lock:
            self._active_runs[batch.batch_id].pop(run.identity, None)

    def cancel(self, batch: ReviewBatch) -> None:
        event = self._cancel_events.get(batch.batch_id)
        if event is not None:
            event.set()
        with self._state_lock:
            if event is None:
                event = self._cancel_events.setdefault(
                    batch.batch_id, threading.Event()
                )
                event.set()
            active_runs = tuple(
                self._active_runs.get(batch.batch_id, {}).values()
            )
        for run in active_runs:
            cancel = getattr(self.local_executor, "cancel", None)
            if callable(cancel):
                cancel(run)
            broker_cancel = getattr(self.broker_executor, "cancel", None)
            if callable(broker_cancel):
                broker_cancel(run)


__all__ = [
    "BLUEFIN_REVIEW_DEADLINE_SECONDS",
    "BrokerUnavailable",
    "DEFAULT_REVIEW_DEADLINE_SECONDS",
    "LocalExecutor",
    "REVIEW_ENGINE_STATE_DIR",
    "ReviewBatch",
    "ReviewBatchResult",
    "ReviewDeadlineExceeded",
    "ReviewEngine",
    "ReviewEvent",
    "append_review_event",
    "parse_review_status",
]
