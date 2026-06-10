<script lang="ts">
  // PauseControl — the operator kill switch for DriftScribe.
  //
  // Eager-fetches GET /pause on mount (safety status, not a lazy detail panel).
  // Three render states: running (quiet card), paused (calm prominent banner),
  // unknown (fetch error, fail-closed amber note).
  //
  // Two-step inline confirm (no modal): clicking Pause/Resume expands a confirm
  // row in-place. POST result (not optimistic update) drives the state flip.
  //
  // Svelte 5 whitespace rule: every label string is a single expression; meta
  // line parts are sibling elements spaced by CSS flex gap (no text seams
  // across {#if} boundaries).

  import { onMount } from 'svelte';

  let {
    call,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
  } = $props();

  // --------------------------------------------------------------------------
  // State
  // --------------------------------------------------------------------------

  // stateKind drives the three render branches. pausedMeta holds the paused
  // doc fields when stateKind === 'paused'. Kept as separate reactive vars
  // (not a union-typed object) to avoid Svelte 5's script-transform issue with
  // locally-defined complex union type generics.
  let stateKind = $state<'loading' | 'running' | 'paused' | 'unknown'>('loading');
  let pausedReason = $state<string | null>(null);
  let pausedActor = $state<string | null>(null);
  let pausedUpdatedAt = $state<string | null>(null);
  let pausedReadError = $state(false);

  let confirming = $state(false);
  let saving = $state(false);
  let postError = $state(false);
  let reasonInput = $state('');

  // Concurrency guard (App.svelte's sequence-counter pattern): every
  // fetchPause / onConfirm run captures ++seq, and in-flight callbacks bail
  // after each await when their captured id is stale — a slow response can
  // never silently overwrite fresher kill-switch state. Non-reactive local.
  let seq = 0;

  // --------------------------------------------------------------------------
  // Structural guard — paused must be boolean
  // --------------------------------------------------------------------------

  function isValidPauseDoc(body: unknown): body is {
    paused: boolean;
    reason?: string | null;
    actor?: string | null;
    updated_at?: string | null;
    read_error?: boolean;
  } {
    if (typeof body !== 'object' || body === null) return false;
    return typeof (body as Record<string, unknown>).paused === 'boolean';
  }

  // --------------------------------------------------------------------------
  // Fetch
  // --------------------------------------------------------------------------

  async function fetchPause(): Promise<void> {
    const my = ++seq;
    stateKind = 'loading';
    postError = false;
    let resp: Response;
    try {
      resp = await call('/pause');
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
    if (!isValidPauseDoc(body)) {
      stateKind = 'unknown';
      return;
    }
    applyDoc(body);
  }

  function applyDoc(doc: {
    paused: boolean;
    reason?: string | null;
    actor?: string | null;
    updated_at?: string | null;
    read_error?: boolean;
  }): void {
    if (doc.paused) {
      pausedReason = doc.reason ?? null;
      pausedActor = doc.actor ?? null;
      pausedUpdatedAt = doc.updated_at ?? null;
      pausedReadError = doc.read_error ?? false;
      stateKind = 'paused';
    } else {
      stateKind = 'running';
    }
  }

  // Mount: eager fetch
  onMount(() => {
    void fetchPause();
  });

  // --------------------------------------------------------------------------
  // Toggle (open confirm row)
  // --------------------------------------------------------------------------

  function onToggle(): void {
    confirming = true;
    postError = false;
    reasonInput = '';
  }

  function onCancel(): void {
    confirming = false;
    postError = false;
    reasonInput = '';
  }

  // --------------------------------------------------------------------------
  // Confirm POST
  // --------------------------------------------------------------------------

  async function onConfirm(): Promise<void> {
    // Single-flight: the confirm button is disabled while saving, but a
    // synthetic/rapid double activation must still produce exactly one POST.
    if (saving) return;
    const my = ++seq;
    const nextPaused = stateKind !== 'paused';
    saving = true;
    postError = false;

    const trimmed = reasonInput.trim();
    const requestBody: Record<string, unknown> = { paused: nextPaused };
    if (nextPaused && trimmed.length > 0) {
      requestBody.reason = trimmed;
    }

    let resp: Response;
    try {
      resp = await call('/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      });
    } catch {
      saving = false;
      if (my !== seq) return; // a newer run owns the UI — only release busy
      postError = true;
      confirming = false;
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
      return;
    }

    if (my !== seq) {
      saving = false;
      return;
    }
    if (!isValidPauseDoc(body)) {
      saving = false;
      postError = true;
      confirming = false;
      return;
    }

    // Flip state from response — no optimistic update
    applyDoc(body);
    saving = false;
    confirming = false;
    reasonInput = '';
  }

  // --------------------------------------------------------------------------
  // Time formatting (mirrors DecisionsRail fmtCreatedAt)
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

  // Derived: the human-readable label string for the pause-toggle button
  // (single expression — no whitespace seam).
  const toggleLabel = $derived(
    saving ? 'Saving…' : stateKind === 'paused' ? 'Resume' : 'Pause',
  );

  // Derived: the confirm action label (single expression).
  const confirmLabel = $derived(
    saving ? 'Saving…' : stateKind === 'paused' ? 'Confirm resume' : 'Confirm pause',
  );
</script>

<div class="pause-control" data-testid="pause-control" role="region" aria-label="DriftScribe pause control">

  {#if stateKind === 'loading'}
    <!-- Minimal loading placeholder — not a separate testid, just invisible to tests -->
    <div class="pause-card pause-card--running">
      <span class="pause-dot pause-dot--ok" aria-hidden="true"></span>
      <span class="pause-state" data-testid="pause-state">Loading pause state…</span>
    </div>

  {:else if stateKind === 'unknown'}
    <!-- Unknown / fetch error — amber fail-closed note -->
    <div class="pause-card pause-card--unknown" role="alert">
      <span class="pause-state pause-state--unknown" data-testid="pause-state">Pause state unknown — DriftScribe fails closed: changes are blocked until this resolves.</span>
      <button
        class="ds-btn ds-btn--ghost pause-retry"
        type="button"
        data-testid="pause-retry"
        onclick={() => void fetchPause()}
      >Retry</button>
    </div>

  {:else if stateKind === 'running'}
    <!-- Running — quiet one-line card -->
    <div class="pause-card pause-card--running">
      <div class="pause-row">
        <span class="pause-dot pause-dot--ok" aria-hidden="true"></span>
        <span class="pause-state" data-testid="pause-state">DriftScribe is active — it can act only within the guardrails below.</span>
        {#if !confirming}
          <button
            class="ds-btn ds-btn--ghost pause-toggle"
            type="button"
            data-testid="pause-toggle"
            onclick={onToggle}
          >{toggleLabel}</button>
        {/if}
      </div>

      {#if confirming}
        <div class="pause-confirm-row">
          <p class="pause-confirm-hint">Pause all agent activity? New chats, rechecks, and approvals will be refused until you resume.</p>
          <div class="pause-confirm-actions">
            <label class="pause-reason-label" for="pause-reason-input">reason (optional)</label>
            <input
              id="pause-reason-input"
              class="pause-reason-input"
              type="text"
              maxlength="500"
              placeholder="reason (optional)"
              data-testid="pause-reason"
              bind:value={reasonInput}
              disabled={saving}
            />
            <button
              class="ds-btn ds-btn--primary pause-confirm-btn"
              type="button"
              data-testid="pause-confirm"
              onclick={() => void onConfirm()}
              disabled={saving}
            >{confirmLabel}</button>
            <button
              class="ds-btn ds-btn--ghost pause-cancel-btn"
              type="button"
              data-testid="pause-cancel"
              onclick={onCancel}
              disabled={saving}
            >Cancel</button>
          </div>
        </div>
      {/if}

      {#if postError}
        <p class="pause-error" data-testid="pause-error">Could not save — pause state unchanged. Please try again.</p>
      {/if}
    </div>

  {:else if stateKind === 'paused'}
    <!-- Paused — prominent but calm full-width banner -->
    <div class="pause-card pause-card--paused" role="status" aria-live="polite">
      <div class="pause-row">
        <!-- {' '} is the ONLY whitespace at the emoji seam (CapabilityCard
             convention) — textContent stays exactly "⏸ DriftScribe is paused…"
             (textContent ignores aria-hidden), which the exact-string test pins. -->
        <span class="pause-state pause-state--paused" data-testid="pause-state"><span aria-hidden="true">⏸</span>{' '}DriftScribe is paused — no new agent activity will start.</span>
        {#if !confirming}
          <button
            class="ds-btn ds-btn--ghost pause-toggle"
            type="button"
            data-testid="pause-toggle"
            onclick={onToggle}
          >{toggleLabel}</button>
        {/if}
      </div>

      <!-- Meta line: actor · time · reason — sibling spans + CSS gap (no text seams) -->
      <div class="pause-meta">
        {#if pausedReadError}
          <span class="pause-meta__warn">pause state could not be read — failing closed</span>
        {:else}
          {#if pausedActor}
            <span class="pause-meta__label">Paused by</span>
            <span class="pause-meta__actor">{pausedActor}</span>
          {/if}
          {#if pausedUpdatedAt}
            <time class="pause-meta__time" datetime={pausedUpdatedAt}>{fmtUpdatedAt(pausedUpdatedAt)}</time>
          {/if}
          {#if pausedReason}
            <span class="pause-meta__label">reason:</span>
            <span class="pause-meta__reason">{pausedReason}</span>
          {/if}
        {/if}
      </div>

      {#if confirming}
        <div class="pause-confirm-row">
          <p class="pause-confirm-hint">Resume agent activity? DriftScribe will be able to start new chats, rechecks, and approvals.</p>
          <div class="pause-confirm-actions">
            <button
              class="ds-btn ds-btn--primary pause-confirm-btn"
              type="button"
              data-testid="pause-confirm"
              onclick={() => void onConfirm()}
              disabled={saving}
            >{confirmLabel}</button>
            <button
              class="ds-btn ds-btn--ghost pause-cancel-btn"
              type="button"
              data-testid="pause-cancel"
              onclick={onCancel}
              disabled={saving}
            >Cancel</button>
          </div>
        </div>
      {/if}

      {#if postError}
        <p class="pause-error" data-testid="pause-error">Could not save — pause state unchanged. Please try again.</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .pause-control {
    width: 100%;
  }

  /* ---- Card shell ---- */
  .pause-card {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border);
  }

  .pause-card--running {
    background: var(--ds-surface);
    border-color: var(--ds-border);
  }

  /* Calm neutral/amber surface — NOT the error color */
  .pause-card--paused {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn-border);
  }

  .pause-card--unknown {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn-border);
    flex-direction: row;
    align-items: center;
  }

  /* ---- Row layout ---- */
  .pause-row {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
    flex-wrap: wrap;
  }

  /* ---- Status dot (running indicator) ---- */
  .pause-dot {
    display: inline-block;
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .pause-dot--ok {
    background: var(--ds-ok);
  }

  /* ---- State text ---- */
  .pause-state {
    flex: 1;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }
  .pause-state--paused {
    font-weight: var(--ds-fw-bold);
  }
  .pause-state--unknown {
    color: var(--ds-muted);
    font-size: var(--ds-fs-2);
    flex: 1;
  }

  /* ---- Toggle button (ghost, calm) ---- */
  .pause-toggle {
    flex-shrink: 0;
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
    color: var(--ds-muted);
    border-color: var(--ds-border-strong);
  }

  /* ---- Retry button ---- */
  .pause-retry {
    flex-shrink: 0;
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }

  /* ---- Meta line (actor · time · reason) ---- */
  .pause-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .pause-meta__label {
    color: var(--ds-muted);
  }
  .pause-meta__actor {
    font-weight: 600;
    color: var(--ds-fg);
  }
  .pause-meta__time {
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }
  .pause-meta__reason {
    font-style: italic;
    color: var(--ds-fg);
  }
  .pause-meta__warn {
    color: var(--ds-warn);
    font-style: italic;
  }

  /* ---- Confirm row ---- */
  .pause-confirm-row {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
  }
  .pause-confirm-hint {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .pause-confirm-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .pause-reason-label {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    white-space: nowrap;
  }
  .pause-reason-input {
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
  /* No local :focus rule — base.css's global :focus-visible applies the
     standard box-shadow: var(--ds-ring) treatment to all inputs. */
  .pause-confirm-btn {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }
  .pause-cancel-btn {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }

  /* ---- Inline error ---- */
  .pause-error {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-danger-ink);
  }
</style>
