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

/** Build the current ChatGPT composer controls needed by the Power selector. */
function powerSurfacePage({ sliderNow = 1, sliderMax = 3, triggerLabel = "Thinking effort", open = true, powerViewActive = true, sliderAriaHidden = "true", triggerHit = true }) {
  let sliderVisible = open;
  let triggerClicks = 0;
  let panel = null;
  let powerControl = null;
  const document = { activeElement: null };
  /** Minimal visible DOM element with controllable product state. */
  class FakeElement {
    /** Create one fixture element. */
    constructor({ text = "", attrs = {}, rect }) {
      this.innerText = text;
      this.attrs = { ...attrs };
      this.rect = rect;
      this.disabled = false;
    }

    /** Return one DOM attribute. */
    getAttribute(name) {
      return this.attrs[name] ?? null;
    }

    /** Report fixture attributes through the DOM presence contract. */
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name);
    }

    /** Return no descendant unless a fixture overrides this method. */
    querySelector() {
      return null;
    }

    /** Return no ancestor unless a fixture overrides this method. */
    closest() {
      return null;
    }

    /** Model DOM containment for trigger hit-testing. */
    contains(element) {
      return element === this;
    }

    /** Return stable fixture geometry. */
    getBoundingClientRect() {
      return this.rect;
    }

    /** Materialize the fixture menu without changing its slider value. */
    click() {
      triggerClicks += 1;
      const opening = this.attrs["aria-expanded"] !== "true";
      this.attrs["aria-expanded"] = opening ? "true" : "false";
      this.attrs["data-state"] = opening ? "open" : "closed";
      sliderVisible = opening;
      if (opening) {
        panel.attrs["data-active"] = "true";
        delete panel.attrs.inert;
        delete powerControl.attrs["aria-disabled"];
      }
    }

    /** Record programmatic focus for keyboard-actuation proof. */
    focus() {
      document.activeElement = this;
    }
  }
  const composer = new FakeElement({ rect: { left: 100, right: 700, top: 100, bottom: 180, width: 600, height: 80 } });
  const trigger = new FakeElement({
    text: triggerLabel,
    attrs: { "aria-haspopup": "menu", "aria-expanded": open ? "true" : "false" },
    rect: { left: 560, right: 690, top: 140, bottom: 176, width: 130, height: 36 },
  });
  const slider = new FakeElement({
    attrs: {
      "aria-valuemin": "0",
      "aria-valuemax": String(sliderMax),
      "aria-valuenow": String(sliderNow),
      ...(sliderAriaHidden === null ? {} : { "aria-hidden": sliderAriaHidden }),
      role: "slider",
    },
    rect: { left: 500, right: 700, top: 200, bottom: 228, width: 200, height: 28 },
  });
  panel = new FakeElement({
    attrs: { "data-active": powerViewActive ? "true" : "false", ...(powerViewActive ? {} : { inert: "" }) },
    rect: { left: 480, right: 720, top: 190, bottom: 240, width: 240, height: 50 },
  });
  powerControl = new FakeElement({
    attrs: { role: "menuitem", "aria-label": "Power", ...(powerViewActive ? {} : { "aria-disabled": "true" }) },
    rect: { left: 480, right: 720, top: 190, bottom: 240, width: 240, height: 50 },
  });
  powerControl.querySelector = (selector) => selector === '[data-model-reasoning-effort-slider] [role="slider"]' ? slider : null;
  powerControl.closest = (selector) => selector === '[data-testid="composer-model-picker-slider-simple-view"]' ? panel : null;
  document.querySelector = (selector) => selector === "#prompt-textarea" ? composer : null;
  document.elementFromPoint = () => triggerHit ? trigger : composer;
  document.querySelectorAll = (selector) => {
    if (selector === 'button,[role="button"]') return [trigger];
    if (selector === '[role="menuitem"][aria-label="Power"]') {
      return sliderVisible ? [powerControl] : [];
    }
    return [];
  };
  return {
    document,
    Element: FakeElement,
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1", pointerEvents: "auto" }),
    powerControl,
    slider,
    trigger,
    triggerClicks: () => triggerClicks,
  };
}

/** Prove the production selector accepts only the current four-position Power contract. */
function testPowerSliderProfileSelection() {
  const worker = evaluateWorker("service_worker_model_profile_selection_pr8_10.js", {
    executeNativeTurn: async () => ({}),
    isConversationWrite: () => false,
    locateAndFocusComposer: async () => "composer",
    sleep: async () => {},
  });
  const expression = vm.runInContext("_pr810PowerSurfaceExpression", worker);
  const supportedMode = vm.runInContext("_pr810Mode", worker);
  const expectedModes = ["INSTANT", "MEDIUM", "HIGH", "PRO"];

  // Every current Power index must have one explicit, deterministic meaning.
  for (const [sliderNow, expectedMode] of expectedModes.entries()) {
    const page = vm.createContext(powerSurfacePage({ sliderNow }));
    const result = vm.runInContext(expression("snapshot"), page);
    assert.equal(result.selectedModeProven, true);
    assert.equal(result.selectedMode, expectedMode);
    assert.equal(result.min, 0);
    assert.equal(result.max, 3);
    assert.equal(result.stepCount, 4);
  }
  assert.equal(supportedMode("PRO"), null);

  // A closed current trigger exports bounded geometry without mutating the page.
  const closedPage = vm.createContext(powerSurfacePage({ sliderNow: 2, open: false }));
  const closed = vm.runInContext(expression("snapshot"), closedPage);
  assert.equal(closed.selectedModeProven, false);
  assert.equal(closed.triggerOpen, false);
  assert.equal(closed.triggerX, 625);
  assert.equal(closed.triggerY, 158);
  assert.equal(closedPage.triggerClicks(), 0);

  // The same current trigger exposes its selected mode after the menu closes.
  const selectedLabelPage = vm.createContext(powerSurfacePage({ triggerLabel: "Medium", open: false }));
  const selectedLabel = vm.runInContext(expression("snapshot"), selectedLabelPage);
  assert.equal(selectedLabel.triggerCount, 1);
  assert.equal(selectedLabel.triggerOpen, false);

  // Unknown direct mode buttons and old three-position sliders are not compatibility inputs.
  const legacyButtonPage = vm.createContext(powerSurfacePage({ triggerLabel: "Fast" }));
  const legacyButton = vm.runInContext(expression("snapshot"), legacyButtonPage);
  assert.equal(legacyButton.proofKind, "power_trigger_missing");
  const legacySliderPage = vm.createContext(powerSurfacePage({ triggerLabel: "High", sliderMax: 2 }));
  const legacySlider = vm.runInContext(expression("snapshot"), legacySliderPage);
  assert.equal(legacySlider.proofKind, "power_slider_missing");
  const interactiveSliderPage = vm.createContext(powerSurfacePage({ sliderAriaHidden: null }));
  const interactiveSlider = vm.runInContext(expression("snapshot"), interactiveSliderPage);
  assert.equal(interactiveSlider.proofKind, "power_slider_missing");
}

/** Execute one complete profile turn against a stateful Power-slider fixture. */
function profileSelectionWorkerScenario({ initialIndex, targetMode, earlyWrite = false, open = true, powerViewActive = true, triggerHit = true, triggerLabel = "Thinking effort" }) {
  const pageContext = vm.createContext(powerSurfacePage({ sliderNow: initialIndex, open, powerViewActive, triggerHit, triggerLabel }));
  const listeners = new Set();
  const keyDowns = [];
  const mouseEvents = [];
  const lifecycle = { foregroundBegins: 0, foregroundRestores: 0 };
  let focusComposer = null;
  let earlyWriteEmitted = false;
  let storedSelection = null;
  const chrome = {
    debugger: {
      onEvent: {
        addListener(listener) {
          listeners.add(listener);
        },
        removeListener(listener) {
          listeners.delete(listener);
        },
      },
      async sendCommand(debuggee, method, params) {
        if (method === "Runtime.evaluate") {
          // Inject the race after the real write guard exists but before proof completes.
          if (earlyWrite && !earlyWriteEmitted) {
            earlyWriteEmitted = true;
            for (const listener of [...listeners]) {
              listener(debuggee, "Network.requestWillBeSent", {
                request: { method: "POST", url: `${CHATGPT_ORIGIN}/backend-api/conversation` },
              });
            }
          }
          const value = vm.runInContext(params.expression, pageContext);
          return { result: { value } };
        }
        if (method === "Input.dispatchKeyEvent") {
          if (params.type !== "rawKeyDown") return {};
          assert.equal(pageContext.document.activeElement, pageContext.powerControl);
          keyDowns.push(params.key);
          const max = Number(pageContext.slider.attrs["aria-valuemax"]);
          const current = Number(pageContext.slider.attrs["aria-valuenow"]);
          if (params.key === "ArrowLeft") {
            pageContext.slider.attrs["aria-valuenow"] = String(Math.max(0, current - 1));
          }
          if (params.key === "ArrowRight") {
            pageContext.slider.attrs["aria-valuenow"] = String(Math.min(max, current + 1));
          }
          return {};
        }
        if (method === "Input.dispatchMouseEvent") {
          mouseEvents.push(params.type);
          if (params.type === "mouseReleased") pageContext.trigger.click();
          return {};
        }
        throw new Error(`unexpected debugger method: ${method}`);
      },
    },
    storage: {
      local: {
        async set(value) {
          storedSelection = value.browserAuthorityLastModelProfileSelectionV1;
        },
      },
    },
  };
  const worker = evaluateWorker("service_worker_model_profile_selection_pr8_10.js", {
    chrome,
    executeNativeTurn: async () => {
      await focusComposer({ tabId: 7 });
      return { ok: true };
    },
    isConversationWrite: (url, method) => method === "POST" && url.includes("/conversation"),
    locateAndFocusComposer: async () => "composer",
    performance,
    sleep: async () => {},
    async _pr88InstantEffortBeginTransientForeground() {
      lifecycle.foregroundBegins += 1;
      return { activated: true, foregroundProven: true };
    },
    async _pr88InstantEffortRestorePriorTab() {
      lifecycle.foregroundRestores += 1;
      return { attempted: true, restored: true };
    },
  });
  focusComposer = vm.runInContext("locateAndFocusComposer", worker);
  const executeTurn = vm.runInContext("executeNativeTurn", worker);
  return {
    execute: () => executeTurn({
      browserAuthorityLeaseId: "lease-1",
      requiredModelMode: targetMode,
      text: "hello",
    }),
    keyDowns,
    lifecycle,
    listeners,
    mouseEvents,
    storedSelection: () => storedSelection,
    triggerClicks: pageContext.triggerClicks,
  };
}

/** Prove selection, early-write rejection, and cleanup through the real turn wrapper. */
async function testPowerSliderSelectionLifecycle() {
  // A stale model view must reset before bounded ArrowLeft/ArrowRight selection.
  const changed = profileSelectionWorkerScenario({ initialIndex: 1, targetMode: "HIGH", powerViewActive: false });
  const changedResult = await changed.execute();
  assert.deepEqual(changed.keyDowns, ["ArrowLeft", "ArrowLeft", "ArrowLeft", "ArrowRight", "ArrowRight"]);
  assert.deepEqual(changed.mouseEvents, ["mousePressed", "mouseReleased", "mousePressed", "mouseReleased"]);
  assert.equal(changed.triggerClicks(), 2);
  assert.equal(changedResult.modelProfileSelection.selectedModeBefore, "MEDIUM");
  assert.equal(changedResult.modelProfileSelection.selectedModeAfter, "HIGH");
  assert.equal(changed.storedSelection().selectionComplete, true);
  assert.equal(changed.lifecycle.foregroundRestores, 1);
  assert.equal(changed.listeners.size, 0);

  // A matching target performs no keys but still proves and cleans up the guarded turn.
  const unchanged = profileSelectionWorkerScenario({ initialIndex: 1, targetMode: "MEDIUM", open: false, triggerLabel: "Medium" });
  const unchangedResult = await unchanged.execute();
  assert.deepEqual(unchanged.keyDowns, []);
  assert.deepEqual(unchanged.mouseEvents, ["mousePressed", "mouseReleased"]);
  assert.equal(unchangedResult.modelProfileSelection.selectionPerformed, false);
  assert.equal(unchanged.lifecycle.foregroundRestores, 1);
  assert.equal(unchanged.listeners.size, 0);

  // A covered trigger must fail before any trusted pointer event leaves the worker.
  const covered = profileSelectionWorkerScenario({ initialIndex: 1, targetMode: "MEDIUM", open: false, triggerHit: false });
  await assert.rejects(covered.execute(), /PR8_10_MODEL_PROFILE_POWER_TRIGGER_NOT_ACTIONABLE/);
  assert.deepEqual(covered.mouseEvents, []);
  assert.equal(covered.lifecycle.foregroundRestores, 1);
  assert.equal(covered.listeners.size, 0);

  // A write observed during initial proof must abort even when no slider mutation is needed.
  const raced = profileSelectionWorkerScenario({ initialIndex: 1, targetMode: "MEDIUM", earlyWrite: true });
  await assert.rejects(raced.execute(), /PR8_10_MODEL_PROFILE_CONVERSATION_WRITE_BEFORE_SELECTION/);
  assert.deepEqual(raced.keyDowns, []);
  assert.equal(raced.lifecycle.foregroundRestores, 1);
  assert.equal(raced.listeners.size, 0);
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
} else if (contract === "power-slider-profile") {
  testPowerSliderProfileSelection();
} else if (contract === "power-slider-lifecycle") {
  await testPowerSliderSelectionLifecycle();
} else {
  throw new Error(`unknown contract: ${contract}`);
}
