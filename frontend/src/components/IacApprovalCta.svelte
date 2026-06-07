<script lang="ts">
  import { iacApprovalHref } from '../lib/approval';

  // First-authoring approval CTA. When a /chat run just opened an infrastructure
  // PR (the `done` frame carried `iac_pr.pr_number`), the operator's final reply
  // mentions /iac-approvals/<N> only as PLAIN TEXT (FinalResponse renders no
  // markdown) and no decision row exists yet, so the DecisionsRail shows no link.
  // This surfaces a clickable, same-origin "Review & approve" link so the
  // operator can jump straight to the approval page.
  //
  // SECURITY: the href is built ONLY from the structured numeric pr_number via
  // `iacApprovalHref` — it constructs `/iac-approvals/<n>` itself, parses no URL,
  // and returns null for anything that is not a positive integer. There is no
  // host, scheme, or attacker-controlled string to smuggle. `pr_url` is
  // deliberately NOT used for the href (it stays in the reply text).
  let { prNumber }: { prNumber: unknown } = $props();

  const href = $derived(iacApprovalHref(prNumber));
</script>

{#if href}
  <div class="iac-cta" data-testid="iac-approval-cta">
    <strong class="iac-cta__title">Infra apply needs your approval — PR #{prNumber}</strong>
    <a
      class="iac-cta__btn"
      data-testid="iac-approval-cta-link"
      {href}
      target="_blank"
      rel="noopener"
    >Review &amp; approve →</a>
  </div>
{/if}

<style>
  /* Amber call-to-action card — an operator action gating real work, so it
     reads as a warm, attention-drawing surface (not an error). Mirrors
     ApprovalCta.svelte (the rollback HITL CTA) for visual consistency. */
  .iac-cta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3) var(--ds-sp-4);
    margin: var(--ds-sp-4) 0;
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-warn-surface);
    border: 1px solid var(--ds-warn-border);
    border-left: 3px solid var(--ds-warn);
    border-radius: var(--ds-radius-sm);
  }

  .iac-cta__title {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    color: var(--ds-warn-ink);
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: var(--ds-lh-snug);
  }

  .iac-cta__title::before {
    content: '';
    flex: 0 0 auto;
    width: 0.55em;
    height: 0.55em;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-warn);
    box-shadow: 0 0 0 3px var(--ds-warn-surface), 0 0 0 4px var(--ds-warn-border);
  }

  /* Mirrors the shared approve-green primary action, kept component-scoped so
     it sits correctly in the amber card. */
  .iac-cta__btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--ds-sp-2);
    flex: 0 0 auto;
    padding: 0.5em 1.15em;
    background: var(--ds-ok);
    border: 1px solid var(--ds-ok-ink);
    border-radius: var(--ds-radius-sm);
    color: #fff;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
    transition: background-color var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease),
      transform var(--ds-dur-fast) var(--ds-ease);
  }

  .iac-cta__btn:hover {
    background: var(--ds-ok-ink);
    text-decoration: none;
  }

  .iac-cta__btn:active {
    transform: translateY(1px);
  }

  @media (max-width: 32rem) {
    .iac-cta__btn {
      width: 100%;
    }
  }
</style>
