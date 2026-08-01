<script lang="ts">
  // TraceDetail — the EXPANDED content of one trace: the interleaved
  // thought → tool → MCP sequence plus whatever the decision doc and PR body
  // add (ds-jns, design §2). Mounted by ReasoningDisclosure under a chat reply,
  // and (PR 2) by DecisionRecord inside a desk ledger row — which is why it
  // takes a cache ENTRY rather than reaching for a store itself.
  //
  // The tool/MCP row markup is COPIED from Timeline.svelte rather than
  // imported: Timeline is a three-panel page-level component that PR 3 deletes,
  // and importing it would tie this surface's lifetime to that one. The shapes
  // are deliberately close so the two read the same while both exist.
  //
  // Escaped plain text throughout — no {@html} anywhere (thought text and tool
  // previews are model-authored). PrBodyDisclosure does its own scheme-checked
  // Markdown subset; that is its contract, not an exception to this one.
  import {
    interleaveTimeline,
    omittedThoughtTokens,
    eventKey,
    type TraceEvent,
  } from '../lib/timeline';
  import type { TraceCacheEntry } from '../lib/traceCache';
  import { workerLabel } from '../lib/labels';
  import { fmtPreview, fmtTokens } from '../lib/format';
  import { t, locale, fmtNumber } from '../lib/i18n';
  import type { Decision } from '../lib/types';
  import DriftDiffCard from './DriftDiffCard.svelte';
  import DecisionSummary from './DecisionSummary.svelte';
  import PrBodyDisclosure from './PrBodyDisclosure.svelte';
  import ApprovalCta from './ApprovalCta.svelte';
  import Icon from './Icon.svelte';

  let {
    traceId,
    entry,
    conversationId = null,
    decision = undefined,
    onRetry,
  }: {
    traceId: string;
    entry: TraceCacheEntry;
    /** Present for a chat turn; absent for a conversation-less decision record.
     *  Decides which of the two shapes the copied link takes (design §4). */
    conversationId?: string | null;
    /** The decision this trace belongs to, when the CALLER knows one the
     *  fetched trace does not carry — a desk ledger row supplies its own copy
     *  from GET /decisions (ds-jns). Omitted (not null) means "use the entry's",
     *  so passing nothing keeps the previous behaviour exactly. Without it a
     *  record could name an action in its header while the panel below it said
     *  the trace could not be loaded, about the same decision. */
    decision?: Decision | null;
    onRetry: () => void;
  } = $props();

  const doc = $derived(decision === undefined ? entry.decision : decision);

  const rows = $derived(interleaveTimeline(entry.events));

  // "Reasoned but Vertex omitted the summaries" (PR #241). Same gating rule as
  // Timeline.svelte and for the same reason: llm_usage is emitted per LLM STEP,
  // so mid-stream a multi-step run can show usage > 0 while a later step's
  // summaries are still in flight — the note must not flash and then vanish.
  // An errored stream is excluded too: the note promises the reply was
  // unaffected, which an interrupted run cannot honestly claim.
  const terminal = $derived(entry.stream === 'complete' || entry.stream === 'idle');
  const omittedTokens = $derived(terminal ? omittedThoughtTokens(entry.events) : 0);

  // An iac_apply decision is RECORDED DIRECTLY by the approval handler — it
  // legitimately has no coordinator reasoning run, so an empty timeline there
  // is expected rather than a failure to load. Every other decision's empty
  // timeline means "couldn't load", and says so.
  const directlyRecorded = $derived(doc?.action === 'iac_apply');

  /** Tokens the whole run spent, summed across its per-step `llm_usage` events.
   *
   *  interleaveTimeline drops those events (they are accounting, not a step in
   *  the story), which is right for the ROW list and left the number rendered
   *  nowhere once the page-level Timeline was deleted. It belongs in the
   *  footer with the trace id: both are facts ABOUT the run rather than parts
   *  of it. Zero/absent → null → the span self-suppresses, so a
   *  directly-recorded trace with no reasoning run shows nothing rather than
   *  "0 spent".
   *
   *  SUMMING is correct and is the part that could quietly be wrong. Each
   *  `llm_usage` carries ONE Gemini call's `usage_metadata` — adk_agent.py's
   *  emit doc: "Multi-turn runs surface it on each turn's final event — so the
   *  dashboards graph per-turn cost." It is per-step, not a running total, so
   *  taking the last event would understate every multi-step run. Same
   *  treatment `omittedThoughtTokens` already gives `thoughts_token_count`. */
  const totalTokens = $derived.by((): number | null => {
    let total = 0;
    let saw = false;
    for (const e of entry.events) {
      if (e.event !== 'llm_usage') continue;
      const n = e.total_token_count;
      if (typeof n === 'number' && Number.isFinite(n)) {
        total += n;
        saw = true;
      }
    }
    return saw && total > 0 ? total : null;
  });

  const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));
  const num = (v: unknown): number | null => (typeof v === 'number' ? v : null);

  function toolLabel(row: { call?: TraceEvent; result?: TraceEvent }): string {
    return workerLabel(str(row.call?.tool_name || row.result?.tool_name) || '(unknown)', $t);
  }

  // The shareable link, in the two shapes design §4's URL-context rule defines.
  // With a conversation it points at the thread AND the message; without one it
  // points at the trace alone, which the router resolves to a desk decision
  // record. Both are URLs — the bare trace id this used to hand over stopped
  // being the honest answer the moment `?reasoning=` gained a destination of
  // its own, and a button sitting beside the id already displayed in this
  // footer should hand over something the id itself cannot.
  // A button, not an anchor: it copies, it does not navigate.
  const copyText = $derived(
    conversationId
      ? `${window.location.origin}/?conversation=${encodeURIComponent(conversationId)}&reasoning=${encodeURIComponent(traceId)}`
      : `${window.location.origin}/?reasoning=${encodeURIComponent(traceId)}`,
  );

  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  async function copyLink(): Promise<void> {
    try {
      await navigator.clipboard.writeText(copyText);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => {
        copied = false;
      }, 1200);
    } catch {
      // Clipboard unavailable (insecure context / denied) — fail quietly; the
      // trace id stays visible beside the button.
    }
  }
</script>

<div class="trace-detail" data-testid="trace-detail">
  {#if entry.enrich === 'error'}
    <!-- Never blank: an unloadable trace states the failure and offers the
         retry (the "unknown ≠ empty" rule). Any events that DID stream stay
         below — the two axes fail independently. -->
    <p class="trace-detail__error" data-testid="trace-detail-error">
      {$t('disclosure.loadError')}
      <button
        type="button"
        class="trace-detail__retry"
        data-testid="trace-detail-retry"
        onclick={onRetry}>{$t('disclosure.retry')}</button>
    </p>
  {:else if entry.enrich === 'loading' && rows.length === 0}
    <p class="trace-detail__loading ds-subtle" data-testid="trace-detail-loading">
      {$t('disclosure.loading')}
    </p>
  {/if}

  {#if omittedTokens > 0}
    <p class="trace-detail__omitted" data-testid="thought-omitted-note">
      {$t('timeline.omittedNote', { n: fmtNumber(omittedTokens, $locale) })}
    </p>
  {/if}

  {#if rows.length > 0}
    <ol class="trace-rows">
      {#each rows as row (row.key)}
        {#if row.kind === 'thought'}
          <li class="trace-row trace-row--thought" data-testid="trace-row-thought">
            <div class="thought">{str(row.event.thought_text)}</div>
          </li>
        {:else if row.kind === 'tool'}
          {@const ok = row.result ? row.result.result_ok !== false : null}
          <li class="trace-row trace-row--tool" data-testid="trace-row-tool">
            <details class="event" data-insert-id={eventKey((row.call ?? row.result)!)}>
              <summary class="event__summary">
                <Icon name="wrench" size={12} />
                <span class="event__tool">{toolLabel(row)}</span>
                {#if ok === true}
                  <span class="pair-result-ok">{$t('timeline.pair.ok')}</span>
                {:else if ok === false}
                  <span class="pair-result-err">{$t('timeline.status.error')}</span>
                {:else}
                  <span class="event__pending">{$t('timeline.status.pending')}</span>
                {/if}
              </summary>
              {#if row.call}
                <div class="event__label">{$t('timeline.pair.toolArgs')}</div>
                <pre class="ds-pre">{JSON.stringify(row.call.tool_args ?? {}, null, 2)}</pre>
              {/if}
              {#if row.result}
                <!-- The HITL gate, at the moment the operator asked for it. A
                     rollback proposal's result payload carries an approval_url,
                     and this turns it into a same-origin Approve button right
                     under the tool call that produced it — instead of leaving
                     the operator to find the URL inside a JSON preview.
                     Carried over from the deleted page-level Timeline, which
                     was its only renderer (ds-jns Task 3.3). The desk's pending
                     hero offers the NEWEST actionable rollback, which is not
                     the same promise: this one is about THIS proposal, and it
                     is where an operator who just asked for a rollback is
                     looking. ApprovalCta owns the security guard — an
                     off-origin or non-/approvals/ URL renders nothing. -->
                {#if str(row.result.tool_name) === 'propose_rollback_tool'}
                  <ApprovalCta resultPreview={str(row.result.result_preview)} />
                {/if}
                <div class="event__label">{$t('timeline.pair.resultPreview')}</div>
                <pre class="ds-pre">{fmtPreview(
                  str(row.result.result_preview) || $t('timeline.pair.emptyPreview'),
                )}</pre>
              {/if}
            </details>
          </li>
        {:else}
          {@const lat = num(row.event.latency_ms)}
          {@const docs = num(row.event.doc_count)}
          <li class="trace-row trace-row--mcp" data-testid="trace-row-mcp">
            <span class="ds-label mcp__kind">{$t('disclosure.mcpLabel')}</span>
            <span class="mcp__tool ds-code"
              >{workerLabel(str(row.event.mcp_tool || row.event.mcp_server) || '(unknown)', $t)}</span>
            <!-- How much grounding this call actually consulted. The latency
                 says the MCP was reached; the doc count says it ANSWERED with
                 something, which is the half that matters for a claim the crew
                 then makes on the strength of it. Rendered per row rather than
                 summed per server (the deleted Timeline's shape) because this
                 list is one call per line. -->
            {#if docs != null && docs > 0}
              <span class="mcp__lat" data-testid="trace-row-mcp-docs"
                >{$t('disclosure.docs', { n: docs })}</span>
            {/if}
            {#if lat != null}
              <span class="mcp__lat">{$t('timeline.latencyMs', { ms: lat })}</span>
            {/if}
          </li>
        {/if}
      {/each}
    </ol>
  {:else if entry.enrich === 'loaded'}
    <p class="trace-detail__empty ds-subtle" data-testid="trace-detail-empty">
      {directlyRecorded ? $t('timeline.empty.directlyRecorded') : $t('timeline.empty.notLoaded')}
    </p>
  {/if}

  <!-- Enrichment the live stream never carried: these come from the lazy
       /trace + /pr-body loads, which is what makes per-message action cards
       possible at all. Each self-suppresses when it has nothing to show. -->
  {#if doc}
    <DriftDiffCard decision={doc} />
    <DecisionSummary decision={doc} />
  {/if}
  <PrBodyDisclosure body={entry.prBody?.body ?? null} truncated={entry.prBody?.body_truncated ?? false} />
  {#if entry.prBodyMissing}
    <p class="trace-detail__soft ds-subtle" data-testid="trace-detail-prbody-missing">
      {$t('disclosure.prBodyMissing')}
    </p>
  {/if}

  <div class="trace-detail__footer">
    {#if totalTokens !== null}
      <span class="trace-detail__tokens ds-code" data-testid="trace-tokens"
        >{$t('disclosure.tokens', {
          tokens: fmtTokens({ total_token_count: totalTokens }, $t, $locale),
        })}</span>
    {/if}
    <span class="ds-label">{$t('disclosure.traceLabel')}</span>
    <span class="trace-detail__id ds-code">{traceId}</span>
    <button
      type="button"
      class="trace-detail__copy"
      class:copied
      data-testid="trace-copy-link"
      title={$t('disclosure.copyTitle')}
      onclick={copyLink}
    >
      <Icon name="copy" size={12} />
      {copied ? $t('disclosure.copied') : $t('disclosure.copyLink')}
    </button>
  </div>
</div>

<style>
  .trace-detail {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
  }

  .trace-rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .trace-row {
    padding: var(--ds-sp-2) 0;
    border-bottom: 1px solid var(--ds-border);
  }
  .trace-row:last-child {
    border-bottom: 0;
  }

  .thought {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--ds-muted);
    font-size: var(--ds-fs-2);
    line-height: var(--ds-lh-body);
    max-width: var(--ds-measure);
  }

  .event__summary {
    display: flex;
    gap: var(--ds-sp-2);
    align-items: center;
    cursor: pointer;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .event__tool {
    flex: 1 1 auto;
    font-weight: var(--ds-fw-medium);
    color: var(--ds-fg);
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

  .trace-row--mcp {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .mcp__kind {
    color: var(--ds-faint);
  }
  .mcp__tool {
    flex: 1 1 auto;
    font-size: var(--ds-fs-1);
    overflow-wrap: anywhere;
  }
  .mcp__lat {
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
    font-family: var(--ds-font-mono);
  }

  .trace-detail__omitted,
  .trace-detail__empty,
  .trace-detail__loading,
  .trace-detail__soft {
    margin: 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    font-style: italic;
    line-height: var(--ds-lh-body);
    max-width: var(--ds-measure);
  }

  .trace-detail__error {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--ds-sp-2);
    color: var(--ds-danger-ink);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
  }
  .trace-detail__retry {
    appearance: none;
    border: none;
    background: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    font: inherit;
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-stream-ink);
    text-decoration: underline;
  }

  .trace-detail__footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
    padding-top: var(--ds-sp-2);
    border-top: 1px solid var(--ds-border);
  }
  /* Run accounting, left of the trace id — both are facts ABOUT the run. */
  .trace-detail__tokens {
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
  }

  .trace-detail__id {
    flex: 1 1 auto;
    min-width: 0;
    color: var(--ds-faint);
    font-size: var(--ds-fs-1);
    overflow-wrap: anywhere;
  }
  .trace-detail__copy {
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-1);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    padding: 2px var(--ds-sp-2);
    cursor: pointer;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .trace-detail__copy:hover {
    color: var(--ds-fg);
  }
  .trace-detail__copy.copied {
    color: var(--ds-ok-ink);
    border-color: var(--ds-ok-ink);
  }
</style>
