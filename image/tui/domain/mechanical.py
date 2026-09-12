"""Pure mechanical-review classification, no Textual imports.

These decide whether a pull request is safe to *update* (rebase-merge onto a
green branch) and how to normalise a dependency title. Every signal is
GitHub's own account of the pull request's state or Renovate's own metadata,
so the merge-readiness rules are pure string/dict in and out and are
testable without importing the Textual application.
"""

from __future__ import annotations

import os
import re

from tui.domain.queue_rules import authoritative_checks

# The bot whose pull requests can be classified as mechanical. The login is
# configurable because the Renovate installation differs per deployment: this
# organisation runs it as `app/mergeraptor`, and hard-coding one name is how a
# correct classifier silently matches nothing somewhere else.
RENOVATE_BOTS = frozenset(
    login.strip().lower()
    for login in os.environ.get(
        "BLUEFIN_REVIEW_RENOVATE_BOTS",
        "app/mergeraptor,app/renovate,renovate[bot],renovate-bot",
    ).split(",")
    if login.strip()
)

# The update types current policy already covers. A major update is a semantic
# decision about the dependency, so it never qualifies for a mechanical branch
# update no matter how green the branch is.
MECHANICAL_UPDATE_TYPES = frozenset({"digest", "pin", "patch", "minor"})

# A check that says anything else — running, queued, failed, absent — is not
# evidence that the branch is currently green.
MECHANICAL_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def reviewer_standing(association: str) -> str:
    """Maintainer or community, from GitHub's own author association.

    GitHub already decides this per review: OWNER, MEMBER and COLLABORATOR
    carry write access to the repository, everything else does not. Reading
    it off the review costs nothing, where asking the permissions API costs
    one round trip per reviewer per stop.
    """
    return "maintainer" if association in MAINTAINER_ASSOCIATIONS else "community"


def dependency_subject(title: str) -> str | None:
    """Normalise a title down to the dependency it updates (walker parity)."""
    s = title.lower()
    s = re.sub(r"^\w+(\([^)]*\))?:\s*", "", s)
    for pattern in (
        r"update module\s+(\S+)",
        r"update dependency\s+(\S+)",
        r"update\s+(\S+)\s+docker\s+(?:tag|digest)",
        r"update\s+(\S+)\s+action",
        r"update\s+(\S+)\s+digest",
        r"update\s+(\S+)\s+to\s+v?[\d.]",
    ):
        found = re.search(pattern, s)
        if found:
            return re.sub(r":[^:/]*$", "", found.group(1).strip())
    return None


def renovate_update_types(body: str) -> set[str]:
    """The update types Renovate declares in its own pull request body.

    Renovate writes one row per updated package into a `| Package | Update |
    Change |` table, and the Update cell carries the type it decided on.
    Reading that cell is not an inference from the title: it is the bot's own
    metadata about what it changed.
    """
    types: set[str] = set()
    columns: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            columns = []
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        lowered = [cell.lower() for cell in cells]
        if "update" in lowered and "package" in lowered:
            columns = lowered
            continue
        if not columns or set(line) <= set("|-: "):
            continue
        index = columns.index("update")
        if index < len(cells) and cells[index]:
            types.add(cells[index].lower())
    return types


def mechanical_reason(author: str, live: dict) -> str | None:
    """Why this branch is safe to *update*, or None when it is not.

    MECHANICAL describes exactly one operation — merging the base branch into
    a green, mergeable branch that is merely behind — and says nothing about
    whether the dependency change itself should be approved or merged. Every
    signal below is live GitHub evidence or Renovate's own metadata. A
    dependency-shaped title proves nothing and is deliberately not consulted:
    that heuristic is duplicate evidence, not a safety boundary.
    """
    if not live:
        return None
    login = (author or (live.get("author") or {}).get("login") or "").lower()
    if login not in RENOVATE_BOTS:
        return None
    if (live.get("state") or "OPEN").upper() != "OPEN":
        return None
    if live.get("isDraft"):
        return None
    if (live.get("mergeable") or "").upper() != "MERGEABLE":
        return None
    if (live.get("mergeStateStatus") or "").upper() != "BEHIND":
        return None
    checks = authoritative_checks(live)
    if not checks:
        return None
    for check in checks:
        outcome = str(check.get("conclusion") or check.get("state") or "").upper()
        if outcome not in MECHANICAL_CHECK_OK:
            return None
    types = renovate_update_types(live.get("body") or "")
    if not types or not types <= MECHANICAL_UPDATE_TYPES:
        return None
    kinds = "/".join(sorted(types))
    return f"{kinds} update by {login}, every check green, mergeable but behind"
