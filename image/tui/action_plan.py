"""A small, immutable authority contract for GitHub mutations.

This module does not call GitHub or persist state. A caller constructs an
exact plan, shows its preview, obtains a direct human confirmation, checks
the live state again, and supplies an executor for the already-previewed
operations. The executor receives no operation that was not in the plan.
"""

from __future__ import annotations

import json
import math
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
_ACTION_KIND = re.compile(r"[a-z][a-z0-9_-]*\Z")
_FORBIDDEN_OPERATION_ARGS = {
    "--admin",
    "--auto",
    "--delete-branch",
    "--force",
    "--force-with-lease",
}
_ALLOWED_ACTION_KINDS = {
    "approve-and-queue",
    "comment",
    "merge",
    "queue",
    "reject",
    "resolve-cluster",
    "review",
    "update-branch",
}
_ALLOWED_PR_OPERATIONS = {
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


def _bounded_detail(value: object) -> str:
    detail = str(value)
    if len(detail) <= MAX_RECEIPT_DETAIL:
        return detail
    return detail[:MAX_RECEIPT_DETAIL]


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

    def __post_init__(self) -> None:
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
    ) -> "CurrentState":
        return cls(
            actor=actor,
            tenant=tenant,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            body=body,
            prerequisites=_resolve_prerequisites(prerequisites, permissions, checks),
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
class ActionPreview:
    """The plan's exact intent, with no executable confirmation capability."""

    plan_identity: str
    actor: str
    tenant: str
    repository: str
    pull_request: int
    head_sha: str
    action_kind: str
    body: str | None
    operations: tuple[GitHubOperation, ...]
    prerequisites: Prerequisites
    created_at: datetime
    expires_at: datetime
    idempotency_key: str


_HUMAN_CAPABILITY = object()


@dataclass(frozen=True, init=False)
class HumanConfirmation:
    """Opaque confirmation issued only by ``ActionPlan.confirm_human``."""

    plan_identity: str
    actor: str
    tenant: str
    pull_request: int
    confirmed_at: datetime
    _capability: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        plan_identity: str,
        actor: str,
        tenant: str,
        pull_request: int,
        confirmed_at: datetime,
        _capability: object,
    ) -> None:
        if _capability is not _HUMAN_CAPABILITY:
            raise HumanConfirmationRequired("only direct human confirmation can authorize execution")
        object.__setattr__(self, "plan_identity", plan_identity)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "pull_request", pull_request)
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "_capability", _capability)

    @classmethod
    def _issue(
        cls,
        *,
        plan_identity: str,
        actor: str,
        tenant: str,
        pull_request: int,
        confirmed_at: datetime,
    ) -> "HumanConfirmation":
        return cls(
            plan_identity=plan_identity,
            actor=actor,
            tenant=tenant,
            pull_request=pull_request,
            confirmed_at=confirmed_at,
            _capability=_HUMAN_CAPABILITY,
        )


_EXECUTION_CAPABILITY = object()


@dataclass(frozen=True, init=False)
class ExecutionEligibility:
    """Opaque authorization produced after confirmation and live revalidation."""

    plan_identity: str
    idempotency_key: str
    actor: str
    tenant: str
    eligible_at: datetime
    _capability: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        plan_identity: str,
        idempotency_key: str,
        actor: str,
        tenant: str,
        eligible_at: datetime,
        _capability: object,
    ) -> None:
        if _capability is not _EXECUTION_CAPABILITY:
            raise ExecutionNotEligible("execution eligibility is issued by plan validation")
        object.__setattr__(self, "plan_identity", plan_identity)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "eligible_at", eligible_at)
        object.__setattr__(self, "_capability", _capability)

    @classmethod
    def _issue(cls, plan: "ActionPlan", *, actor: str, tenant: str, now: datetime):
        return cls(
            plan_identity=plan.identity,
            idempotency_key=plan.idempotency_key,
            actor=actor,
            tenant=tenant,
            eligible_at=now,
            _capability=_EXECUTION_CAPABILITY,
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


@dataclass(frozen=True)
class ActionReceipt:
    """A bounded, non-transcript result for one plan execution."""

    plan_identity: str
    idempotency_key: str
    status: str
    total_operations: int
    attempted_operations: int
    completed_operations: int
    started_at: datetime
    finished_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _bounded_detail(self.detail))


class ReceiptLedger(Protocol):
    """Caller-owned atomic claim and bounded receipt storage contract."""

    def claim(self, idempotency_key: str) -> bool:
        """Atomically claim a key, returning false when already claimed."""

    def record(self, receipt: ActionReceipt) -> None:
        """Persist the bounded receipt for a claimed key."""


def _coerce_operation(value: GitHubOperation | Sequence[str]) -> GitHubOperation:
    if isinstance(value, GitHubOperation):
        return value
    return GitHubOperation.from_argv(value)


def _validate_operation(
    operation: GitHubOperation,
    *,
    repository: str,
    pull_request: int,
    body: str | None,
) -> bool:
    argv = operation.argv
    if argv[0] != "gh":
        raise InvalidPlanError("every operation must invoke gh directly")
    if any(argument in _FORBIDDEN_OPERATION_ARGS for argument in argv):
        raise InvalidPlanError("admin, force, and branch-deletion operations are forbidden")
    try:
        repo_index = argv.index("--repo")
        operation_repository = argv[repo_index + 1]
    except (ValueError, IndexError) as error:
        raise InvalidPlanError("every operation must bind --repo to the plan repository") from error
    if operation_repository != repository:
        raise InvalidPlanError("operation repository does not match the plan")

    if len(argv) < 3:
        raise InvalidPlanError("operation is incomplete")
    if argv[1] == "pr":
        if len(argv) < 4 or argv[2] not in _ALLOWED_PR_OPERATIONS:
            raise InvalidPlanError("operation is not an existing pull-request mutation")
        try:
            operation_pull_request = int(argv[3])
        except ValueError as error:
            raise InvalidPlanError("pull-request operation must name its PR number") from error
        if operation_pull_request != pull_request:
            raise InvalidPlanError("operation pull request does not match the plan")
    elif argv[1:3] != ("label", "create"):
        raise InvalidPlanError("operation is not an existing review mutation")

    saw_body = False
    for index, argument in enumerate(argv):
        if argument not in {"--body", "--body-file"}:
            continue
        if index + 1 >= len(argv):
            raise InvalidPlanError("body operation is missing its exact value")
        if body is None:
            raise InvalidPlanError("body-bearing operation requires the exact plan body")
        saw_body = True
        if argument == "--body" and argv[index + 1] != body:
            raise InvalidPlanError("operation body does not match the exact plan body")
        if argument == "--body-file" and operation.body != body:
            raise InvalidPlanError("body-file operation must carry the exact plan body")
    if operation.body is not None and not saw_body:
        raise InvalidPlanError("operation body is not represented by its argv")
    return saw_body


@dataclass(frozen=True)
class ActionPlan:
    """Immutable intent bound to one PR head and one human decision."""

    actor: str
    tenant: str
    repository: str
    pull_request: int
    head_sha: str
    action_kind: str
    body: str | None
    operations: tuple[GitHubOperation, ...]
    prerequisites: Prerequisites
    created_at: datetime
    expires_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        actor = _text(self.actor, "actor")
        tenant = _text(self.tenant, "tenant")
        repository = _text(self.repository, "repository")
        if not _REPOSITORY.fullmatch(repository):
            raise InvalidPlanError("repository must be owner/name")
        action_kind = _text(self.action_kind, "action_kind")
        if not _ACTION_KIND.fullmatch(action_kind) or action_kind not in _ALLOWED_ACTION_KINDS:
            raise InvalidPlanError("action_kind is not an existing review mutation")
        if self.body is not None and not isinstance(self.body, str):
            raise InvalidPlanError("body must be an exact Markdown string or None")
        prerequisites = self.prerequisites
        if not isinstance(prerequisites, Prerequisites):
            raise InvalidPlanError("prerequisites must be a Prerequisites value")
        try:
            operations = tuple(_coerce_operation(operation) for operation in self.operations)
        except TypeError as error:
            raise InvalidPlanError("operations must be a sequence") from error
        if not operations:
            raise InvalidPlanError("a plan must contain at least one exact operation")
        if len(operations) > MAX_OPERATIONS:
            raise InvalidPlanError(f"a plan cannot contain more than {MAX_OPERATIONS} operations")
        plan_has_body = False
        for operation in operations:
            plan_has_body = _validate_operation(
                operation,
                repository=repository,
                pull_request=_pull_request(self.pull_request),
                body=self.body,
            ) or plan_has_body
        if self.body is not None and not plan_has_body:
            raise InvalidPlanError("exact plan body is not represented by its operations")
        created_at = _utc(self.created_at, "created_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at <= created_at:
            raise InvalidPlanError("expires_at must be after created_at")
        idempotency_key = _text(self.idempotency_key, "idempotency_key")
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "pull_request", _pull_request(self.pull_request))
        object.__setattr__(self, "head_sha", _head(self.head_sha))
        object.__setattr__(self, "action_kind", action_kind)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "idempotency_key", idempotency_key)

    @classmethod
    def build(
        cls,
        *,
        actor: str,
        tenant: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        action_kind: str,
        body: str | None = None,
        operations: Sequence[GitHubOperation | Sequence[str]],
        prerequisites: Prerequisites | None = None,
        permissions: Mapping[str, object] | None = None,
        checks: Mapping[str, object] | None = None,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> "ActionPlan":
        created = _utc(created_at, "created_at") if created_at else datetime.now(timezone.utc)
        expires = (
            _utc(expires_at, "expires_at")
            if expires_at
            else created + DEFAULT_PLAN_TTL
        )
        resolved_prerequisites = _resolve_prerequisites(prerequisites, permissions, checks)
        resolved_operations = tuple(_coerce_operation(operation) for operation in operations)
        if idempotency_key is None:
            idempotency_key = sha256(
                _canonical(
                    {
                        "actor": actor,
                        "tenant": tenant,
                        "repository": repository,
                        "pull_request": pull_request,
                        "head_sha": head_sha.lower() if isinstance(head_sha, str) else head_sha,
                        "action_kind": action_kind,
                        "body": body,
                        "operations": [
                            {"argv": list(operation.argv), "body": operation.body}
                            for operation in resolved_operations
                        ],
                        "prerequisites": resolved_prerequisites.payload(),
                        "created_at": created.isoformat(),
                        "expires_at": expires.isoformat(),
                    }
                )
            ).hexdigest()
        return cls(
            actor=actor,
            tenant=tenant,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            action_kind=action_kind,
            body=body,
            operations=resolved_operations,
            prerequisites=resolved_prerequisites,
            created_at=created,
            expires_at=expires,
            idempotency_key=idempotency_key,
        )

    @property
    def identity(self) -> str:
        payload = {
            "actor": self.actor,
            "tenant": self.tenant,
            "repository": self.repository,
            "pull_request": self.pull_request,
            "head_sha": self.head_sha,
            "action_kind": self.action_kind,
            "body": self.body,
            "operations": [
                {"argv": list(operation.argv), "body": operation.body}
                for operation in self.operations
            ],
            "prerequisites": self.prerequisites.payload(),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "idempotency_key": self.idempotency_key,
        }
        return sha256(_canonical(payload)).hexdigest()

    @property
    def plan_hash(self) -> str:
        return self.identity

    @property
    def plan_id(self) -> str:
        return self.identity

    def is_expired(self, now: datetime | None = None) -> bool:
        return _now(now) >= self.expires_at

    def preview(self) -> ActionPreview:
        return ActionPreview(
            plan_identity=self.identity,
            actor=self.actor,
            tenant=self.tenant,
            repository=self.repository,
            pull_request=self.pull_request,
            head_sha=self.head_sha,
            action_kind=self.action_kind,
            body=self.body,
            operations=self.operations,
            prerequisites=self.prerequisites,
            created_at=self.created_at,
            expires_at=self.expires_at,
            idempotency_key=self.idempotency_key,
        )

    def confirm_human(
        self,
        *,
        preview: ActionPreview,
        actor: str,
        tenant: str,
        typed_pull_request: int,
        now: datetime | None = None,
    ) -> HumanConfirmation:
        if not isinstance(preview, ActionPreview):
            raise HumanConfirmationRequired("confirmation must follow an ActionPlan preview")
        if preview.plan_identity != self.identity:
            raise HumanConfirmationRequired("preview belongs to another plan")
        confirmed_at = _now(now)
        self._ensure_live(confirmed_at)
        if actor != self.actor:
            raise HumanConfirmationRequired("confirmation actor does not match the plan")
        if tenant != self.tenant:
            raise HumanConfirmationRequired("confirmation tenant does not match the plan")
        if typed_pull_request != self.pull_request:
            raise HumanConfirmationRequired("typed pull request does not match the plan")
        return HumanConfirmation._issue(
            plan_identity=self.identity,
            actor=actor,
            tenant=tenant,
            pull_request=typed_pull_request,
            confirmed_at=confirmed_at,
        )

    def _ensure_live(self, now: datetime) -> None:
        if now < self.created_at:
            raise PlanExpiredError("plan is not valid before its creation time")
        if now >= self.expires_at:
            raise PlanExpiredError("plan has expired")

    def revalidate(
        self,
        current: CurrentState,
        *,
        now: datetime | None = None,
    ) -> None:
        self._ensure_live(_now(now))
        if not isinstance(current, CurrentState):
            raise PlanDriftError("current state is required")
        comparisons = (
            ("actor", self.actor, current.actor),
            ("tenant", self.tenant, current.tenant),
            ("repository", self.repository, current.repository),
            ("pull_request", self.pull_request, current.pull_request),
            ("head", self.head_sha, current.head_sha),
            ("body", self.body, current.body),
            ("permissions", self.prerequisites.permissions, current.prerequisites.permissions),
            ("checks", self.prerequisites.checks, current.prerequisites.checks),
        )
        for field_name, expected, actual in comparisons:
            if expected != actual:
                raise PlanDriftError(f"{field_name} drift invalidates the ActionPlan")

    def execution_eligibility(
        self,
        confirmation: HumanConfirmation,
        current: CurrentState,
        *,
        now: datetime | None = None,
    ) -> ExecutionEligibility:
        if not isinstance(confirmation, HumanConfirmation):
            raise HumanConfirmationRequired(
                "a preview or model-only confirmation cannot authorize execution"
            )
        if confirmation._capability is not _HUMAN_CAPABILITY:
            raise HumanConfirmationRequired("human confirmation capability is invalid")
        if confirmation.plan_identity != self.identity:
            raise HumanConfirmationRequired("confirmation belongs to another plan")
        if confirmation.actor != self.actor or confirmation.tenant != self.tenant:
            raise HumanConfirmationRequired("confirmation authority drifted")
        if confirmation.pull_request != self.pull_request:
            raise HumanConfirmationRequired("confirmation pull request drifted")
        current_time = _now(now)
        self._ensure_live(current_time)
        if confirmation.confirmed_at > current_time:
            raise HumanConfirmationRequired("confirmation is from the future")
        self.revalidate(current, now=current_time)
        return ExecutionEligibility._issue(
            self,
            actor=confirmation.actor,
            tenant=confirmation.tenant,
            now=current_time,
        )

    def execute(
        self,
        eligibility: ExecutionEligibility,
        current: CurrentState,
        executor: Callable[[GitHubOperation], OperationResult | int],
        *,
        ledger: ReceiptLedger,
        now: datetime | None = None,
    ) -> ActionReceipt:
        if not isinstance(eligibility, ExecutionEligibility):
            raise ExecutionNotEligible("execution requires plan-issued eligibility")
        if eligibility._capability is not _EXECUTION_CAPABILITY:
            raise ExecutionNotEligible("execution eligibility capability is invalid")
        if eligibility.plan_identity != self.identity:
            raise ExecutionNotEligible("execution eligibility belongs to another plan")
        if eligibility.idempotency_key != self.idempotency_key:
            raise ExecutionNotEligible("execution idempotency key does not match")
        if eligibility.actor != self.actor or eligibility.tenant != self.tenant:
            raise ExecutionNotEligible("execution authority does not match")
        started_at = _now(now)
        self.revalidate(current, now=started_at)
        if not callable(executor):
            raise ExecutionNotEligible("an operation executor is required")
        if not callable(getattr(ledger, "claim", None)) or not callable(
            getattr(ledger, "record", None)
        ):
            raise ExecutionNotEligible("a caller-owned receipt ledger is required")
        if not ledger.claim(self.idempotency_key):
            raise ExecutionNotEligible("execution idempotency key was already claimed")

        for index, operation in enumerate(self.operations):
            try:
                result = executor(operation)
                if isinstance(result, bool):
                    raise TypeError("executor returned a boolean instead of an operation result")
                if isinstance(result, int):
                    result = OperationResult(return_code=result)
                if not isinstance(result, OperationResult):
                    raise TypeError("executor returned an invalid operation result")
            except Exception as error:
                receipt = ActionReceipt(
                    plan_identity=self.identity,
                    idempotency_key=self.idempotency_key,
                    status="failed",
                    total_operations=len(self.operations),
                    attempted_operations=index + 1,
                    completed_operations=index,
                    started_at=started_at,
                    finished_at=started_at,
                    detail=_bounded_detail(error),
                )
                ledger.record(receipt)
                return receipt
            if result.return_code != 0:
                receipt = ActionReceipt(
                    plan_identity=self.identity,
                    idempotency_key=self.idempotency_key,
                    status="failed",
                    total_operations=len(self.operations),
                    attempted_operations=index + 1,
                    completed_operations=index,
                    started_at=started_at,
                    finished_at=started_at,
                    detail=result.detail,
                )
                ledger.record(receipt)
                return receipt

        receipt = ActionReceipt(
            plan_identity=self.identity,
            idempotency_key=self.idempotency_key,
            status="succeeded",
            total_operations=len(self.operations),
            attempted_operations=len(self.operations),
            completed_operations=len(self.operations),
            started_at=started_at,
            finished_at=started_at,
        )
        ledger.record(receipt)
        return receipt


def build_action_plan(**kwargs: Any) -> ActionPlan:
    """Functional construction entry point for non-class-oriented callers."""

    return ActionPlan.build(**kwargs)


__all__ = [
    "ActionPlan",
    "ActionPlanError",
    "ActionPreview",
    "ActionReceipt",
    "CurrentState",
    "ExecutionEligibility",
    "ExecutionNotEligible",
    "FrozenInstanceError",
    "GitHubOperation",
    "HumanConfirmation",
    "HumanConfirmationRequired",
    "InvalidPlanError",
    "MAX_RECEIPT_DETAIL",
    "OperationResult",
    "PlanDriftError",
    "PlanExpiredError",
    "Prerequisites",
    "ReceiptLedger",
    "build_action_plan",
]
