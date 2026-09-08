# image/tui/review_cache.py
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator

from tui.review_receipt import ReviewReceipt, cache_digest
from tui.review_run import ReviewRun

REVIEW_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60


class ReviewCache:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        if root is None:
            state_root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            root = os.path.join(state_root, "bluefin-review", "reviews")
        self.root = Path(root).expanduser()

    @staticmethod
    def prefix(run: ReviewRun) -> str:
        owner, repository = run.repository.split("/", 1)
        return f"{owner}__{repository}__{run.pull_request}"

    @staticmethod
    def _digest(run: ReviewRun, check_scope_version: str) -> str:
        return cache_digest(run, check_scope_version)

    def path_for(self, run: ReviewRun, check_scope_version: str) -> Path:
        return self.root / (
            f"{self.prefix(run)}-{self._digest(run, check_scope_version)}.json"
        )

    def path_for_receipt(self, receipt: ReviewReceipt) -> Path:
        identity = receipt.identity
        owner, repository = identity.repository.split("/", 1)
        digest = cache_digest(identity.run_identity, identity.check_scope_version)
        return self.root / (
            f"{owner}__{repository}__{identity.pull_request}-{digest}.json"
        )

    @contextlib.contextmanager
    def _locked_root(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.root / ".cache.lock",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        with os.fdopen(descriptor) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, run: ReviewRun, check_scope_version: str) -> ReviewReceipt | None:
        path = self.path_for(run, check_scope_version)
        try:
            receipt = ReviewReceipt.from_json(path.read_text(encoding="utf-8"))
            if receipt.identity.cache_identity != self._digest(run, check_scope_version):
                return None
            return receipt
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
            return None

    def put(self, receipt: ReviewReceipt) -> Path:
        path = self.path_for_receipt(receipt)
        with self._locked_root():
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root, prefix=".review-", suffix=".tmp"
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(receipt.to_json())
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def remove_if_matches(self, receipt: ReviewReceipt) -> bool:
        path = self.path_for_receipt(receipt)
        with self._locked_root():
            try:
                if path.read_text(encoding="utf-8").strip() != receipt.to_json():
                    return False
                path.unlink()
                return True
            except FileNotFoundError:
                return False
            except UnicodeError:
                return False

    def prune(self, now: float | None = None) -> None:
        cutoff = (time.time() if now is None else now) - REVIEW_CACHE_RETENTION_SECONDS
        if not self.root.exists():
            return
        try:
            with self._locked_root():
                names = list(self.root.iterdir())
                for path in names:
                    if path.suffix != ".json":
                        continue
                    try:
                        if path.is_file() and path.stat().st_mtime < cutoff:
                            path.unlink()
                    except OSError:
                        continue
        except OSError:
            return
