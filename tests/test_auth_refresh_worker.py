from __future__ import annotations

import json
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatgpt_web_adapter import auth_refresh_worker


class FakeSessionResponse:
    """Minimal context-managed response for the isolated HTTPS worker."""

    status = 200

    def __init__(self) -> None:
        self.headers = Message()
        self.headers.add_header("set-cookie", "rotated-cookie=value; Secure")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "accessToken": "new-access",
                "sessionToken": "new-session",
                "expires": "2030-01-01T00:00:00.000Z",
                "user": {"private": "must-not-cross-worker-boundary"},
            }
        ).encode()


def test_worker_reduces_session_response(monkeypatch) -> None:
    """Return only the session fields and cookies consumed by the parent."""

    captured = {}

    class FakeOpener:
        """Record the fixed request accepted by the worker."""

        def open(self, request, *, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeSessionResponse()

    def build_worker_opener(*handlers):
        return FakeOpener()

    monkeypatch.setattr(auth_refresh_worker, "build_opener", build_worker_opener)

    result = auth_refresh_worker.fetch_session(
        {"cookie": "private-session", "accept-encoding": "identity"}, 19.0
    )

    assert captured == {
        "url": auth_refresh_worker.SESSION_URL,
        "timeout": 19.0,
    }
    assert result == {
        "status": 200,
        "data": {
            "accessToken": "new-access",
            "sessionToken": "new-session",
            "expires": "2030-01-01T00:00:00.000Z",
        },
        "set_cookie_headers": ["rotated-cookie=value; Secure"],
    }


def test_worker_does_not_forward_credentials_across_redirect(monkeypatch) -> None:
    """Surface a redirect without contacting its credential-bearing destination."""

    requests = []

    class RedirectHandler(BaseHTTPRequestHandler):
        """Record source and destination requests from the real urllib handler chain."""

        def do_GET(self) -> None:
            """Redirect the source and record any forbidden destination request."""

            # Record only fake test credentials so a followed redirect is directly observable.
            requests.append(
                {
                    "path": self.path,
                    "cookie": self.headers.get("cookie"),
                    "authorization": self.headers.get("authorization"),
                }
            )
            if self.path == "/source":
                self.send_response(302)
                self.send_header(
                    "location",
                    f"http://127.0.0.1:{self.server.server_port}/destination",
                )
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args) -> None:
            """Suppress local-server noise in test output."""

            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(
        auth_refresh_worker,
        "SESSION_URL",
        f"http://127.0.0.1:{server.server_port}/source",
    )
    try:
        result = auth_refresh_worker.fetch_session(
            {
                "cookie": "fake-private-session",
                "authorization": "Bearer fake-private-access",
            },
            2.0,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)

    assert result == {"status": 302, "data": None, "set_cookie_headers": []}
    assert requests == [
        {
            "path": "/source",
            "cookie": "fake-private-session",
            "authorization": "Bearer fake-private-access",
        }
    ]


def test_worker_redacts_header_validation_failure(monkeypatch) -> None:
    """Collapse credential-bearing urllib failures to a secret-free result."""

    class InvalidHeaderOpener:
        """Reject a malformed header as urllib does before network I/O."""

        def open(self, request, *, timeout):
            raise ValueError("Invalid header value b'private-session-value'")

    monkeypatch.setattr(
        auth_refresh_worker,
        "build_opener",
        lambda *handlers: InvalidHeaderOpener(),
    )

    result = auth_refresh_worker.fetch_session(
        {"cookie": "private-session-value"}, 19.0
    )

    assert result == {"status": 0, "data": None, "set_cookie_headers": []}
    assert "private-session-value" not in json.dumps(result)
