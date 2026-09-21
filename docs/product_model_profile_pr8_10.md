# PR8.10 — Product Profiles over the Composer Power Slider

Status: CURRENT IMPLEMENTATION UPDATED — live gate pending after the Power-slider migration.

## Goal

Select one explicit product effort for every browser-owned turn before prompt insertion.

Current product contract:

```text
INSTANT -> Power 0
MEDIUM  -> Power 1
HIGH    -> Power 2
PRO     -> Power 3 -> unsupported
```

The public CLI accepts only `INSTANT`, `MEDIUM`, and `HIGH`. Position 3 is never targeted.

## Selection contract

An explicit profile requirement is strict:

```text
profile requested
  -> exact product target resolved
  -> exact composer-local menu trigger resolved as `Thinking effort` or its selected mode
  -> trigger center hit-tested, then opened with one trusted CDP pointer click
  -> an already-open stale model view is closed and reopened with two bounded clicks
  -> active `[role=menuitem][aria-label=Power]` keyboard target resolved
  -> nested aria-hidden `[data-model-reasoning-effort-slider]` evidence slider resolved
  -> current mode derived from proven ARIA range 0..3 and current value
  -> if needed, Power menuitem focused
  -> three ArrowLeft events establish index 0 across the proven 0..3 range
  -> bounded ArrowRight count reaches target index
  -> target ARIA value and derived mode proven before prompt insertion/write
or
  -> fail before conversation write
```

PR8.10 is the sole production selector. It removes the profile requirement before delegating to older worker overlays, so obsolete direct mode-button and three-position-slider selectors cannot run.

A mode already selected requires no slider mutation. The background runtime tab is still foregrounded briefly because the product reveals the current value only inside the menu.

## Safety

- one ordinary browser-owned product write per test turn;
- no automatic retry;
- no direct mode-button compatibility path;
- no three-position-slider compatibility path;
- no model-option coordinate guessing;
- no Advanced navigation;
- no raw request/response export;
- no Fetch interception;
- no response-body extraction;
- conversation write before selection proof is a hard failure;
- unsupported explicit product modes fail before write;
- no silent profile fallback.

## Historical production live evidence

The evidence below validated the retired three-position UI. It does not validate the current Power surface.

The bounded sequence completed successfully:

```text
FAST -> DEEP -> BALANCED
```

Observed evidence:

- `FAST`: `INSTANT -> INSTANT`, `NO_SELECTION_REQUIRED`, selected mode proven before write;
- `DEEP`: `INSTANT -> HIGH`, slider index `2`, transient foreground activation and restoration proven;
- `BALANCED`: `HIGH -> MEDIUM`, slider index `1`, transient foreground activation and restoration proven;
- all three turns reported `conversationWriteBeforeSelection = false`;
- `write_attempts = 3`, `write_completions = 3`, `automatic_write_retry = false`;
- `strict_prewrite_selection_supported = true`;
- `all_three_slider_states_proven = true`;
- `max_profile_mapped = false`.

The first post-write evidence lookup exposed a namespace collision between PR8.10's stored selection lease and the outer PR8.8 transport lease envelope. The repair moved the selection record into a dedicated `modelProfileSelection` namespace; a repeated full live gate then passed.

## Production runtime surface

`ChatGPTProductRuntime.send`, `send_text`, and `send_text_observed` accept a per-turn semantic requirement:

```python
runtime.send(
    "hello",
    conversation=conversation,
    model_profile="DEEP",
)
```

The generic `ProductWriteTransport` protocol remains unchanged. `model_profile=` is forwarded only when the selected transport explicitly advertises PR8.10 support. The browser-owned transport places the requirement inside the same `ProductModelProfileProvider.require_profile()` context used by the successful live gate.

A custom browser-native provider that does not expose the PR8.10 profile context does not inherit the capability claim and rejects an explicit profile before write.

## Capability boundary

The production browser-owned transport now graduates only the capabilities supported by live evidence:

```text
model_selection        = AVAILABLE
reasoning_selection    = AVAILABLE
model_preservation     = UNKNOWN
reasoning_preservation = UNKNOWN
```

Cross-conversation sticky-state scope was not established by the three-state transition gate. Selection is therefore modeled as a `TURN_REQUIREMENT`; preservation remains unclaimed until independent scope evidence exists.

Preservation is not an unresolved PR8.10 shipping blocker: PR8.10 closes with the narrower, evidence-backed per-turn selection contract and leaves independent cross-conversation preservation proof to future work.

## Final validation

- focused PR8.10/PR8.8/PR8.9 integration gate: `48 passed`;
- PR8.8 browser-native provider compatibility boundary: `4 passed`;
- repository-wide regression suite: `1136 passed`;
- no additional live product writes were required after the successful bounded PR8.10 live gate;
- the two full-suite repairs were test-double compatibility updates only and did not change the production write path.
