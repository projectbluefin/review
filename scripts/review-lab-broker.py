#!/usr/bin/env python3
"""Host-side lab broker: the container asks for outcomes, the host acts.

`just review-queue` starts this process on the host before the sandboxed
container exists, and hands the container exactly one thing: a unix socket.
The kubeconfig, the `kubectl`/`argo`/`gh` binaries, the Kubernetes
credentials and the GitHub issue-write token all stay on this side of the
socket. An agent that talks its way past its own instructions still cannot
reach the cluster except through the three verbs below, because there is no
verb that carries a command.

That is the whole design: the caller names an outcome, never an
implementation. Nothing a caller sends is interpolated into a shell, a
manifest, a namespace, a resource name, a kubectl argument, or a template
name. The only caller-supplied values that reach a host argv are a
repository, a pull request number, a 40-hex head sha, and a profile name
that must be a key of SAFE_PROFILES; every one of them is matched against a
fixed pattern first, and every host command runs as an explicit argv list.

Lab use is optional and never blocks a review. Every failure path here ends
in a well-formed answer — DEGRADED, `not-applicable`, or `unavailable` — so
an unreachable cluster costs a reviewer nothing but the evidence it would
have added.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = 1

# Three actions, and no fourth. An earlier draft carried a `capabilities`
# call and a `list_profiles` call; design review collapsed both into
# `status`, because every extra verb is another thing an agent can probe and
# another shape the container has to trust. `status` carries the profile
# list, so a caller learns what it may ask for from the same answer that
# tells it whether asking is worthwhile.
ACTIONS = ("status", "health", "submit")

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 262144

# A client that connects and says nothing must not hold a worker thread, and
# a client that stops reading must not hold one either.
READ_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 30.0
RECV_CHUNK_BYTES = 8192
MAX_DRAIN_BYTES = 4 * 1024 * 1024

KUBECTL_TIMEOUT_SECONDS = 15
ARGO_TIMEOUT_SECONDS = 60
GH_TIMEOUT_SECONDS = 30
REGISTRY_TIMEOUT_SECONDS = 15

DEFAULT_NAMESPACE = "argo"

# The lab publishes its USB4 mesh state as node annotations rather than as a
# service, so reading it costs one node listing the broker already needs.
USB4_NODES = ("ghost", "exo-0")
USB4_LINK_ANNOTATION = "lab.projectbluefin.io/usb4-link"
USB4_OBSERVED_ANNOTATION = "lab.projectbluefin.io/usb4-link-observed-at"
USB4_LINK_VALUES = ("up", "down")
# The annotation is rewritten by a node-local agent on a short cycle, so a
# sample older than this is not evidence of the current link — it is
# evidence that something stopped writing.
USB4_FRESH_SECONDS = 45

TERMINAL_WORKFLOW_PHASES = ("Succeeded", "Failed", "Error", "Skipped")
FAILED_WORKFLOW_PHASES = ("Failed", "Error")
FAILING_POD_PHASES = ("Failed", "Unknown")

# Every list in an evidence block is capped: the response has a hard size
# limit, and a cluster with four hundred failing pods must produce the same
# shape of answer as one with two.
MAX_EVIDENCE_NAMES = 10
MAX_EVIDENCE_ROWS = 10
MAX_PROMETHEUS_SAMPLES = 8
MAX_FINDINGS = 8

# Prometheus is read through the Kubernetes API service proxy and nowhere
# else: the broker holds cluster credentials, not a private endpoint, and a
# direct URL would be exactly the maintainer-local dependency this appliance
# refuses to grow. The query is chosen from this map by key; a request can
# never name one.
PROMETHEUS_PROXY_PATH = (
    "/api/v1/namespaces/monitoring/services/prometheus-operated:9090/proxy/api/v1/query"
)
PROMETHEUS_QUERIES = {
    "failed-pods": 'sum(kube_pod_status_phase{phase="Failed"})',
    "up": "up",
}
# Only these label keys survive into a response. Prometheus labels carry
# node addresses in `instance`, and an address the container never needed is
# an address it should never receive.
PROMETHEUS_LABEL_KEYS = ("job", "phase", "namespace", "node")

# The safe profile map. This is one explicit constant, not discovery: a
# broker that lists WorkflowTemplates and offers what it finds would offer
# whatever anyone installed, which is the opposite of a boundary. Only
# QA/test/verification templates belong here.
SAFE_PROFILES = {
    "bluefin-qa-pipeline": {
        "template": "bluefin-qa-pipeline",
        "needs_image": True,
        "repositories": (
            "projectbluefin/bluefin",
            "projectbluefin/bluefin-lts",
            "projectbluefin/dakota",
        ),
    },
    "k8sgpt-on-demand": {
        "template": "k8sgpt-on-demand",
        "needs_image": False,
        # An empty tuple means the profile applies to any repository: cluster
        # diagnosis is about the lab, not about whose pull request prompted
        # the look.
        "repositories": (),
    },
}

# A template whose name contains one of these words changes the lab instead
# of testing it. The guard runs over SAFE_PROFILES at import, so an edit
# that adds `install-lab` or `teardown-nodes` to the map fails on the
# author's machine rather than in front of a cluster.
REFUSED_TEMPLATE_WORDS = (
    "install",
    "publish",
    "teardown",
    "toggle",
    "debug",
    "destroy",
    "reset",
    "upgrade",
)

# k8sgpt is reached only by submitting this template. The broker never runs
# the `k8sgpt` binary, never passes an AI backend credential, and never uses
# an explain/AI flag: the workflow owns whatever backend the lab configured,
# and the broker owns none of it.
DIAGNOSIS_PROFILE = "k8sgpt-on-demand"

# What gets pinned into a workflow is always the real registry. The API base
# is a separate host-side constant so the resolver can be exercised end to
# end against a local server; it is host configuration and is never taken
# from a request.
GHCR_REFERENCE_HOST = "ghcr.io"
GHCR_API_BASE = os.environ.get("BLUEFIN_REVIEW_GHCR_BASE", "https://ghcr.io").rstrip("/")
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# Findings are routed by what broke, not by who asked. A fault filed against
# a repository that does not own it costs a maintainer more than not filing
# it at all, so anything this map cannot place is reported back unrouted and
# nothing is opened.
FINDING_ROUTES = {
    "cluster-platform": "projectbluefin/lab",
    "server-product": "projectbluefin/server",
}
PLATFORM_NAMESPACES = (
    "argo",
    "buildbarn",
    "cert-manager",
    "kube-node-lease",
    "kube-public",
    "kube-system",
    "longhorn-system",
    "metallb-system",
    "monitoring",
    "tigera-operator",
)
PRODUCT_NAMESPACES = (
    "bluefin-server",
    "server",
)

# One sample is a glitch; two is a pattern. Filing on first sight turns a
# rebooting node into an issue nobody wanted, so a finding must be observed
# twice before it reaches GitHub.
MIN_OBSERVATIONS_BEFORE_FILING = 2
COMMENT_INTERVAL_SECONDS = 3600
FINGERPRINT_VERSION = "v1"
MARKER_TEMPLATE = "<!-- bluefin-review-lab-finding: {version} {fingerprint} -->"
STATE_LOCK_TIMEOUT_SECONDS = 30

# Evidence that reaches GitHub is bounded twice: by lines and by characters.
MAX_ISSUE_EVIDENCE_LINES = 40
MAX_ISSUE_EVIDENCE_CHARS = 4000
MAX_DETAIL_CHARS = 240

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
WORKFLOW_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
LABEL_VALUE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Applied to every string this module sends to GitHub, and to every detail
# built from a host tool's own output. Tool stderr is where a server URL or
# a token leaks from, and an issue body is public.
REDACTIONS = (
    (re.compile(r"https?://\S+"), "[redacted-url]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[redacted-address]"),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{10,}"), "[redacted-token]"),
    (
        re.compile(
            r"(?i)\b(?:authorization|bearer|token|password|passwd|secret|api[-_]?key)\b"
            r"\s*[:=]?\s*\S+"
        ),
        "[redacted-secret]",
    ),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b"), "[redacted-opaque]"),
)


class Rejected(Exception):
    """A request the protocol refuses. Carries the wire error code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class BrokerContext:
    """Everything a handler may know: session, namespace, allowed repositories.

    Deliberately not a bag of open configuration — a handler that cannot
    reach host paths or credentials cannot leak them.
    """

    def __init__(self, session: str, namespace: str = DEFAULT_NAMESPACE, repositories=()) -> None:
        self.session = session
        self.namespace = namespace
        self.repositories = tuple(repositories)

    def allows(self, repository: str) -> bool:
        return not self.repositories or repository in self.repositories


class HostResult:
    """One host command's outcome, with failure folded into data.

    `reason` is non-empty whenever the command did not complete cleanly, so
    a caller degrades by reading a field instead of by catching something.
    """

    def __init__(self, argv, code: int, stdout: str = "", stderr: str = "", reason: str = "") -> None:
        self.argv = tuple(argv)
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.reason


def verify_profile_map(profiles=None) -> None:
    """Refuse a profile map that names a template this broker may not run."""
    for name, profile in (SAFE_PROFILES if profiles is None else profiles).items():
        template = str(profile.get("template", ""))
        if not template or not PROFILE_RE.match(template):
            raise RuntimeError(f"profile {name!r} has no usable template name")
        if is_refused_template(template):
            raise RuntimeError(
                f"profile {name!r} names template {template!r}, which the safety guard refuses"
            )


def is_refused_template(template: str) -> bool:
    lowered = str(template).lower()
    return any(word in lowered for word in REFUSED_TEMPLATE_WORDS)


def redact(text: str) -> str:
    value = str(text)
    for pattern, replacement in REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def brief(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """One short, redacted line. Details are for humans, not for transport."""
    collapsed = " ".join(redact(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def bound_block(text: str, max_lines: int = MAX_ISSUE_EVIDENCE_LINES, max_chars: int = MAX_ISSUE_EVIDENCE_CHARS) -> str:
    lines = redact(text).splitlines()[:max_lines]
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block


def ok_payload(**fields) -> dict:
    payload = {"version": PROTOCOL_VERSION, "ok": True}
    payload.update(fields)
    return payload


def error_payload(code: str, detail: str) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": code,
        "detail": brief(detail),
    }


def json_line(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def bounded_response(payload: dict) -> bytes:
    """Serialize within the wire cap, dropping evidence before truth.

    Dropping is ordered from the most expendable section to the least, and
    it is always announced: a caller that cannot see `truncated` would read
    a shrunken evidence block as a healthy one.
    """
    line = json_line(payload)
    if len(line) <= MAX_RESPONSE_BYTES:
        return line
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        for section in ("prometheus", "node_usage", "workflows", "pods", "nodes"):
            if section in evidence:
                evidence.pop(section)
                evidence["truncated"] = True
                line = json_line(payload)
                if len(line) <= MAX_RESPONSE_BYTES:
                    return line
    if isinstance(payload.get("findings"), list) and payload["findings"]:
        payload["findings"] = []
        payload["detail"] = brief("findings dropped: response exceeded the protocol size limit")
        line = json_line(payload)
        if len(line) <= MAX_RESPONSE_BYTES:
            return line
    minimal = {"version": PROTOCOL_VERSION, "ok": bool(payload.get("ok", True))}
    for key in ("state", "result", "error"):
        if key in payload:
            minimal[key] = payload[key]
    if "evidence" in payload:
        minimal["evidence"] = {"truncated": True}
    if "findings" in payload:
        minimal["findings"] = []
    minimal["detail"] = "response exceeded the protocol size limit; content dropped"
    return json_line(minimal)


def run_host(argv, timeout: int) -> HostResult:
    """The one place this module executes anything.

    Explicit argv, never a shell string; stdin closed so a tool that decides
    to prompt (an expired `gh` login) fails instead of holding the socket;
    output captured so nothing lands on the maintainer's terminal.
    """
    try:
        done = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return HostResult(argv, 127, reason=f"{argv[0]} is not installed on the host")
    except subprocess.TimeoutExpired:
        return HostResult(argv, 124, reason=f"{argv[0]} timed out after {timeout}s")
    except OSError as failure:
        return HostResult(argv, 126, reason=f"{argv[0]} could not run: {failure.strerror}")
    reason = "" if done.returncode == 0 else f"{argv[0]} exited {done.returncode}"
    if reason and done.stderr.strip():
        reason = f"{reason}: {brief(done.stderr, 120)}"
    return HostResult(argv, done.returncode, done.stdout, done.stderr, reason)


def kubectl(args, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> HostResult:
    return run_host(["kubectl", *args], timeout)


def kubectl_json(args, timeout: int = KUBECTL_TIMEOUT_SECONDS):
    """Run a kubectl query and parse it, returning (payload, reason)."""
    result = kubectl(args, timeout)
    if not result.ok:
        return None, brief(result.reason or "kubectl failed", 120)
    try:
        return json.loads(result.stdout), ""
    except ValueError:
        return None, "kubectl returned output that is not JSON"


def parse_rfc3339(value):
    """Parse an RFC3339 stamp, or return None.

    A naive stamp is treated as unparseable rather than assumed to be UTC:
    guessing the writer's zone silently ages or rejuvenates the sample, and
    freshness is the entire value of this annotation.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(datetime.timezone.utc)


def index_nodes(payload):
    nodes = {}
    if not isinstance(payload, dict):
        return nodes
    for item in payload.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("metadata") or {}).get("name")
        if isinstance(name, str) and name:
            nodes[name] = item
    return nodes


def node_is_ready(node) -> bool:
    for condition in ((node.get("status") or {}).get("conditions") or []):
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def usb4_report(nodes: dict, now: datetime.datetime) -> dict:
    """The USB4 mesh as the dashboard renders it: per-node link plus one flag.

    A link value is only reported when this broker can date it to the recent
    past. Missing, malformed, future-dated and stale samples all collapse to
    "unknown", because a value the host cannot date is indistinguishable
    from no observation — and a stale "up" is the one answer that would
    convince a maintainer the mesh is fine while it is not.
    """
    report = {}
    fresh = True
    for name in USB4_NODES:
        node = nodes.get(name) or {}
        annotations = (node.get("metadata") or {}).get("annotations") or {}
        link = annotations.get(USB4_LINK_ANNOTATION)
        observed = parse_rfc3339(annotations.get(USB4_OBSERVED_ANNOTATION))
        usable = False
        if link in USB4_LINK_VALUES and observed is not None:
            age = (now - observed).total_seconds()
            usable = 0 <= age <= USB4_FRESH_SECONDS
        report[name] = link if usable else "unknown"
        fresh = fresh and usable
    report["fresh"] = fresh
    return report


def workflow_phase(item) -> str:
    phase = ((item.get("status") or {}).get("phase")) if isinstance(item, dict) else ""
    return phase if isinstance(phase, str) else ""


def workflow_name(item) -> str:
    name = ((item.get("metadata") or {}).get("name")) if isinstance(item, dict) else ""
    return name if isinstance(name, str) else ""


def session_workflows(context: BrokerContext):
    return kubectl_json(
        [
            "get",
            "workflows.argoproj.io",
            "-n",
            context.namespace,
            "-l",
            f"review.session={sanitize_label_value(context.session)}",
            "-o",
            "json",
        ]
    )


def sanitize_label_value(value: str) -> str:
    """A Kubernetes label value, derived rather than accepted.

    Callers supply repositories and shas that are already pattern-matched;
    this is the second gate, so a label can never carry a comma, a space, or
    anything else that would change how `argo` reads its own argument.
    """
    cleaned = LABEL_VALUE_RE.sub("-", str(value)).strip("-._")
    return cleaned[:63]


def require_repository(request: dict) -> str:
    value = request.get("repository")
    if not isinstance(value, str) or not REPOSITORY_RE.match(value):
        raise Rejected("bad-request", "repository must be owner/repo")
    return value


def require_pr(request: dict) -> int:
    value = request.get("pr")
    # bool is an int in Python, and `true` is not a pull request number.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Rejected("bad-request", "pr must be a positive integer")
    return value


def require_head(request: dict) -> str:
    value = request.get("head")
    if not isinstance(value, str) or not HEAD_RE.match(value):
        raise Rejected("bad-request", "head must be a 40 character lowercase hex sha")
    return value


def require_profile(request: dict) -> str:
    value = request.get("profile")
    if not isinstance(value, str) or not PROFILE_RE.match(value):
        raise Rejected("bad-request", "profile must be a plain profile name")
    return value


def decode_request(raw: bytes, context: BrokerContext) -> dict:
    if len(raw) > MAX_REQUEST_BYTES:
        raise Rejected("bad-request", f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected("bad-request", "request is not valid JSON")
    if not isinstance(request, dict):
        raise Rejected("bad-request", "request is not a JSON object")
    version = request.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise Rejected("bad-request", "version is missing or not an integer")
    if version != PROTOCOL_VERSION:
        raise Rejected("unsupported-version", f"this broker speaks version {PROTOCOL_VERSION}")
    action = request.get("action")
    if not isinstance(action, str) or not action:
        raise Rejected("bad-request", "action is missing or not a string")
    if action not in ACTIONS:
        raise Rejected("unknown-action", "actions are status, health, submit")
    session = request.get("session")
    if not isinstance(session, str) or not session:
        raise Rejected("bad-request", "session is missing or not a string")
    # The session id is the only thing distinguishing this container's
    # requests from another's on a shared host, so it is compared without a
    # timing signal.
    if not hmac.compare_digest(session, context.session):
        raise Rejected("wrong-session", "request session does not match this broker")
    return request


def handle_status(context: BrokerContext, request: dict) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    unavailable = []

    nodes_payload, nodes_reason = kubectl_json(["get", "nodes", "-o", "json"])
    if nodes_reason:
        unavailable.append(f"nodes: {nodes_reason}")
    workflows_payload, workflows_reason = session_workflows(context)
    if workflows_reason:
        unavailable.append(f"workflows: {workflows_reason}")

    active = False
    for item in (workflows_payload or {}).get("items", []) or []:
        if workflow_phase(item) not in TERMINAL_WORKFLOW_PHASES:
            active = True
            break

    usb4 = usb4_report(index_nodes(nodes_payload), now)
    state = "DEGRADED" if unavailable else "READY"
    detail = "; ".join(unavailable) if unavailable else "cluster reachable"
    return ok_payload(
        state=state,
        active=active,
        profiles=sorted(SAFE_PROFILES),
        usb4=usb4,
        detail=brief(detail),
    )


def collect_evidence(context: BrokerContext, now: datetime.datetime) -> dict:
    """Bounded, machine-readable cluster evidence.

    Every section is optional: a probe that fails records a short reason in
    `unavailable` and leaves the rest of the answer intact, because partial
    evidence is still evidence and a missing metrics API is not an outage.
    """
    evidence = {"unavailable": []}

    nodes_payload, nodes_reason = kubectl_json(["get", "nodes", "-o", "json"])
    nodes = index_nodes(nodes_payload)
    # The USB4 annotations ride on the node listing this section already
    # fetched, so health never pays for a second node query.
    evidence["usb4"] = usb4_report(nodes, now)
    if nodes_reason:
        evidence["unavailable"].append(f"nodes: {nodes_reason}")
    else:
        not_ready = sorted(name for name, node in nodes.items() if not node_is_ready(node))
        evidence["nodes"] = {
            "total": len(nodes),
            "ready": len(nodes) - len(not_ready),
            "not_ready": not_ready[:MAX_EVIDENCE_NAMES],
        }

    pods_payload, pods_reason = kubectl_json(["get", "pods", "--all-namespaces", "-o", "json"])
    if pods_reason:
        evidence["unavailable"].append(f"pods: {pods_reason}")
    else:
        phases = {}
        failing = []
        for item in (pods_payload or {}).get("items", []) or []:
            metadata = item.get("metadata") or {}
            phase = (item.get("status") or {}).get("phase") or "Unknown"
            phases[phase] = phases.get(phase, 0) + 1
            if phase in FAILING_POD_PHASES:
                failing.append(f"{metadata.get('namespace', '')}/{metadata.get('name', '')}")
        evidence["pods"] = {
            "phases": phases,
            "failing_total": len(failing),
            "failing": sorted(failing)[:MAX_EVIDENCE_NAMES],
        }

    workflows_payload, workflows_reason = kubectl_json(
        ["get", "workflows.argoproj.io", "-n", context.namespace, "-o", "json"]
    )
    if workflows_reason:
        evidence["unavailable"].append(f"workflows: {workflows_reason}")
    else:
        items = (workflows_payload or {}).get("items", []) or []
        failed = sorted(
            workflow_name(item) for item in items if workflow_phase(item) in FAILED_WORKFLOW_PHASES
        )
        evidence["workflows"] = {
            "sampled": len(items),
            "failed_total": len(failed),
            "failed": failed[:MAX_EVIDENCE_NAMES],
        }

    usage = kubectl(["top", "nodes", "--no-headers"])
    if not usage.ok:
        evidence["unavailable"].append(f"node_usage: {brief(usage.reason, 120)}")
    else:
        rows = []
        for line in usage.stdout.splitlines()[:MAX_EVIDENCE_ROWS]:
            fields = line.split()
            if len(fields) >= 5:
                rows.append(
                    {
                        "node": fields[0][:63],
                        "cpu": fields[1][:16],
                        "cpu_percent": fields[2][:8],
                        "memory": fields[3][:16],
                        "memory_percent": fields[4][:8],
                    }
                )
        evidence["node_usage"] = rows

    prometheus, prometheus_reason = query_prometheus("failed-pods")
    if prometheus_reason:
        prometheus, fallback_reason = query_prometheus("up")
        if fallback_reason:
            evidence["unavailable"].append(f"prometheus: {prometheus_reason}")
    if prometheus:
        evidence["prometheus"] = prometheus

    return evidence


def query_prometheus(key: str):
    """One allowlisted query, through the API service proxy."""
    query = PROMETHEUS_QUERIES.get(key)
    if query is None:
        return None, f"query {key!r} is not allowlisted"
    raw = f"{PROMETHEUS_PROXY_PATH}?{urllib.parse.urlencode({'query': query})}"
    payload, reason = kubectl_json(["get", "--raw", raw])
    if reason:
        return None, reason
    result = ((payload or {}).get("data") or {}).get("result")
    if not isinstance(result, list):
        return None, "prometheus returned no result vector"
    samples = []
    for item in result[:MAX_PROMETHEUS_SAMPLES]:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric") or {}
        value = item.get("value") or []
        samples.append(
            {
                "labels": {
                    label: str(metric.get(label))[:63]
                    for label in PROMETHEUS_LABEL_KEYS
                    if label in metric
                },
                "value": str(value[1])[:32] if len(value) > 1 else "",
            }
        )
    return {"query": key, "series": len(result), "samples": samples}, ""


def classify_namespace(namespace: str) -> str:
    if namespace in PLATFORM_NAMESPACES:
        return "cluster-platform"
    if namespace in PRODUCT_NAMESPACES:
        return "server-product"
    return "unroutable"


def derive_findings(evidence: dict, usb4: dict) -> list:
    """Turn bounded evidence into at most MAX_FINDINGS routable facts.

    A finding is a (class, kind, subject) triple and nothing else: the
    subject is what stays stable across observations, and stability is what
    makes deduplication work at all.
    """
    findings = []
    for name in (evidence.get("nodes") or {}).get("not_ready", []):
        findings.append(
            {
                "class": "cluster-platform",
                "kind": "node-not-ready",
                "subject": name,
                "evidence": [f"node {name} does not report condition Ready=True"],
            }
        )
    for name in (evidence.get("workflows") or {}).get("failed", []):
        findings.append(
            {
                "class": "cluster-platform",
                "kind": "workflow-failed",
                "subject": name,
                "evidence": [f"workflow {name} ended in a failed or errored phase"],
            }
        )
    for name in (evidence.get("pods") or {}).get("failing", []):
        namespace = name.split("/", 1)[0]
        findings.append(
            {
                "class": classify_namespace(namespace),
                "kind": "pod-failing",
                "subject": name,
                "evidence": [f"pod {name} reports a failed or unknown phase"],
            }
        )
    for node in USB4_NODES:
        if usb4.get(node) == "down":
            findings.append(
                {
                    "class": "cluster-platform",
                    "kind": "usb4-link-down",
                    "subject": node,
                    "evidence": [f"node {node} reports its USB4 link down in a fresh sample"],
                }
            )
    return findings[:MAX_FINDINGS]


def fingerprint(finding: dict) -> str:
    """A stable, versioned identity for one fault.

    The version travels in the hash and in the marker, so changing what a
    fingerprint covers produces new issues instead of silently reusing
    someone else's.
    """
    material = "|".join(
        (
            FINGERPRINT_VERSION,
            str(finding.get("class", "")),
            str(finding.get("kind", "")),
            str(finding.get("subject", "")),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def marker_for(value: str) -> str:
    return MARKER_TEMPLATE.format(version=FINGERPRINT_VERSION, fingerprint=value)


def state_directory() -> str:
    root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(root, "bluefin-review")


def findings_state_path() -> str:
    return os.path.join(state_directory(), "lab-findings.json")


def findings_lock_path() -> str:
    return os.path.join(state_directory(), "lab-findings.lock")


@contextlib.contextmanager
def findings_lock():
    """The one fixed lock two dashboards on a host contend for.

    The path is fixed rather than derived from the session, because the
    point is cross-process serialization: two brokers deriving two lock
    paths would each hold their own lock and both open the same issue.
    """
    directory = state_directory()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle = open(findings_lock_path(), "a+", encoding="utf-8")
    deadline = time.monotonic() + STATE_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as failure:
                if failure.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                # A stuck holder must degrade to a visible "failed" finding,
                # never to a health response that hangs.
                if time.monotonic() >= deadline:
                    raise TimeoutError("another process holds the lab findings lock")
                time.sleep(0.1)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_findings_state() -> dict:
    try:
        with open(findings_state_path(), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or not isinstance(state.get("findings"), dict):
        # A truncated write from a hard kill resets the counters rather than
        # silencing the feedback loop permanently.
        state = {"version": 1, "findings": {}}
    return state


def save_findings_state(state: dict) -> None:
    path = findings_state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    staging = f"{path}.new"
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(state, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)


def issue_body(finding: dict, marker: str, entry: dict) -> str:
    block = bound_block("\n".join(str(line) for line in finding.get("evidence", [])))
    observed = int(entry.get("count", 1))
    return "\n".join(
        (
            marker,
            "",
            f"The Bluefin review appliance observed this {observed} times while gathering",
            "lab health evidence for a pull request review.",
            "",
            f"- class: `{redact(finding.get('class', ''))}`",
            f"- kind: `{redact(finding.get('kind', ''))}`",
            f"- subject: `{redact(finding.get('subject', ''))}`",
            "",
            "Evidence:",
            "",
            "```",
            block,
            "```",
            "",
            "This issue is maintained automatically: the marker above is how the",
            "appliance recognizes it, so keep it in the body when editing.",
        )
    )


def gh_json(args, timeout: int = GH_TIMEOUT_SECONDS):
    result = run_host(["gh", *args], timeout)
    if not result.ok:
        return None, brief(result.reason or "gh failed", 120)
    try:
        return json.loads(result.stdout or "[]"), ""
    except ValueError:
        return None, "gh returned output that is not JSON"


def existing_issue(repository: str, marker: str, remembered: int = 0):
    """Ask GitHub before creating anything.

    GitHub's issue search index lags creation by minutes, so a broker that
    only searched would open a fresh duplicate on every cycle until the
    index caught up. The issue this host opened is recorded in its own
    state, so that number is checked first and by identity alone — the
    broker does not need to re-derive the identity of an issue it created.
    The search is the fallback for a finding this host has never filed, and
    its results are re-checked for the marker because search is fuzzy.
    """
    if remembered:
        issue, reason = gh_json(
            ["issue", "view", str(remembered), "--repo", repository, "--json", "number,state"]
        )
        if not reason and isinstance(issue, dict) and str(issue.get("state", "")).upper() == "OPEN":
            return issue, ""
        # A remembered issue that was closed, deleted or is unreadable is not
        # a duplicate: a fault that returns after a maintainer closed its
        # issue deserves a new one rather than a comment nobody is watching.
    issues, reason = gh_json(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--search",
            marker,
            "--json",
            "number,title,body",
        ]
    )
    if reason:
        return None, reason
    for issue in issues or []:
        if isinstance(issue, dict) and marker in str(issue.get("body", "")):
            return issue, ""
    return None, ""


def file_finding(finding: dict, evidence_digest: str, now: float) -> dict:
    """Upsert one finding into its routed repository.

    Never raises and never blocks the caller's answer: every outcome —
    including a `gh` that fails — comes back as a state a maintainer can
    read in the health response.
    """
    identity = fingerprint(finding)
    record = {
        "class": finding.get("class", ""),
        "kind": finding.get("kind", ""),
        "subject": finding.get("subject", ""),
        "fingerprint": identity,
        "repository": "",
        "issue": 0,
        "state": "unroutable",
        "detail": "",
    }
    repository = FINDING_ROUTES.get(record["class"], "")
    if not repository:
        record["detail"] = "no repository owns this class of fault; nothing filed"
        return record
    record["repository"] = repository
    marker = marker_for(identity)

    try:
        with findings_lock():
            state = load_findings_state()
            entry = state["findings"].setdefault(
                identity,
                {"count": 0, "first_seen": now, "last_seen": now, "last_comment": 0.0, "issue": 0, "digest": ""},
            )
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_seen"] = now
            entry["class"] = record["class"]
            entry["kind"] = record["kind"]
            entry["repository"] = repository

            if entry["count"] < MIN_OBSERVATIONS_BEFORE_FILING:
                record["state"] = "suppressed"
                record["detail"] = (
                    f"observed {entry['count']} time; filing needs "
                    f"{MIN_OBSERVATIONS_BEFORE_FILING}"
                )
                save_findings_state(state)
                return record

            issue, reason = existing_issue(repository, marker, int(entry.get("issue", 0) or 0))
            if reason:
                record["state"] = "failed"
                record["detail"] = f"duplicate search failed: {reason}"
                save_findings_state(state)
                return record

            if issue is not None:
                record["state"] = "already-open"
                record["issue"] = int(issue.get("number", 0) or 0)
                entry["issue"] = record["issue"]
                stale_comment = (now - float(entry.get("last_comment", 0.0))) >= COMMENT_INTERVAL_SECONDS
                if record["issue"] and evidence_digest != entry.get("digest", "") and stale_comment:
                    comment = run_host(
                        [
                            "gh",
                            "issue",
                            "comment",
                            str(record["issue"]),
                            "--repo",
                            repository,
                            "--body",
                            issue_body(finding, marker, entry),
                        ],
                        GH_TIMEOUT_SECONDS,
                    )
                    if comment.ok:
                        entry["last_comment"] = now
                        entry["digest"] = evidence_digest
                        record["detail"] = "existing issue updated with new evidence"
                    else:
                        record["state"] = "failed"
                        record["detail"] = f"comment failed: {brief(comment.reason, 120)}"
                else:
                    record["detail"] = "existing issue already carries this finding"
                save_findings_state(state)
                return record

            title = brief(f"lab: {record['kind']} on {record['subject']}", 120)
            created = run_host(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    repository,
                    "--title",
                    title,
                    "--body",
                    issue_body(finding, marker, entry),
                ],
                GH_TIMEOUT_SECONDS,
            )
            if not created.ok:
                record["state"] = "failed"
                record["detail"] = f"issue create failed: {brief(created.reason, 120)}"
                save_findings_state(state)
                return record
            record["state"] = "filed"
            record["issue"] = issue_number(created.stdout)
            entry["issue"] = record["issue"]
            entry["digest"] = evidence_digest
            entry["last_comment"] = now
            record["detail"] = "issue opened from a repeated observation"
            save_findings_state(state)
            return record
    except (OSError, TimeoutError, ValueError) as failure:
        record["state"] = "failed"
        record["detail"] = brief(f"finding state unavailable: {failure}", 120)
        return record


def issue_number(stdout: str) -> int:
    """`gh issue create` prints the issue URL; the trailing segment is the number."""
    for line in reversed((stdout or "").strip().splitlines()):
        candidate = line.strip().rstrip("/").rsplit("/", 1)[-1]
        if candidate.isdigit():
            return int(candidate)
    return 0


def finding_digest(finding: dict, evidence: dict) -> str:
    """What counts as materially new evidence for one finding.

    Node CPU and Prometheus values move on every sample, so hashing the
    whole evidence block would call every observation new and turn an open
    issue into a comment feed nobody reads. The digest covers the finding's
    own lines and the fault picture around it — the parts that change only
    when something actually changed.
    """
    material = {
        "finding": [
            finding.get("class", ""),
            finding.get("kind", ""),
            finding.get("subject", ""),
            list(finding.get("evidence", [])),
        ],
        "nodes": (evidence.get("nodes") or {}).get("not_ready", []),
        "pods": (evidence.get("pods") or {}).get("failing", []),
        "workflows": (evidence.get("workflows") or {}).get("failed", []),
        "usb4": [(evidence.get("usb4") or {}).get(name, "") for name in USB4_NODES],
    }
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def file_findings(findings: list, evidence: dict) -> list:
    now = time.time()
    return [file_finding(finding, finding_digest(finding, evidence), now) for finding in findings]


class ImageResolution:
    """Either an exact pinned reference, or the reason there is not one."""

    def __init__(self, reference: str = "", reason: str = "") -> None:
        self.reference = reference
        self.reason = reason


def mint_anonymous_token(repository: str, timeout: int):
    url = (
        f"{GHCR_API_BASE}/token?"
        + urllib.parse.urlencode(
            {"service": GHCR_REFERENCE_HOST, "scope": f"repository:{repository}:pull"}
        )
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as failure:
        # A denied mint is not a transport problem: it means the package is
        # not readable anonymously, which is the same answer as "no image".
        return "", f"registry denied an anonymous token (HTTP {failure.code})"
    except (urllib.error.URLError, OSError, ValueError):
        return "", "registry token endpoint is unreachable"
    token = payload.get("token") or payload.get("access_token") or ""
    if not isinstance(token, str) or not token:
        return "", "registry returned no anonymous token"
    return token, ""


def resolve_head_image(repository: str, head: str, timeout: int = REGISTRY_TIMEOUT_SECONDS) -> ImageResolution:
    """Resolve `sha-<head>` to an immutable digest, or explain why not.

    A profile that tests an image must test *this* pull request's image. A
    moving tag would silently test whatever was newest when the workflow
    started, so `latest`, `stable` and the branch name are not substitutes
    and are never attempted: no digest means no run.
    """
    token, reason = mint_anonymous_token(repository, timeout)
    if not token:
        return ImageResolution(reason=reason)
    url = f"{GHCR_API_BASE}/v2/{repository}/manifests/sha-{head}"
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("Accept", MANIFEST_ACCEPT)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            digest = response.headers.get("docker-content-digest", "") or ""
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return ImageResolution(reason=f"no image is published for head {head[:12]}")
        return ImageResolution(reason=f"registry answered HTTP {failure.code}")
    except (urllib.error.URLError, OSError, ValueError):
        return ImageResolution(reason="registry manifest endpoint is unreachable")
    digest = digest.strip()
    if not DIGEST_RE.match(digest):
        return ImageResolution(reason="registry returned no usable content digest")
    return ImageResolution(reference=f"{GHCR_REFERENCE_HOST}/{repository}@{digest}")


def perform_submit(context: BrokerContext, repository: str, pr: int, head: str, profile: str) -> dict:
    """The only path that starts work on the cluster."""
    definition = SAFE_PROFILES.get(profile)
    if definition is None:
        return ok_payload(result="not-applicable", detail=f"profile {profile[:63]!r} is not offered")
    template = str(definition["template"])
    if is_refused_template(template):
        return ok_payload(
            result="not-applicable", detail="the profile's template is refused by the safety guard"
        )
    applies_to = definition["repositories"]
    if applies_to and repository not in applies_to:
        return ok_payload(
            result="not-applicable", detail=f"profile {profile} does not apply to {repository}"
        )
    if not context.allows(repository):
        return ok_payload(
            result="not-applicable", detail=f"{repository} is outside this session's scope"
        )

    argv = [
        "argo",
        "submit",
        "--from",
        f"workflowtemplate/{template}",
        "-n",
        context.namespace,
    ]
    if definition["needs_image"]:
        resolved = resolve_head_image(repository, head)
        if not resolved.reference:
            return ok_payload(result="not-applicable", detail=resolved.reason)
        argv += ["--parameter", f"image={resolved.reference}"]
    labels = ",".join(
        (
            f"review.session={sanitize_label_value(context.session)}",
            f"review.repository={sanitize_label_value(repository.replace('/', '_'))}",
            f"review.pr={pr}",
            f"review.head={head}",
        )
    )
    argv += ["--labels", labels, "--output", "name"]

    result = run_host(argv, ARGO_TIMEOUT_SECONDS)
    if not result.ok:
        return error_payload("unavailable", result.reason or "argo submit failed")
    name = ""
    for line in reversed(result.stdout.strip().splitlines()):
        candidate = line.strip()
        if WORKFLOW_NAME_RE.match(candidate):
            name = candidate
            break
    return ok_payload(
        result="submitted",
        workflow=name,
        detail=f"{template} submitted to namespace {context.namespace}",
    )


def handle_submit(context: BrokerContext, request: dict) -> dict:
    repository = require_repository(request)
    pr = require_pr(request)
    head = require_head(request)
    profile = require_profile(request)
    return perform_submit(context, repository, pr, head, profile)


def handle_health(context: BrokerContext, request: dict) -> dict:
    repository = require_repository(request)
    pr = require_pr(request)
    head = require_head(request)

    now = datetime.datetime.now(datetime.timezone.utc)
    evidence = collect_evidence(context, now)
    findings = derive_findings(evidence, evidence["usb4"])
    reported = file_findings(findings, evidence)

    # Diagnosis is on demand, not on schedule: a healthy look costs the lab
    # nothing, and a fault gets the cluster's own analyzer pointed at it
    # through the same guarded submit path everything else uses.
    if findings:
        diagnosis = perform_submit(context, repository, pr, head, DIAGNOSIS_PROFILE)
        diagnosis = {
            "profile": DIAGNOSIS_PROFILE,
            "result": diagnosis.get("result", diagnosis.get("error", "unavailable")),
            "workflow": diagnosis.get("workflow", ""),
            "detail": diagnosis.get("detail", ""),
        }
    else:
        diagnosis = {
            "profile": DIAGNOSIS_PROFILE,
            "result": "not-applicable",
            "workflow": "",
            "detail": "no fault signal; diagnosis not requested",
        }

    degraded = bool(evidence["unavailable"]) or bool(findings)
    if evidence["unavailable"]:
        detail = "; ".join(evidence["unavailable"])
    elif findings:
        detail = f"{len(findings)} fault(s) observed in the lab"
    else:
        detail = "lab evidence gathered with no faults"
    return ok_payload(
        state="DEGRADED" if degraded else "READY",
        evidence=evidence,
        findings=reported,
        diagnosis=diagnosis,
        detail=brief(detail),
    )


def dispatch(context: BrokerContext, raw: bytes) -> dict:
    """One try/except for the whole protocol.

    A broker that dies on a malformed request hands an agent a denial of
    service against the maintainer's own dashboard, so every unexpected
    failure becomes a protocol error instead of a traceback.
    """
    try:
        request = decode_request(raw, context)
        if request["action"] == "status":
            return handle_status(context, request)
        if request["action"] == "health":
            return handle_health(context, request)
        return handle_submit(context, request)
    except Rejected as refusal:
        return error_payload(refusal.code, refusal.detail)
    except Exception as failure:  # noqa: BLE001 - the last line of defense
        return error_payload("unavailable", f"broker error: {type(failure).__name__}")


def read_request_line(connection) -> bytes:
    """Read one newline-terminated request, capped and drained.

    Accumulation stops one byte past the cap so `decode_request` can answer
    `bad-request`, but the rest of the line is still read: a client that is
    told its request is too large should hear that, not a reset connection.
    """
    buffer = bytearray()
    drained = 0
    received = False
    while True:
        chunk = connection.recv(RECV_CHUNK_BYTES)
        if not chunk:
            # Nothing at all means the peer connected and left; an empty
            # line is a request, and gets a protocol error like any other
            # unreadable one.
            return bytes(buffer) if received else None
        received = True
        drained += len(chunk)
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunk = chunk[:newline]
        room = MAX_REQUEST_BYTES + 1 - len(buffer)
        if room > 0:
            buffer.extend(chunk[:room])
        if newline >= 0 or drained > MAX_DRAIN_BYTES:
            return bytes(buffer)


class BrokerHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = self.request
        try:
            connection.settimeout(READ_TIMEOUT_SECONDS)
            raw = read_request_line(connection)
        except OSError:
            return
        if raw is None:
            return
        payload = dispatch(self.server.context, raw)
        line = bounded_response(payload)
        try:
            # Gathering evidence can outlast the read timeout, so the write
            # deadline is its own budget rather than the one the request
            # arrived under.
            connection.settimeout(WRITE_TIMEOUT_SECONDS)
            connection.sendall(line)
        except OSError:
            return


class BrokerServer(socketserver.ThreadingUnixStreamServer):
    # A slow or absent handler must not stall the next caller: the dashboard
    # polls `status` while a `health` call is still gathering evidence.
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, context: BrokerContext) -> None:
        self.context = context
        super().__init__(path, BrokerHandler)

    def handle_error(self, request, client_address) -> None:
        # Handlers already answer with a protocol error; nothing about a
        # connection belongs on the maintainer's terminal.
        return


def prepare_socket_path(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        # A previous run that was killed leaves the inode behind; bind()
        # would fail with EADDRINUSE on a socket nobody is listening to.
        with contextlib.suppress(OSError):
            os.unlink(path)


def serve(path: str, context: BrokerContext) -> int:
    prepare_socket_path(path)
    # bind() honours the umask, so the socket is never briefly reachable by
    # anyone else; the chmod afterwards states the intent regardless of the
    # inherited mask.
    previous_umask = os.umask(0o177)
    try:
        server = BrokerServer(path, context)
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)

    def stop(_signum, _frame) -> None:
        # shutdown() blocks until serve_forever() returns, which would
        # deadlock if called from the thread running it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        json.dumps(
            {"version": PROTOCOL_VERSION, "ready": True, "session": context.session},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(path)
    return 0


def probe(timeout: int) -> int:
    """Answer whether this host can broker at all, without touching a socket.

    The launcher runs this before it starts anything, so a host with no
    cluster access degrades to "no lab" at launch instead of to a dashboard
    full of `unavailable`.
    """
    context_result = run_host(["kubectl", "config", "current-context"], timeout)
    if not context_result.ok:
        answer = {"usable": False, "context": "", "detail": brief(context_result.reason, 120)}
        print(json.dumps(answer, separators=(",", ":"), sort_keys=True), flush=True)
        return 1
    current = brief(context_result.stdout, 63)
    nodes = run_host(["kubectl", "get", "nodes", "-o", "name"], timeout)
    answer = {
        "usable": nodes.ok,
        "context": current,
        "detail": "cluster reachable" if nodes.ok else brief(nodes.reason, 120),
    }
    print(json.dumps(answer, separators=(",", ":"), sort_keys=True), flush=True)
    return 0 if nodes.ok else 1


def parse_repositories(value: str) -> tuple:
    repositories = []
    for item in (value or "").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if not REPOSITORY_RE.match(candidate):
            raise SystemExit(f"review-lab-broker: {candidate!r} is not owner/repo")
        repositories.append(candidate)
    return tuple(repositories)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="review-lab-broker.py",
        description="Host-side broker for optional lab access from the review container.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve_parser = subcommands.add_parser("serve", help="serve the broker socket until stopped")
    serve_parser.add_argument("--socket", required=True, help="unix socket path to bind")
    serve_parser.add_argument("--session", required=True, help="session id every request must carry")
    serve_parser.add_argument(
        "--repositories", default="", help="comma separated owner/repo allowlist for this session"
    )

    probe_parser = subcommands.add_parser("probe", help="report whether the host can reach a cluster")

    arguments = parser.parse_args(argv)
    if arguments.command == "probe":
        return probe(KUBECTL_TIMEOUT_SECONDS)

    if not arguments.session.strip():
        raise SystemExit("review-lab-broker: --session must not be empty")
    context = BrokerContext(
        session=arguments.session,
        namespace=DEFAULT_NAMESPACE,
        repositories=parse_repositories(arguments.repositories),
    )
    return serve(arguments.socket, context)


verify_profile_map()


if __name__ == "__main__":
    sys.exit(main())
