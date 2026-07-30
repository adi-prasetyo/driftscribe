<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { GroupKey } from '../lib/timeline';
  import type { IconName } from '../lib/icons';
  import Icon from './Icon.svelte';
  import { t, locale } from '../lib/i18n';

  // One of the three top-level reasoning groups. MUST be a real <details> with
  // id="group-{key}" and a direct child <div class="events" data-group="{key}">
  // — the Playwright e2e sets `.open = true` on #group-tools and asserts
  // [data-group="tools"] becomes visible (Appendix B).
  let {
    key,
    title,
    icon,
    count = 0,
    open = false,
    empty = false,
    hint,
    variant = 'card',
    emptyText,
    children,
  }: {
    key: GroupKey;
    title: string;
    /** Optional decorative icon rendered before the title. */
    icon?: IconName;
    count?: number;
    open?: boolean;
    empty?: boolean;
    /**
     * Visual weight (ds-7ag.5). `card` is the original boxed look and stays the
     * default, so no existing consumer changes. `quiet` demotes the group to a
     * plain disclosure — no box when closed, a muted label row, content on a
     * well when open — for drawers that hold METADATA rather than substance.
     * The chat page had six boxes of equal weight, so nothing on it read as the
     * page's point; this is what lets the reasoning group be the one that does.
     *
     * Both variants keep the e2e DOM contract documented above (a real
     * <details id="group-{key}"> with a [data-group] child).
     */
    variant?: 'card' | 'quiet';
    /**
     * Replaces the generic "No {title} yet." empty line. Optional, and when
     * omitted the generic copy is unchanged — so a group opts IN to saying what
     * would fill it rather than every group having to author the sentence.
     */
    emptyText?: string;
    /**
     * Optional explanatory hover-help. When set, a small help-circle icon is
     * rendered next to the title with this text as its tooltip + aria-label.
     * Supplementary operator hint (hover/SR), not focus/touch-robust help.
     */
    hint?: string;
    children?: Snippet;
  } = $props();
</script>

<details id={`group-${key}`} class="group" class:group--quiet={variant === 'quiet'} {open}>
  <summary class="group__summary">
    <span class="group__title">{#if icon}<Icon name={icon} size={14} extraClass="group__title-icon" />{/if}{title}{#if hint}<span class="group__hint" title={hint} aria-label={hint} role="img"><Icon name="help-circle" size={13} /></span>{/if}</span>
    {#if count > 0}
      <span class="ds-pill ds-pill--muted group__count">{count}</span>
    {/if}
  </summary>
  <div class="events" data-group={key}>
    {#if empty}
      <!-- Lowercasing is an EN-only grammar rule (the title lands mid-sentence
           in 'No {title} yet.'); JA titles like 'MCP 通信' must pass unchanged.
           emptyText, when the consumer supplies one, is already a whole
           sentence in the active locale and interpolates nothing. -->
      <p class="group__empty">{emptyText ?? $t('misc.group.emptyState', { title: $locale === 'en' ? title.toLowerCase() : title })}</p>
    {:else}
      {@render children?.()}
    {/if}
  </div>
</details>

<style>
  .group {
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    background: var(--ds-surface);
    margin: var(--ds-sp-3) 0;
    overflow: hidden;
  }
  .group__summary {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    cursor: pointer;
    font-weight: var(--ds-fw-semibold);
    list-style: none;
    user-select: none;
  }
  .group__summary::-webkit-details-marker {
    display: none;
  }
  .group__summary::before {
    content: '▸';
    color: var(--ds-faint);
    font-size: 0.8em;
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .group[open] > .group__summary::before {
    transform: rotate(90deg);
  }
  .group__title {
    flex: 1 1 auto;
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .group__title :global(.group__title-icon) {
    color: var(--ds-muted);
  }
  .group__hint {
    display: inline-flex;
    align-items: center;
    color: var(--ds-faint);
    cursor: help;
  }
  .events {
    padding: var(--ds-sp-2) var(--ds-sp-4) var(--ds-sp-4);
    border-top: 1px solid var(--ds-border);
  }
  .group__empty {
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
    font-style: italic;
    padding: var(--ds-sp-2) 0;
  }

  /* ---- quiet variant (ds-7ag.5) -------------------------------------------
     A metadata drawer, not a card: the box goes away and a hairline above the
     summary is all that separates it from what it follows. The label row reads
     like a .ds-label, and the open content sits on a --ds-surface-2 well so the
     disclosure still has a visible body without a border around the whole
     thing. Only the reasoning group keeps the card look, which is the point —
     it is the page's substance. */
  .group--quiet {
    border: none;
    border-radius: 0;
    background: none;
    margin: 0;
  }
  .group--quiet > .group__summary {
    border-top: 1px solid var(--ds-border);
    padding: var(--ds-sp-3) 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    letter-spacing: 0.01em;
  }
  .group--quiet > .events {
    border-top: none;
    background: var(--ds-surface-2);
    border-radius: var(--ds-radius);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    margin-bottom: var(--ds-sp-3);
  }
</style>
