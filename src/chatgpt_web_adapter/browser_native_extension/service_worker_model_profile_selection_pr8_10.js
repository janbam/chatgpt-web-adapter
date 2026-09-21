// Strict model-profile selection through ChatGPT's composer-local Power slider.
// The product exposes four positions, but CWA intentionally supports only the
// first three: INSTANT (0), MEDIUM (1), and HIGH (2). Position 3 is Pro.

/** Schema version for bounded model-profile selection evidence. */
const PR810_MODEL_PROFILE_SCHEMA_VERSION = 1;
/** Extension storage key for the last completed selection record. */
const PR810_MODEL_PROFILE_STORAGE_KEY = "browserAuthorityLastModelProfileSelectionV1";
/** Supported product modes and their exact Power indices. */
const PR810_MODEL_MODE_INDEX = Object.freeze({INSTANT: 0, MEDIUM: 1, HIGH: 2});
/** Inclusive maximum exposed by the current four-position Power slider. */
const PR810_POWER_SLIDER_MAX = 3;
/** Maximum time allowed to expose and prove the initial Power value. */
const PR810_INITIAL_MODE_ACQUISITION_TIMEOUT_MS = 8000;
/** Poll interval for product-menu materialization and slider settlement. */
const PR810_POWER_SURFACE_POLL_MS = 100;

/** Selector implementation captured before this overlay becomes authoritative. */
const _pr810ModelProfilePriorExecuteNativeTurn = executeNativeTurn;
/** Composer locator captured before profile enforcement wraps it. */
const _pr810ModelProfilePriorLocateAndFocusComposer = locateAndFocusComposer;
/** One active turn-scoped profile requirement, or null outside a turn. */
let _pr810ModelProfileContext = null;

/** Return one supported product mode, or null for an unsupported value. */
function _pr810Mode(value) {
  const mode = typeof value === "string" ? value.trim().toUpperCase() : "";
  return Object.prototype.hasOwnProperty.call(PR810_MODEL_MODE_INDEX, mode) ? mode : null;
}

/** Normalize a Browser Authority lease identifier. */
function _pr810Lease(value) {
  const lease = typeof value === "string" ? value.trim() : "";
  return lease || null;
}

/** Reject characterization requests that could also perform product work. */
function _pr810QueryConflict(message) {
  return (
    message?.text != null ||
    message?.conversationId != null ||
    message?.browserAuthorityLeaseId != null ||
    message?.canonicalCompleted === true
  );
}

/** Preserve bounded initial-mode diagnostics in the public failure code. */
function _pr810InitialModeFailure(before) {
  const proofKind = typeof before?.proofKind === "string" ? before.proofKind : "unknown";
  const composerReady = before?.composerReady === true ? "true" : "false";
  const candidateCount = Number.isInteger(before?.candidateCount) ? before.candidateCount : 0;
  return `PR8_10_MODEL_PROFILE_INITIAL_MODE_NOT_PROVEN:${proofKind}:composer_ready=${composerReady}:candidate_count=${candidateCount}`;
}

/** Build the browser-local Power surface observation or action. */
function _pr810PowerSurfaceExpression(action) {
  return `(() => {
    const ACTION = ${JSON.stringify(action)};
    const normalize = (value) => String(value || '').trim().toLowerCase().replace(/[\\s_\\-]+/g, ' ');
    const number = (value) => {
      if (value === null || value === undefined || value === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const modeForIndex = (index) => {
      if (index === 0) return 'INSTANT';
      if (index === 1) return 'MEDIUM';
      if (index === 2) return 'HIGH';
      if (index === 3) return 'PRO';
      return null;
    };
    const visible = (element) => {
      if (!(element instanceof Element)) return false;
      const rect = element.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return false;
      const style = getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    };
    const composer = [
      '#prompt-textarea',
      '[contenteditable="true"][data-lexical-editor="true"]',
      'textarea[placeholder]'
    ].map((selector) => document.querySelector(selector)).find((element) => element && visible(element));
    if (!composer) {
      return {
        composerReady: false, selectedMode: null, selectedModeProven: false,
        candidateCount: 0, proofKind: 'composer_missing', triggerCount: 0, sliderCount: 0
      };
    }

    // Bind one exact product trigger to this composer before touching any UI.
    const composerRect = composer.getBoundingClientRect();
    const triggerLabels = new Set(['thinking effort', 'instant', 'medium', 'high', 'pro']);
    const triggers = Array.from(document.querySelectorAll('button,[role="button"]')).filter((element) => {
      if (!visible(element)) return false;
      const label = normalize(element.innerText || element.getAttribute('aria-label') || element.getAttribute('title'));
      if (!triggerLabels.has(label) || normalize(element.getAttribute('aria-haspopup')) !== 'menu') return false;
      const rect = element.getBoundingClientRect();
      const dx = Math.max(0, Math.max(composerRect.left - rect.right, rect.left - composerRect.right));
      const dy = Math.max(0, Math.max(composerRect.top - rect.bottom, rect.top - composerRect.bottom));
      return Math.hypot(dx, dy) <= 800;
    });
    if (triggers.length !== 1) {
      return {
        composerReady: true, selectedMode: null, selectedModeProven: false,
        candidateCount: triggers.length,
        proofKind: triggers.length ? 'ambiguous_power_trigger' : 'power_trigger_missing',
        triggerCount: triggers.length, sliderCount: 0
      };
    }
    const trigger = triggers[0];
    const triggerRect = trigger.getBoundingClientRect();
    const triggerOpen = (
      trigger.getAttribute('aria-expanded') === 'true' ||
      normalize(trigger.getAttribute('data-state')) === 'open'
    );
    const triggerDisabled = (
      trigger.disabled === true || trigger.getAttribute('aria-disabled') === 'true' ||
      getComputedStyle(trigger).pointerEvents === 'none'
    );
    const triggerX = triggerRect.left + triggerRect.width / 2;
    const triggerY = triggerRect.top + triggerRect.height / 2;
    const triggerHit = document.elementFromPoint(triggerX, triggerY);
    const triggerHitProven = triggerHit === trigger || (triggerHit instanceof Element && trigger.contains(triggerHit));

    // Require the active product Power menuitem; its aria-hidden thumb is evidence, not the keyboard target.
    const sliders = Array.from(document.querySelectorAll(
      '[role="menuitem"][aria-label="Power"]'
    )).filter(visible).map((control) => {
      const panel = control.closest('[data-testid="composer-model-picker-slider-simple-view"]');
      const element = control.querySelector('[data-model-reasoning-effort-slider] [role="slider"]');
      return {
        control,
        element,
        panel,
        min: number(element?.getAttribute('aria-valuemin')),
        max: number(element?.getAttribute('aria-valuemax')),
        now: number(element?.getAttribute('aria-valuenow')),
        disabled: control.getAttribute('aria-disabled') === 'true'
      };
    }).filter((slider) => (
      slider.element && visible(slider.element) && slider.panel &&
      slider.panel.getAttribute('data-active') === 'true' && !slider.panel.hasAttribute('inert') &&
      slider.element.getAttribute('aria-hidden') === 'true' &&
      slider.min === 0 && slider.max === ${PR810_POWER_SLIDER_MAX} &&
      Number.isInteger(slider.now) && slider.now >= slider.min && slider.now <= slider.max
    ));
    if (sliders.length !== 1) {
      return {
        composerReady: true, selectedMode: null, selectedModeProven: false,
        candidateCount: sliders.length,
        proofKind: sliders.length ? 'power_slider_ambiguous' : 'power_slider_missing',
        triggerCount: 1, sliderCount: sliders.length, triggerOpen,
        triggerDisabled,
        triggerHitProven,
        triggerX,
        triggerY
      };
    }
    const slider = sliders[0];
    let focusProven = document.activeElement === slider.control;
    if (ACTION === 'focus') {
      try { slider.control.focus({preventScroll: true}); }
      catch { try { slider.control.focus(); } catch {} }
      focusProven = document.activeElement === slider.control;
    }
    const selectedMode = modeForIndex(slider.now);
    return {
      composerReady: true,
      selectedMode,
      selectedModeProven: selectedMode !== null,
      candidateCount: 1,
      proofKind: selectedMode !== null ? 'power_slider_value' : 'power_slider_value_unsupported',
      triggerCount: 1,
      sliderCount: 1,
      triggerOpen,
      triggerDisabled,
      triggerHitProven,
      triggerX,
      triggerY,
      min: slider.min,
      max: slider.max,
      now: slider.now,
      stepCount: slider.max - slider.min + 1,
      focusProven,
      disabled: slider.disabled
    };
  })()`;
}

/** Dispatch one trusted pointer click to the uniquely proven Power trigger. */
async function _pr810ClickPowerTrigger(debuggee, surface) {
  if (
    surface?.triggerCount !== 1 || surface?.triggerDisabled === true ||
    surface?.triggerHitProven !== true ||
    !Number.isFinite(surface?.triggerX) || !Number.isFinite(surface?.triggerY)
  ) {
    throw new Error("PR8_10_MODEL_PROFILE_POWER_TRIGGER_NOT_ACTIONABLE");
  }
  const event = {x: surface.triggerX, y: surface.triggerY, button: "left", clickCount: 1};
  await chrome.debugger.sendCommand(debuggee, "Input.dispatchMouseEvent", {
    ...event,
    type: "mousePressed"
  });
  await chrome.debugger.sendCommand(debuggee, "Input.dispatchMouseEvent", {
    ...event,
    type: "mouseReleased"
  });
}

/** Observe or act on the current Power surface without exporting raw DOM. */
async function _pr810PowerSurfaceSnapshot(debuggee, action = "snapshot") {
  const result = await chrome.debugger.sendCommand(debuggee, "Runtime.evaluate", {
    expression: _pr810PowerSurfaceExpression(action),
    returnByValue: true,
    awaitPromise: true
  });
  const value = result?.result?.value;
  return value && typeof value === "object"
    ? value
    : {
        composerReady: false, selectedMode: null, selectedModeProven: false,
        candidateCount: 0, proofKind: "power_surface_probe_failed",
        triggerCount: 0, sliderCount: 0
      };
}

/** Open the Power surface once, then wait for a provable slider value. */
async function _pr810AcquireInitialMode(debuggee, timeoutMs) {
  const startedAt = performance.now();
  let last = null;
  let openAttempted = false;
  let staleSurfaceReset = false;
  while (performance.now() - startedAt < timeoutMs) {
    last = await _pr810PowerSurfaceSnapshot(debuggee, "snapshot");
    if (last?.selectedModeProven === true) {
      return {...last, triggerOpened: openAttempted || last?.triggerOpened === true};
    }
    if (
      !openAttempted && !staleSurfaceReset && last?.composerReady === true &&
      last?.triggerCount === 1 && last?.sliderCount === 0 && last?.triggerOpen === true
    ) {
      // Reset an already-open model list so the product rematerializes its Power view.
      await _pr810ClickPowerTrigger(debuggee, last);
      staleSurfaceReset = true;
      await sleep(PR810_POWER_SURFACE_POLL_MS);
      continue;
    }
    if (
      !openAttempted && last?.composerReady === true && last?.triggerOpen !== true &&
      last?.triggerCount === 1 && last?.sliderCount === 0
    ) {
      // A trusted trigger click exposes evidence only. It cannot change the Power value.
      await _pr810ClickPowerTrigger(debuggee, last);
      openAttempted = true;
      await sleep(PR810_POWER_SURFACE_POLL_MS);
      continue;
    }
    await sleep(PR810_POWER_SURFACE_POLL_MS);
  }
  const final = last || await _pr810PowerSurfaceSnapshot(debuggee, "snapshot");
  return {...final, triggerOpened: openAttempted || final?.triggerOpened === true};
}

/** Dispatch one semantic keyboard key to the focused Power slider. */
async function _pr810DispatchKey(debuggee, key, code, virtualKeyCode) {
  await chrome.debugger.sendCommand(debuggee, "Input.dispatchKeyEvent", {
    type: "rawKeyDown", key, code,
    windowsVirtualKeyCode: virtualKeyCode,
    nativeVirtualKeyCode: virtualKeyCode
  });
  await chrome.debugger.sendCommand(debuggee, "Input.dispatchKeyEvent", {
    type: "keyUp", key, code,
    windowsVirtualKeyCode: virtualKeyCode,
    nativeVirtualKeyCode: virtualKeyCode
  });
}

/** Wait until both the slider index and derived product mode equal the target. */
async function _pr810WaitForTarget(debuggee, targetMode, targetIndex, timeoutMs = 8000) {
  const startedAt = performance.now();
  let surface = null;
  while (performance.now() - startedAt < timeoutMs) {
    surface = await _pr810PowerSurfaceSnapshot(debuggee, "snapshot");
    if (
      surface?.selectedModeProven === true &&
      surface?.selectedMode === targetMode &&
      surface?.min === 0 && surface?.max === PR810_POWER_SLIDER_MAX &&
      surface?.stepCount === PR810_POWER_SLIDER_MAX + 1 && surface?.now === targetIndex
    ) {
      return surface;
    }
    await sleep(PR810_POWER_SURFACE_POLL_MS);
  }
  return surface;
}

/** Guard the complete proof and selection phase against an early conversation write. */
function _pr810InstallWriteBoundary(debuggee, context) {
  const listener = (source, method, params) => {
    if (source?.tabId !== debuggee.tabId || method !== "Network.requestWillBeSent") return;
    const request = params?.request;
    if (isConversationWrite(request?.url || "", request?.method || "")) {
      if (context.selectionComplete !== true) context.conversationWriteBeforeSelection = true;
      try { chrome.debugger.onEvent.removeListener(listener); } catch {}
      context.writeBoundaryListener = null;
    }
  };
  chrome.debugger.onEvent.addListener(listener);
  context.writeBoundaryListener = listener;
}

/** Prove or select the requested Power index before composer input is possible. */
async function _pr810EnsureTargetMode(debuggee, context) {
  if (context.selectionChecked === true) return;
  context.selectionChecked = true;
  const startedAt = performance.now();
  const targetMode = context.requestedModelMode;
  const targetIndex = PR810_MODEL_MODE_INDEX[targetMode];

  // Guard even the menu-opening probe so every product write sees completed proof.
  _pr810InstallWriteBoundary(debuggee, context);
  const foreground = await _pr88InstantEffortBeginTransientForeground(debuggee);
  context.transientForegroundActivated = foreground.activated === true;
  context.transientForegroundProven = foreground.foregroundProven === true;
  try {
    // Foreground the hidden runtime tab before opening its product menu.
    const initialModeStartedAt = performance.now();
    const before = await _pr810AcquireInitialMode(
      debuggee,
      PR810_INITIAL_MODE_ACQUISITION_TIMEOUT_MS
    );
    context.initialModeAcquisitionElapsedMs = Math.max(
      0,
      Math.round(performance.now() - initialModeStartedAt)
    );
    context.initialModeComposerReady = before?.composerReady === true;
    context.selectedModeBefore = before?.selectedMode || null;
    context.selectedModeBeforeProven = before?.selectedModeProven === true;
    context.selectedModeBeforeProofKind = before?.proofKind || "unknown";
    context.selectedModeBeforeCandidateCount = Number.isInteger(before?.candidateCount)
      ? before.candidateCount
      : 0;
    context.powerSliderValueBefore = Number.isFinite(before?.now) ? before.now : null;
    context.powerSurfaceOpened = before?.triggerOpened === true;

    if (before?.selectedModeProven !== true || typeof before?.selectedMode !== "string") {
      throw new Error(_pr810InitialModeFailure(before));
    }
    if (_pr810Mode(before.selectedMode) === null) {
      throw new Error(`PR8_10_MODEL_PROFILE_INITIAL_MODE_UNSUPPORTED:${before.selectedMode}`);
    }

    if (before.selectedMode === targetMode) {
      if (context.conversationWriteBeforeSelection === true) {
        throw new Error("PR8_10_MODEL_PROFILE_CONVERSATION_WRITE_BEFORE_SELECTION");
      }
      context.selectionPerformed = false;
      context.selectionMechanism = "NO_SELECTION_REQUIRED";
      context.selectedModeAfter = targetMode;
      context.selectedModeAfterProven = true;
      context.sliderValueAfter = before.now;
      context.selectionComplete = true;
    } else {
      context.selectionPerformed = true;
      context.selectionMechanism = "POWER_MENUITEM_LEFT_BOUND_PLUS_RIGHT";
      if (
        before?.candidateCount !== 1 || before?.min !== 0 ||
        before?.max !== PR810_POWER_SLIDER_MAX ||
        before?.stepCount !== PR810_POWER_SLIDER_MAX + 1 || before?.disabled === true
      ) {
        throw new Error(`PR8_10_MODEL_PROFILE_POWER_SLIDER_NOT_PROVEN:${before?.proofKind || "range_mismatch"}`);
      }

      const focused = await _pr810PowerSurfaceSnapshot(debuggee, "focus");
      if (
        focused?.focusProven !== true || focused?.min !== 0 ||
        focused?.max !== PR810_POWER_SLIDER_MAX ||
        focused?.stepCount !== PR810_POWER_SLIDER_MAX + 1
      ) {
        throw new Error("PR8_10_MODEL_PROFILE_POWER_SLIDER_FOCUS_NOT_PROVEN");
      }

      for (let index = 0; index < PR810_POWER_SLIDER_MAX; index += 1) {
        await _pr810DispatchKey(debuggee, "ArrowLeft", "ArrowLeft", 37);
      }
      for (let index = 0; index < targetIndex; index += 1) {
        await _pr810DispatchKey(debuggee, "ArrowRight", "ArrowRight", 39);
      }

      // Require exact product state after actuation, never merely the dispatched key count.
      const after = await _pr810WaitForTarget(debuggee, targetMode, targetIndex);
      context.sliderValueAfter = Number.isFinite(after?.now) ? after.now : null;
      context.selectedModeAfter = after?.selectedMode || null;
      context.selectedModeAfterProven = after?.selectedModeProven === true;
      if (context.conversationWriteBeforeSelection === true) {
        throw new Error("PR8_10_MODEL_PROFILE_CONVERSATION_WRITE_BEFORE_SELECTION");
      }
      if (after?.selectedModeProven !== true || after?.selectedMode !== targetMode) {
        throw new Error(`PR8_10_MODEL_PROFILE_DID_NOT_SETTLE:${targetMode}`);
      }
      if (after?.now !== targetIndex) {
        throw new Error(`PR8_10_MODEL_PROFILE_SLIDER_TARGET_NOT_REACHED:${targetIndex}`);
      }
      context.selectionComplete = true;
    }
  } finally {
    const restored = await _pr88InstantEffortRestorePriorTab(foreground);
    context.foregroundRestoreAttempted = restored.attempted === true;
    context.foregroundRestoreProven = restored.restored === true;
  }
  context.selectionElapsedMs = Math.max(0, Math.round(performance.now() - startedAt));
}

/** Enforce the profile requirement before delegating to composer focus and input. */
locateAndFocusComposer = async function _locateAndFocusComposerWithModelProfile(debuggee) {
  if (_pr810ModelProfileContext !== null) {
    await _pr810EnsureTargetMode(debuggee, _pr810ModelProfileContext);
  }
  return _pr810ModelProfilePriorLocateAndFocusComposer(debuggee);
};

/** Build the bounded selection evidence stored for one Browser Authority lease. */
function _pr810Record(context) {
  return {
    schemaVersion: PR810_MODEL_PROFILE_SCHEMA_VERSION,
    browserAuthorityLeaseId: context.leaseId,
    requestedModelMode: context.requestedModelMode,
    requestedSliderIndex: PR810_MODEL_MODE_INDEX[context.requestedModelMode],
    initialModeAcquisitionTimeoutMs: PR810_INITIAL_MODE_ACQUISITION_TIMEOUT_MS,
    initialModeAcquisitionElapsedMs: Number.isFinite(context.initialModeAcquisitionElapsedMs)
      ? context.initialModeAcquisitionElapsedMs
      : null,
    initialModeComposerReady: context.initialModeComposerReady === true,
    selectedModeBefore: context.selectedModeBefore,
    selectedModeBeforeProven: context.selectedModeBeforeProven === true,
    selectedModeBeforeProofKind: context.selectedModeBeforeProofKind || null,
    selectedModeBeforeCandidateCount: Number.isInteger(context.selectedModeBeforeCandidateCount)
      ? context.selectedModeBeforeCandidateCount
      : 0,
    powerSliderValueBefore: Number.isFinite(context.powerSliderValueBefore)
      ? context.powerSliderValueBefore
      : null,
    powerSurfaceOpened: context.powerSurfaceOpened === true,
    selectionPerformed: context.selectionPerformed === true,
    selectionMechanism: context.selectionMechanism || null,
    selectedModeAfter: context.selectedModeAfter,
    selectedModeAfterProven: context.selectedModeAfterProven === true,
    sliderValueAfter: Number.isFinite(context.sliderValueAfter) ? context.sliderValueAfter : null,
    selectionComplete: context.selectionComplete === true,
    conversationWriteBeforeSelection: context.conversationWriteBeforeSelection === true,
    transientForegroundActivated: context.transientForegroundActivated === true,
    transientForegroundProven: context.transientForegroundProven === true,
    foregroundRestoreAttempted: context.foregroundRestoreAttempted === true,
    foregroundRestoreProven: context.foregroundRestoreProven !== false,
    selectionElapsedMs: Number.isFinite(context.selectionElapsedMs) ? context.selectionElapsedMs : null
  };
}

/** Read the last bounded profile-selection record from extension storage. */
async function _pr810StoredRecord() {
  try {
    const stored = await chrome.storage.local.get(PR810_MODEL_PROFILE_STORAGE_KEY);
    const value = stored?.[PR810_MODEL_PROFILE_STORAGE_KEY];
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

/** Add strict Power-profile enforcement and evidence to one leased product turn. */
executeNativeTurn = async function _executeNativeTurnWithModelProfile(message) {
  if (message?.characterizeProductModelProfileSupport === true) {
    if (_pr810QueryConflict(message)) throw new Error("PR8_10_MODEL_PROFILE_SUPPORT_FLAG_CONFLICT");
    return {
      modelProfileSelectionSupported: true,
      modelProfileSelectionSchemaVersion: PR810_MODEL_PROFILE_SCHEMA_VERSION,
      supportedProductModes: ["INSTANT", "MEDIUM", "HIGH"],
      sliderIndices: {...PR810_MODEL_MODE_INDEX},
      strictPrewriteVerification: true,
      boundedInitialModeAcquisition: true,
      initialModeAcquisitionTimeoutMs: PR810_INITIAL_MODE_ACQUISITION_TIMEOUT_MS,
      powerSliderRange: {min: 0, max: PR810_POWER_SLIDER_MAX},
      unsupportedPowerIndices: [3],
      maxProfileMapped: false
    };
  }

  if (message?.characterizeProductModelProfileSelectionRecord === true) {
    if (_pr810QueryConflict(message)) throw new Error("PR8_10_MODEL_PROFILE_RECORD_FLAG_CONFLICT");
    const expectedLease = _pr810Lease(message?.expectedBrowserAuthorityLeaseId);
    const record = await _pr810StoredRecord();
    if (!record) throw new Error("PR8_10_MODEL_PROFILE_RECORD_UNAVAILABLE");
    if (expectedLease && record.browserAuthorityLeaseId !== expectedLease) {
      throw new Error("PR8_10_MODEL_PROFILE_LEASE_MISMATCH");
    }
    return {
      modelProfileSelectionSupported: true,
      modelProfileSelection: record
    };
  }

  const requestedRaw = message?.requiredModelMode;
  const requestedMode = _pr810Mode(requestedRaw);
  const leaseId = _pr810Lease(message?.browserAuthorityLeaseId);
  const ordinaryWrite = typeof message?.text === "string" && Boolean(message.text.trim()) && leaseId !== null;
  if (!ordinaryWrite || requestedRaw == null) return _pr810ModelProfilePriorExecuteNativeTurn(message);
  if (requestedMode === null) throw new Error(`PR8_10_MODEL_MODE_UNSUPPORTED:${String(requestedRaw)}`);
  if (_pr810ModelProfileContext !== null) throw new Error("PR8_10_MODEL_PROFILE_CONTEXT_ALREADY_ACTIVE");

  const context = {
    leaseId,
    requestedModelMode: requestedMode,
    selectionChecked: false,
    selectionComplete: false,
    conversationWriteBeforeSelection: false,
    writeBoundaryListener: null
  };
  _pr810ModelProfileContext = context;
  try {
    // PR8.10 is the sole production selector. Do not invoke obsolete PR8.8 selector overlays.
    const priorMessage = {...message};
    delete priorMessage.requiredModelMode;
    delete priorMessage.requireNoReasoningRoute;
    const result = await _pr810ModelProfilePriorExecuteNativeTurn(priorMessage);
    if (context.selectionComplete !== true || context.selectedModeAfterProven !== true || context.selectedModeAfter !== requestedMode) {
      throw new Error("PR8_10_MODEL_PROFILE_PREWRITE_PROOF_MISSING");
    }
    const record = _pr810Record(context);
    try {
      await chrome.storage.local.set({[PR810_MODEL_PROFILE_STORAGE_KEY]: record});
    } catch {}
    return {...result, modelProfileSelection: record};
  } finally {
    if (context.writeBoundaryListener) {
      try { chrome.debugger.onEvent.removeListener(context.writeBoundaryListener); } catch {}
    }
    _pr810ModelProfileContext = null;
  }
};
