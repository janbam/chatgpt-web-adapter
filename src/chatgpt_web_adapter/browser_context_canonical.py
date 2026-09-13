from __future__ import annotations

from typing import Any

from .attach import attach_conversation
from .browser_native_provider import BrowserNativeTurnProvider
from .client import ChatGPTWebClient
from .messages import get_messages
from .status import get_status
from .types import AttachedConversation, ChatConversation, ChatMessage, ConversationRef, ConversationStatus

BROWSER_CONTEXT_CANONICAL_READ_PLANE = "BROWSER_CONTEXT_CANONICAL_HTTP"


class BrowserContextCanonicalClient:
    """Interpret exact Chrome-fetched canonical payloads with existing Python policy."""

    def __init__(
        self,
        source_client: Any,
        provider: BrowserNativeTurnProvider,
        *,
        read_timeout: float = 30.0,
    ) -> None:
        if not callable(getattr(provider, "read_conversation", None)):
            raise TypeError("provider must expose read_conversation()")
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        self.source_client = source_client
        self.provider = provider
        self.read_timeout = float(read_timeout)
        self._browser_native_turn_provider = provider

    def _get_conversation_payload(self, conversation_id: str) -> dict[str, Any]:
        """Return one exact canonical payload fetched inside authenticated Chrome."""

        ref = ConversationRef(conversation_id)
        return self.provider.read_conversation(
            ref.conversation_id,
            timeout=self.read_timeout,
        )

    def get_status(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
    ) -> ConversationStatus:
        """Inspect canonical lifecycle state using the established status interpreter."""

        return get_status(self, conversation)

    def get_messages(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        **kwargs: Any,
    ) -> list[ChatMessage]:
        """Read the canonical current branch using the established message interpreter."""

        return get_messages(self, conversation, **kwargs)

    def attach_conversation(
        self,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
    ) -> AttachedConversation:
        """Attach canonical identity and metadata without duplicating browser-side policy."""

        return attach_conversation(self, conversation)

    @staticmethod
    def _emit_event(callback: Any, event_type: str, **payload: Any) -> None:
        """Emit the event shape expected by the existing canonical helpers."""

        if callback is not None:
            callback({"type": event_type, **payload})

    @staticmethod
    def _current_message_from_conversation(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Reuse the established current-node fallback policy."""

        return ChatGPTWebClient._current_message_from_conversation(payload)

    @staticmethod
    def _latest_assistant_from_conversation(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """Reuse the established latest-assistant fallback policy."""

        return ChatGPTWebClient._latest_assistant_from_conversation(payload)

    @staticmethod
    def _latest_message_any_from_conversation(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """Reuse the established latest-message fallback policy."""

        return ChatGPTWebClient._latest_message_any_from_conversation(payload)
