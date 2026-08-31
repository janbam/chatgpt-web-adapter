from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "src" / "chatgpt_web_adapter" / "browser_native_extension"


def test_canonical_read_overlay_is_the_manifest_entrypoint():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.1.14"
    assert manifest["background"]["service_worker"] == "service_worker_canonical_read.js"


def test_canonical_read_overlay_fetches_same_origin_and_emits_bounded_exact_chunks():
    text = (EXT / "service_worker_canonical_read.js").read_text(encoding="utf-8")
    assert 'importScripts("service_worker_temporary_chat_route_reopen_probe.js")' in text
    assert "${CHATGPT_ORIGIN}/backend-api/conversation/" in text
    assert 'credentials: "include"' in text
    assert "CWA_CANONICAL_CHUNK_BASE64_CHARS = 600_000" in text
    assert 'type: "canonical_read_chunk"' in text
    assert 'type: "canonical_read_result"' in text
    assert '"CANONICAL_READ_ACCESS_CHALLENGED"' in text
    assert 'chrome.debugger.sendCommand(debuggee, "Runtime.evaluate"' in text
    assert "response.arrayBuffer()" in text
    assert "cookies" not in text.lower()
    assert "cf_clearance" not in text
    assert "_puid" not in text


def test_pr88_release_primitive_lives_below_temporary_wrappers():
    text = (EXT / "service_worker_runtime_tab_reconciliation.js").read_text(
        encoding="utf-8"
    )
    assert 'importScripts("service_worker_observability.js")' in text
    assert 'message?.type !== "release_runtime_tab"' in text
    assert "browserAuthorityLeaseId" in text
    assert "expectedRuntimeTabId" in text
    assert "BROWSER_NATIVE_AUTHORITY_LEASE_CHANGED" in text
    assert "BROWSER_NATIVE_RUNTIME_TAB_CHANGED" in text
    assert "chrome.tabs.remove(storedTabId)" in text
    assert "activeRequestId !== null" in text