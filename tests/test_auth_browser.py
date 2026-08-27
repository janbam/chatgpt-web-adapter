from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chatgpt_web_adapter import auth_browser
from chatgpt_web_adapter.auth import CHATGPT_SESSION_COOKIE
from chatgpt_web_adapter.exceptions import AuthError


class FakePage:
    async def get(self, url):
        assert url in {"https://chatgpt.com/", "about:blank"}
        return self

    async def evaluate(self, expression, **kwargs):
        if "api/auth/session" in expression:
            return {
                "status": 200,
                "body": {
                    "accessToken": "browser-access",
                    "sessionToken": "browser-session-json",
                    "expires": "2030-01-01T00:00:00.000Z",
                },
            }
        return "Browser Test Agent"

    async def send(self, command):
        return [
            SimpleNamespace(
                domain=".chatgpt.com",
                name=CHATGPT_SESSION_COOKIE,
                value="browser-session-cookie",
            ),
            SimpleNamespace(domain=".chatgpt.com", name="oai-did", value="device-1"),
            SimpleNamespace(domain="example.com", name="ignored", value="outside"),
        ]


class FakeBrowser:
    def __init__(self):
        self.page = FakePage()
        self.stopped = False
        self.opened_new_tab = False

    async def get(self, url, *, new_tab=False):
        assert url in {"https://chatgpt.com/", "about:blank"}
        self.opened_new_tab = new_tab
        return self.page

    async def stop(self):
        self.stopped = True


def test_browser_login_captures_and_persists_reusable_session(tmp_path, monkeypatch) -> None:
    browser = FakeBrowser()

    async def start(**kwargs):
        assert kwargs["user_data_dir"] == str(tmp_path / "profile")
        assert kwargs["headless"] is False
        return browser

    fake_zendriver = SimpleNamespace(
        start=start,
        cdp=SimpleNamespace(
            network=SimpleNamespace(
                get_all_cookies=lambda: "get-all-cookies",
                CookieParam=lambda **kwargs: kwargs,
                set_cookies=lambda cookies: ("set-cookies", cookies),
                TimeSinceEpoch=float,
            )
        ),
    )
    monkeypatch.setattr(auth_browser, "_import_zendriver", lambda: fake_zendriver)

    result = auth_browser.browser_login(
        tmp_path / "auth.json",
        profile_dir=tmp_path / "profile",
        timeout=1,
    )

    saved = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert browser.stopped is True
    assert result.auth.accessToken == "browser-access"
    assert result.auth.cookies[CHATGPT_SESSION_COOKIE] == "browser-session-cookie"
    assert result.auth.cookies["oai-did"] == "device-1"
    assert "ignored" not in result.auth.cookies
    assert saved["sessionToken"] == "browser-session-json"
    assert saved["sessionExpiresAt"] == "2030-01-01T00:00:00.000Z"
    assert saved["headers"]["user-agent"] == "Browser Test Agent"
    assert saved["browserCookies"][0]["domain"] == ".chatgpt.com"
    assert "proof_token" not in saved
    assert "turnstile_token" not in saved


def test_browser_login_seeds_existing_session_into_persistent_profile(
    tmp_path, monkeypatch
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "accessToken": "not.a.jwt",
                "sessionToken": "existing-session",
            }
        ),
        encoding="utf-8",
    )
    browser = FakeBrowser()
    started_urls = []
    commands = []

    async def start(**kwargs):
        return browser

    async def browser_get(url):
        started_urls.append(url)
        return browser.page

    async def page_send(command):
        commands.append(command)
        if command == "get-all-cookies":
            return [
                SimpleNamespace(
                    domain=".chatgpt.com",
                    name=CHATGPT_SESSION_COOKIE,
                    value="browser-session-cookie",
                )
            ]
        return None

    browser.get = browser_get
    browser.page.send = page_send
    fake_network = SimpleNamespace(
        CookieParam=lambda **kwargs: kwargs,
        set_cookies=lambda cookies: ("set-cookies", cookies),
        delete_cookies=lambda name, **kwargs: ("delete-cookie", name),
        get_all_cookies=lambda: "get-all-cookies",
        TimeSinceEpoch=float,
    )
    monkeypatch.setattr(
        auth_browser,
        "_import_zendriver",
        lambda: SimpleNamespace(
            start=start,
            cdp=SimpleNamespace(network=fake_network),
        ),
    )

    auth_browser.browser_login(
        auth_file,
        profile_dir=tmp_path / "profile",
        timeout=1,
    )

    assert started_urls == ["about:blank"]
    seed_command = next(command for command in commands if command[0] == "set-cookies")
    assert seed_command[1][0]["value"] == "existing-session"


def test_read_chrome_debug_endpoint_accepts_consent_gated_browser_path(tmp_path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/test-capability\n",
        encoding="utf-8",
    )

    endpoint = auth_browser._read_chrome_debug_endpoint(profile)

    assert endpoint.port == 9222
    assert endpoint.websocket_url == (
        "ws://127.0.0.1:9222/devtools/browser/test-capability"
    )
    assert "test-capability" not in repr(endpoint)


@pytest.mark.parametrize(
    "payload",
    ["", "9222\n", "invalid\n/devtools/browser/id\n", "9222\n/not-browser/id\n"],
)
def test_read_chrome_debug_endpoint_rejects_incomplete_or_unsafe_state(
    tmp_path, payload
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(payload, encoding="utf-8")

    with pytest.raises(AuthError, match="DevTools"):
        auth_browser._read_chrome_debug_endpoint(profile)


def test_attach_running_browser_reuses_chrome_endpoint_without_launching(
    tmp_path, monkeypatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "9333\n/devtools/browser/test-capability\n",
        encoding="utf-8",
    )
    created = {}

    class FakeConnection:
        def __init__(self, url, *, _owner):
            created["url"] = url
            created["owner"] = _owner
            self.handlers = {}
            self.closed = False
            self.commands = []
            self.websocket = None

        async def send(self, command):
            self.commands.append(command)

        async def aclose(self):
            self.closed = True

    class AttachedBrowser:
        def __init__(self, config):
            self.config = config
            self.connection = None
            self.updated = False

        async def _handle_target_update(self, event):
            return None

        async def update_targets(self):
            self.updated = True

    target = SimpleNamespace(
        TargetInfoChanged=object(),
        TargetCreated=object(),
        TargetDestroyed=object(),
        TargetCrashed=object(),
        set_discover_targets=lambda **kwargs: ("discover", kwargs),
    )
    fake_zendriver = SimpleNamespace(
        Config=lambda **kwargs: SimpleNamespace(**kwargs),
        Browser=AttachedBrowser,
        ContraDict=lambda payload, **kwargs: SimpleNamespace(
            webSocketDebuggerUrl=payload["webSocketDebuggerUrl"]
        ),
        Connection=FakeConnection,
        cdp=SimpleNamespace(target=target),
    )

    async def open_websocket(url, *, timeout):
        assert timeout == 45
        return "connected-websocket"

    monkeypatch.setattr(auth_browser, "_open_debug_websocket", open_websocket)

    import asyncio

    browser = asyncio.run(
        auth_browser._attach_running_browser(fake_zendriver, profile, 45)
    )

    assert browser.config.host == "127.0.0.1"
    assert browser.config.port == 9333
    assert browser.updated is True
    assert created["owner"] is browser
    assert created["url"].endswith("/devtools/browser/test-capability")
    assert browser.connection.websocket == "connected-websocket"
    assert browser.connection.commands == [("discover", {"discover": True})]


def test_browser_login_attaches_without_closing_existing_chrome(
    tmp_path, monkeypatch
) -> None:
    browser = FakeBrowser()
    disconnected = {}

    async def attach(zendriver, profile_dir, timeout):
        assert profile_dir == tmp_path / "profile"
        assert timeout == 1
        return browser

    async def disconnect(attached_browser, page, zendriver):
        disconnected["browser"] = attached_browser
        disconnected["page"] = page

    fake_zendriver = SimpleNamespace(
        cdp=SimpleNamespace(
            network=SimpleNamespace(
                get_all_cookies=lambda: "get-all-cookies",
                CookieParam=lambda **kwargs: kwargs,
                set_cookies=lambda cookies: ("set-cookies", cookies),
                TimeSinceEpoch=float,
            )
        ),
    )
    monkeypatch.setattr(auth_browser, "_import_zendriver", lambda: fake_zendriver)
    monkeypatch.setattr(auth_browser, "_attach_running_browser", attach)
    monkeypatch.setattr(auth_browser, "_disconnect_attached_browser", disconnect)

    result = auth_browser.browser_login(
        tmp_path / "auth.json",
        profile_dir=tmp_path / "profile",
        timeout=1,
        attach_existing=True,
    )

    assert result.auth.accessToken == "browser-access"
    assert browser.opened_new_tab is True
    assert browser.stopped is False
    assert disconnected == {"browser": browser, "page": browser.page}


def test_browser_login_rejects_active_event_loop() -> None:
    async def run() -> None:
        with pytest.raises(AuthError, match="active asyncio event loop"):
            auth_browser.browser_login(timeout=1)

    import asyncio

    asyncio.run(run())


def test_browser_login_rejects_headless_existing_profile_attachment() -> None:
    with pytest.raises(ValueError, match="attach_existing"):
        auth_browser.browser_login(headless=True, attach_existing=True)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"status": 401, "body": {}}, {"status": 200, "body": {}}],
)
def test_valid_session_payload_rejects_incomplete_auth(payload) -> None:
    assert auth_browser._valid_session_payload(payload) is None


def test_session_expiry_timestamp_parses_chatgpt_iso_value() -> None:
    assert auth_browser._session_expiry_timestamp("2030-01-01T00:00:00.000Z") == 1893456000
