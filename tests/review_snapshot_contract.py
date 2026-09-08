# tests/review_snapshot_contract.py
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.review_snapshot import BatchReviewItem, hydrate_batch_snapshot
from tui.bluefin_review_tui import Stop


BASE = "a" * 40
HEAD = "b" * 40


class SnapshotContractTests(unittest.TestCase):
    def stop(self, number):
        return Stop("projectbluefin/review", number, "review", f"PR {number}")

    def test_snapshot_hydrates_exact_base_and_head_for_each_stop(self):
        snapshot = hydrate_batch_snapshot(
            [self.stop(1), self.stop(2)],
            lambda repo, number: {
                "title": f"PR {number}",
                "baseRefOid": BASE,
                "headRefOid": HEAD,
                "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            },
        )
        self.assertTrue(snapshot.ready)
        self.assertEqual([item.number for item in snapshot.items], [1, 2])
        self.assertEqual(snapshot.items[0].request().head_sha, HEAD)

    def test_one_unhydratable_pr_blocks_dispatch_without_discarding_failure_detail(self):
        def fetch(repo, number):
            if number == 2:
                raise RuntimeError("head unavailable")
            return {"baseRefOid": BASE, "headRefOid": HEAD, "statusCheckRollup": []}

        snapshot = hydrate_batch_snapshot([self.stop(1), self.stop(2)], fetch)
        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.failures["projectbluefin/review#2"], "head unavailable")
        self.assertEqual(len(snapshot.items), 1)

    def test_malformed_or_abbreviated_shas_fail_closed(self):
        snapshot = hydrate_batch_snapshot(
            [self.stop(1)],
            lambda repo, number: {"baseRefOid": "a", "headRefOid": HEAD},
        )
        self.assertFalse(snapshot.ready)
        self.assertIn("full", snapshot.failures["projectbluefin/review#1"])

    def test_empty_snapshot_is_not_ready(self):
        snapshot = hydrate_batch_snapshot([], lambda repo, number: {})
        self.assertFalse(snapshot.ready)
        self.assertEqual(len(snapshot.items), 0)
        self.assertEqual(len(snapshot.failures), 0)

    def test_uppercase_or_non_hex_sha_fails_closed(self):
        snapshot = hydrate_batch_snapshot(
            [self.stop(1)],
            lambda repo, number: {"baseRefOid": BASE.upper(), "headRefOid": HEAD},
        )
        self.assertFalse(snapshot.ready)
        self.assertIn("baseRefOid must be a full lowercase SHA", snapshot.failures["projectbluefin/review#1"])

        snapshot2 = hydrate_batch_snapshot(
            [self.stop(1)],
            lambda repo, number: {"baseRefOid": BASE, "headRefOid": "g" * 40},
        )
        self.assertFalse(snapshot2.ready)
        self.assertIn("headRefOid must be a full lowercase SHA", snapshot2.failures["projectbluefin/review#1"])

    def test_verification_status_mapping(self):
        snapshot = hydrate_batch_snapshot(
            [self.stop(1)],
            lambda repo, number: {
                "baseRefOid": BASE,
                "headRefOid": HEAD,
                "statusCheckRollup": [
                    {"name": "lint", "conclusion": "FAILURE"},
                    {"name": "build", "state": "PENDING"},
                    {"context": "deploy", "conclusion": "SKIPPED"},
                    {"conclusion": "ERROR"},
                    "not-a-dict",
                ],
            },
        )
        self.assertTrue(snapshot.ready)
        item = snapshot.items[0]
        self.assertEqual(len(item.verification), 4)
        self.assertEqual(item.verification[0]["state"], "unverified")
        self.assertEqual(item.verification[0]["evidence"], "FAILURE")
        self.assertEqual(item.verification[1]["state"], "pending")
        self.assertEqual(item.verification[1]["evidence"], "PENDING")
        self.assertEqual(item.verification[2]["state"], "verified")
        self.assertEqual(item.verification[2]["name"], "deploy")
        self.assertEqual(item.verification[3]["state"], "unverified")
        self.assertEqual(item.verification[3]["name"], "CI check 4")

    def test_request_construction(self):
        item = BatchReviewItem(
            "projectbluefin/review#1",
            "projectbluefin/review",
            1,
            "PR 1",
            BASE,
            HEAD,
            {},
            [],
        )
        req = item.request(actor="alice", tenant="bluefin")
        self.assertEqual(req.owner, "projectbluefin")
        self.assertEqual(req.repository, "review")
        self.assertEqual(req.pull_request_number, 1)
        self.assertEqual(req.base_sha, BASE)
        self.assertEqual(req.head_sha, HEAD)
        self.assertEqual(req.actor, "alice")
        self.assertEqual(req.tenant, "bluefin")
        self.assertEqual(req.generated_at, "batch-snapshot")

    def test_oserror_from_fetch_callable_fails_closed(self):
        def fetch(repo, number):
            raise OSError("network down")

        snapshot = hydrate_batch_snapshot([self.stop(1)], fetch)
        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.failures["projectbluefin/review#1"], "network down")

    def test_called_process_error_from_fetch_callable_fails_closed(self):
        import subprocess

        def fetch(repo, number):
            raise subprocess.CalledProcessError(1, ["gh", "pr", "view", "1"])

        snapshot = hydrate_batch_snapshot([self.stop(1)], fetch)
        self.assertFalse(snapshot.ready)
        self.assertIn("non-zero exit status 1", snapshot.failures["projectbluefin/review#1"])

    def test_rerun_checks_collapse_to_authoritative_latest(self):
        snapshot = hydrate_batch_snapshot(
            [self.stop(1)],
            lambda repo, number: {
                "baseRefOid": BASE,
                "headRefOid": HEAD,
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "test",
                        "workflowName": "CI",
                        "startedAt": "2026-09-06T10:00:00Z",
                        "completedAt": "2026-09-06T10:05:00Z",
                        "conclusion": "FAILURE",
                    },
                    {
                        "__typename": "CheckRun",
                        "name": "test",
                        "workflowName": "CI",
                        "startedAt": "2026-09-06T10:10:00Z",
                        "completedAt": "2026-09-06T10:15:00Z",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "coverage",
                        "state": "PENDING",
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "coverage",
                        "state": "SUCCESS",
                    },
                ],
            },
        )
        self.assertTrue(snapshot.ready)
        item = snapshot.items[0]
        self.assertEqual(len(item.verification), 2)
        test_check = next(v for v in item.verification if v["name"] == "test")
        self.assertEqual(test_check["state"], "verified")
        self.assertEqual(test_check["evidence"], "SUCCESS")
        cov_check = next(v for v in item.verification if v["name"] == "coverage")
        self.assertEqual(cov_check["state"], "verified")
        self.assertEqual(cov_check["evidence"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
