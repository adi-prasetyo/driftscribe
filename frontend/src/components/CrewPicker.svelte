<script module lang="ts">
  // Native radios group by form-owner + name, NOT component boundary — two
  // pickers in one form would cross-wire if they shared a literal name. Each
  // instance takes a unique group name from this module counter (Codex review
  // 019ed108). `$props.id()` would do this too but needs Svelte ≥5.20; the repo
  // pins ^5.19, so a counter keeps it version-safe.
  let _pickerSeq = 0;
  function nextPickerId(): number {
    _pickerSeq += 1;
    return _pickerSeq;
  }
</script>

<script lang="ts">
  /**
   * CrewPicker — the chat-composer workload selector, rendered as four small
   * crew cards instead of a native <select>. The SELECTED card's CrewGlyph
   * loops; the rest rest on their static frame, so the motion signals "this is
   * the active crew member" (design: docs/plans/2026-06-17-crew-picker-cards-
   * design.md). The cards are compact (glyph + name); each card's one-sentence
   * `summary` surfaces as a tooltip on hover / keyboard focus.
   *
   * Accessibility: each card is a <label> wrapping a visually-hidden native
   * <input type="radio">, so keyboard arrow-nav + screen-reader semantics come
   * for free (what the old <select> gave). The card shows a focus ring via
   * :has(input:focus-visible). The summary tooltip is wired as the radio's
   * `aria-describedby`, so assistive tech announces e.g. "Anchor, Detects
   * drift between a Cloud Run service's live env vars…" regardless of hover
   * state. Autonomy is intentionally NOT signalled here — it lives in the
   * "what this agent can — and cannot — do" card.
   *
   * `value` is the frozen symbolic workload (drift/upgrade/explore/provision);
   * the /chat contract is unchanged.
   */
  import { WORKLOADS, type Workload } from '../lib/workloads';
  import CrewGlyph from './CrewGlyph.svelte';

  let {
    value = $bindable(),
    disabled = false,
  }: {
    value: Workload;
    disabled?: boolean;
  } = $props();

  const groupName = `crew-${nextPickerId()}`;
</script>

<div class="crew-picker" class:historical={disabled}>
  <fieldset class="crew-picker__group" {disabled}>
    <legend class="crew-sr-only">Choose a crew member</legend>
    {#each WORKLOADS as wl (wl.value)}
      {@const nameId = `${groupName}-${wl.value}-name`}
      {@const hintId = `${groupName}-${wl.value}-hint`}
      <label
        class="crew-card"
        class:crew-card--selected={value === wl.value}
        data-testid="crew-card-{wl.value}"
      >
        <input
          class="crew-sr-only crew-card__radio"
          type="radio"
          name={groupName}
          value={wl.value}
          bind:group={value}
          aria-labelledby={nameId}
          aria-describedby={hintId}
          {disabled}
        />
        <CrewGlyph verb={wl.value} animated={value === wl.value && !disabled} size={26} />
        <!-- aria-labelledby pins the accessible NAME to just the crew name, so
             the descriptor (also inside the label) isn't duplicated into the
             name AND the description (Codex review 019ed108). -->
        <span class="crew-card__name" id={nameId}>{wl.name}</span>
        <!-- One-sentence summary — a tooltip on hover/focus, and the radio's
             accessible DESCRIPTION via aria-describedby. -->
        <span class="crew-card__hint" id={hintId} role="tooltip">{wl.summary}</span>
      </label>
    {/each}
  </fieldset>
</div>

<style>
  .crew-picker {
    display: flex;
    width: 100%;
  }

  /* fieldset reset — it's a layout row, not a boxed group. */
  .crew-picker__group {
    flex: 1 1 auto;
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: var(--ds-sp-2);
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }

  .crew-card {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-1);
    padding: 0.32em 0.62em 0.32em 0.42em;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg-soft);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    cursor: pointer;
    user-select: none;
    transition:
      border-color var(--ds-dur-fast) var(--ds-ease),
      background var(--ds-dur-fast) var(--ds-ease),
      color var(--ds-dur-fast) var(--ds-ease);
  }
  .crew-card:hover {
    border-color: var(--ds-muted);
  }
  /* The hidden radio is the focus target — surface a ring on the card so
     keyboard users can see where they are (Codex review 019ed108). */
  .crew-card:has(.crew-card__radio:focus-visible) {
    border-color: var(--ds-stream-border);
    box-shadow: var(--ds-ring);
  }
  /* Selected: keep the white well (same as the unselected cards) and mark the
     choice with a thin blue border + slightly stronger label; the looping glyph
     is the other selected signal. */
  .crew-card--selected {
    border-color: var(--ds-stream);
    background: var(--ds-surface);
    color: var(--ds-fg);
  }

  .crew-card__name {
    white-space: nowrap;
  }

  /* Summary tooltip — hidden until hover or keyboard focus, floating above
     the card. Decorative-position only; the text is also the radio's
     aria-describedby, so assistive tech gets it without hover. The summary is
     a full sentence, so it WRAPS (white-space: normal) inside a capped width —
     min() keeps it from clipping the viewport edge on a narrow screen. */
  .crew-card__hint {
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%) translateY(3px);
    width: max-content;
    max-width: min(20rem, 78vw);
    white-space: normal;
    text-align: left;
    line-height: 1.4;
    padding: 0.4em 0.6em;
    font-size: var(--ds-fs-1);
    font-weight: 400;
    color: var(--ds-fg-soft);
    background: var(--ds-surface);
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    box-shadow: var(--ds-shadow-sm);
    opacity: 0;
    pointer-events: none;
    transition:
      opacity var(--ds-dur-fast) var(--ds-ease),
      transform var(--ds-dur-fast) var(--ds-ease);
    z-index: 5;
  }
  .crew-card:hover .crew-card__hint,
  .crew-card:has(.crew-card__radio:focus-visible) .crew-card__hint {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }

  /* Disabled (historical replay): inert. Parent .chat-form opacity dims; we
     just kill the affordance and suppress the tooltip. */
  .crew-picker.historical .crew-card {
    cursor: not-allowed;
  }
  .crew-picker.historical .crew-card__hint {
    display: none;
  }

  /* Visually-hidden helper (matches the ReplyPending sr-only pattern). */
  .crew-sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    border: 0;
    white-space: nowrap;
    clip: rect(0 0 0 0);
    clip-path: inset(50%);
  }

  /* Narrow composers: let the cards share the row evenly above the input. */
  @media (max-width: 30rem) {
    .crew-card {
      flex: 1 1 auto;
      justify-content: center;
      /* Re-anchor the tooltip to the full-width picker instead of the card
         (below): a one-sentence tooltip centered on an EDGE card would clip
         the viewport here (and the cards can wrap to two rows, so :first/
         :last-child can't reliably pick the edges). Making the card static
         lets the hint resolve to .crew-picker. */
      position: static;
    }
    .crew-picker {
      position: relative;
    }
    /* Centered over the full-width picker + capped to the viewport, so it
       stays on-screen whichever card is hovered. */
    .crew-card__hint {
      max-width: 92vw;
    }
  }
</style>
