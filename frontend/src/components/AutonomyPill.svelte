<script lang="ts">
  // AutonomyPill — the COMPACT header surface of the autonomy dial.
  //
  // Mirrors PausePill: a quiet status pill in the header that, while loaded,
  // doubles as the trigger — clicking it opens an anchored popover hosting the
  // full three-segment dial (Observe / Propose / Propose + Apply) with the
  // arm-then-confirm flow, reason field, explainer, and meta line.
  //
  // Server-truth state + the confirm POST live in the shared autonomyStore; only
  // the transient popover UI (open / armed / saving / postError / reason /
  // explainer / sliding-pill geometry) is local. Modes are operator choices, not
  // alarms — no per-mode warn coloring; the ONLY warn tint is the degraded
  // read_error / unknown signal.

  import { onMount, tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import {
    AUTONOMY_MODES,
    MODE_LABELS,
    MODE_BLURBS,
    AUTONOMY_EXPLAINER_HEADING,
    AUTONOMY_EXPLAINER_BODY,
    type AutonomyMode,
  } from '../lib/autonomy';
  import type { AutonomyStore } from '../lib/autonomyStore';
  import { announceHeaderPopoverOpen, HEADER_POPOVER_EVENT } from '../lib/headerPopover';
  import { motionMs } from '../lib/motion';
  import Icon from './Icon.svelte';

  let { autonomy }: { autonomy: AutonomyStore } = $props();
  const st = $derived($autonomy);

  const MODE_ICONS = {
    observe: 'eye',
    propose: 'git-pull-request',
    propose_apply: 'zap',
  } as const;

  // ---- local popover UI ----
  let open = $state(false);
  let confirming = $state(false);
  let pendingMode = $state<AutonomyMode | null>(null);
  let saving = $state(false);
  let postError = $state(false);
  let reasonInput = $state('');
  let explainerOpen = $state(false);

  // ---- refs ----
  let containerEl = $state<HTMLDivElement | null>(null); // outside-click root
  let toggleEl = $state<HTMLButtonElement | null>(null);
  let reasonEl = $state<HTMLInputElement | null>(null);
  let segmentsEl = $state<HTMLElement | null>(null); // dial container (measure)
  let segmentEls = $state<(HTMLElement | null)[]>([null, null, null]);

  // ---- sliding-pill geometry ----
  let pillLeft = $state(0);
  let pillWidth = $state(0);
  let pillMeasured = $state(false);
  let pillReady = $state(false);
  let pillRafId: number | undefined;

  // Popover placement. The pill is the LEFT-most header action and the header
  // wraps the actions onto their own (left-aligned) row on sub-wide viewports, so
  // a pill-anchored `right: 0` dropdown overflows the viewport's left edge. We
  // instead place the popover relative to the VIEWPORT on open and clamp it
  // on-screen: right edge aligned to the pill's right edge, but if that pushes the
  // left edge off-screen, pin the left edge to the inset. position:fixed so the
  // (wrapping) header height never matters.
  let popoverStyle = $state('');
  const POPOVER_INSET = 12; // ~ var(--ds-sp-3)
  const POPOVER_MAX_W = 384; // 24rem

  function positionPopover(): void {
    if (!toggleEl || typeof window === 'undefined') return;
    const r = toggleEl.getBoundingClientRect();
    const vw = window.innerWidth;
    const width = Math.min(POPOVER_MAX_W, vw - 2 * POPOVER_INSET);
    // default: right edge under the pill's right edge
    let right = Math.max(POPOVER_INSET, vw - r.right);
    // if the left edge would fall off-screen, pin the left edge to the inset
    if (vw - right - width < POPOVER_INSET) {
      right = Math.max(POPOVER_INSET, vw - POPOVER_INSET - width);
    }
    popoverStyle =
      `position:fixed; top:${Math.round(r.bottom + 6)}px; right:${Math.round(right)}px; ` +
      `left:auto; bottom:auto; width:${Math.round(width)}px;`;
  }

  // Pill label: name the mode in words. read_error is a degraded signal, not a
  // per-mode alarm — show "Observe · fail-closed" (the server fails closed to
  // Observe) on a warn tint.
  const pillLabel = $derived(
    st.readError ? `${MODE_LABELS.observe} · fail-closed` : MODE_LABELS[st.mode],
  );
  const confirmLabel = $derived(saving ? 'Saving…' : 'Confirm');

  function resetPopover(): void {
    confirming = false;
    pendingMode = null;
    postError = false;
    reasonInput = '';
    explainerOpen = false;
    pillMeasured = false;
    pillReady = false; // Codex #3: never reopen with stale geometry
    if (pillRafId !== undefined) {
      cancelAnimationFrame(pillRafId);
      pillRafId = undefined;
    }
  }

  // returnFocus=false for foreign-bus closes so closing THIS popover because the
  // OTHER opened does not yank focus back to this (now-closed) toggle and away
  // from the pill the user just opened (Codex plan-review #1).
  function closePopover(returnFocus = true): void {
    open = false;
    resetPopover();
    if (returnFocus) toggleEl?.focus();
  }

  function onToggle(): void {
    if (open) {
      closePopover();
    } else {
      resetPopover();
      positionPopover(); // place before paint so it never flashes pill-anchored
      open = true;
      announceHeaderPopoverOpen('autonomy');
    }
  }

  // Keep the popover on-screen if the viewport resizes while it is open.
  function onWindowResize(): void {
    if (open) positionPopover();
  }

  // Focus the ACTIVE segment on open (Codex plan-review #2): type-safe via the
  // bound ref, no querySelector. st.mode is read inside the async callback so it
  // is NOT a tracked dep — the effect re-runs only when `open` toggles.
  $effect(() => {
    if (!open) return;
    void tick().then(() => {
      if (!open) return;
      segmentEls[AUTONOMY_MODES.indexOf(st.mode)]?.focus();
    });
  });

  // Sliding-pill measurement — only while the popover is open AND loaded; the
  // segments exist only then. Track stateKind AND mode (mirrors AutonomyControl:
  // currentMode defaults to propose_apply, so a GET returning that value never
  // changes state and an effect keyed only on mode would miss the mount). The
  // cleanup cancels the pending rAF (the effect re-runs on a mode change while
  // open) AND disconnects the per-open ResizeObserver (Codex #3 + plan-review #3).
  $effect(() => {
    const o = open;
    const kind = st.kind;
    const mode = st.mode;
    void mode;
    let cancelled = false;
    let ro: { disconnect(): void } | undefined;

    if (o && kind === 'loaded') {
      void tick().then(() => {
        if (cancelled) return;
        measurePill();
        if (typeof ResizeObserver !== 'undefined' && segmentsEl) {
          const observer = new ResizeObserver(() => {
            if (!cancelled) measurePill();
          });
          observer.observe(segmentsEl);
          ro = observer;
        }
      });
    }

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
    const i = AUTONOMY_MODES.indexOf(st.mode);
    const el = i >= 0 ? segmentEls[i] : null;
    if (!el) return;
    const left = el.offsetLeft;
    const width = el.offsetWidth;
    if (width > 0) {
      pillLeft = left;
      pillWidth = width;
      pillMeasured = true;
      if (!pillReady && pillRafId === undefined) {
        pillRafId = requestAnimationFrame(() => {
          pillRafId = undefined;
          pillReady = true;
        });
      }
    }
  }

  function onSegmentClick(mode: AutonomyMode): void {
    if (saving || mode === st.mode) return;
    confirming = true;
    pendingMode = mode;
    postError = false;
    reasonInput = '';
    void tick().then(() => reasonEl?.focus());
  }

  function onCancelArm(): void {
    confirming = false;
    pendingMode = null;
    postError = false;
    reasonInput = '';
  }

  async function onConfirm(): Promise<void> {
    if (saving || !pendingMode) return;
    saving = true;
    postError = false;
    const ok = await autonomy.confirm(pendingMode, reasonInput);
    saving = false;
    if (ok) {
      // pill now reflects the new mode → close (matches PausePill on success)
      open = false;
      resetPopover();
      toggleEl?.focus();
    } else {
      postError = true;
      confirming = false;
      pendingMode = null;
    }
  }

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

  // Dismiss gated on !saving — never tear down mid-commit (a failed POST must be
  // able to set postError into the still-open panel).
  function onWindowKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape' && open && !saving) closePopover();
  }
  function onWindowPointerDown(e: PointerEvent): void {
    if (!open || saving) return;
    if (containerEl && !containerEl.contains(e.target as Node)) closePopover();
  }

  onMount(() => {
    const onForeign = (e: Event) => {
      // A foreign open (the Pause pill) closes us WITHOUT returning focus (so we
      // don't yank it off the pill the user just opened). Gated on !saving: never
      // tear down mid-POST — so both popovers may be open ONLY transiently while
      // one is committing (the documented saving-exception invariant).
      if ((e as CustomEvent).detail?.id !== 'autonomy' && open && !saving) closePopover(false);
    };
    window.addEventListener(HEADER_POPOVER_EVENT, onForeign);
    return () => window.removeEventListener(HEADER_POPOVER_EVENT, onForeign);
  });
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} onresize={onWindowResize} />

<div class="autonomy-pill" bind:this={containerEl}>
  {#if st.kind === 'loading'}
    <span class="ds-pill ds-pill--muted autonomy-pill__status" data-testid="autonomy-pill-state"
      ><span class="autonomy-pill__dot" aria-hidden="true"></span>Mode · checking…</span
    >
  {:else if st.kind === 'unknown'}
    <button
      class="ds-pill ds-pill--warn autonomy-pill__btn"
      type="button"
      data-testid="autonomy-pill-retry"
      aria-label="Autonomy state could not be read. Retry."
      title="Autonomy state could not be read; the agent is failing closed to Observe. Click to retry."
      onclick={() => void autonomy.fetchAutonomy()}
      ><Icon name="alert-triangle" size={12} />State unknown · retry</button
    >
  {:else}
    <button
      class="ds-pill {st.readError ? 'ds-pill--warn' : 'ds-pill--muted'} autonomy-pill__btn"
      class:autonomy-pill__btn--open={open}
      type="button"
      data-testid="autonomy-pill-toggle"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-label={`Autonomy mode: ${pillLabel}. Change it.`}
      bind:this={toggleEl}
      onclick={onToggle}
      ><Icon name={MODE_ICONS[st.mode]} size={12} />{pillLabel}<Icon
        name="chevron-down"
        size={12}
        extraClass="autonomy-pill__chev"
      /></button
    >

    {#if open}
      <div
        class="autonomy-popover"
        data-testid="autonomy-popover"
        role="dialog"
        aria-label="Autonomy mode"
        style={popoverStyle}
        transition:slide={{ duration: motionMs(160) }}
      >
        <!-- Three-segment control -->
        <div
          class="autonomy-segments"
          class:autonomy-segments--measured={pillMeasured}
          role="group"
          aria-label="Autonomy mode"
          bind:this={segmentsEl}
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
              class:autonomy-segment--active={mode === st.mode}
              class:autonomy-segment--armed={confirming && pendingMode === mode}
              type="button"
              data-testid="autonomy-mode-{mode}"
              aria-pressed={mode === st.mode ? 'true' : 'false'}
              disabled={saving}
              onclick={() => onSegmentClick(mode)}
              bind:this={segmentEls[i]}
              ><Icon name={MODE_ICONS[mode]} size={14} />{MODE_LABELS[mode]}</button
            >
          {/each}
        </div>

        <!-- Current mode (committed, NOT pendingMode) + blurb -->
        <div class="autonomy-mode-summary">
          <p class="autonomy-current" data-testid="autonomy-current">
            <span class="autonomy-current__label">Current</span>
            <span class="autonomy-current__sep" aria-hidden="true">·</span>
            <span class="autonomy-current__mode" data-testid="autonomy-current-mode"
              >{MODE_LABELS[st.mode]}</span
            >
          </p>
          <p class="autonomy-blurb">{MODE_BLURBS[st.mode]}</p>
        </div>

        <!-- Progressive-disclosure explainer, collapsed by default -->
        <div class="autonomy-explainer">
          <button
            class="autonomy-explainer__toggle"
            class:autonomy-explainer__toggle--open={explainerOpen}
            type="button"
            data-testid="autonomy-explainer-toggle"
            aria-expanded={explainerOpen}
            onclick={() => (explainerOpen = !explainerOpen)}
            ><Icon name="chevron-down" size={14} extraClass="autonomy-explainer__chev" />{AUTONOMY_EXPLAINER_HEADING}</button
          >
          {#if explainerOpen}
            <p
              class="autonomy-explainer__body"
              data-testid="autonomy-explainer-body"
              transition:slide={{ duration: motionMs(200) }}
            >{AUTONOMY_EXPLAINER_BODY}</p>
          {/if}
        </div>

        <!-- Meta line: actor · time · reason, OR the read_error warning -->
        {#if st.readError}
          <span class="autonomy-meta__warn" data-testid="autonomy-read-error"
            >autonomy state could not be read, failing closed to Observe</span
          >
        {:else}
          <div class="autonomy-meta">
            {#if st.actor}
              <span class="autonomy-meta__label">Set by</span>
              <span class="autonomy-meta__actor">{st.actor}</span>
            {/if}
            {#if st.updatedAt}
              <time class="autonomy-meta__time" datetime={st.updatedAt}>{fmtUpdatedAt(st.updatedAt)}</time>
            {/if}
            {#if st.reason}
              <span class="autonomy-meta__label">reason:</span>
              <span class="autonomy-meta__reason">{st.reason}</span>
            {/if}
          </div>
        {/if}

        <!-- Confirm row (appears when a different segment is armed) -->
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
                bind:this={reasonEl}
                bind:value={reasonInput}
                disabled={saving}
              />
              <button
                class="ds-btn ds-btn--primary autonomy-confirm-btn"
                type="button"
                data-testid="autonomy-confirm"
                onclick={() => void onConfirm()}
                disabled={saving}><Icon name="check" size={14} />{confirmLabel}</button
              >
              <button
                class="ds-btn ds-btn--ghost autonomy-cancel-btn"
                type="button"
                data-testid="autonomy-cancel"
                onclick={onCancelArm}
                disabled={saving}><Icon name="x" size={14} />Cancel</button
              >
            </div>
          </div>
        {/if}

        {#if postError}
          <p class="autonomy-error" data-testid="autonomy-error"
            >Could not save. Autonomy state unchanged. Please try again.</p
          >
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .autonomy-pill {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  /* The pill button — strip native chrome, keep the .ds-pill look. */
  .autonomy-pill__btn {
    appearance: none;
    font-family: inherit;
    cursor: pointer;
    transition:
      background var(--ds-dur-fast) var(--ds-ease),
      border-color var(--ds-dur-fast) var(--ds-ease);
  }
  .autonomy-pill__btn:hover {
    border-color: var(--ds-border-strong);
  }
  .autonomy-pill__btn :global(.autonomy-pill__chev) {
    transition: transform var(--ds-dur-fast) var(--ds-ease);
    opacity: 0.7;
  }
  .autonomy-pill__btn--open :global(.autonomy-pill__chev) {
    transform: rotate(180deg);
  }
  .autonomy-pill__dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--ds-muted);
    flex-shrink: 0;
  }

  /* ---- Popover ---- */
  .autonomy-popover {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    width: min(24rem, 92vw);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius);
    border: 1px solid var(--ds-border);
    background: var(--ds-surface);
    box-shadow: var(--ds-shadow-md, var(--ds-shadow-sm));
    overflow: hidden;
    text-align: left;
  }
  /* NB: the actual position (position:fixed; top/right/width) is set inline by
     positionPopover() on open — it is viewport-anchored + clamped on-screen
     because the pill is the left-most action and the header wraps. The absolute
     right:0 above is only a no-JS fallback. */

  /* ---- Three-segment control (ported from AutonomyControl; segments stretch to
       fill the narrower popover so they never overflow) ---- */
  .autonomy-segments {
    display: flex;
    gap: 0;
    width: 100%;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    overflow: hidden;
    position: relative;
  }
  .autonomy-segments__pill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    background: var(--ds-stream-surface);
    border-radius: inherit;
    z-index: 0;
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
    flex: 1 1 0;
    padding: 0.35em 0.5em;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-muted);
    cursor: pointer;
    border-right: 1px solid var(--ds-border-strong);
    position: relative;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.35em;
    white-space: nowrap;
    transition:
      background-color var(--ds-dur-fast) var(--ds-ease),
      color var(--ds-dur-fast) var(--ds-ease);
  }
  .autonomy-segment:last-child {
    border-right: none;
  }
  .autonomy-segment:not(.autonomy-segment--active):not(.autonomy-segment--armed):not(:disabled):hover {
    background: var(--ds-surface-2);
    color: var(--ds-fg);
  }
  .autonomy-segment--active {
    background: var(--ds-stream-surface);
    color: var(--ds-stream-ink);
    cursor: default;
  }
  .autonomy-segments--measured .autonomy-segment--active {
    background: transparent;
    color: var(--ds-stream-ink);
  }
  .autonomy-segment--armed {
    box-shadow: inset 0 0 0 1px var(--ds-stream);
    background: color-mix(in srgb, var(--ds-stream-surface) 60%, transparent);
    color: var(--ds-stream-ink);
  }
  .autonomy-segments--measured .autonomy-segment--armed {
    background: color-mix(in srgb, var(--ds-stream-surface) 60%, transparent);
    color: var(--ds-stream-ink);
  }
  .autonomy-segment:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  /* ---- Current-mode caption + blurb ---- */
  .autonomy-mode-summary {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .autonomy-current {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: 0.4em;
    font-size: var(--ds-fs-1);
  }
  .autonomy-current__label,
  .autonomy-current__sep {
    color: var(--ds-muted);
  }
  .autonomy-current__mode {
    color: var(--ds-fg);
    font-weight: var(--ds-fw-semibold);
  }
  .autonomy-blurb {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  /* ---- Progressive-disclosure explainer ---- */
  .autonomy-explainer {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
  }
  .autonomy-explainer__toggle {
    appearance: none;
    border: none;
    background: none;
    padding: 0;
    width: fit-content;
    display: inline-flex;
    align-items: center;
    gap: 0.35em;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    cursor: pointer;
    text-align: left;
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .autonomy-explainer__toggle:hover {
    color: var(--ds-fg);
  }
  .autonomy-explainer__toggle :global(.autonomy-explainer__chev) {
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .autonomy-explainer__toggle--open :global(.autonomy-explainer__chev) {
    transform: rotate(180deg);
  }
  .autonomy-explainer__body {
    margin: 0;
    font-size: var(--ds-fs-1);
    line-height: 1.5;
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
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.6em;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg);
  }
  .autonomy-confirm-btn,
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
