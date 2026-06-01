<script lang="ts">
  // FinalResponse — the hero reply card.
  //
  // Contract (Appendix B of the UI-refresh plan): a single
  //   <section id="final-response-card" data-testid="final-response"
  //            aria-live="polite" class="ds-card" class:error hidden={reply==null}>
  // The Playwright e2e (transparency.spec.ts:43) waits for
  // [data-testid="final-response"] to become *visible*, which only happens once
  // `reply` lands and the bound `hidden` attribute drops. Until then the card is
  // absent from layout entirely.
  //
  // Visual: this is the page hero. A subtle left accent border — green for a
  // normal coordinator reply, red when `isError` — plus generous padding and a
  // small uppercase eyebrow label. The reply body preserves newlines
  // (white-space: pre-wrap) and uses the humanist body font for prose, switching
  // to the reserved monospace face only when the payload looks like JSON.

  let {
    reply,
    isError = false,
  }: {
    reply: string | null;
    isError?: boolean;
  } = $props();

  // Heuristic: does the reply look like a JSON object/array? Only then do we
  // switch to the monospace face (tokens.css reserves mono for code/diffs/ids).
  // Trim first so leading whitespace/newlines don't defeat the bracket check.
  const looksLikeJson = $derived.by(() => {
    if (reply == null) return false;
    const t = reply.trim();
    if (t.length < 2) return false;
    const first = t[0];
    const last = t[t.length - 1];
    if (!((first === '{' && last === '}') || (first === '[' && last === ']'))) {
      return false;
    }
    try {
      JSON.parse(t);
      return true;
    } catch {
      return false;
    }
  });

  const heading = $derived(isError ? 'Error' : 'Coordinator reply');
</script>

<section
  id="final-response-card"
  data-testid="final-response"
  aria-live="polite"
  class="ds-card"
  class:error={isError}
  hidden={reply == null}
>
  {#if reply != null}
    <p class="ds-label final__label" class:final__label--error={isError}>
      {heading}
    </p>
    <div class="final__body" class:final__body--json={looksLikeJson}>{reply}</div>
  {/if}
</section>

<style>
  /* Hero treatment over the shared .ds-card surface: a slightly larger,
     warmer presence with a semantic left accent rail. */
  #final-response-card {
    position: relative;
    border-left: 3px solid var(--ds-ok);
    padding: var(--ds-sp-5) var(--ds-sp-6);
    box-shadow: var(--ds-shadow);
  }

  /* A faint top-of-rail glow so the hero reads as "the answer," not just
     another card. Green normally, red when .error (below). */
  #final-response-card::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: linear-gradient(
      to bottom,
      var(--ds-ok),
      color-mix(in srgb, var(--ds-ok) 35%, transparent)
    );
    border-top-left-radius: var(--ds-radius);
    border-bottom-left-radius: var(--ds-radius);
  }

  /* The bound `hidden` attribute must actually remove the card from layout;
     .ds-card sets display:block via the cascade, so re-assert none here. */
  #final-response-card[hidden] {
    display: none;
  }

  .final__label {
    display: block;
    margin: 0 0 var(--ds-sp-3);
    color: var(--ds-ok-ink);
  }

  .final__body {
    font-family: var(--ds-font);
    font-size: var(--ds-fs-3);
    line-height: var(--ds-lh-body);
    color: var(--ds-fg);
    /* The hero must honor the agent's own line breaks. */
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-width: var(--ds-measure);
  }

  /* JSON / structured payloads read as code, in the reserved mono face, on the
     sunken inset well so they stand apart from prose. */
  .final__body--json {
    font-family: var(--ds-font-mono);
    font-size: var(--ds-fs-2);
    color: var(--ds-fg-soft);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    padding: var(--ds-sp-4);
    overflow-x: auto;
    max-width: none;
  }

  /* --- Error variant: swap the green accent for red ---------------------- */
  #final-response-card.error {
    border-left-color: var(--ds-danger);
    background: var(--ds-danger-surface);
  }

  #final-response-card.error::before {
    background: linear-gradient(
      to bottom,
      var(--ds-danger),
      color-mix(in srgb, var(--ds-danger) 35%, transparent)
    );
  }

  .final__label--error {
    color: var(--ds-danger-ink);
  }

  #final-response-card.error .final__body {
    color: var(--ds-danger-ink);
  }
</style>
