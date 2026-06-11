<script lang="ts">
  // AutonomyControl — three-segment dial (Observe / Propose / Propose + Apply).
  //
  // Mirrors PauseControl: same `call` prop contract, same monotonic seq stale-
  // response guard, same single-flight busy guard, same structural GET validation
  // (malformed 200 → 'unknown'), no optimistic updates (apply POST response).
  //
  // Visual: ds-* token palette only; Observe segment is NOT styled as a warning —
  // modes are operator choices, not alarms.

  import { onMount } from 'svelte';
  import { AUTONOMY_MODES, MODE_LABELS, MODE_BLURBS, parseAutonomyDoc } from '../lib/autonomy';
  import type { AutonomyDoc, AutonomyMode } from '../lib/autonomy';

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
      <div class="autonomy-segments" role="group" aria-label="Autonomy mode">
        {#each AUTONOMY_MODES as mode (mode)}
          <button
            class="autonomy-segment"
            class:autonomy-segment--active={mode === currentMode}
            type="button"
            data-testid="autonomy-mode-{mode}"
            aria-pressed={mode === currentMode ? 'true' : 'false'}
            disabled={saving}
            onclick={() => onSegmentClick(mode)}
          >{MODE_LABELS[mode]}</button>
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
        <div class="autonomy-confirm-row">
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
              placeholder="reason (optional)"
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
            >{confirmLabel}</button>
            <button
              class="ds-btn ds-btn--ghost autonomy-cancel-btn"
              type="button"
              data-testid="autonomy-cancel"
              onclick={onCancel}
              disabled={saving}
            >Cancel</button>
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
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border);
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
