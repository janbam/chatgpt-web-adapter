from __future__ import annotations

import io
import socket

import pytest

from chatgpt_web_adapter.browser_native_protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    encode_message,
    read_native_message,
    recv_local_message,
    send_local_message,
    write_native_message,
)


def test_native_stdio_frame_round_trip() -> None:
    stream = io.BytesIO()
    write_native_message(stream, {"protocol": PROTOCOL_VERSION, "value": "мир"})
    stream.seek(0)
    assert read_native_message(stream) == {"protocol": PROTOCOL_VERSION, "value": "мир"}


def test_loopback_frame_round_trip() -> None:
    left, right = socket.socketpair()
    try:
        send_local_message(left, {"protocol": PROTOCOL_VERSION, "value": 7})
        assert recv_local_message(right) == {"protocol": PROTOCOL_VERSION, "value": 7}
    finally:
        left.close()
        right.close()


def test_canonical_chunk_envelope_stays_below_frame_limit() -> None:
    frame = {
        "protocol": PROTOCOL_VERSION,
        "type": "canonical_read_chunk",
        "request_id": "request-1",
        "chunkIndex": 0,
        "chunkCount": 2,
        "totalBytes": 900_000,
        "sha256": "0" * 64,
        "data": "A" * 600_000,
    }

    assert len(encode_message(frame)) < MAX_MESSAGE_BYTES


def test_protocol_rejects_an_oversized_single_canonical_frame() -> None:
    with pytest.raises(ValueError, match="exceeds 1 MB"):
        encode_message({"type": "canonical_read_chunk", "data": "A" * MAX_MESSAGE_BYTES})
