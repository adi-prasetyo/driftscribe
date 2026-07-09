<!-- frontend/src/components/DemoNoticeBell.svelte -->
<script module lang="ts">
  // DemoNoticeBell — header bell for the public judging-window notice.
  // Replaces DemoNoticeBanner (task C, 2026-07-07 demo-reset-and-notice plan):
  // same copy, same localStorage flag, but dismissal is now recoverable — the
  // bell stays in the header and reopens the notice on demand, so the
  // click-anywhere close can afford to be casual.
  //
  // DEMO_MODE lives in the Cloudflare edge worker, invisible to the SPA, so
  // there is still no server flag to gate on: the bell always renders, and the
  // whole component is deleted at close-window time (runbook), never
  // flag-hidden. This is a single-notice bell, NOT a notification center — one
  // message, no list; do not generalize while it has one tenant.
  //
  // Same key as the old banner on purpose: a visitor who already dismissed the
  // banner is not re-badged.
  export const DEMO_NOTICE_DISMISSED_KEY = 'driftscribe_demo_notice_dismissed';

  /**
   * Auto-open at boot? Only when not yet dismissed AND the visitor did not
   * arrive on an errand (?ask_pr / ?preview_pr deep links, or a ?reasoning
   * shared replay link) — same principle as shouldOfferTour (lib/tour.ts):
   * interrupting intent is worse than waiting. The unread badge still shows,
   * so nothing is lost.
   */
  export function shouldAutoOpenNotice(search: string, dismissed: boolean): boolean {
    if (dismissed) return false;
    const params = new URLSearchParams(search);
    return (
      params.get('ask_pr') === null &&
      params.get('preview_pr') === null &&
      params.get('reasoning') === null
    );
  }
</script>

<script lang="ts">
  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import { announceHeaderPopoverOpen, HEADER_POPOVER_EVENT } from '../lib/headerPopover';
  import { motionMs } from '../lib/motion';
  import Icon from './Icon.svelte';

  // search is a prop only for testability; App passes nothing and the default
  // reads window.location.search once at component initialization — before
  // App's onMount strips the intent params, same boot-decision shape as the
  // tour offer.
  let { search = window.location.search }: { search?: string } = $props();

  function readDismissed(): boolean {
    try {
      return window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY) === '1';
    } catch {
      return false;
    }
  }
  function persistDismissed(): void {
    try {
      window.localStorage.setItem(DEMO_NOTICE_DISMISSED_KEY, '1');
    } catch {
      /* best-effort — worst case the notice re-offers next visit */
    }
  }

  let dismissed = $state(readDismissed());
  let open = $state(false);
  // Whether the CURRENT open came from the user clicking the bell. Only then
  // may a close return focus to the bell; a boot auto-open was never focused,
  // so yanking focus to the bell on its close would BE the focus steal the
  // no-steal decision forbids (Codex review, blocking #1).
  let userOpened = false;

  let containerEl = $state<HTMLDivElement | null>(null);
  let toggleEl = $state<HTMLButtonElement | null>(null);

  // Viewport-anchored fixed placement, clamped on-screen — same trick and
  // constants as AutonomyPill.positionPopover (the bell is even further left
  // in the header, so pill-anchored right:0 would overflow the same way).
  let popoverStyle = $state('');
  const POPOVER_INSET = 12; // ~ var(--ds-sp-3)
  const POPOVER_MAX_W = 384; // 24rem

  function positionPopover(): void {
    if (!toggleEl || typeof window === 'undefined') return;
    const r = toggleEl.getBoundingClientRect();
    const vw = window.innerWidth;
    const width = Math.min(POPOVER_MAX_W, vw - 2 * POPOVER_INSET);
    let right = Math.max(POPOVER_INSET, vw - r.right);
    if (vw - right - width < POPOVER_INSET) {
      right = Math.max(POPOVER_INSET, vw - POPOVER_INSET - width);
    }
    popoverStyle =
      `position:fixed; top:${Math.round(r.bottom + 6)}px; right:${Math.round(right)}px; ` +
      `left:auto; bottom:auto; width:${Math.round(width)}px;`;
  }

  // ANY close counts as "seen": persist + clear the badge (operator decision —
  // casual dismissal is fine because the bell keeps the copy one click away).
  // Focus returns to the bell ONLY for user-opened closes; returnFocus=false
  // additionally for foreign/outside closes (AutonomyPill's rationale).
  function closeNotice(returnFocus = true): void {
    if (!open) return;
    open = false;
    if (!dismissed) {
      persistDismissed();
      dismissed = true;
    }
    if (returnFocus && userOpened) toggleEl?.focus();
    userOpened = false;
  }

  function openNotice(userInitiated: boolean): void {
    userOpened = userInitiated;
    positionPopover();
    open = true;
    announceHeaderPopoverOpen('notice');
  }

  function onToggle(): void {
    if (open) closeNotice();
    else openNotice(true);
  }

  function onWindowKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape' && open) closeNotice();
  }
  function onWindowPointerDown(e: PointerEvent): void {
    if (!open) return;
    if (containerEl && !containerEl.contains(e.target as Node)) closeNotice(false);
  }
  function onWindowResize(): void {
    if (open) positionPopover();
  }

  onMount(() => {
    const onForeign = (e: Event) => {
      if ((e as CustomEvent).detail?.id !== 'notice') closeNotice(false);
    };
    window.addEventListener(HEADER_POPOVER_EVENT, onForeign);
    // Boot auto-open, AFTER subscribing so a later foreign open closes us.
    // openNotice announces on the bus, which is a no-op at boot (nothing else
    // is open yet) and keeps a single open path. No focus move: the popover is
    // a passive note, keyboard users land in the app as normal.
    if (shouldAutoOpenNotice(search, dismissed)) openNotice(false);
    return () => window.removeEventListener(HEADER_POPOVER_EVENT, onForeign);
  });

  // Re-anchor on late layout shift. The boot auto-open fires before /autonomy
  // and /pause resolve; when those header pills swap their loading placeholder
  // for real content their widths change, and because the actions cluster is
  // right-aligned the bell slides left under the already-open (position:fixed)
  // popover — leaving it visibly detached on the very first visit. onWindowResize
  // does not catch this: it is a same-viewport reflow, not a resize. Observe the
  // actions row for size changes while open and re-place the popover. (Only the
  // boot auto-open is exposed to this; a bell-click open happens after the header
  // has settled.)
  $effect(() => {
    if (!open || typeof ResizeObserver === 'undefined') return;
    const row = containerEl?.closest('.app-header__actions') ?? containerEl?.parentElement;
    if (!row) return;
    const ro = new ResizeObserver(() => positionPopover());
    ro.observe(row);
    return () => ro.disconnect();
  });
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} onresize={onWindowResize} />

<div class="demo-bell" bind:this={containerEl}>
  <button
    class="ds-pill ds-pill--muted demo-bell__btn"
    class:demo-bell__btn--open={open}
    type="button"
    data-testid="demo-notice-bell"
    aria-haspopup="true"
    aria-expanded={open}
    aria-label={dismissed
      ? 'Live sandbox notice'
      : 'Live sandbox notice, 1 unread'}
    bind:this={toggleEl}
    onclick={onToggle}
  >
    <Icon name="bell" size={12} />
    {#if !dismissed}
      <span class="demo-bell__badge" data-testid="demo-notice-badge" aria-hidden="true">1</span>
    {/if}
  </button>

  {#if open}
    <div
      class="demo-bell__popover"
      data-testid="demo-notice-popover"
      role="note"
      aria-label="Live sandbox notice"
      style={popoverStyle}
      transition:slide={{ duration: motionMs(160) }}
    >
      <p class="demo-bell__lead">This is a live sandbox.</p>
      <p class="ds-subtle demo-bell__sub">
        Ask a crew to investigate drift, propose a fix, or roll back the
        payment-demo service and watch it happen. You can't break it for the
        next visitor: the service heals itself every couple of hours, the
        upgrade demo resets within a couple of hours of being fixed, and
        adoption requests are tidied away after a couple of hours so the next
        visitor gets the full demo.
      </p>
      <button
        class="ds-btn ds-btn--ghost demo-bell__dismiss"
        type="button"
        data-testid="demo-notice-dismiss"
        onclick={() => closeNotice()}>Got it</button
      >
    </div>
  {/if}
</div>

<style>
  .demo-bell {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .demo-bell__btn {
    appearance: none;
    font-family: inherit;
    cursor: pointer;
    position: relative;
    transition:
      background var(--ds-dur-fast) var(--ds-ease),
      border-color var(--ds-dur-fast) var(--ds-ease);
  }
  .demo-bell__btn:hover {
    border-color: var(--ds-border-strong);
  }
  .demo-bell__badge {
    position: absolute;
    top: -0.3rem;
    right: -0.3rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 0.85rem;
    height: 0.85rem;
    padding: 0 0.15rem;
    border-radius: 999px;
    background: var(--ds-danger);
    color: #fff;
    font-size: 0.55rem;
    font-weight: var(--ds-fw-bold);
    line-height: 1;
    pointer-events: none;
  }
  .demo-bell__popover {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    width: min(24rem, 92vw);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border-radius: var(--ds-radius);
    border: 1px solid var(--ds-border);
    border-left: 3px solid var(--ds-stream);
    background: var(--ds-surface);
    box-shadow: var(--ds-shadow-md, var(--ds-shadow-sm));
    text-align: left;
  }
  /* NB: real placement (position:fixed; top/right/width) is set inline by
     positionPopover() on open, viewport-clamped — the rule above is only a
     no-JS fallback, same as AutonomyPill. */
  .demo-bell__lead {
    margin: 0;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-bold);
  }
  .demo-bell__sub {
    margin: 0;
  }
  .demo-bell__dismiss {
    align-self: flex-end;
    font-size: var(--ds-fs-1);
    padding: 0.3em 0.85em;
  }
</style>
