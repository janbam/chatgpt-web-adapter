from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import CHATGPT_SESSION_COOKIE, CHAT_URL, DEFAULT_AUTH_FILE, load_auth_data
from .auth_store import persist_auth_data
from .browser_cookies import (
    browser_cookie_params,
    flatten_browser_cookies,
    serialize_browser_cookies,
)
from .browser_profile_lock import BrowserProfileLock
from .exceptions import AuthError
from .types import AuthData

SESSION_SCRIPT = """
fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'})
  .then(async response => ({status: response.status, body: await response.json()}))
  .catch(error => ({status: 0, error: String(error)}))
"""


@dataclass(frozen=True)
class BrowserLoginResult:
    auth: AuthData
    auth_file: Path
    profile_dir: Path
    persisted: bool


@dataclass(frozen=True)
class ChromeDebugEndpoint:
    """Consent-gated DevTools endpoint published by a running Chrome profile."""

    port: int
    websocket_path: str = field(repr=False)

    @property
    def websocket_url(self) -> str:
        """Return the loopback WebSocket URL without persisting it."""

        return f"ws://127.0.0.1:{self.port}{self.websocket_path}"


def default_browser_profile_dir() -> Path:
    configured = os.getenv("CHATGPT_WEB_ADAPTER_PROFILE_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "chatgpt-web-adapter" / "browser-profile"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "chatgpt-web-adapter" / "browser-profile"
    state_root = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state_root / "chatgpt-web-adapter" / "browser-profile"


def _read_chrome_debug_endpoint(profile_dir: Path) -> ChromeDebugEndpoint:
    """Read Chrome's ephemeral consent-gated endpoint from a running profile."""

    active_port_file = profile_dir / "DevToolsActivePort"
    try:
        lines = active_port_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise AuthError(
            "The selected Chrome profile is not publishing a DevTools endpoint. "
            "Keep Chrome running and enable Remote debugging at "
            "chrome://inspect/#remote-debugging."
        ) from error
    except OSError as error:
        raise AuthError(
            "Failed to read the selected Chrome profile's DevTools endpoint"
        ) from error

    # Accept only Chrome's browser-level loopback endpoint shape; never follow an
    # arbitrary host or path from mutable profile state.
    if len(lines) < 2:
        raise AuthError("The selected Chrome profile has an incomplete DevTools endpoint")
    try:
        port = int(lines[0])
    except ValueError as error:
        raise AuthError("The selected Chrome profile has an invalid DevTools port") from error
    websocket_path = lines[1].strip()
    if not 1 <= port <= 65535 or not websocket_path.startswith("/devtools/browser/"):
        raise AuthError("The selected Chrome profile has an invalid DevTools endpoint")
    return ChromeDebugEndpoint(port=port, websocket_path=websocket_path)


async def _open_debug_websocket(url: str, *, timeout: float) -> Any:
    """Open Chrome's consent-gated socket using the interactive login timeout."""

    from websockets.asyncio.client import connect
    from zendriver.core.connection import MAX_SIZE, PING_TIMEOUT

    return await connect(
        url,
        open_timeout=timeout,
        ping_timeout=PING_TIMEOUT,
        max_size=MAX_SIZE,
    )


def _chrome_attach_error() -> AuthError:
    """Return a secret-free remediation error for Chrome attachment failures."""

    return AuthError(
        "Could not attach to the selected Chrome profile. Keep Chrome running, "
        "enable Remote debugging at chrome://inspect/#remote-debugging, and approve "
        "Chrome's Allow remote debugging dialog."
    )


async def _attach_running_browser(
    zendriver: Any,
    profile_dir: Path,
    timeout: float,
) -> Any:
    """Attach zendriver to Chrome's consent-gated default-profile endpoint."""

    endpoint = _read_chrome_debug_endpoint(profile_dir)
    config = zendriver.Config(
        user_data_dir=str(profile_dir),
        host="127.0.0.1",
        port=endpoint.port,
    )
    browser = zendriver.Browser(config)
    browser.info = zendriver.ContraDict(
        {"webSocketDebuggerUrl": endpoint.websocket_url},
        silent=True,
    )
    browser.connection = zendriver.Connection(endpoint.websocket_url, _owner=browser)

    # Chrome waits for the user-facing consent dialog; zendriver's default
    # handshake timeout is too short for an interactive approval.
    try:
        browser.connection.websocket = await _open_debug_websocket(
            endpoint.websocket_url,
            timeout=timeout,
        )
    except Exception:
        # Do not retain an exception chain that may contain the ephemeral
        # WebSocket capability in third-party diagnostics.
        raise _chrome_attach_error() from None

    # Recreate zendriver's normal target-discovery setup without launching or
    # assuming ownership of the already-running Chrome process.
    browser.connection.handlers[zendriver.cdp.target.TargetInfoChanged] = [
        browser._handle_target_update
    ]
    browser.connection.handlers[zendriver.cdp.target.TargetCreated] = [
        browser._handle_target_update
    ]
    browser.connection.handlers[zendriver.cdp.target.TargetDestroyed] = [
        browser._handle_target_update
    ]
    browser.connection.handlers[zendriver.cdp.target.TargetCrashed] = [
        browser._handle_target_update
    ]
    try:
        await browser.connection.send(
            zendriver.cdp.target.set_discover_targets(discover=True)
        )
        await browser.update_targets()
    except Exception:
        try:
            await browser.connection.aclose()
        except Exception:
            pass
        raise _chrome_attach_error() from None
    return browser


async def _disconnect_attached_browser(
    browser: Any,
    page: Any,
    zendriver: Any,
) -> None:
    """Close the adapter-owned tab and connection while leaving Chrome running."""

    connection = getattr(browser, "connection", None)
    if connection is None:
        return

    # Remove only the temporary auth tab; the surrounding Chrome session remains
    # owned by the user and must survive adapter shutdown.
    target_id = getattr(page, "target_id", None)
    if target_id is not None:
        try:
            await connection.send(zendriver.cdp.target.close_target(target_id))
        except Exception:
            pass
    try:
        if not bool(getattr(connection, "closed", False)):
            await connection.aclose()
    except Exception:
        pass


async def _open_login_page(browser: Any, url: str, *, attach_existing: bool) -> Any:
    """Open an isolated auth tab only when borrowing an existing browser."""

    if attach_existing:
        return await browser.get(url, new_tab=True)
    return await browser.get(url)


def _import_zendriver() -> Any:
    try:
        import zendriver
    except ImportError as error:
        raise AuthError(
            "Browser login requires the optional browser extra: "
            "pip install 'chatgpt-web-adapter[browser]'"
        ) from error
    return zendriver


def _cookie_dict(browser_cookies: Any) -> dict[str, str]:
    return flatten_browser_cookies(serialize_browser_cookies(browser_cookies))


def _valid_session_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or int(value.get("status") or 0) != 200:
        return None
    body = value.get("body")
    if not isinstance(body, dict):
        return None
    access_token = body.get("accessToken")
    session_token = body.get("sessionToken")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    if not isinstance(session_token, str) or not session_token.strip():
        return None
    return body


def _session_expiry_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


async def _graceful_browser_stop(browser: Any, zendriver: Any) -> None:
    """Give Chromium time to flush a custom profile before zendriver terminates it."""

    connection = getattr(browser, "connection", None)
    process = getattr(browser, "_process", None)
    if connection is None or process is None:
        await browser.stop()
        return
    try:
        await connection.send(zendriver.cdp.browser.close())
        for _ in range(100):
            if getattr(process, "returncode", None) is not None:
                break
            await asyncio.sleep(0.1)
    except Exception:
        pass
    try:
        if not bool(getattr(connection, "closed", False)):
            await connection.aclose()
    except Exception:
        pass
    await browser.stop()


async def _delete_session_cookies(page: Any, zendriver: Any, names: Any) -> None:
    for name in names:
        if name == CHATGPT_SESSION_COOKIE or str(name).startswith(
            f"{CHATGPT_SESSION_COOKIE}."
        ):
            for domain in ("chatgpt.com", ".chatgpt.com"):
                try:
                    await page.send(
                        zendriver.cdp.network.delete_cookies(
                            str(name), domain=domain, path="/"
                        )
                    )
                except Exception:
                    pass


async def _browser_login_async(
    *,
    auth_file: Path,
    profile_dir: Path,
    timeout: float,
    headless: bool,
    browser_executable_path: str | Path | None,
    persist: bool,
    reuse_existing_auth: bool,
    profile_lock_timeout: float,
    attach_existing: bool,
) -> BrowserLoginResult:
    zendriver = _import_zendriver()
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_lock = BrowserProfileLock(profile_dir, timeout=profile_lock_timeout)
    try:
        await asyncio.to_thread(profile_lock.acquire)
    except TimeoutError as error:
        raise AuthError(str(error)) from error
    browser: Any = None
    page: Any = None
    try:
        # Attach only when explicitly requested; ordinary login continues to own
        # and close the browser process it launches.
        if attach_existing:
            browser = await _attach_running_browser(zendriver, profile_dir, timeout)
        else:
            browser = await zendriver.start(
                user_data_dir=str(profile_dir),
                headless=headless,
                browser_executable_path=browser_executable_path,
            )
        seed_cookies: dict[str, str] = {}
        seed_browser_cookies: list[dict[str, Any]] = []
        seed_expires: float | None = None
        if reuse_existing_auth and auth_file.is_file():
            try:
                seed_auth = load_auth_data(
                    auth_file, allow_expired_session_refresh=True
                )
                seed_cookies = seed_auth.cookies
                seed_browser_cookies = seed_auth.browserCookies
                seed_expires = _session_expiry_timestamp(seed_auth.expires)
            except (AuthError, OSError, ValueError):
                seed_cookies = {}
        if not reuse_existing_auth:
            page = await _open_login_page(
                browser, "about:blank", attach_existing=attach_existing
            )
            existing = await page.send(zendriver.cdp.network.get_all_cookies())
            await _delete_session_cookies(
                page,
                zendriver,
                [getattr(cookie, "name", "") for cookie in existing],
            )
            await page.get(CHAT_URL)
        elif seed_cookies:
            page = await _open_login_page(
                browser, "about:blank", attach_existing=attach_existing
            )
            await _delete_session_cookies(page, zendriver, seed_auth.cookies)
            cookie_params = browser_cookie_params(
                zendriver.cdp,
                seed_browser_cookies,
                seed_cookies,
                fallback_expires=seed_expires,
            )
            await page.send(zendriver.cdp.network.set_cookies(cookie_params))
            await page.get(CHAT_URL)
        else:
            page = await _open_login_page(
                browser, CHAT_URL, attach_existing=attach_existing
            )
        deadline = time.monotonic() + timeout
        seeded_session_deadline = time.monotonic() + 8.0 if seed_cookies else None
        cleared_invalid_seed = False
        session: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                session = _valid_session_payload(
                    await page.evaluate(
                        SESSION_SCRIPT,
                        await_promise=True,
                        return_by_value=True,
                    )
                )
            except Exception:
                session = None
            if session is not None:
                break
            if (
                seeded_session_deadline is not None
                and not cleared_invalid_seed
                and time.monotonic() >= seeded_session_deadline
            ):
                await _delete_session_cookies(page, zendriver, seed_auth.cookies)
                cleared_invalid_seed = True
                await page.get(CHAT_URL)
            await asyncio.sleep(1.0)
        if session is None:
            raise AuthError(
                "Browser login timed out. Complete sign-in in the opened ChatGPT window "
                "and keep it open until authorization is saved."
            )

        browser_cookies = await page.send(zendriver.cdp.network.get_all_cookies())
        browser_cookie_records = serialize_browser_cookies(browser_cookies)
        cookies = flatten_browser_cookies(browser_cookie_records)
        if not cookies:
            raise AuthError("Browser login succeeded but no ChatGPT cookies were captured")
        session_expiry = _session_expiry_timestamp(session.get("expires"))
        captured_session_cookies = {
            name: value
            for name, value in cookies.items()
            if name == CHATGPT_SESSION_COOKIE
            or name.startswith(f"{CHATGPT_SESSION_COOKIE}.")
        }
        captured_session_objects = [
            cookie
            for cookie in browser_cookies
            if getattr(cookie, "name", "") == CHATGPT_SESSION_COOKIE
            or getattr(cookie, "name", "").startswith(f"{CHATGPT_SESSION_COOKIE}.")
        ]
        if any(
            getattr(cookie, "name", "").startswith(f"{CHATGPT_SESSION_COOKIE}.")
            for cookie in captured_session_objects
        ):
            await _delete_session_cookies(
                page, zendriver, [CHATGPT_SESSION_COOKIE]
            )
            cookies.pop(CHATGPT_SESSION_COOKIE, None)
            captured_session_cookies.pop(CHATGPT_SESSION_COOKIE, None)
            captured_session_objects = [
                cookie
                for cookie in captured_session_objects
                if getattr(cookie, "name", "") != CHATGPT_SESSION_COOKIE
            ]
            browser_cookie_records = [
                record
                for record in browser_cookie_records
                if record.get("name") != CHATGPT_SESSION_COOKIE
            ]
        user_agent = await page.evaluate(
            "window.navigator.userAgent", return_by_value=True
        )
        if captured_session_cookies and session_expiry is not None:
            await page.get("about:blank")
            await page.send(
                zendriver.cdp.network.set_cookies(
                    [
                        zendriver.cdp.network.CookieParam(
                            name=str(getattr(cookie, "name")),
                            value=str(getattr(cookie, "value")),
                            domain=str(getattr(cookie, "domain")),
                            path=str(getattr(cookie, "path", "/")),
                            secure=bool(getattr(cookie, "secure", True)),
                            http_only=bool(getattr(cookie, "http_only", True)),
                            expires=zendriver.cdp.network.TimeSinceEpoch(
                                session_expiry
                            ),
                        )
                        for cookie in captured_session_objects
                    ]
                )
            )
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.8",
            "referer": CHAT_URL,
        }
        if isinstance(user_agent, str) and user_agent.strip():
            headers["user-agent"] = user_agent.strip()
        auth = AuthData(
            accessToken=session["accessToken"].strip(),
            accessTokenSource="browser-login:accessToken",
            cookies=cookies,
            browserCookies=browser_cookie_records,
            headers=headers,
            expires=session.get("expires"),
        )
        if persist:
            persist_auth_data(
                auth,
                auth_file,
                session_token=session["sessionToken"].strip(),
                session_expires_at=session.get("expires"),
            )
        return BrowserLoginResult(
            auth=auth,
            auth_file=auth_file,
            profile_dir=profile_dir,
            persisted=bool(persist),
        )
    finally:
        if browser is not None:
            if attach_existing:
                await _disconnect_attached_browser(browser, page, zendriver)
            else:
                await _graceful_browser_stop(browser, zendriver)
        await asyncio.to_thread(profile_lock.release)


def browser_login(
    auth_file: str | Path = DEFAULT_AUTH_FILE,
    *,
    profile_dir: str | Path | None = None,
    timeout: float = 300.0,
    headless: bool = False,
    browser_executable_path: str | Path | None = None,
    persist: bool = True,
    reuse_existing_auth: bool = True,
    profile_lock_timeout: float = 30.0,
    attach_existing: bool = False,
) -> BrowserLoginResult:
    """Open or attach to ChatGPT, then persist reusable session auth.

    ``attach_existing`` uses Chrome's consent-gated ``DevToolsActivePort``
    endpoint and leaves the existing browser process running.
    """

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if attach_existing and headless:
        raise ValueError("attach_existing cannot be combined with headless browser mode")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _browser_login_async(
                auth_file=Path(auth_file),
                profile_dir=Path(profile_dir) if profile_dir is not None else default_browser_profile_dir(),
                timeout=float(timeout),
                headless=bool(headless),
                browser_executable_path=browser_executable_path,
                persist=bool(persist),
                reuse_existing_auth=bool(reuse_existing_auth),
                profile_lock_timeout=float(profile_lock_timeout),
                attach_existing=bool(attach_existing),
            )
        )
    raise AuthError("Synchronous browser_login cannot run inside an active asyncio event loop")
