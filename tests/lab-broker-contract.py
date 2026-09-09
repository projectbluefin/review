#!/usr/bin/env python3
"""Contract for the host-side lab broker.

Runs anywhere: no cluster, no network, no credentials. `kubectl`, `argo`,
`gh` and `k8sgpt` are bash stubs on a PATH that contains nothing else, so a
broker that reached for a real binary would find nothing at all — the test
cannot accidentally talk to a maintainer's cluster, and a request that
escapes the protocol shows up as a line in a stub's log instead of as a
deleted namespace. HOME, XDG_STATE_HOME and XDG_RUNTIME_DIR are redirected
into a private short-lived `/tmp` root, and the anonymous ghcr resolver is
pointed at a loopback HTTP server.

Each scenario is its own broker process with its own stubs, fixtures and
state, because the interesting behaviour is what the broker does when a host
tool is missing, lying, or failing — and that is a property of a whole
process, not of a call.
"""

from __future__ import annotations

import datetime
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BROKER = REPO / "scripts" / "review-lab-broker.py"
# AF_UNIX paths are capped near 108 bytes. Keep the scenario root independent
# of the checkout depth: the descriptive name lives in the check text, not in
# the path.
WORK = Path(tempfile.mkdtemp(prefix="bluefin-lab-", dir="/tmp"))

SESSION = "review-lab-session-1"
HEAD_PUBLISHED = "a" * 40
HEAD_UNPUBLISHED = "b" * 40
PINNED_DIGEST = "sha256:" + "1" * 64
LINK = "lab.projectbluefin.io/usb4-link"
OBSERVED = "lab.projectbluefin.io/usb4-link-observed-at"

PASSED = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def check_equal(name: str, actual, expected) -> None:
    check(name, actual == expected, f"expected {expected!r}, got {actual!r}")


def load_broker_module():
    spec = importlib.util.spec_from_file_location("review_lab_broker", BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BROKER_MODULE = load_broker_module()


def stamp(offset_seconds: float = 0.0) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=offset_seconds
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def node(name: str, *, ready: bool = True, link=None, observed=None) -> dict:
    annotations = {}
    if link is not None:
        annotations[LINK] = link
    if observed is not None:
        annotations[OBSERVED] = observed
    return {
        "metadata": {"name": name, "annotations": annotations},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def fresh_nodes() -> dict:
    return {
        "items": [
            node("ghost", link="up", observed=stamp()),
            node("exo-0", link="up", observed=stamp()),
        ]
    }


def pod(namespace: str, name: str, phase: str) -> dict:
    return {"metadata": {"name": name, "namespace": namespace}, "status": {"phase": phase}}


def workflow(name: str, phase: str) -> dict:
    return {"metadata": {"name": name}, "status": {"phase": phase}}


PROMETHEUS_FIXTURE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {"job": "kube-state-metrics", "instance": "10.42.0.7:8080"},
                "value": [1757000000, "2"],
            }
        ],
    },
}
TOP_NODES_FIXTURE = "ghost 250m 3% 4000Mi 25%\nexo-0 120m 1% 2000Mi 12%\n"


class FakeRegistry(http.server.BaseHTTPRequestHandler):
    """The anonymous ghcr flow the broker uses, and nothing more.

    Only `sha-<head>` references that were explicitly published resolve; a
    broker that fell back to a moving tag would get a 404 here, which is
    exactly the answer that must end in `not-applicable`.
    """

    published: dict = {}
    denied: set = set()
    manifest_accepts: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def resolve(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path.startswith("/token"):
            scope = (urllib.parse.parse_qs(parts.query).get("scope") or [""])[0]
            fields = scope.split(":")
            repository = fields[1] if len(fields) > 2 else ""
            if repository in self.denied:
                return 403, {}, b'{"errors":[{"code":"DENIED"}]}'
            return 200, {"Content-Type": "application/json"}, b'{"token":"anonymous-contract-token"}'
        match = re.match(r"^/v2/([^/]+/[^/]+)/manifests/([^/]+)$", parts.path)
        if match:
            type(self).manifest_accepts.append(self.headers.get("Accept", ""))
            digest = self.published.get(f"{match.group(1)}:{match.group(2)}")
            if digest:
                return (
                    200,
                    {
                        "Docker-Content-Digest": digest,
                        "Content-Type": "application/vnd.oci.image.index.v1+json",
                    },
                    b"",
                )
            return 404, {}, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'
        return 404, {}, b""

    def respond(self, with_body: bool) -> None:
        status, headers, body = self.resolve()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body and body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        self.respond(True)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's interface
        self.respond(False)


def start_registry():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


BASH = shutil.which("bash") or "/bin/bash"

# The stubs use bash builtins only. PATH holds nothing but the stub
# directory, so an external `cat` would not resolve — and neither would the
# real kubectl if the broker ever ignored PATH.
KUBECTL_STUB = """#!{bash}
printf 'call: %s\\n' "$*" >> "$LAB_LOG/kubectl.log"
if [[ "${{LAB_KUBECTL_MODE:-ok}}" == "fail" ]]; then
  printf 'kubectl: the server could not be reached\\n' >&2
  exit 1
fi
fixture=""
case "$*" in
  *"config current-context"*) printf 'ghost-lab\\n'; exit 0 ;;
  *"top nodes"*) fixture="top-nodes.txt" ;;
  *"get --raw"*) fixture="prometheus.json" ;;
  *"get nodes"*) fixture="nodes.json" ;;
  *"get pods"*) fixture="pods.json" ;;
  *workflows.argoproj.io*)
    case "$*" in
      *"review.session="*) fixture="session-workflows.json" ;;
      *) fixture="workflows.json" ;;
    esac ;;
esac
if [[ -n "$fixture" && -f "$LAB_FIXTURES/$fixture" ]]; then
  printf '%s\\n' "$(<"$LAB_FIXTURES/$fixture")"
  exit 0
fi
printf 'kubectl: no fixture for %s\\n' "$*" >&2
exit 1
"""

ARGO_STUB = """#!{bash}
printf 'call: %s\\n' "$*" >> "$LAB_LOG/argo.log"
if [[ "${{LAB_ARGO_MODE:-ok}}" == "fail" ]]; then
  printf 'argo: no workflow templates found\\n' >&2
  exit 1
fi
printf 'review-lab-workflow-abc12\\n'
"""

GH_STUB = """#!{bash}
printf 'call: %s %s\\n' "${{1:-}}" "${{2:-}}" >> "$LAB_LOG/gh.log"
printf 'argv: %s\\n' "$*" >> "$LAB_LOG/gh.log"
if [[ "${{LAB_GH_MODE:-ok}}" == "fail" ]]; then
  printf 'gh: HTTP 401 Bad credentials\\n' >&2
  exit 1
fi
if [[ "${{1:-}} ${{2:-}}" == "issue list" ]]; then
  if [[ -f "$LAB_FIXTURES/issues.json" ]]; then
    printf '%s\\n' "$(<"$LAB_FIXTURES/issues.json")"
  else
    printf '[]\\n'
  fi
  exit 0
fi
if [[ "${{1:-}} ${{2:-}}" == "issue create" ]]; then
  printf 'https://github.com/projectbluefin/lab/issues/451\\n'
  exit 0
fi
if [[ "${{1:-}} ${{2:-}}" == "issue view" ]]; then
  if [[ -f "$LAB_FIXTURES/issue-view.json" ]]; then
    printf '%s\\n' "$(<"$LAB_FIXTURES/issue-view.json")"
  else
    printf '{{"number":%s,"state":"OPEN"}}\\n' "${{3:-0}}"
  fi
  exit 0
fi
exit 0
"""

K8SGPT_STUB = """#!{bash}
printf 'call: %s\\n' "$*" >> "$LAB_LOG/k8sgpt.log"
exit 0
"""

STUBS = {"kubectl": KUBECTL_STUB, "argo": ARGO_STUB, "gh": GH_STUB, "k8sgpt": K8SGPT_STUB}


class Broker:
    """One broker process with its own stubs, fixtures, state and socket."""

    def __init__(self, key: str, *, registry: str, session: str = SESSION, repositories=(),
                 tools=("kubectl", "argo", "gh", "k8sgpt"), mode=None, nested: bool = False) -> None:
        self.key = key
        self.root = WORK / key
        self.bin = self.root / "bin"
        self.fixtures = self.root / "fx"
        self.logs = self.root / "log"
        for directory in (self.bin, self.fixtures, self.logs, self.root / "home", self.root / "state", self.root / "run"):
            directory.mkdir(parents=True, exist_ok=True)
        for tool in tools:
            path = self.bin / tool
            path.write_text(STUBS[tool].format(bash=BASH))
            path.chmod(0o755)
        # `nested` leaves the socket's parent directory absent, which is how
        # the launcher calls this: the broker owns creating its own runtime
        # directory with a private mode.
        self.socket_directory = self.root / "run" / "sock" if nested else self.root
        self.socket_path = str(self.socket_directory / "s")
        if len(self.socket_path) > 100:
            raise SystemExit(f"socket path is too long for AF_UNIX: {self.socket_path}")
        self.session = session
        self.repositories = tuple(repositories)
        self.environment = {
            "PATH": str(self.bin),
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_RUNTIME_DIR": str(self.root / "run"),
            "LAB_LOG": str(self.logs),
            "LAB_FIXTURES": str(self.fixtures),
            "BLUEFIN_REVIEW_GHCR_BASE": registry,
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.environment.update(mode or {})
        self.output = None
        self.process = None

    def fixture(self, name: str, content) -> None:
        text = content if isinstance(content, str) else json.dumps(content)
        (self.fixtures / name).write_text(text)

    def start(self) -> "Broker":
        argv = [
            sys.executable,
            str(BROKER),
            "serve",
            "--socket",
            self.socket_path,
            "--session",
            self.session,
        ]
        if self.repositories:
            argv += ["--repositories", ",".join(self.repositories)]
        self.output = open(self.root / "broker.out", "w+", encoding="utf-8")
        self.process = subprocess.Popen(
            argv, env=self.environment, stdout=self.output, stderr=subprocess.STDOUT
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"broker {self.key} exited early:\n{self.read_output()}")
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(2)
                    probe.connect(self.socket_path)
                return self
            except OSError:
                time.sleep(0.05)
        raise SystemExit(f"broker {self.key} never bound its socket:\n{self.read_output()}")

    def read_output(self) -> str:
        if self.output is None:
            return ""
        self.output.flush()
        return (self.root / "broker.out").read_text()

    def send(self, payload, timeout: float = 30.0) -> bytes:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(self.socket_path)
            try:
                client.sendall(raw)
            except OSError:
                pass
            chunks = b""
            while not chunks.endswith(b"\n") and len(chunks) < 2 * 1024 * 1024:
                block = client.recv(65536)
                if not block:
                    break
                chunks += block
        return chunks

    def call(self, payload, timeout: float = 30.0) -> dict:
        raw = self.send(payload, timeout)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"parse_error": raw.decode("utf-8", "replace")}

    def request(self, action: str, **fields) -> dict:
        payload = {"version": 1, "action": action, "session": self.session}
        payload.update(fields)
        return self.call(payload)

    def log(self, tool: str) -> str:
        path = self.logs / f"{tool}.log"
        return path.read_text() if path.exists() else ""

    def clear_logs(self) -> None:
        for path in self.logs.glob("*.log"):
            path.unlink()

    def state_file(self) -> Path:
        return self.root / "state" / "bluefin-review" / "lab-findings.json"

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)
        if self.output is not None:
            self.output.close()

    def __enter__(self) -> "Broker":
        return self.start()

    def __exit__(self, *_exception) -> None:
        self.stop()


def healthy_fixtures(broker: Broker) -> None:
    broker.fixture("nodes.json", fresh_nodes())
    broker.fixture("pods.json", {"items": [pod("argo", "workflow-controller-1", "Running")]})
    broker.fixture("workflows.json", {"items": [workflow("qa-1", "Succeeded")]})
    broker.fixture("session-workflows.json", {"items": []})
    broker.fixture("top-nodes.txt", TOP_NODES_FIXTURE)
    broker.fixture("prometheus.json", PROMETHEUS_FIXTURE)


def socket_checks(registry: str) -> None:
    """The socket the launcher hands the container, and nothing around it."""
    with Broker("s", registry=registry, nested=True) as broker:
        healthy_fixtures(broker)
        check_equal(
            "a missing socket directory is created private",
            oct(stat.S_IMODE(os.stat(broker.socket_directory).st_mode)),
            oct(0o700),
        )
        check_equal(
            "the socket itself is owner-only",
            oct(stat.S_IMODE(os.stat(broker.socket_path).st_mode)),
            oct(0o600),
        )
        check("the socket is a socket", stat.S_ISSOCK(os.stat(broker.socket_path).st_mode))

        # A caller that connects and then says nothing must not stall the
        # dashboard's next poll: the server threads per connection precisely
        # so a slow client costs one thread instead of the whole broker.
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        idle.settimeout(10)
        idle.connect(broker.socket_path)
        try:
            answer = broker.request("status")
            check("a silent connection does not block the next caller", answer.get("ok") is True)
        finally:
            idle.close()


def protocol_checks(registry: str) -> None:
    """Malformed, oversized, misversioned, misaddressed and injected input."""
    with Broker("p", registry=registry, repositories=("projectbluefin/bluefin",)) as broker:
        healthy_fixtures(broker)

        raw = broker.send(b"{not json\n")
        answer = json.loads(raw.decode("utf-8"))
        check_equal("malformed JSON is bad-request", answer.get("error"), "bad-request")
        check("error response carries version 1", answer.get("version") == 1)
        check("error response is not ok", answer.get("ok") is False)
        check("response is a single line", raw.count(b"\n") == 1)

        oversized = json.dumps({"version": 1, "action": "status", "session": SESSION, "pad": "x" * 70000})
        answer = broker.call(oversized.encode("utf-8") + b"\n")
        check_equal("oversized request is bad-request", answer.get("error"), "bad-request")

        answer = broker.call(b"[]\n")
        check_equal("non-object request is bad-request", answer.get("error"), "bad-request")

        answer = broker.call(b"\n")
        check_equal("empty request is bad-request", answer.get("error"), "bad-request")

        answer = broker.call({"version": 1, "action": "teardown", "session": SESSION})
        check_equal("unknown action is unknown-action", answer.get("error"), "unknown-action")

        answer = broker.call({"version": 2, "action": "status", "session": SESSION})
        check_equal("version 2 is unsupported-version", answer.get("error"), "unsupported-version")

        answer = broker.call({"version": "1", "action": "status", "session": SESSION})
        check_equal("non-integer version is bad-request", answer.get("error"), "bad-request")

        answer = broker.call({"version": 1, "action": "status", "session": "someone-elses-session"})
        check_equal("foreign session is wrong-session", answer.get("error"), "wrong-session")

        answer = broker.request("health", repository="projectbluefin/bluefin", pr=123, head="abc1234")
        check_equal("abbreviated head is bad-request", answer.get("error"), "bad-request")

        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr="123", head=HEAD_PUBLISHED
        )
        check_equal("string pr is bad-request", answer.get("error"), "bad-request")

        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=True, head=HEAD_PUBLISHED
        )
        check_equal("boolean pr is bad-request", answer.get("error"), "bad-request")

        answer = broker.request("health", pr=1, head=HEAD_PUBLISHED)
        check_equal("missing repository is bad-request", answer.get("error"), "bad-request")

        answer = broker.request("status", extra="ignored", namespace="kube-system")
        check("status ignores extra keys", answer.get("ok") is True, repr(answer))
        check("status reports READY or DEGRADED", answer.get("state") in ("READY", "DEGRADED"))
        check("status never reports ACTIVE as a state", answer.get("state") != "ACTIVE")
        check("status carries the profile list", sorted(answer.get("profiles", [])) == [
            "bluefin-qa-pipeline",
            "k8sgpt-on-demand",
        ], repr(answer.get("profiles")))
        check("status carries active as a boolean", isinstance(answer.get("active"), bool))
        check(
            "status carries the usb4 block",
            set(answer.get("usb4", {})) == {"ghost", "exo-0", "fresh"},
            repr(answer.get("usb4")),
        )
        check("status never grew a capabilities list", "capabilities" not in answer)

        mode = stat.S_IMODE(os.stat(broker.socket_path).st_mode)
        check_equal("socket is mode 0600", oct(mode), oct(0o600))

        broker.clear_logs()
        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin",
            pr=123,
            head=HEAD_PUBLISHED,
            profile="bluefin-qa-pipeline",
            namespace="kube-system",
            args=["--force", "delete"],
            manifest="apiVersion: v1",
        )
        argo = broker.log("argo")
        check_equal("published head submits", answer.get("result"), "submitted")
        check_equal("submit reports the workflow name", answer.get("workflow"), "review-lab-workflow-abc12")
        check(
            "submit runs the template from the profile map",
            "--from workflowtemplate/bluefin-qa-pipeline" in argo,
            argo,
        )
        check(
            "submit pins the head image by digest",
            f"--parameter image=ghcr.io/projectbluefin/bluefin@{PINNED_DIGEST}" in argo,
            argo,
        )
        check(
            "submit never substitutes a moving tag",
            ":latest" not in argo and ":stable" not in argo and ":main" not in argo,
            argo,
        )
        check(
            "submit labels the run with session, repository, pr and head",
            f"--labels review.session={SESSION},review.repository=projectbluefin_bluefin,"
            f"review.pr=123,review.head={HEAD_PUBLISHED}" in argo,
            argo,
        )
        check("request namespace never reaches argo", "kube-system" not in argo, argo)
        check("request args never reach argo", "--force" not in argo, argo)
        check("request manifest never reaches argo", "apiVersion" not in argo, argo)
        check(
            "the resolver names both OCI media types",
            any(
                "application/vnd.oci.image.index.v1+json" in accept
                and "application/vnd.oci.image.manifest.v1+json" in accept
                for accept in FakeRegistry.manifest_accepts
            ),
            repr(FakeRegistry.manifest_accepts),
        )

        broker.clear_logs()
        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin",
            pr=124,
            head=HEAD_UNPUBLISHED,
            profile="bluefin-qa-pipeline",
        )
        check_equal("unpublished head is not-applicable", answer.get("result"), "not-applicable")
        check("unpublished head is not an error", answer.get("ok") is True)
        check("unpublished head submits nothing", broker.log("argo") == "", broker.log("argo"))

        broker.clear_logs()
        answer = broker.request(
            "submit",
            repository="projectbluefin/review",
            pr=1,
            head=HEAD_PUBLISHED,
            profile="bluefin-qa-pipeline",
        )
        check_equal(
            "repository outside the profile is not-applicable", answer.get("result"), "not-applicable"
        )
        check("out-of-profile repository submits nothing", broker.log("argo") == "")

        answer = broker.request(
            "submit",
            repository="projectbluefin/dakota",
            pr=1,
            head=HEAD_PUBLISHED,
            profile="bluefin-qa-pipeline",
        )
        check_equal(
            "repository outside the session scope is not-applicable",
            answer.get("result"),
            "not-applicable",
        )

        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin",
            pr=1,
            head=HEAD_PUBLISHED,
            profile="install-lab",
        )
        check_equal("unknown profile is not-applicable", answer.get("result"), "not-applicable")
        check("unknown profile is not an error", answer.get("ok") is True)

        broker.clear_logs()
        for injected in (
            "k8sgpt-on-demand; rm -rf /",
            "bluefin-qa-pipeline && kubectl delete ns argo",
            "../../etc/passwd",
        ):
            answer = broker.request(
                "submit",
                repository="projectbluefin/bluefin",
                pr=1,
                head=HEAD_PUBLISHED,
                profile=injected,
            )
            check(
                f"injected profile {injected!r} never submits",
                answer.get("error") == "bad-request" or answer.get("result") == "not-applicable",
                repr(answer),
            )
        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin; kubectl delete ns argo",
            pr=1,
            head=HEAD_PUBLISHED,
            profile="bluefin-qa-pipeline",
        )
        check_equal("injected repository is bad-request", answer.get("error"), "bad-request")
        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin",
            pr=1,
            head="$(id)",
            profile="bluefin-qa-pipeline",
        )
        check_equal("injected head is bad-request", answer.get("error"), "bad-request")
        logs = broker.log("argo") + broker.log("kubectl") + broker.log("gh")
        check("no injected shell text reaches a host tool", "rm -rf" not in logs, logs)
        check("no injected delete reaches kubectl", "delete ns" not in logs, logs)
        check("k8sgpt is never executed directly", broker.log("k8sgpt") == "")

        broker.stop()
        check("the socket is unlinked on shutdown", not os.path.exists(broker.socket_path))


def status_checks(registry: str) -> None:
    """The USB4 truth table and the active-workflow rule."""
    with Broker("u", registry=registry) as broker:
        broker.fixture("session-workflows.json", {"items": []})

        broker.fixture("nodes.json", fresh_nodes())
        usb4 = broker.request("status").get("usb4", {})
        check_equal("two fresh links report fresh", usb4.get("fresh"), True)
        check_equal("a fresh up link reports up", usb4.get("ghost"), "up")
        check_equal("both nodes are reported", usb4.get("exo-0"), "up")

        broker.fixture(
            "nodes.json",
            {"items": [node("ghost", link="down", observed=stamp()), node("exo-0", link="up", observed=stamp())]},
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("a fresh down link reports down", usb4.get("ghost"), "down")
        check_equal("a down link is still fresh", usb4.get("fresh"), True)

        broker.fixture(
            "nodes.json",
            {"items": [node("ghost", link="up", observed=stamp()), node("exo-0", link="up")]},
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("a missing timestamp is unknown", usb4.get("exo-0"), "unknown")
        check_equal("a missing timestamp is not fresh", usb4.get("fresh"), False)
        check_equal("the other node keeps its value", usb4.get("ghost"), "up")

        broker.fixture(
            "nodes.json",
            {
                "items": [
                    node("ghost", link="up", observed=stamp()),
                    node("exo-0", link="up", observed="yesterday-ish"),
                ]
            },
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("a malformed timestamp is unknown", usb4.get("exo-0"), "unknown")
        check_equal("a malformed timestamp is not fresh", usb4.get("fresh"), False)

        broker.fixture(
            "nodes.json",
            {
                "items": [
                    node("ghost", link="up", observed=stamp()),
                    node("exo-0", link="up", observed=stamp(300)),
                ]
            },
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("a future timestamp is unknown", usb4.get("exo-0"), "unknown")
        check_equal("a future timestamp is not fresh", usb4.get("fresh"), False)

        broker.fixture(
            "nodes.json",
            {
                "items": [
                    node("ghost", link="up", observed=stamp()),
                    node("exo-0", link="up", observed=stamp(-120)),
                ]
            },
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("a sample older than 45s is not fresh", usb4.get("fresh"), False)
        check_equal("a stale sample reports unknown", usb4.get("exo-0"), "unknown")

        broker.fixture(
            "nodes.json",
            {"items": [node("ghost", link="up", observed=stamp()), node("exo-0", link="wobbly", observed=stamp())]},
        )
        usb4 = broker.request("status").get("usb4", {})
        check_equal("an unknown link value is unknown", usb4.get("exo-0"), "unknown")
        check_equal("an unknown link value is not fresh", usb4.get("fresh"), False)

        broker.fixture("nodes.json", {"items": [node("ghost", link="up", observed=stamp())]})
        usb4 = broker.request("status").get("usb4", {})
        check_equal("an absent node is unknown", usb4.get("exo-0"), "unknown")
        check_equal("an absent node is not fresh", usb4.get("fresh"), False)

        broker.fixture("nodes.json", fresh_nodes())
        broker.fixture(
            "session-workflows.json",
            {"items": [workflow("qa-old", "Succeeded"), workflow("qa-now", "Running")]},
        )
        answer = broker.request("status")
        check_equal("a running session workflow is active", answer.get("active"), True)
        check_equal("a reachable cluster is READY", answer.get("state"), "READY")

        broker.fixture(
            "session-workflows.json",
            {"items": [workflow("qa-old", "Succeeded"), workflow("qa-bad", "Failed")]},
        )
        check_equal(
            "only terminal session workflows are inactive",
            broker.request("status").get("active"),
            False,
        )


def health_checks(registry: str) -> None:
    """Bounded evidence, on-demand diagnosis, and the filing state machine."""
    with Broker("h", registry=registry) as broker:
        healthy_fixtures(broker)
        broker.fixture("pods.json", {"items": [pod("argo", "stuck-pod", "Failed")]})
        broker.fixture(
            "workflows.json", {"items": [workflow("qa-1", "Succeeded"), workflow("qa-2", "Failed")]}
        )

        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=42, head=HEAD_PUBLISHED
        )
        serialized = json.dumps(answer)
        evidence = answer.get("evidence", {})
        check("health answers ok", answer.get("ok") is True, repr(answer)[:400])
        check_equal("faults make health DEGRADED", answer.get("state"), "DEGRADED")
        check("health stays under the response cap", len(serialized) < 262144, str(len(serialized)))
        check_equal("evidence counts ready nodes", evidence.get("nodes", {}).get("ready"), 2)
        check_equal("evidence counts pod phases", evidence.get("pods", {}).get("phases"), {"Failed": 1})
        check_equal(
            "evidence names failed workflows", evidence.get("workflows", {}).get("failed"), ["qa-2"]
        )
        check_equal(
            "evidence carries node usage from the metrics API",
            evidence.get("node_usage", [{}])[0].get("cpu"),
            "250m",
        )
        check_equal(
            "evidence carries one bounded prometheus result",
            evidence.get("prometheus", {}).get("query"),
            "failed-pods",
        )
        check(
            "prometheus evidence drops address labels",
            "instance" not in json.dumps(evidence.get("prometheus", {})),
            json.dumps(evidence.get("prometheus", {})),
        )
        check(
            "prometheus is read through the api service proxy",
            "/api/v1/namespaces/monitoring/services/prometheus-operated:9090/proxy"
            in broker.log("kubectl"),
            broker.log("kubectl"),
        )
        states = sorted(finding.get("state") for finding in answer.get("findings", []))
        check_equal("a first observation is suppressed", states, ["suppressed", "suppressed"])
        check("nothing is filed on first sight", "call: issue create" not in broker.log("gh"))

        argo = broker.log("argo")
        check(
            "diagnosis runs the k8sgpt workflow template",
            "--from workflowtemplate/k8sgpt-on-demand" in argo,
            argo,
        )
        check_equal("diagnosis is reported", answer.get("diagnosis", {}).get("result"), "submitted")
        check(
            "diagnosis passes no AI or explain flag",
            not any(flag in argo for flag in ("explain", "--ai", "--backend", "--anonymize")),
            argo,
        )
        check(
            "diagnosis passes no credential",
            not any(
                secret in argo.lower()
                for secret in ("token", "bearer", "password", "secret", "kubeconfig", "api-key")
            ),
            argo,
        )
        check("k8sgpt is never executed directly", broker.log("k8sgpt") == "")

        broker.clear_logs()
        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=42, head=HEAD_PUBLISHED
        )
        findings = {finding["subject"]: finding for finding in answer.get("findings", [])}
        check_equal(
            "a repeated observation is filed",
            sorted(finding["state"] for finding in findings.values()),
            ["filed", "filed"],
        )
        check_equal(
            "a filed finding is routed to the lab repository",
            findings.get("argo/stuck-pod", {}).get("repository"),
            "projectbluefin/lab",
        )
        check_equal(
            "a filed finding reports its issue number",
            findings.get("argo/stuck-pod", {}).get("issue"),
            451,
        )
        check_equal("filing creates two issues", broker.log("gh").count("call: issue create"), 2)
        check(
            "a duplicate search runs before creating",
            broker.log("gh").index("call: issue list") < broker.log("gh").index("call: issue create"),
        )
        identity = BROKER_MODULE.fingerprint(
            {"class": "cluster-platform", "kind": "pod-failing", "subject": "argo/stuck-pod"}
        )
        check(
            "the issue body carries the versioned marker",
            f"<!-- bluefin-review-lab-finding: v1 {identity} -->" in broker.log("gh"),
            broker.log("gh")[-800:],
        )
        check(
            "the issue body leaks no url or token",
            "https://" not in broker.log("gh").split("argv: issue create")[-1],
        )
        check("the findings state file is written", broker.state_file().exists())
        check(
            "the shared lock lives at the fixed state path",
            (broker.root / "state" / "bluefin-review" / "lab-findings.lock").exists(),
        )
        state = json.loads(broker.state_file().read_text())
        check_equal("state counts observations", state["findings"][identity]["count"], 2)
        check_equal("state records the opened issue", state["findings"][identity]["issue"], 451)

        broker.clear_logs()
        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=42, head=HEAD_PUBLISHED
        )
        check(
            "a third observation opens nothing new",
            "call: issue create" not in broker.log("gh"),
            broker.log("gh"),
        )
        check(
            "the issue this host opened is checked by number, not by search",
            "call: issue view" in broker.log("gh"),
            broker.log("gh"),
        )
        check(
            "unchanged evidence adds no comment",
            "call: issue comment" not in broker.log("gh"),
            broker.log("gh"),
        )
        check_equal(
            "a remembered open issue gives already-open",
            sorted(finding["state"] for finding in answer.get("findings", [])),
            ["already-open", "already-open"],
        )

        # A maintainer closing the issue is a decision, not a duplicate: the
        # same fault seen afterwards has to come back as new work.
        broker.fixture("issue-view.json", {"number": 451, "state": "CLOSED"})
        broker.clear_logs()
        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=42, head=HEAD_PUBLISHED
        )
        check(
            "a fault that returns after its issue was closed files again",
            "call: issue create" in broker.log("gh"),
            broker.log("gh"),
        )


def already_open_checks(registry: str) -> None:
    """An open issue carrying the marker is reused, never duplicated."""
    identity = BROKER_MODULE.fingerprint(
        {"class": "cluster-platform", "kind": "pod-failing", "subject": "argo/stuck-pod"}
    )
    marker = f"<!-- bluefin-review-lab-finding: v1 {identity} -->"
    with Broker("a", registry=registry) as broker:
        healthy_fixtures(broker)
        broker.fixture("pods.json", {"items": [pod("argo", "stuck-pod", "Failed")]})
        broker.fixture(
            "issues.json",
            [{"number": 12, "title": "lab: pod-failing on argo/stuck-pod", "body": marker + "\nolder evidence"}],
        )
        request = {"repository": "projectbluefin/bluefin", "pr": 7, "head": HEAD_PUBLISHED}

        broker.request("health", **request)
        broker.clear_logs()
        answer = broker.request("health", **request)
        findings = answer.get("findings", [])
        check_equal("an existing marker gives already-open", [f["state"] for f in findings], ["already-open"])
        check_equal("an existing issue number is reported", findings[0].get("issue"), 12)
        check(
            "an existing issue is never duplicated",
            "call: issue create" not in broker.log("gh"),
            broker.log("gh"),
        )
        check_equal(
            "a pre-existing issue records the current evidence once",
            broker.log("gh").count("call: issue comment"),
            1,
        )

        broker.clear_logs()
        broker.request("health", **request)
        check(
            "unchanged evidence adds no further comment",
            "call: issue comment" not in broker.log("gh"),
            broker.log("gh"),
        )
        check(
            "a known issue is rechecked by number",
            "argv: issue view 12 --repo projectbluefin/lab" in broker.log("gh"),
            broker.log("gh"),
        )

        # New evidence inside the hour still waits: an issue that grows a
        # comment on every poll is an issue a maintainer mutes.
        broker.fixture(
            "nodes.json",
            {
                "items": [
                    node("ghost", ready=False, link="up", observed=stamp()),
                    node("exo-0", link="up", observed=stamp()),
                ]
            },
        )
        broker.clear_logs()
        answer = broker.request("health", **request)
        states = {finding["subject"]: finding["state"] for finding in answer.get("findings", [])}
        check(
            "comments are rate limited to one per fingerprint per hour",
            "call: issue comment" not in broker.log("gh"),
            broker.log("gh"),
        )
        check_equal("a new fault is suppressed on first sight", states.get("ghost"), "suppressed")
        check_equal("the known fault stays already-open", states.get("argo/stuck-pod"), "already-open")


def unroutable_checks(registry: str) -> None:
    """Faults nobody owns are reported back, not filed anywhere."""
    with Broker("r", registry=registry) as broker:
        healthy_fixtures(broker)
        broker.fixture(
            "pods.json",
            {"items": [pod("scratch", f"experiment-{index}", "Failed") for index in range(25)]},
        )
        request = {"repository": "projectbluefin/bluefin", "pr": 9, "head": HEAD_PUBLISHED}

        answer = broker.request("health", **request)
        evidence = answer.get("evidence", {})
        check_equal("failing pod names are capped at ten", len(evidence["pods"]["failing"]), 10)
        check_equal("the full failing count survives the cap", evidence["pods"]["failing_total"], 25)
        check_equal("findings are capped", len(answer.get("findings", [])), 8)
        check(
            "unroutable findings are reported as such",
            {finding["state"] for finding in answer["findings"]} == {"unroutable"},
            repr(answer["findings"])[:400],
        )
        check_equal(
            "an unroutable finding names no repository",
            {finding["repository"] for finding in answer["findings"]},
            {""},
        )
        broker.request("health", **request)
        check("an unroutable finding never calls gh", broker.log("gh") == "", broker.log("gh"))
        check(
            "an unroutable fault still gets a diagnosis",
            "--from workflowtemplate/k8sgpt-on-demand" in broker.log("argo"),
        )


def filing_failure_checks(registry: str) -> None:
    """A broken `gh` is visible in the answer and fatal to nothing."""
    with Broker("g", registry=registry, mode={"LAB_GH_MODE": "fail"}) as broker:
        healthy_fixtures(broker)
        broker.fixture("pods.json", {"items": [pod("argo", "stuck-pod", "Failed")]})
        request = {"repository": "projectbluefin/bluefin", "pr": 11, "head": HEAD_PUBLISHED}

        broker.request("health", **request)
        answer = broker.request("health", **request)
        findings = answer.get("findings", [])
        check("health still answers when gh fails", answer.get("ok") is True)
        check_equal("a failed filing is visible", [f["state"] for f in findings], ["failed"])
        check("a failed filing explains itself", bool(findings[0].get("detail")), repr(findings[0]))
        check(
            "a failed filing leaks no credential text",
            "Bad credentials" not in json.dumps(answer) or "401" in json.dumps(answer),
        )


def submit_failure_checks(registry: str) -> None:
    """A host tool that fails is `unavailable`, not a crash."""
    with Broker("x", registry=registry, mode={"LAB_ARGO_MODE": "fail"}) as broker:
        healthy_fixtures(broker)
        answer = broker.request(
            "submit",
            repository="projectbluefin/bluefin",
            pr=5,
            head=HEAD_PUBLISHED,
            profile="bluefin-qa-pipeline",
        )
        check_equal("a failing argo is unavailable", answer.get("error"), "unavailable")
        check("an unavailable answer is not ok", answer.get("ok") is False)
        check("an unavailable answer explains itself", bool(answer.get("detail")))


def degradation_checks(registry: str) -> None:
    """No kubectl and a failing kubectl both degrade to a real answer."""
    with Broker("n", registry=registry, tools=("argo", "gh")) as broker:
        answer = broker.request("status")
        check("status answers without kubectl", answer.get("ok") is True, repr(answer))
        check_equal("a missing kubectl is DEGRADED", answer.get("state"), "DEGRADED")
        check_equal("a missing kubectl reports no activity", answer.get("active"), False)
        check_equal("a missing kubectl reports no fresh link", answer.get("usb4", {}).get("fresh"), False)
        check_equal("a missing kubectl reports unknown links", answer.get("usb4", {}).get("ghost"), "unknown")
        check("a degraded status still lists profiles", "bluefin-qa-pipeline" in answer.get("profiles", []))
        check("a degraded status explains itself", bool(answer.get("detail")))

        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=3, head=HEAD_PUBLISHED
        )
        check("health answers without kubectl", answer.get("ok") is True, repr(answer)[:300])
        check_equal("health without kubectl is DEGRADED", answer.get("state"), "DEGRADED")
        check("health without kubectl names what is missing", bool(answer["evidence"]["unavailable"]))
        check_equal("health without evidence files nothing", answer.get("findings"), [])
        check("no evidence means no diagnosis", broker.log("argo") == "", broker.log("argo"))

    with Broker("k", registry=registry, mode={"LAB_KUBECTL_MODE": "fail"}) as broker:
        healthy_fixtures(broker)
        answer = broker.request("status")
        check_equal("a failing kubectl is DEGRADED", answer.get("state"), "DEGRADED")
        check("a failing kubectl still answers well-formed", answer.get("version") == 1)
        answer = broker.request(
            "health", repository="projectbluefin/bluefin", pr=3, head=HEAD_PUBLISHED
        )
        check("health survives a failing kubectl", answer.get("ok") is True)
        check_equal("health with a failing kubectl is DEGRADED", answer.get("state"), "DEGRADED")


def probe_checks(registry: str) -> None:
    """The launcher's pre-flight question: can this host broker at all?"""
    working = Broker("pb", registry=registry)
    healthy_fixtures(working)
    result = subprocess.run(
        [sys.executable, str(BROKER), "probe"],
        env=working.environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    answer = json.loads(result.stdout)
    check_equal("a reachable cluster probes usable", answer.get("usable"), True)
    check_equal("probe exits zero when usable", result.returncode, 0)
    check_equal("probe names the current context", answer.get("context"), "ghost-lab")
    check("probe prints no url", "https://" not in result.stdout)

    missing = Broker("pn", registry=registry, tools=("argo",))
    result = subprocess.run(
        [sys.executable, str(BROKER), "probe"],
        env=missing.environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    answer = json.loads(result.stdout)
    check_equal("a host without kubectl probes unusable", answer.get("usable"), False)
    check_equal("probe exits nonzero when unusable", result.returncode, 1)
    check("an unusable probe explains itself", bool(answer.get("detail")))


def module_checks() -> None:
    """Guards that live in the module, tested where they are enforced."""
    check(
        "the profile map is exactly the reviewed pair",
        sorted(BROKER_MODULE.SAFE_PROFILES) == ["bluefin-qa-pipeline", "k8sgpt-on-demand"],
    )
    check(
        "the qa profile pins an image and names its repositories",
        BROKER_MODULE.SAFE_PROFILES["bluefin-qa-pipeline"]["needs_image"] is True
        and BROKER_MODULE.SAFE_PROFILES["bluefin-qa-pipeline"]["repositories"]
        == ("projectbluefin/bluefin", "projectbluefin/bluefin-lts", "projectbluefin/dakota"),
    )
    check(
        "the diagnosis profile applies to every repository and needs no image",
        BROKER_MODULE.SAFE_PROFILES["k8sgpt-on-demand"]["repositories"] == ()
        and BROKER_MODULE.SAFE_PROFILES["k8sgpt-on-demand"]["needs_image"] is False,
    )
    check("the shipped map passes its own guard", BROKER_MODULE.verify_profile_map() is None)
    for refused in ("install-lab", "teardown-nodes", "toggle-usb4", "cluster-reset", "upgrade-k3s"):
        check(f"the guard refuses {refused!r}", BROKER_MODULE.is_refused_template(refused))
    for allowed in ("bluefin-qa-pipeline", "k8sgpt-on-demand"):
        check(f"the guard allows {allowed!r}", not BROKER_MODULE.is_refused_template(allowed))
    raised = False
    try:
        BROKER_MODULE.verify_profile_map(
            {"lab-install": {"template": "install-lab", "needs_image": False, "repositories": ()}}
        )
    except RuntimeError:
        raised = True
    check("an unsafe profile fails the guard at import", raised)

    check_equal(
        "a fingerprint is sixteen stable hex characters",
        BROKER_MODULE.fingerprint({"class": "cluster-platform", "kind": "node-not-ready", "subject": "ghost"}),
        BROKER_MODULE.fingerprint({"class": "cluster-platform", "kind": "node-not-ready", "subject": "ghost"}),
    )
    check(
        "different subjects fingerprint differently",
        BROKER_MODULE.fingerprint({"class": "cluster-platform", "kind": "node-not-ready", "subject": "ghost"})
        != BROKER_MODULE.fingerprint({"class": "cluster-platform", "kind": "node-not-ready", "subject": "exo-0"}),
    )
    redacted = BROKER_MODULE.redact(
        "kubeconfig at https://10.0.0.4:6443 with token ghp_abcdefghijklmnopqrstuvwxyz012345"
    )
    check("redaction removes urls", "10.0.0.4" not in redacted and "https://" not in redacted, redacted)
    check("redaction removes tokens", "ghp_abcdefghij" not in redacted, redacted)

    oversized = BROKER_MODULE.bounded_response(
        {
            "version": 1,
            "ok": True,
            "state": "DEGRADED",
            "evidence": {"prometheus": {"samples": ["x" * 400000]}, "nodes": {"ready": 1}},
            "findings": [],
            "detail": "",
        }
    )
    check("an oversized response is capped", len(oversized) <= 262144, str(len(oversized)))
    check("a capped response says so", b'"truncated":true' in oversized, oversized[:200].decode())


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    FakeRegistry.published = {f"projectbluefin/bluefin:sha-{HEAD_PUBLISHED}": PINNED_DIGEST}
    server, registry = start_registry()
    try:
        module_checks()
        socket_checks(registry)
        protocol_checks(registry)
        status_checks(registry)
        health_checks(registry)
        already_open_checks(registry)
        unroutable_checks(registry)
        filing_failure_checks(registry)
        submit_failure_checks(registry)
        degradation_checks(registry)
        probe_checks(registry)
    finally:
        server.shutdown()

    total = PASSED + len(FAILURES)
    for failure in FAILURES:
        print(f"FAIL: {failure}")
    print(f"lab broker contract: {PASSED}/{total} checks passed")
    if FAILURES:
        print(f"scenario output kept in {WORK}")
        return 1
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
