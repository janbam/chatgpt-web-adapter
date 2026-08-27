"""Secret-safe HTTPS worker for ChatGPT Web session refresh."""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .auth_refresh import SESSION_URL


class _RejectSessionRedirects(HTTPRedirectHandler):
    """Keep session credentials bound to the fixed ChatGPT endpoint."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        """Reject redirects so urllib exposes their status as an HTTP error."""

        return None


def fetch_session(headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """Fetch and reduce one ChatGPT session response without exposing failures."""

    request = Request(SESSION_URL, method="GET", headers=headers)
    try:
        opener = build_opener(_RejectSessionRedirects())
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            raw_body = response.read()
            set_cookie_headers = response.headers.get_all("set-cookie", [])
    except HTTPError as error:
        try:
            return {"status": int(error.code), "data": None, "set_cookie_headers": []}
        finally:
            error.close()
    except (OSError, URLError, ValueError):
        # Collapse external exception text because header validation can echo credentials.
        return {"status": 0, "data": None, "set_cookie_headers": []}

    # Return only fields the parent refresh contract consumes.
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    data = (
        {key: payload.get(key) for key in ("accessToken", "sessionToken", "expires")}
        if isinstance(payload, dict)
        else None
    )
    return {
        "status": status,
        "data": data,
        "set_cookie_headers": [
            value for value in set_cookie_headers if isinstance(value, str)
        ],
    }


def main() -> int:
    """Read one private request from stdin and emit one reduced JSON response."""

    try:
        request = json.load(sys.stdin)
        headers = request["headers"]
        timeout = float(request["timeout"])
        if not isinstance(headers, dict):
            return 2
        result = fetch_session(headers, timeout)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 2
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
