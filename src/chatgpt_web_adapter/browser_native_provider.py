from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .browser_native_protocol import (
    PROTOCOL_VERSION,
    bridge_descriptor_path,
    recv_local_message,
    send_local_message,
)
from .exceptions import RequestError
from .types import ChatConversation, ConversationRef


@dataclass(frozen=True)
class BrowserNativeBridgeStatus:
    available: bool
    extension_connected: bool
    host_pid: int | None = None
    extension_id: str | None = None
    runtime_tab_id: int | None = None


@dataclass(frozen=True)
class BrowserNativeTurnResult:
    conversation_id: str
    turn_exchange_id: str | None
    response_status: int
    response_mime_type: str | None
    final_url: str | None
    tab_id: int | None
    tab_was_active: bool
    elapsed_ms: int | None
    runtime_reloaded: bool = False
    runtime_reload_ms: int | None = None
    runtime_tab_preexisting: bool | None = None
    runtime_tab_created_for_turn: bool | None = None
    tab_active_after: bool | None = None
    tab_activated_during_turn: bool | None = None
    foreground_activation_observed: bool | None = None
    browser_authority_lease_id: str | None = None


@dataclass(frozen=True)
class BrowserNativeRuntimeTabReleaseResult:
    released: bool
    already_absent: bool
    runtime_tab_id: int | None
    browser_authority_lease_id: str


class BrowserNativeCanonicalReadError(RequestError):
    """Safe failure metadata from a browser-context canonical conversation read."""

    def __init__(
        self,
        reason_code: str,
        *,
        conversation_id: str,
        status_code: int | None = None,
        content_type: str | None = None,
        retryable: bool = False,
        route_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.conversation_id = conversation_id
        self.content_type = content_type
        self.retryable = bool(retryable)
        # Keep diagnostics useful without letting bridge-provided metadata expand the error surface.
        self.route_candidates = self._safe_route_candidates(route_candidates)
        details = [f"reason={reason_code}"]
        if status_code is not None:
            details.append(f"status={status_code}")
        if content_type:
            details.append(f"content_type={content_type}")
        super().__init__(
            f"browser canonical read failed: {' '.join(details)}",
            status_code=status_code,
            endpoint="conversation",
            request_stage="browser_native_canonical_read",
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "reason_code": self.reason_code,
                "conversation_id": self.conversation_id,
                "content_type": self.content_type,
                "retryable": self.retryable,
                "route_candidates": self.route_candidates,
            }
        )
        return payload

    @staticmethod
    def _safe_route_candidates(
        candidates: list[dict[str, Any]] | None,
    ) -> list[dict[str, str | int | None]]:
        """Return bounded, non-sensitive route diagnostics from the extension response."""

        if not isinstance(candidates, list):
            return []

        safe_candidates: list[dict[str, str | int | None]] = []
        # Preserve only the explicitly supported fields so error serialization cannot leak new bridge data.
        for candidate in candidates[-40:]:
            if not isinstance(candidate, dict):
                continue
            path = candidate.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or "?" in path
                or "#" in path
            ):
                continue
            initiator_type = candidate.get("initiatorType")
            response_status = candidate.get("responseStatus")
            safe_candidates.append(
                {
                    "path": path[:256],
                    "initiator_type": initiator_type[:32]
                    if isinstance(initiator_type, str)
                    else "",
                    "response_status": response_status
                    if isinstance(response_status, int) and not isinstance(response_status, bool)
                    else None,
                }
            )
        return safe_candidates


class _CanonicalReadChunkCollector:
    """Validate and reassemble one exact canonical response without oversized frames."""

    def __init__(self, *, request_id: str, conversation_id: str) -> None:
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.chunks: dict[int, bytes] = {}
        self.chunk_count: int | None = None
        self.total_bytes: int | None = None
        self.sha256: str | None = None
        self.error_reason: str | None = None

    def add(self, frame: dict[str, Any]) -> None:
        """Accept the next ordered chunk or retain its first integrity failure."""

        if self.error_reason is not None:
            return
        try:
            # Bind every chunk to one request and one immutable transfer manifest.
            if frame.get("request_id") != self.request_id:
                raise ValueError("CANONICAL_READ_CHUNK_REQUEST_MISMATCH")
            index = frame.get("chunkIndex")
            count = frame.get("chunkCount")
            total_bytes = frame.get("totalBytes")
            digest = frame.get("sha256")
            data = frame.get("data")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or not 0 <= index < count
            ):
                raise ValueError("CANONICAL_READ_CHUNK_INDEX_INVALID")
            if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
                raise ValueError("CANONICAL_READ_TOTAL_BYTES_INVALID")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("CANONICAL_READ_DIGEST_INVALID")
            if not isinstance(data, str):
                raise ValueError("CANONICAL_READ_CHUNK_DATA_INVALID")
            manifest = (count, total_bytes, digest)
            current = (self.chunk_count, self.total_bytes, self.sha256)
            if self.chunk_count is not None and current != manifest:
                raise ValueError("CANONICAL_READ_CHUNK_MANIFEST_MISMATCH")
            if index in self.chunks:
                raise ValueError("CANONICAL_READ_CHUNK_DUPLICATE")
            if index != len(self.chunks):
                raise ValueError("CANONICAL_READ_CHUNK_ORDER_INVALID")
            self.chunk_count, self.total_bytes, self.sha256 = manifest
            self.chunks[index] = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as error:
            self.error_reason = str(error) or "CANONICAL_READ_CHUNK_INVALID"

    def finish(self, response: dict[str, Any]) -> bytes:
        """Seal the manifest and return bytes only after all integrity checks pass."""

        if self.error_reason is not None:
            raise ValueError(self.error_reason)

        # Require the final frame to repeat and seal the transfer manifest.
        final_manifest = (
            response.get("chunkCount"),
            response.get("totalBytes"),
            response.get("sha256"),
        )
        expected_manifest = (self.chunk_count, self.total_bytes, self.sha256)
        if expected_manifest != final_manifest:
            raise ValueError("CANONICAL_READ_FINAL_MANIFEST_MISMATCH")
        if self.chunk_count is None or len(self.chunks) != self.chunk_count:
            raise ValueError("CANONICAL_READ_CHUNK_MISSING")
        if set(self.chunks) != set(range(self.chunk_count)):
            raise ValueError("CANONICAL_READ_CHUNK_SEQUENCE_INVALID")

        body = b"".join(self.chunks[index] for index in range(self.chunk_count))
        if len(body) != self.total_bytes:
            raise ValueError("CANONICAL_READ_TOTAL_BYTES_MISMATCH")
        actual_digest = hashlib.sha256(body).hexdigest()
        if self.sha256 is None or not hmac.compare_digest(actual_digest, self.sha256):
            raise ValueError("CANONICAL_READ_DIGEST_MISMATCH")
        return body


class BrowserNativeTurnProvider:
    """Send ordinary text turns through the official ChatGPT page runtime.

    PR8.9 adds optional revision-safe text event frames over the same loopback
    request. Raw conversation SSE still never leaves the extension.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        connect_timeout: float = 3.0,
        turn_timeout: float = 150.0,
    ) -> None:
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        if turn_timeout <= 0:
            raise ValueError("turn_timeout must be positive")
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.connect_timeout = float(connect_timeout)
        self.turn_timeout = float(turn_timeout)
        self._authority_context = threading.local()

    @property
    def descriptor_path(self) -> Path:
        return bridge_descriptor_path(self.state_dir)

    def _load_descriptor(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.descriptor_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RequestError(
                "BROWSER_NATIVE_BRIDGE_UNAVAILABLE: no running Native Messaging bridge",
                request_stage="browser_native_bridge",
            ) from error
        except (OSError, ValueError) as error:
            raise RequestError(
                f"BROWSER_NATIVE_BRIDGE_DESCRIPTOR_INVALID: {error}",
                request_stage="browser_native_bridge",
            ) from error
        if not isinstance(payload, dict):
            raise RequestError(
                "BROWSER_NATIVE_BRIDGE_DESCRIPTOR_INVALID: expected object",
                request_stage="browser_native_bridge",
            )
        host = payload.get("host")
        port = payload.get("port")
        token = payload.get("token")
        protocol = payload.get("protocol")
        if host not in {"127.0.0.1", "localhost"}:
            raise RequestError(
                "BROWSER_NATIVE_BRIDGE_DESCRIPTOR_INVALID: non-loopback host",
                request_stage="browser_native_bridge",
            )
        if not isinstance(port, int) or not (0 < port < 65536):
            raise RequestError(
                "BROWSER_NATIVE_BRIDGE_DESCRIPTOR_INVALID: invalid port",
                request_stage="browser_native_bridge",
            )
        if not isinstance(token, str) or len(token) < 20:
            raise RequestError(
                "BROWSER_NATIVE_BRIDGE_DESCRIPTOR_INVALID: invalid token",
                request_stage="browser_native_bridge",
            )
        if protocol != PROTOCOL_VERSION:
            raise RequestError(
                f"BROWSER_NATIVE_PROTOCOL_MISMATCH: host={protocol} client={PROTOCOL_VERSION}",
                request_stage="browser_native_bridge",
            )
        return payload

    def _rpc(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            request_sent = False
            try:
                descriptor = self._load_descriptor()
                request = {
                    "protocol": PROTOCOL_VERSION,
                    "token": descriptor["token"],
                    **payload,
                }
                remaining = max(0.1, deadline - time.monotonic())
                with socket.create_connection(
                    (descriptor["host"], descriptor["port"]),
                    timeout=min(self.connect_timeout, remaining),
                ) as sock:
                    sock.settimeout(remaining)
                    send_local_message(sock, request)
                    request_sent = True
                    while True:
                        response = recv_local_message(sock)
                        if response.get("protocol") != PROTOCOL_VERSION:
                            raise RequestError(
                                "BROWSER_NATIVE_PROTOCOL_MISMATCH: invalid broker response",
                                request_stage="browser_native_bridge",
                            )
                        response_type = response.get("type")
                        if response_type in {"turn_event", "canonical_read_chunk"}:
                            if response.get("request_id") != payload.get("request_id"):
                                raise RequestError(
                                    "BROWSER_NATIVE_RESPONSE_MISMATCH",
                                    request_stage="browser_native_bridge",
                                )
                            callback_payload = (
                                response.get("event")
                                if response_type == "turn_event"
                                else response
                            )
                            if isinstance(callback_payload, dict) and on_event is not None:
                                try:
                                    on_event(dict(callback_payload))
                                except Exception:
                                    # Intermediate delivery cannot replay a delegated
                                    # write; canonical integrity is checked at finalization.
                                    pass
                            continue
                        return response
            except (RequestError, OSError, EOFError, ValueError) as error:
                last_error = error
                if request_sent:
                    raise RequestError(
                        f"BROWSER_NATIVE_BRIDGE_RESPONSE_LOST_AFTER_DELEGATION: {error}",
                        request_stage="browser_native_bridge",
                    ) from error
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise RequestError(
            f"BROWSER_NATIVE_BRIDGE_UNAVAILABLE: {last_error}",
            request_stage="browser_native_bridge",
        ) from last_error

    def complete_canonical_readback(self) -> bool:
        """Release the leased host lane after Python proves read terminality."""

        lease_id = self._current_browser_authority_lease_id()
        if lease_id is None:
            return True
        deadline = time.monotonic() + max(1.0, self.connect_timeout + 5.5)
        while time.monotonic() < deadline:
            try:
                response = self._rpc(
                    {
                        "type": "canonical_read_complete",
                        "request_id": str(uuid.uuid4()),
                        "browserAuthorityLeaseId": lease_id,
                    },
                    timeout=min(
                        self.connect_timeout,
                        max(0.1, deadline - time.monotonic()),
                    ),
                )
            except RequestError:
                response = None
            if isinstance(response, dict) and response.get("ok") is True:
                return True
            if (
                isinstance(response, dict)
                and response.get("error") != "BROWSER_NATIVE_BRIDGE_BUSY"
            ):
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        # The host's bounded reservation expiry remains the crash-safe fallback.
        return False

    def read_conversation(
        self,
        conversation_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Fetch and verify one exact canonical conversation payload in Chrome."""

        ref = ConversationRef(conversation_id)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        request_id = str(uuid.uuid4())
        lease_id = self._current_browser_authority_lease_id()
        collector = _CanonicalReadChunkCollector(
            request_id=request_id,
            conversation_id=ref.conversation_id,
        )
        response = self._rpc(
            {
                "type": "canonical_read",
                "request_id": request_id,
                "conversationId": ref.conversation_id,
                "timeoutMs": int(timeout * 1000),
                "browserAuthorityLeaseId": lease_id,
            },
            timeout=timeout + 6.0,
            on_event=collector.add,
        )
        if response.get("request_id") != request_id:
            raise BrowserNativeCanonicalReadError(
                "CANONICAL_READ_RESPONSE_MISMATCH",
                conversation_id=ref.conversation_id,
            )
        if not response.get("ok"):
            reason = response.get("reasonCode") or response.get("error")
            reason_code = (
                reason
                if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]+", reason)
                else "CANONICAL_READ_FAILED"
            )
            status = response.get("status")
            status_code = (
                status
                if isinstance(status, int) and not isinstance(status, bool)
                else None
            )
            content_type = response.get("contentType")
            raise BrowserNativeCanonicalReadError(
                reason_code,
                conversation_id=ref.conversation_id,
                status_code=status_code,
                content_type=content_type[:128]
                if isinstance(content_type, str) and content_type
                else None,
                retryable=response.get("retryable") is True,
                route_candidates=response.get("routeCandidates"),
            )
        if response.get("type") != "canonical_read_result":
            raise BrowserNativeCanonicalReadError(
                "CANONICAL_READ_RESULT_TYPE_INVALID",
                conversation_id=ref.conversation_id,
            )

        try:
            raw_body = collector.finish(response)
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            reason = str(error)
            reason_code = (
                reason
                if re.fullmatch(r"[A-Z0-9_]+", reason or "")
                else "CANONICAL_READ_MALFORMED_JSON"
            )
            raise BrowserNativeCanonicalReadError(
                reason_code,
                conversation_id=ref.conversation_id,
                status_code=response.get("status")
                if isinstance(response.get("status"), int)
                else None,
                content_type=response.get("contentType")
                if isinstance(response.get("contentType"), str)
                else None,
            ) from error
        if not isinstance(payload, dict):
            raise BrowserNativeCanonicalReadError(
                "CANONICAL_READ_JSON_OBJECT_REQUIRED",
                conversation_id=ref.conversation_id,
                status_code=response.get("status")
                if isinstance(response.get("status"), int)
                else None,
                content_type=response.get("contentType")
                if isinstance(response.get("contentType"), str)
                else None,
            )
        return payload

    def status(self) -> BrowserNativeBridgeStatus:
        try:
            response = self._rpc(
                {"type": "ping", "request_id": str(uuid.uuid4())},
                timeout=self.connect_timeout,
            )
        except RequestError:
            return BrowserNativeBridgeStatus(False, False)
        return BrowserNativeBridgeStatus(
            available=bool(response.get("ok")),
            extension_connected=bool(response.get("extensionConnected")),
            host_pid=response.get("hostPid") if isinstance(response.get("hostPid"), int) else None,
            extension_id=response.get("extensionId") if isinstance(response.get("extensionId"), str) else None,
            runtime_tab_id=response.get("runtimeTabId") if isinstance(response.get("runtimeTabId"), int) else None,
        )

    @staticmethod
    def _optional_bool(response: dict[str, Any], key: str) -> bool | None:
        value = response.get(key)
        return value if isinstance(value, bool) else None

    def set_browser_authority_lease(self, lease_id: str) -> None:
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise ValueError("browser authority lease_id is required")
        self._authority_context.lease_id = lease_id.strip()

    def clear_browser_authority_lease(self) -> None:
        if hasattr(self._authority_context, "lease_id"):
            del self._authority_context.lease_id

    def _current_browser_authority_lease_id(self) -> str | None:
        value = getattr(self._authority_context, "lease_id", None)
        return value if isinstance(value, str) and value else None

    def _send_text_request(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None,
        timeout: float | None,
        canonical_completed_at_ms: int | None,
        on_text_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> BrowserNativeTurnResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if len(text) > 200_000:
            raise ValueError("text is too large for browser-native turn")
        conversation_id = None
        if conversation is not None:
            conversation_id = ConversationRef.from_any(conversation).conversation_id
        if canonical_completed_at_ms is not None and conversation_id is None:
            raise ValueError("stale UI recovery requires an existing conversation")
        if canonical_completed_at_ms is not None:
            if isinstance(canonical_completed_at_ms, bool) or canonical_completed_at_ms <= 0:
                raise ValueError("canonical_completed_at_ms must be a positive integer")
            canonical_completed_at_ms = int(canonical_completed_at_ms)

        total_timeout = self.turn_timeout if timeout is None else float(timeout)
        if total_timeout <= 0:
            raise ValueError("timeout must be positive")
        request_id = str(uuid.uuid4())
        authority_lease_id = self._current_browser_authority_lease_id()
        response = self._rpc(
            {
                "type": "turn",
                "request_id": request_id,
                "conversationId": conversation_id,
                "text": text,
                "timeoutMs": int(total_timeout * 1000),
                "canonicalCompleted": canonical_completed_at_ms is not None,
                "canonicalCompletedAtMs": canonical_completed_at_ms,
                "browserAuthorityLeaseId": authority_lease_id,
                "streamTextObservations": on_text_event is not None,
            },
            timeout=total_timeout + self.connect_timeout,
            on_event=on_text_event,
        )
        if response.get("request_id") != request_id:
            raise RequestError(
                "BROWSER_NATIVE_RESPONSE_MISMATCH",
                request_stage="browser_native_bridge",
            )
        if not response.get("ok"):
            error = response.get("error") or "BROWSER_NATIVE_TURN_FAILED"
            raise RequestError(str(error), request_stage="browser_native_turn")
        result_conversation_id = response.get("conversationId")
        status = response.get("responseStatus")
        if not isinstance(result_conversation_id, str) or not result_conversation_id.strip():
            raise RequestError(
                "BROWSER_NATIVE_TURN_MISSING_CONVERSATION_ID",
                request_stage="browser_native_turn",
            )
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise RequestError(
                f"BROWSER_NATIVE_TURN_HTTP_STATUS:{status}",
                status_code=status if isinstance(status, int) else None,
                request_stage="browser_native_turn",
            )
        response_lease_id = response.get("browserAuthorityLeaseId")
        if authority_lease_id is not None and response_lease_id != authority_lease_id:
            raise RequestError(
                "BROWSER_NATIVE_AUTHORITY_LEASE_MISMATCH",
                request_stage="browser_native_turn",
            )
        return BrowserNativeTurnResult(
            conversation_id=result_conversation_id.strip(),
            turn_exchange_id=response.get("turnExchangeId")
            if isinstance(response.get("turnExchangeId"), str)
            else None,
            response_status=status,
            response_mime_type=response.get("responseMimeType")
            if isinstance(response.get("responseMimeType"), str)
            else None,
            final_url=response.get("finalUrl") if isinstance(response.get("finalUrl"), str) else None,
            tab_id=response.get("tabId") if isinstance(response.get("tabId"), int) else None,
            tab_was_active=bool(response.get("tabWasActive")),
            elapsed_ms=response.get("elapsedMs") if isinstance(response.get("elapsedMs"), int) else None,
            runtime_reloaded=bool(response.get("runtimeReloaded")),
            runtime_reload_ms=response.get("runtimeReloadMs")
            if isinstance(response.get("runtimeReloadMs"), int)
            else None,
            runtime_tab_preexisting=self._optional_bool(response, "runtimeTabPreexisting"),
            runtime_tab_created_for_turn=self._optional_bool(response, "runtimeTabCreatedForTurn"),
            tab_active_after=self._optional_bool(response, "tabActiveAfter"),
            tab_activated_during_turn=self._optional_bool(response, "tabActivatedDuringTurn"),
            foreground_activation_observed=self._optional_bool(response, "foregroundActivationObserved"),
            browser_authority_lease_id=response_lease_id
            if isinstance(response_lease_id, str)
            else None,
        )

    def send_text(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
    ) -> BrowserNativeTurnResult:
        return self._send_text_request(
            text,
            conversation=conversation,
            timeout=timeout,
            canonical_completed_at_ms=None,
        )

    def send_text_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str | None = None,
        timeout: float | None = None,
        on_text_event: Callable[[dict[str, Any]], None],
    ) -> BrowserNativeTurnResult:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        return self._send_text_request(
            text,
            conversation=conversation,
            timeout=timeout,
            canonical_completed_at_ms=None,
            on_text_event=on_text_event,
        )

    def send_text_with_stale_ui_recovery(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
    ) -> BrowserNativeTurnResult:
        return self._send_text_request(
            text,
            conversation=conversation,
            timeout=timeout,
            canonical_completed_at_ms=canonical_completed_at_ms,
        )

    def send_text_with_stale_ui_recovery_streaming(
        self,
        text: str,
        *,
        conversation: ConversationRef | ChatConversation | dict[str, Any] | str,
        timeout: float | None = None,
        canonical_completed_at_ms: int,
        on_text_event: Callable[[dict[str, Any]], None],
    ) -> BrowserNativeTurnResult:
        if not callable(on_text_event):
            raise TypeError("on_text_event must be callable")
        return self._send_text_request(
            text,
            conversation=conversation,
            timeout=timeout,
            canonical_completed_at_ms=canonical_completed_at_ms,
            on_text_event=on_text_event,
        )

    def release_runtime_tab(
        self,
        *,
        expected_runtime_tab_id: int | None,
        browser_authority_lease_id: str,
        timeout: float = 10.0,
    ) -> BrowserNativeRuntimeTabReleaseResult:
        if expected_runtime_tab_id is not None and (
            isinstance(expected_runtime_tab_id, bool)
            or not isinstance(expected_runtime_tab_id, int)
            or expected_runtime_tab_id <= 0
        ):
            raise ValueError("expected_runtime_tab_id must be a positive int or None")
        if not isinstance(browser_authority_lease_id, str) or not browser_authority_lease_id.strip():
            raise ValueError("browser_authority_lease_id is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        request_id = str(uuid.uuid4())
        lease_id = browser_authority_lease_id.strip()
        response = self._rpc(
            {
                "type": "release_runtime_tab",
                "request_id": request_id,
                "expectedRuntimeTabId": expected_runtime_tab_id,
                "browserAuthorityLeaseId": lease_id,
                "timeoutMs": int(timeout * 1000),
            },
            timeout=timeout + self.connect_timeout,
        )
        if response.get("request_id") != request_id:
            raise RequestError(
                "BROWSER_NATIVE_RESPONSE_MISMATCH",
                request_stage="browser_native_runtime_tab_release",
            )
        if not response.get("ok"):
            error = response.get("error") or "BROWSER_NATIVE_RUNTIME_TAB_RELEASE_FAILED"
            raise RequestError(
                str(error),
                request_stage="browser_native_runtime_tab_release",
            )
        response_lease_id = response.get("browserAuthorityLeaseId")
        if response_lease_id != lease_id:
            raise RequestError(
                "BROWSER_NATIVE_AUTHORITY_LEASE_MISMATCH",
                request_stage="browser_native_runtime_tab_release",
            )
        runtime_tab_id = response.get("runtimeTabId")
        return BrowserNativeRuntimeTabReleaseResult(
            released=bool(response.get("released")),
            already_absent=bool(response.get("alreadyAbsent")),
            runtime_tab_id=runtime_tab_id if isinstance(runtime_tab_id, int) else None,
            browser_authority_lease_id=lease_id,
        )
