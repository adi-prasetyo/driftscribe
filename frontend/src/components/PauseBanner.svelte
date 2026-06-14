<script lang="ts">
  // PauseBanner — the LOUD content surface of the pause kill-switch.
  //
  // Renders NOTHING while running/loading (the compact header PausePill owns
  // the all-clear state). Only when the agent is paused or the pause state is
  // unknown/fail-closed does this prominent banner return below the header —
  // an engaged kill-switch must never be a tiny chip.
  //
  // Server-truth state + the confirm POST live in the shared pauseStore; only
  // the transient resume-confirm UI (confirming/saving/postError) is local.

  import { slide } from 'svelte/transition';
  import { motionMs } from '../lib/motion';
  import Icon from './Icon.svelte';
  import type { PauseStore } from '../lib/pauseStore';

  let { pause }: { pause: PauseStore } = $props();

  const st = $derived($pause);

  // Local resume-confirm UI (the loud banner's Resume → inline confirm row).
  let confirming = $state(false);
  let saving = $state(false);
  let postError = $state(false);

  // Reset on external transition (e.g. a refresh that finds it running) so a
  // stale confirm row / error can't survive a state flip and reappear later.
  $effect(() => {
    if (st.kind !== 'paused') {
      confirming = false;
      postError = false;
      saving = false;
    }
  });

  function onResumeClick(): void {
    confirming = true;
    postError = false;
  }

  function onCancel(): void {
    if (saving) return;
    confirming = false;
    postError = false;
  }

  async function onConfirm(): Promise<void> {
    if (saving) return;
    saving = true;
    postError = false;
    const ok = await pause.confirm(false);
    saving = false;
    confirming = false;
    if (!ok) postError = true;
  }

  // Time formatting (mirrors PauseControl / DecisionsRail fmtCreatedAt).
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

  const confirmLabel = $derived(saving ? 'Saving…' : 'Confirm resume');
</script>

{#if st.kind === 'unknown'}
  <!-- Unknown / fetch error — amber fail-closed note -->
  <div class="pause-card pause-card--unknown" data-testid="pause-banner" role="alert">
    <span class="pause-state pause-state--unknown" data-testid="pause-state"
      >Pause state unknown — DriftScribe fails closed: changes are blocked until this resolves.</span
    >
    <button
      class="ds-btn ds-btn--ghost pause-retry"
      type="button"
      data-testid="pause-retry"
      onclick={() => void pause.fetchPause()}>Retry</button
    >
  </div>
{:else if st.kind === 'paused'}
  <!-- Paused — prominent but calm full-width banner -->
  <div
    class="pause-card pause-card--paused"
    data-testid="pause-banner"
    role="status"
    aria-live="polite"
  >
    <div class="pause-row">
      <!-- Icon is aria-hidden SVG — contributes NO textContent. Exact-string
           contract: pause-state textContent is
           "DriftScribe is paused — no new agent activity will start." -->
      <span class="pause-state pause-state--paused" data-testid="pause-state"
        ><Icon name="pause" size={14} />DriftScribe is paused — no new agent activity will start.</span
      >
      {#if !confirming}
        <button
          class="ds-btn ds-btn--ghost pause-toggle"
          type="button"
          data-testid="pause-toggle"
          onclick={onResumeClick}><Icon name="play" size={14} />Resume</button
        >
      {/if}
    </div>

    <!-- Meta line: actor · time · reason — sibling spans + CSS gap (no text seams) -->
    <div class="pause-meta">
      {#if st.readError}
        <span class="pause-meta__warn">pause state could not be read — failing closed</span>
      {:else}
        {#if st.actor}
          <span class="pause-meta__label">Paused by</span>
          <span class="pause-meta__actor">{st.actor}</span>
        {/if}
        {#if st.updatedAt}
          <time class="pause-meta__time" datetime={st.updatedAt}>{fmtUpdatedAt(st.updatedAt)}</time>
        {/if}
        {#if st.reason}
          <span class="pause-meta__label">reason:</span>
          <span class="pause-meta__reason">{st.reason}</span>
        {/if}
      {/if}
    </div>

    {#if confirming}
      <div class="pause-confirm-row" data-testid="pause-confirm-row" transition:slide={{ duration: motionMs(200) }}>
        <p class="pause-confirm-hint">
          Resume agent activity? DriftScribe will be able to start new chats, rechecks, and approvals.
        </p>
        <div class="pause-confirm-actions">
          <button
            class="ds-btn ds-btn--primary pause-confirm-btn"
            type="button"
            data-testid="pause-confirm"
            onclick={() => void onConfirm()}
            disabled={saving}><Icon name="check" size={14} />{confirmLabel}</button
          >
          <button
            class="ds-btn ds-btn--ghost pause-cancel-btn"
            type="button"
            data-testid="pause-cancel"
            onclick={onCancel}
            disabled={saving}><Icon name="x" size={14} />Cancel</button
          >
        </div>
      </div>
    {/if}

    {#if postError}
      <p class="pause-error" data-testid="pause-error">
        Could not save — pause state unchanged. Please try again.
      </p>
    {/if}
  </div>
{/if}

<style>
  /* ---- Card shell ---- */
  .pause-card {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius);
    border: 1px solid var(--ds-border);
    box-shadow: var(--ds-shadow-sm);
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

  /* ---- State text ---- */
  .pause-state {
    flex: 1;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }
  .pause-state--paused {
    font-weight: var(--ds-fw-bold);
    /* The leading pause icon sits inline before the text — flex it apart. */
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
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
    overflow: hidden;
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
