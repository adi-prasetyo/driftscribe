<script lang="ts">
  // HandoffChip — the confirmation a crew's handoff suggestion is waiting on.
  //
  // This is the affordance that replaces the crew picker. The picker asked the
  // operator to know which crew they needed BEFORE typing; the chip appears
  // only after a crew has read the question and decided it belongs to a
  // sibling, and it names the concrete route in the operator's own terms.
  //
  // Presentational by construction: it holds no nonce and does no fetching.
  // Confirming starts an LLM turn and rewrites the conversation's crew, so
  // that work belongs to App, which owns runSeq, the thread, and the stream.
  //
  // Deliberately NOT auto-confirming, and deliberately naming the target crew
  // on the button: an operator-pasted log could steer a crew into proposing a
  // handoff nobody asked for, and a human reading the concrete action before
  // it runs is the mitigation the whole system leans on.
  import CrewGlyph from './CrewGlyph.svelte';
  import { crewName } from '../lib/workloads';
  import { t } from '../lib/i18n';
  import type { HandoffOffer } from '../lib/sse';

  let {
    offer,
    pending = false,
    disabled = false,
    errorText = null,
    onConfirm,
    onDecline,
  }: {
    offer: HandoffOffer;
    /** A confirm/decline POST is in flight — both buttons lock, the confirm
     *  button narrates what is happening. */
    pending?: boolean;
    /** The composer is unavailable (live stream / historical replay / resume).
     *  Confirming would start a turn, so it obeys the same gate. */
    disabled?: boolean;
    /** A refusal, already resolved to display text by the caller (it owns the
     *  status → message mapping). Null when there is nothing to report. */
    errorText?: string | null;
    onConfirm: () => void;
    onDecline: () => void;
  } = $props();

  // Optional chaining is load-bearing, not defensive habit: when the parent
  // clears its offer, Svelte can re-run this derived before the enclosing {#if}
  // tears the component down, so `offer` is briefly null on a component the
  // caller believes is already gone. crewName() already accepts undefined.
  const crews = $derived({ from: crewName(offer?.from), to: crewName(offer?.to) });
  const locked = $derived(pending || disabled);
</script>

<section
  class="handoff-chip"
  data-testid="handoff-chip"
  aria-label={$t('conversations.handoff.ariaLabel')}
>
  <p class="handoff-chip__head">
    <span class="handoff-chip__glyph">
      <!-- The crew being BROUGHT IN — the chip is about where the conversation
           is going, so that crew's mark is the anchor. -->
      <CrewGlyph verb={offer?.to ?? ''} size={20} animated={false} />
    </span>
    <span class="handoff-chip__title">{$t('conversations.handoff.title', crews)}</span>
  </p>

  <!-- The crew's own justification, escaped plain text behind a quotation rule
       (same stance as the transition row and every other model-authored string
       in the thread). -->
  {#if offer?.reason}
    <p class="handoff-chip__reason" data-testid="handoff-chip-reason">{offer.reason}</p>
  {/if}

  <div class="handoff-chip__actions">
    <button
      class="ds-btn handoff-chip__confirm"
      data-testid="handoff-confirm"
      type="button"
      disabled={locked}
      onclick={onConfirm}
      >{pending
        ? $t('conversations.handoff.working', crews)
        : $t('conversations.handoff.confirm', crews)}</button>
    <!-- Declining is a real POST, not a client-side dismiss: it burns the
         proposal and records the refusal so the crew stops re-offering. That
         is why it is a button of equal weight and not an × in the corner. -->
    <button
      class="handoff-chip__decline"
      data-testid="handoff-decline"
      type="button"
      disabled={locked}
      onclick={onDecline}>{$t('conversations.handoff.decline')}</button>
  </div>

  {#if errorText}
    <!-- Assertive: the operator just clicked and needs to know the click did
         nothing. Every refusal leaves the conversation untouched. -->
    <p class="handoff-chip__error" data-testid="handoff-error" role="alert">{errorText}</p>
  {/if}
</section>

<style>
  .handoff-chip {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-3);
    background: var(--ds-stream-surface);
    border: 1px solid var(--ds-stream-border);
    border-radius: var(--ds-radius);
  }

  .handoff-chip__head {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    margin: 0;
  }

  .handoff-chip__glyph {
    display: inline-flex;
    align-items: center;
    color: var(--ds-stream-ink);
    flex-shrink: 0;
  }

  .handoff-chip__title {
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: var(--ds-lh-body);
    color: var(--ds-fg);
  }

  .handoff-chip__reason {
    margin: 0;
    padding-left: var(--ds-sp-3);
    border-left: 2px solid var(--ds-stream-border);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
    color: var(--ds-muted);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .handoff-chip__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
  }

  /* "Not now" is a real action with real consequences, but it is not the one
     being recommended — quiet, borderless, same height as the primary. */
  .handoff-chip__decline {
    appearance: none;
    border: none;
    background: none;
    padding: var(--ds-sp-1) var(--ds-sp-2);
    cursor: pointer;
    font-family: inherit;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-muted);
    border-radius: var(--ds-radius-sm);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .handoff-chip__decline:hover:not(:disabled) {
    color: var(--ds-fg);
  }
  .handoff-chip__decline:disabled,
  .handoff-chip__confirm:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .handoff-chip__error {
    margin: 0;
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
    color: var(--ds-danger-ink);
  }
</style>
