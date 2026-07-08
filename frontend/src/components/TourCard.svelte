<!-- frontend/src/components/TourCard.svelte -->
<script lang="ts">
  // TourCard — the docked, NON-modal step card for the onboarding tour
  // (roadmap item 14). It spotlights the REAL panels (scrollIntoView + a
  // .tour-spotlight outline on the matching [data-tour] element) instead of
  // re-rendering their data in a modal — the operator learns the actual UI.
  //
  // The adopt step routes through the SAME prefill bridge as the panel's
  // Adopt buttons (App.handleAdopt): prefilled, NEVER auto-sent (T4), and
  // disabled under the same chatDisabled condition.
  import {
    TOUR_STEPS,
    welcomeLine,
    estateLine,
    CONTROLS_LINE,
    NEXT_LINE,
    adoptStepState,
  } from '../lib/tour';
  import type { InfraGraph, PendingApproval } from '../lib/infra_graph';

  let {
    graph = null,
    pendingApprovals = [],
    adoptDisabled = false,
    onAdoptPrefill,
    onClose,
  }: {
    /** Lifted /infra/graph payload (InfraDiagram.onGraph); null until loaded. */
    graph?: InfraGraph | null;
    /**
     * Lifted /infra/pending-approvals list (InfraDiagram.onPending): the same
     * open-adoption-PR list the panel shows. Lets the adopt step skip a
     * resource that already has a PR in review instead of suggesting a
     * duplicate adoption. Empty until loaded / on any fetch error.
     */
    pendingApprovals?: PendingApproval[];
    /** Same condition that disables ChatForm/Adopt (busy / historical replay). */
    adoptDisabled?: boolean;
    /** Routes through App.handleAdopt — prefills the composer, never sends. */
    onAdoptPrefill?: (prefill: string) => void;
    /** Close/Finish — App marks the tour done and unmounts this card. */
    onClose?: () => void;
  } = $props();

  let stepIndex = $state(0);
  const step = $derived(TOUR_STEPS[stepIndex]);
  const adoptState = $derived(adoptStepState(graph, pendingApprovals));

  // Spotlight the current step's target: toggle .tour-spotlight on the
  // matching [data-tour] element and scroll it into view. The effect cleanup
  // removes the class on step change and on unmount, so a closed tour never
  // leaves an outline behind.
  $effect(() => {
    const target = step.target;
    if (target === null) return;
    const el = document.querySelector(`[data-tour="${target}"]`);
    if (!(el instanceof HTMLElement)) return;
    el.classList.add('tour-spotlight');
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return () => el.classList.remove('tour-spotlight');
  });

  function next(): void {
    if (stepIndex < TOUR_STEPS.length - 1) stepIndex += 1;
  }
  function back(): void {
    if (stepIndex > 0) stepIndex -= 1;
  }
  function prefillAdopt(): void {
    if (adoptDisabled || adoptState.kind !== 'target') return;
    onAdoptPrefill?.(adoptState.prefill);
    next(); // flow straight into "what happens next" with the composer spotlit
  }
</script>

<aside class="ds-card tour-card" data-testid="tour-card" aria-label="Guided tour">
  <header class="tour-card__head">
    <span class="ds-label tour-card__title">{step.title}</span>
    <span class="ds-subtle tour-card__progress" data-testid="tour-progress"
      >{stepIndex + 1} of {TOUR_STEPS.length}</span
    >
    <button
      class="ds-btn ds-btn--ghost tour-card__close"
      type="button"
      aria-label="Close tour"
      data-testid="tour-close"
      onclick={() => onClose?.()}>×</button
    >
  </header>

  <p class="tour-card__body" data-testid="tour-body" aria-live="polite">
    {#if step.id === 'welcome'}{welcomeLine(graph)}
    {:else if step.id === 'estate'}{estateLine(graph)}
    {:else if step.id === 'controls'}{CONTROLS_LINE}
    {:else if step.id === 'adopt'}{adoptState.line}
    {:else}{NEXT_LINE}{/if}
  </p>

  {#if step.id === 'adopt' && adoptState.kind === 'target'}
    <div class="tour-card__action">
      <button
        class="ds-btn ds-btn--approve"
        type="button"
        data-testid="tour-adopt-btn"
        disabled={adoptDisabled}
        title={adoptDisabled
          ? 'Unavailable while the chat is busy or reviewing a past trace.'
          : undefined}
        onclick={prefillAdopt}>Prefill the request</button
      >
      <p class="ds-subtle tour-card__note">
        This only prefills the chat. Nothing is sent until you press Send.
      </p>
    </div>
  {/if}

  {#if step.id === 'next' && adoptDisabled}
    <!-- Honesty (Codex MF3): the copy says "when you send" but Send is
         disabled right now (busy stream / historical replay) — say so. -->
    <p class="ds-subtle tour-card__note" data-testid="tour-busy-note">
      The chat is busy or showing a past trace right now, so sending becomes
      available when it finishes.
    </p>
  {/if}

  <footer class="tour-card__nav">
    <button
      class="ds-btn ds-btn--ghost"
      type="button"
      data-testid="tour-back"
      disabled={stepIndex === 0}
      onclick={back}>Back</button
    >
    {#if stepIndex < TOUR_STEPS.length - 1}
      <button class="ds-btn" type="button" data-testid="tour-next" onclick={next}
        >Next</button
      >
    {:else}
      <button class="ds-btn" type="button" data-testid="tour-finish" onclick={() => onClose?.()}
        >Finish</button
      >
    {/if}
  </footer>
</aside>

<style>
  .tour-card {
    position: fixed;
    right: var(--ds-sp-4);
    bottom: var(--ds-sp-4);
    z-index: 50; /* below the AuthPanel modal (100) — auth always wins */
    width: min(360px, calc(100vw - 2 * var(--ds-sp-4)));
    padding: var(--ds-sp-4) var(--ds-sp-5);
    box-shadow: var(--ds-shadow-lg);
  }
  .tour-card__head {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }
  .tour-card__title {
    flex: 1 1 auto;
    margin: 0;
  }
  .tour-card__progress {
    flex: none;
    font-variant-numeric: tabular-nums;
  }
  .tour-card__close {
    flex: none;
    padding: 0 0.5em;
    font-size: var(--ds-fs-2);
    line-height: 1.4;
  }
  .tour-card__body {
    margin: var(--ds-sp-3) 0;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg-soft);
  }
  .tour-card__action {
    margin: 0 0 var(--ds-sp-3);
  }
  .tour-card__note {
    margin: var(--ds-sp-2) 0 0;
  }
  .tour-card__nav {
    display: flex;
    justify-content: space-between;
    gap: var(--ds-sp-3);
  }

  /* The spotlight outline lives here (:global — it lands on App-owned
     wrappers). ds-ok tokens: confidence-green, matching the Start-here chip. */
  :global(.tour-spotlight) {
    outline: 2px solid var(--ds-ok);
    outline-offset: 3px;
    border-radius: var(--ds-radius);
  }
</style>
