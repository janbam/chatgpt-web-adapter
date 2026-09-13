from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading

import pytest

from chatgpt_web_adapter.browser_native_protocol import recv_local_message, send_local_message
from chatgpt_web_adapter.browser_native_provider import (
    BrowserNativeCanonicalReadError,
    BrowserNativeTurnProvider,
)


def _round_trip(tmp_path, invoke):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "t" * 32
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request = recv_local_message(connection)
            captured.update(request)
            send_local_message(
                connection,
                {
                    "protocol": 1,
                    "type": "turn_result",
                    "request_id": request["request_id"],
                    "ok": True,
                    "conversationId": "conversation-1",
                    "turnExchangeId": "turn-1",
                    "responseStatus": 200,
                    "responseMimeType": "text/event-stream",
                    "finalUrl": "https://chatgpt.com/c/conversation-1",
                    "tabId": 42,
                    "tabWasActive": False,
                    "elapsedMs": 1234,
                    "runtimeReloaded": True,
                    "runtimeReloadMs": 321,
                },
            )
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    result = invoke(BrowserNativeTurnProvider(state_dir=tmp_path))
    thread.join(timeout=2)
    return token, captured, result


def test_provider_round_trip_uses_loopback_token_and_safe_result(tmp_path) -> None:
    token, captured, result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text("hello", timeout=2),
    )

    assert captured["token"] == token
    assert captured["conversationId"] is None
    assert captured["canonicalCompleted"] is False
    assert captured["canonicalCompletedAtMs"] is None
    assert result.conversation_id == "conversation-1"
    assert result.turn_exchange_id == "turn-1"
    assert result.response_status == 200
    assert result.tab_was_active is False
    assert result.runtime_reloaded is True
    assert result.runtime_reload_ms == 321


def test_provider_serializes_fresh_canonical_completion_recovery_evidence(tmp_path) -> None:
    _, captured, result = _round_trip(
        tmp_path,
        lambda provider: provider.send_text_with_stale_ui_recovery(
            "hello",
            conversation="conversation-1",
            timeout=2,
            canonical_completed_at_ms=123456,
        ),
    )

    assert captured["conversationId"] == "conversation-1"
    assert captured["canonicalCompleted"] is True
    assert captured["canonicalCompletedAtMs"] == 123456
    assert result.runtime_reloaded is True
    assert result.runtime_reload_ms == 321


def _canonical_round_trip(tmp_path, frame_factory, invoke):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    token = "c" * 32
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "host": "127.0.0.1",
                "port": listener.getsockname()[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request = recv_local_message(connection)
            captured.update(request)
            for frame in frame_factory(request["request_id"]):
                send_local_message(connection, frame)
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        return captured, invoke(BrowserNativeTurnProvider(state_dir=tmp_path))
    finally:
        thread.join(timeout=2)


def _canonical_success_frames(request_id: str, raw: bytes) -> list[dict]:
    encoded = base64.b64encode(raw).decode("ascii")
    chunks = [encoded[index : index + 600_000] for index in range(0, len(encoded), 600_000)] or [""]
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {
        "protocol": 1,
        "request_id": request_id,
        "chunkCount": len(chunks),
        "totalBytes": len(raw),
        "sha256": digest,
    }
    return [
        {
            **manifest,
            "type": "canonical_read_chunk",
            "chunkIndex": index,
            "data": chunk,
        }
        for index, chunk in enumerate(chunks)
    ] + [
        {
            **manifest,
            "type": "canonical_read_result",
            "ok": True,
            "status": 200,
            "contentType": "application/json",
        }
    ]


def test_provider_reassembles_exact_multi_frame_canonical_json(tmp_path) -> None:
    payload = {
        "conversation_id": "conversation-1",
        "current_node": "node-1",
        "mapping": {},
        "large_exact_field": "x" * 900_000,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    captured, result = _canonical_round_trip(
        tmp_path,
        lambda request_id: _canonical_success_frames(request_id, raw),
        lambda provider: provider.read_conversation("conversation-1", timeout=2),
    )

    assert captured["type"] == "canonical_read"
    assert captured["conversationId"] == "conversation-1"
    assert result == payload


def test_provider_surfaces_terminal_403_without_challenge_body(tmp_path) -> None:
    def frames(request_id):
        return [
            {
                "protocol": 1,
                "type": "canonical_read_result",
                "request_id": request_id,
                "ok": False,
                "reasonCode": "CANONICAL_READ_ACCESS_CHALLENGED",
                "status": 403,
                "contentType": "text/html",
                "retryable": False,
                "routeCandidates": [
                    {
                        "path": "/backend-api/conversation/conversation-1",
                        "initiatorType": "fetch",
                        "responseStatus": 403,
                    },
                    {"path": "/backend-api/conversation?secret=never-expose"},
                ],
            }
        ]

    with pytest.raises(BrowserNativeCanonicalReadError) as caught:
        _canonical_round_trip(
            tmp_path,
            frames,
            lambda provider: provider.read_conversation("conversation-1", timeout=2),
        )

    error = caught.value
    assert error.reason_code == "CANONICAL_READ_ACCESS_CHALLENGED"
    assert error.status_code == 403
    assert error.content_type == "text/html"
    assert error.retryable is False
    assert error.route_candidates == [
        {
            "path": "/backend-api/conversation/conversation-1",
            "initiator_type": "fetch",
            "response_status": 403,
        }
    ]
    assert error.to_dict()["route_candidates"] == error.route_candidates
    assert "<html" not in str(error).lower()
    assert "challenge" not in (error.to_dict().get("body_preview") or "").lower()


@pytest.mark.parametrize(
    ("mutate", "reason_code"),
    [
        (lambda frames: [frames[1], frames[0], *frames[2:]], "CANONICAL_READ_CHUNK_ORDER_INVALID"),
        (lambda frames: [frames[0], frames[0], *frames[1:]], "CANONICAL_READ_CHUNK_DUPLICATE"),
        (lambda frames: [frames[0], frames[-1]], "CANONICAL_READ_CHUNK_MISSING"),
        (
            lambda frames: [
                {**frame, "sha256": "0" * 64}
                for frame in frames
            ],
            "CANONICAL_READ_DIGEST_MISMATCH",
        ),
    ],
)
def test_provider_rejects_broken_chunk_integrity(tmp_path, mutate, reason_code) -> None:
    raw = json.dumps({"conversation_id": "conversation-1", "blob": "x" * 900_000}).encode()

    with pytest.raises(BrowserNativeCanonicalReadError) as caught:
        _canonical_round_trip(
            tmp_path,
            lambda request_id: mutate(_canonical_success_frames(request_id, raw)),
            lambda provider: provider.read_conversation("conversation-1", timeout=2),
        )

    assert caught.value.reason_code == reason_code


@pytest.mark.parametrize(
    ("reason_code", "status_code", "retryable"),
    [
        ("CANONICAL_READ_NOT_VISIBLE", 404, True),
        ("CANONICAL_READ_ACCESS_CHALLENGED", 403, False),
    ],
)
def test_raw_canonical_reads_never_acknowledge_python_terminality(
    monkeypatch,
    tmp_path,
    reason_code,
    status_code,
    retryable,
) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    provider.set_browser_authority_lease("lease-1")
    operations = []

    def rpc(payload, **kwargs):
        operations.append(payload["type"])
        return {
            "protocol": 1,
            "type": "canonical_read_result",
            "request_id": payload["request_id"],
            "ok": False,
            "reasonCode": reason_code,
            "status": status_code,
            "retryable": retryable,
        }

    monkeypatch.setattr(provider, "_rpc", rpc)

    with pytest.raises(BrowserNativeCanonicalReadError):
        provider.read_conversation("conversation-1")

    assert operations == ["canonical_read"]


def test_successful_200_read_waits_for_runtime_terminality_ack(monkeypatch, tmp_path) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    provider.set_browser_authority_lease("lease-1")
    raw = json.dumps({"conversation_id": "conversation-1", "current_node": None}).encode()
    operations = []

    def rpc(payload, **kwargs):
        operations.append(payload["type"])
        frames = _canonical_success_frames(payload["request_id"], raw)
        for frame in frames[:-1]:
            kwargs["on_event"](frame)
        return frames[-1]

    monkeypatch.setattr(provider, "_rpc", rpc)

    assert provider.read_conversation("conversation-1")["current_node"] is None
    assert operations == ["canonical_read"]


def test_canonical_completion_rejects_lost_reservation(monkeypatch, tmp_path) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    provider.set_browser_authority_lease("lease-1")
    monkeypatch.setattr(
        provider,
        "_rpc",
        lambda payload, **kwargs: {
            "ok": False,
            "error": "BROWSER_NATIVE_AUTHORITY_RESERVATION_LOST",
        },
    )

    assert provider.complete_canonical_readback() is False


def test_canonical_completion_retries_busy_acknowledgement(monkeypatch, tmp_path) -> None:
    provider = BrowserNativeTurnProvider(state_dir=tmp_path)
    provider.set_browser_authority_lease("lease-1")
    responses = [
        {"ok": False, "error": "BROWSER_NATIVE_BRIDGE_BUSY"},
        {"ok": True, "type": "canonical_read_complete_result"},
    ]
    calls = []

    def rpc(payload, **kwargs):
        calls.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(provider, "_rpc", rpc)

    assert provider.complete_canonical_readback() is True
    assert [call["type"] for call in calls] == [
        "canonical_read_complete",
        "canonical_read_complete",
    ]
    assert all(call["browserAuthorityLeaseId"] == "lease-1" for call in calls)
