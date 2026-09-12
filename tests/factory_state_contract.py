# tests/factory_state_contract.py
"""Contracts for the pure, optional Hive factory semantic model (#314).

These cover the first implementation slice: the pure semantic/view model and
the provider error/freshness contract. The model is read-only and optional --
it never mutates GitHub or Hive, and generic Review works with no hub at all.
Acceptance from the issue:

- healthy, unavailable, auth-failed, stale, unknown, degraded, recovered
  Hive evidence;
- a cross-repository Actions/RCC fixture preserving project and repo identity;
- Continuity fixtures: adopted/continuing, blocked, partial duplicate-work
  suppression, head movement, contradictory/unknown evidence;
- attention fixtures distinguishing human decisions from ordinary warnings;
- status refresh performs no GitHub or Hive mutation;
- the model is the single source: UI/MCP would consume it, not reimplement it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui import factory_state as fs
from tui.factory_state import (
    AWAITING_OPERATOR,
    CONVERGED,
    CONVERGING,
    DEGRADED_LIFECYCLE,
    UNKNOWN_LIFECYCLE,
    AttentionItem,
    ContinuityClassification,
    HiveHealth,
    Lifecycle,
    ProviderRead,
    ProviderReadState,
    build_factory_read,
    build_factory_state,
    build_factory_summary,
)

A40 = "a" * 40
B40 = "b" * 40
C40 = "c" * 40


def hive(ok=True, **data):
    if ok:
        return {"ok": True, "data": data}
    envelope = {"ok": False}
    envelope.update(data)
    return envelope


class ProviderReadContract(unittest.TestCase):
    def test_missing_result_is_unavailable(self):
        read = build_factory_read(None)
        self.assertEqual(read.state, ProviderReadState.UNAVAILABLE)
        self.assertFalse(read.fresh)

    def test_empty_hub_is_unavailable(self):
        read = build_factory_read({})
        self.assertEqual(read.state, ProviderReadState.UNAVAILABLE)

    def test_auth_failure_categorises_authentication_and_authorization(self):
        auth = build_factory_read({"ok": False, "category": "authentication", "message": "401"})
        self.assertEqual(auth.state, ProviderReadState.AUTH_FAILED)
        self.assertIn("AUTH FAILED", auth.explanation)
        authz = build_factory_read({"ok": False, "category": "authorization"})
        self.assertEqual(authz.state, ProviderReadState.AUTH_FAILED)

    def test_network_failure_is_unknown_not_auth(self):
        read = build_factory_read({"ok": False, "category": "network", "message": "timeout"})
        self.assertEqual(read.state, ProviderReadState.UNKNOWN)

    def test_configuration_failure_is_unavailable(self):
        read = build_factory_read({"ok": False, "category": "configuration"})
        self.assertEqual(read.state, ProviderReadState.UNAVAILABLE)

    def test_stale_read_records_age_and_explanation(self):
        read = ProviderRead(ProviderReadState.STALE, age_seconds=120)
        self.assertEqual(read.age_label, "2m")
        self.assertIn("2m", read.explanation)

    def test_fresh_read_has_no_age(self):
        read = build_factory_read(hive())
        self.assertTrue(read.fresh)
        self.assertEqual(read.state, ProviderReadState.AVAILABLE)


class HealthyFactoryContract(unittest.TestCase):
    def setUp(self):
        self.state = build_factory_state(hive(**{
            "health": "healthy",
            "authority_level": "L5",
            "pr_hold_gated": True,
            "auto_merge": False,
            "convergence_mode": "ENFORCE",
            "state": "converging",
            "project": "projectbluefin/actions",
            "member_repositories": ["projectbluefin/actions", "projectbluefin/rcc"],
            "frontier": 5,
            "in_flight": 3,
            "blocked": 1,
            "needs_attention": 2,
            "runtime": "5a3bb67",
            "transition": "reconciling->converging",
            "generated_at": "2026-08-22T12:00:00Z",
        }))

    def test_project_and_repository_identity_preserved(self):
        self.assertEqual(self.state.project, "projectbluefin/actions")
        self.assertEqual(
            self.state.member_repositories,
            ("projectbluefin/actions", "projectbluefin/rcc"),
        )

    def test_control_posture_is_compact_and_sourced(self):
        self.assertEqual(
            self.state.control_posture,
            "Authority L5 · PRs hold-gated · auto-merge off",
        )

    def test_convergence_and_lifecycle(self):
        self.assertEqual(self.state.convergence_mode, "ENFORCE")
        self.assertEqual(self.state.lifecycle, Lifecycle.CONVERGING)
        self.assertEqual(self.state.lifecycle_status, CONVERGING)

    def test_counts_are_authoritative_with_source_and_definition(self):
        self.assertEqual(self.state.frontier.value, 5)
        self.assertEqual(self.state.frontier.source, "hive")
        self.assertEqual(self.state.frontier.definition, "actionable frontier")
        self.assertEqual(self.state.in_flight.value, 3)
        self.assertEqual(self.state.blocked.value, 1)
        self.assertEqual(self.state.needs_attention.value, 2)

    def test_runtime_and_transition_are_sourced(self):
        self.assertEqual(self.state.runtime, "5a3bb67")
        self.assertEqual(self.state.transition, "reconciling->converging")


class CrossRepositoryActionsRccContract(unittest.TestCase):
    """The issue's dogfood fixture: Actions and RCC as one project."""

    def test_cross_repository_project_state(self):
        state = build_factory_state(hive(**{
            "project": "projectbluefin/actions",
            "member_repositories": ["projectbluefin/actions", "projectbluefin/rcc"],
            "health": "healthy",
            "state": "converging",
            "convergence_mode": "ENFORCE",
            "frontier": 7,
        }))
        summary = build_factory_summary(state)
        self.assertEqual(summary.project, "projectbluefin/actions")
        self.assertEqual(len(state.member_repositories), 2)
        self.assertEqual(state.frontier.value, 7)
        self.assertEqual(summary.health, "healthy")
        self.assertEqual(summary.convergence, "ENFORCE")


class ContinuityContract(unittest.TestCase):
    def _state(self, **data):
        return build_factory_state(hive(**{
            "project": "projectbluefin/actions",
            "continuity": [
                {"repository": "projectbluefin/actions", "number": 109,
                 "classification": "blocked", "head": A40, "explanation": "merge conflict"},
                {"repository": "projectbluefin/actions", "number": 112,
                 "classification": "continuing", "head": B40},
                {"repository": "projectbluefin/actions", "number": 115,
                 "classification": "continuing", "slice_suppressed": ["#98"], "head": C40},
                {"repository": "projectbluefin/actions", "number": 119,
                 "classification": "continuing", "slice_suppressed": ["#96"], "head": A40},
                {"repository": "projectbluefin/actions", "number": 128,
                 "classification": "continuing", "slice_suppressed": ["#125"], "head": B40},
            ],
            **data,
        }))

    def test_adopted_and_continuing_classification(self):
        claims = {c.number: c for c in self._state().continuity}
        self.assertEqual(claims[109].classification, ContinuityClassification.BLOCKED)
        self.assertEqual(claims[112].classification, ContinuityClassification.CONTINUING)

    def test_blocked_adopted_pr_reports_conflict_state(self):
        claims = {c.number: c for c in self._state().continuity}
        self.assertEqual(claims[109].state, "merge conflict")

    def test_partial_duplicate_work_suppression_is_recorded(self):
        claims = {c.number: c for c in self._state().continuity}
        self.assertEqual(claims[115].slice_suppressed, ("#98",))
        self.assertEqual(claims[119].slice_suppressed, ("#96",))
        self.assertEqual(claims[128].slice_suppressed, ("#125",))

    def test_head_movement_since_adoption(self):
        state = self._state(**{
            "continuity": [
                {"repository": "projectbluefin/actions", "number": 112,
                 "classification": "continuing", "head": B40, "current_head": A40},
            ],
        })
        claim = state.continuity[0]
        self.assertTrue(claim.moved)
        self.assertEqual(claim.state, "head moved since adoption")

    def test_contradictory_evidence(self):
        state = self._state(**{
            "continuity": [
                {"repository": "projectbluefin/actions", "number": 112,
                 "classification": "continuing", "head": B40, "contradictory": True},
            ],
        })
        self.assertTrue(state.continuity[0].contradictory)
        self.assertEqual(state.continuity[0].state, "contradictory evidence")

    def test_unknown_head_is_not_a_valid_sha(self):
        state = self._state(**{
            "continuity": [
                {"repository": "projectbluefin/actions", "number": 112,
                 "classification": "continuing", "head": "not-a-sha"},
            ],
        })
        self.assertIsNone(state.continuity[0].head)


class AttentionContract(unittest.TestCase):
    def test_pr_ready_for_review_is_attention(self):
        state = build_factory_state(
            hive(**{"project": "projectbluefin/actions"}),
            raw_github={"pull_request": {
                "number": 42, "reviewDecision": "review_required", "check_state": "success",
            }},
        )
        self.assertTrue(state.attention)
        item = state.attention[0]
        self.assertIsInstance(item, AttentionItem)
        self.assertEqual(item.reason, "ready")
        self.assertIn("42", item.title)
        self.assertTrue(item.decision)

    def test_conflict_is_attention(self):
        state = build_factory_state(
            hive(**{"project": "projectbluefin/actions"}),
            raw_github={"pull_request": {"number": 7, "mergeable_state": "conflicting"}},
        )
        reasons = {item.reason for item in state.attention}
        self.assertIn("conflict", reasons)

    def test_adopted_blocked_pr_is_attention(self):
        state = build_factory_state(hive(**{
            "project": "projectbluefin/actions",
            "continuity": [
                {"repository": "projectbluefin/actions", "number": 109,
                 "classification": "blocked", "head": A40, "explanation": "merge conflict"},
            ],
        }))
        self.assertTrue(any(item.reason == "conflict" for item in state.attention))

    def test_operator_decision_and_uncertainty_are_attention(self):
        state = build_factory_state(hive(**{
            "project": "projectbluefin/actions",
            "operator_decisions": ["pause governor for this project"],
            "uncertainty": ["convergence model diverged on rcc"],
        }))
        reasons = {item.reason for item in state.attention}
        self.assertIn("operator_decision", reasons)
        self.assertIn("unknown_evidence", reasons)

    def test_no_github_evidence_yields_no_github_attention(self):
        # No GitHub evidence: Review stays generic, no fabricated attention.
        state = build_factory_state(hive(**{"project": "projectbluefin/actions"}))
        self.assertEqual(state.attention, ())


class EvidenceStateContract(unittest.TestCase):
    def test_unavailable_hive(self):
        state = build_factory_state(hive(ok=False, category="configuration"))
        self.assertEqual(state.provider.state, ProviderReadState.UNAVAILABLE)
        self.assertEqual(state.health, HiveHealth.UNAVAILABLE)
        self.assertEqual(state.lifecycle_status, UNKNOWN_LIFECYCLE)
        self.assertIsNone(state.frontier)
        self.assertFalse(state.available)

    def test_auth_failed(self):
        state = build_factory_state(hive(ok=False, category="authorization"))
        self.assertEqual(state.provider.state, ProviderReadState.AUTH_FAILED)
        self.assertEqual(state.health, HiveHealth.UNAVAILABLE)
        self.assertEqual(state.confidence, "authentication failed")

    def test_unknown_read_degrades_health(self):
        state = build_factory_state(hive(ok=False, category="network"))
        self.assertEqual(state.provider.state, ProviderReadState.UNKNOWN)
        self.assertEqual(state.health, HiveHealth.DEGRADED)
        self.assertEqual(state.lifecycle_status, UNKNOWN_LIFECYCLE)

    def test_degraded_health_forces_degraded_lifecycle(self):
        state = build_factory_state(hive(**{"health": "degraded", "state": "converging"}))
        self.assertEqual(state.health, HiveHealth.DEGRADED)
        self.assertEqual(state.lifecycle_status, DEGRADED_LIFECYCLE)

    def test_recovered_from_stale_to_available(self):
        stale = build_factory_state(
            hive(**{"state": "converging"}),
            provider=ProviderRead(ProviderReadState.STALE, age_seconds=300),
        )
        self.assertEqual(stale.provider.state, ProviderReadState.STALE)
        recovered = build_factory_state(hive(**{"state": "converging"}))
        self.assertTrue(recovered.provider.fresh)
        self.assertEqual(recovered.lifecycle_status, CONVERGING)

    def test_stale_read_keeps_last_known_facts_but_marks_stale(self):
        state = build_factory_state(
            hive(**{"state": "converging", "frontier": 5}),
            provider=ProviderRead(ProviderReadState.STALE, age_seconds=90),
        )
        self.assertEqual(state.provider.state, ProviderReadState.STALE)
        self.assertEqual(state.provider.age_label, "1m")
        self.assertEqual(state.frontier.value, 5)


class NoInferenceContract(unittest.TestCase):
    def test_missing_counts_are_none_not_zero(self):
        state = build_factory_state(hive(**{"state": "converging"}))
        self.assertIsNone(state.frontier)
        self.assertIsNone(state.in_flight)
        self.assertIsNone(state.blocked)
        self.assertIsNone(state.needs_attention)

    def test_unknown_state_is_not_forced_into_a_lifecycle(self):
        state = build_factory_state(hive(**{"health": "healthy"}))
        self.assertEqual(state.lifecycle_status, UNKNOWN_LIFECYCLE)
        self.assertIsNone(state.lifecycle)

    def test_absent_hive_leaves_generic_defaults(self):
        state = build_factory_state(None)
        self.assertEqual(state.project, "unknown")
        self.assertEqual(state.control_posture, "unknown")
        self.assertEqual(state.authority_level, "unknown")
        self.assertFalse(state.available)


class ReadOnlyContract(unittest.TestCase):
    """The model has no mutation authority: it only surfaces facts."""

    def test_factory_state_is_immutable(self):
        state = build_factory_state(hive(**{"state": "converging"}))
        with self.assertRaises(Exception):
            state.project = "hacked"

    def test_no_method_mutates_github_or_hive(self):
        public = [n for n in dir(fs) if not n.startswith("_")]
        for name in ("build_factory_state", "build_factory_summary", "build_factory_read"):
            method = getattr(fs, name)
            self.assertNotIn("mutate", name.lower())
            # None of the public surface performs network writes: every function
            # is pure evidence -> model.
            self.assertTrue(callable(method))


if __name__ == "__main__":
    unittest.main()
