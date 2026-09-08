#!/usr/bin/env python3
"""Contract tests for the passive attended worker-status companion."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))
from tui import worker_status


class WorkerStatusContract(unittest.TestCase):
    def test_read_projections_uses_only_the_four_read_endpoints(self):
        class Response:
            def __init__(self, path):
                self.path = path
                self.headers = type("Headers", (), {"get_content_type": lambda self: "application/json"})()
            def getcode(self):
                return 200
            def read(self, _limit):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False

        class API:
            def __init__(self):
                self.paths = []
            def open(self, request, timeout):
                self.paths.append((request.full_url, request.get_method(), timeout))
                return Response(request.full_url)

        api = API()
        result = worker_status.hive_api.read_projections("https://hive.example", "token", opener=api)
        self.assertTrue(result["ok"])
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _, _ in api.paths], [
            "status", "fleet", "triage", "metrics"
        ])
        self.assertTrue(all(method == "GET" for _, method, _ in api.paths))

    def test_projection_uses_explicit_fields_and_unknown_for_missing_values(self):
        projection = worker_status.project_status(
            {
                "status": {"hub": "online", "actionable_items": 4, "active_contributors": 2},
                "fleet": [{"identity": "worker-7", "state": "working", "current_task": {
                    "repository": "projectbluefin/review", "issue": 151, "title": "Worker status"
                }}],
            },
            identity="worker-7",
        )
        self.assertEqual(projection.identity, "worker-7")
        self.assertEqual(projection.state, "working")
        self.assertEqual(projection.repository, "projectbluefin/review")
        self.assertEqual(projection.issue, "151")
        self.assertEqual(projection.title, "Worker status")
        self.assertEqual(projection.actionable, "4")
        self.assertEqual(projection.contributors, "2")
        self.assertEqual(projection.freshness, "unknown")

        unknown = worker_status.project_status({}, identity="missing")
        self.assertEqual(unknown.state, "unknown")
        self.assertEqual(unknown.repository, "unknown")
        self.assertEqual(unknown.issue, "unknown")

    def test_refresh_deduplicates_inflight_reads_and_backs_off_after_failure(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def read():
            calls.append(1)
            started.set()
            await release.wait()
            raise RuntimeError("offline")

        async def scenario():
            controller = worker_status.RefreshController(read, clock=lambda: 10.0)
            first = controller.refresh()
            await started.wait()
            second = controller.refresh()
            await asyncio.sleep(0)
            self.assertIs(first, second)
            release.set()
            result = await first
            self.assertFalse(result)
            self.assertEqual(calls, [1])
            self.assertFalse(await controller.refresh())
            self.assertEqual(calls, [1])

        asyncio.run(scenario())

    def test_attach_command_uses_the_selected_named_container(self):
        with patch.dict(os.environ, {"REVIEW_CONTAINER_NAME": "review-2"}, clear=False):
            self.assertEqual(
                worker_status.attach_command(),
                "podman exec -it review-2 tmux attach -t contributor",
            )

    def test_render_text_includes_connection_identity_work_and_counts(self):
        projection = worker_status.Projection(
            connection="online", identity="worker-7", state="idle",
            repository="projectbluefin/review", issue="151", title="Worker status",
            actionable="4", contributors="2", freshness="<1m ago",
            attach="podman exec -it review-container tmux attach -t contributor",
        )
        rendered = worker_status.render_text(projection)
        for value in ("online", "worker-7", "idle", "projectbluefin/review", "151",
                      "Worker status", "4", "2", "<1m ago", "podman exec"):
            self.assertIn(value, rendered)


if __name__ == "__main__":
    unittest.main()
