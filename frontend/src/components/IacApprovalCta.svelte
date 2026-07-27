<script lang="ts">
  import { iacApprovalHref } from '../lib/approval';
  import { t, locale } from '../lib/i18n';

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

  const href = $derived(iacApprovalHref(prNumber, $locale));
</script>

{#if href}
  <div class="iac-cta" data-testid="iac-approval-cta">
    <strong class="iac-cta__title"
      >{$t('approval.iacCta.title', { pr: String(prNumber) })}</strong
    >
    <a
      class="iac-cta__btn"
      data-testid="iac-approval-cta-link"
      {href}
      target="_blank"
      rel="noopener"
    >{$t('approval.iacCta.reviewApprove')}</a>
    <!-- Static cage teaser — the authoritative, drift-pinned copy of this claim
         lives server-side (BLAST_CANNOT_TOUCH_NOTE in driftscribe_lib/iac_plan_summary.py,
         rendered on the approval page). This is a teaser whose three claims —
         no control-plane changes, no IAM changes, no deletes/replacements/un-managing —
         are the denylist's stable v1 floor (enforced by iac_plan_denylist.py and
         re-checked by the apply worker before apply). No per-plan counts appear here
         because no plan exists at done-time: C2 is workflow_dispatch, so the plan is
         only created after the operator dispatches it, not at PR-open time. -->
    <p class="iac-cta__cage-note" data-testid="iac-cta-cage-note">{$t('approval.iacCta.cageNote')}</p>
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

  /* Full-width note sitting below the title + button row. flex-basis: 100%
     forces a line break inside the flex-wrap container without disrupting
     the title/button alignment above it. */
  .iac-cta__cage-note {
    flex-basis: 100%;
    margin: 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
  }

  @media (max-width: 32rem) {
    .iac-cta__btn {
      width: 100%;
    }
  }
</style>
