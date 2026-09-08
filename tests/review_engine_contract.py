import json
import subprocess
import threading
import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.capacity import CapacityGovernor
from tui.headroom import HeadroomRoute
from tui.review_cache import ReviewCache
from tui.review_engine import (
    BrokerUnavailable,
    LocalExecutor,
    ReviewEngine,
    ReviewEvent,
    _prepare_worktree,
    _worktree_path,
    append_review_event,
    parse_review_status,
)
from tui.review_receipt import ReviewReceipt
from tui.review_result import ReviewResult
from tui.review_snapshot import BatchReviewItem, BatchSnapshot
from tui.review_run import ReviewRun


BASE = "a" * 40
HEADS = ("b" * 40, "c" * 40, "d" * 40)


def item(number, head):
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


class FakeLocalExecutor:
    def __init__(self):
        self.calls = []
        self.workdirs = []
        self.started = threading.Barrier(2)

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
        self.calls.append(review_item.key)
        if len(self.calls) <= 2:
            self.started.wait(timeout=5)
        self.workdirs.append(workdir)
        if review_item.number == 2:
            raise RuntimeError("provider failed")
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


class FakeHeadroomSession:
    def __init__(self, routes=None):
        self.refresh_calls = []
        self.route_calls = []
        self.routes = list(routes or [])

    def refresh(self, backend):
        self.refresh_calls.append(backend)
        return HeadroomRoute(
            "ACTIVE", backend, "http://127.0.0.1:8787", "ready"
        )

    def route_for_call(self, backend):
        self.route_calls.append(backend)
        if self.routes:
            return self.routes.pop(0)
        return HeadroomRoute(
            "ACTIVE", backend, "http://127.0.0.1:8787", "ready"
        )

    def telemetry(self, backend):
        return {
            "state": "ACTIVE",
            "route": "http://127.0.0.1:8787",
            "status_line": "[ACTIVE] Codex: via Headroom; Caveman ON [C]",
            "requests": 4,
            "tokens_saved": 300,
            "output_tokens_saved": 20,
            "output_reduction_percent": 20.0,
            "output_reduction_method": "measured",
            "statistics_degraded": False,
        }

    def status_line(self, backend, caveman):
        return self.telemetry(backend)["status_line"]


class BlockingRouteHeadroom(FakeHeadroomSession):
    def __init__(self):
        super().__init__()
        self.routing = threading.Event()
        self.release = threading.Event()

    def route_for_call(self, backend):
        self.routing.set()
        self.release.wait(timeout=5)
        return super().route_for_call(backend)


class BlockingFallbackRouteHeadroom(FakeHeadroomSession):
    def __init__(self):
        super().__init__()
        self.fallback_routing = threading.Event()
        self.release = threading.Event()

    def route_for_call(self, backend):
        self.route_calls.append(backend)
        if len(self.route_calls) == 2:
            self.fallback_routing.set()
            self.release.wait(timeout=5)
        return HeadroomRoute(
            "ACTIVE", backend, "http://127.0.0.1:8787", "ready"
        )


class InterleavingHeadroom(FakeHeadroomSession):
    def __init__(self):
        super().__init__()
        self._state = "DIRECT"
        self._route = ""
        self._worker_calls = 0
        self._worker_lock = threading.Lock()
        self._first_worker_routing = threading.Event()

    def route_for_call(self, backend):
        self.route_calls.append(backend)
        if threading.current_thread() is threading.main_thread():
            self._state = "DIRECT"
            self._route = ""
            return HeadroomRoute("DIRECT", backend, None, "initial")
        with self._worker_lock:
            self._worker_calls += 1
            call = self._worker_calls
        if call == 1:
            self._state = "ACTIVE"
            self._route = "http://127.0.0.1:8787"
            self._first_worker_routing.set()
            time.sleep(0.1)
            return HeadroomRoute(
                "ACTIVE", backend, "http://127.0.0.1:8787", "ready"
            )
        self._first_worker_routing.wait(timeout=1)
        self._state = "DEGRADED"
        self._route = ""
        return HeadroomRoute(
            "DEGRADED", backend, None, "readiness probe failed"
        )

    def telemetry(self, backend):
        return {
            **super().telemetry(backend),
            "state": self._state,
            "route": self._route,
            "status_line": f"[{self._state}] {backend}",
        }


class ImmediateExecutor:
    def __init__(self, state="complete"):
        self.calls = []
        self.state = state

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
        self.calls.append(review_item.key)
        return ReviewReceipt.from_result(
            run,
            ReviewResult(
                1,
                self.state,
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


class BlockingExecutor(ImmediateExecutor):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, *args):
        self.started.set()
        self.release.wait(timeout=5)
        return super().run(*args)

    def cancel(self, run):
        self.release.set()


class UnavailableBroker:
    def run(self, *args):
        raise BrokerUnavailable("broker offline")


class RecordingExecutor(ImmediateExecutor):
    def __init__(self):
        super().__init__()
        self.snapshots = []

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
        self.snapshots.append(
            (headroom_route.state, headroom_telemetry["state"])
        )
        return super().run(
            review_item,
            run,
            workdir,
            check_scope_version,
            check_scope,
            headroom_route,
            headroom_telemetry,
        )


class FailingPutCache(ReviewCache):
    def put(self, receipt):
        if receipt.identity.pull_request == 1:
            raise OSError("disk full")
        return super().put(receipt)


class BlockingPutCache(ReviewCache):
    def __init__(self, root):
        super().__init__(root)
        self.started = threading.Event()
        self.release = threading.Event()

    def put(self, receipt):
        self.started.set()
        self.release.wait(timeout=5)
        return super().put(receipt)


class ReplacingPutCache(ReviewCache):
    def __init__(self, root):
        super().__init__(root)
        self.started = threading.Event()
        self.release = threading.Event()
        self.replacement = None

    def put(self, receipt):
        path = super().put(receipt)
        self.started.set()
        self.release.wait(timeout=5)
        if self.replacement is not None:
            ReviewCache.put(self, self.replacement)
        return path


class CleanupFailingCache(BlockingPutCache):
    def remove_if_matches(self, receipt):
        raise OSError("cache cleanup denied")


class EngineContractTests(unittest.TestCase):
    @staticmethod
    def prepared_worktree(review_item, root):
        path = (
            Path(root)
            / f"{review_item.repository.replace('/', '__')}-{review_item.head_sha[:24]}"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_three_items_dispatch_and_one_failure_does_not_block_the_other_two(self):
        with tempfile.TemporaryDirectory() as root:
            executor = FakeLocalExecutor()
            governor = CapacityGovernor(
                cap=2,
                per_review_budget_mb=1,
                reserve_mb=1,
                mem_available_mb=lambda: 100,
                cpu_count=lambda: 4,
            )
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=governor,
                local_executor=executor,
            )
            events = []
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot(
                        tuple(item(n, h) for n, h in enumerate(HEADS, 1)), {}
                    ),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                    on_event=events.append,
                )
            self.assertEqual(
                set(result.results),
                {
                    "projectbluefin/review#1",
                    "projectbluefin/review#3",
                },
            )
            self.assertEqual(
                result.failures["projectbluefin/review#2"], "provider failed"
            )
            self.assertEqual(len(executor.calls), 3)
            self.assertEqual(len(set(executor.workdirs)), 3)
            self.assertTrue(events)
            self.assertTrue(all(event.batch_id for event in events))
            self.assertTrue(
                all(
                    event.head_sha
                    == next(
                        selected.head_sha
                        for selected in (
                            item(n, h)
                            for n, h in enumerate(HEADS, 1)
                        )
                        if selected.key == event.key
                    )
                    for event in events
                )
            )
            status_path = next(Path(root).glob("*.jsonl"))
            status = parse_review_status(str(status_path))
            self.assertEqual(status["projectbluefin/review#1"]["state"], "complete")
            self.assertEqual(status["projectbluefin/review#2"]["state"], "failed")
            self.assertEqual(status["projectbluefin/review#3"]["state"], "complete")
            self.assertNotIn(str(executor.workdirs[0]), status_path.read_text())

    def test_exact_cache_hit_skips_executor(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ReviewCache(Path(root) / "reviews")
            fake = FakeLocalExecutor()
            headroom = FakeHeadroomSession()
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=headroom,
                local_executor=fake,
            )
            selected = item(1, HEADS[0])
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            cache.put(
                ReviewReceipt.from_result(
                    run,
                    ReviewResult(
                        1,
                        "complete",
                        {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        [],
                        [],
                        {
                            "backend": "goose",
                            "model": "gemini-3.8-flash",
                        },
                        {},
                        {},
                        [],
                    ),
                    ["cached"],
                    "scope-v7",
                )
            )
            result = engine.run_sync(
                BatchSnapshot((selected,), {}),
                "goose",
                "gemini-3.8-flash",
                "high",
                "scope-v7",
            )
            self.assertIn(selected.key, result.results)
            self.assertEqual(fake.calls, [])
            self.assertEqual(headroom.route_calls, [])
            self.assertEqual(result.results[selected.key].transcript, ("cached",))

    def test_exact_cache_hit_completes_when_runnable_capacity_is_zero(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ReviewCache(Path(root) / "reviews")
            selected = item(1, HEADS[0])
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            cache.put(
                ReviewReceipt.from_result(
                    run,
                    ReviewResult(
                        1,
                        "complete",
                        {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        [],
                        [],
                        {"backend": run.backend, "model": run.model},
                        {},
                        {},
                        [],
                    ),
                    ["cached"],
                    "scope-v7",
                )
            )
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 0,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor(),
            )
            batch = engine.start(
                BatchSnapshot((selected,), {}),
                "goose",
                "gemini-3.8-flash",
                "high",
                "scope-v7",
            )
            try:
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(batch.running)
                self.assertEqual(
                    parse_review_status(batch.status_path)[selected.key]["state"],
                    "cached",
                )
            finally:
                engine.cancel(batch)

    def test_non_success_cache_entry_is_retried(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ReviewCache(Path(root) / "reviews")
            selected = item(1, HEADS[0])
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            cache.put(
                ReviewReceipt.from_result(
                    run,
                    ReviewResult(
                        1,
                        "failed",
                        {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        [],
                        [],
                        {"backend": run.backend, "model": run.model},
                        {},
                        {},
                        [],
                    ),
                    ["failed"],
                    "scope-v7",
                )
            )
            executor = ImmediateExecutor()
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(executor.calls, [selected.key])
            self.assertEqual(result.results[selected.key].analysis.state, "complete")

    def test_later_cache_hit_is_not_blocked_by_an_uncached_zero_slot_item(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ReviewCache(Path(root) / "reviews")
            uncached = item(1, HEADS[0])
            cached_item = item(2, HEADS[1])
            cached_run = ReviewRun.from_request(
                cached_item.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            cache.put(
                ReviewReceipt.from_result(
                    cached_run,
                    ReviewResult(
                        1,
                        "complete",
                        {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        [],
                        [],
                        {
                            "backend": cached_run.backend,
                            "model": cached_run.model,
                        },
                        {},
                        {},
                        [],
                    ),
                    ["cached"],
                    "scope-v7",
                )
            )
            executor = ImmediateExecutor()
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 0,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )
            batch = engine.start(
                BatchSnapshot((uncached, cached_item), {}),
                "goose",
                "gemini-3.8-flash",
                "high",
                "scope-v7",
            )
            try:
                deadline = time.monotonic() + 1
                state = {}
                while time.monotonic() < deadline:
                    state = parse_review_status(batch.status_path)
                    if state.get(cached_item.key, {}).get("state") == "cached":
                        break
                    time.sleep(0.01)
                self.assertEqual(state[cached_item.key]["state"], "cached")
                self.assertTrue(batch.running)
                self.assertEqual(executor.calls, [])
            finally:
                engine.cancel(batch)
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)

    def test_headroom_refreshes_per_batch_and_routes_each_dispatched_pr(self):
        with tempfile.TemporaryDirectory() as root:
            headroom = FakeHeadroomSession()
            executor = FakeLocalExecutor()
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=2,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 4,
                ),
                headroom_session=headroom,
                local_executor=executor,
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot(
                        tuple(item(n, h) for n, h in enumerate(HEADS, 1)), {}
                    ),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(headroom.refresh_calls, ["goose", "goose"])
            self.assertEqual(
                headroom.route_calls, ["goose", "goose", "goose"]
            )
            self.assertEqual(
                result.results[
                    "projectbluefin/review#1"
                ].provenance["headroom_state"],
                "ACTIVE",
            )
            self.assertEqual(
                result.results[
                    "projectbluefin/review#1"
                ].provenance["headroom_output_reduction_percent"],
                20.0,
            )

    def test_state_parser_repairs_a_torn_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.jsonl"
            path.write_text(
                '{"key":"one","state":"complete"}\n{"key":"two","sta'
            )
            events = parse_review_status(str(path))
            self.assertEqual(events["one"]["state"], "complete")
            self.assertNotIn("two", events)

    def test_event_append_preserves_complete_tail_and_replaces_torn_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.jsonl"
            path.write_text('{"key":"one","state":"running"}')
            append_review_event(
                str(path), ReviewEvent("two", "complete", "done", 7)
            )
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["key"], "one")
            self.assertEqual(json.loads(lines[1])["key"], "two")

            path.write_text(
                '{"key":"one","state":"complete"}\n{"key":"two","sta'
            )
            append_review_event(
                str(path), ReviewEvent("three", "failed", "provider failed", 8)
            )
            lines = path.read_text().splitlines()
            self.assertEqual(
                [json.loads(line)["key"] for line in lines],
                ["one", "three"],
            )

    def test_terminal_event_retry_is_idempotent_and_nonterminal_reentry_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.jsonl"
            terminal = ReviewEvent("one", "complete", "done", 7, "one.json")
            append_review_event(str(path), terminal)
            append_review_event(str(path), terminal)
            self.assertEqual(len(path.read_text().splitlines()), 1)

            with self.assertRaisesRegex(RuntimeError, "already terminal"):
                append_review_event(
                    str(path), ReviewEvent("one", "running", "again", 8)
                )
            self.assertEqual(
                parse_review_status(str(path))["one"]["state"], "complete"
            )

    def test_terminal_event_retry_ignores_a_new_write_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.jsonl"
            append_review_event(
                str(path), ReviewEvent("one", "complete", "done", 7, "one.json")
            )
            append_review_event(
                str(path), ReviewEvent("one", "complete", "done", 8, "one.json")
            )
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_local_executor_uses_receipt_mode_and_returns_bounded_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            workdir = Path(root) / "worktree"
            workdir.mkdir()
            args_path = Path(root) / "args.json"
            env_path = Path(root) / "env.txt"
            run = ReviewRun.from_request(
                item(1, HEADS[0]).request(),
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
                ["compact"],
                "scope-v7",
            )
            command = Path(root) / "receipt-command"
            command.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                f"open({str(args_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                f"open({str(env_path)!r}, 'w').write(\n"
                "    os.environ['BLUEFIN_REVIEW_REPOSITORY_ROOT']\n"
                ")\n"
                f"print({receipt.to_json()!r})\n"
            )
            command.chmod(0o755)

            actual = LocalExecutor(str(command)).run(
                item(1, HEADS[0]),
                run,
                workdir,
                "scope-v7",
                "/opt/bluefin/review-scope",
                HeadroomRoute(
                    "ACTIVE", "goose", "http://127.0.0.1:8787", "ready"
                ),
                {
                    "status_line": "[ACTIVE] Goose via Headroom",
                    "output_reduction_percent": 20.0,
                    "output_reduction_method": "measured",
                    "output_tokens_saved": 20,
                },
            )

            arguments = json.loads(args_path.read_text())
            self.assertEqual(arguments[0], "receipt")
            self.assertEqual(env_path.read_text(), str(workdir))
            self.assertIn("--check-scope", arguments)
            self.assertEqual(actual.provenance["headroom_state"], "ACTIVE")
            self.assertEqual(
                actual.provenance["headroom_output_reduction_percent"], 20.0
            )

    def test_existing_worktree_must_be_clean_at_the_exact_head(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            path = _worktree_path(root, selected)
            path.mkdir(parents=True)
            subprocess.run(
                ["git", "-C", str(path), "init", "--quiet"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "config",
                    "user.email",
                    "test@example.com",
                ],
                check=True,
            )
            tracked = path / "tracked.txt"
            tracked.write_text("clean\n")
            subprocess.run(
                ["git", "-C", str(path), "add", "tracked.txt"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "commit",
                    "--quiet",
                    "-m",
                    "test: seed worktree",
                ],
                check=True,
            )
            head = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            selected = item(1, head)
            expected_path = _worktree_path(root, selected)
            if expected_path != path:
                path.rename(expected_path)
                path = expected_path
            tracked = path / "tracked.txt"
            tracked.write_text("dirty\n")
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                _prepare_worktree(selected, root)

    def test_unready_snapshot_is_rejected_before_batch_creation(self):
        with tempfile.TemporaryDirectory() as root:
            engine = ReviewEngine(state_root=root)
            with self.assertRaisesRegex(ValueError, "not ready"):
                engine.start(
                    BatchSnapshot((), {"projectbluefin/review#1": "missing head"}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(list(Path(root).glob("*.jsonl")), [])

    def test_cancelled_active_review_is_not_cached_or_reported_complete(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            executor = BlockingExecutor()
            cache = ReviewCache(Path(root) / "reviews")
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(executor.started.wait(timeout=1))
                engine.cancel(batch)
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(
                parse_review_status(batch.status_path)[selected.key]["state"],
                "cancelled",
            )
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            self.assertIsNone(cache.get(run, "scope-v7"))

    def test_cache_write_failure_is_isolated_from_other_reviews(self):
        with tempfile.TemporaryDirectory() as root:
            engine = ReviewEngine(
                state_root=root,
                cache=FailingPutCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=2,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 4,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot(
                        (item(1, HEADS[0]), item(2, HEADS[1])), {}
                    ),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(
                result.failures["projectbluefin/review#1"], "disk full"
            )
            self.assertIn("projectbluefin/review#2", result.results)

    def test_failed_receipt_is_retryable_and_not_cached(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            cache = ReviewCache(Path(root) / "reviews")
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor("failed"),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertNotIn(selected.key, result.results)
            self.assertIn(selected.key, result.failures)
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            self.assertIsNone(cache.get(run, "scope-v7"))

    def test_receipt_identity_mismatch_is_a_per_review_failure(self):
        class WrongReceiptExecutor(ImmediateExecutor):
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
                wrong_run = ReviewRun.from_request(
                    item(2, HEADS[1]).request(),
                    backend=run.backend,
                    model=run.model,
                    effort=run.effort,
                )
                return ReviewReceipt.from_result(
                    wrong_run,
                    ReviewResult(
                        1,
                        "complete",
                        {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        [],
                        [],
                        {"backend": run.backend, "model": run.model},
                        {},
                        {},
                        [],
                    ),
                    ["wrong review"],
                    check_scope_version,
                )

        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            cache = ReviewCache(Path(root) / "reviews")
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=WrongReceiptExecutor(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(
                result.failures[selected.key], "receipt identity mismatch"
            )
            self.assertEqual(result.results, {})
            expected_run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            self.assertIsNone(cache.get(expected_run, "scope-v7"))

    def test_cancel_during_cache_write_discards_the_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            cache = BlockingPutCache(Path(root) / "reviews")
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(cache.started.wait(timeout=1))
                engine.cancel(batch)
                cache.release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(
                parse_review_status(batch.status_path)[selected.key]["state"],
                "cancelled",
            )
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            self.assertIsNone(cache.get(run, "scope-v7"))

    def test_cancel_during_worktree_preparation_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            executor = ImmediateExecutor()
            preparing = threading.Event()
            release = threading.Event()

            def prepare(review_item, worktree_root):
                preparing.set()
                release.wait(timeout=5)
                return self.prepared_worktree(review_item, worktree_root)

            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=prepare,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(preparing.wait(timeout=1))
                engine.cancel(batch)
                release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(executor.calls, [])
            self.assertEqual(
                parse_review_status(batch.status_path)[selected.key]["state"],
                "cancelled",
            )

    def test_cancel_during_route_selection_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            executor = ImmediateExecutor()
            headroom = BlockingRouteHeadroom()
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=headroom,
                local_executor=executor,
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(headroom.routing.wait(timeout=1))
                engine.cancel(batch)
                headroom.release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(executor.calls, [])
            self.assertEqual(
                parse_review_status(batch.status_path)[selected.key]["state"],
                "cancelled",
            )

    def test_cancel_between_start_and_submission_prevents_executor_call(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            executor = ImmediateExecutor()
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=executor,
            )
            submit_started = threading.Event()
            release_submit = threading.Event()
            from tui.scheduler import scheduler

            shared_scheduler = scheduler()
            original_submit = shared_scheduler.submit

            def blocked_submit(governor, function, *args, **kwargs):
                submit_started.set()
                release_submit.wait(timeout=5)
                return original_submit(governor, function, *args, **kwargs)

            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ), patch.object(shared_scheduler, "submit", new=blocked_submit):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(submit_started.wait(timeout=1))
                cancel_thread = threading.Thread(
                    target=engine.cancel, args=(batch,)
                )
                cancel_thread.start()
                time.sleep(0.02)
                release_submit.set()
                cancel_thread.join(timeout=1)
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(executor.calls, [])

    def test_cancel_does_not_delete_a_newer_matching_cache_entry(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            cache = ReplacingPutCache(Path(root) / "reviews")
            run = ReviewRun.from_request(
                selected.request(),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
            )
            replacement = ReviewReceipt.from_result(
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
                ["newer receipt"],
                "scope-v7",
            )
            cache.replacement = replacement
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(cache.started.wait(timeout=1))
                engine.cancel(batch)
                cache.release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            cached = cache.get(run, "scope-v7")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.transcript, ("newer receipt",))

    def test_broker_fallback_rechecks_headroom_before_local_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            local = ImmediateExecutor()
            headroom = FakeHeadroomSession(
                [
                    HeadroomRoute(
                        "ACTIVE",
                        "codex",
                        "http://127.0.0.1:8787",
                        "ready",
                    ),
                    HeadroomRoute(
                        "DEGRADED",
                        "codex",
                        None,
                        "readiness probe failed",
                    ),
                ]
            )
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=headroom,
                local_executor=local,
                broker_executor=UnavailableBroker(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot((selected,), {}),
                    "codex",
                    "gpt-5.6-sol",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(headroom.route_calls, ["codex", "codex"])
            self.assertEqual(
                result.results[selected.key].provenance["headroom_state"],
                "DEGRADED",
            )

    def test_cancel_during_fallback_routing_prevents_local_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            local = ImmediateExecutor()
            headroom = BlockingFallbackRouteHeadroom()
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=headroom,
                local_executor=local,
                broker_executor=UnavailableBroker(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "codex",
                    "gpt-5.6-sol",
                    "high",
                    "scope-v7",
                )
                self.assertTrue(headroom.fallback_routing.wait(timeout=1))
                engine.cancel(batch)
                headroom.release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(local.calls, [])
            self.assertEqual(
                parse_review_status(batch.status_path)[selected.key]["state"],
                "cancelled",
            )

    def test_concurrent_fallbacks_keep_route_and_telemetry_in_one_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            local = RecordingExecutor()
            engine = ReviewEngine(
                state_root=root,
                cache=ReviewCache(Path(root) / "reviews"),
                governor=CapacityGovernor(
                    cap=2,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 4,
                ),
                headroom_session=InterleavingHeadroom(),
                local_executor=local,
                broker_executor=UnavailableBroker(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                result = engine.run_sync(
                    BatchSnapshot(
                        (item(1, HEADS[0]), item(2, HEADS[1])), {}
                    ),
                    "codex",
                    "gpt-5.6-sol",
                    "high",
                    "scope-v7",
                )
            self.assertEqual(len(result.results), 2)
            self.assertEqual(
                sorted(local.snapshots),
                [("ACTIVE", "ACTIVE"), ("DEGRADED", "DEGRADED")],
            )

    def test_cancelled_cache_cleanup_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            selected = item(1, HEADS[0])
            cache = CleanupFailingCache(Path(root) / "reviews")
            events = []
            engine = ReviewEngine(
                state_root=root,
                cache=cache,
                governor=CapacityGovernor(
                    cap=1,
                    per_review_budget_mb=1,
                    reserve_mb=1,
                    mem_available_mb=lambda: 100,
                    cpu_count=lambda: 2,
                ),
                headroom_session=FakeHeadroomSession(),
                local_executor=ImmediateExecutor(),
            )
            with patch(
                "tui.review_engine._prepare_worktree",
                side_effect=self.prepared_worktree,
            ):
                batch = engine.start(
                    BatchSnapshot((selected,), {}),
                    "goose",
                    "gemini-3.8-flash",
                    "high",
                    "scope-v7",
                    on_event=events.append,
                )
                self.assertTrue(cache.started.wait(timeout=1))
                engine.cancel(batch)
                cache.release.set()
                deadline = time.monotonic() + 1
                while batch.running and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(batch.running)
            self.assertEqual(events[-1].state, "failed")
            self.assertIn("cache cleanup denied", events[-1].note)

    def test_batch_status_paths_are_reserved_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            engine = ReviewEngine(
                state_root=root,
                headroom_session=FakeHeadroomSession(),
            )
            snapshot = BatchSnapshot((item(1, HEADS[0]),), {})
            telemetry = FakeHeadroomSession().telemetry("goose")
            barrier = threading.Barrier(8)
            batches = []

            def create_batch():
                barrier.wait(timeout=5)
                batches.append(
                    engine._new_batch(
                        snapshot,
                        "goose",
                        "gemini-3.8-flash",
                        "high",
                        telemetry,
                    )
                )

            threads = [threading.Thread(target=create_batch) for _ in range(8)]
            with patch(
                "tui.review_engine.time.strftime",
                return_value="20260906-141432",
            ), patch.object(Path, "exists", return_value=False):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertEqual(len({batch.batch_id for batch in batches}), 8)
            self.assertEqual(len(list(Path(root).glob("*.jsonl"))), 8)


if __name__ == "__main__":
    unittest.main()
