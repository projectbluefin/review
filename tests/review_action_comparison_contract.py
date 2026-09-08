import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui import bluefin_review_tui as tui


HEAD = "7" * 40
OTHER_HEAD = "8" * 40


def review(state="complete", *, repository="projectbluefin/review", number=154, head=HEAD, findings=None):
    findings = findings or []
    counts = {severity: 0 for severity in ("critical", "high", "medium", "low")}
    for finding in findings:
        counts[finding["severity"]] += 1
    return tui.ReviewResult(
        1,
        state,
        counts=counts,
        findings=findings,
        raw_evidence=["evidence " + str(index) for index in range(12)],
        provenance={
            "backend": "goose",
            "model": "gpt-5.6-luna",
            "repository": repository,
            "pull_request": number,
            "head_sha": head,
        },
    )


def classify(result, action, **kwargs):
    head_sha = kwargs.pop("head_sha", HEAD)
    return tui.classify_review_action(
        result,
        action,
        repository="projectbluefin/review",
        number=154,
        head_sha=head_sha,
        actor="maintainer",
        **kwargs,
    )


class ReviewActionComparisonContractTests(unittest.TestCase):
    def test_exact_head_verdicts_record_bounded_evidence_identity_and_category(self):
        result = review(findings=[{"severity": "high", "file": "a.py", "line": 1, "title": "risk"}])
        receipt = classify(result, "request changes")
        self.assertEqual(receipt.classification, "agreement")
        self.assertEqual(receipt.action, "request-changes")
        self.assertEqual(receipt.finding_count, 1)
        self.assertEqual(receipt.identity, "maintainer")
        self.assertEqual(len(receipt.evidence), 4)
        self.assertTrue(all(len(line) <= 240 for line in receipt.evidence))

        self.assertEqual(classify(result, "approved").classification, "disagreement")
        self.assertEqual(classify(review(), "accept").classification, "agreement")

    def test_only_verified_merge_or_queue_completion_can_be_classified(self):
        result = review(findings=[{"severity": "medium", "file": "a.py", "line": 1, "title": "risk"}])
        for action in ("merge", "queue"):
            self.assertEqual(classify(result, action).classification, "unclassified")
        self.assertEqual(
            classify(result, "merge", action_verified=True).classification,
            "disagreement",
        )
        self.assertEqual(classify(result, "queued", action_verified=True).classification, "unclassified")

    def test_successful_acceptance_categories_keep_queue_distinct_from_merge(self):
        result = review()

        queued = classify(result, "approve-and-queue")
        self.assertEqual(queued.classification, "agreement")
        self.assertEqual(queued.action, "approve-and-queue")
        self.assertFalse(queued.verified)

        merged = classify(result, "merge", action_verified=True)
        self.assertEqual(merged.classification, "agreement")
        self.assertEqual(merged.action, "merge")
        self.assertTrue(merged.verified)

        category = tui.ReviewDashboard._action_category
        self.assertEqual(
            category(["python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/154"]),
            "approve-and-queue",
        )

    def test_changed_head_and_invalid_results_are_unclassified(self):
        result = review(findings=[{"severity": "high", "file": "a.py", "line": 1, "title": "risk"}])
        cases = [
            (result, {"head_sha": OTHER_HEAD}),
            (review(repository="projectbluefin/other"), {}),
            (review(number=155), {}),
            (review(head=OTHER_HEAD), {}),
            (review("incomplete"), {}),
            (review("unparsable"), {}),
            (review("failed"), {}),
            (review("cancelled"), {}),
            (result, {"action_success": False}),
            (result, {"action_head": OTHER_HEAD}),
        ]
        for candidate, options in cases:
            self.assertEqual(classify(candidate, "request-changes", **options).classification, "unclassified")

    def test_non_verdict_actions_are_unclassified(self):
        result = review()
        for action in ("comment", "branch-update", "unsupported", "queue", "merge"):
            self.assertEqual(classify(result, action).classification, "unclassified")

    def test_mutation_commands_have_explicit_action_categories(self):
        category = tui.ReviewDashboard._action_category
        self.assertEqual(category(["gh", "pr", "review", "154", "--approve"]), "approve")
        self.assertEqual(category(["gh", "pr", "review", "154", "--request-changes"]), "request-changes")
        self.assertEqual(category(["gh", "pr", "edit", "154", "--add-label", "lgtm"]), "queue")
        self.assertEqual(category(["gh", "pr", "comment", "154"]), "comment")
        self.assertEqual(category(["gh", "pr", "update-branch", "154"]), "branch-update")
        self.assertEqual(category(["gh", "pr", "merge", "154"]), "merge")
        self.assertEqual(category(["printf", "unsafe"]), "unsupported")

    def test_ledger_evicts_old_receipts_and_requires_current_stop_head(self):
        ledger = object.__new__(tui.ReviewDashboard)
        ledger.action_comparisons = {}
        ledger.last_action_comparison = None
        ledger.self_login = "maintainer"
        stop = type("Stop", (), {
            "repository": "projectbluefin/review",
            "number": 154,
            "head_identity": HEAD,
            "review_result": review(),
        })()
        for number in range(tui.MAX_ACTION_RECEIPTS + 1):
            stop.number = number
            stop.review_result = review(number=number)
            ledger.record_action_comparison(stop, ["gh", "pr", "review", str(number), "--approve"])
        self.assertLessEqual(len(ledger.action_comparisons), tui.MAX_ACTION_RECEIPTS)

        stop.number = 154
        stop.head_identity = OTHER_HEAD
        self.assertIsNone(ledger.action_comparison_for(stop))

    def test_aliases_and_finding_counts_remain_bounded(self):
        findings = [
            {"severity": "low", "file": "a.py", "line": index + 1, "title": "risk"}
            for index in range(100)
        ]
        result = review(state="findings", findings=findings)
        for action in ("approve-review", "changes-requested", "request-change"):
            receipt = classify(result, action)
            expected = "disagreement" if action == "approve-review" else "agreement"
            self.assertEqual(receipt.classification, expected)
            self.assertLessEqual(receipt.finding_count, tui.MAX_ACTION_FINDINGS)


if __name__ == "__main__":
    unittest.main()
