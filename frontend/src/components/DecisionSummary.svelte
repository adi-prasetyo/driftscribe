<script lang="ts">
  // DecisionSummary — a structured "what was decided" card for a historical
  // decision that carries no reasoning prose (e.g. an iac_apply, produced by the
  // HITL approval handler rather than the agent reasoning loop). It fills the
  // gap where FinalResponse stays hidden (no rationale/rendered_body) and the
  // Timeline is empty (no `event`-typed log entries exist for such a decision).
  //
  // It renders ONLY the safe, allowlisted rows produced by decisionFields() —
  // see lib/decision.ts for the security rationale (the /trace decision doc is
  // unredacted, so no dynamic field iteration here).

  import type { Decision } from '../lib/types';
  import { decisionFields } from '../lib/decision';

  let { decision }: { decision: Decision | null } = $props();

  const fields = $derived(decisionFields(decision));
</script>

{#if decision && fields.length > 0}
  <section class="ds-card decision-summary" data-testid="decision-summary" aria-label="Decision summary">
    <p class="ds-label decision-summary__label">Decision</p>
    <dl class="decision-summary__grid">
      {#each fields as f (f.label)}
        <div class="decision-summary__row">
          <dt class="decision-summary__key">{f.label}</dt>
          <dd class="decision-summary__val">
            {#if f.badge}
              <span class="ds-pill ds-pill--{f.badge}">{f.value}</span>
            {:else if f.code}
              <code class="ds-code" title={f.title ?? f.value}>{f.value}</code>
            {:else}
              {f.value}
            {/if}
          </dd>
        </div>
      {/each}
    </dl>
  </section>
{/if}

<style>
  .decision-summary {
    /* A calm, settled left accent — distinct from FinalResponse's hero green. */
    border-left: 3px solid var(--ds-border-strong);
    padding: var(--ds-sp-5) var(--ds-sp-6);
  }

  .decision-summary__label {
    display: block;
    margin: 0 0 var(--ds-sp-4);
    color: var(--ds-muted);
  }

  .decision-summary__grid {
    margin: 0;
    display: grid;
    gap: var(--ds-sp-3);
  }

  .decision-summary__row {
    display: grid;
    grid-template-columns: minmax(7rem, max-content) 1fr;
    align-items: baseline;
    gap: var(--ds-sp-3) var(--ds-sp-4);
  }

  .decision-summary__key {
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    font-weight: var(--ds-fw-semibold);
  }

  .decision-summary__val {
    margin: 0;
    color: var(--ds-fg);
    font-size: var(--ds-fs-2);
    overflow-wrap: anywhere;
    min-width: 0;
  }

  @media (max-width: 540px) {
    .decision-summary__row {
      grid-template-columns: 1fr;
      gap: var(--ds-sp-1);
    }
  }
</style>
