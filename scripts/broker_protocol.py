#!/usr/bin/env python3
"""The one spelling of the review broker wire protocol (#492).

Both host-side brokers (review-exec-broker, review-lab-broker) speak the
same protocol over a unix socket: one newline-terminated JSON request
carrying a version, an action, and the session id, answered by one
newline-terminated JSON response. That layer — framing, byte caps, request
validation, the session check, and the socket server lifecycle — lives here
exactly once; a broker contributes only its action set, its handlers, and
its timeouts.

The overlong-request behavior is drain-then-answer: a client whose request
exceeds the cap gets a well-formed `bad-request`, never a reset connection.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import signal
import socketserver
import threading

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 262144
RECV_CHUNK_BYTES = 8192
# An overlong line is drained, not trusted: stop accumulating one byte past
# the cap so decode_request can answer, but keep reading so the answer is
# heard instead of racing a close.
MAX_DRAIN_BYTES = 4 * 1024 * 1024


class Rejected(Exception):
    """A request the protocol refuses. Carries the wire error code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def json_line(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def error_payload(code: str, detail: str) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": code,
        "detail": str(detail)[:240],
    }


def decode_request(raw: bytes, *, session: str, actions) -> dict:
    """Validate the envelope every broker shares; the action set is the broker's."""
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
    if action not in actions:
        raise Rejected("unknown-action", "actions are " + ", ".join(actions))
    request_session = request.get("session")
    if not isinstance(request_session, str) or not request_session:
        raise Rejected("bad-request", "session is missing or not a string")
    # The session id is the only thing distinguishing this container's
    # requests from another's on a shared host, so it is compared without a
    # timing signal.
    if not hmac.compare_digest(request_session, session):
        raise Rejected("wrong-session", "request session does not match this broker")
    return request


def read_request_line(connection) -> bytes | None:
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


def bounded_response(payload: dict) -> bytes:
    """Serialize within the wire cap; an oversized answer degrades to an error."""
    data = json_line(payload)
    if len(data) > MAX_RESPONSE_BYTES:
        data = json_line(error_payload("response-too-large", "response exceeded byte cap"))
    return data


class BrokerHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = self.request
        try:
            connection.settimeout(self.server.read_timeout)
            raw = read_request_line(connection)
        except OSError:
            return
        if raw is None:
            return
        payload = self.server.dispatch_request(self.server.context, raw)
        line = self.server.encode_response(payload)
        try:
            # Gathering evidence can outlast the read timeout, so the write
            # deadline is its own budget rather than the one the request
            # arrived under.
            connection.settimeout(self.server.write_timeout)
            connection.sendall(line)
        except OSError:
            return


class BrokerServer(socketserver.ThreadingUnixStreamServer):
    # A slow or absent handler must not stall the next caller: the dashboard
    # polls `status` while another call is still gathering evidence.
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        path: str,
        context,
        dispatch,
        *,
        read_timeout: float = 30.0,
        write_timeout: float = 30.0,
        encode_response=bounded_response,
    ) -> None:
        self.context = context
        self.dispatch_request = dispatch
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.encode_response = encode_response
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


def serve(
    path: str,
    context,
    dispatch,
    *,
    read_timeout: float = 30.0,
    write_timeout: float = 30.0,
    encode_response=bounded_response,
    on_start=None,
    on_stop=None,
) -> int:
    """Serve the broker socket until signalled; on_start/on_stop are the broker's hooks."""
    prepare_socket_path(path)
    # bind() honours the umask, so the socket is never briefly reachable by
    # anyone else; the chmod afterwards states the intent regardless of the
    # inherited mask.
    previous_umask = os.umask(0o177)
    try:
        server = BrokerServer(
            path,
            context,
            dispatch,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            encode_response=encode_response,
        )
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)

    def stop(_signum, _frame) -> None:
        # shutdown() blocks until serve_forever() returns, which would
        # deadlock if called from the thread running it.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if on_start is not None:
        on_start(context)

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
        if on_stop is not None:
            try:
                on_stop(context)
            except Exception:
                pass
        server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(path)
    return 0
