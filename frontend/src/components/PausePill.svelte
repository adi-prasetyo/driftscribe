<script lang="ts">
  // PausePill — the COMPACT header surface of the pause kill-switch.
  //
  // Running (the ~95% case): a quiet green status pill that doubles as the
  // pause trigger — clicking it opens a small popover hosting the two-step
  // confirm (+ optional reason). Paused / unknown: the pill turns loud but is
  // status-only — the actionable Resume / Retry live in the prominent
  // PauseBanner below the header (the "pill + banner" decision).
  //
  // Server-truth state + the confirm POST live in the shared pauseStore; only
  // the transient popover UI (open/saving/postError/reasonInput) is local.

  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import { motionMs } from '../lib/motion';
  import { announceHeaderPopoverOpen, HEADER_POPOVER_EVENT } from '../lib/headerPopover';
  import Icon from './Icon.svelte';
  import type { PauseStore } from '../lib/pauseStore';

  let { pause }: { pause: PauseStore } = $props();

  const st = $derived($pause);

  // Local popover UI.
  let open = $state(false);
  let saving = $state(false);
  let postError = $state(false);
  let reasonInput = $state('');

  let containerEl = $state<HTMLDivElement | null>(null);
  let toggleEl = $state<HTMLButtonElement | null>(null);
  let reasonEl = $state<HTMLInputElement | null>(null);

  // Reset on external transition: if the store leaves `running` (e.g. a refresh
  // finds it paused), tear down popover state so a stale open panel / typed
  // reason can't reappear on a later running cycle. (Codex #4)
  $effect(() => {
    if (st.kind !== 'running') {
      open = false;
      postError = false;
      reasonInput = '';
      saving = false;
    }
  });

  // Focus the reason input when the popover opens (best-effort; guarded ref).
  $effect(() => {
    if (open && reasonEl) reasonEl.focus();
  });

  // returnFocus=false for foreign-bus closes (the Autonomy pill opened) so we
  // don't yank focus back to this now-closed toggle (autonomy-pill plan-review #1).
  function closePopover(returnFocus = true): void {
    open = false;
    postError = false;
    reasonInput = '';
    // Return focus to the toggle if it still exists (after a successful pause
    // the running button unmounts — guard handles that).
    if (returnFocus) toggleEl?.focus();
  }

  function onToggle(): void {
    if (open) {
      closePopover();
    } else {
      open = true;
      postError = false;
      announceHeaderPopoverOpen('pause');
    }
  }

  onMount(() => {
    const onForeign = (e: Event) => {
      // A foreign open (the Autonomy pill) closes us without stealing focus.
      // Gated on !saving: never tear down mid-POST, so both popovers may overlap
      // ONLY transiently while one is committing (the saving-exception invariant).
      if ((e as CustomEvent).detail?.id !== 'pause' && open && !saving) closePopover(false);
    };
    window.addEventListener(HEADER_POPOVER_EVENT, onForeign);
    return () => window.removeEventListener(HEADER_POPOVER_EVENT, onForeign);
  });

  async function onConfirm(): Promise<void> {
    if (saving) return;
    saving = true;
    postError = false;
    const ok = await pause.confirm(true, reasonInput);
    saving = false;
    if (ok) {
      open = false;
      reasonInput = '';
      postError = false;
    } else {
      postError = true;
    }
  }

  // Dismiss gated on !saving (Codex #3): never tear down the popover mid-commit,
  // or a failed POST would set postError into a closed panel and be lost.
  function onWindowKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape' && open && !saving) closePopover();
  }
  function onWindowPointerDown(e: PointerEvent): void {
    if (!open || saving) return;
    if (containerEl && !containerEl.contains(e.target as Node)) closePopover();
  }

  const confirmLabel = $derived(saving ? 'Saving…' : 'Confirm pause');
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<div class="pause-pill" bind:this={containerEl}>
  {#if st.kind === 'loading'}
    <span class="ds-pill ds-pill--muted pause-pill__status" data-testid="pause-pill-state"
      ><span class="pause-pill__dot" aria-hidden="true"></span>Checking…</span
    >
  {:else if st.kind === 'running'}
    <button
      class="ds-pill ds-pill--ok pause-pill__btn"
      class:pause-pill__btn--open={open}
      type="button"
      data-testid="pause-pill-toggle"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-label="DriftScribe is active. Agent activity allowed within guardrails. Pause DriftScribe."
      bind:this={toggleEl}
      onclick={onToggle}
      ><span class="pause-pill__dot pause-pill__dot--ok" aria-hidden="true"></span>Active<Icon
        name="chevron-down"
        size={12}
        extraClass="pause-pill__chev"
      /></button
    >

    {#if open}
      <div
        class="pause-popover"
        data-testid="pause-popover"
        role="dialog"
        aria-label="Pause DriftScribe"
        transition:slide={{ duration: motionMs(160) }}
      >
        <p class="pause-popover__hint">
          Pause all agent activity? New chats, rechecks, and approvals will be refused until you
          resume.
        </p>
        <label class="pause-popover__label" for="pause-popover-reason">reason (optional)</label>
        <input
          id="pause-popover-reason"
          class="pause-popover__reason"
          type="text"
          maxlength="500"
          placeholder="e.g. scheduled maintenance"
          data-testid="pause-popover-reason"
          bind:this={reasonEl}
          bind:value={reasonInput}
          disabled={saving}
        />
        <div class="pause-popover__actions">
          <button
            class="ds-btn ds-btn--primary pause-popover__confirm"
            type="button"
            data-testid="pause-popover-confirm"
            onclick={() => void onConfirm()}
            disabled={saving}><Icon name="check" size={14} />{confirmLabel}</button
          >
          <button
            class="ds-btn ds-btn--ghost pause-popover__cancel"
            type="button"
            data-testid="pause-popover-cancel"
            onclick={() => closePopover()}
            disabled={saving}><Icon name="x" size={14} />Cancel</button
          >
        </div>
        {#if postError}
          <p class="pause-popover__error" data-testid="pause-popover-error">
            Could not pause. State unchanged. Please try again.
          </p>
        {/if}
      </div>
    {/if}
  {:else if st.kind === 'paused'}
    <span class="ds-pill ds-pill--warn pause-pill__status" data-testid="pause-pill-state"
      ><Icon name="pause" size={12} />Paused</span
    >
  {:else}
    <!-- unknown / fail-closed -->
    <span class="ds-pill ds-pill--warn pause-pill__status" data-testid="pause-pill-state"
      ><Icon name="alert-triangle" size={12} />State unknown</span
    >
  {/if}
</div>

<style>
  .pause-pill {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  /* The running pill is a button — strip native chrome, keep .ds-pill look. */
  .pause-pill__btn {
    appearance: none;
    font-family: inherit;
    cursor: pointer;
    transition:
      background var(--ds-dur-fast) var(--ds-ease),
      border-color var(--ds-dur-fast) var(--ds-ease);
  }
  .pause-pill__btn:hover {
    border-color: var(--ds-ok-ink);
  }
  .pause-pill__btn :global(.pause-pill__chev) {
    transition: transform var(--ds-dur-fast) var(--ds-ease);
    opacity: 0.7;
  }
  .pause-pill__btn--open :global(.pause-pill__chev) {
    transform: rotate(180deg);
  }

  .pause-pill__dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--ds-muted);
    flex-shrink: 0;
  }
  .pause-pill__dot--ok {
    background: var(--ds-ok);
  }

  /* ---- Popover ---- */
  .pause-popover {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    width: min(20rem, 80vw);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius);
    border: 1px solid var(--ds-border);
    background: var(--ds-surface);
    box-shadow: var(--ds-shadow-md, var(--ds-shadow-sm));
    overflow: hidden;
    text-align: left;
  }
  .pause-popover__hint {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .pause-popover__label {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .pause-popover__reason {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.6em;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg);
  }
  .pause-popover__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .pause-popover__confirm,
  .pause-popover__cancel {
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }
  .pause-popover__error {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-danger-ink);
  }
</style>
