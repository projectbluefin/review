"""Pure, optional semantic model for Hive-aware maintainer state.

This is the shared view model behind issue #314: it gives Review a
repository-level, cross-repository picture of a Hive-operated project without
Review ever re-evaluating Hive's own decisions. It is *optional* -- generic
single-repository GitHub Review never imports it, and no base Review schema
depends on it. When Hive is absent every field simply reads as unavailable or
unknown, never inferred.

The model is deliberately read-only. It surfaces authoritative Hive and GitHub
facts and their freshness; it does not plan, admit, rank, schedule, continue,
or merge. See the issue's "Product contract" and "Non-goals" for the boundary.

Design notes
------------
- Every value is either sourced from evidence or explicitly unknown. We never
  fabricate a count or a lifecycle stage to fill a card.
- Freshness and error handling live in one place: :class:`ProviderRead`. A
  read that fails, is stale, or cannot be interpreted collapses the fields it
  touched to ``UNKNOWN`` rather than guessing.
- The canonical Hive lifecycle is mapped only when the evidence justifies it;
  otherwise the model reports ``UNKNOWN`` or ``DEGRADED``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "ProviderRead",
    "ProviderReadState",
    "HiveHealth",
    "Lifecycle",
    "SemanticStatus",
    "ContinuityClassification",
    "ContinuityClaim",
    "AttentionItem",
    "CountField",
    "FactoryState",
    "FactorySummary",
    "build_factory_read",
    "build_factory_state",
    "build_factory_summary",
    "now_utc",
]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^[0-9a-f]{7,40}$")

UNKNOWN = "unknown"
UNKNOWN_LABEL = "UNKNOWN"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SemanticStatus:
    """A machine value paired with a compact human label.

    The value is the canonical key; the label is what the TUI/MCP shows.
    Unknown and degraded states carry their own labels so a card never presents
    a fabricated status.
    """

    value: str
    label: str

    @property
    def is_unknown(self) -> bool:
        return self.value == UNKNOWN

    @property
    def is_degraded(self) -> bool:
        return self.value == "degraded"


# Lifecycle stages the issue's candidate human-facing model maps onto.
ACQUIRING = SemanticStatus("acquiring", "ACQUIRING")
RECONCILING = SemanticStatus("reconciling", "RECONCILING")
CONVERGING = SemanticStatus("converging", "CONVERGING")
AWAITING_OPERATOR = SemanticStatus("awaiting_operator", "AWAITING OPERATOR")
CONVERGED = SemanticStatus("converged", "CONVERGED")
UNKNOWN_LIFECYCLE = SemanticStatus(UNKNOWN, UNKNOWN_LABEL)
DEGRADED_LIFECYCLE = SemanticStatus("degraded", "DEGRADED")


class Lifecycle(str, Enum):
    """Canonical project lifecycle as Hive exposes it."""

    ACQUIRING = "acquiring"
    RECONCILING = "reconciling"
    CONVERGING = "converging"
    AWAITING_OPERATOR = "awaiting_operator"
    CONVERGED = "converged"


_LIFECYCLE_STATUS = {
    Lifecycle.ACQUIRING: ACQUIRING,
    Lifecycle.RECONCILING: RECONCILING,
    Lifecycle.CONVERGING: CONVERGING,
    Lifecycle.AWAITING_OPERATOR: AWAITING_OPERATOR,
    Lifecycle.CONVERGED: CONVERGED,
}


class ProviderReadState(str, Enum):
    """The provider error/freshness contract.

    These are the only six ways a Hive/GitHub read can stand when the model is
    built. Every field on the resulting :class:`FactoryState` is sourced or
    unknown depending on which of these a read produced.
    """

    AVAILABLE = "available"      # fresh, interpreted
    STALE = "stale"              # last known, read failed, age recorded
    UNAVAILABLE = "unavailable"  # hub not configured / never read
    AUTH_FAILED = "auth_failed"  # 401/403
    UNKNOWN = "unknown"          # read but not interpretable

    @property
    def label(self) -> str:
        return {
            ProviderReadState.AVAILABLE: "CURRENT",
            ProviderReadState.STALE: "STALE",
            ProviderReadState.UNAVAILABLE: "UNAVAILABLE",
            ProviderReadState.AUTH_FAILED: "AUTH FAILED",
            ProviderReadState.UNKNOWN: "READ UNKNOWN",
        }[self]


class HiveHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ContinuityClassification(str, Enum):
    ADOPTED = "adopted"          # Continuity owns this PR outright
    CONTINUING = "continuing"    # continuation lane, bounded duplicate suppression
    BLOCKED = "blocked"          # adoption blocked (e.g. real conflict)
    SUPERSEDED = "superseded"    # replaced by a later claim


# Attention reasons that genuinely require human judgment (issue #314).
AUTH_FAILURE = "auth_failure"
CI_FAILURE = "ci_failure"
CONFLICT = "conflict"
CONTRADICTORY = "contradictory"
HELD = "held"
READY = "ready"
STALE_REVIEW = "stale_review"
OPERATOR_DECISION = "operator_decision"
UNKNOWN_EVIDENCE = "unknown_evidence"


@dataclass(frozen=True)
class ProviderRead:
    """How the authoritative provider read stood, and why.

    This is the provider error/freshness contract in one value. It carries
    enough to explain itself to a maintainer without leaking credentials.
    """

    state: ProviderReadState
    age_seconds: float | None = None
    category: str = ""
    message: str = ""

    @property
    def fresh(self) -> bool:
        return self.state is ProviderReadState.AVAILABLE

    @property
    def age_label(self) -> str:
        if self.state is not ProviderReadState.STALE or self.age_seconds is None:
            return ""
        seconds = max(0, int(self.age_seconds))
        return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"

    @property
    def explanation(self) -> str:
        """One human line: state, age, and the authoritative reason if any."""
        parts = [self.state.label]
        if self.state is ProviderReadState.STALE and self.age_label:
            parts.append(f"last known {self.age_label}")
        if self.category and self.category not in parts:
            parts.append(self.category)
        if self.message and self.message not in parts:
            parts.append(self.message)
        return " · ".join(parts)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _nonempty(value: Any) -> str:
    text = _str(value)
    return text.strip() or UNKNOWN


def _sha(value: Any) -> str | None:
    text = _str(value)
    return text if _SHA.fullmatch(text) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(_str(item) for item in value if _str(item).strip())
    text = _str(value)
    return (text,) if text.strip() else ()


def _freshness(value: Any, *, now: datetime | None = None) -> float | None:
    """Seconds since an ISO evidence timestamp, or None if not parseable."""
    text = _str(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now or now_utc()
    return max(0.0, (now - parsed).total_seconds())


def build_provider_read(
    result: Mapping[str, Any] | None,
) -> ProviderRead:
    """Turn a raw provider response into one of the six read states.

    ``result`` is exactly what :func:`hive_api.read_projections` (or a GitHub
    call) returns: ``{"ok": True, "data": {...}}`` or an error envelope
    ``{"ok": False, "category": ..., "message": ...}``. A missing/empty hub is
    ``UNAVAILABLE``; a token that was rejected is ``AUTH_FAILED``; a read that
    failed against last-known facts is ``STALE`` (caller supplies the age).
    """
    if not result:
        return ProviderRead(ProviderReadState.UNAVAILABLE)
    if not isinstance(result, Mapping):
        return ProviderRead(ProviderReadState.UNKNOWN, category="malformed")
    if result.get("ok", False) is not True:
        category = _nonempty(result.get("category"))
        message = _nonempty(result.get("message"))
        if category in {"authentication", "authorization"}:
            return ProviderRead(ProviderReadState.AUTH_FAILED, category=category, message=message)
        if category == "configuration":
            return ProviderRead(ProviderReadState.UNAVAILABLE, category=category, message=message)
        return ProviderRead(ProviderReadState.UNKNOWN, category=category, message=message)
    return ProviderRead(ProviderReadState.AVAILABLE)


@dataclass(frozen=True)
class ContinuityClaim:
    """One Continuity-adopted pull request, as Hive reports it.

    ``slice_owned`` / ``slice_suppressed`` describe bounded duplicate-work
    suppression for linked slices. ``explanation`` is the authoritative reason
    (e.g. "merge conflict", "partial claim #96"). ``head`` is the exact 40-char
    head; ``moved`` is true when the head no longer matches what was adopted.
    """

    repository: str
    number: int
    classification: ContinuityClassification
    head: str | None = None
    slice_owned: tuple[str, ...] = ()
    slice_suppressed: tuple[str, ...] = ()
    explanation: str = UNKNOWN
    moved: bool = False
    contradictory: bool = False

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def state(self) -> str:
        if self.contradictory:
            return "contradictory evidence"
        if self.moved:
            return "head moved since adoption"
        if self.classification is ContinuityClassification.BLOCKED:
            return self.explanation if self.explanation != UNKNOWN else "blocked"
        return self.classification.value

    @classmethod
    def from_mapping(cls, mapping: Any) -> ContinuityClaim | None:
        if not isinstance(mapping, Mapping):
            return None
        repository = _nonempty(mapping.get("repository") or mapping.get("repo"))
        if repository == UNKNOWN:
            repository = ""
        number = _int(mapping.get("number") or mapping.get("pull_request"))
        if number is None:
            return None
        classification = _classification(
            mapping.get("classification") or mapping.get("state")
        )
        head = _sha(mapping.get("head") or mapping.get("head_sha") or mapping.get("exact_head"))
        owned = _string_list(mapping.get("slice_owned") or mapping.get("owned_slices"))
        suppressed = _string_list(
            mapping.get("slice_suppressed") or mapping.get("suppressed_slices")
        )
        explanation = _nonempty(
            mapping.get("explanation") or mapping.get("reason") or mapping.get("note")
        )
        current_head = _str(mapping.get("current_head") or mapping.get("live_head"))
        moved = bool(head) and _sha(current_head) is not None and head != _sha(current_head)
        contradictory = bool(mapping.get("contradictory") or mapping.get("conflict"))
        return cls(
            repository=repository,
            number=number,
            classification=classification,
            head=head,
            slice_owned=owned,
            slice_suppressed=suppressed,
            explanation=explanation,
            moved=moved,
            contradictory=contradictory,
        )


def _classification(value: Any) -> ContinuityClassification:
    text = _str(value).strip().lower()
    for member in ContinuityClassification:
        if member.value == text:
            return member
    return ContinuityClassification.CONTINUING


@dataclass(frozen=True)
class AttentionItem:
    """One thing that needs human judgment (issue #314 "Needs your attention").

    ``reason`` is an authoritative category. ``decision`` names the exact human
    action required. ``evidence`` cites the source fact. Ordinary Hive warnings
    and ordinary work items are *not* attention -- each item must carry a
    ``reason`` that maps to a real decision.
    """

    reason: str
    title: str
    decision: str
    repository: str = ""
    number: int = 0
    evidence: str = ""

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}" if self.repository else self.title


@dataclass(frozen=True)
class CountField:
    """An authoritative count with the definition of what it counts.

    ``value`` is None when the source could not supply it; the card then shows
    UNKNOWN rather than a fabricated number.
    """

    label: str
    value: int | None
    source: str
    definition: str = ""

    @property
    def display(self) -> str:
        return UNKNOWN if self.value is None else str(self.value)


@dataclass(frozen=True)
class FactoryState:
    """Authoritative Hive + GitHub project facts and their freshness.

    A single cross-repository project view. Every field is sourced or unknown;
    the model never infers counts or a lifecycle stage to complete the picture.
    """

    provider: ProviderRead
    project: str = UNKNOWN                       # "projectbluefin/actions" (cross-repo)
    member_repositories: tuple[str, ...] = ()
    health: HiveHealth = HiveHealth.UNAVAILABLE
    runtime: str | None = None
    authority_level: str = UNKNOWN
    control_posture: str = UNKNOWN
    convergence_mode: str = UNKNOWN
    lifecycle: Lifecycle | None = None
    lifecycle_status: SemanticStatus = UNKNOWN_LIFECYCLE
    transition: str = ""                          # current/last meaningful transition
    source_ref: str | None = None                 # generation/ref/SHA Hive supplied
    source_timestamp: str = ""
    freshness_seconds: float | None = None
    confidence: str = UNKNOWN                     # confidence/unknown reason
    frontier: CountField | None = None
    in_flight: CountField | None = None
    blocked: CountField | None = None
    needs_attention: CountField | None = None
    continuity: tuple[ContinuityClaim, ...] = ()
    attention: tuple[AttentionItem, ...] = ()
    acmm_generated_gaps: int | None = None
    acmm_state: str = UNKNOWN
    acmm_last_reconciliation: str = ""
    acmm_reconciliation_freshness: float | None = None
    operator_decisions: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.provider.fresh and self.health is not HiveHealth.UNAVAILABLE

    @property
    def degraded(self) -> bool:
        return self.lifecycle_status.is_degraded or self.health is HiveHealth.DEGRADED


@dataclass(frozen=True)
class FactorySummary:
    """The compact card that answers the control question (issue #314)."""

    project: str
    health: str
    authority: str
    convergence: str
    lifecycle_status: SemanticStatus
    frontier: CountField | None
    in_flight: CountField | None
    blocked: CountField | None
    needs_attention: CountField | None
    continuity: tuple[ContinuityClaim, ...]
    acmm_generated_gaps: int | None
    acmm_state: str
    reconciliation: str
    provider: ProviderRead
    attention: tuple[AttentionItem, ...]

    @property
    def degraded(self) -> bool:
        return self.lifecycle_status.is_degraded or self.health.lower() == "degraded"


# Canonical lifecycle terms Hive might use, mapped to the semantic lifecycle.
_LIFECYCLE_TERMS = {
    "acquiring": Lifecycle.ACQUIRING,
    "reconciling": Lifecycle.RECONCILING,
    "reconcile": Lifecycle.RECONCILING,
    "converging": Lifecycle.CONVERGING,
    "awaiting_operator": Lifecycle.AWAITING_OPERATOR,
    "awaiting operator": Lifecycle.AWAITING_OPERATOR,
    "awaiting_operator_decision": Lifecycle.AWAITING_OPERATOR,
    "converged": Lifecycle.CONVERGED,
}

_HEALTH_TERMS = {
    "healthy": HiveHealth.HEALTHY,
    "green": HiveHealth.HEALTHY,
    "degraded": HiveHealth.DEGRADED,
    "unavailable": HiveHealth.UNAVAILABLE,
    "down": HiveHealth.UNAVAILABLE,
}


def _lifecycle_from(value: Any) -> tuple[Lifecycle | None, SemanticStatus]:
    text = _str(value).strip().lower()
    lifecycle = _LIFECYCLE_TERMS.get(text)
    if lifecycle is None and isinstance(value, Mapping):
        nested = _str(value.get("state") or value.get("name") or value.get("lifecycle"))
        lifecycle = _LIFECYCLE_TERMS.get(_str(nested).strip().lower())
    return lifecycle, (_LIFECYCLE_STATUS[lifecycle] if lifecycle else UNKNOWN_LIFECYCLE)


def _health_from(value: Any) -> HiveHealth:
    if isinstance(value, Mapping):
        value = value.get("health") or value.get("status")
    return _HEALTH_TERMS.get(_str(value).strip().lower(), HiveHealth.UNAVAILABLE)


def _count_from(
    label: str,
    source: str,
    definition: str,
    mapping: Mapping[str, Any],
    *keys: str,
) -> CountField | None:
    for key in keys:
        value = _int(mapping.get(key))
        if value is not None:
            return CountField(label, value, source, definition)
    for key in keys:
        if mapping.get(key) is not None:
            return CountField(label, None, source, definition)
    return None


def build_factory_read(result: Mapping[str, Any] | None) -> ProviderRead:
    """Provider read state for a raw Hive/GitHub response envelope."""
    return build_provider_read(result)


def _continuity_items(data: Mapping[str, Any]) -> list[Any]:
    raw = data.get("continuity") or data.get("adopted_prs") or data.get("continuity_claims")
    if isinstance(raw, Mapping):
        raw = raw.get("claims") or raw.get("prs") or list(raw.values())
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def build_factory_state(
    raw_hive: Mapping[str, Any] | None,
    *,
    provider: ProviderRead | None = None,
    raw_github: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> FactoryState:
    """Assemble a :class:`FactoryState` from authoritative Hive evidence.

    ``raw_hive`` is the interpreted Hive project-state payload (``{"ok": True,
    "data": {...}}``). ``raw_github`` is optional GitHub PR/check/review/hold
    evidence the authoritative provider also observed. When ``provider`` is not
    passed it is derived from ``raw_hive``.

    Nothing here is inferred: any field the evidence does not supply stays
    UNKNOWN, and a non-fresh provider collapses confidence to UNKNOWN.
    """
    raw_hive = raw_hive or {}
    raw_github = raw_github or {}
    if provider is None:
        provider = build_provider_read(raw_hive)

    data = raw_hive.get("data") if isinstance(raw_hive.get("data"), Mapping) else raw_hive

    health = _health_from(data.get("health") or data.get("hive_health"))
    if provider.state is ProviderReadState.UNAVAILABLE:
        health = HiveHealth.UNAVAILABLE
    elif provider.state is ProviderReadState.AUTH_FAILED:
        health = HiveHealth.UNAVAILABLE
    elif provider.state is ProviderReadState.UNKNOWN:
        # We read something but could not interpret it: cannot confirm health.
        health = HiveHealth.DEGRADED

    lifecycle, lifecycle_status = _lifecycle_from(
        data.get("state") or data.get("lifecycle") or data.get("convergence_state")
    )
    if health is HiveHealth.DEGRADED:
        lifecycle_status = DEGRADED_LIFECYCLE
    if provider.state in (ProviderReadState.UNAVAILABLE, ProviderReadState.UNKNOWN):
        lifecycle_status = UNKNOWN_LIFECYCLE
        lifecycle = None

    source_timestamp = _str(
        data.get("generated_at") or data.get("updated_at") or data.get("timestamp")
    )
    freshness = _freshness(source_timestamp, now=now)
    source_ref = (
        _sha(data.get("sha") or data.get("generation_sha") or data.get("ref"))
        or (_REF.fullmatch(_str(data.get("generation") or data.get("ref"))))
    )

    frontier = _count_from(
        "Frontier", "hive", "actionable frontier", data,
        "frontier", "actionable", "actionable_frontier", "actionable_items",
    )
    in_flight = _count_from(
        "In flight", "hive", "work in flight", data,
        "in_flight", "inflight", "in_flight_count", "active",
    )
    blocked = _count_from(
        "Blocked", "hive", "blocked work", data,
        "blocked", "blocked_count", "blocked_items",
    )
    needs_attention = _count_from(
        "Needs you", "hive", "operator-attention items", data,
        "needs_attention", "needs_human", "operator_attention",
    )

    authority = _nonempty(data.get("authority_level") or data.get("authority"))
    control_parts = []
    if authority != UNKNOWN:
        control_parts.append(f"Authority {authority}")
    hold = data.get("pr_hold_gated") or data.get("hold_gated")
    if isinstance(hold, bool):
        control_parts.append("PRs hold-gated" if hold else "PRs not hold-gated")
    auto_merge = data.get("auto_merge")
    if isinstance(auto_merge, bool):
        control_parts.append("auto-merge off" if not auto_merge else "auto-merge on")
    control_posture = " · ".join(control_parts) if control_parts else UNKNOWN

    runtime = _str(data.get("runtime") or data.get("runtime_id") or data.get("runtime_sha"))
    runtime = None if runtime == UNKNOWN else runtime

    convergence = _nonempty(data.get("convergence_mode") or data.get("convergence"))

    member_repos = _string_list(
        data.get("member_repositories") or data.get("repositories") or data.get("member_repos")
    )
    project = _nonempty(data.get("project") or data.get("project_id") or data.get("name"))
    if project == UNKNOWN and member_repos:
        project = member_repos[0]

    transition = _nonempty(data.get("transition") or data.get("last_transition"))
    if transition == UNKNOWN:
        transition = ""
    confidence = _nonempty(data.get("confidence") or data.get("confidence_reason"))
    if confidence == UNKNOWN and provider.state is ProviderReadState.AUTH_FAILED:
        confidence = "authentication failed"

    continuity = tuple(
        claim
        for claim in (ContinuityClaim.from_mapping(item) for item in _continuity_items(data))
        if claim is not None
    )

    attention = _attention_from(raw_github, data, project)

    acmm = data.get("acmm") or data.get("generated_gap_reconciliation") or {}
    if not isinstance(acmm, Mapping):
        acmm = {}
    acmm_generated_gaps = _int(acmm.get("generated_gaps") or acmm.get("generated_gap_count"))
    acmm_state = _nonempty(acmm.get("state") or acmm.get("evaluation"))
    if acmm_state == UNKNOWN:
        acmm_state = _nonempty(data.get("acmm_state"))
    acmm_recon = _str(
        acmm.get("last_reconciliation") or acmm.get("last_run") or acmm.get("reconciliation")
    )
    acmm_freshness = _freshness(acmm_recon, now=now)

    operator_decisions = _string_list(data.get("operator_decisions") or data.get("decisions"))
    uncertainty = _string_list(data.get("uncertainty") or data.get("unknown_reasons"))

    return FactoryState(
        provider=provider,
        project=project,
        member_repositories=member_repos,
        health=health,
        runtime=runtime,
        authority_level=authority,
        control_posture=control_posture,
        convergence_mode=convergence,
        lifecycle=lifecycle,
        lifecycle_status=lifecycle_status,
        transition=transition,
        source_ref=source_ref,
        source_timestamp=source_timestamp,
        freshness_seconds=freshness,
        confidence=confidence,
        frontier=frontier,
        in_flight=in_flight,
        blocked=blocked,
        needs_attention=needs_attention,
        continuity=continuity,
        attention=attention,
        acmm_generated_gaps=acmm_generated_gaps,
        acmm_state=acmm_state,
        acmm_last_reconciliation=acmm_recon,
        acmm_reconciliation_freshness=acmm_freshness,
        operator_decisions=operator_decisions,
        uncertainty=uncertainty,
    )


def _attention_from(
    raw_github: Mapping[str, Any], data: Mapping[str, Any], project: str
) -> tuple[AttentionItem, ...]:
    """Derive maintainer-attention items from GitHub + Hive evidence.

    Only items that map to a real human decision are included; ordinary
    warnings and ordinary work items are excluded. When there is no GitHub
    evidence the returned tuple may be empty (Review stays generic).
    """
    items: list[AttentionItem] = []
    project = project if project != UNKNOWN else ""

    for claim in (
        ContinuityClaim.from_mapping(item)
        for item in _continuity_items(data)
        if isinstance(item, Mapping)
    ):
        if claim.contradictory:
            items.append(AttentionItem(
                reason=CONTRADICTORY,
                title=f"{claim.key} contradictory Continuity evidence",
                decision="Reconcile the conflicting Continuity claims",
                repository=claim.repository,
                number=claim.number,
                evidence="contradictory evidence",
            ))
        elif claim.classification is ContinuityClassification.BLOCKED:
            items.append(AttentionItem(
                reason=CONFLICT,
                title=f"{claim.key} adoption blocked",
                decision="Resolve the conflict or re-point the Continuity lane",
                repository=claim.repository,
                number=claim.number,
                evidence=claim.explanation if claim.explanation != UNKNOWN else "admission blocked",
            ))
        if claim.moved and not claim.contradictory:
            items.append(AttentionItem(
                reason=STALE_REVIEW,
                title=f"{claim.key} head moved since adoption",
                decision="Rerun the reviewed decision on the new head",
                repository=claim.repository,
                number=claim.number,
                evidence="head moved since adoption",
            ))

    if isinstance(raw_github, Mapping):
        live_pr = None
        for key in ("pull_request", "pr", "current_pr"):
            value = raw_github.get(key)
            if isinstance(value, Mapping):
                live_pr = value
                break
        if isinstance(live_pr, Mapping):
            number = _int(live_pr.get("number")) or 0
            review = _str(live_pr.get("reviewDecision") or live_pr.get("review_state") or "").lower()
            merge = _str(live_pr.get("mergeable_state") or live_pr.get("merge_state") or "").lower()
            check = _str(live_pr.get("check_state") or live_pr.get("ci") or "").lower()
            if merge in {"conflicting", "dirty", "blocked"}:
                items.append(AttentionItem(
                    reason=CONFLICT,
                    title=f"PR #{number} has merge conflicts",
                    decision="Resolve conflicts or request changes",
                    repository=project,
                    number=number,
                    evidence="mergeable_state conflicts",
                ))
            elif review == "review_required" and check == "success":
                items.append(AttentionItem(
                    reason=READY,
                    title=f"PR #{number} ready for maintainer review",
                    decision="Review and approve or request changes",
                    repository=project,
                    number=number,
                    evidence="review required, CI green",
                ))
            elif check == "failure":
                items.append(AttentionItem(
                    reason=CI_FAILURE,
                    title=f"PR #{number} CI failing",
                    decision="Judge whether the CI failure is real and actionable",
                    repository=project,
                    number=number,
                    evidence="check_state failure",
                ))

    for decision in _string_list(data.get("operator_decisions") or data.get("decisions")):
        items.append(AttentionItem(
            reason=OPERATOR_DECISION,
            title=decision,
            decision="Confirm or override the explicit operator decision",
            evidence=decision,
        ))
    for reason in _string_list(data.get("uncertainty") or data.get("unknown_reasons")):
        items.append(AttentionItem(
            reason=UNKNOWN_EVIDENCE,
            title=f"Unknown: {reason}",
            decision="Supply the missing evidence to remove the uncertainty",
            evidence=reason,
        ))
    return tuple(items)


def build_factory_summary(state: FactoryState) -> FactorySummary:
    """Render the compact control card from a :class:`FactoryState`."""
    return FactorySummary(
        project=state.project,
        health=state.health.value,
        authority=state.authority_level,
        convergence=state.convergence_mode,
        lifecycle_status=state.lifecycle_status,
        frontier=state.frontier,
        in_flight=state.in_flight,
        blocked=state.blocked,
        needs_attention=state.needs_attention,
        continuity=state.continuity,
        acmm_generated_gaps=state.acmm_generated_gaps,
        acmm_state=state.acmm_state,
        reconciliation=_reconciliation_label(state),
        provider=state.provider,
        attention=state.attention,
    )


def _reconciliation_label(state: FactoryState) -> str:
    if state.acmm_last_reconciliation == "":
        return state.acmm_state if state.acmm_state != UNKNOWN else UNKNOWN
    if state.acmm_reconciliation_freshness is None:
        return f"{state.acmm_state} · last run unknown age"
    seconds = int(state.acmm_reconciliation_freshness)
    age = f"{seconds}s ago" if seconds < 60 else f"{seconds // 60}m ago"
    return f"{state.acmm_state} · {age}"
