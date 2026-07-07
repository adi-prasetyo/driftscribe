<!-- frontend/src/components/TourBanner.svelte -->
<script lang="ts">
  // TourBanner — the first-visit tour offer (item 14). Shown by App only when
  // shouldOfferTour() said yes; dismissing marks the tour done (the header
  // Tour button remains the permanent reopen path).
  let {
    onStart,
    onDismiss,
  }: {
    onStart?: () => void;
    onDismiss?: () => void;
  } = $props();
</script>

<div class="ds-card tour-banner" data-testid="tour-banner" role="note">
  <div class="tour-banner__text">
    <p class="tour-banner__lead">New here? Take the 5-minute tour.</p>
    <p class="ds-subtle tour-banner__sub">
      See your estate, the controls you hold, and how to adopt your first
      resource into IaC.
    </p>
  </div>
  <div class="tour-banner__actions">
    <button
      class="ds-btn ds-btn--approve"
      type="button"
      data-testid="tour-banner-start"
      onclick={() => onStart?.()}>Start the tour</button
    >
    <button
      class="ds-btn ds-btn--ghost"
      type="button"
      data-testid="tour-banner-dismiss"
      onclick={() => onDismiss?.()}>Dismiss</button
    >
  </div>
</div>

<style>
  .tour-banner {
    display: flex;
    /* Wrap on narrow phones: without it the two buttons squeeze the text to a
       sliver at ~375px. Wrapped lines hold one item each, so the centered
       alignment only ever applies to the wide single-line case, where it is
       the shipped look against this banner's short copy. */
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-4) var(--ds-sp-5);
  }
  .tour-banner__text {
    min-width: 0;
    /* Basis below the text's natural width so the wrap decision doesn't use
       max-content: the text shares the row with the buttons (then grows) on
       desktop, and only genuinely narrow cards wrap. */
    flex: 1 1 20rem;
  }
  .tour-banner__lead {
    margin: 0;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-bold);
  }
  .tour-banner__sub {
    margin: var(--ds-sp-1) 0 0;
  }
  .tour-banner__actions {
    display: inline-flex;
    gap: var(--ds-sp-2);
    flex: none;
  }
</style>
