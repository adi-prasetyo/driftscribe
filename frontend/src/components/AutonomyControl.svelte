<script lang="ts">
  // AutonomyControl — three-segment dial (Observe / Propose / Propose + Apply).
  //
  // Mirrors PauseControl: same `call` prop contract, same monotonic seq stale-
  // response guard, same single-flight busy guard, same structural GET validation
  // (malformed 200 → 'unknown'), no optimistic updates (apply POST response).
  //
  // Visual: ds-* token palette only; Observe segment is NOT styled as a warning —
  // modes are operator choices, not alarms.

  import { onMount, tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import { AUTONOMY_MODES, MODE_LABELS, MODE_BLURBS, parseAutonomyDoc } from '../lib/autonomy';
  import type { AutonomyDoc, AutonomyMode } from '../lib/autonomy';
  import { motionMs } from '../lib/motion';
  import Icon from './Icon.svelte';

  let {
    call,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
  } = $props();

  // --------------------------------------------------------------------------
  // State
  // --------------------------------------------------------------------------

  let stateKind = $state<'loading' | 'loaded' | 'unknown'>('loading');
  let currentMode = $state<AutonomyMode>('propose_apply');
  let currentReason = $state<string | null>(null);
  let currentActor = $state<string | null>(null);
  let currentUpdatedAt = $state<string | null>(null);
  let currentReadError = $state(false);

  // Confirm-row state: pendingMode is the mode the user clicked but hasn't confirmed.
  let confirming = $state(false);
  let pendingMode = $state<AutonomyMode | null>(null);
  let saving = $state(false);
  let postError = $state(false);
  let reasonInput = $state('');

  // Monotonic seq guard — mirrors PauseControl exactly.
  let seq = 0;

  // --------------------------------------------------------------------------
  // Sliding pill state
  // --------------------------------------------------------------------------

  // Per-segment element refs (indexed by AUTONOMY_MODES order)
  let segmentEls = $state<(HTMLElement | null)[]>([null, null, null]);
  let containerEl = $state<HTMLElement | null>(null);

  // Pill geometry (in px)
  let pillLeft = $state(0);
  let pillWidth = $state(0);
  // Whether we have a valid nonzero measurement — gates the --measured class
  let pillMeasured = $state(false);
  // Whether the first measurement has been placed (gates the CSS transition)
  let pillReady = $state(false);
  // Pending first-measurement rAF — component-scoped so the $effect cleanup
  // can actually cancel it (measurePill runs outside the effect closure).
  let pillRafId: number | undefined;

  // --------------------------------------------------------------------------
  // Fetch
  // --------------------------------------------------------------------------

  async function fetchAutonomy(): Promise<void> {
    const my = ++seq;
    stateKind = 'loading';
    postError = false;
    let resp: Response;
    try {
      resp = await call('/autonomy');
    } catch {
      if (my !== seq) return;
      stateKind = 'unknown';
      return;
    }
    if (my !== seq) return;
    if (!resp.ok) {
      stateKind = 'unknown';
      return;
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      if (my !== seq) return;
      stateKind = 'unknown';
      return;
    }
    if (my !== seq) return;
    const doc = parseAutonomyDoc(body);
    if (!doc) {
      stateKind = 'unknown';
      return;
    }
    applyDoc(doc);
  }

  function applyDoc(doc: AutonomyDoc): void {
    currentMode = doc.mode;
    currentReason = doc.reason;
    currentActor = doc.actor;
    currentUpdatedAt = doc.updated_at;
    currentReadError = doc.read_error;
    stateKind = 'loaded';
  }

  onMount(() => {
    void fetchAutonomy();
  });

  // --------------------------------------------------------------------------
  // Sliding pill: measurement effect
  // --------------------------------------------------------------------------
  // Must read BOTH stateKind AND currentMode as reactive deps:
  //   - currentMode defaults to 'propose_apply'; if the GET returns that same
  //     value, the state never changes and an effect keyed only on currentMode
  //     fires once against the loading branch (no segment DOM) and never again.
  //   - By tracking stateKind we re-fire when the loaded branch mounts its DOM.
  // The callback is synchronous: reads deps, kicks tick().then(...) internally.

  $effect(() => {
    // Read both reactive deps unconditionally so Svelte tracks them.
    const kind = stateKind;
    const mode = currentMode;

    let cancelled = false;
    let ro: { disconnect(): void } | undefined;

    if (kind === 'loaded') {
      tick().then(() => {
        if (cancelled) return;
        measurePill();

        // Re-measure on container resize — guarded: jsdom has no ResizeObserver.
        if (typeof ResizeObserver !== 'undefined' && containerEl) {
          const observer = new ResizeObserver(() => {
            if (!cancelled) measurePill();
          });
          observer.observe(containerEl);
          ro = observer;
        }
      });
    }

    // Synchronous cleanup.
    return () => {
      cancelled = true;
      if (pillRafId !== undefined) {
        cancelAnimationFrame(pillRafId);
        pillRafId = undefined;
      }
      ro?.disconnect();
      ro = undefined;
    };
  });

  function measurePill(): void {
    const modeIndex = AUTONOMY_MODES.indexOf(currentMode);
    const el = modeIndex >= 0 ? segmentEls[modeIndex] : null;
    if (!el) return;

    const left = el.offsetLeft;
    const width = el.offsetWidth;
    if (width > 0) {
      pillLeft = left;
      pillWidth = width;
      pillMeasured = true;
      // Enable transition only after the first valid measurement (no first-paint slide).
      if (!pillReady && pillRafId === undefined) {
        // Use rAF so the initial position is committed before transition kicks
        // in. The === undefined guard prevents a resize-triggered re-measure
        // from overwriting (and thereby leaking) an already-pending rAF.
        pillRafId = requestAnimationFrame(() => {
          pillRafId = undefined;
          pillReady = true;
        });
      }
    }
  }

  // --------------------------------------------------------------------------
  // Segment click → arm confirm row
  // --------------------------------------------------------------------------

  function onSegmentClick(mode: AutonomyMode): void {
    if (saving) return;
    if (mode === currentMode) return; // clicking the active segment does nothing
    confirming = true;
    pendingMode = mode;
    postError = false;
    reasonInput = '';
  }

  function onCancel(): void {
    confirming = false;
    pendingMode = null;
    postError = false;
    reasonInput = '';
  }

  // --------------------------------------------------------------------------
  // Confirm POST
  // --------------------------------------------------------------------------

  async function onConfirm(): Promise<void> {
    if (saving) return;
    if (!pendingMode) return;
    const my = ++seq;
    const targetMode = pendingMode;
    saving = true;
    postError = false;

    const trimmed = reasonInput.trim();
    const requestBody: Record<string, unknown> = { mode: targetMode };
    if (trimmed.length > 0) {
      requestBody.reason = trimmed;
    }

    let resp: Response;
    try {
      resp = await call('/autonomy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      });
    } catch {
      saving = false;
      if (my !== seq) return;
      postError = true;
      confirming = false;
      pendingMode = null;
      return;
    }

    if (my !== seq) {
      saving = false;
      return;
    }
    if (!resp.ok) {
      saving = false;
      postError = true;
      confirming = false;
      pendingMode = null;
      return;
    }

    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      saving = false;
      if (my !== seq) return;
      postError = true;
      confirming = false;
      pendingMode = null;
      return;
    }

    if (my !== seq) {
      saving = false;
      return;
    }
    const doc = parseAutonomyDoc(body);
    if (!doc) {
      saving = false;
      postError = true;
      confirming = false;
      pendingMode = null;
      return;
    }

    // Apply from response — no optimistic update
    applyDoc(doc);
    saving = false;
    confirming = false;
    pendingMode = null;
    reasonInput = '';
  }

  // --------------------------------------------------------------------------
  // Time formatting (mirrors PauseControl / DecisionsRail)
  // --------------------------------------------------------------------------

  function fmtUpdatedAt(iso: string | null): string {
    if (!iso) return '';
    const parsed = Date.parse(iso);
    if (Number.isNaN(parsed)) return iso;
    try {
      return new Intl.DateTimeFormat(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(parsed);
    } catch {
      return iso;
    }
  }

  const confirmLabel = $derived(saving ? 'Saving…' : 'Confirm');

  // Mode → icon mapping
  const MODE_ICONS = {
    observe: 'eye',
    propose: 'git-pull-request',
    propose_apply: 'zap',
  } as const;
</script>

<div
  class="autonomy-control"
  data-testid="autonomy-control"
  role="region"
  aria-label="DriftScribe autonomy dial"
>
  {#if stateKind === 'loading'}
    <div class="autonomy-card autonomy-card--loaded">
      <span class="autonomy-state">Loading autonomy state…</span>
    </div>

  {:else if stateKind === 'unknown'}
    <div class="autonomy-card autonomy-card--unknown" role="alert">
      <span class="autonomy-state autonomy-state--unknown">Autonomy state unknown — could not read the dial setting.</span>
      <button
        class="ds-btn ds-btn--ghost autonomy-retry"
        type="button"
        data-testid="autonomy-retry"
        onclick={() => void fetchAutonomy()}
      >Retry</button>
    </div>

  {:else if stateKind === 'loaded'}
    <div class="autonomy-card autonomy-card--loaded">
      <!-- Three-segment control -->
      <div
        class="autonomy-segments"
        class:autonomy-segments--measured={pillMeasured}
        role="group"
        aria-label="Autonomy mode"
        bind:this={containerEl}
      >
        <!-- Sliding active pill (decorative, aria-hidden) -->
        <div
          class="autonomy-segments__pill"
          class:autonomy-segments__pill--ready={pillReady}
          aria-hidden="true"
          style="transform: translateX({pillLeft}px); width: {pillWidth}px;"
        ></div>

        {#each AUTONOMY_MODES as mode, i (mode)}
          <button
            class="autonomy-segment"
            class:autonomy-segment--active={mode === currentMode}
            class:autonomy-segment--armed={confirming && pendingMode === mode}
            type="button"
            data-testid="autonomy-mode-{mode}"
            aria-pressed={mode === currentMode ? 'true' : 'false'}
            disabled={saving}
            onclick={() => onSegmentClick(mode)}
            bind:this={segmentEls[i]}
          ><Icon name={MODE_ICONS[mode]} size={14} />{MODE_LABELS[mode]}</button>
        {/each}
      </div>

      <!-- Blurb for current mode -->
      <p class="autonomy-blurb">{MODE_BLURBS[currentMode]}</p>

      <!-- Meta line: actor · time · reason -->
      {#if currentReadError}
        <span class="autonomy-meta__warn" data-testid="autonomy-read-error"
          >autonomy state could not be read — failing closed to Observe</span>
      {:else}
        <div class="autonomy-meta">
          {#if currentActor}
            <span class="autonomy-meta__label">Set by</span>
            <span class="autonomy-meta__actor">{currentActor}</span>
          {/if}
          {#if currentUpdatedAt}
            <time class="autonomy-meta__time" datetime={currentUpdatedAt}
              >{fmtUpdatedAt(currentUpdatedAt)}</time>
          {/if}
          {#if currentReason}
            <span class="autonomy-meta__label">reason:</span>
            <span class="autonomy-meta__reason">{currentReason}</span>
          {/if}
        </div>
      {/if}

      <!-- Confirm row (appears when a different segment is clicked) -->
      {#if confirming && pendingMode}
        <div class="autonomy-confirm-row" transition:slide={{ duration: motionMs(200) }}>
          <p class="autonomy-confirm-hint">
            Switch to <strong>{MODE_LABELS[pendingMode]}</strong>? {MODE_BLURBS[pendingMode]}
          </p>
          <div class="autonomy-confirm-actions">
            <label class="autonomy-reason-label" for="autonomy-reason-input">reason (optional)</label>
            <input
              id="autonomy-reason-input"
              class="autonomy-reason-input"
              type="text"
              maxlength="500"
              placeholder="why this change? (recorded in the audit log)"
              data-testid="autonomy-reason"
              bind:value={reasonInput}
              disabled={saving}
            />
            <button
              class="ds-btn ds-btn--primary autonomy-confirm-btn"
              type="button"
              data-testid="autonomy-confirm"
              onclick={() => void onConfirm()}
              disabled={saving}
            ><Icon name="check" size={14} />{confirmLabel}</button>
            <button
              class="ds-btn ds-btn--ghost autonomy-cancel-btn"
              type="button"
              data-testid="autonomy-cancel"
              onclick={onCancel}
              disabled={saving}
            ><Icon name="x" size={14} />Cancel</button>
          </div>
        </div>
      {/if}

      {#if postError}
        <p class="autonomy-error" data-testid="autonomy-error"
          >Could not save — autonomy state unchanged. Please try again.</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .autonomy-control {
    width: 100%;
  }

  .autonomy-card {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius);
    border: 1px solid var(--ds-border);
    box-shadow: var(--ds-shadow-sm);
  }

  .autonomy-card--loaded {
    background: var(--ds-surface);
    border-color: var(--ds-border);
  }

  .autonomy-card--unknown {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn-border);
    flex-direction: row;
    align-items: center;
  }

  .autonomy-state {
    flex: 1;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }

  .autonomy-state--unknown {
    color: var(--ds-muted);
    font-size: var(--ds-fs-2);
    flex: 1;
  }

  .autonomy-retry {
    flex-shrink: 0;
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }

  /* ---- Three-segment control ---- */
  .autonomy-segments {
    display: flex;
    gap: 0;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    overflow: hidden;
    width: fit-content;
    position: relative;
  }

  /* Sliding active pill — positioned behind the segment buttons */
  .autonomy-segments__pill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    background: var(--ds-stream-surface);
    border-radius: inherit;
    z-index: 0;
    /* Transition is opt-in: added only after first measurement via --ready class */
    pointer-events: none;
  }

  .autonomy-segments__pill--ready {
    transition:
      transform var(--ds-dur) var(--ds-ease),
      width var(--ds-dur) var(--ds-ease);
  }

  .autonomy-segment {
    appearance: none;
    border: none;
    background: var(--ds-surface);
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-muted);
    cursor: pointer;
    border-right: 1px solid var(--ds-border-strong);
    position: relative;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    gap: 0.35em;
    transition:
      background-color var(--ds-dur-fast) var(--ds-ease),
      color var(--ds-dur-fast) var(--ds-ease);
  }

  .autonomy-segment:last-child {
    border-right: none;
  }

  /* Active mode — neutral/calm emphasis, NOT warn/danger */
  .autonomy-segment--active {
    background: var(--ds-stream-surface);
    color: var(--ds-stream-ink);
  }

  /*
   * When the pill has valid nonzero geometry (--measured class on container),
   * let the pill carry the active highlight; make the active segment bg transparent.
   * Measurement failure (jsdom, edge cases) falls back to the solid active style above.
   */
  .autonomy-segments--measured .autonomy-segment--active {
    background: transparent;
    color: var(--ds-stream-ink);
  }

  /* Armed state: segment was clicked but not yet confirmed */
  .autonomy-segment--armed {
    box-shadow: inset 0 0 0 1px var(--ds-stream);
    background: color-mix(in srgb, var(--ds-stream-surface) 60%, transparent);
    color: var(--ds-stream-ink);
  }

  /* Armed takes precedence over measured-transparent-active */
  .autonomy-segments--measured .autonomy-segment--armed {
    background: color-mix(in srgb, var(--ds-stream-surface) 60%, transparent);
    color: var(--ds-stream-ink);
  }

  .autonomy-segment:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  /* ---- Blurb ---- */
  .autonomy-blurb {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  /* ---- Meta line ---- */
  .autonomy-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  .autonomy-meta__label {
    color: var(--ds-muted);
  }

  .autonomy-meta__actor {
    font-weight: 600;
    color: var(--ds-fg);
  }

  .autonomy-meta__time {
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }

  .autonomy-meta__reason {
    font-style: italic;
    color: var(--ds-fg);
  }

  .autonomy-meta__warn {
    font-size: var(--ds-fs-1);
    color: var(--ds-warn);
    font-style: italic;
  }

  /* ---- Confirm row ---- */
  .autonomy-confirm-row {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    overflow: hidden;
  }

  .autonomy-confirm-hint {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  .autonomy-confirm-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
  }

  .autonomy-reason-label {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    white-space: nowrap;
  }

  .autonomy-reason-input {
    flex: 1;
    min-width: 10rem;
    max-width: 22rem;
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.6em;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg);
  }

  .autonomy-confirm-btn {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }

  .autonomy-cancel-btn {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }

  /* ---- Inline error ---- */
  .autonomy-error {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-danger-ink);
  }
</style>
