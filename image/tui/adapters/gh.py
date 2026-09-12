"""I/O adapters for the review dashboard: GitHub ``gh`` and the Hive HTTP API.

This is the single home for the network and subprocess edges of the
dashboard. It imports the rest of ``tui.*`` (``gh_client``, ``hive_api``) but
nothing Textual, so the edges stay testable in their own right and the pure
domain rules never have to pull in a network call.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from urllib.parse import urlsplit

from tui import hive_api
from tui.gh_client import gh as gh_client_read
from tui.gh_client import run_mutation as gh_client_run_mutation

# How long a stopped review has to die politely before it is killed.
STOP_GRACE_SECONDS = 5.0

# The live evidence the gh pr view call fetches for one pull request.
LIVE_PR_FIELDS = (
    "author,state,baseRefOid,headRefOid,isDraft,mergeable,mergeStateStatus,"
    "reviewDecision,additions,deletions,changedFiles,updatedAt,body,"
    "closingIssuesReferences,statusCheckRollup,labels,reviews,"
    "isCrossRepository,maintainerCanModify"
)

MUTATION_TIMEOUT = 60
HIVE_TIMEOUT = 15


def bounded_detail(detail: str) -> str:
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", str(detail))
    return " ".join(detail.split())[:240]


def hive_api_base() -> str:
    """The selected hub's HTTPS root.

    The launcher may select a registered deployment, and the image hook
    supplies the default. Token-bearing dashboard requests never use plaintext
    transport or URLs containing user information.
    """
    hub = os.environ.get("HIVE_HUB", "")
    if "," in hub:
        return ""
    if hub.startswith("wss://"):
        http = "https://" + hub[len("wss://") :]
    elif hub.startswith("https://"):
        http = hub
    else:
        return ""
    try:
        parsed = urlsplit(http)
        if not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
    except ValueError:
        return ""
    return http[: -len("/contribute")] if http.endswith("/contribute") else http


def gh(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # Bare subprocess.run calls replaced by throttled gh_client.read
    return gh_client_read(*args, timeout=timeout)


def _run_mutation(
    command: list[str] | Sequence[str],
    timeout: int = MUTATION_TIMEOUT,
    idempotent: bool = False,
) -> subprocess.CompletedProcess:
    # Bare subprocess.run(command) replaced by gh_client.run_mutation with deadlines
    return gh_client_run_mutation(command, timeout=timeout, idempotent=idempotent)


def fetch_live_review(repository: str, number: int) -> dict:
    result = gh(
        "pr",
        "view",
        str(number),
        "--repo",
        repository,
        "--json",
        LIVE_PR_FIELDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            bounded_detail(
                (result.stderr or result.stdout).strip()
                or f"GitHub could not read {repository}#{number}"
            )
        )
    try:
        live = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError(
            f"GitHub returned malformed evidence for {repository}#{number}"
        ) from error
    if not isinstance(live, dict):
        raise ValueError(
            f"GitHub returned malformed evidence for {repository}#{number}"
        )
    return live


def hive_token() -> str:
    """The hub bearer token: GH_TOKEN when exported, else the host's own gh
    login. The dashboard runs where the maintainer is already authed with
    gh; requiring a second, separately exported token is how a connected
    hub reads as unreachable. Read-only either way."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    try:
        result = gh("auth", "token", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def hive_get(path: str) -> hive_api.Result:
    """Read one hub endpoint. Read-only, and never fatal.

    Consulting Hive must not be able to break the dashboard. The result keeps
    routing, authentication, authorization, network, malformed-response, and
    server failures distinct without exposing credentials.
    """
    base = hive_api_base()
    token = hive_token()
    if not base:
        return hive_api.Result(False, "configuration", "not configured", {})
    return hive_api.request(f"{base}{path}", token, timeout=HIVE_TIMEOUT)
