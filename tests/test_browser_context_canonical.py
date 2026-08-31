from __future__ import annotations

from types import SimpleNamespace

import pytest

from chatgpt_web_adapter.browser_context_canonical import (
    BROWSER_CONTEXT_CANONICAL_READ_PLANE,
    BrowserContextCanonicalClient,
)
from chatgpt_web_adapter.browser_native_provider import BrowserNativeBridgeStatus
from chatgpt_web_adapter.product_runtime import ChatGPTProductRuntime


class _SourceCanonical:
    def get_status(self, conversation):
        raise AssertionError("browser-owned status must not use source canonical HTTP")

    def get_messages(self, conversation, **kwargs):
        raise AssertionError("browser-owned messages must not use source canonical HTTP")

    def attach_conversation(self, conversation):
        raise AssertionError("browser-owned attach must not use source canonical HTTP")


class _Provider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.read_calls: list[tuple[str, float]] = []

    def read_conversation(self, conversation_id: str, *, timeout: float = 30.0):
        self.read_calls.append((conversation_id, timeout))
        return self.payload

    def set_browser_authority_lease(self, lease_id):
        self.lease_id = lease_id

    def complete_canonical_readback(self):
        return True

    def clear_browser_authority_lease(self):
        self.lease_id = None

    def status(self):
        return BrowserNativeBridgeStatus(
            available=True,
            extension_connected=True,
            runtime_tab_id=77,
        )

    def send_text(self, *args, **kwargs):
        raise AssertionError("read tests must not write")


def _payload() -> dict:
    return {
        "conversation_id": "conversation-1",
        "title": "Canonical title",
        "current_node": "assistant-1",
        "mapping": {
            "user-1": {
                "parent": None,
                "children": ["assistant-1", "stale-assistant"],
                "message": {
                    "id": "user-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["question"]},
                    "recipient": "all",
                    "metadata": {},
                },
            },
            "assistant-1": {
                "parent": "user-1",
                "children": [],
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["canonical answer"]},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "end_turn": True,
                    "metadata": {
                        "finish_details": {"type": "stop"},
                        "model_slug": "gpt-browser",
                    },
                },
            },
            "stale-assistant": {
                "parent": "user-1",
                "children": [],
                "message": {
                    "id": "stale-assistant",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["stale branch"]},
                    "recipient": "all",
                    "metadata": {"finish_details": {"type": "stop"}},
                },
            },
        },
    }


def test_browser_context_client_preserves_status_messages_and_attach_semantics() -> None:
    provider = _Provider(_payload())
    client = BrowserContextCanonicalClient(_SourceCanonical(), provider)

    status = client.get_status("conversation-1")
    messages = client.get_messages("conversation-1", limit=None)
    attached = client.attach_conversation("conversation-1")

    assert status.status == "completed"
    assert status.message_id == "assistant-1"
    assert [message.text for message in messages] == ["question", "canonical answer"]
    assert attached.title == "Canonical title"
    assert attached.current_node == "assistant-1"
    assert attached.detected_model == "gpt-browser"
    assert len(provider.read_calls) == 3


def test_browser_owned_runtime_routes_public_canonical_reads_through_provider() -> None:
    provider = _Provider(_payload())
    runtime = ChatGPTProductRuntime(_SourceCanonical(), provider=provider)

    status = runtime.get_status("conversation-1")
    health = runtime.health("conversation-1")

    assert isinstance(runtime.canonical, BrowserContextCanonicalClient)
    assert status.status == "completed"
    assert health.ready is True
    assert health.read_plane == BROWSER_CONTEXT_CANONICAL_READ_PLANE
    assert health.canonical_read_checked is True
    assert provider.read_calls == [
        ("conversation-1", 30.0),
        ("conversation-1", 30.0),
    ]


def test_browser_owned_provider_without_lease_propagation_fails_assembly() -> None:
    source = SimpleNamespace(
        get_status=lambda conversation: SimpleNamespace(status="completed"),
        get_messages=lambda conversation, **kwargs: [],
        attach_conversation=lambda conversation: SimpleNamespace(conversation_id=conversation),
    )
    provider = SimpleNamespace(
        read_conversation=lambda *args, **kwargs: {},
        status=lambda: BrowserNativeBridgeStatus(True, True, runtime_tab_id=77),
        send_text=lambda *args, **kwargs: None,
    )

    with pytest.raises(TypeError, match="set_browser_authority_lease"):
        ChatGPTProductRuntime(source, provider=provider)


def test_browser_owned_provider_without_browser_read_route_fails_assembly() -> None:
    source = SimpleNamespace(
        get_status=lambda conversation: SimpleNamespace(status="completed"),
        get_messages=lambda conversation, **kwargs: [],
        attach_conversation=lambda conversation: SimpleNamespace(conversation_id=conversation),
    )
    provider = SimpleNamespace(
        status=lambda: BrowserNativeBridgeStatus(True, True, runtime_tab_id=77),
        send_text=lambda *args, **kwargs: None,
    )

    with pytest.raises(TypeError, match="read_conversation"):
        ChatGPTProductRuntime(source, provider=provider)
