import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.capacity import CapacityGovernor
from tui.review_cache import ReviewCache
from tui.review_engine import (
    ReviewEngine,
    _mirror_path,
    _prepare_worktree,
    _worktree_path,
)
from tui.review_receipt import ReviewReceipt
from tui.review_result import ReviewResult
from tui.review_snapshot import BatchReviewItem, BatchSnapshot

BASE = "a" * 40
HEADS = ("b" * 40, "c" * 40, "d" * 40, "e" * 40)


def item(number, head, repo="projectbluefin/review"):
    return BatchReviewItem(
        f"{repo}#{number}",
        repo,
        number,
        f"PR {number}",
        BASE,
        head,
        {"baseRefOid": BASE, "headRefOid": head},
        [],
    )


class ConcurrentTrackingExecutor:
    def __init__(self, barrier_count=0):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.barrier = (
            threading.Barrier(barrier_count) if barrier_count > 0 else None
        )

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
            if self.active > self.max_active:
                self.max_active = self.active
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
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


class ReviewTransportContractTests(unittest.TestCase):
    def _scratch_dir(self):
        scratch = Path(__file__).parents[1] / ".cache" / "transport-contract"
        scratch.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=scratch)

    def test_batch_transport_single_clone_and_n_worktrees(self):
        """N pull requests in one repository perform ONE clone and N worktree preparations."""
        with self._scratch_dir() as root:
            # Set up a real local origin git repo with 3 commits
            origin = Path(root) / "origin"
            origin.mkdir()
            subprocess.run(["git", "-C", str(origin), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@e"], check=True)
            commits = []
            for i in range(3):
                (origin / f"file_{i}.txt").write_text(f"content {i}\n")
                subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
                subprocess.run(
                    ["git", "-C", str(origin), "commit", "--no-verify", "-m", f"test: {i}"],
                    check=True,
                )
                commits.append(
                    subprocess.check_output(
                        ["git", "-C", str(origin), "rev-parse", "HEAD"], text=True
                    ).strip()
                )

            recorded_commands = []
            original_run = subprocess.run

            def intercept_run(cmd, *args, **kwargs):
                recorded_commands.append(list(cmd))
                # Intercept gh repo clone to clone from local origin
                if len(cmd) >= 3 and cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
                    dest = cmd[4]
                    return original_run(
                        ["git", "clone", "--quiet", str(origin), dest],
                        *args,
                        **kwargs,
                    )
                # Intercept git fetch origin to fetch from local origin
                if len(cmd) >= 4 and cmd[0] == "git" and "fetch" in cmd and "origin" in cmd:
                    new_cmd = list(cmd)
                    idx = new_cmd.index("origin")
                    new_cmd[idx] = str(origin)
                    return original_run(new_cmd, *args, **kwargs)
                return original_run(cmd, *args, **kwargs)

            worktree_root = Path(root) / "worktrees"
            engine = ReviewEngine(
                state_root=root,
                worktree_root=worktree_root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(cap=3, per_review_budget_mb=1, reserve_mb=1),
                local_executor=ConcurrentTrackingExecutor(),
            )

            items = tuple(item(i + 1, commits[i]) for i in range(3))
            with patch("subprocess.run", side_effect=intercept_run):
                result = engine.run_sync(
                    BatchSnapshot(items, {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )

            self.assertEqual(len(result.failures), 0)
            self.assertEqual(len(result.results), 3)

            clone_calls = [
                c for c in recorded_commands
                if len(c) >= 3 and c[0] == "gh" and c[1] == "repo" and c[2] == "clone"
            ]
            self.assertEqual(
                len(clone_calls),
                1,
                f"Expected exactly 1 clone for 3 PRs in same repo, got {len(clone_calls)}: {clone_calls}",
            )

            worktree_adds = [
                c for c in recorded_commands
                if len(c) >= 4 and c[0] == "git" and "worktree" in c and "add" in c
            ]
            self.assertEqual(
                len(worktree_adds),
                3,
                f"Expected exactly 3 worktree preparations, got {len(worktree_adds)}: {worktree_adds}",
            )

    def test_prs_across_multiple_repositories_produce_one_mirror_per_repo(self):
        """Pull requests across several repositories produce one mirror per repository, not one per PR."""
        with self._scratch_dir() as root:
            # Create two origins
            origins = {}
            commits_by_repo = {}
            for repo_name in ("org/repoA", "org/repoB"):
                repo_origin = Path(root) / repo_name.replace("/", "__")
                repo_origin.mkdir(parents=True)
                subprocess.run(["git", "-C", str(repo_origin), "init", "--quiet"], check=True)
                subprocess.run(["git", "-C", str(repo_origin), "config", "user.name", "T"], check=True)
                subprocess.run(["git", "-C", str(repo_origin), "config", "user.email", "t@e"], check=True)
                repo_commits = []
                for i in range(2):
                    (repo_origin / f"f_{i}.txt").write_text(f"{repo_name} {i}\n")
                    subprocess.run(["git", "-C", str(repo_origin), "add", "."], check=True)
                    subprocess.run(
                        ["git", "-C", str(repo_origin), "commit", "--no-verify", "-m", f"test: {i}"],
                        check=True,
                    )
                    repo_commits.append(
                        subprocess.check_output(
                            ["git", "-C", str(repo_origin), "rev-parse", "HEAD"], text=True
                        ).strip()
                    )
                origins[repo_name] = repo_origin
                commits_by_repo[repo_name] = repo_commits

            recorded_commands = []
            original_run = subprocess.run

            def intercept_run(cmd, *args, **kwargs):
                recorded_commands.append(list(cmd))
                if len(cmd) >= 3 and cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
                    repo = cmd[3]
                    dest = cmd[4]
                    return original_run(
                        ["git", "clone", "--quiet", str(origins[repo]), dest],
                        *args,
                        **kwargs,
                    )
                if len(cmd) >= 4 and cmd[0] == "git" and "fetch" in cmd and "origin" in cmd:
                    new_cmd = list(cmd)
                    idx = new_cmd.index("origin")
                    # Determine repo from mirror path
                    mirror_path = cmd[cmd.index("-C") + 1]
                    matching_origin = next(
                        (str(o) for r, o in origins.items() if r.replace("/", "__") in mirror_path),
                        "origin",
                    )
                    new_cmd[idx] = matching_origin
                    return original_run(new_cmd, *args, **kwargs)
                return original_run(cmd, *args, **kwargs)

            worktree_root = Path(root) / "worktrees"
            engine = ReviewEngine(
                state_root=root,
                worktree_root=worktree_root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(cap=4, per_review_budget_mb=1, reserve_mb=1),
                local_executor=ConcurrentTrackingExecutor(),
            )

            # 2 PRs in repoA, 2 PRs in repoB = 4 PRs across 2 repos
            items = (
                item(1, commits_by_repo["org/repoA"][0], repo="org/repoA"),
                item(2, commits_by_repo["org/repoA"][1], repo="org/repoA"),
                item(1, commits_by_repo["org/repoB"][0], repo="org/repoB"),
                item(2, commits_by_repo["org/repoB"][1], repo="org/repoB"),
            )

            with patch("subprocess.run", side_effect=intercept_run):
                result = engine.run_sync(
                    BatchSnapshot(items, {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )

            self.assertEqual(len(result.failures), 0)
            self.assertEqual(len(result.results), 4)

            clone_calls = [
                c for c in recorded_commands
                if len(c) >= 3 and c[0] == "gh" and c[1] == "repo" and c[2] == "clone"
            ]
            self.assertEqual(
                len(clone_calls),
                2,
                f"Expected exactly 2 clones for 2 repos (not 4), got {len(clone_calls)}: {clone_calls}",
            )

            # Verify mirror directories: exactly 2 mirrors exist
            mirror_a = _mirror_path(str(worktree_root), "org/repoA")
            mirror_b = _mirror_path(str(worktree_root), "org/repoB")
            self.assertTrue(mirror_a.exists(), f"Mirror A {mirror_a} should exist")
            self.assertTrue(mirror_b.exists(), f"Mirror B {mirror_b} should exist")

    def test_two_pull_requests_in_same_repo_review_concurrently(self):
        """Two pull requests in the same repository review concurrently."""
        with self._scratch_dir() as root:
            origin = Path(root) / "origin"
            origin.mkdir()
            subprocess.run(["git", "-C", str(origin), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@e"], check=True)
            commits = []
            for i in range(2):
                (origin / f"f_{i}.txt").write_text(f"content {i}\n")
                subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
                subprocess.run(
                    ["git", "-C", str(origin), "commit", "--no-verify", "-m", f"test: {i}"],
                    check=True,
                )
                commits.append(
                    subprocess.check_output(
                        ["git", "-C", str(origin), "rev-parse", "HEAD"], text=True
                    ).strip()
                )

            original_run = subprocess.run

            def intercept_run(cmd, *args, **kwargs):
                if len(cmd) >= 3 and cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
                    dest = cmd[4]
                    return original_run(["git", "clone", "--quiet", str(origin), dest], *args, **kwargs)
                if len(cmd) >= 4 and cmd[0] == "git" and "fetch" in cmd and "origin" in cmd:
                    new_cmd = list(cmd)
                    idx = new_cmd.index("origin")
                    new_cmd[idx] = str(origin)
                    return original_run(new_cmd, *args, **kwargs)
                return original_run(cmd, *args, **kwargs)

            worktree_root = Path(root) / "worktrees"
            executor = ConcurrentTrackingExecutor(barrier_count=2)
            engine = ReviewEngine(
                state_root=root,
                worktree_root=worktree_root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(cap=2, per_review_budget_mb=1, reserve_mb=1),
                local_executor=executor,
            )

            items = (item(1, commits[0]), item(2, commits[1]))
            with patch("subprocess.run", side_effect=intercept_run):
                result = engine.run_sync(
                    BatchSnapshot(items, {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )

            self.assertEqual(len(result.failures), 0)
            self.assertEqual(len(result.results), 2)
            self.assertGreaterEqual(
                executor.max_active,
                2,
                f"Observed peak concurrency must be >= 2, got {executor.max_active}",
            )

    def test_head_drift_is_refused(self):
        """A worktree whose head has drifted is refused."""
        with self._scratch_dir() as root:
            selected = item(1, HEADS[0])
            path = _worktree_path(root, selected)
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(path), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e"], check=True)
            (path / "f.txt").write_text("clean\n")
            subprocess.run(["git", "-C", str(path), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(path), "commit", "--no-verify", "-m", "test: seed"],
                check=True,
            )
            with self.assertRaisesRegex(RuntimeError, "drifted"):
                _prepare_worktree(selected, root)

    def test_dirty_worktree_is_refused(self):
        """A worktree with local modifications is refused."""
        with self._scratch_dir() as root:
            path = _worktree_path(root, item(1, HEADS[0]))
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(path), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e"], check=True)
            (path / "tracked.txt").write_text("clean\n")
            subprocess.run(["git", "-C", str(path), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(path), "commit", "--no-verify", "-m", "test: seed"],
                check=True,
            )
            head = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
            selected = item(1, head)
            # Recompute expected path matching head
            actual_path = _worktree_path(root, selected)
            if actual_path != path:
                path.rename(actual_path)
                path = actual_path
            (path / "tracked.txt").write_text("dirty changes\n")
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                _prepare_worktree(selected, root)

    def test_standalone_prepare_worktree_reuses_mirror(self):
        """Standalone _prepare_worktree clones mirror once and reuses it for subsequent items."""
        with self._scratch_dir() as root:
            origin = Path(root) / "origin"
            origin.mkdir()
            subprocess.run(["git", "-C", str(origin), "init", "--quiet"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@e"], check=True)
            commits = []
            for i in range(2):
                (origin / f"f_{i}.txt").write_text(f"content {i}\n")
                subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
                subprocess.run(
                    ["git", "-C", str(origin), "commit", "--no-verify", "-m", f"test: {i}"],
                    check=True,
                )
                commits.append(
                    subprocess.check_output(
                        ["git", "-C", str(origin), "rev-parse", "HEAD"], text=True
                    ).strip()
                )

            recorded_commands = []
            original_run = subprocess.run

            def intercept_run(cmd, *args, **kwargs):
                recorded_commands.append(list(cmd))
                if len(cmd) >= 3 and cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
                    dest = cmd[4]
                    return original_run(["git", "clone", "--quiet", str(origin), dest], *args, **kwargs)
                if len(cmd) >= 4 and cmd[0] == "git" and "fetch" in cmd and "origin" in cmd:
                    new_cmd = list(cmd)
                    idx = new_cmd.index("origin")
                    new_cmd[idx] = str(origin)
                    return original_run(new_cmd, *args, **kwargs)
                return original_run(cmd, *args, **kwargs)

            worktree_root = Path(root) / "worktrees"
            item1 = item(1, commits[0])
            item2 = item(2, commits[1])

            with patch("subprocess.run", side_effect=intercept_run):
                wt1 = _prepare_worktree(item1, str(worktree_root))
                wt2 = _prepare_worktree(item2, str(worktree_root))

            self.assertTrue(wt1.exists())
            self.assertTrue(wt2.exists())
            self.assertNotEqual(wt1, wt2)

            clones = [
                c for c in recorded_commands
                if len(c) >= 3 and c[0] == "gh" and c[1] == "repo" and c[2] == "clone"
            ]
            self.assertEqual(len(clones), 1, f"Expected 1 clone, got {len(clones)}")


if __name__ == "__main__":
    unittest.main()
