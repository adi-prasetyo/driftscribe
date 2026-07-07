<!-- frontend/src/components/DemoNoticeBanner.svelte -->
<script module lang="ts">
  // DemoNoticeBanner — homepage notice for the public judging window (task C,
  // 2026-07-07 demo-reset-and-notice plan). DEMO_MODE lives in the Cloudflare
  // edge worker, invisible to the SPA, so there is no server flag to gate on:
  // the banner always offers itself and lets the operator dismiss it here, in
  // this browser, for good. It is honest even after the judging window ends
  // (the claims stay true), and is removed from the code entirely at
  // close-window time rather than hidden behind a flag.
  //
  // Dismissal is a pure local preference, same shape as the tour's
  // done-flag (`lib/tour.ts`): guarded localStorage read/write so a strict
  // privacy mode can only make the banner re-offer, never throw.
  export const DEMO_NOTICE_DISMISSED_KEY = 'driftscribe_demo_notice_dismissed';
</script>

<script lang="ts">
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
      /* best-effort — worst case the banner re-offers next visit */
    }
  }

  let dismissed = $state(readDismissed());

  function onDismiss(): void {
    persistDismissed();
    dismissed = true;
  }
</script>

{#if !dismissed}
  <div class="ds-card demo-notice" data-testid="demo-notice-banner" role="note">
    <div class="demo-notice__text">
      <p class="demo-notice__lead">This is a live sandbox.</p>
      <p class="ds-subtle demo-notice__sub">
        Ask a crew to investigate drift, propose a fix, or roll back the
        payment-demo service and watch it happen. You can't break it for the
        next visitor: the service heals itself every couple of hours, and the
        upgrade demo resets every morning.
      </p>
    </div>
    <button
      class="ds-btn ds-btn--ghost demo-notice__dismiss"
      type="button"
      data-testid="demo-notice-dismiss"
      aria-label="Dismiss the live sandbox notice"
      onclick={onDismiss}>Dismiss</button
    >
  </div>
{/if}

<style>
  .demo-notice {
    display: flex;
    /* Paragraph-length copy (unlike TourBanner's one-line sub): on narrow
       phones the body runs 5+ lines, so the row must wrap (Dismiss drops
       below the paragraph) and top-align instead of centering the button
       mid-sentence. */
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-4) var(--ds-sp-5);
    /* Same left-accent language as .ds-note (base.css) — a quieter sibling of
       TourBanner's plain card, not a clone. */
    border-left: 3px solid var(--ds-stream);
  }
  .demo-notice__text {
    min-width: 0;
  }
  .demo-notice__lead {
    margin: 0;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-bold);
  }
  .demo-notice__sub {
    margin: var(--ds-sp-1) 0 0;
  }
  .demo-notice__dismiss {
    flex: none;
  }
</style>
