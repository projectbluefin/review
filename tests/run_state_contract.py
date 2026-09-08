# tests/run_state_contract.py
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "image"))

from tui.run_state import (
    IllegalRunTransition,
    RunIdentity,
    RunState,
    RunStateStore,
    TerminalOutcome,
)


def _sha(char: str) -> str:
    return char * 40


def _identity(number: int, head: str | None = None) -> RunIdentity:
    return RunIdentity(
        repository="projectbluefin/review",
        pull_request=number,
        base_sha=_sha("a"),
        head_sha=head or f"{number:040x}"[-40:],
        backend="goose",
        model="gpt-5.6-sol",
        effort="high",
        check_scope_version="checks-v1",
    )


class RunStateContractTests(unittest.TestCase):
    def _store_dir(self):
        scratch = Path(__file__).parents[1] / ".cache" / "run-state-contract"
        scratch.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=scratch)

    def test_untrustworthy_review_results_are_distinct_terminal_states(self):
        cases = {
            RunState.REVIEW_MISSING: TerminalOutcome.REVIEW_MISSING,
            RunState.REVIEW_FAILED: TerminalOutcome.REVIEW_FAILED,
            RunState.REVIEW_INCOMPLETE: TerminalOutcome.REVIEW_INCOMPLETE,
            RunState.REVIEW_UNPARSABLE: TerminalOutcome.REVIEW_UNPARSABLE,
        }
        with self._store_dir() as root:
            store = RunStateStore(root)
            seen_states = set()
            for offset, (state, outcome) in enumerate(cases.items(), start=1):
                identity = _identity(offset)
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)

                record = store.transition(identity, state, reason=outcome.value)

                seen_states.add(record.state)
                self.assertEqual(record.terminal_outcome, outcome)
                self.assertTrue(record.is_terminal)
                self.assertFalse(record.may_mutate())

            self.assertEqual(seen_states, set(cases))

    def test_clean_review_may_reach_mutation(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)

            record = store.transition(identity, RunState.REVIEW_CLEAN)

            self.assertEqual(record.state, RunState.REVIEW_CLEAN)
            self.assertIsNone(record.terminal_outcome)
            self.assertTrue(record.may_mutate())
            self.assertTrue(store.may_mutate(identity))

    def test_identity_round_trips_through_persistence(self):
        with self._store_dir() as root:
            identity = RunIdentity(
                repository="projectbluefin/review",
                pull_request=409,
                base_sha=_sha("1"),
                head_sha=_sha("2"),
                backend="codex",
                model="gpt-5.6-terra",
                effort="xhigh",
                check_scope_version="2026-09-07",
            )
            store = RunStateStore(root)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)

            reopened = RunStateStore(root).get(identity)

            self.assertIsNotNone(reopened)
            self.assertEqual(reopened.identity, identity)

    def test_head_change_drives_distinct_terminal_state(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1, _sha("b"))
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)

            unchanged = store.revalidate_head(identity, _sha("b"))
            changed = store.revalidate_head(identity, _sha("c"))

            self.assertEqual(unchanged.state, RunState.REVIEW_CLEAN)
            self.assertEqual(changed.state, RunState.HEAD_CHANGED)
            self.assertEqual(changed.terminal_outcome, TerminalOutcome.HEAD_CHANGED)
            self.assertFalse(changed.may_mutate())

    def test_illegal_transition_raises_and_terminal_is_terminal(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)

            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.REVIEW_CLEAN)

            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_MISSING)

            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.REVIEWING)

    def test_records_survive_restart(self):
        with self._store_dir() as root:
            identity = _identity(1)
            store = RunStateStore(root)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)
            del store

            record = RunStateStore(root).get(identity)

            self.assertIsNotNone(record)
            self.assertEqual(record.state, RunState.REVIEW_CLEAN)
            self.assertTrue(record.may_mutate())

    def test_prune_retains_newest_records_within_bound(self):
        with self._store_dir() as root:
            store = RunStateStore(root, max_records=10)
            for number in range(30):
                identity = _identity(number + 1)
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)
                store.transition(identity, RunState.REVIEW_MISSING)

            retained = store.prune()

            self.assertLessEqual(len(retained), 10)
            self.assertEqual(
                [record.number for record in retained],
                list(range(21, 31)),
            )

    def test_concurrent_writers_do_not_corrupt_or_lose_records(self):
        with self._store_dir() as root:
            store = RunStateStore(root, max_records=200)

            def write(number: int) -> None:
                identity = _identity(number)
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)
                store.transition(identity, RunState.REVIEW_CLEAN)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(1, 81)))

            records = RunStateStore(root, max_records=200).records()

            self.assertEqual(len(records), 80)
            self.assertEqual(
                {record.identity.pull_request for record in records},
                set(range(1, 81)),
            )
            self.assertTrue(all(record.state == RunState.REVIEW_CLEAN for record in records))

    def test_early_return_state_does_not_leak_in_flight(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            reviewing = store.transition(identity, RunState.REVIEWING)

            blocked = store.transition(
                identity,
                RunState.BLOCKED,
                reason="already being reviewed",
            )

            self.assertTrue(reviewing.in_flight)
            self.assertFalse(blocked.in_flight)
            self.assertFalse(RunStateStore(root).get(identity).in_flight)

    def test_in_flight_context_manager_cleans_up_on_early_return(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)

            def early_return_workflow():
                with store.in_flight(identity, RunState.REVIEWING):
                    # Simulate early return like 4a61f1c
                    return "aborted early"

            result = early_return_workflow()
            self.assertEqual(result, "aborted early")
            record = store.get(identity)
            self.assertIsNotNone(record)
            self.assertFalse(record.in_flight)
            self.assertEqual(record.state, RunState.REVIEW_INCOMPLETE)
            self.assertEqual(record.terminal_outcome, TerminalOutcome.REVIEW_INCOMPLETE)

    def test_record_exposes_full_identity_and_status_properties(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = RunIdentity(
                repository="projectbluefin/review",
                pull_request=410,
                base_sha=_sha("1"),
                head_sha=_sha("2"),
                backend="codex",
                model="gpt-5.6-terra",
                effort="high",
                check_scope_version="checks-v2",
            )
            record = store.create(identity)

            self.assertEqual(record.repository, "projectbluefin/review")
            self.assertEqual(record.number, 410)
            self.assertEqual(record.base_sha, _sha("1"))
            self.assertEqual(record.head_sha, _sha("2"))
            self.assertEqual(record.backend, "codex")
            self.assertEqual(record.model, "gpt-5.6-terra")
            self.assertEqual(record.effort, "high")
            self.assertEqual(record.check_scope_version, "checks-v2")
            self.assertFalse(record.is_terminal)

            clean = store.transition(identity, RunState.REVIEWING)
            clean = store.transition(identity, RunState.REVIEW_CLEAN)
            self.assertTrue(clean.may_mutate())
            self.assertTrue(store.may_mutate(identity))

    def test_blocked_and_retry_at_carry_explicit_values(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)

            with self.assertRaises(ValueError):
                store.transition(identity, RunState.RETRY_AT)

            retry_time = "2026-09-07T14:00:00Z"
            retrying = store.transition(
                identity,
                RunState.RETRY_AT,
                retry_at=retry_time,
                reason="rate limit backoff",
            )
            self.assertEqual(retrying.state, RunState.RETRY_AT)
            self.assertEqual(retrying.retry_at, retry_time)
            self.assertEqual(retrying.reason, "rate limit backoff")
            self.assertFalse(retrying.may_mutate())

            unblocked = store.transition(
                identity,
                RunState.BLOCKED,
                reason="circuit breaker open: github api",
            )
            self.assertEqual(unblocked.state, RunState.BLOCKED)
            self.assertEqual(unblocked.reason, "circuit breaker open: github api")
            self.assertEqual(unblocked.retry_at, "")

    def test_blocked_cannot_arbitrarily_jump_to_mutating(self):
        with self._store_dir() as root:
            store = RunStateStore(root)

            # 1. PENDING -> BLOCKED -> MUTATING must raise
            id1 = _identity(1)
            store.create(id1)
            store.transition(id1, RunState.BLOCKED, reason="github rate limit")
            with self.assertRaises(IllegalRunTransition):
                store.transition(id1, RunState.MUTATING)

            # 2. REVIEW_CLEAN -> BLOCKED -> REVIEW_CLEAN is allowed
            id2 = _identity(2)
            store.create(id2)
            store.transition(id2, RunState.REVIEWING)
            store.transition(id2, RunState.REVIEW_CLEAN)
            store.transition(id2, RunState.BLOCKED, reason="merge lock")
            resumed = store.transition(id2, RunState.REVIEW_CLEAN)
            self.assertEqual(resumed.state, RunState.REVIEW_CLEAN)
            self.assertTrue(resumed.may_mutate())

            # 3. REVIEW_CLEAN -> BLOCKED -> MUTATING must raise (must resume to REVIEW_CLEAN first)
            id3 = _identity(3)
            store.create(id3)
            store.transition(id3, RunState.REVIEWING)
            store.transition(id3, RunState.REVIEW_CLEAN)
            store.transition(id3, RunState.BLOCKED, reason="merge lock")
            with self.assertRaises(IllegalRunTransition):
                store.transition(id3, RunState.MUTATING)

    def test_blocked_preserves_resume_state_across_retry_at(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)

            # REVIEW_CLEAN -> BLOCKED -> RETRY_AT -> REVIEW_CLEAN
            store.transition(identity, RunState.BLOCKED, reason="429 rate limit")
            store.transition(
                identity,
                RunState.RETRY_AT,
                retry_at="2026-09-07T13:00:00Z",
                reason="backoff scheduled",
            )
            # Cannot jump directly to MUTATING from RETRY_AT
            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.MUTATING)

            # Must resume to REVIEW_CLEAN
            resumed = store.transition(identity, RunState.REVIEW_CLEAN)
            self.assertEqual(resumed.state, RunState.REVIEW_CLEAN)

    def test_mutation_failed_is_distinct_terminal_state(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)
            store.transition(identity, RunState.MUTATING)

            # MUTATING -> REVIEW_FAILED must raise (mutation failure is not review failure)
            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.REVIEW_FAILED)

            # MUTATING -> MUTATION_FAILED is distinct terminal state
            failed = store.transition(
                identity,
                RunState.MUTATION_FAILED,
                reason="squash merge failed: conflict",
            )
            self.assertEqual(failed.state, RunState.MUTATION_FAILED)
            self.assertEqual(failed.terminal_outcome, TerminalOutcome.MUTATION_FAILED)
            self.assertTrue(failed.is_terminal)
            self.assertFalse(failed.may_mutate())

    def test_re_running_terminal_run_raises_illegal_run_transition(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)
            store.transition(identity, RunState.MUTATING)
            store.transition(identity, RunState.COMPLETED)

            # Run is terminal: calling transition raises IllegalRunTransition
            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.REVIEWING)

            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.MUTATING)

    def test_resume_state_round_trips_through_persistence(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)
            store.transition(identity, RunState.BLOCKED, reason="wait for tests")

            # Reopen store from same path
            reopened = RunStateStore(root).get(identity)
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened.state, RunState.BLOCKED)
            self.assertEqual(reopened.resume_state, RunState.REVIEW_CLEAN)

    def test_prune_preserves_in_flight_records(self):
        with self._store_dir() as root:
            store = RunStateStore(root, max_records=5)
            # Create 2 in-flight runs
            active1 = _identity(101)
            active2 = _identity(102)
            store.create(active1)
            store.transition(active1, RunState.REVIEWING)
            store.create(active2)
            store.transition(active2, RunState.REVIEWING)

            # Create 10 terminal runs
            for number in range(1, 11):
                identity = _identity(number)
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)
                store.transition(identity, RunState.REVIEW_MISSING)

            retained = store.prune()
            # Active runs must still be present
            self.assertIsNotNone(store.get(active1))
            self.assertIsNotNone(store.get(active2))
            retained_prs = {r.number for r in retained}
            self.assertIn(101, retained_prs)
            self.assertIn(102, retained_prs)
            self.assertLessEqual(len(retained), 5)

    def test_lookup_by_pull_request(self):
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(409)
            store.create(identity)
            found = store.get_by_pr("projectbluefin/review", 409)
            self.assertIsNotNone(found)
            self.assertEqual(found.identity, identity)
            self.assertIsNone(store.get_by_pr("projectbluefin/review", 999))
    def test_human_review_refusal_is_distinct_from_a_failed_mutation(self):
        with self._store_dir() as root:
            store = RunStateStore(root)

            def landed_to(state):
                identity = _identity(414 + list(RunState).index(state))
                store.create(identity)
                store.transition(identity, RunState.REVIEWING)
                store.transition(identity, RunState.REVIEW_CLEAN)
                store.transition(identity, RunState.MUTATING)
                return store.transition(identity, state, reason="gate")

            refused = landed_to(RunState.HUMAN_REVIEW_MISSING)
            broke = landed_to(RunState.MUTATION_FAILED)

            self.assertNotEqual(refused.state, broke.state)
            self.assertNotEqual(refused.terminal_outcome, broke.terminal_outcome)
            self.assertEqual(
                refused.terminal_outcome, TerminalOutcome.HUMAN_REVIEW_MISSING
            )
            for record in (refused, broke):
                self.assertTrue(record.is_terminal)
                self.assertFalse(record.may_mutate())

    def test_escalation_required_and_re_reviewing_transitions(self):
        """#411: ESCALATION_REQUIRED and RE_REVIEWING allow re-review but forbid direct mutation."""
        with self._store_dir() as root:
            store = RunStateStore(root)
            identity = _identity(1)
            store.create(identity)
            store.transition(identity, RunState.REVIEWING)
            store.transition(identity, RunState.REVIEW_CLEAN)

            # REVIEW_CLEAN -> ESCALATION_REQUIRED
            escalating = store.transition(identity, RunState.ESCALATION_REQUIRED, reason="needs strong review")
            self.assertEqual(escalating.state, RunState.ESCALATION_REQUIRED)
            self.assertFalse(escalating.is_terminal)
            self.assertFalse(escalating.may_mutate())

            # ESCALATION_REQUIRED cannot transition directly to MUTATING
            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.MUTATING)

            # ESCALATION_REQUIRED -> RE_REVIEWING
            re_reviewing = store.transition(identity, RunState.RE_REVIEWING)
            self.assertEqual(re_reviewing.state, RunState.RE_REVIEWING)
            self.assertTrue(re_reviewing.in_flight)
            self.assertFalse(re_reviewing.may_mutate())

            # RE_REVIEWING cannot transition directly to MUTATING
            with self.assertRaises(IllegalRunTransition):
                store.transition(identity, RunState.MUTATING)

            # RE_REVIEWING -> REVIEW_CLEAN -> MUTATING (with high-assurance identity)
            clean = store.transition(identity, RunState.REVIEW_CLEAN)
            self.assertEqual(clean.state, RunState.REVIEW_CLEAN)
            self.assertTrue(clean.may_mutate())

            mutating = store.transition(identity, RunState.MUTATING)
            self.assertEqual(mutating.state, RunState.MUTATING)

    def test_transition_to_mutating_enforces_high_assurance_or_low_risk(self):
        """#411: cheap model clean verdict cannot authorise merge unless low-risk."""
        with self._store_dir() as root:
            store = RunStateStore(root)
            cheap_identity = RunIdentity(
                repository="projectbluefin/review",
                pull_request=411,
                base_sha=_sha("a"),
                head_sha=_sha("b"),
                backend="goose",
                model="gemini-3.8-flash",
                effort="high",
                check_scope_version="checks-v1",
            )
            store.create(cheap_identity)
            store.transition(cheap_identity, RunState.REVIEWING)
            store.transition(cheap_identity, RunState.REVIEW_CLEAN)

            # Attempting to mutate cheap model without low-risk flag raises
            with self.assertRaises(IllegalRunTransition):
                store.transition(cheap_identity, RunState.MUTATING)

            # Low-risk flag allows cheap model to mutate
            mutating = store.transition(cheap_identity, RunState.MUTATING, low_risk=True)
            self.assertEqual(mutating.state, RunState.MUTATING)


if __name__ == "__main__":
    unittest.main()
