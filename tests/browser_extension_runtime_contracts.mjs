import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const TESTS_DIR = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_DIR = path.join(
  TESTS_DIR,
  "..",
  "src",
  "chatgpt_web_adapter",
  "browser_native_extension"
);
const CHATGPT_ORIGIN = "https://chatgpt.com";

/** Evaluate one worker overlay against explicit imported-worker mocks. */
function evaluateWorker(fileName, globals) {
  const source = fs.readFileSync(path.join(EXTENSION_DIR, fileName), "utf8");
  const context = vm.createContext(globals);
  vm.runInContext(source, context, { filename: fileName });
  return context;
}

/** Prove provisional identities cannot survive normalization or fallback selection. */
function testCanonicalIdentitySelection() {
  const context = evaluateWorker("service_worker_recovery.js", {
    importScripts() {},
    executeNativeTurn: async () => ({}),
    executeOfficialPageTurn: async () => ({}),
  });
  const normalize = vm.runInContext("_pr811CanonicalConversationId", context);
  const select = vm.runInContext("_pr811SelectCanonicalConversationId", context);

  assert.equal(normalize("WEB:123"), null);
  assert.equal(normalize("  wEb:123  "), null);
  assert.equal(normalize("  durable-123  "), "durable-123");
  assert.equal(normalize("   "), null);
  assert.equal(normalize(null), null);
  assert.equal(select("durable-stream", "durable-route"), "durable-stream");
  assert.equal(select("WEB:stream", "durable-route"), "durable-route");
  assert.equal(select("WEB:stream", "web:route"), null);
}

/** Build a minimal Fetch response accepted by the browser-evaluated closure. */
function response({ status = 200, contentType = "application/json", jsonValue, jsonError, body = "{}" }) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? contentType : null;
      },
    },
    async json() {
      if (jsonError) throw jsonError;
      return jsonValue;
    },
    async arrayBuffer() {
      return new TextEncoder().encode(body).buffer;
    },
  };
}

/** Execute the real canonical worker with browser and CDP boundaries mocked. */
function canonicalWorkerScenario({ sessionResponse, canonicalResponse, sessionError = null }) {
  const calls = [];
  const lifecycle = { attached: 0, detached: 0 };
  const pageContext = vm.createContext({
    AbortController,
    TextEncoder,
    Uint8Array,
    btoa(value) {
      return Buffer.from(value, "binary").toString("base64");
    },
    clearTimeout,
    crypto: webcrypto,
    fetch: async (url, init) => {
      calls.push({ url, init });
      if (calls.length === 1) {
        if (sessionError) throw sessionError;
        return sessionResponse;
      }
      return canonicalResponse;
    },
    location: { origin: CHATGPT_ORIGIN },
    performance: { getEntriesByType: () => [] },
    setTimeout,
    URL,
  });
  const chrome = {
    debugger: {
      async attach() {
        lifecycle.attached += 1;
      },
      async detach() {
        lifecycle.detached += 1;
      },
      async sendCommand(_debuggee, method, params) {
        assert.equal(method, "Runtime.evaluate");
        const value = await vm.runInContext(params.expression, pageContext);
        return { result: { value } };
      },
    },
    tabs: {
      async create() {
        throw new Error("runtime tab must already exist");
      },
      async get() {
        return { id: 7, status: "complete", url: `${CHATGPT_ORIGIN}/` };
      },
    },
  };
  const context = evaluateWorker("service_worker_canonical_read.js", {
    activeRequestId: null,
    BRIDGE_PROTOCOL_VERSION: 1,
    CDP_PROTOCOL_VERSION: "1.3",
    CHATGPT_ORIGIN,
    chrome,
    importScripts() {},
    isChatGPTUrl: (url) => url.startsWith(CHATGPT_ORIGIN),
    onNativeMessage: async () => {},
    safePortPost(port, message) {
      port.messages.push(message);
      return true;
    },
    storedRuntimeTabId: async () => 7,
    storeRuntimeTabId: async () => {},
    waitForTabComplete: async () => ({ id: 7, status: "complete", url: `${CHATGPT_ORIGIN}/` }),
  });
  return { calls, context, lifecycle };
}

/** Prove bearer use, browser-local secrecy, and fail-closed session handling. */
async function testCanonicalBearerBoundary() {
  const secret = "browser-only-secret";
  const body = '{"conversation_id":"conversation-1","mapping":{}}';
  const success = canonicalWorkerScenario({
    sessionResponse: response({ jsonValue: { accessToken: secret } }),
    canonicalResponse: response({ body }),
  });
  const canonicalRead = vm.runInContext("_cwaCanonicalRead", success.context);
  const port = { messages: [] };

  await canonicalRead({ request_id: "request-1", conversationId: "conversation-1", timeoutMs: 5_000 }, port);

  assert.equal(success.calls.length, 2);
  assert.equal(success.calls[0].url, `${CHATGPT_ORIGIN}/api/auth/session`);
  assert.equal(success.calls[0].init.headers.authorization, undefined);
  assert.equal(success.calls[1].url, `${CHATGPT_ORIGIN}/backend-api/conversation/conversation-1`);
  assert.equal(success.calls[1].init.headers.authorization, `Bearer ${secret}`);
  assert.equal(success.lifecycle.attached, 1);
  assert.equal(success.lifecycle.detached, 1);
  assert.equal(port.messages.at(-1).ok, true);
  assert.equal(JSON.stringify(port.messages).includes(secret), false);

  const unusableSessions = [
    {
      expectedReason: "CANONICAL_READ_AUTHENTICATION_REQUIRED",
      sessionResponse: response({ status: 401, jsonValue: {} }),
    },
    {
      expectedReason: "CANONICAL_READ_SESSION_NON_JSON",
      sessionResponse: response({ contentType: "text/html", jsonValue: {} }),
    },
    {
      expectedReason: "CANONICAL_READ_SESSION_INVALID",
      sessionResponse: response({ jsonValue: {} }),
    },
    {
      expectedReason: "CANONICAL_READ_SESSION_INVALID",
      sessionResponse: response({ jsonError: new Error("malformed") }),
    },
  ];
  for (const scenario of unusableSessions) {
    const failure = canonicalWorkerScenario({
      sessionResponse: scenario.sessionResponse,
      canonicalResponse: response({ body }),
    });
    const canonicalFetch = vm.runInContext("_cwaCanonicalFetch", failure.context);
    const result = await canonicalFetch(7, "conversation-1", 5_000);

    assert.equal(failure.calls.length, 1);
    assert.equal(result.ok, false);
    assert.equal(result.reasonCode, scenario.expectedReason);
    assert.equal(result.retryable, false);
  }

  const networkFailure = canonicalWorkerScenario({
    sessionResponse: response({ jsonValue: {} }),
    canonicalResponse: response({ body }),
    sessionError: new Error("offline"),
  });
  const canonicalFetch = vm.runInContext("_cwaCanonicalFetch", networkFailure.context);
  const result = await canonicalFetch(7, "conversation-1", 5_000);
  assert.equal(networkFailure.calls.length, 1);
  assert.equal(result.reasonCode, "CANONICAL_READ_NETWORK_ERROR");
}

const contract = process.argv[2];
if (contract === "canonical-identity") {
  testCanonicalIdentitySelection();
} else if (contract === "canonical-bearer") {
  await testCanonicalBearerBoundary();
} else {
  throw new Error(`unknown contract: ${contract}`);
}
