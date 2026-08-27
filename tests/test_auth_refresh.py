from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from chatgpt_web_adapter import auth_refresh
from chatgpt_web_adapter.auth import CHATGPT_SESSION_COOKIE
from chatgpt_web_adapter.auth_refresh import refresh_auth_session


class RefreshClient:
    """Small refresh client that records externally meaningful cookie updates."""

    def __init__(self, auth_file) -> None:
        self.auth_file = auth_file
        self.auth = SimpleNamespace(
            accessToken="old-access",
            accessTokenSource="file",
            expires=None,
            cookies={CHATGPT_SESSION_COOKIE: "old-session", "oai-did": "device"},
            headers={"user-agent": "pytest"},
        )
        self.base_headers = {}
        self.timeout = 17
        self.response_headers = None

    def _build_headers(self, extra):
        headers = {
            "authorization": f"Bearer {self.auth.accessToken}",
            "cookie": "; ".join(f"{key}={value}" for key, value in self.auth.cookies.items()),
        }
        headers.update({key: value for key, value in extra.items() if value is not None})
        return headers

    def _update_cookies_from_text(self, header_text):
        self.response_headers = header_text
        if "rotated-session" in header_text:
            self.auth.cookies[CHATGPT_SESSION_COOKIE] = "rotated-session"


def test_refresh_auth_rotates_and_atomically_persists_session(tmp_path, monkeypatch) -> None:
    captured = {}

    def run_worker(command, *, input, capture_output, timeout, check):
        # Prove credentials cross only the private stdin boundary, never process arguments.
        captured["command"] = command
        captured["request"] = json.loads(input)
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        captured["check"] = check
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": 200,
                    "data": {
                        "accessToken": "new-access",
                        "sessionToken": "new-session",
                        "expires": "2030-01-01T00:00:00.000Z",
                    },
                    "set_cookie_headers": [
                        f"{CHATGPT_SESSION_COOKIE}=rotated-session; Path=/; Secure"
                    ],
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(auth_refresh.subprocess, "run", run_worker)
    auth_file = tmp_path / "auth_data.json"
    auth_file.write_text(
        json.dumps(
            {
                "accessToken": "old-access",
                "sessionToken": "old-session",
                "account": {"id": "preserve-me"},
                "proof_token": ["must", "disappear"],
                "turnstile_token": "must-disappear",
            }
        ),
        encoding="utf-8",
    )
    client = RefreshClient(auth_file)

    result = refresh_auth_session(client)

    assert captured["command"] == [
        auth_refresh.sys.executable,
        "-m",
        auth_refresh.AUTH_REFRESH_WORKER_MODULE,
    ]
    assert "old-session" not in " ".join(captured["command"])
    assert captured["request"]["headers"]["cookie"].endswith("old-session; oai-did=device")
    assert captured["request"]["headers"]["accept-encoding"] == "identity"
    assert captured["request"]["timeout"] == 17.0
    assert captured["capture_output"] is True
    assert captured["timeout"] == 17.0
    assert captured["check"] is False
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert result.session_token_rotated is True
    assert result.persisted is True
    assert client.auth.accessToken == "new-access"
    assert client.response_headers == (
        f"set-cookie: {CHATGPT_SESSION_COOKIE}=rotated-session; Path=/; Secure"
    )
    assert saved["accessToken"] == "new-access"
    assert saved["sessionToken"] == "new-session"
    assert saved["cookies"][CHATGPT_SESSION_COOKIE] == "rotated-session"
    assert saved["account"] == {"id": "preserve-me"}
    assert "proof_token" not in saved
    assert "turnstile_token" not in saved
    assert not list(tmp_path.glob("*.tmp"))


def test_refresh_auth_preserves_worker_http_status(tmp_path, monkeypatch) -> None:
    """Surface a reduced worker HTTP rejection without parsing credential data."""

    monkeypatch.setattr(
        auth_refresh.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=b'{"status": 403, "data": null, "set_cookie_headers": []}',
            stderr=b"",
        ),
    )
    client = RefreshClient(tmp_path / "auth_data.json")

    with pytest.raises(auth_refresh.AuthError, match=r"status=403$"):
        refresh_auth_session(client, persist=False)


def test_refresh_auth_enforces_worker_deadline(tmp_path, monkeypatch) -> None:
    """Terminate DNS, connection, or body stalls at the configured total timeout."""

    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(auth_refresh.subprocess, "run", time_out)
    client = RefreshClient(tmp_path / "auth_data.json")

    with pytest.raises(
        auth_refresh.AuthError,
        match="failed before receiving an HTTP response",
    ):
        refresh_auth_session(client, persist=False)
