<script lang="ts">
  import type { TokenState } from '../lib/api';

  // Operator auth indicator (plan Appendix B). Renders #token-status as a
  // ds-pill whose label + variant map off the current TokenState, followed by a
  // subtle text-style #change-token-btn that re-opens the AuthPanel via onChange.
  let {
    state,
    onChange,
  }: {
    state: TokenState;
    onChange: () => void;
  } = $props();

  // state → {label, pillClass} per the hard contract. Kept as a derived lookup
  // so the pill and its aria-live announcement stay in lockstep with `state`.
  const VARIANTS: Record<TokenState, { label: string; pillClass: string }> = {
    ok: { label: 'token ok', pillClass: 'ds-pill--ok' },
    missing: { label: 'no token', pillClass: 'ds-pill--muted' },
    invalid: { label: 'token rejected', pillClass: 'ds-pill--danger' },
  };

  const variant = $derived(VARIANTS[state]);
</script>

<span class="token-status">
  <span id="token-status" class={'ds-pill ' + variant.pillClass} aria-live="polite"
    >{variant.label}</span
  >
  <button id="change-token-btn" type="button" class="change-token" onclick={onChange}
    >change token</button
  >
</span>

<style>
  .token-status {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }

  /* Subtle text/link-style button — quiet next to the pill, but clearly
     actionable on hover/focus. Not a .ds-btn; this is a tertiary affordance. */
  .change-token {
    appearance: none;
    border: 0;
    background: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    color: var(--ds-muted);
    line-height: 1.4;
    text-decoration: none;
    border-radius: var(--ds-radius-sm);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }

  .change-token:hover {
    color: var(--ds-stream-ink);
    text-decoration: underline;
    text-underline-offset: 0.18em;
  }

  .change-token:active {
    color: var(--ds-stream);
  }
</style>
