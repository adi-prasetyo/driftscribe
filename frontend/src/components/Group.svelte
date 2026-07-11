<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { GroupKey } from '../lib/timeline';
  import type { IconName } from '../lib/icons';
  import Icon from './Icon.svelte';
  import { t } from '../lib/i18n';

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
     * Optional explanatory hover-help. When set, a small help-circle icon is
     * rendered next to the title with this text as its tooltip + aria-label.
     * Supplementary operator hint (hover/SR), not focus/touch-robust help.
     */
    hint?: string;
    children?: Snippet;
  } = $props();
</script>

<details id={`group-${key}`} class="group" {open}>
  <summary class="group__summary">
    <span class="group__title">{#if icon}<Icon name={icon} size={14} extraClass="group__title-icon" />{/if}{title}{#if hint}<span class="group__hint" title={hint} aria-label={hint} role="img"><Icon name="help-circle" size={13} /></span>{/if}</span>
    {#if count > 0}
      <span class="ds-pill ds-pill--muted group__count">{count}</span>
    {/if}
  </summary>
  <div class="events" data-group={key}>
    {#if empty}
      <p class="group__empty">{$t('misc.group.emptyState', { title: title.toLowerCase() })}</p>
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
</style>
