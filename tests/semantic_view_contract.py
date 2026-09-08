"""Focused contracts for the dashboard's pure semantic view models."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image" / "tui"))

from semantic_view import DecisionState, build_decision_card

from review_result import ReviewResult


class SemanticViewContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
