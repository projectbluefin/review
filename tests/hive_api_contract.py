"""Executable contract for the dashboard's Hive HTTP client."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "image" / "tui"))

import hive_api


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Handler(BaseHTTPRequestHandler):
    login_posts = 0

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        oversized = b'{"pad":"' + b"x" * hive_api.MAX_BODY + b'"}'
        cases = {
            "/ok": (200, b'{"hub":"online"}', "application/json"),
            "/auth": (401, b'{"error":"secret-token"}', "application/json"),
            "/forbidden": (403, b'{"error":"no standing"}', "application/json"),
            "/login-edge": (302, b"", "text/html"),
            "/html": (200, b"<!doctype html>\n<html>secret-token</html>", "text/html"),
            "/broken-json": (200, b'{"hub":', "application/json"),
            "/list": (200, b'["a", "list"]', "application/json"),
            "/oversized": (200, oversized, "application/json"),
            "/server": (503, b'{"error":"maintenance"}', "application/json"),
        }
        code, body, content_type = cases[self.path]
        if self.path == "/login-edge":
            self.send_response(code)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        self._reply(code, body, content_type)

    def do_POST(self) -> None:
        if self.path == "/queue-redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if self.path == "/login":
            type(self).login_posts += 1
            self._reply(200, b'{"status":"queued"}')
            return
        if self.path == "/queue-false-success":
            self._reply(200, b'{"status":"error","error":"not queued"}')
            return
        self._reply(200, b'{"status":"queued"}')


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        oversized_excerpt = '{"pad":"' + "x" * (hive_api.MAX_SNIPPET - len('{"pad":"'))
        expected = {
            "/auth": "authentication rejected (401)",
            "/forbidden": "authorization rejected (403)",
            "/login-edge": "API routing redirected (302)",
            "/html": (
                "malformed API response: 200 text/html "
                "'<!doctype html> <html>[redacted]</html>'"
            ),
            "/broken-json": (
                "malformed API response: 200 application/json, invalid JSON '{\"hub\":'"
            ),
            "/list": (
                "malformed API response: 200 application/json, not a JSON object "
                "'[\"a\", \"list\"]'"
            ),
            "/oversized": (
                f"malformed API response: 200 application/json, "
                f"over {hive_api.MAX_BODY} bytes '{oversized_excerpt}'"
            ),
            "/server": "Hive server error (503)",
        }
        ok = hive_api.request(f"{base}/ok", "secret-token")
        check(ok.ok and ok.data == {"hub": "online"}, f"valid JSON failed: {ok}")
        for path, message in expected.items():
            result = hive_api.request(f"{base}{path}", "secret-token")
            check(not result.ok, f"{path} must fail")
            check(result.message == message, f"{path}: {result.message!r}")
            check("secret-token" not in json.dumps(result.as_dict()), "token leaked")
            check(len(json.dumps(result.as_dict())) < 600, "error is not bounded")

        # The malformed classification must name its own shape (#337): the
        # status, content type, byte count, and a bounded redacted excerpt
        # are what tell an intercepted SPA page apart from invalid JSON.
        html = hive_api.request(f"{base}/html", "secret-token")
        check(html.category == "malformed", str(html))
        html_detail = html.data.get("detail", "")
        check("status=200" in html_detail, html_detail)
        check("content-type=text/html" in html_detail, html_detail)
        check("bytes=" in html_detail, html_detail)
        check("<!doctype html> <html>[redacted]</html>" in html_detail, html_detail)

        broken = hive_api.request(f"{base}/broken-json", "secret-token")
        check(broken.category == "malformed", str(broken))
        broken_detail = broken.data.get("detail", "")
        check("content-type=application/json" in broken_detail, broken_detail)
        check("body='{\"hub\":'" in broken_detail, broken_detail)

        missing = hive_api.request(f"{base}/ok", "")
        check(missing.message == "authentication token missing", str(missing))
        network = hive_api.request("http://127.0.0.1:1/status", "secret-token", timeout=0.1)
        check(network.message == "network error", str(network))

        malformed_url = hive_api.request("https://[", "secret-token")
        check(not malformed_url.ok, "malformed Hive URL must fail")
        check(malformed_url.category == "configuration", str(malformed_url))
        check(malformed_url.message == "invalid Hive API URL", str(malformed_url))
        check("secret-token" not in json.dumps(malformed_url.as_dict()), "URL error leaked token")

        env = {**os.environ, "GH_TOKEN": "secret-token"}
        helper = str(ROOT / "image" / "tui" / "hive_api.py")
        malformed_cli = subprocess.run(
            [sys.executable, helper, "queue", "https://["],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        check(malformed_cli.returncode != 0, "malformed queue URL must fail")
        check("invalid Hive API URL" in malformed_cli.stderr, malformed_cli.stderr)
        check("Traceback" not in malformed_cli.stderr, "URL failure leaked a traceback")
        check("secret-token" not in malformed_cli.stderr, "URL failure leaked token")

        redirected = subprocess.run(
            [sys.executable, helper, "queue", f"{base}/queue-redirect"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        check(redirected.returncode != 0, "redirected mutation must fail")
        check("API routing redirected (302)" in redirected.stderr, redirected.stderr)
        check(Handler.login_posts == 0, "mutating POST was replayed against login")
        check("secret-token" not in redirected.stderr, "CLI leaked token")

        control_token = "dummy-line-one\ndummy-line-two"
        invalid_header = subprocess.run(
            [sys.executable, helper, "queue", f"{base}/queue-ok"],
            capture_output=True,
            text=True,
            env={**os.environ, "GH_TOKEN": control_token},
            timeout=5,
        )
        check(invalid_header.returncode != 0, "control characters must reject the token")
        check("invalid authentication token" in invalid_header.stderr, invalid_header.stderr)
        check("Traceback" not in invalid_header.stderr, "header failure leaked a traceback")
        for fragment in control_token.splitlines():
            check(fragment not in invalid_header.stderr, "header failure leaked token data")

        false_success = subprocess.run(
            [sys.executable, helper, "queue", f"{base}/queue-false-success"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        check(false_success.returncode != 0, "non-queued response must fail")
        check("queued" not in false_success.stdout, "false queued output")

        queued = subprocess.run(
            [sys.executable, helper, "queue", f"{base}/queue-ok"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        check(queued.returncode == 0, queued.stderr)
        check(json.loads(queued.stdout) == {"status": "queued"}, queued.stdout)
    finally:
        server.shutdown()
        server.server_close()

    print("hive API contract: PASS")


if __name__ == "__main__":
    main()
