"""A small, immutable authority contract for GitHub mutations.

This module does not call GitHub or persist state. A caller constructs an
exact plan, shows its preview, obtains a direct human confirmation, checks
the live state again, and supplies an executor for the already-previewed
operations. The executor receives no operation that was not in the plan.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Protocol


DEFAULT_PLAN_TTL = timedelta(minutes=10)
MAX_OPERATIONS = 32
MAX_RECEIPT_DETAIL = 256
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_REPOSITORY = re.compile(r"[^/\s]+/[^/\s]+\Z")
_FORBIDDEN_OPERATION_ARGS = {
    "--admin",
    "--auto",
    "--delete-branch",
    "--force",
    "--force-with-lease",
    "-f",
}
_ALLOWED_BATCH_GH_PR_COMMANDS = {
    "close",
    "comment",
    "edit",
    "merge",
    "review",
    "update-branch",
}


class ActionPlanError(Exception):
    """Base class for contract failures."""


class InvalidPlanError(ActionPlanError, ValueError):
    """The requested plan cannot represent a safe exact operation."""


class PlanDriftError(ActionPlanError):
    """The live state no longer matches the state bound into a plan."""


class PlanExpiredError(PlanDriftError):
    """The plan is outside its bounded validity window."""


class HumanConfirmationRequired(ActionPlanError):
    """Execution was attempted without the opaque human confirmation token."""


class ExecutionNotEligible(ActionPlanError):
    """The execution authorization is missing or belongs to another plan."""


_JSON_SCALAR = type(None) | bool | int | float | str



def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidPlanError(f"{field_name} must be timezone-aware")
    if value.utcoffset() is None:
        raise InvalidPlanError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    return _utc(value, "now") if value is not None else datetime.now(timezone.utc)


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidPlanError(f"{field_name} must be a non-empty exact string")
    return value


def _head(value: str, field_name: str = "head_sha") -> str:
    if not isinstance(value, str) or not _FULL_SHA.fullmatch(value):
        raise InvalidPlanError(f"{field_name} must be the full 40-character head SHA")
    return value.lower()


def _pull_request(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidPlanError("pull_request must be a positive integer")
    return value


def _scalar(value: object, field_name: str) -> _JSON_SCALAR:
    if not isinstance(value, (type(None), bool, int, float, str)):
        raise InvalidPlanError(f"{field_name} must contain JSON scalar values")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidPlanError(f"{field_name} cannot contain non-finite numbers")
    return value


def _pairs(value: Mapping[str, object] | Sequence[tuple[str, object]], field_name: str):
    if isinstance(value, Mapping):
        entries = value.items()
    else:
        entries = value
    normalized: list[tuple[str, _JSON_SCALAR]] = []
    try:
        for key, item in entries:
            if not isinstance(key, str) or not key:
                raise InvalidPlanError(f"{field_name} keys must be non-empty strings")
            normalized.append((key, _scalar(item, field_name)))
    except (TypeError, ValueError) as error:
        if isinstance(error, InvalidPlanError):
            raise
        raise InvalidPlanError(f"{field_name} must be a mapping") from error
    return tuple(sorted(normalized))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _mapping_payload(pairs: tuple[tuple[str, _JSON_SCALAR], ...]) -> dict[str, _JSON_SCALAR]:
    return {key: value for key, value in pairs}


@dataclass(frozen=True)
class Prerequisites:
    """The exact permission and check snapshot a plan was built from."""

    permissions: tuple[tuple[str, _JSON_SCALAR], ...]
    checks: tuple[tuple[str, _JSON_SCALAR], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "permissions", _pairs(self.permissions, "permissions"))
        object.__setattr__(self, "checks", _pairs(self.checks, "checks"))

    @classmethod
    def from_mappings(
        cls,
        *,
        permissions: Mapping[str, object],
        checks: Mapping[str, object],
    ) -> "Prerequisites":
        return cls(
            permissions=_pairs(permissions, "permissions"),
            checks=_pairs(checks, "checks"),
        )

    def payload(self) -> dict[str, dict[str, _JSON_SCALAR]]:
        return {
            "permissions": _mapping_payload(self.permissions),
            "checks": _mapping_payload(self.checks),
        }


@dataclass(frozen=True)
class GitHubOperation:
    """One exact argv vector that a caller may execute after confirmation."""

    argv: tuple[str, ...]
    body: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)):
            raise InvalidPlanError("operation argv must be a sequence of strings")
        try:
            argv = tuple(self.argv)
        except TypeError as error:
            raise InvalidPlanError("operation argv must be a sequence of strings") from error
        if not argv or any(not isinstance(argument, str) for argument in argv):
            raise InvalidPlanError("operation argv must be a non-empty string sequence")
        if self.body is not None and not isinstance(self.body, str):
            raise InvalidPlanError("operation body must be an exact Markdown string or None")
        object.__setattr__(self, "argv", argv)

    @classmethod
    def from_argv(
        cls,
        argv: Sequence[str],
        *,
        body: str | None = None,
    ) -> "GitHubOperation":
        return cls(tuple(argv), body=body)


@dataclass(frozen=True)
class CurrentState:
    """Read-only live evidence used to revalidate an immutable plan."""

    actor: str
    tenant: str
    repository: str
    pull_request: int
    head_sha: str
    body: str | None
    prerequisites: Prerequisites
    is_draft: bool = False

    def __post_init__(self) -> None:
        if self.is_draft:
            raise PlanDriftError("PR is draft")
        object.__setattr__(self, "actor", _text(self.actor, "actor"))
        object.__setattr__(self, "tenant", _text(self.tenant, "tenant"))
        object.__setattr__(self, "repository", _text(self.repository, "repository"))
        if not _REPOSITORY.fullmatch(self.repository):
            raise InvalidPlanError("repository must be owner/name")
        object.__setattr__(self, "pull_request", _pull_request(self.pull_request))
        object.__setattr__(self, "head_sha", _head(self.head_sha))
        if self.body is not None and not isinstance(self.body, str):
            raise InvalidPlanError("body must be an exact Markdown string or None")
        if not isinstance(self.prerequisites, Prerequisites):
            raise InvalidPlanError("prerequisites must be a Prerequisites value")

    @classmethod
    def capture(
        cls,
        *,
        actor: str,
        tenant: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        body: str | None = None,
        prerequisites: Prerequisites | None = None,
        permissions: Mapping[str, object] | None = None,
        checks: Mapping[str, object] | None = None,
        is_draft: bool | None = None,
        live: Mapping[str, object] | None = None,
    ) -> "CurrentState":
        draft = is_draft is True or (live is not None and bool(live.get("isDraft")))
        if draft:
            raise PlanDriftError("PR is draft")
        return cls(
            actor=actor,
            tenant=tenant,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            body=body,
            prerequisites=_resolve_prerequisites(prerequisites, permissions, checks),
            is_draft=False,
        )


def _resolve_prerequisites(
    prerequisites: Prerequisites | None,
    permissions: Mapping[str, object] | None,
    checks: Mapping[str, object] | None,
) -> Prerequisites:
    if prerequisites is not None and (permissions is not None or checks is not None):
        raise InvalidPlanError("pass prerequisites or permission/check mappings, not both")
    if prerequisites is not None:
        if not isinstance(prerequisites, Prerequisites):
            raise InvalidPlanError("prerequisites must be a Prerequisites value")
        return prerequisites
    return Prerequisites.from_mappings(
        permissions=permissions or {},
        checks=checks or {},
    )


@dataclass(frozen=True)
class OperationResult:
    """The bounded result an operation executor reports to the contract."""

    return_code: int
    detail: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.return_code, bool) or not isinstance(self.return_code, int):
            raise ValueError("return_code must be an integer")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")


class ReceiptLedger(Protocol):
    """Caller-owned atomic claim and bounded receipt storage contract."""

    def claim(self, idempotency_key: str) -> bool:
        """Atomically claim a key, returning false when already claimed."""

    def record(self, receipt: object) -> None:
        """Persist the bounded receipt for a claimed key."""


@dataclass(frozen=True)
class BatchMutationItem:
    repository: str
    pull_request: int
    head_sha: str
    prerequisites: Prerequisites
    operations: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not _REPOSITORY.fullmatch(_text(self.repository, "repository")):
            raise InvalidPlanError("repository must be owner/name")
        object.__setattr__(self, "pull_request", _pull_request(self.pull_request))
        object.__setattr__(self, "head_sha", _head(self.head_sha))
        if not isinstance(self.prerequisites, Prerequisites):
            raise InvalidPlanError("prerequisites must be a Prerequisites value")
        operations = tuple(tuple(operation) for operation in self.operations)
        if not operations or any(
            not operation or any(not isinstance(arg, str) for arg in operation)
            for operation in operations
        ):
            raise InvalidPlanError("batch operations must be non-empty argv vectors")
        for operation in operations:
            if any(arg in _FORBIDDEN_OPERATION_ARGS for arg in operation) or any(
                arg == "git push"
                or (arg == "git" and idx + 1 < len(operation) and operation[idx + 1] == "push")
                for idx, arg in enumerate(operation)
            ):
                raise InvalidPlanError("admin, force, and branch-deletion operations are forbidden")
            is_valid_gh = (
                len(operation) >= 3
                and operation[0] == "gh"
                and operation[1] == "pr"
                and operation[2] in _ALLOWED_BATCH_GH_PR_COMMANDS
            )
            is_valid_hive = (
                len(operation) == 4
                and operation[0] == "python3"
                and operation[1] == "image/tui/hive_api.py"
                and operation[2] == "queue"
                and (operation[3].startswith("https://") or operation[3].startswith("http://"))
            )
            if not (is_valid_gh or is_valid_hive):
                raise InvalidPlanError(
                    "batch operations must be an allowed gh pr command or python3 image/tui/hive_api.py queue"
                )
        object.__setattr__(self, "operations", operations)

    @property
    def identity(self) -> str:
        return f"{self.repository}#{self.pull_request}@{self.head_sha}"

    @property
    def number(self) -> int:
        return self.pull_request


@dataclass(frozen=True)
class BatchActionPreview:
    plan_identity: str
    actor: str
    tenant: str
    action_kind: str
    items: tuple[BatchMutationItem, ...]
    created_at: datetime
    expires_at: datetime


_BATCH_HUMAN_CAPABILITY = object()
_BATCH_EXECUTION_CAPABILITY = object()


@dataclass(frozen=True, init=False)
class BatchHumanConfirmation:
    plan_identity: str
    actor: str
    tenant: str
    items: tuple[str, ...]
    confirmed_at: datetime
    _capability: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        plan_identity: str,
        actor: str,
        tenant: str,
        items: Sequence[str],
        confirmed_at: datetime,
        _capability: object,
    ) -> None:
        if _capability is not _BATCH_HUMAN_CAPABILITY:
            raise HumanConfirmationRequired("batch confirmation is not human-issued")
        object.__setattr__(self, "plan_identity", plan_identity)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "items", tuple(items))
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "_capability", _capability)


@dataclass(frozen=True, init=False)
class BatchExecutionEligibility:
    plan_identity: str
    actor: str
    tenant: str
    confirmed_items: tuple[str, ...]
    eligible_at: datetime
    _capability: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        plan_identity: str,
        actor: str,
        tenant: str,
        confirmed_items: Sequence[str],
        eligible_at: datetime,
        _capability: object,
    ) -> None:
        if _capability is not _BATCH_EXECUTION_CAPABILITY:
            raise ExecutionNotEligible("batch execution eligibility is not plan-issued")
        object.__setattr__(self, "plan_identity", plan_identity)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "confirmed_items", tuple(confirmed_items))
        object.__setattr__(self, "eligible_at", eligible_at)
        object.__setattr__(self, "_capability", _capability)


class BatchResultMap(dict):
    """Mapping supporting lookup by int (PR number), key (repo#number), tuple (repo, number), or identity."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._by_repo_pr: dict[tuple[str, int], Any] = {}
        self._by_key: dict[str, Any] = {}
        self._by_identity: dict[str, Any] = {}
        for k, v in list(self.items()):
            if isinstance(k, tuple) and len(k) == 2:
                self._by_repo_pr[k] = v
            elif isinstance(k, str) and "#" in k:
                if "@" in k:
                    self._by_identity[k] = v
                self._by_key[k] = v

    def record(self, item: BatchMutationItem, value: Any) -> None:
        self[item.pull_request] = value
        self._by_repo_pr[(item.repository, item.pull_request)] = value
        self._by_key[f"{item.repository}#{item.pull_request}"] = value
        self._by_identity[item.identity] = value

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, tuple) and len(key) == 2:
            return self._by_repo_pr[key]
        if isinstance(key, str) and "#" in key:
            if "@" in key and key in self._by_identity:
                return self._by_identity[key]
            if key in self._by_key:
                return self._by_key[key]
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, tuple) and len(key) == 2:
            return key in self._by_repo_pr
        if isinstance(key, str) and "#" in key:
            return key in self._by_identity or key in self._by_key
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


@dataclass(frozen=True)
class BatchActionReceipt:
    succeeded: BatchResultMap | dict[int, int]
    rejected: BatchResultMap | dict[int, str]
    failed: BatchResultMap | dict[int, str]

    def __post_init__(self) -> None:
        if not isinstance(self.succeeded, BatchResultMap):
            m = BatchResultMap(self.succeeded)
            object.__setattr__(self, "succeeded", m)
        if not isinstance(self.rejected, BatchResultMap):
            m = BatchResultMap(self.rejected)
            object.__setattr__(self, "rejected", m)
        if not isinstance(self.failed, BatchResultMap):
            m = BatchResultMap(self.failed)
            object.__setattr__(self, "failed", m)


@dataclass(frozen=True)
class BatchActionPlan:
    actor: str
    tenant: str
    action_kind: str
    items: tuple[BatchMutationItem, ...]
    created_at: datetime
    expires_at: datetime
    _identity: str

    @classmethod
    def build(
        cls,
        *,
        actor: str,
        tenant: str,
        action_kind: str,
        items: Sequence[BatchMutationItem],
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> "BatchActionPlan":
        created = _utc(created_at, "created_at") if created_at else datetime.now(timezone.utc)
        expires = _utc(expires_at, "expires_at") if expires_at else created + DEFAULT_PLAN_TTL
        normalized = tuple(items)
        if not normalized:
            raise InvalidPlanError("batch action plan requires at least one item")
        if len(normalized) > MAX_OPERATIONS:
            raise InvalidPlanError(f"batch action plan cannot exceed {MAX_OPERATIONS} items")
        material = {
            "actor": actor,
            "tenant": tenant,
            "action_kind": action_kind,
            "items": [
                {
                    "identity": item.identity,
                    "prerequisites": item.prerequisites.payload(),
                    "operations": [list(operation) for operation in item.operations],
                }
                for item in normalized
            ],
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
        }
        return cls(
            _text(actor, "actor"),
            _text(tenant, "tenant"),
            _text(action_kind, "action_kind"),
            normalized,
            created,
            expires,
            sha256(_canonical(material)).hexdigest(),
        )

    @property
    def identity(self) -> str:
        return self._identity

    def preview(self) -> BatchActionPreview:
        return BatchActionPreview(
            self._identity,
            self.actor,
            self.tenant,
            self.action_kind,
            self.items,
            self.created_at,
            self.expires_at,
        )

    def confirm_human(
        self,
        *,
        preview: BatchActionPreview,
        actor: str,
        tenant: str,
        typed_items: str,
        now: datetime | None = None,
    ) -> BatchHumanConfirmation:
        current = _now(now)
        if preview.plan_identity != self._identity or actor != self.actor or tenant != self.tenant:
            raise HumanConfirmationRequired("batch confirmation does not match the plan")
        if current < self.created_at or current >= self.expires_at:
            raise PlanExpiredError("batch action plan has expired")
        expected = tuple(item.identity for item in self.items)
        actual = tuple(str(value) for value in str(typed_items).split())
        if actual != expected:
            raise HumanConfirmationRequired("typed confirmation does not match every exact PR and head")
        return BatchHumanConfirmation(
            plan_identity=self._identity,
            actor=actor,
            tenant=tenant,
            items=actual,
            confirmed_at=current,
            _capability=_BATCH_HUMAN_CAPABILITY,
        )

    def execution_eligibility(
        self,
        confirmation: BatchHumanConfirmation,
        *,
        now: datetime | None = None,
    ) -> BatchExecutionEligibility:
        current = _now(now)
        if not isinstance(confirmation, BatchHumanConfirmation):
            raise HumanConfirmationRequired("batch execution requires human confirmation")
        if (
            confirmation.plan_identity != self._identity
            or confirmation.items != tuple(item.identity for item in self.items)
        ):
            raise HumanConfirmationRequired("batch confirmation is for another list")
        if current < self.created_at or current >= self.expires_at:
            raise PlanExpiredError("batch action plan has expired")
        return BatchExecutionEligibility(
            plan_identity=self._identity,
            actor=self.actor,
            tenant=self.tenant,
            confirmed_items=confirmation.items,
            eligible_at=current,
            _capability=_BATCH_EXECUTION_CAPABILITY,
        )

    def execute(
        self,
        eligibility: BatchExecutionEligibility,
        current_state: Callable[[BatchMutationItem], CurrentState],
        executor: Callable[[BatchMutationItem, tuple[str, ...]], OperationResult | int],
        *,
        ledger: ReceiptLedger,
        now: datetime | None = None,
    ) -> BatchActionReceipt:
        current = _now(now)
        if current < self.created_at or current >= self.expires_at:
            raise PlanExpiredError("batch action plan has expired")
        if not isinstance(eligibility, BatchExecutionEligibility) or eligibility.plan_identity != self._identity:
            raise ExecutionNotEligible("batch execution eligibility does not match")
        if getattr(eligibility, "_capability", None) is not _BATCH_EXECUTION_CAPABILITY:
            raise ExecutionNotEligible("batch execution eligibility is not plan-issued")
        if eligibility.actor != self.actor or eligibility.tenant != self.tenant:
            raise ExecutionNotEligible("batch execution authority does not match")
        if eligibility.confirmed_items != tuple(item.identity for item in self.items):
            raise ExecutionNotEligible("batch execution eligibility does not match")
        succeeded = BatchResultMap()
        rejected = BatchResultMap()
        failed = BatchResultMap()
        for item in self.items:
            try:
                live = current_state(item)
                if live.head_sha != item.head_sha or live.prerequisites != item.prerequisites:
                    rejected.record(item, "head drift invalidates the item")
                    continue
                for operation in item.operations:
                    result = executor(item, operation)
                    if isinstance(result, int) and not isinstance(result, bool):
                        result = OperationResult(result)
                    if not isinstance(result, OperationResult) or result.return_code != 0:
                        detail = (
                            result.detail
                            if isinstance(result, OperationResult)
                            else "invalid operation result"
                        )
                        failed.record(item, detail[:MAX_RECEIPT_DETAIL])
                        break
                else:
                    succeeded.record(item, len(item.operations))
            except PlanDriftError as error:
                rejected.record(item, str(error))
            except Exception as error:
                failed.record(item, str(error)[:MAX_RECEIPT_DETAIL])
        receipt = BatchActionReceipt(succeeded, rejected, failed)
        ledger.record(receipt)
        return receipt


__all__ = [
    "ActionPlanError",
    "BatchActionPlan",
    "BatchActionPreview",
    "BatchActionReceipt",
    "BatchExecutionEligibility",
    "BatchHumanConfirmation",
    "BatchMutationItem",
    "BatchResultMap",
    "CurrentState",
    "ExecutionNotEligible",
    "GitHubOperation",
    "HumanConfirmationRequired",
    "InvalidPlanError",
    "MAX_RECEIPT_DETAIL",
    "OperationResult",
    "PlanDriftError",
    "PlanExpiredError",
    "Prerequisites",
    "ReceiptLedger",
]
