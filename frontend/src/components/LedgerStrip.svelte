<script lang="ts">
  /**
   * LedgerStrip — the desk page's "Recent record" ledger (Task 3.4, mockup
   * ".striph"/".strip"/".srow" — docs/plans/2026-07-28-composite-mockup.html
   * lines ~145-162 for the CSS, ~327-348 for the markup).
   *
   * ds-3em: its own CARD, mounted by App between the desk and the estate. It
   * used to render inside ApprovalDesk's card, and the border grouped it with
   * the one pending proposal — so this estate-wide ledger read as that PR's
   * history. The name "strip" is kept because every selector, test and mockup
   * reference uses it; what changed is the shell, not the rows.
   *
   * Reduces the same
   * `decisions` list the rail already holds to a handful of rows via the pure
   * `ledgerRows()` (lib/ledger.ts) — this component only renders what that
   * function already decided; it computes no classification itself (mirrors
   * InstrumentBand's "deliberately dumb" convention).
   *
   * The mockup's quiet 定期点検 (periodic scan, "no drift found") row is
   * DEFERRED, not implemented here — see lib/ledger.ts's header comment for
   * why (scan runs aren't persisted anywhere the SPA can read).
   *
   * The mini SealStamp renders ONLY on `applied` rows, and always with
   * `animate` left at its default `false`: this strip re-renders on every
   * overview refresh, and only the desk hero's single freshly-approved stamp
   * should ever fire the stamp-in animation (SealStamp's own header comment).
   */
  import { t, locale, type TranslateFn } from '../lib/i18n';
  import { ledgerRows, ledgerTotal, type LedgerRow, type LedgerState } from '../lib/ledger';
  import { fmtClock, fmtPreview, decisionActionLabel } from '../lib/format';
  import { isReplayableTraceId } from '../lib/deeplink';
  import type { TraceCache } from '../lib/traceCache';
  import type { Decision, EnvDiff } from '../lib/types';
  import DecisionRecord from './DecisionRecord.svelte';
  import SealStamp from './SealStamp.svelte';

  let {
    decisions,
    max,
    recordTraceId = null,
    cache = null,
    onRecordChange = null,
  }: {
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    max?: number;
    /** The one open record, owned by App and passed down through ApprovalDesk
     *  (ds-jns). At most one row is open because there is only one id to be
     *  open — exclusivity is structural here, not bookkeeping this component
     *  has to keep right. */
    recordTraceId?: string | null;
    cache?: TraceCache | null;
    /** Ask App to open (`traceId`) or close (`null`) a record. Omitted →
     *  every row renders inert, exactly as it did before this existed. */
    onRecordChange?: ((traceId: string | null) => void) | null;
  } = $props();

  // One-way, deliberately: "show more" reveals the rest of the snapshot and
  // there is no "show less". Re-capping could hide a row whose record is open —
  // `keepTraceId` would rescue it, but the honest simplification is not to
  // create the situation.
  let showAll = $state(false);
  const total = $derived(ledgerTotal(decisions));
  const rows = $derived(
    ledgerRows(decisions, showAll ? Number.MAX_SAFE_INTEGER : (max as number), {
      keepTraceId: recordTraceId,
    }),
  );

  /** The trace this row can open a record for, or null for no affordance at all.
   *
   *  Two reasons a row gets nothing.
   *
   *  `Decision.trace_id` is optional and the docs are an open shape, so a row
   *  can carry nothing, or carry junk. Gated on `isReplayableTraceId` — the
   *  SAME rule the `?reasoning=` parser applies (lib/deeplink) — because
   *  opening a record writes that param: a laxer gate here would produce a row
   *  that opens once and then fails to restore on reload or share, which is
   *  the drift `isReplayableTraceId` was exported to prevent.
   *
   *  And a trace can belong to MORE THAN ONE decision. One request records the
   *  create-class IaC pair — a `waiting_for_rebake` pending and its merged
   *  successor — under a single trace id (agent/state_store.py's
   *  `find_decision_by_trace_id` documents this and answers with the NEWEST).
   *  Keying the accordion on the trace alone therefore opened a record under
   *  every matching row at once. Only the first match is openable, and "first
   *  here" is the same decision /trace will answer with — but that equivalence
   *  rests on TWO things, not one, and the second is easy to lose:
   *
   *   1. both orderings are newest-first — this strip by the doc's `created_at`
   *      (ledgerRows), that lookup by the server-managed `create_time`;
   *   2. on a TIE, `ledgerRows`' comparator returns 0 and leans on
   *      Array.prototype.sort's stability to keep the INPUT order — and the
   *      input is GET /decisions, which `list_decisions` already returns
   *      newest-first by `create_time`. Ties are not hypothetical: the pair is
   *      written by one request, and `Date.parse` truncates the sub-millisecond
   *      precision that separates the two docs on the server.
   *
   *  So a caller that hands this component a list in some other order can make
   *  the older sibling the openable one. `decisions` comes straight from the
   *  overview store's snapshot today; anything else needs a sort first.
   *
   *  Since ds-b0k this is mostly a RESIDUAL guard, and it is worth being exact
   *  about what is left for it to catch. `ledgerRows`' fold now collapses the
   *  create-class pair before this function ever sees it: both docs share an
   *  `event_key` and both are `waiting_for_rebake`, so rule 1 drops the older
   *  and one row reaches the strip. Verified by rendering the pair — with the
   *  key, one row (`awaiting_apply`); without it, two.
   *
   *  So what still arrives here as two rows on one trace is what the fold
   *  deliberately cannot reach: a doc with no `event_key`, a status this build
   *  does not recognise (the fold fails toward RETENTION on purpose), or two
   *  lanes that happen to share a trace. The gate stays for those, and its
   *  reason is the one thing true of all of them — the record is keyed by
   *  TRACE, so a second affordance on the same trace opens the SAME record. It
   *  is a duplicate door, not a door to something else.
   *
   *  Two earlier versions of this comment justified the gate by what the rows
   *  show, and both were wrong, in opposite directions. Before ds-b0k the pair
   *  classified identically (`open` for each), so the claim that the row said
   *  which sibling it was, was false. After ds-b0k they classify
   *  `awaiting_apply` vs `awaiting_merge` and are plainly distinguishable — so
   *  the correction was falsified too. Neither fact is what holds the gate up:
   *  it rests on the destination being identical, which no amount of row copy
   *  changes.
   *
   *  Null means the row renders as plain text. Never a disabled control: the
   *  strip is a record, and a greyed-out button on a row whose reasoning was
   *  never captured advertises something that does not exist. */
  function openableTrace(row: LedgerRow, index: number): string | null {
    if (onRecordChange === null || cache === null) return null;
    if (!isReplayableTraceId(row.decision.trace_id)) return null;
    const tid = row.decision.trace_id;
    return rows.slice(0, index).some((r) => r.decision.trace_id === tid) ? null : tid;
  }

  function toggle(traceId: string): void {
    onRecordChange?.(traceId === recordTraceId ? null : traceId);
  }

  /** The id linking a row to the record panel it controls.
   *
   *  `aria-controls` is advisory in the ARIA disclosure pattern — the panel is
   *  the row's immediate next sibling, so a screen reader moving forward
   *  reaches it regardless. It earns its place for the readers that offer a
   *  "jump to controlled element" command, which is worth more here than
   *  usual: the panel is TALL (prose, a field grid, a trace timeline), so
   *  arrowing past a collapsed-then-expanded row is a long trip.
   *
   *  Safe as a raw interpolation because `traceId` reached this function
   *  through `openableTrace`'s `isReplayableTraceId` gate — 32 hex characters,
   *  so the result is always a valid HTML id and never needs escaping. */
  const recordDomId = (traceId: string): string => `ledger-record-${traceId}`;

  // Decorative next to a text title — aria-hidden on the glyph span itself
  // (below) keeps a screen reader from reading raw punctuation aloud.
  // `failed` and `unconfirmed` get DIFFERENT glyphs on purpose. An outcome we
  // could not confirm is not a failure — the operation may still be running —
  // so it reads as a question, not a cross (ds-2mc).
  // `awaiting_apply` shares the in-flight glyph with `open` — both are work
  // that has not landed — but keeps its own state so the copy, the CSS hook and
  // `data-state` can distinguish "nobody has approved this" from "this was
  // approved and merged, and the apply has not run yet" (ds-db0).
  const GLYPH: Record<LedgerState, string> = {
    applied: '✓', open: '◍', awaiting_merge: '◍', awaiting_apply: '◍',
    noted: '⬤', failed: '✕', unconfirmed: '?',
  };

  function titleFor(row: LedgerRow, tf: TranslateFn): string {
    if (row.state === 'applied') return tf('desk.ledger.appliedTitle');
    if (row.state === 'open') return tf('desk.ledger.openTitle');
    if (row.state === 'awaiting_merge') return tf('desk.ledger.mergingTitle');
    if (row.state === 'awaiting_apply') return tf('desk.ledger.applyPendingTitle');
    if (row.state === 'failed') return tf('desk.ledger.failedTitle');
    if (row.state === 'unconfirmed') return tf('desk.ledger.unconfirmedTitle');
    return decisionActionLabel(row.decision.action, tf);
  }

  /** `contract_status` values that are NOT a policy violation, and therefore
   *  not what a rollback is ABOUT. Live proof both are reachable: the applied
   *  PAYMENT_MODE rollback ships FEATURE_NEW_CHECKOUT at `match` alongside it —
   *  context on the diff card, never the subject.
   *
   *  ⚠️ These values are MODEL-AUTHORED and the rollback gate does not trust
   *  them. `agent/validator.py:324` derives the real verdict from the contract
   *  and the live value, counts violations among the diffs the proposal
   *  REPORTED, and never rewrites the proposal — so `agent/main.py:1988`
   *  persists whatever the LLM wrote (Codex review). A genuine violation
   *  mislabelled `match` is therefore filterable here.
   *
   *  That is survivable because of what this string IS. The subtitle is
   *  best-effort identity, never a verdict, and the `target_revision` fallback
   *  is always there — but by TWO guards, not one, and an earlier draft of this
   *  comment named only the first (Codex r3):
   *
   *    - autonomous path: `validate()` runs at `agent/main.py:2519`, before the
   *      only call to `_do_rollback` at `:2693`, and `validator.py:219` raises
   *      without a `target_revision`;
   *    - chat path: `adk_tools.py:488` rejects anything `_REVISION_NAME`
   *      does not fullmatch (including empty), then writes the decision
   *      DIRECTLY at `:613` — `validator.py` never runs on this path at all.
   *
   *  So the worst case is a row naming the revision instead of the variable —
   *  less specific, still not the blank subject that caused the misread. Do NOT
   *  grow this into anything that asserts a policy outcome; for that, the
   *  status would first have to be derived server-side from ground truth.
   *
   *  A positive exclusion set, deliberately, not `!isViolation`: an
   *  unrecognised status must fall THROUGH to being named. Rule (i) — on an
   *  audit surface unknown fails toward retention, and here retention means
   *  naming. The subtitle asserts "this decision concerns X", never "X violated
   *  policy", which is what makes naming an unknown status safe rather than a
   *  second claim we cannot support. */
  const NON_VIOLATION_STATUS: ReadonlySet<string> = new Set(['match', 'present_allow_manual']);

  /** Longest derived subject before the ellipsis bites. The decision doc is
   *  UNREDACTED server data, so a name here is not length-bounded by anything
   *  the SPA controls, and the strip is a fixed-width grid. */
  const SUBJECT_MAX = 80;

  /** What a rollback is about, derived from the fields it actually carries.
   *
   *  Gated on `action === 'rollback'` on purpose. A `drift_issue` and an
   *  `escalation` also carry `diffs`, but "what is this decision about" has a
   *  different answer there (the issue, the escalation) — so this is not a
   *  generic diff-naming helper and must not become one.
   *
   *  Names are deduplicated: `agent/validator.py:275-286` permits identical
   *  duplicate diffs, and "PAYMENT_MODE, PAYMENT_MODE" reads as two variables.
   *
   *  Deliberately NOT `diffRows()` (lib/diff.ts) — that clamps each name for a
   *  table layout and routes values through `displayDiffValue`. Different
   *  filter, different truncation contract; sharing one would couple a subtitle
   *  to a card's formatting decisions. */
  function rollbackSubject(d: Decision): string | undefined {
    if (d.action !== 'rollback' || !Array.isArray(d.diffs)) return undefined;
    const seen = new Set<string>();
    for (const raw of d.diffs as unknown[]) {
      if (raw === null || typeof raw !== 'object') continue;
      const o = raw as Partial<EnvDiff>;
      if (typeof o.name !== 'string' || o.name === '') continue;
      const status = typeof o.contract_status === 'string' ? o.contract_status : '';
      if (NON_VIOLATION_STATUS.has(status)) continue;
      seen.add(o.name);
    }
    return seen.size > 0 ? fmtPreview([...seen].join(', '), SUBJECT_MAX) : undefined;
  }

  // Best-effort identity only, never a placeholder: a "—" or "unknown" string
  // would claim a fact we don't have. Omitting the <small> entirely (undefined,
  // not '') is the honest rendering when no field is present.
  //
  // ds-bch: before the rollback arms below, an APPLIED rollback rendered
  // "Approved · applied" with no subject at all — it carries neither pr_title
  // nor pr_number. On a strip, a row with no subject borrows the eye's memory
  // of the row above it, and on 2026-08-02 that is exactly what happened: an
  // operator read a PAYMENT_MODE rollback as the adopt-probe-topic PR sitting
  // above it and concluded that PR had been applied. It had not.
  //
  // Ordering is deliberate. pr_title/pr_number stay first — a PR title is a
  // better subject than anything derivable — and the derived arms only ever
  // fill a gap those left empty.
  function subtitleFor(row: LedgerRow): string | undefined {
    const d = row.decision;
    if (typeof d.pr_title === 'string' && d.pr_title !== '') return d.pr_title;
    if (typeof d.pr_number === 'number') return `#${d.pr_number}`;
    const subject = rollbackSubject(d);
    if (subject !== undefined) return subject;
    // Last resort, and a real one: prod holds a rollback doc (2026-07-29) with
    // a target_revision and no diffs at all. The revision it rolled back TO
    // still says which service this row concerns.
    if (d.action === 'rollback' && typeof d.target_revision === 'string' && d.target_revision !== '') {
      return fmtPreview(d.target_revision, SUBJECT_MAX);
    }
    return undefined;
  }
</script>

<!-- The row's four cells, written once and rendered into either shell below.
     Which shell a row gets is the whole affordance decision, so the cells must
     not be able to differ between them. -->
{#snippet cells(row: LedgerRow)}
  <span class="ledger-strip__time">{fmtClock(row.decision.created_at ?? '', $locale)}</span>
  <span class="ledger-strip__glyph ledger-strip__glyph--{row.state}" aria-hidden="true"
    >{GLYPH[row.state]}</span
  >
  <span class="ledger-strip__title">
    {titleFor(row, $t)}
    {#if subtitleFor(row) !== undefined}
      <small>{subtitleFor(row)}</small>
    {/if}
  </span>
  {#if row.state === 'applied'}
    <SealStamp size="sm" />
  {:else}
    <span></span>
  {/if}
{/snippet}

{#if rows.length > 0}
  <div class="ledger-strip" data-testid="ledger-strip">
    <div class="ledger-strip__heading">{$t('desk.ledger.heading')}</div>
    <div class="ledger-strip__rows">
      {#each rows as row, i (row.decision.decision_id)}
        {@const traceId = openableTrace(row, i)}
        {#if traceId !== null}
          <button
            type="button"
            class="ledger-strip__row ledger-strip__row--open"
            data-testid="ledger-strip-row"
            data-state={row.state}
            aria-expanded={traceId === recordTraceId}
            aria-controls={recordDomId(traceId)}
            onclick={() => toggle(traceId)}
          >
            {@render cells(row)}
          </button>
        {:else}
          <div class="ledger-strip__row" data-testid="ledger-strip-row" data-state={row.state}>
            {@render cells(row)}
          </div>
        {/if}
        {#if traceId !== null && traceId === recordTraceId && cache !== null}
          <div class="ledger-strip__record" id={recordDomId(traceId)}>
            <DecisionRecord {traceId} {cache} decision={row.decision} />
          </div>
        {/if}
      {/each}
      {#if total > rows.length}
        <button
          type="button"
          class="ledger-strip__more"
          data-testid="ledger-show-more"
          onclick={() => (showAll = true)}>{$t('desk.ledger.showMore', { n: total })}</button>
      {/if}
    </div>
  </div>
{/if}

<style>
  /* Its own card since ds-3em, where it used to be a strip inside the desk's.
     The `width: 100% / max-width / margin: 0 auto` triple is copied VERBATIM
     from `.estate-view` and `.approval-desk`, and `width: 100%` is the
     load-bearing member of it: this is a grid item in `.layout--full` with auto
     margins, so without an explicit width it is sized shrink-to-fit and lands
     on whatever its widest row happens to measure — 384px against two 780px
     cards, which is exactly the ds-cmc failure. Keep all three identical to its
     two siblings; a card that reaches 780px by accident stops the moment its
     contents change. */
  .ledger-strip {
    width: 100%;
    max-width: 780px;
    margin: 0 auto;
    background: var(--ds-bg);
    color: var(--ds-fg);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius, 6px);
    overflow: hidden;
  }

  /* No `border-top`: the card's own border is now the line that used to
     separate this heading from the desk content above it, and keeping both
     drew two rules 1px apart. */
  .ledger-strip__heading {
    padding: 10px 40px;
    font-family: var(--ds-font-mono);
    font-size: 10.5px;
    letter-spacing: 0.2em;
    color: var(--ds-faint);
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .ledger-strip__heading::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--ds-border);
  }

  .ledger-strip__rows {
    padding: 0 40px 26px;
  }

  .ledger-strip__row {
    display: grid;
    grid-template-columns: 58px 18px 1fr auto;
    gap: 14px;
    padding: 10px 0;
    border-bottom: 1px solid var(--ds-border);
    align-items: baseline;
    font-size: 12.5px;
  }
  .ledger-strip__row:last-child {
    border-bottom: 0;
  }

  /* The interactive shell. Only the button chrome is reset here — everything
     that positions the row (grid, padding, font-size, baseline alignment)
     stays in the base rule above, so the two shells cannot drift apart.
     `border-bottom` is restated because `border: 0` had to clear the UA's;
     `.ledger-strip__row:last-child` still wins over it on specificity (0,2,0
     vs 0,1,0), so a trailing open row still loses its rule as before. */
  .ledger-strip__row--open {
    appearance: none;
    width: 100%;
    margin: 0;
    border: 0;
    border-bottom: 1px solid var(--ds-border);
    background: none;
    color: inherit;
    font-family: inherit;
    text-align: left;
    cursor: pointer;
    transition: background var(--ds-dur-fast) var(--ds-ease);
  }
  .ledger-strip__row--open:hover {
    background: var(--ds-surface);
  }
  .ledger-strip__row--open:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
    border-radius: var(--ds-radius-sm);
  }
  @media (prefers-reduced-motion: reduce) {
    .ledger-strip__row--open {
      transition: none;
    }
  }

  .ledger-strip__record {
    padding: var(--ds-sp-4) 0;
    border-bottom: 1px solid var(--ds-border);
  }

  .ledger-strip__more {
    appearance: none;
    display: block;
    margin: 12px 0 0;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
    font-family: inherit;
    font-size: 11.5px;
    color: var(--ds-fg-soft);
    text-decoration: underline;
  }
  .ledger-strip__more:hover {
    color: var(--ds-fg);
  }
  .ledger-strip__more:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
    border-radius: var(--ds-radius-sm);
  }

  /* The timestamp is the ledger's whole point — WHEN each decision happened —
     so it is meaningful text and reads through ink-2 (6.47:1), not the lightest
     grey (3.08:1) it shared with the eyebrow heading (ds-qbo). At 11.5px mono
     it needed the contrast more than anything else on the strip. */
  .ledger-strip__time {
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-fg-soft);
    font-variant-numeric: tabular-nums;
  }

  .ledger-strip__glyph {
    font-size: 12px;
    line-height: 1.3;
  }
  .ledger-strip__glyph--applied {
    color: var(--ds-ok);
  }
  .ledger-strip__glyph--open,
  .ledger-strip__glyph--awaiting_merge,
  .ledger-strip__glyph--awaiting_apply {
    color: var(--ds-warn);
  }
  .ledger-strip__glyph--noted {
    color: var(--ds-faint);
  }

  .ledger-strip__title {
    color: var(--ds-fg);
  }
  .ledger-strip__title small {
    display: block;
    font-size: 11.5px;
    color: var(--ds-fg-soft);
    margin-top: 1px;
  }
</style>
