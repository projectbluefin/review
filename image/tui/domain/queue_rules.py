"""Pure queue-classification rules, no Textual imports.

These decide a pull request's recommended action, which merge-queue segment
it sits in, and how a row is coloured and marked. They consume only the
snapshot/live dicts and the process environment, so the correctness-critical
merge-readiness rules are testable without importing the Textual application.
"""

from __future__ import annotations

from collections.abc import Mapping

from tui.domain.markers import no_color_requested, ui_glyph


def stop_style(action: str, mergeable: str, checks: str, review: str) -> str:
    """The colour a row is worth, from the snapshot's own state fields.

    A hundred rows of identical grey is a queue you read linearly. These
    states are already in every snapshot item, so colour costs nothing and
    turns the list into something scannable: what is ready, what is merely
    stuck behind its own branch, and what nobody can act on yet.
    """
    if no_color_requested():
        return ""
    if mergeable == "dirty":
        return ""
    if checks == "failure":
        return "red"
    if action == "ready-for-human-merge":
        return "bold green"
    if action in ("review", "triage") or review == "approved":
        return "cyan"
    if action == "investigate" or checks == "unknown":
        return "grey62"
    return ""


def ci_marker(checks: str) -> str:
    """Carry the snapshot's CI state as text, not colour alone."""
    return {
        "success": f"{ui_glyph('✓', '+')} CI GREEN",
        "failure": f"{ui_glyph('✗', 'x')} CI FAILED",
        "pending": f"{ui_glyph('…', '.')} CI PENDING",
        "unknown": "? CI UNKNOWN",
    }.get(checks, "? CI UNKNOWN")


def authoritative_checks(live: dict) -> list[dict]:
    """Return the latest run for each stable current-head check context.

    GitHub's pull-request ``statusCheckRollup`` is fetched together with
    ``headRefOid``, so every entry belongs to that exact current head. Reruns
    may leave older entries in the rollup; a check-run context is its workflow
    plus job name, while a commit status context is its context string.
    """
    latest: dict[tuple[str, ...], tuple[tuple[str, str, int], dict]] = {}
    ungrouped: list[dict] = []
    for index, check in enumerate(live.get("statusCheckRollup") or []):
        if not isinstance(check, (dict, Mapping)):
            continue
        typename = str(check.get("__typename") or "")
        name = str(check.get("name") or "")
        context = str(check.get("context") or "")
        if typename == "CheckRun" or name:
            key = ("check-run", str(check.get("workflowName") or ""), name)
        elif typename == "StatusContext" or context:
            key = ("status-context", context)
        else:
            ungrouped.append(check)
            continue
        rank = (
            str(check.get("startedAt") or ""),
            str(check.get("completedAt") or ""),
            index,
        )
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, check)
    return [item[1] for item in sorted(latest.values(), key=lambda item: item[0])] + ungrouped


def effective_check_state(snapshot: str, live: dict) -> str:
    """Prefer fetched check evidence, retaining the snapshot when absent."""
    checks = authoritative_checks(live)
    if not checks:
        return snapshot or "unknown"
    outcomes = [check.get("conclusion") or check.get("state") or "PENDING" for check in checks]
    if any(outcome in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for outcome in outcomes):
        return "failure"
    if any(outcome not in ("SUCCESS", "NEUTRAL", "SKIPPED") for outcome in outcomes):
        return "pending"
    return "success"


def classify_action(check_state: str, mergeable_state: str, review_state: str) -> str:
    """The queue's recommended action, classified from live GitHub evidence.

    First match wins: a failing check is actionable before a conflict is,
    incomplete evidence is a task of its own, and only a fully green,
    approved pull request is ready for a human merge.
    """
    if check_state == "failure":
        return "fix-ci"
    if mergeable_state == "dirty":
        return "resolve-conflicts"
    if "unknown" in (check_state, mergeable_state, review_state):
        return "investigate"
    if review_state == "approved":
        return "ready-for-human-merge"
    return "review"


def classify_queue_item(item: dict) -> str:
    """Which segment of a repository's merge queue this pull request sits in.

    First match wins, and the order is the maintainer's: something already
    handed to the sweep is queued no matter what else is true of it, and a
    conflict outranks a failing check because it blocks the check from
    meaning anything.
    """
    if item.get("mergeable_state") == "dirty":
        return "conflicts"
    if item.get("check_state") == "failure":
        return "ci"
    if "lgtm" in (item.get("labels") or []):
        return "queued"
    if item.get("recommended_action") == "ready-for-human-merge":
        return "ready"
    if item.get("recommended_action") == "review":
        return "review"
    return "unclear"
