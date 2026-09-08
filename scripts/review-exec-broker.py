#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import re
import signal
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
ACTIONS = ("status", "submit", "logs", "cancel")
NAMESPACE = "bluefin-system"
JOB_DEADLINE_SECONDS = 3600
JOB_TTL_SECONDS = 3600
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 262144
READ_TIMEOUT_SECONDS = 30.0
WRITE_TIMEOUT_SECONDS = 30.0
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BACKENDS = frozenset({"goose", "codex"})
EFFORTS = frozenset({"low", "medium", "high", "max"})


class Rejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class HostResult:
    argv: tuple[str, ...]
    code: int
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.reason


def run_host(argv: list[str] | tuple[str, ...], *, input_text: str = "", timeout: int = 30) -> HostResult:
    try:
        result = subprocess.run(
            list(argv),
            input=input_text,
            stdin=subprocess.PIPE if input_text else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return HostResult(tuple(argv), 127, reason=f"{argv[0]} is not installed")
    except subprocess.TimeoutExpired:
        return HostResult(tuple(argv), 124, reason=f"{argv[0]} timed out")
    except OSError as error:
        return HostResult(tuple(argv), 126, reason=str(error))
    reason = "" if result.returncode == 0 else (result.stderr.strip() or f"exit {result.returncode}")[:240]
    return HostResult(tuple(argv), result.returncode, result.stdout, result.stderr, reason)


def run_kubectl(args: list[str], *, input_text: str = "", timeout: int = 30) -> HostResult:
    return run_host(["kubectl", *args], input_text=input_text, timeout=timeout)


@dataclass(frozen=True)
class BrokerContext:
    session: str
    image: str
    namespace: str = NAMESPACE


def _require(value: Any, pattern: re.Pattern, message: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Rejected("bad-request", message)
    return value


def require_repository(request: dict) -> str:
    return _require(request.get("repository"), REPOSITORY_RE, "repository must be owner/repo")


def require_sha(request: dict, field: str) -> str:
    return _require(request.get(field), SHA_RE, f"{field} must be a full lowercase SHA")


def require_number(request: dict) -> int:
    value = request.get("number")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Rejected("bad-request", "number must be a positive integer")
    return value


def require_backend(request: dict) -> str:
    backend = request.get("backend")
    if backend not in BACKENDS:
        raise Rejected("bad-request", "backend must be goose or codex")
    return str(backend)


def require_effort(request: dict) -> str:
    effort = request.get("effort")
    if effort not in EFFORTS:
        raise Rejected("bad-request", "effort is unsupported")
    return str(effort)


def sanitize_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:63]


def decode_request(raw: bytes, context: BrokerContext) -> dict:
    if len(raw) > MAX_REQUEST_BYTES:
        raise Rejected("bad-request", "request is too large")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise Rejected("bad-request", "request is not JSON") from error
    if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
        raise Rejected("bad-request", "version is missing or unsupported")
    if request.get("action") not in ACTIONS:
        raise Rejected("unknown-action", "actions are status, submit, logs, cancel")
    if not isinstance(request.get("session"), str) or not hmac.compare_digest(
        request["session"], context.session
    ):
        raise Rejected("wrong-session", "request session does not match the broker")
    return request


def job_manifest(context: BrokerContext, request: dict) -> dict:
    repository = require_repository(request)
    number = require_number(request)
    base_sha = require_sha(request, "base_sha")
    head_sha = require_sha(request, "head_sha")
    backend = require_backend(request)
    model = _require(request.get("model"), re.compile(r"^[A-Za-z0-9._-]+$"), "model is invalid")
    effort = require_effort(request)
    labels = {
        "app.kubernetes.io/name": "review-exec",
        "review.owner": "review-exec",
        "review.session": sanitize_label(context.session),
        "review.repository": sanitize_label(repository.replace("/", "_")),
        "review.pr": str(number),
        "review.head": head_sha,
    }
    args = [
        "--repository", repository,
        "--pull-request", str(number),
        "--base-sha", base_sha,
        "--head-sha", head_sha,
        "--backend", backend,
        "--model", model,
        "--effort", effort,
        "--check-scope-version", os.environ.get("BLUEFIN_REVIEW_SCOPE_VERSION", "image-v1"),
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "generateName": "review-exec-",
            "namespace": context.namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": JOB_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "review-contributor",
                    "automountServiceAccountToken": False,
                    "containers": [{
                        "name": "review",
                        "image": context.image,
                        "command": ["/usr/local/bin/bluefin-review", "receipt"],
                        "args": args,
                        "envFrom": [{
                            "secretRef": {
                                "name": "review-contributor-secret",
                                "optional": True,
                            }
                        }],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                        },
                    }],
                },
            },
        },
    }


def handle_submit(context: BrokerContext, request: dict) -> dict:
    manifest = job_manifest(context, request)
    result = run_kubectl(["create", "-f", "-"], input_text=json.dumps(manifest))
    if not result.ok:
        return {"version": PROTOCOL_VERSION, "ok": False, "error": "unavailable", "detail": result.reason[:240]}
    job = result.stdout.strip().split("/")[-1]
    return {"version": PROTOCOL_VERSION, "ok": True, "result": "submitted", "job": job}


def handle_status(context: BrokerContext, _request: dict) -> dict:
    result = run_kubectl([
        "get", "jobs.batch", "-n", context.namespace,
        "-l", f"review.owner=review-exec,review.session={sanitize_label(context.session)}",
        "-o", "json",
    ])
    if not result.ok:
        return {"version": PROTOCOL_VERSION, "ok": False, "error": "unavailable", "detail": result.reason[:240]}
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return {"version": PROTOCOL_VERSION, "ok": False, "error": "unavailable", "detail": "kubectl returned invalid JSON"}
    jobs = []
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        jobs.append({
            "job": metadata.get("name", ""),
            "active": int(status.get("active", 0) or 0),
            "succeeded": int(status.get("succeeded", 0) or 0),
            "failed": int(status.get("failed", 0) or 0),
        })
    return {"version": PROTOCOL_VERSION, "ok": True, "jobs": jobs}


def _job_name(request: dict) -> str:
    value = request.get("job")
    if not isinstance(value, str) or not re.fullmatch(r"review-exec-[a-z0-9-]+", value):
        raise Rejected("bad-request", "job is invalid")
    return value


def handle_logs(context: BrokerContext, request: dict) -> dict:
    job = _job_name(request)
    result = run_kubectl(["logs", "job/" + job, "-n", context.namespace], timeout=30)
    return {
        "version": PROTOCOL_VERSION,
        "ok": result.ok,
        "logs": result.stdout[-120_000:],
        "detail": result.reason[:240],
    }


def handle_cancel(context: BrokerContext, request: dict) -> dict:
    job = _job_name(request)
    result = run_kubectl([
        "delete", "job", job, "-n", context.namespace,
        "--ignore-not-found=true",
    ])
    return {
        "version": PROTOCOL_VERSION,
        "ok": result.ok,
        "cancelled": job,
        "detail": result.reason[:240],
    }


def cancel_session_jobs(context: BrokerContext) -> None:
    run_kubectl([
        "delete", "jobs.batch", "-n", context.namespace,
        "-l", f"review.owner=review-exec,review.session={sanitize_label(context.session)}",
        "--ignore-not-found=true",
    ])


def sweep_orphans(context: BrokerContext) -> None:
    run_kubectl([
        "delete", "jobs.batch", "-n", context.namespace,
        "-l", "review.owner=review-exec",
        "--field-selector", "status.conditions.type=Complete",
        "--ignore-not-found=true",
    ])


def dispatch(context: BrokerContext, raw: bytes) -> dict:
    try:
        request = decode_request(raw, context)
        action = request["action"]
        if action == "submit":
            return handle_submit(context, request)
        if action == "status":
            return handle_status(context, request)
        if action == "logs":
            return handle_logs(context, request)
        return handle_cancel(context, request)
    except Rejected as error:
        return {"version": PROTOCOL_VERSION, "ok": False, "error": error.code, "detail": error.detail}
    except Exception as error:
        return {"version": PROTOCOL_VERSION, "ok": False, "error": "unavailable", "detail": type(error).__name__}


def read_request_line(sock) -> bytes | None:
    buffer = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buffer.extend(chunk)
        if b"\n" in buffer:
            line, _ = buffer.split(b"\n", 1)
            return bytes(line)
        if len(buffer) > MAX_REQUEST_BYTES:
            raise Rejected("bad-request", "request line is too long")


def bounded_response(payload: dict) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_RESPONSE_BYTES:
        data = json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "ok": False,
                "error": "response-too-large",
                "detail": "response exceeded byte cap",
            },
            separators=(",", ":"),
        ).encode("utf-8")
    return data + b"\n"


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
            connection.settimeout(WRITE_TIMEOUT_SECONDS)
            connection.sendall(line)
        except OSError:
            return


class BrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, context: BrokerContext) -> None:
        self.context = context
        super().__init__(path, BrokerHandler)

    def handle_error(self, request, client_address) -> None:
        return


def prepare_socket_path(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.unlink(path)


def serve(path: str, context: BrokerContext) -> int:
    prepare_socket_path(path)
    previous_umask = os.umask(0o177)
    try:
        server = BrokerServer(path, context)
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)

    def stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sweep_orphans(context)

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
        try:
            cancel_session_jobs(context)
        except Exception:
            pass
        server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(path)
    return 0


def probe(timeout: int = 10) -> int:
    context_result = run_host(["kubectl", "config", "current-context"], timeout=timeout)
    if not context_result.ok:
        answer = {"usable": False, "context": "", "detail": context_result.reason[:120]}
        print(json.dumps(answer, separators=(",", ":"), sort_keys=True), flush=True)
        return 1
    current = context_result.stdout.strip()[:63]
    nodes = run_host(["kubectl", "get", "nodes", "-o", "name"], timeout=timeout)
    answer = {
        "usable": nodes.ok,
        "context": current,
        "detail": "cluster reachable" if nodes.ok else nodes.reason[:120],
    }
    print(json.dumps(answer, separators=(",", ":"), sort_keys=True), flush=True)
    return 0 if nodes.ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="review-exec-broker.py",
        description="Host-side broker for optional review job execution from the review container.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve_parser = subcommands.add_parser("serve", help="serve the broker socket until stopped")
    serve_parser.add_argument("--socket", required=True, help="unix socket path to bind")
    serve_parser.add_argument("--session", required=True, help="session id every request must carry")
    serve_parser.add_argument("--image", required=True, help="container image for review jobs")

    subcommands.add_parser("probe", help="probe host kubectl reachability")

    args = parser.parse_args(argv)
    if args.command == "probe":
        return probe()
    if args.command == "serve":
        context = BrokerContext(session=args.session, image=args.image)
        return serve(args.socket, context)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
