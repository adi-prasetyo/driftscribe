<script lang="ts">
  import { fly } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import {
    groupEvents,
    subKey,
    pairToolEvents,
    eventKey,
    type TraceEvent,
    type TimelineStatus,
    type GroupKey,
  } from '../lib/timeline';
  import { workerLabel } from '../lib/labels';
  import { fmtTokens, fmtPreview } from '../lib/format';
  import { motionMs } from '../lib/motion';
  import Group from './Group.svelte';
  import ApprovalCta from './ApprovalCta.svelte';

  let {
    events = [],
    status = 'pending',
  }: { events?: TraceEvent[]; status?: TimelineStatus } = $props();

  const groups = $derived(groupEvents(events));

  interface Sub {
    key: string;
    label: string;
    events: TraceEvent[];
  }

  function subgroupsOf(list: TraceEvent[]): Sub[] {
    const map = new Map<string, TraceEvent[]>();
    const order: string[] = [];
    for (const e of list) {
      const k = subKey(e);
      if (!map.has(k)) {
        map.set(k, []);
        order.push(k);
      }
      map.get(k)!.push(e);
    }
    return order.map((k) => ({ key: k, label: workerLabel(k), events: map.get(k)! }));
  }

  const toolSubs = $derived(subgroupsOf(groups.tools));
  const mcpSubs = $derived(subgroupsOf(groups.mcp));

  // Settle animation: entrances + reflow, collapsed to 0ms under reduced motion.
  const flyIn = $derived({ y: 8, duration: motionMs(260) });
  const flipDur = $derived(motionMs(260));

  const str = (v: unknown): string =>
    typeof v === 'string' ? v : v == null ? '' : String(v);
  const num = (v: unknown): number | null => (typeof v === 'number' ? v : null);

  function latencySpan(evts: TraceEvent[]): string {
    const ls = evts.map((e) => num(e.latency_ms)).filter((v): v is number => v != null);
    if (ls.length === 0) return '';
    const total = ls.reduce((a, b) => a + b, 0);
    return `${total} ms`;
  }

  function docCount(evts: TraceEvent[]): number {
    return evts.reduce((acc, e) => acc + (num(e.doc_count) ?? 0), 0);
  }

  const titleFor: Record<GroupKey, string> = {
    coordinator: 'Coordinator reasoning',
    tools: 'Tools & workers',
    mcp: 'MCP traffic',
  };
</script>

{#snippet toolPair(pair: { call?: TraceEvent; result?: TraceEvent })}
  {@const ok = pair.result ? pair.result.result_ok !== false : null}
  {@const ts = str(pair.call?.timestamp || pair.result?.timestamp)}
  <details class="event" data-insert-id={eventKey((pair.call ?? pair.result)!)}>
    <summary class="event__summary">
      <span class="event__meta">{ts}</span>
      {#if ok === true}
        <span class="pair-result-ok">ok</span>
      {:else if ok === false}
        <span class="pair-result-err">error</span>
      {:else}
        <span class="event__pending">pending</span>
      {/if}
    </summary>
    {#if pair.call}
      <div class="event__label">tool_args</div>
      <pre class="ds-pre">{JSON.stringify(pair.call.tool_args ?? {}, null, 2)}</pre>
    {/if}
    {#if pair.result}
      {#if str(pair.result.tool_name) === 'propose_rollback_tool'}
        <ApprovalCta resultPreview={str(pair.result.result_preview)} />
      {/if}
      <div class="event__label">result_preview</div>
      <pre class="ds-pre">{fmtPreview(str(pair.result.result_preview) || '(empty)')}</pre>
    {/if}
  </details>
{/snippet}

<div class="timeline">
  <Group
    key="coordinator"
    title={titleFor.coordinator}
    count={groups.coordinator.length}
    open={true}
    empty={groups.coordinator.length === 0}
  >
    {#each groups.coordinator as e (eventKey(e))}
      <div class="event-item" in:fly={flyIn} animate:flip={{ duration: flipDur }}>
        {#if e.event === 'llm_thought'}
          <div class="thought">{str(e.thought_text)}</div>
        {:else if e.event === 'llm_usage'}
          <div class="usage">
            <span class="ds-label">tokens</span>
            <span class="usage__val ds-code">{fmtTokens({ total_token_count: num(e.total_token_count) })}</span>
          </div>
        {/if}
      </div>
    {/each}
  </Group>

  <Group
    key="tools"
    title={titleFor.tools}
    count={groups.tools.length}
    empty={groups.tools.length === 0}
  >
    {#each toolSubs as sub (sub.key)}
      {@const pairs = pairToolEvents(sub.events)}
      <details class="subgroup" animate:flip={{ duration: flipDur }}>
        <summary class="subgroup__summary">
          <span class="subgroup__label">{sub.label}</span>
          <span class="ds-pill ds-pill--muted">{pairs.length} call{pairs.length === 1 ? '' : 's'}</span>
          {#if latencySpan(sub.events)}<span class="subgroup__lat">{latencySpan(sub.events)}</span>{/if}
        </summary>
        <div class="sub-events">
          {#each pairs as pair, i (eventKey((pair.call ?? pair.result)!) + ':' + i)}
            <div in:fly={flyIn}>{@render toolPair(pair)}</div>
          {/each}
        </div>
      </details>
    {/each}
  </Group>

  <Group
    key="mcp"
    title={titleFor.mcp}
    count={groups.mcp.length}
    empty={groups.mcp.length === 0}
  >
    {#each mcpSubs as sub (sub.key)}
      <details class="subgroup" animate:flip={{ duration: flipDur }}>
        <summary class="subgroup__summary">
          <span class="subgroup__label">{sub.label}</span>
          <span class="ds-pill ds-pill--muted">{sub.events.length}</span>
          {#if docCount(sub.events) > 0}<span class="subgroup__lat">{docCount(sub.events)} docs</span>{/if}
          {#if latencySpan(sub.events)}<span class="subgroup__lat">{latencySpan(sub.events)}</span>{/if}
        </summary>
        <div class="sub-events">
          {#each sub.events as e (eventKey(e))}
            <div class="mcp-row" in:fly={flyIn}>
              <span class="event__meta">{str(e.timestamp)}</span>
              <span class="mcp-row__tool ds-code">{str(e.mcp_tool || e.mcp_server)}</span>
              {#if num(e.latency_ms) != null}<span class="mcp-row__lat">{num(e.latency_ms)} ms</span>{/if}
            </div>
          {/each}
        </div>
      </details>
    {/each}
  </Group>
</div>

<style>
  .timeline {
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .event-item {
    padding: var(--ds-sp-3) 0;
    border-bottom: 1px solid var(--ds-border);
  }
  .event-item:last-child {
    border-bottom: 0;
  }
  .thought {
    white-space: pre-wrap;
    color: var(--ds-fg);
    font-size: var(--ds-fs-2);
    line-height: var(--ds-lh-body);
  }
  .usage {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    color: var(--ds-muted);
  }
  .usage__val {
    font-size: var(--ds-fs-1);
  }
  .subgroup {
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    margin: var(--ds-sp-2) 0;
    background: var(--ds-surface);
  }
  .subgroup__summary {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-2) var(--ds-sp-3);
    cursor: pointer;
    list-style: none;
  }
  .subgroup__summary::-webkit-details-marker {
    display: none;
  }
  .subgroup__label {
    flex: 1 1 auto;
    font-weight: var(--ds-fw-medium);
  }
  .subgroup__lat {
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
    font-family: var(--ds-font-mono);
  }
  .sub-events {
    padding: 0 var(--ds-sp-3) var(--ds-sp-2);
  }
  .event {
    border-top: 1px solid var(--ds-border);
    padding: var(--ds-sp-2) 0;
  }
  .event__summary {
    display: flex;
    gap: var(--ds-sp-2);
    align-items: center;
    cursor: pointer;
    font-size: var(--ds-fs-1);
  }
  .event__meta {
    color: var(--ds-faint);
    font-family: var(--ds-font-mono);
    font-size: var(--ds-fs-1);
  }
  .event__label {
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    margin-top: var(--ds-sp-2);
  }
  .event__pending {
    color: var(--ds-warn-ink);
  }
  .pair-result-ok {
    color: var(--ds-ok-ink);
    font-weight: var(--ds-fw-semibold);
  }
  .pair-result-err {
    color: var(--ds-danger-ink);
    font-weight: var(--ds-fw-semibold);
  }
  .mcp-row {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-1) 0;
    border-top: 1px solid var(--ds-border);
  }
  .mcp-row__lat {
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
    font-family: var(--ds-font-mono);
  }
</style>
