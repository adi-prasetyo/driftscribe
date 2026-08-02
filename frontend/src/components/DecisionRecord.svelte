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
  import {
    decisionGithubLink,
    decisionGithubDryRun,
    iacPrHref,
    iacApprovalHref,
    iacApproveLabel,
    iacApprovalCtaState,
  } from '../lib/approval';
  import { modeLabel, type AutonomyMode } from '../lib/autonomy';
  import { prefersReducedMotion } from '../lib/motion';
  import { t, locale, type MessageKey } from '../lib/i18n';
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
   *  copy fills the gaps, which is strictly additive to what either alone said.
   *
   *  Merged ONLY when both copies name the same `decision_id`. A trace can
   *  belong to several decisions (the create-class IaC lifecycle pair), and
   *  GET /trace answers with the newest — so a caller holding an older sibling
   *  would otherwise get a document that never existed: this row's status
   *  fields over that row's prose. LedgerStrip makes this unreachable by
   *  offering only the newest row an affordance, and this is the guard that
   *  keeps it unreachable if that ever changes.
   *
   *  On a mismatch the ROW wins outright — no merge, since the two documents
   *  are different decisions and splicing them is the hybrid this guard exists
   *  to prevent. The row is the fresher of the two, which is the opposite of
   *  what "the backend's answer" suggests: the cache entry is fetched ONCE per
   *  trace and then frozen, while `decision` re-flows from GET /decisions on
   *  every overview poll. So the reachable mismatch is a record held open
   *  across the write of a newer sibling — cache pinned to `pending`, row
   *  refreshed to `merged` — and answering with the cached copy would leave the
   *  card contradicting the row it is expanded under. */
  const doc = $derived.by((): Decision | null => {
    const fetched = entry.decision;
    if (!decision) return fetched ?? null;
    if (!fetched) return decision;
    return fetched.decision_id === decision.decision_id
      ? ({ ...fetched, ...decision } as Decision)
      : decision;
  });

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

  /** Always empty, and provably equivalent to the real thing HERE.
   *
   *  The set only distinguishes IMPLICIT supersession — a newer terminal row
   *  for this generation — from `apply`/`continue`. Those two are demoted above,
   *  and implicit supersession has no PR number to name, so it renders the same
   *  neutral page label. Three inputs, one output.
   *
   *  This card carried a `decisions` prop for exactly that computation until
   *  the demotion made it unable to change anything (Codex review round 5).
   *  Threading a snapshot through two call sites to feed a lookup whose result
   *  cannot reach a pixel is worse than not having it: the next reader has to
   *  work out that it is inert. The EXPLICIT "Superseded by PR #N" label is
   *  unaffected — it reads `doc.superseded_by_pr` and never consulted this. */
  const NO_SUPERSEDED: ReadonlySet<string> = new Set();

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

  /** The GitHub artifact this decision produced, if any.
   *
   *  Also carried over rather than invented: the decisions rail rendered this
   *  link, and deleting the rail (ds-jns Task 3.3) left it with no renderer at
   *  all — an Anchor drift_issue would have named an issue nowhere reachable.
   *  The record is where a decision's detail lives now, so it lands here. The
   *  action allowlist + host allowlist that gate it moved into lib/approval.ts
   *  intact; see decisionGithubLink for why both halves are load-bearing. */
  /** The GitHub artifact, from whichever of the two gates owns this action.
   *
   *  They stay separate rather than merging into one allowlist because the two
   *  urls have different provenance: `decisionGithubLink` reads a url the
   *  ACTING crew wrote onto the decision, while an `iac_apply`'s comes from the
   *  coordinator deriving it at serve time off the trusted config repo. Both
   *  end at `safeGithubHref`.
   *
   *  The iac arm exists because a COMPLETED iac_apply had nowhere else to link:
   *  DecisionSummary prints its PR as plain `#47`, and the desk's pending hero
   *  only ever offers the one that still needs an operator. A record of work
   *  already done is precisely the case neither covers. */
  /** The app's OWN record of an infra change: the plan it applied, who
   *  approved it, and — when it failed — why.
   *
   *  Restored from the deleted decisions rail (Codex review round 2). The
   *  GitHub link above reaches the PR; this reaches `/iac-approvals/<n>`, which
   *  is where the plan, the approval history and the failure details live.
   *
   *  The label is state-aware, with ONE deliberate demotion: this card never
   *  renders `apply` or `continue`, the two ACTIONABLE states. It offers the
   *  neutral page label in their place.
   *
   *  Those two are the only states derived from an ABSENCE — "no newer terminal
   *  row was found, so this change is still yours to apply" — and this card
   *  cannot support that claim (Codex review round 4). `/decisions` is
   *  `limit=50`, so an old record and its newer terminal sibling can BOTH be
   *  outside the window, which is durable rather than a polling lag; and a
   *  failed refresh retains the previous array, so the list can be confidently
   *  wrong. Either way the helper finds nothing and "Apply this change →" is
   *  offered for work that already ended.
   *
   *  Demotion rather than suppression, because the division is real: a RECORD
   *  says what happened, and the desk's pending hero says what to do next. The
   *  hero's SELECTION is sound — it only ever picks a decision FROM the
   *  snapshot it reasons over, so its absence claim is over a list that
   *  contains the row. Its FRESHNESS is not, yet: on a failed `/decisions`
   *  refresh the store retains the previous array and `deskModel` reaches the
   *  decisions-derived IaC rule before it consults `degraded`, so a row whose
   *  terminal outcome landed meanwhile can still be offered (ds-smr — filed,
   *  and not this component's to fix). That is a reason to fix the hero, not a
   *  reason for this card to start making the claim too.
   *
   *  Every remaining state is either read off the decision's own fields
   *  (`history`, `failure`, explicit `superseded`) or is the neutral page
   *  label, and those are safe under a bounded or stale list — which is why
   *  this card needs no snapshot at all (see NO_SUPERSEDED).
   *
   *  Follows the rail's href rule: a superseded row links to the PR that
   *  superseded it, not to its own dead page. */
  const iacApproval = $derived.by((): { href: string; label: string } | null => {
    if (!doc || doc.action !== 'iac_apply') return null;
    const sup = doc.superseded_by_pr;
    const target =
      typeof sup === 'number' && Number.isInteger(sup) && sup > 0 ? sup : doc.pr_number;
    const href = iacApprovalHref(target, $locale);
    if (href === null) return null;
    const kind = iacApprovalCtaState(doc, NO_SUPERSEDED).kind;
    const label =
      kind === 'apply' || kind === 'continue'
        ? $t('shared.approve.goToPage')
        : iacApproveLabel(doc, NO_SUPERSEDED, $t);
    return { href, label };
  });

  const github = $derived.by((): { href: string; labelKey: MessageKey } | null => {
    if (!doc) return null;
    const own = decisionGithubLink(doc);
    if (own !== null) return own;
    const iac = iacPrHref(doc);
    return iac === null ? null : { href: iac, labelKey: 'decisions.row.githubLink.viewPr' };
  });

  /** Two tokens that say a decision did LESS than its headline implies. Both
   *  rendered only in the deleted decisions rail; nothing read the fields at
   *  all between that deletion and this. They are the highest-stakes thing on
   *  the card — a row that reads "filed issue #99" for an issue that was never
   *  filed, or that shows an action the operator's own dial actually stopped,
   *  is the one failure this whole product is built not to have.
   *
   *  Deliberately NOT folded into DecisionSummary's field table: that table is
   *  what the decision RECORDS, and these two are statements about whether it
   *  happened. */
  const dryRun = $derived(doc ? decisionGithubDryRun(doc) : false);
  const suppressedMode = $derived.by((): string | null => {
    if (doc?.suppressed_by_autonomy !== true) return null;
    const m = doc.autonomy_mode;
    // The backend only suppresses in observe today, but all three dial modes
    // localize through the shared label; an unrecognized future value falls
    // back to its raw string rather than rendering a catalog key.
    return m === 'observe' || m === 'propose' || m === 'propose_apply'
      ? modeLabel(m as AutonomyMode, $t)
      : (m ?? '');
  });

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

  {#if suppressedMode !== null || dryRun}
    <p class="record__caveats">
      {#if suppressedMode !== null}
        <span class="ds-pill ds-pill--muted" data-testid="decision-autonomy-suppressed"
          >{$t('decisions.autonomy.suppressed', { mode: suppressedMode })}</span>
      {/if}
      {#if dryRun}
        <span class="ds-pill ds-pill--muted" data-testid="decision-dry-run"
          >{$t('decisions.dryRun.pill')}</span>
      {/if}
    </p>
  {/if}

  {#if prose !== null}
    <div class="record__prose" data-testid="decision-record-prose">
      <p class="ds-label record__prose-label">{$t('desk.record.prose')}</p>
      <div class="record__prose-body">{prose}</div>
    </div>
  {/if}

  {#if iacApproval !== null || github !== null}
    <p class="record__github">
      {#if iacApproval !== null}
        <a
          class="record__github-link"
          data-testid="iac-approve-link"
          href={iacApproval.href}
          target="_blank"
          rel="noopener">{iacApproval.label}</a>
      {/if}
      {#if github !== null}
      <a
        class="record__github-link"
        data-testid="decision-github-link"
        href={github.href}
        target="_blank"
        rel="noopener noreferrer">{$t(github.labelKey)}</a>
      {/if}
    </p>
  {/if}

  <!-- `decision={doc}`: the panel must reason about the SAME decision the header
       above it names, or one card can say "Infra apply" and "this reasoning
       couldn't be loaded" about one row. -->
  <TraceDetail {traceId} {entry} decision={doc} onRetry={() => void cache.retry(traceId)} />

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

  .record__github {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ds-sp-3);
    margin: 0;
  }
  /* Directly under the header, above everything the decision claims — these
     qualify the whole card, so they must be read before it, not after. */
  .record__caveats {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ds-sp-2);
    margin: 0;
  }
  .record__github-link {
    font-size: var(--ds-fs-1);
    color: var(--ds-navy);
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
