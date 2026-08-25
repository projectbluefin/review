"""Small, redirect-safe HTTP client for the dashboard's Hive API calls."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass


MAX_DETAIL = 240
MAX_SNIPPET = 120
MAX_BODY = 16_384


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class Result:
    ok: bool
    category: str
    message: str
    data: dict

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "category": self.category,
            "message": self.message,
            "data": self.data,
        }


def _bounded(value: object, limit: int = MAX_DETAIL) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    return " ".join(text.split())[:limit]


def _failure(category: str, message: str, detail: object = "") -> Result:
    bounded = _bounded(detail)
    return Result(False, category, message, {"detail": bounded} if bounded else {})


def _http_failure(code: int, body: bytes, token: str) -> Result:
    detail = body[:MAX_BODY].decode("utf-8", "replace").replace(token, "[redacted]")
    if 300 <= code < 400:
        return _failure("routing", f"API routing redirected ({code})")
    if code == 401:
        return _failure("authentication", "authentication rejected (401)", detail)
    if code == 403:
        return _failure("authorization", "authorization rejected (403)", detail)
    if code >= 500:
        return _failure("server", f"Hive server error ({code})", detail)
    return _failure("http", f"API request rejected ({code})", detail)


def _malformed(code: int, content_type: str, body: bytes, token: str, reason: str) -> Result:
    """A 2xx that is not a JSON object names its own shape (#337).

    `malformed API response` alone cannot distinguish an intercepted SPA page
    from invalid JSON, so the failure carries the status, content type, byte
    count, and a bounded redacted body excerpt.
    """
    excerpt = _bounded(
        body[:MAX_BODY].decode("utf-8", "replace").replace(token, "[redacted]"),
        MAX_SNIPPET,
    )
    shape = f"{code} {content_type}"
    if len(body) > MAX_BODY:
        shape = f"{shape}, over {MAX_BODY} bytes"
    if reason:
        shape = f"{shape}, {reason}"
    detail = f"status={code} content-type={content_type} bytes={len(body)}"
    message = f"malformed API response: {shape}"
    if excerpt:
        detail = f"{detail} body={excerpt!r}"
        message = f"{message} '{excerpt}'"
    return _failure("malformed", message, detail)


def request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    timeout: float = 15,
    opener=None,
) -> Result:
    if not token.strip():
        return _failure("authentication", "authentication token missing")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in token):
        return _failure("authentication", "invalid authentication token")
    client = opener or urllib.request.build_opener(NoRedirect())
    try:
        http_request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    except ValueError:
        return _failure("configuration", "invalid Hive API URL")
    try:
        with client.open(http_request, timeout=timeout) as response:
            code = response.getcode()
            body = response.read(MAX_BODY + 1)
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return _http_failure(error.code, error.read(MAX_BODY), token)
    except ValueError:
        return _failure("authentication", "invalid authentication token")
    except (urllib.error.URLError, TimeoutError, OSError):
        return _failure("network", "network error")

    if not 200 <= code < 300:
        return _http_failure(code, body, token)
    if len(body) > MAX_BODY or content_type != "application/json":
        return _malformed(code, content_type, body, token, "")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _malformed(code, content_type, body, token, "invalid JSON")
    if not isinstance(data, dict):
        return _malformed(code, content_type, body, token, "not a JSON object")
    return Result(True, "ok", "online", data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("queue",))
    parser.add_argument("url")
    args = parser.parse_args()
    result = request(args.url, os.environ.get("GH_TOKEN", ""), method="POST")
    if not result.ok:
        print(json.dumps(result.as_dict(), separators=(",", ":")), file=sys.stderr)
        return 1
    if result.data.get("status") != "queued":
        failure = _failure("response", "Hive did not confirm queueing")
        print(json.dumps(failure.as_dict(), separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps({"status": "queued"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
