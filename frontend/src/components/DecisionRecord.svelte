<script lang="ts">
  // DecisionRecord — one decision opened on the DESK (ds-jns, design §3).
  //
  // Two callers, one card: the ledger row's accordion body (LedgerStrip), and
  // the pinned record App mounts when a bare `?reasoning=` deep link names a
  // decision older than the listed rows. Both are "the operator asked to see
  // this decision"; the only difference is whether a ledger row supplied the
  // decision doc, so `decision` is optional and `note` carries the pinned
  // case's one extra sentence.
  //
  // Unlike ReasoningDisclosure there is NO toggle. The record is mounted when
  // it is opened and unmounted when it is closed, so mounting IS the open
  // action — which is why the /trace fetch fires from an $effect here and from
  // a click handler there. Both are equally bounded by operator intent.
  //
  // It owns no trace state; the per-trace cache does (lib/traceCache.ts), so a
  // pinned record, an expanded ledger row and a live chat stream never fight
  // over one global timeline.
  import type { TraceCache, TraceCacheEntry } from '../lib/traceCache';
  import type { TraceEvent } from '../lib/timeline';
  import type { Decision } from '../lib/types';
  import { crewName } from '../lib/workloads';
  import { decisionActionLabel, fmtWhen } from '../lib/format';
  import { prefersReducedMotion } from '../lib/motion';
  import { t, locale } from '../lib/i18n';
  import CrewGlyph from './CrewGlyph.svelte';
  import TraceDetail from './TraceDetail.svelte';

  let {
    traceId,
    cache,
    decision = null,
    note = null,
  }: {
    traceId: string;
    cache: TraceCache;
    /** The row this record was opened from. Absent for a pinned deep link,
     *  which has only a trace id until GET /trace answers. */
    decision?: Decision | null;
    note?: 'outOfWindow' | null;
  } = $props();

  // Absent until the cache has been touched for this id — one frame, since the
  // $effect below fetches on mount. The blank stands in so the card renders its
  // header rather than nothing.
  const BLANK: TraceCacheEntry = {
    events: [],
    stream: 'idle',
    enrich: 'idle',
    decision: null,
    prBody: null,
    prBodyMissing: false,
    complete: false,
  };
  const entry = $derived($cache.get(traceId) ?? BLANK);

  /** The decision this record is about, assembled from both docs that describe
   *  it rather than picked between them.
   *
   *  They are not interchangeable. The ledger row's copy comes from
   *  GET /decisions and carries the serve-time joins that listing performs
   *  (live approval status, reconciled merge state); GET /trace's copy carries
   *  the fields the listing has no reason to project — `rationale`,
   *  `rendered_body`, the env diffs. Preferring the row outright hid the
   *  decision's own prose; preferring the fetched one would risk downgrading an
   *  enriched field to a staler value. The row wins on overlap and the fetched
   *  copy fills the gaps, which is strictly additive to what either alone said. */
  const doc = $derived(
    decision && entry.decision
      ? ({ ...entry.decision, ...decision } as Decision)
      : (decision ?? entry.decision ?? null),
  );

  /** The crew that produced this trace, or null.
   *
   *  It comes from the EVENTS, not from `doc`: decision documents carry no
   *  workload at all (neither `record_decision` writer in agent/main.py
   *  persists one), so a glyph derived from the decision would be CrewGlyph's
   *  unknown node on every single row. The log events do carry it — every
   *  emit in adk_agent.py stamps `current_workload()` — and one trace is one
   *  run by one crew, so the first event that names one names all of them.
   *
   *  Null is a real answer, not a gap to fill: an iac_apply is recorded
   *  directly by the approval handler with no reasoning run behind it, so
   *  there is no crew, and a static unknown square would be decoration
   *  claiming an agent that never ran. */
  function firstWorkload(events: TraceEvent[]): string | null {
    for (const e of events) {
      if (typeof e.workload === 'string' && e.workload !== '') return e.workload;
    }
    return null;
  }
  const crew = $derived(firstWorkload(entry.events));

  const str = (v: unknown): string => (typeof v === 'string' && v !== '' ? v : '');
  const action = $derived(decisionActionLabel(doc?.action, $t));
  // fmtWhen, the same helper DecisionSummary's "When" row uses below — one
  // moment must never print two ways in one card (ds-qbo). Deliberately NOT
  // the ledger row's fmtClock: that is an HH:mm column in a list of today's
  // work, and a record can be any age (the pinned one always is).
  const when = $derived(
    fmtWhen(typeof doc?.created_at === 'string' ? doc.created_at : '', $locale),
  );
  const hasHeader = $derived(crew !== null || action !== '' || when !== '');

  /** The decision's own prose.
   *
   *  Carried over from the page-level replay, whose hero card read exactly
   *  `rationale ?? rendered_body` (App.svelte's openTrace). Nothing else on the
   *  desk shows it — the ledger row has a title, DecisionSummary has a field
   *  table, and neither is the sentence explaining WHY. Without this, re-routing
   *  every door to the record would have quietly dropped the one piece of the
   *  replay that was prose.
   *
   *  Escaped plain text, like every other model-authored string on this surface
   *  (`rendered_body` is Markdown source and renders as its own text — the same
   *  thing FinalResponse did with it). */
  const prose = $derived(str(doc?.rationale) || str(doc?.rendered_body) || null);

  // "The trace loaded and nothing is attached to it" — a different fact from
  // "it wouldn't load", which TraceDetail's own error line already states, and
  // from "still loading". Reachable without anything being wrong: a bare
  // `?reasoning=` link can name a chat turn's trace, which is reasoning with no
  // decision behind it.
  const incomplete = $derived(entry.enrich === 'loaded' && doc === null);

  // Mount = open, so this is the fetch. Reads only the two props, never
  // `$cache` — reading the store here would re-arm the effect on every cache
  // write, and ensure() writes.
  $effect(() => {
    void cache.ensure(traceId);
  });

  // Mount = open here too, and an opened record is frequently off-screen: the
  // ledger row that opens it can be the fourth of four, and the desk's pending
  // hero sits a full viewport above the strip. Unconditional because every
  // mount of this component IS an operator asking to see this record — there is
  // no passive one to protect.
  let el = $state<HTMLElement | null>(null);
  $effect(() => {
    // `traceId` is read for its dependency, not its value: App's pinned record
    // keeps ONE element across a re-target (a second deep link, a different
    // ledger row while the pin is up), so without it a newly opened record
    // would silently stay off-screen.
    void traceId;
    el?.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'nearest' });
  });
</script>

<article class="record" data-testid="decision-record" bind:this={el}>
  {#if note === 'outOfWindow'}
    <p class="record__note" data-testid="decision-record-outofwindow">
      {$t('desk.record.outOfWindow')}
    </p>
  {/if}

  {#if hasHeader}
    <header class="record__header" data-testid="decision-record-header">
      {#if crew !== null}
        <CrewGlyph verb={crew} size={20} animated={false} />
        <span class="record__crew" data-testid="decision-record-crew">{crewName(crew)}</span>
      {/if}
      {#if action !== ''}
        <span class="record__action" data-testid="decision-record-action">{action}</span>
      {/if}
      {#if when !== ''}
        <time class="record__when" data-testid="decision-record-when" datetime={doc?.created_at}
          >{when}</time>
      {/if}
    </header>
  {/if}

  {#if prose !== null}
    <div class="record__prose" data-testid="decision-record-prose">
      <p class="ds-label record__prose-label">{$t('desk.record.prose')}</p>
      <div class="record__prose-body">{prose}</div>
    </div>
  {/if}

  <TraceDetail {traceId} {entry} onRetry={() => void cache.retry(traceId)} />

  {#if incomplete}
    <p class="record__incomplete" data-testid="decision-record-incomplete">
      {$t('desk.record.incomplete')}
    </p>
  {/if}
</article>

<style>
  .record {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
  }

  .record__header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--ds-sp-2);
    padding-bottom: var(--ds-sp-2);
    border-bottom: 1px solid var(--ds-border);
  }

  .record__crew {
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-fg);
  }

  .record__action {
    flex: 1 1 auto;
    min-width: 0;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }

  /* Pushed to the trailing edge only when the action row grew to fill the
     line; on a wrapped narrow card it simply follows. */
  .record__when {
    font-family: var(--ds-font-mono);
    font-size: var(--ds-fs-1);
    color: var(--ds-fg-soft);
    font-variant-numeric: tabular-nums;
  }

  /* The record's headline content, so it reads as prose rather than as another
     muted footnote — the one thing on this card that is a sentence. */
  .record__prose-label {
    display: block;
    margin: 0 0 var(--ds-sp-1);
    color: var(--ds-muted);
  }
  .record__prose-body {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-width: var(--ds-measure);
    color: var(--ds-fg);
    font-size: var(--ds-fs-2);
    line-height: var(--ds-lh-body);
  }

  /* Both trailing lines share TraceDetail's quiet register — they are
     statements about the record, not failures of it. */
  .record__note,
  .record__incomplete {
    margin: 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    font-style: italic;
    line-height: var(--ds-lh-body);
    max-width: var(--ds-measure);
  }
</style>
