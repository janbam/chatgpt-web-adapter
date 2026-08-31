from __future__ import annotations

import json
import threading
from pathlib import Path

import chatgpt_web_adapter as adapter
import pytest

from chatgpt_web_adapter.conversation_prepare import prepare_text_turn
from chatgpt_web_adapter.exceptions import RequestError


def test_prepare_debug_trace_never_serializes_conduit_token(tmp_path: Path) -> None:
    secret = "secret-conduit-credential"

    class TraceClient(adapter.ChatGPTWebClient):
        def __init__(self) -> None:
            self.debug_trace_dir = tmp_path
            self.debug_trace_sanitize = False
            self._debug_trace_counter = 0

        @staticmethod
        def _build_headers(extra):
            return {key: value for key, value in extra.items() if value is not None}

        def _json_request(self, method, url, payload, headers):
            # Simulate the generic HTTP tracer receiving the credential-bearing
            # response while prepare suppression is active.
            self._write_debug_trace(
                "http",
                {"response_body": {"status": "ok", "conduit_token": secret}},
            )
            assert self.debug_trace_dir == tmp_path
            return 200, {"status": "ok", "conduit_token": secret}

    client = TraceClient()
    result, _payload = prepare_text_turn(
        client,
        "secret prompt",
        model="gpt-5-6-thinking",
        conversation={"conversation_id": "conv-1", "message_id": "parent-1"},
        partial_query_message_id="user-1",
    )

    assert result.conduit_token == secret
    traces = sorted(tmp_path.glob("*.json"))
    assert len(traces) == 1
    rendered = traces[0].read_text(encoding="utf-8")
    assert secret not in rendered
    assert "secret prompt" not in rendered

    payload = json.loads(rendered)
    assert payload["conduit_token_present"] is True
    assert payload["raw_response_recorded"] is False
    assert payload["response_keys"] == ["conduit_token", "status"]


def test_prepare_trace_suppression_is_context_local_for_concurrent_calls(
    tmp_path: Path,
) -> None:
    barrier = threading.Barrier(2)

    class TraceClient(adapter.ChatGPTWebClient):
        def __init__(self) -> None:
            self.debug_trace_dir = tmp_path
            self.debug_trace_sanitize = False
            self._debug_trace_counter = 0

        @staticmethod
        def _build_headers(extra):
            return {key: value for key, value in extra.items() if value is not None}

        def _json_request(self, method, url, payload, headers):
            secret = payload["partial_query"]["content"]["parts"][0]
            barrier.wait(timeout=5)
            self._write_debug_trace(
                "http",
                {"response_body": {"status": "ok", "conduit_token": secret}},
            )
            assert self.debug_trace_dir == tmp_path
            return 200, {"status": "ok", "conduit_token": secret}

    client = TraceClient()
    errors: list[BaseException] = []

    def run(prompt: str) -> None:
        try:
            prepare_text_turn(
                client,
                prompt,
                model="gpt-5-6-thinking",
                conversation={"conversation_id": "conv-1", "message_id": "parent-1"},
                partial_query_message_id=f"user-{prompt}",
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=("secret-a",)),
        threading.Thread(target=run, args=("secret-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    rendered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(tmp_path.glob("*.json"))
    )
    assert "secret-a" not in rendered
    assert "secret-b" not in rendered
    assert '"raw_response_recorded": false' in rendered


def test_conduit_header_is_redacted_even_when_trace_sanitization_is_disabled() -> None:
    client = object.__new__(adapter.ChatGPTWebClient)
    client.debug_trace_sanitize = False

    headers = client._sanitize_headers_mapping(
        {
            "x-conduit-token": "secret-conduit",
            "x-debug-visible": "visible",
        }
    )

    assert headers["x-conduit-token"] == "<redacted>"
    assert headers["x-debug-visible"] == "visible"


def test_cookie_headers_are_redacted_when_debug_sanitization_is_disabled() -> None:
    client = object.__new__(adapter.ChatGPTWebClient)
    client.debug_trace_sanitize = False

    headers = client._sanitize_header_lines(
        "HTTP/2 403\nset-cookie: cf_clearance=secret\nx-debug-visible: visible"
    )

    assert headers == [
        "HTTP/2 403",
        "set-cookie: <redacted>",
        "x-debug-visible: visible",
    ]


def test_challenge_response_body_is_redacted_even_when_debug_sanitization_is_disabled() -> None:
    secret = "secret-cloudflare-challenge-token"
    client = object.__new__(adapter.ChatGPTWebClient)
    client.debug_trace_sanitize = False

    traced = client._trace_response_body(
        f"<html>{secret}</html>".encode(),
        status=403,
        header_text="HTTP/2 403\ncontent-type: text/html; charset=UTF-8",
    )

    rendered = json.dumps(traced)
    assert secret not in rendered
    assert traced == {
        "kind": "redacted",
        "size": len(f"<html>{secret}</html>".encode()),
        "reason": "sensitive_http_error",
        "content_type": "text/html; charset=utf-8",
    }


def test_canonical_http_error_does_not_embed_response_body() -> None:
    secret = "secret-cloudflare-challenge-token"

    class Client(adapter.ChatGPTWebClient):
        @staticmethod
        def _build_headers(extra):
            return extra

        def _json_request(self, method, url, payload, headers):
            return 403, f"<html>{secret}</html>"

    client = object.__new__(Client)
    with pytest.raises(RequestError) as caught:
        client._get_conversation_payload("conversation-1")

    assert caught.value.status_code == 403
    assert caught.value.body_preview is None
    assert secret not in str(caught.value)
