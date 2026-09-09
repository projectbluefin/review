"""Focused contract tests for the batch exact-head action plan."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


TUI_ROOT = Path(__file__).resolve().parents[1] / "image"
if str(TUI_ROOT) not in sys.path:
    sys.path.insert(0, str(TUI_ROOT))

try:
    from tui import action_plan as contract
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


def batch_item(module, number, head):
    return module.BatchMutationItem(
        repository="projectbluefin/review",
        pull_request=number,
        head_sha=head,
        prerequisites=module.Prerequisites.from_mappings(
            permissions={"push": True},
            checks={"ci": "success"},
        ),
        operations=(
            (
                "python3",
                "image/tui/hive_api.py",
                "queue",
                f"https://hive.example/pr/{number}",
            ),
        ),
    )


def test_batch_gate_binds_every_exact_head():
    module = contract
    first = batch_item(module, 184, "a" * 40)
    second = batch_item(module, 185, "b" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    preview = plan.preview()
    confirmation = plan.confirm_human(
        preview=preview,
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=(
            "projectbluefin/review#184@"
            + "a" * 40
            + " projectbluefin/review#185@"
            + "b" * 40
        ),
        now=NOW,
    )
    assert confirmation.items == (first.identity, second.identity)


def test_batch_drift_rejects_only_the_changed_item():
    module = contract
    first = batch_item(module, 184, "a" * 40)
    second = batch_item(module, 185, "b" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    confirmation = plan.confirm_human(
        preview=plan.preview(),
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=(
            "projectbluefin/review#184@" + "a" * 40
            + " projectbluefin/review#185@" + "b" * 40
        ),
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)
    seen = []
    current = lambda item: module.CurrentState.capture(
        actor="maintainer",
        tenant="projectbluefin",
        repository=item.repository,
        pull_request=item.pull_request,
        head_sha=("c" * 40 if item.pull_request == 184 else item.head_sha),
        permissions={"push": True},
        checks={"ci": "success"},
    )
    receipt = plan.execute(
        eligibility,
        current,
        lambda item, operation: seen.append(item.pull_request) or 0,
        ledger=TestReceiptLedger(),
        now=NOW,
    )
    assert receipt.rejected == {184: "head drift invalidates the item"}
    assert receipt.succeeded == {185: 1}
    assert seen == [185]


def test_batch_execution_fails_on_expiration():
    module = contract
    first = batch_item(module, 184, "a" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first,),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    preview = plan.preview()
    confirmation = plan.confirm_human(
        preview=preview,
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=first.identity,
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)
    current = lambda item: module.CurrentState.capture(
        actor="maintainer",
        tenant="projectbluefin",
        repository=item.repository,
        pull_request=item.pull_request,
        head_sha=item.head_sha,
        permissions={"push": True},
        checks={"ci": "success"},
    )
    try:
        plan.execute(
            eligibility,
            current,
            lambda item, operation: 0,
            ledger=TestReceiptLedger(),
            now=EXPIRES + timedelta(seconds=1),
        )
        assert False, "expected PlanExpiredError"
    except module.PlanExpiredError as error:
        assert "expired" in str(error)


def test_batch_mutation_item_forbids_unsafe_arguments():
    module = contract
    forbidden = [
        ("--admin",),
        ("--force",),
        ("-f",),
        ("--delete-branch",),
        ("git", "push"),
        ("git push",),
    ]
    for arg_seq in forbidden:
        op = ("gh", "pr", "merge", "184", *arg_seq)
        try:
            module.BatchMutationItem(
                repository="projectbluefin/review",
                pull_request=184,
                head_sha="a" * 40,
                prerequisites=module.Prerequisites.from_mappings(
                    permissions={"push": True},
                    checks={"ci": "success"},
                ),
                operations=(op,),
            )
            assert False, f"expected InvalidPlanError for {arg_seq}"
        except module.InvalidPlanError as error:
            assert "admin, force, and branch-deletion operations are forbidden" in str(error)


def test_batch_mutation_item_requires_allowed_patterns():
    module = contract
    invalid_ops = [
        ("rm", "-rf", "/tmp"),
        ("curl", "https://example.com"),
        ("echo", "hello"),
    ]
    for op in invalid_ops:
        try:
            module.BatchMutationItem(
                repository="projectbluefin/review",
                pull_request=184,
                head_sha="a" * 40,
                prerequisites=module.Prerequisites.from_mappings(
                    permissions={"push": True},
                    checks={"ci": "success"},
                ),
                operations=(op,),
            )
            assert False, f"expected InvalidPlanError for {op}"
        except module.InvalidPlanError:
            pass


def test_batch_confirmation_rejects_mismatched_items():
    module = contract
    first = batch_item(module, 184, "a" * 40)
    second = batch_item(module, 185, "b" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    preview = plan.preview()
    # Missing second item
    try:
        plan.confirm_human(
            preview=preview,
            actor="maintainer",
            tenant="projectbluefin",
            typed_items=first.identity,
            now=NOW,
        )
        assert False, "expected HumanConfirmationRequired"
    except module.HumanConfirmationRequired as error:
        assert "typed confirmation does not match every exact PR and head" in str(error)

    # Wrong head on second item
    try:
        plan.confirm_human(
            preview=preview,
            actor="maintainer",
            tenant="projectbluefin",
            typed_items=f"{first.identity} projectbluefin/review#185@{'c' * 40}",
            now=NOW,
        )
        assert False, "expected HumanConfirmationRequired"
    except module.HumanConfirmationRequired as error:
        assert "typed confirmation does not match every exact PR and head" in str(error)


def test_batch_execution_requires_plan_issued_capability():
    module = contract
    first = batch_item(module, 184, "a" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first,),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    confirmation = plan.confirm_human(
        preview=plan.preview(),
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=first.identity,
        now=NOW,
    )
    fake_eligibility = object()
    try:
        plan.execute(
            fake_eligibility,
            lambda item: None,
            lambda item, op: 0,
            ledger=TestReceiptLedger(),
            now=NOW,
        )
        assert False, "expected ExecutionNotEligible"
    except module.ExecutionNotEligible as error:
        assert "eligibility" in str(error)


def test_batch_cross_repository_pr_number_collision():
    module = contract
    first = module.BatchMutationItem(
        repository="projectbluefin/repo-a",
        pull_request=31,
        head_sha="a" * 40,
        prerequisites=module.Prerequisites.from_mappings(
            permissions={"push": True}, checks={"ci": "success"}
        ),
        operations=(("gh", "pr", "review", "31", "--repo", "projectbluefin/repo-a", "--approve"),),
    )
    second = module.BatchMutationItem(
        repository="projectbluefin/repo-b",
        pull_request=31,
        head_sha="b" * 40,
        prerequisites=module.Prerequisites.from_mappings(
            permissions={"push": True}, checks={"ci": "success"}
        ),
        operations=(("gh", "pr", "review", "31", "--repo", "projectbluefin/repo-b", "--approve"),),
    )
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    preview = plan.preview()
    confirmation = plan.confirm_human(
        preview=preview,
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=f"{first.identity} {second.identity}",
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)
    current = lambda item: module.CurrentState.capture(
        actor="maintainer",
        tenant="projectbluefin",
        repository=item.repository,
        pull_request=item.pull_request,
        head_sha=item.head_sha,
        permissions={"push": True},
        checks={"ci": "success"},
    )
    seen = []
    receipt = plan.execute(
        eligibility,
        current,
        lambda item, operation: seen.append(item.identity) or 0,
        ledger=TestReceiptLedger(),
        now=NOW,
    )
    assert len(seen) == 2
    assert receipt.succeeded[("projectbluefin/repo-a", 31)] == 1
    assert receipt.succeeded[("projectbluefin/repo-b", 31)] == 1
    assert receipt.succeeded[first.identity] == 1
    assert receipt.succeeded[second.identity] == 1
    assert ("projectbluefin/repo-a", 31) in receipt.succeeded
    assert ("projectbluefin/repo-b", 31) in receipt.succeeded


def test_batch_forbidden_gh_commands_raise_invalid_plan():
    module = contract
    forbidden_ops = [
        ("gh", "repo", "delete", "projectbluefin/review"),
        ("gh", "api", "-X", "DELETE", "/repos/projectbluefin/review"),
        ("gh", "pr", "merge", "184", "--delete-branch"),
        ("gh", "pr", "merge", "184", "--admin"),
        ("gh", "pr", "merge", "184", "--force"),
        ("gh", "pr", "merge", "184", "-f"),
        ("gh", "issue", "delete", "184"),
        ("gh", "issue", "view", "184"),
        ("gh", "issue", "comment", "184", "--body", "fixed"),
        ("gh", "issue", "edit", "184", "--add-label", "bug"),
        ("gh", "issue", "close", "184"),
    ]
    for op in forbidden_ops:
        try:
            module.BatchMutationItem(
                repository="projectbluefin/review",
                pull_request=184,
                head_sha="a" * 40,
                prerequisites=module.Prerequisites.from_mappings(
                    permissions={"push": True}, checks={"ci": "success"}
                ),
                operations=(op,),
            )
            assert False, f"expected InvalidPlanError for {op}"
        except module.InvalidPlanError:
            pass

    # Allowed operations
    allowed_ops = [
        ("gh", "pr", "review", "184", "--approve"),
        ("gh", "pr", "merge", "184"),
        ("gh", "pr", "edit", "184", "--add-label", "lgtm"),
        ("gh", "pr", "comment", "184", "--body", "done"),
        ("gh", "pr", "close", "184"),
        ("gh", "pr", "update-branch", "184"),
        ("gh", "pr", "update-branch", "184", "--repo", "projectbluefin/review"),
        ("python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/184"),
    ]
    for op in allowed_ops:
        item = module.BatchMutationItem(
            repository="projectbluefin/review",
            pull_request=184,
            head_sha="a" * 40,
            prerequisites=module.Prerequisites.from_mappings(
                permissions={"push": True}, checks={"ci": "success"}
            ),
            operations=(op,),
        )
        assert item.operations == (op,)


def test_batch_hive_script_exactness():
    module = contract
    invalid_hive_ops = [
        ("python3", "evil_hive_api.py", "queue", "https://hive.example/pr/184"),
        ("python3", "image/tui/evil_hive_api.py", "queue", "https://hive.example/pr/184"),
        ("python", "image/tui/hive_api.py", "queue", "https://hive.example/pr/184"),
        ("/usr/bin/python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/184"),
        ("python3", "image/tui/hive_api.py", "queue"),
        ("python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/184", "--extra"),
        ("python3", "image/tui/hive_api.py", "status", "https://hive.example/pr/184"),
        ("python3", "image/tui/hive_api.py", "queue", "ftp://hive.example/pr/184"),
        ("python3", "image/tui/hive_api.py", "queue", "file:///etc/passwd"),
        ("python3", "image/tui/hive_api.py", "queue", "invalid-url"),
    ]
    for op in invalid_hive_ops:
        try:
            module.BatchMutationItem(
                repository="projectbluefin/review",
                pull_request=184,
                head_sha="a" * 40,
                prerequisites=module.Prerequisites.from_mappings(
                    permissions={"push": True}, checks={"ci": "success"}
                ),
                operations=(op,),
            )
            assert False, f"expected InvalidPlanError for non-exact hive op: {op}"
        except module.InvalidPlanError:
            pass

    valid_hive_ops = [
        ("python3", "image/tui/hive_api.py", "queue", "https://hive.example/pr/184"),
        ("python3", "image/tui/hive_api.py", "queue", "http://hive.example/pr/184"),
    ]
    for op in valid_hive_ops:
        item = module.BatchMutationItem(
            repository="projectbluefin/review",
            pull_request=184,
            head_sha="a" * 40,
            prerequisites=module.Prerequisites.from_mappings(
                permissions={"push": True}, checks={"ci": "success"}
            ),
            operations=(op,),
        )
        assert item.operations == (op,)


def test_batch_cross_repository_pr_number_collision_mixed_outcomes():
    module = contract
    first = module.BatchMutationItem(
        repository="projectbluefin/repo-a",
        pull_request=31,
        head_sha="a" * 40,
        prerequisites=module.Prerequisites.from_mappings(
            permissions={"push": True}, checks={"ci": "success"}
        ),
        operations=(("gh", "pr", "review", "31", "--repo", "projectbluefin/repo-a", "--approve"),),
    )
    second = module.BatchMutationItem(
        repository="projectbluefin/repo-b",
        pull_request=31,
        head_sha="b" * 40,
        prerequisites=module.Prerequisites.from_mappings(
            permissions={"push": True}, checks={"ci": "success"}
        ),
        operations=(("gh", "pr", "review", "31", "--repo", "projectbluefin/repo-b", "--approve"),),
    )
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    preview = plan.preview()
    confirmation = plan.confirm_human(
        preview=preview,
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=f"{first.identity} {second.identity}",
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)
    current = lambda item: module.CurrentState.capture(
        actor="maintainer",
        tenant="projectbluefin",
        repository=item.repository,
        pull_request=item.pull_request,
        head_sha=("c" * 40 if item.repository == "projectbluefin/repo-b" else item.head_sha),
        permissions={"push": True},
        checks={"ci": "success"},
    )
    seen = []
    receipt = plan.execute(
        eligibility,
        current,
        lambda item, operation: seen.append(item.identity) or 0,
        ledger=TestReceiptLedger(),
        now=NOW,
    )
    assert seen == [first.identity]
    assert receipt.succeeded[("projectbluefin/repo-a", 31)] == 1
    assert ("projectbluefin/repo-a", 31) in receipt.succeeded
    assert ("projectbluefin/repo-b", 31) not in receipt.succeeded
    assert receipt.rejected[("projectbluefin/repo-b", 31)] == "head drift invalidates the item"
    assert ("projectbluefin/repo-b", 31) in receipt.rejected
    assert ("projectbluefin/repo-a", 31) not in receipt.rejected


def test_batch_draft_rejection():
    module = contract
    # CurrentState capture with draft
    try:
        module.CurrentState.capture(
            actor="maintainer",
            tenant="projectbluefin",
            repository="projectbluefin/review",
            pull_request=184,
            head_sha="a" * 40,
            live={"isDraft": True},
        )
        assert False, "expected PlanDriftError for draft PR"
    except module.PlanDriftError as error:
        assert "PR is draft" in str(error)

    try:
        module.CurrentState.capture(
            actor="maintainer",
            tenant="projectbluefin",
            repository="projectbluefin/review",
            pull_request=184,
            head_sha="a" * 40,
            is_draft=True,
        )
        assert False, "expected PlanDriftError for is_draft=True"
    except module.PlanDriftError as error:
        assert "PR is draft" in str(error)

    # Batch execution with one draft item
    first = batch_item(module, 184, "a" * 40)
    second = batch_item(module, 185, "b" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    confirmation = plan.confirm_human(
        preview=plan.preview(),
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=f"{first.identity} {second.identity}",
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)

    def current_fetcher(item):
        if item.pull_request == 184:
            raise module.PlanDriftError("PR is draft")
        return module.CurrentState.capture(
            actor="maintainer",
            tenant="projectbluefin",
            repository=item.repository,
            pull_request=item.pull_request,
            head_sha=item.head_sha,
            permissions={"push": True},
            checks={"ci": "success"},
        )

    receipt = plan.execute(
        eligibility,
        current_fetcher,
        lambda item, operation: 0,
        ledger=TestReceiptLedger(),
        now=NOW,
    )
    assert receipt.rejected[184] == "PR is draft"
    assert ("projectbluefin/review", 184) in receipt.rejected
    assert receipt.succeeded[185] == 1
    assert ("projectbluefin/review", 185) in receipt.succeeded


def test_batch_stale_fetch_does_not_fall_back_to_stale_cache():
    module = contract
    # 1. ActionPlan execution rejects failed/drifted fetch and never falls back
    first = batch_item(module, 184, "a" * 40)
    second = batch_item(module, 185, "b" * 40)
    plan = module.BatchActionPlan.build(
        actor="maintainer",
        tenant="projectbluefin",
        action_kind="approve-and-queue",
        items=(first, second),
        created_at=NOW,
        expires_at=EXPIRES,
    )
    confirmation = plan.confirm_human(
        preview=plan.preview(),
        actor="maintainer",
        tenant="projectbluefin",
        typed_items=f"{first.identity} {second.identity}",
        now=NOW,
    )
    eligibility = plan.execution_eligibility(confirmation, now=NOW)

    def failing_fetcher(item):
        if item.pull_request == 184:
            raise module.PlanDriftError("cannot fetch live PR state")
        return module.CurrentState.capture(
            actor="maintainer",
            tenant="projectbluefin",
            repository=item.repository,
            pull_request=item.pull_request,
            head_sha=item.head_sha,
            permissions={"push": True},
            checks={"ci": "success"},
        )

    receipt = plan.execute(
        eligibility,
        failing_fetcher,
        lambda item, op: 0,
        ledger=TestReceiptLedger(),
        now=NOW,
    )
    assert receipt.rejected[184] == "cannot fetch live PR state"
    assert ("projectbluefin/review", 184) in receipt.rejected
    assert receipt.succeeded[185] == 1
    assert ("projectbluefin/review", 185) in receipt.succeeded

    # 2. In dashboard plan builder: fresh fetch failure does not fall back to cached stop.live
    import unittest.mock
    class MockBase:
        pass
    mock_app = unittest.mock.MagicMock()
    mock_app.App = MockBase
    sys.modules["textual.app"] = mock_app
    for mod in [
        "rich", "rich.errors", "rich.syntax", "rich.text", "textual", "textual.binding", "textual.containers",
        "textual.css", "textual.css.query", "textual.screen", "textual.widgets",
        "textual.geometry",
    ]:
        sys.modules.setdefault(mod, unittest.mock.MagicMock())
    sys.modules["rich.errors"].MarkupError = type("MarkupError", (Exception,), {})
    import tui.bluefin_review_tui as tui

    class DummyDashboard:
        self_login = "maintainer"
        def _queue_command(self, stop):
            return ["python3", "image/tui/hive_api.py", "queue", f"https://hive.example/pr/{stop.number}"]
        def _queueable(self, stop):
            return True
        build_batch_queue_plan = tui.ReviewDashboard.build_batch_queue_plan

    import os
    os.environ.setdefault("HIVE_HUB", "wss://hive.example/contribute")
    dashboard = DummyDashboard()
    stop_stale = tui.Stop("projectbluefin/review", 184, "review", "stale PR")
    stop_stale.live = {"headRefOid": "a" * 40, "isDraft": False}
    # fetch_live_pr fails
    dashboard.fetch_live_pr = unittest.mock.Mock(side_effect=RuntimeError("API error"))
    try:
        dashboard.build_batch_queue_plan([stop_stale])
        assert False, "expected InvalidPlanError when stale-fetch fails without fallback"
    except module.InvalidPlanError as error:
        assert "no queueable pull requests" in str(error)

    # When one fails and one succeeds with fresh data, only fresh is included
    stop_fresh = tui.Stop("projectbluefin/review", 185, "review", "fresh PR")
    stop_fresh.live = {"headRefOid": "old" * 10, "isDraft": False}
    fresh_head = "b" * 40
    def mixed_fetch(repo, number, force=False):
        if number == 184:
            raise RuntimeError("API error")
        return {"headRefOid": fresh_head, "isDraft": False, "statusCheckRollup": []}
    dashboard.fetch_live_pr = mixed_fetch
    built_plan = dashboard.build_batch_queue_plan([stop_stale, stop_fresh])
    assert len(built_plan.items) == 1
    assert built_plan.items[0].pull_request == 185
    assert built_plan.items[0].head_sha == fresh_head


class BatchActionPlanContractTests(unittest.TestCase):
    def test_shared_action_plan_module_exists(self):
        self.assertIsNotNone(
            importlib.util.find_spec("tui.action_plan"),
            "the shared action plan contract module must exist",
        )

    def test_batch_gate_binds_every_exact_head(self):
        test_batch_gate_binds_every_exact_head()

    def test_batch_drift_rejects_only_the_changed_item(self):
        test_batch_drift_rejects_only_the_changed_item()

    def test_batch_execution_fails_on_expiration(self):
        test_batch_execution_fails_on_expiration()

    def test_batch_mutation_item_forbids_unsafe_arguments(self):
        test_batch_mutation_item_forbids_unsafe_arguments()

    def test_batch_mutation_item_requires_allowed_patterns(self):
        test_batch_mutation_item_requires_allowed_patterns()

    def test_batch_confirmation_rejects_mismatched_items(self):
        test_batch_confirmation_rejects_mismatched_items()

    def test_batch_execution_requires_plan_issued_capability(self):
        test_batch_execution_requires_plan_issued_capability()

    def test_batch_cross_repository_pr_number_collision(self):
        test_batch_cross_repository_pr_number_collision()

    def test_batch_forbidden_gh_commands_raise_invalid_plan(self):
        test_batch_forbidden_gh_commands_raise_invalid_plan()

    def test_batch_hive_script_exactness(self):
        test_batch_hive_script_exactness()

    def test_batch_cross_repository_pr_number_collision_mixed_outcomes(self):
        test_batch_cross_repository_pr_number_collision_mixed_outcomes()

    def test_batch_draft_rejection(self):
        test_batch_draft_rejection()

    def test_batch_stale_fetch_does_not_fall_back_to_stale_cache(self):
        test_batch_stale_fetch_does_not_fall_back_to_stale_cache()


if __name__ == "__main__":
    unittest.main()
