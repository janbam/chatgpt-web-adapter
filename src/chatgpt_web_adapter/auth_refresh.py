from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .auth import (
    CHATGPT_SESSION_COOKIE,
    CHAT_URL,
    _get_access_token_expiry,
)
from .exceptions import AuthError
from .auth_store import persist_auth_data
from .web_session import _sync_device_header

SESSION_URL = f"{CHAT_URL.rstrip('/')}/api/auth/session"
AUTH_REFRESH_SKEW_SECONDS = 300
AUTH_REFRESH_WORKER_MODULE = "chatgpt_web_adapter.auth_refresh_worker"


@dataclass(frozen=True)
class AuthRefreshResult:
    """Redacted outcome of a successful ChatGPT session refresh."""

    status_code: int
    access_token_present: bool
    session_token_rotated: bool
    expires_present: bool
    persisted: bool


def auth_needs_refresh(access_token: str | None, *, now: datetime | None = None) -> bool:
    """Return whether an access token is absent or close to expiration."""

    if not isinstance(access_token, str) or not access_token.strip():
        return True
    expires_at = _get_access_token_expiry(access_token)
    if expires_at is None:
        return False
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    return expires_at <= current + timedelta(seconds=AUTH_REFRESH_SKEW_SECONDS)


def _persist_refreshed_auth(
    client: Any,
    path: Path,
    response: dict[str, Any],
) -> None:
    """Persist refreshed session material through the atomic auth store."""

    persist_auth_data(
        client.auth,
        path,
        session_token=response["sessionToken"].strip(),
        session_expires_at=response.get("expires"),
    )


def _request_session_json(client: Any, headers: dict[str, str]) -> tuple[int, Any]:
    """Run the Python HTTPS worker under one cancellable wall-clock deadline."""

    timeout = float(getattr(client, "timeout", 30))
    command = [sys.executable, "-m", AUTH_REFRESH_WORKER_MODULE]
    payload = json.dumps({"headers": headers, "timeout": timeout}).encode()
    try:
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Never retain worker details that could include credential-bearing input or output.
        raise AuthError(
            "ChatGPT session refresh failed before receiving an HTTP response"
        ) from None
    if completed.returncode != 0:
        raise AuthError(
            "ChatGPT session refresh failed before receiving an HTTP response"
        )
    try:
        result = json.loads(completed.stdout)
        status = int(result["status"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise AuthError("ChatGPT session refresh returned an invalid response") from None

    # Preserve any cookie rotation exposed through normal response headers.
    update_cookies = getattr(client, "_update_cookies_from_text", None)
    if callable(update_cookies):
        set_cookie_headers = result.get("set_cookie_headers", [])
        if isinstance(set_cookie_headers, list):
            update_cookies(
                "\n".join(
                    f"set-cookie: {value}"
                    for value in set_cookie_headers
                    if isinstance(value, str)
                )
            )
    return status, result.get("data")


def refresh_auth_session(
    client: Any,
    *,
    persist: bool = True,
    auth_file: str | Path | None = None,
) -> AuthRefreshResult:
    """Refresh access/session credentials through the live session endpoint."""

    # Refuse refresh without the durable browser session that authorizes it.
    cookies = getattr(getattr(client, "auth", None), "cookies", None)
    if not isinstance(cookies, dict) or not any(
        name == CHATGPT_SESSION_COOKIE or name.startswith(f"{CHATGPT_SESSION_COOKIE}.")
        for name in cookies
    ):
        raise AuthError("Session refresh requires a ChatGPT session cookie")
    previous_session = cookies.get(CHATGPT_SESSION_COOKIE)
    headers = client._build_headers(
        {
            "accept": "application/json",
            "content-type": None,
            "origin": CHAT_URL.rstrip("/"),
            "referer": CHAT_URL,
        }
    )
    headers["accept-encoding"] = "identity"
    headers.pop("content-type", None)

    # ChatGPT rejects curl's transport fingerprint here, so keep credentials in-process.
    status, data = _request_session_json(client, headers)
    if int(status) != 200 or not isinstance(data, dict):
        raise AuthError(f"ChatGPT session refresh failed: status={status}")

    # Validate the complete replacement credential set before mutating client state.
    access_token = data.get("accessToken")
    session_token = data.get("sessionToken")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AuthError("ChatGPT session refresh response has no accessToken")
    if not isinstance(session_token, str) or not session_token.strip():
        raise AuthError("ChatGPT session refresh response has no sessionToken")

    client.auth.accessToken = access_token.strip()
    client.auth.accessTokenSource = "session-refresh:accessToken"
    client.auth.expires = data.get("expires")
    _sync_device_header(client)

    # Persist only after the live client reflects the accepted response.
    target = Path(auth_file) if auth_file is not None else getattr(client, "auth_file", None)
    persisted = bool(persist and isinstance(target, Path))
    if persisted:
        _persist_refreshed_auth(client, target, data)
    return AuthRefreshResult(
        status_code=int(status),
        access_token_present=True,
        session_token_rotated=previous_session != session_token.strip(),
        expires_present=data.get("expires") is not None,
        persisted=persisted,
    )
