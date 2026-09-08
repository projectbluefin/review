"""Manifest probe contract: presence, absence, auth, and error semantics."""

from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "image"))

from tui import landing  # noqa: E402


class RegistryHandler(BaseHTTPRequestHandler):
    mode = "success"
    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        type(self).requests.append(self.path)
        if path == "/token":
            self._json(200, {"token": "test-token"})
            return
        if path != "/v2/org/repo/manifests/stable":
            self._json(200, {"tags": ["stable"]})
            return
        mode = type(self).mode
        if mode == "missing":
            self._json(404, {})
        elif mode in {"auth401", "auth403"}:
            self._json(int(mode[-3:]), {})
        elif mode == "invalid":
            self._raw(200, b"not-json")
        elif mode == "transport":
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        else:
            self._json(
                200,
                {"manifests": [{"digest": "sha256:child"}]},
                {"docker-content-digest": "sha256:manifest"},
            )

    def _json(self, status: int, body: dict, headers: dict | None = None) -> None:
        self._raw(status, json.dumps(body).encode(), headers)

    def _raw(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class ManifestProbeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.original_base = landing.GHCR_BASE
        landing.GHCR_BASE = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        landing.GHCR_BASE = cls.original_base
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def probe(self, mode: str) -> tuple[dict, int, list[str]]:
        RegistryHandler.mode = mode
        RegistryHandler.requests = []
        answer, status = landing.probe_package("Org/Repo", "stable")
        return answer, status, list(RegistryHandler.requests)

    def assert_manifest_only(self, requests: list[str]) -> None:
        self.assertIn("/v2/org/repo/manifests/stable", " ".join(requests))
        self.assertFalse(
            any("/tags/list" in request for request in requests),
            f"manifest lookup must not request tags/list: {requests}",
        )

    def test_success_is_present_and_keeps_digest_children(self) -> None:
        answer, status, requests = self.probe("success")
        self.assertEqual(status, 0)
        self.assertTrue(answer["present"])
        self.assertTrue(answer["readable"])
        self.assertEqual(answer["digest"], "sha256:manifest")
        self.assertEqual(answer["children"], ["sha256:child"])
        self.assert_manifest_only(requests)

    def test_404_is_absent_without_success_fields(self) -> None:
        answer, status, requests = self.probe("missing")
        self.assertEqual(status, 0)
        self.assertFalse(answer["present"])
        self.assertNotIn("digest", answer)
        self.assert_manifest_only(requests)

    def test_auth_denial_is_not_proven_absence(self) -> None:
        for mode in ("auth401", "auth403"):
            with self.subTest(mode=mode):
                answer, status, requests = self.probe(mode)
                self.assertEqual(status, 0)
                self.assertFalse(answer["readable"])
                self.assertNotIn("present", answer)
                self.assert_manifest_only(requests)

    def test_invalid_response_is_an_error_without_presence(self) -> None:
        answer, status, requests = self.probe("invalid")
        self.assertEqual(status, 1)
        self.assertIn("error", answer)
        self.assertNotIn("present", answer)
        self.assert_manifest_only(requests)

    def test_transport_failure_is_an_error_without_presence(self) -> None:
        answer, status, requests = self.probe("transport")
        self.assertEqual(status, 1)
        self.assertIn("error", answer)
        self.assertNotIn("present", answer)
        self.assert_manifest_only(requests)


if __name__ == "__main__":
    unittest.main()
