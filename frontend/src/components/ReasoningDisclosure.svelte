<script lang="ts">
  // ReasoningDisclosure — the collapsed reasoning line under a crew reply, and
  // the expand that opens its full trace (ds-jns, design §2).
  //
  // This is the product's soul moved INLINE: transparency used to live in three
  // page-level panels detached from the message that produced them, and opening
  // a past one swapped the whole column into replay mode. Here it is one muted
  // line per message that expands in place.
  //
  // It owns NO trace state — the cache does, keyed by trace id, so a live
  // stream and several expanded historical disclosures coexist without fighting
  // over one global timeline (the reason design §0 exists).
  import { deriveThoughtSubtitle, type TraceEvent } from '../lib/timeline';
  import type { TraceCache, TraceCacheEntry } from '../lib/traceCache';
  import { t } from '../lib/i18n';
  import TraceDetail from './TraceDetail.svelte';
  import Icon from './Icon.svelte';

  let {
    traceId,
    cache,
    conversationId = null,
  }: {
    traceId: string;
    cache: TraceCache;
    conversationId?: string | null;
  } = $props();

  let open = $state(false);

  // Absent until something touches this trace — a history turn's disclosure has
  // no entry until it is expanded. The blank stands in so the collapsed line
  // renders its static label instead of nothing.
  const BLANK: TraceCacheEntry = {
    events: [],
    stream: 'idle',
    enrich: 'idle',
    decision: null,
    prBody: null,
    prBodyMissing: false,
  };
  const entry = $derived($cache.get(traceId) ?? BLANK);

  const streaming = $derived(entry.stream === 'streaming');

  function latestThoughtText(events: TraceEvent[]): string | null {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.event === 'llm_thought' && typeof e.thought_text === 'string') return e.thought_text;
    }
    return null;
  }

  // The line's text, in the design's stated order: latest thought chunk →
  // static label. While streaming with nothing summarized yet it says
  // "Thinking…" rather than offering to show reasoning that does not exist —
  // Vertex can omit summaries for a whole run, so that state is not transient.
  const subtitle = $derived(
    deriveThoughtSubtitle(latestThoughtText(entry.events)) ??
      (streaming ? $t('disclosure.thinking') : $t('disclosure.showReasoning')),
  );

  async function toggle(): Promise<void> {
    open = !open;
    // Only ever asks on the way OPEN, and ensure() is itself a no-op while the
    // stream is running (the cache's contract) — the `meta` frame created the
    // entry before any event, so an early expand cannot fire a /trace fetch for
    // a trace still being produced.
    if (open) await cache.ensure(traceId);
  }
</script>

<div class="disclosure" class:is-open={open}>
  <button
    type="button"
    class="disclosure__line"
    class:is-streaming={streaming}
    data-testid="reasoning-disclosure"
    aria-expanded={open}
    aria-label={$t('disclosure.toggleAria')}
    onclick={() => void toggle()}
  >
    <Icon name="brain" size={12} extraClass="disclosure__icon" />
    <span class="disclosure__subtitle" data-testid="reasoning-subtitle">{subtitle}</span>
    {#if entry.stream === 'error'}
      <span class="disclosure__stream-error" data-testid="reasoning-stream-error"
        >{$t('disclosure.streamError')}</span>
    {/if}
    <Icon name="chevron-down" size={12} extraClass="disclosure__chevron" />
  </button>
  {#if open}
    <div class="disclosure__body">
      <TraceDetail
        {traceId}
        {entry}
        {conversationId}
        onRetry={() => void cache.retry(traceId)}
      />
    </div>
  {/if}
</div>

<style>
  .disclosure {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    margin: var(--ds-sp-1) 0 var(--ds-sp-2);
  }

  /* Quiet by default: this sits inside a reply bubble and must read as a
     footnote to the message, not as a second action. */
  .disclosure__line {
    appearance: none;
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    width: 100%;
    border: none;
    background: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    text-align: left;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    font-style: italic;
    line-height: var(--ds-lh-body);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .disclosure__line:hover {
    color: var(--ds-fg);
  }
  .disclosure__line:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
    border-radius: var(--ds-radius-sm);
  }

  .disclosure__subtitle {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* The live line pulses while the coordinator is still thinking. Under
     reduced motion it rests at the dim end rather than animating. */
  .disclosure__line.is-streaming .disclosure__subtitle {
    animation: disclosure-shimmer 1.6s var(--ds-ease) infinite;
  }
  @keyframes disclosure-shimmer {
    0%,
    100% {
      opacity: 0.55;
    }
    50% {
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .disclosure__line.is-streaming .disclosure__subtitle {
      animation: none;
      opacity: 0.75;
    }
  }

  /* One chevron, rotated — there is no chevron-up in the icon set, and adding
     a near-duplicate glyph for a state a transform already expresses would be
     a second thing to keep in step. */
  .disclosure :global(.disclosure__chevron) {
    flex-shrink: 0;
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .disclosure.is-open :global(.disclosure__chevron) {
    transform: rotate(180deg);
  }
  @media (prefers-reduced-motion: reduce) {
    .disclosure :global(.disclosure__chevron) {
      transition: none;
    }
  }

  .disclosure__stream-error {
    color: var(--ds-danger-ink);
    font-style: normal;
    flex-shrink: 0;
  }

  .disclosure__body {
    padding: var(--ds-sp-3);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-bg);
  }
</style>
