"""Focused contracts for the dashboard's pure semantic view models."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image" / "tui"))

try:
    import semantic_view as semantic_view_module
    from semantic_view import (
        ACTIONS,
        ActionID,
        DecisionState,
        build_decision_card,
        build_queue_row,
    )
except ModuleNotFoundError:
    semantic_view_module = None
    ACTIONS = None
    ActionID = None
    DecisionState = None
    build_decision_card = None
    build_queue_row = None

build_review_state = getattr(semantic_view_module, "build_review_state", None)


def action_id(value):
    if ActionID is None:
        return None
    return next((item for item in ActionID if item.value == value), None)

from review_result import ReviewResult


@unittest.skipIf(ActionID is None, "semantic view module has not been implemented")
class SemanticViewContractTests(unittest.TestCase):
    def test_registry_has_one_stable_entry_per_action_id(self):
        self.assertEqual(tuple(ACTIONS), tuple(ActionID))
        self.assertEqual(len(ACTIONS), len({action.value for action in ActionID}))
        self.assertEqual(ActionID.APPROVE_AND_QUEUE.value, "approve-and-queue")
        self.assertTrue(ACTIONS[ActionID.APPROVE_AND_QUEUE].mutating)
        self.assertTrue(ACTIONS[ActionID.APPROVE_AND_QUEUE].confirmation_required)
        self.assertFalse(ACTIONS[ActionID.VIEW_DIFF].mutating)
        self.assertFalse(ACTIONS[ActionID.OPEN_BROWSER].ordinary_journey)

    def test_registry_names_human_actions_without_mutation_ambiguity(self):
        values = {action.value for action in ActionID}
        for value in (
            "navigate-up",
            "navigate-down",
            "navigate-first",
            "navigate-last",
            "navigate-page-up",
            "navigate-page-down",
            "pane-next",
            "pane-previous",
            "back",
            "help",
            "open-command-palette",
            "copy-review-context",
            "choose-review-verdict",
            "approve-review",
            "request-changes",
            "comment-review",
            "submit-review",
            "generate-body",
            "edit-body",
            "preview-body",
            "switch-harness",
            "prepare-harness",
            "sign-in-harness",
            "install-harness",
            "retry-harness-detection",
            "harness-diagnostics",
            "start-review",
            "close-pull-request",
        ):
            self.assertIn(value, values)
        for value in ("reject", "leave-review", "handoff", "comment"):
            self.assertNotIn(value, values)

        def action(value):
            return next((item for item in ActionID if item.value == value), None)

        choose_verdict = action("choose-review-verdict")
        approval = action("approve-review")
        request_changes = action("request-changes")
        comment = action("comment-review")
        submit = action("submit-review")
        context = action("copy-review-context")
        navigate_up = action("navigate-up")
        self.assertIsNotNone(approval)
        self.assertIsNotNone(request_changes)
        self.assertIsNotNone(comment)
        self.assertIsNotNone(choose_verdict)
        self.assertIsNotNone(submit)
        self.assertIsNotNone(context)
        self.assertIsNotNone(navigate_up)
        if approval and request_changes and comment and choose_verdict and submit and context and navigate_up:
            self.assertFalse(ACTIONS[choose_verdict].mutating)
            self.assertFalse(ACTIONS[approval].mutating)
            self.assertFalse(ACTIONS[request_changes].mutating)
            self.assertFalse(ACTIONS[comment].mutating)
            self.assertTrue(ACTIONS[submit].mutating)
            self.assertTrue(ACTIONS[submit].confirmation_required)
            self.assertFalse(ACTIONS[context].mutating)
            self.assertTrue(ACTIONS[navigate_up].suspended_in_editor)

    def test_queue_row_preserves_current_snapshot_meaning(self):
        row = build_queue_row({
            "repository": "projectbluefin/review",
            "number": 196,
            "title": "semantic foundation",
            "author": "raptor",
            "head_sha": "a" * 40,
            "mergeable_state": "dirty",
            "check_state": "failure",
            "review_state": "approved",
            "recommended_action": "review",
        })
        self.assertEqual(row.identity, "projectbluefin/review#196")
        self.assertEqual(row.exact_head, "a" * 40)
        self.assertEqual(row.mergeability.label, "CONFLICTS")
        self.assertEqual(row.ci.label, "CI FAILED")
        self.assertEqual(row.review.label, "APPROVED")
        start_review = action_id("start-review")
        self.assertIsNotNone(start_review)
        self.assertEqual(row.primary_action, start_review)

    def test_queue_row_fails_closed_for_unknown_or_invalid_state(self):
        row = build_queue_row({
            "repository": "projectbluefin/review",
            "number": 196,
            "title": "semantic foundation",
            "author": "raptor",
            "head_sha": "not-an-exact-head",
            "mergeable_state": "mystery",
            "check_state": "mystery",
            "review_state": "mystery",
            "recommended_action": "mystery",
        })
        self.assertIsNone(row.exact_head)
        self.assertEqual(row.mergeability.label, "MERGEABILITY UNKNOWN")
        self.assertEqual(row.ci.label, "CI UNKNOWN")
        self.assertEqual(row.review.label, "REVIEW UNKNOWN")
        self.assertIsNone(row.primary_action)

    def test_queue_row_carries_identity_freshness_summary_and_available_actions(self):
        row = build_queue_row({
            "repository": "projectbluefin/review",
            "number": 196,
            "title": "semantic foundation",
            "author": "raptor",
            "tldr": "bind evidence to the reviewed head",
            "head_sha": "b" * 40,
            "reviewed_head_sha": "b" * 40,
            "mergeable_state": "clean",
            "check_state": "success",
            "review_state": "review_required",
            "recommended_action": "review",
            "available_actions": [
                "navigate-up",
                "copy-review-context",
                "approve-review",
                "not-a-real-action",
            ],
        })
        self.assertEqual(getattr(row, "tldr", None), "bind evidence to the reviewed head")
        self.assertEqual(getattr(row, "current_head", None), "b" * 40)
        self.assertEqual(getattr(row, "reviewed_head", None), "b" * 40)
        self.assertEqual(getattr(getattr(row, "freshness", None), "value", None), "current")
        self.assertEqual(
            getattr(row, "available_actions", ()),
            (
                action_id("navigate-up"),
                action_id("copy-review-context"),
                action_id("approve-review"),
            ),
        )

    def test_queue_row_requires_full_equal_heads_for_current_freshness(self):
        reviewed_full = "0123456789ab" + "c" * 28
        current_full = "0123456789ab" + "d" * 28
        row = build_queue_row({
            "repository": "projectbluefin/review",
            "number": 196,
            "title": "semantic foundation",
            "author": "raptor",
            "head_sha": current_full,
            "reviewed_head_sha": reviewed_full[:12],
            "freshness": "current",
        })
        self.assertEqual(row.exact_head, current_full)
        self.assertIsNone(row.reviewed_head)
        self.assertNotEqual(row.freshness.value, "current")

    def test_decision_card_binds_codex_provenance_full_head(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "findings": [{
                "severity": "high",
                "title": "unsafe mutation",
                "file": "image/tui/example.py",
                "line": 7,
            }],
            "verification": [{
                "name": "unit",
                "state": "verified",
                "evidence": "python3 tests/example.py",
            }],
            "provenance": {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "head_sha": "b" * 40,
            },
        })
        card = build_decision_card(result, exact_head="b" * 40)
        self.assertEqual(card.state, DecisionState.FINDINGS)
        self.assertEqual(card.exact_head, "b" * 40)
        self.assertEqual(card.reviewed_head, "b" * 40)
        self.assertEqual(card.freshness.value, "current")
        self.assertEqual(card.provenance.backend, "codex")
        self.assertEqual(card.provenance.model, "gpt-5.6-luna")
        self.assertEqual(card.findings[0].title, "unsafe mutation")
        self.assertEqual(card.verification[0].state, "verified")
        self.assertFalse(card.clean)

    def test_decision_card_carries_live_identity_heads_statuses_and_actions(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "findings": [{
                "severity": "high",
                "title": "unsafe mutation",
                "file": "image/tui/example.py",
                "line": 7,
            }],
            "provenance": {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "head_sha": "b" * 40,
                "reasoning_effort": "high",
            },
            "live": {
                "repository": "projectbluefin/review",
                "number": 196,
                "title": "semantic foundation",
                "tldr": "bind evidence to the reviewed head",
                "mergeable_state": "clean",
                "check_state": "success",
                "review_state": "review_required",
                "available_actions": [
                    "request-changes",
                    "preview-body",
                ],
            },
        })
        card = build_decision_card(result, exact_head="b" * 40)
        self.assertEqual(getattr(card, "repository", None), "projectbluefin/review")
        self.assertEqual(getattr(card, "number", None), 196)
        self.assertEqual(getattr(card, "title", None), "semantic foundation")
        self.assertEqual(getattr(card, "tldr", None), "bind evidence to the reviewed head")
        self.assertEqual(getattr(card, "current_head", None), "b" * 40)
        self.assertEqual(getattr(card, "reviewed_head", None), "b" * 40)
        self.assertEqual(getattr(getattr(card, "freshness", None), "value", None), "current")
        self.assertEqual(getattr(getattr(card, "ci", None), "label", None), "CI GREEN")
        self.assertEqual(
            getattr(getattr(card, "mergeability", None), "label", None),
            "MERGEABLE",
        )
        self.assertEqual(
            getattr(card, "available_actions", ()),
            (
                action_id("request-changes"),
                action_id("preview-body"),
            ),
        )

    def test_decision_card_leads_with_a_complete_maintainer_summary(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "findings",
            "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "findings": [{
                "severity": "high",
                "title": "unsafe mutation",
                "file": "image/tui/example.py",
                "line": 7,
            }],
            "provenance": {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "head_sha": "b" * 40,
            },
            "live": {
                "title": "Make completed reviews decision-first",
                "tldr": "Replace the transcript-first result with a maintainer brief.",
                "mergeable_state": "clean",
                "check_state": "success",
            },
        })

        card = build_decision_card(result, exact_head="b" * 40)

        self.assertEqual(
            card.summary.what_changed,
            "Replace the transcript-first result with a maintainer brief.",
        )
        self.assertEqual(card.summary.risk_impact, "HIGH risk · 1 actionable finding")
        self.assertEqual(card.summary.ci_merge_state, "CI GREEN · MERGEABLE")
        self.assertEqual(
            card.summary.recommended_action,
            "Request changes or comment on the cited finding.",
        )

    def test_decision_card_binds_landed_goose_live_full_head(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
            "provenance": {
                "backend": "goose",
                "model": "gpt-5.6-luna",
                "repository": "projectbluefin/review",
                "pull_request": 196,
            },
            "live": {
                "ci": "success",
                "mergeable": "MERGEABLE",
                "merge_state": "CLEAN",
                "head": "b" * 40,
            },
        })
        card = build_decision_card(result, exact_head="b" * 40)
        self.assertEqual(card.state, DecisionState.CLEAN)
        self.assertEqual(card.exact_head, "b" * 40)
        self.assertEqual(card.reviewed_head, "b" * 40)
        self.assertEqual(card.freshness.value, "current")
        self.assertEqual(card.repository, "projectbluefin/review")
        self.assertEqual(card.number, 196)
        self.assertEqual(card.ci.label, "CI GREEN")
        self.assertEqual(card.mergeability.label, "MERGEABLE")

    def test_decision_card_fails_closed_for_disagreeing_goose_live_head(self):
        reviewed_full = "0123456789ab" + "c" * 28
        current_full = "0123456789ab" + "d" * 28
        for live_head in (reviewed_full[:12], reviewed_full):
            with self.subTest(live_head=live_head):
                result = ReviewResult.from_dict({
                    "version": 1,
                    "state": "complete",
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "findings": [],
                    "provenance": {
                        "backend": "goose",
                        "model": "gpt-5.6-luna",
                        "repository": "projectbluefin/review",
                        "pull_request": 196,
                    },
                    "live": {"head": live_head},
                })
                card = build_decision_card(result, exact_head=current_full)
                self.assertEqual(card.state, DecisionState.STALE)
                self.assertIsNone(card.exact_head)

    def test_decision_card_fails_closed_when_head_sources_disagree(self):
        result = ReviewResult.from_dict({
            "version": 1,
            "state": "complete",
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [],
            "provenance": {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "head_sha": "a" * 40,
            },
            "live": {"head": "b" * 12},
        })
        card = build_decision_card(result, exact_head="b" * 40)
        self.assertEqual(card.state, DecisionState.STALE)
        self.assertIsNone(card.exact_head)
        self.assertEqual(card.freshness.value, "stale")

    def test_review_state_view_owns_lifecycle_states(self):
        self.assertIsNotNone(build_review_state)
        if build_review_state is None:
            return
        for raw, expected in (
            ("READY", DecisionState.READY),
            ("RUNNING", DecisionState.RUNNING),
            ("STALE", DecisionState.STALE),
            ("CANCELLED", DecisionState.CANCELLED),
        ):
            with self.subTest(raw=raw):
                state = build_review_state(raw)
                self.assertEqual(state.state, expected)
                self.assertEqual(state.label, raw)

    def test_decision_card_fails_closed_for_unbound_or_stale_evidence(self):
        for provenance in (
            {"backend": "codex", "model": "gpt-5.6-luna"},
            {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "head_sha": "a" * 40,
            },
        ):
            with self.subTest(provenance=provenance):
                result = ReviewResult.from_dict({
                    "version": 1,
                    "state": "complete",
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "findings": [],
                    "provenance": provenance,
                })
                card = build_decision_card(result, exact_head="b" * 40)
                self.assertEqual(card.state, DecisionState.STALE)
                self.assertIsNone(card.exact_head)
                self.assertFalse(card.clean)

    def test_decision_card_preserves_both_effort_provenance_keys(self):
        for provenance, expected in (
            ({"effort": "medium"}, "medium"),
            ({"reasoning_effort": "high"}, "high"),
        ):
            with self.subTest(provenance=provenance):
                result = ReviewResult.from_dict({
                    "version": 1,
                    "state": "findings",
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "findings": [],
                    "provenance": {
                        "backend": "codex",
                        "model": "gpt-5.6-luna",
                        **provenance,
                        "head_sha": "b" * 40,
                    },
                })
                card = build_decision_card(result, exact_head="b" * 40)
                self.assertEqual(card.provenance.effort, expected)

    def test_decision_card_never_promotes_nonterminal_or_invalid_results(self):
        expected = {
            "incomplete": DecisionState.INCOMPLETE,
            "failed": DecisionState.FAILED,
            "unparsable": DecisionState.UNPARSABLE,
        }
        for raw_state, semantic_state in expected.items():
            with self.subTest(raw_state=raw_state):
                result = ReviewResult(1, raw_state)
                card = build_decision_card(result, exact_head="invalid")
                self.assertEqual(card.state, semantic_state)
                self.assertIsNone(card.exact_head)
                self.assertFalse(card.clean)


class SemanticViewRedTest(unittest.TestCase):
    def test_semantic_view_module_exists(self):
        self.assertIsNotNone(ActionID, "semantic view module must exist")


if __name__ == "__main__":
    unittest.main()
