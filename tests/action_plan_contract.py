"""Focused contract tests for the shared exact-head ActionPlan."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


TUI_ROOT = Path(__file__).resolve().parents[1] / "image" / "tui"
if str(TUI_ROOT) not in sys.path:
    sys.path.insert(0, str(TUI_ROOT))

try:
    import action_plan as contract
except ModuleNotFoundError:
    contract = None


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(minutes=10)
REPOSITORY = "projectbluefin/review"
PULL_REQUEST = 184
HEAD = "a" * 40
ACTOR = "maintainer"
TENANT = "projectbluefin"


def operation(*args: str):
    if contract is None:
        return None
    return contract.GitHubOperation.from_argv(args)


def prerequisites(
    permissions: dict[str, bool] | None = None,
    checks: dict[str, str] | None = None,
):
    if contract is None:
        return None
    return contract.Prerequisites.from_mappings(
        permissions=permissions or {"push": True},
        checks=checks or {"ci": "success"},
    )


def make_plan(
    *,
    body: str | None = "Reviewed exactly.\n",
    operations=None,
    action_kind: str = "review",
    plan_head: str = HEAD,
    plan_prerequisites=None,
    expires_at: datetime = EXPIRES,
):
    if contract is None:
        return None
    operations = operations or (
        operation(
            "gh",
            "pr",
            "review",
            str(PULL_REQUEST),
            "--repo",
            REPOSITORY,
            "--approve",
            "--body",
            body or "",
        ),
    )
    return contract.ActionPlan.build(
        actor=ACTOR,
        tenant=TENANT,
        repository=REPOSITORY,
        pull_request=PULL_REQUEST,
        head_sha=plan_head,
        action_kind=action_kind,
        body=body,
        operations=operations,
        prerequisites=plan_prerequisites or prerequisites(),
        created_at=NOW,
        expires_at=expires_at,
        idempotency_key="issue-184-test-plan",
    )


def current_state(
    *,
    actor: str = ACTOR,
    tenant: str = TENANT,
    head_sha: str = HEAD,
    body: str | None = "Reviewed exactly.\n",
    permissions: dict[str, bool] | None = None,
    checks: dict[str, str] | None = None,
):
    if contract is None:
        return None
    return contract.CurrentState.capture(
        actor=actor,
        tenant=tenant,
        repository=REPOSITORY,
        pull_request=PULL_REQUEST,
        head_sha=head_sha,
        body=body,
        prerequisites=prerequisites(permissions, checks),
    )


class TestReceiptLedger:
    def __init__(self):
        self.claimed = set()
        self.receipts = []

    def claim(self, idempotency_key):
        if idempotency_key in self.claimed:
            return False
        self.claimed.add(idempotency_key)
        return True

    def record(self, receipt):
        self.receipts.append(receipt)


class ActionPlanContractTests(unittest.TestCase):
    def require_contract(self):
        if contract is None:
            self.fail("the shared ActionPlan contract module must exist")
        return contract

    def test_shared_action_plan_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("action_plan"),
            "the shared ActionPlan contract module must exist",
        )

    def test_plan_is_immutable_and_hash_binds_exact_body_and_operations(self) -> None:
        module = self.require_contract()
        plan = make_plan()

        self.assertEqual(plan.identity, plan.plan_hash)
        self.assertEqual(plan.operations[0].argv[-1], "Reviewed exactly.\n")
        self.assertEqual(make_plan().identity, plan.identity)
        self.assertNotEqual(
            make_plan(body="Reviewed exactly.\n ").identity,
            plan.identity,
        )
        self.assertNotEqual(
            make_plan(
                operations=(
                    operation(
                        "gh",
                        "pr",
                        "review",
                        str(PULL_REQUEST),
                        "--repo",
                        REPOSITORY,
                        "--approve",
                        "--body",
                        "Reviewed exactly.\n",
                    ),
                    operation(
                        "gh",
                        "pr",
                        "edit",
                        str(PULL_REQUEST),
                        "--repo",
                        REPOSITORY,
                        "--add-label",
                        "lgtm",
                    ),
                ),
            ).identity,
            plan.identity,
        )
        with self.assertRaises((AttributeError, module.FrozenInstanceError)):
            plan.head_sha = "b" * 40

    def test_plan_requires_full_head_and_safe_exact_operations(self) -> None:
        module = self.require_contract()
        with self.assertRaises(module.InvalidPlanError):
            make_plan(plan_head="a" * 39)
        with self.assertRaises(module.InvalidPlanError):
            make_plan(action_kind="invented-mutation")
        with self.assertRaises(module.InvalidPlanError):
            make_plan(
                operations=(
                    operation(
                        "gh",
                        "pr",
                        "merge",
                        str(PULL_REQUEST + 1),
                        "--repo",
                        REPOSITORY,
                    ),
                ),
            )
        with self.assertRaises(module.InvalidPlanError):
            make_plan(
                operations=(
                    operation(
                        "gh",
                        "pr",
                        "merge",
                        str(PULL_REQUEST),
                        "--repo",
                        REPOSITORY,
                        "--auto",
                    ),
                ),
            )
        with self.assertRaises(module.InvalidPlanError):
            make_plan(
                operations=(
                    operation(
                        "gh",
                        "pr",
                        "review",
                        str(PULL_REQUEST),
                        "--repo",
                        REPOSITORY,
                        "--approve",
                        "--body-file",
                        "/tmp/review.md",
                    ),
                ),
            )
        with self.assertRaises(module.InvalidPlanError):
            make_plan(
                operations=(
                    operation(
                        "gh",
                        "pr",
                        "merge",
                        str(PULL_REQUEST),
                        "--repo",
                        REPOSITORY,
                        "--admin",
                    ),
                ),
            )

    def test_preview_exposes_exact_intent_without_authority(self) -> None:
        self.require_contract()
        plan = make_plan()
        preview = plan.preview()

        self.assertEqual(preview.plan_identity, plan.identity)
        self.assertEqual(preview.body, "Reviewed exactly.\n")
        self.assertEqual(preview.operations, plan.operations)
        self.assertFalse(hasattr(preview, "confirmation"))

    def test_model_only_preview_cannot_authorize_execution(self) -> None:
        module = self.require_contract()
        plan = make_plan()
        with self.assertRaises(module.HumanConfirmationRequired):
            plan.execution_eligibility(plan.preview(), current_state(), now=NOW)

    def test_human_confirmation_requires_exact_actor_tenant_and_typed_pr(self) -> None:
        module = self.require_contract()
        plan = make_plan()
        preview = plan.preview()

        confirmation = plan.confirm_human(
            preview=preview,
            actor=ACTOR,
            tenant=TENANT,
            typed_pull_request=PULL_REQUEST,
            now=NOW,
        )
        self.assertEqual(confirmation.plan_identity, plan.identity)
        with self.assertRaises(module.HumanConfirmationRequired):
            plan.confirm_human(
                preview=preview,
                actor="different-maintainer",
                tenant=TENANT,
                typed_pull_request=PULL_REQUEST,
                now=NOW,
            )
        with self.assertRaises(module.HumanConfirmationRequired):
            plan.confirm_human(
                preview=preview,
                actor=ACTOR,
                tenant=TENANT,
                typed_pull_request=PULL_REQUEST + 1,
                now=NOW,
            )

    def test_revalidation_fails_closed_on_authority_and_evidence_drift(self) -> None:
        module = self.require_contract()
        plan = make_plan()
        cases = (
            ("actor", {"actor": "other"}),
            ("tenant", {"tenant": "other-tenant"}),
            ("head", {"head_sha": "b" * 40}),
            ("body", {"body": "Changed body.\n"}),
            ("permissions", {"permissions": {"push": False}}),
            ("checks", {"checks": {"ci": "failure"}}),
        )
        for field, changes in cases:
            with self.subTest(field=field):
                with self.assertRaises(module.PlanDriftError) as error:
                    plan.revalidate(current_state(**changes), now=NOW)
                self.assertIn(field, str(error.exception))

    def test_expired_plan_cannot_be_confirmed_or_revalidated(self) -> None:
        module = self.require_contract()
        plan = make_plan(expires_at=NOW + timedelta(seconds=1))
        with self.assertRaises(module.PlanExpiredError):
            plan.confirm_human(
                preview=plan.preview(),
                actor=ACTOR,
                tenant=TENANT,
                typed_pull_request=PULL_REQUEST,
                now=NOW + timedelta(seconds=2),
            )
        with self.assertRaises(module.PlanExpiredError):
            plan.revalidate(current_state(), now=NOW + timedelta(seconds=2))

    def test_one_confirmation_covers_only_explicit_operations_for_one_pr(self) -> None:
        module = self.require_contract()
        body = "Approved by @maintainer for Hive auto-merge on green CI."
        plan = make_plan(
            action_kind="approve-and-queue",
            body=body,
            operations=(
                operation(
                    "gh",
                    "label",
                    "create",
                    "lgtm",
                    "--repo",
                    REPOSITORY,
                ),
                operation(
                    "gh",
                    "pr",
                    "review",
                    str(PULL_REQUEST),
                    "--repo",
                    REPOSITORY,
                    "--approve",
                    "--body",
                    body,
                ),
                operation(
                    "gh",
                    "pr",
                    "edit",
                    str(PULL_REQUEST),
                    "--repo",
                    REPOSITORY,
                    "--add-label",
                    "lgtm",
                ),
            ),
        )
        preview = plan.preview()
        confirmation = plan.confirm_human(
            preview=preview,
            actor=ACTOR,
            tenant=TENANT,
            typed_pull_request=PULL_REQUEST,
            now=NOW,
        )
        eligibility = plan.execution_eligibility(
            confirmation,
            current_state(body=body),
            now=NOW,
        )
        seen = []

        def executor(operation_record):
            seen.append(operation_record.argv)
            return module.OperationResult(return_code=0)

        receipt = plan.execute(
            eligibility,
            current_state(body=body),
            executor,
            ledger=TestReceiptLedger(),
            now=NOW,
        )
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(receipt.attempted_operations, 3)
        self.assertEqual(tuple(seen), tuple(op.argv for op in plan.operations))

    def test_execution_revalidates_before_running_any_operation(self) -> None:
        module = self.require_contract()
        plan = make_plan()
        preview = plan.preview()
        confirmation = plan.confirm_human(
            preview=preview,
            actor=ACTOR,
            tenant=TENANT,
            typed_pull_request=PULL_REQUEST,
            now=NOW,
        )
        eligibility = plan.execution_eligibility(
            confirmation,
            current_state(),
            now=NOW,
        )
        calls = []
        with self.assertRaises(module.PlanDriftError):
            plan.execute(
                eligibility,
                current_state(head_sha="b" * 40),
                lambda op: calls.append(op),
                ledger=TestReceiptLedger(),
                now=NOW,
            )
        self.assertEqual(calls, [])

    def test_execution_claims_idempotency_key_before_runner_and_rejects_replay(self) -> None:
        module = self.require_contract()
        plan = make_plan()
        preview = plan.preview()
        confirmation = plan.confirm_human(
            preview=preview,
            actor=ACTOR,
            tenant=TENANT,
            typed_pull_request=PULL_REQUEST,
            now=NOW,
        )
        eligibility = plan.execution_eligibility(
            confirmation,
            current_state(),
            now=NOW,
        )

        ledger = TestReceiptLedger()
        calls = []

        def executor(operation_record):
            calls.append(operation_record)
            return module.OperationResult(return_code=0)

        first = plan.execute(
            eligibility,
            current_state(),
            executor,
            ledger=ledger,
            now=NOW,
        )
        self.assertEqual(first.status, "succeeded")
        with self.assertRaises(module.ExecutionNotEligible):
            plan.execute(
                eligibility,
                current_state(),
                executor,
                ledger=ledger,
                now=NOW,
            )
        self.assertEqual(len(calls), len(plan.operations))
        self.assertEqual(ledger.receipts, [first])

    def test_failed_sequence_stops_and_receipt_is_bounded(self) -> None:
        module = self.require_contract()
        plan = make_plan(
            operations=(
                operation(
                    "gh",
                    "pr",
                    "review",
                    str(PULL_REQUEST),
                    "--repo",
                    REPOSITORY,
                    "--approve",
                    "--body",
                    "Reviewed exactly.\n",
                ),
                operation(
                    "gh",
                    "pr",
                    "edit",
                    str(PULL_REQUEST),
                    "--repo",
                    REPOSITORY,
                    "--add-label",
                    "lgtm",
                ),
            ),
        )
        preview = plan.preview()
        confirmation = plan.confirm_human(
            preview=preview,
            actor=ACTOR,
            tenant=TENANT,
            typed_pull_request=PULL_REQUEST,
            now=NOW,
        )
        eligibility = plan.execution_eligibility(
            confirmation,
            current_state(),
            now=NOW,
        )
        calls = []

        def executor(operation_record):
            calls.append(operation_record)
            return module.OperationResult(
                return_code=1,
                detail="x" * 10_000,
            )

        receipt = plan.execute(
            eligibility,
            current_state(),
            executor,
            ledger=TestReceiptLedger(),
            now=NOW,
        )
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.attempted_operations, 1)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(receipt.detail), module.MAX_RECEIPT_DETAIL)


if __name__ == "__main__":
    unittest.main()
