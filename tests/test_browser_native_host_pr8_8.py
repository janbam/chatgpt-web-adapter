from __future__ import annotations

import time

import chatgpt_web_adapter.browser_native_host as subject


def test_broker_forwards_release_runtime_tab_under_authority_lock(monkeypatch, tmp_path):
    broker = subject.BrowserNativeBroker(state_dir=tmp_path)
    broker.extension_connected = True

    def fake_write(stream, forwarded):
        assert forwarded["type"] == "release_runtime_tab"
        assert forwarded["expectedRuntimeTabId"] == 77
        broker.route_native_message(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "type": "release_runtime_tab_result",
                "request_id": forwarded["request_id"],
                "ok": True,
                "released": True,
                "alreadyAbsent": False,
                "runtimeTabId": 77,
                "browserAuthorityLeaseId": "lease-1",
            }
        )

    monkeypatch.setattr(subject, "write_native_message", fake_write)
    try:
        result = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "release_runtime_tab",
                "request_id": "r1",
                "expectedRuntimeTabId": 77,
                "browserAuthorityLeaseId": "lease-1",
                "timeoutMs": 1000,
            }
        )
    finally:
        broker._server.server_close()

    assert result["ok"] is True
    assert result["released"] is True


def test_broker_reserves_turn_lane_for_matching_canonical_readback(monkeypatch, tmp_path):
    broker = subject.BrowserNativeBroker(state_dir=tmp_path)
    broker.extension_connected = True

    def fake_write(stream, forwarded):
        operation = forwarded["type"]
        if operation == "turn":
            broker.route_native_message(
                {
                    "protocol": subject.PROTOCOL_VERSION,
                    "type": "turn_result",
                    "request_id": forwarded["request_id"],
                    "ok": True,
                }
            )
            return
        assert operation == "canonical_read"
        broker.route_native_message(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "type": "canonical_read_result",
                "request_id": forwarded["request_id"],
                "ok": True,
                "chunkCount": 1,
                "totalBytes": 2,
                "sha256": "0" * 64,
            }
        )

    monkeypatch.setattr(subject, "write_native_message", fake_write)
    try:
        turn = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "turn-1",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
        competing = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "turn-2",
                "browserAuthorityLeaseId": "lease-2",
            }
        )
        readback = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read",
                "request_id": "read-1",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
        second_readback = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read",
                "request_id": "read-2",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
        before_ack = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "turn-3",
                "browserAuthorityLeaseId": "lease-3",
            }
        )
        acknowledged = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read_complete",
                "request_id": "complete-1",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
    finally:
        broker._clear_authority_reservation(release_lane=True)
        broker._server.server_close()

    assert turn["ok"] is True
    assert competing["ok"] is False
    assert competing["error"] == "BROWSER_NATIVE_BRIDGE_BUSY"
    assert readback["ok"] is True
    assert second_readback["ok"] is True
    assert before_ack["error"] == "BROWSER_NATIVE_BRIDGE_BUSY"
    assert acknowledged["ok"] is True
    assert broker.turn_lock.locked() is False


def test_canonical_timeout_re_reserves_lane_until_terminal_ack(monkeypatch, tmp_path):
    broker = subject.BrowserNativeBroker(state_dir=tmp_path)
    broker.extension_connected = True

    class ImmediateQueue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

        put_nowait = put

        def get(self, timeout=None):
            if self.items:
                return self.items.pop(0)
            raise subject.queue.Empty

    def fake_write(stream, forwarded):
        if forwarded["type"] == "turn":
            broker.route_native_message(
                {
                    "protocol": subject.PROTOCOL_VERSION,
                    "type": "turn_result",
                    "request_id": forwarded["request_id"],
                    "ok": True,
                }
            )

    monkeypatch.setattr(subject.queue, "Queue", ImmediateQueue)
    monkeypatch.setattr(subject, "write_native_message", fake_write)
    try:
        turn = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "turn-timeout",
                "browserAuthorityLeaseId": "lease-timeout",
            }
        )
        readback = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read",
                "request_id": "read-timeout",
                "browserAuthorityLeaseId": "lease-timeout",
            }
        )
        competing = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "competing-turn",
                "browserAuthorityLeaseId": "other-lease",
            }
        )
        acknowledgement = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read_complete",
                "request_id": "complete-timeout",
                "browserAuthorityLeaseId": "lease-timeout",
            }
        )
    finally:
        broker._clear_authority_reservation(release_lane=True)
        broker._server.server_close()

    assert turn["ok"] is True
    assert readback["error"] == "BROWSER_NATIVE_EXTENSION_TIMEOUT"
    assert competing["error"] == "BROWSER_NATIVE_BRIDGE_BUSY"
    assert acknowledgement["ok"] is True
    assert broker.turn_lock.locked() is False


def test_abandoned_readback_reservation_expires(monkeypatch, tmp_path):
    broker = subject.BrowserNativeBroker(state_dir=tmp_path)
    broker.extension_connected = True
    monkeypatch.setattr(subject, "AUTHORITY_READBACK_RESERVATION_SECONDS", 0.01)

    def fake_write(stream, forwarded):
        broker.route_native_message(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "type": "turn_result",
                "request_id": forwarded["request_id"],
                "ok": True,
            }
        )

    monkeypatch.setattr(subject, "write_native_message", fake_write)
    try:
        result = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "turn",
                "request_id": "turn-1",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
        deadline = time.time() + 1
        while broker.turn_lock.locked() and time.time() < deadline:
            time.sleep(0.01)
        acknowledgement = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "canonical_read_complete",
                "request_id": "complete-late",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
    finally:
        broker._clear_authority_reservation(release_lane=True)
        broker._server.server_close()

    assert result["ok"] is True
    assert acknowledgement == {
        "protocol": subject.PROTOCOL_VERSION,
        "request_id": "complete-late",
        "ok": False,
        "error": "BROWSER_NATIVE_AUTHORITY_RESERVATION_LOST",
    }
    assert broker.turn_lock.locked() is False


def test_broker_rejects_release_while_turn_authority_lock_busy(tmp_path):
    broker = subject.BrowserNativeBroker(state_dir=tmp_path)
    broker.extension_connected = True
    broker.turn_lock.acquire()
    try:
        result = broker.handle_local_request(
            {
                "protocol": subject.PROTOCOL_VERSION,
                "token": broker.token,
                "type": "release_runtime_tab",
                "request_id": "r1",
                "browserAuthorityLeaseId": "lease-1",
            }
        )
    finally:
        broker.turn_lock.release()
        broker._server.server_close()

    assert result["ok"] is False
    assert result["error"] == "BROWSER_NATIVE_BRIDGE_BUSY"
