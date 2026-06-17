<script module lang="ts">
  // Unique id per instance to wire the button's aria-controls to its panel.
  // `$props.id()` would do this but needs Svelte ≥5.20; the repo pins ^5.19, so
  // a module counter keeps it version-safe (same approach as CrewPicker).
  let _hintSeq = 0;
  function nextHintId(): number {
    _hintSeq += 1;
    return _hintSeq;
  }
</script>

<script lang="ts">
  /**
   * HelpHint — a small focusable help-circle that reveals an explanatory `text`.
   * A real <button> (keyboard- AND touch-reachable, unlike Group.svelte's
   * title-only span) that TOGGLES an inline help panel. The panel flows in
   * normal layout — it grows the card, exactly like the rail's lifecycle
   * expander — so it can NEVER be clipped by the rail's `overflow-y:auto`, the
   * failure mode a floating CSS tooltip hits in this narrow scroll container.
   * `title` gives mouse users an instant hover peek; click / Enter / tap toggles
   * the panel for touch + keyboard; aria-expanded + aria-controls + role="note"
   * wire it for assistive tech. Place this at the END of its line so the opened
   * block panel breaks cleanly onto its own row below.
   */
  import Icon from './Icon.svelte';

  let { text }: { text: string } = $props();
  let open = $state(false);
  const panelId = `help-hint-${nextHintId()}`;
</script>

<button
  type="button"
  class="help-hint__btn"
  aria-label="Explain this status"
  aria-expanded={open}
  aria-controls={panelId}
  title={text}
  data-testid="status-help"
  onclick={() => (open = !open)}
>
  <Icon name="help-circle" size={13} />
</button>
{#if open}
  <span class="help-hint__panel" id={panelId} role="note" data-testid="status-help-panel"
    >{text}</span>
{/if}

<style>
  .help-hint__btn {
    appearance: none;
    border: none;
    background: none;
    padding: 0;
    margin: 0 0 0 0.3em;
    display: inline-flex;
    align-items: center;
    vertical-align: middle;
    color: var(--ds-faint);
    cursor: help;
    border-radius: var(--ds-radius-sm);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .help-hint__btn:hover {
    color: var(--ds-muted);
  }
  .help-hint__btn[aria-expanded='true'] {
    color: var(--ds-stream-ink);
  }
  .help-hint__btn:focus-visible {
    outline: none;
    color: var(--ds-stream-ink);
    box-shadow: var(--ds-ring);
  }

  /* Inline panel — normal flow (NOT absolute), so it is clip-proof inside the
     rail's overflow. flex-basis:100% makes it take its own line inside the flex
     lifecycle-step; display:block does the same inside the <p> meta line. */
  .help-hint__panel {
    display: block;
    flex-basis: 100%;
    width: 100%;
    margin-top: var(--ds-sp-2);
    padding: 0.5em 0.65em;
    font-size: var(--ds-fs-1);
    font-weight: 400;
    line-height: 1.45;
    color: var(--ds-fg-soft);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
  }
</style>
