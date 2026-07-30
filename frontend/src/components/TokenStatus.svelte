<script lang="ts">
  import type { TokenState } from '../lib/api';
  import Icon from './Icon.svelte';
  import { t, type MessageKey } from '../lib/i18n';

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

  // state → {key, pillClass, quiet} per the hard contract. Kept as a derived
  // lookup so the pill and its aria-live announcement stay in lockstep with
  // `state`. Built once at module eval, so it holds semantic ids (not translated
  // text) — the label itself is resolved reactively at render via `$t`.
  //
  // ds-7ag.3: `ok` is `quiet`. A green filled chip made "your token is fine" as
  // loud as a problem, and the header carried five other filled chips saying
  // equally little. Problems SHOULD be loud, so missing/invalid keep today's
  // emphasis; the healthy state steps back. The decision lives HERE rather than
  // as a branch in the markup, so all three states are readable in one place.
  const VARIANTS: Record<TokenState, { key: MessageKey; pillClass: string; quiet?: true }> = {
    ok: { key: 'auth.status.ok', pillClass: '', quiet: true },
    missing: { key: 'auth.status.missing', pillClass: 'ds-pill--muted' },
    invalid: { key: 'auth.status.invalid', pillClass: 'ds-pill--danger' },
  };

  const variant = $derived(VARIANTS[state]);
  const label = $derived($t(variant.key));
</script>

<span class="token-status">
  <!-- class:quiet as a DIRECTIVE, not concatenated into the class string: the
       compiler has to see the class literally or it prunes the scoped rule for
       it as unused. -->
  <span
    id="token-status"
    class={'ds-pill ' + variant.pillClass}
    class:token-status__quiet={variant.quiet}
    aria-live="polite"><Icon name="key-round" size={12} />{label}</span
  >
  <button id="change-token-btn" type="button" class="change-token" onclick={onChange}
    >{$t('auth.status.changeToken')}</button
  >
</span>

<style>
  .token-status {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }

  /* The quiet healthy state (ds-7ag.3). No fill, no border, muted label — and
     the key icon the component already renders becomes the status signal,
     re-inked --ds-ok. It IS the dot, so no separate dot is added. */
  .token-status__quiet {
    background: transparent;
    border-color: transparent;
    color: var(--ds-muted);
    font-weight: var(--ds-fw-medium);
    padding-inline: 0;
  }
  .token-status__quiet :global(svg) {
    color: var(--ds-ok);
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
