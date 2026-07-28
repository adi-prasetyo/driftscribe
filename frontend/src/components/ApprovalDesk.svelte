<script lang="ts">
  /**
   * ApprovalDesk — the SPA's front door (Task 3.5, docs/plans/2026-07-28-
   * composite-mockup.html "SCREEN 1 — THE DESK"). Three states driven by
   * `deskModel()` (lib/desk.ts): PENDING (one thing needs the operator right
   * now), STAMPED (something was just resolved — a short-lived receipt), or
   * RESTING — the thesis screen. "Nothing needs you right now" is the
   * product's promise kept, not a gap to apologize for, so resting is
   * designed, not just "the empty case".
   *
   * Data comes EXCLUSIVELY from props (the overview store's snapshot,
   * App.svelte-owned) — this component performs no fetches of its own. The
   * one exception is `refresh`, an optional callback the desk uses to arm a
   * short, bounded re-check burst after sending the operator to an approval
   * page (see "fast convergence" below) — that's asking the ALREADY-OWNED
   * store to run its existing refresh sooner, not a new fetch path.
   */
  import { onMount } from 'svelte';
  import { t, locale, type TranslateFn } from '../lib/i18n';
  import { deskModel, awaitingCount, type DeskModel, type DeskPending, type DeskStamped } from '../lib/desk';
  import { resourceCards, scopeTotals, type InfraGraph, type PendingApproval } from '../lib/infra_graph';
  import { fmtWhen } from '../lib/format';
  import type { Decision } from '../lib/types';
  import type { AppView } from '../lib/deeplink';
  import InstrumentBand from './InstrumentBand.svelte';
  import LedgerStrip from './LedgerStrip.svelte';
  import SealStamp from './SealStamp.svelte';
  import DriftDiffCard from './DriftDiffCard.svelte';

  let {
    graph,
    decisions,
    pendingApprovals,
    onNavigate,
    refresh,
  }: {
    graph: InfraGraph | null;
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    pendingApprovals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined;
    onNavigate: (view: AppView) => void;
    /** overviewStore.refresh, threaded in so the return-ladder below (fast
     *  convergence after an approval) can ask the ALREADY-OWNED store to
     *  refetch sooner — this component still performs no fetch of its own.
     *  Optional so a test can render the desk without wiring it. */
    refresh?: (reason?: string) => void | Promise<void>;
  } = $props();

  // ---- instrument band numbers (scope-totals, not re-derived) ----
  const cards = $derived(graph ? resourceCards(graph, $t) : []);
  const scope = $derived(scopeTotals(cards, graph?.totals?.resources ?? 0));

  // ---- desk state selection ----
  // `decayTick` has no meaning of its own — bumping it is purely "please
  // recompute `model` now" (see the decay effect below). `deskModel` is
  // omitted an explicit `now`, so every recompute (decisions/pendingApprovals/
  // locale change, OR a decayTick bump) reads a fresh real clock internally
  // (lib/desk.ts: `input.now ?? Date.now()`) — no separate `now` state to
  // keep in sync here.
  let decayTick = $state(0);
  const model = $derived.by((): DeskModel => {
    void decayTick;
    return deskModel({ decisions, pendingApprovals, locale: $locale });
  });

  // The honest system-wide total of everything awaiting the operator, NOT
  // "is the desk currently pending" (deskModel surfaces only ONE at a time,
  // as a queue — see lib/desk.ts's awaitingCount header comment for why the
  // two numbers are deliberately different facts, and its dedicated test
  // for the case where the desk shows a single pending card while this is
  // 2+). Recomputes on decisions/pendingApprovals/locale changes only — NOT
  // tied to the stamped-decay `decayTick` above: a stamped item was never
  // counted as "awaiting" in the first place (awaitingCount only looks at
  // PENDING rows), so its decay never changes this number.
  const awaiting = $derived(awaitingCount({ decisions, pendingApprovals, locale: $locale }));

  // Stamped decay: when `model` is 'stamped', schedule exactly one timer to
  // force a recompute just past `stampedUntil`, so the desk falls back to
  // resting on its own — the store's next poll/focus refresh could be tens
  // of seconds away, and a 9-minutes-stale "you just did this" receipt reads
  // as a bug. The function returned here is a Svelte $effect teardown: the
  // framework calls it before this effect's own next run AND on component
  // destroy. That contract — not the setTimeout call itself — is what
  // guarantees at most one decay timer is ever pending: every re-arm (a
  // second approval landing before the first stamp decayed, an unrelated
  // poll refresh, leaving the desk view) tears down the previous timer
  // first, and unmounting the component tears down the last one.
  //
  // The trailing `+ 1` is the same off-by-one the return ladder below already
  // solves with `lastDelay + 1`. `selectStamped` treats the window as
  // INCLUSIVE (`now <= stampedUntil`), so without it a timer firing at exactly
  // `stampedUntil` recomputes a model that is STILL 'stamped' and re-arms with
  // a delay of 0 rather than decaying.
  //
  // Measured, not assumed — and it is smaller than it first looks. Spying on
  // setTimeout across the boundary: without the `+ 1`, the delays are
  // [540000, 0] (one redundant re-arm, and only one); with it, [540001] and
  // nothing further. It is a single wasted scheduling, NOT a livelock — an
  // earlier reading of this as "never converges" mistook a pending-but-not-
  // yet-due timer for a re-arm loop. Both versions land on resting at
  // stampedUntil + 1ms, so none of this was ever user-visible; the `+ 1` just
  // makes the wake-up land just PAST the window it waits on, which is what the
  // code always meant.
  $effect(() => {
    const m = model;
    if (m.kind !== 'stamped') return;
    const delay = Math.max(0, m.stampedUntil - Date.now()) + 1;
    const timer = setTimeout(() => {
      decayTick += 1;
    }, delay);
    return () => clearTimeout(timer);
  });

  // ---- fast convergence after an approval (bead ds-wd2.2) ----
  // overviewStore already refetches once on ANY tab focus/visibilitychange —
  // a general-purpose "the operator came back" trigger. This is narrower and
  // additive: when the desk KNOWS it just sent the operator to an approval
  // href (the CTA below opens it in a new tab), arm a short burst of EXTRA
  // refreshes on return, because the backend's apply can still be mid-flight
  // when that first general refetch lands — the next chance otherwise is the
  // store's 45s poll, too slow for the stamp to feel immediate.
  //
  // Bounded: RETURN_LADDER_DELAYS_MS is the entire budget — exactly that
  // many refresh() calls, nothing here re-arms itself past its own length.
  // Cannot stack: `ladderRunning` is what stops a SECOND ladder from
  // starting while one's timers are still pending (a focus event mid-ladder
  // is a no-op); `armed` is what stops an UNRELATED tab-focus from starting
  // a ladder at all (only a focus event that follows an actual CTA click
  // consumes it). Together the ladder can only ever be idle or running,
  // never running twice at once.
  const RETURN_LADDER_DELAYS_MS = [0, 3000, 8000] as const;
  let armed = false;
  let ladderRunning = false;
  let ladderTimers: ReturnType<typeof setTimeout>[] = [];

  function armReturnLadder(): void {
    armed = true;
  }

  function onWindowFocus(): void {
    if (!armed || ladderRunning) return;
    if (document.visibilityState !== 'visible') return;
    armed = false;
    ladderRunning = true;
    ladderTimers = RETURN_LADDER_DELAYS_MS.map((delay) =>
      setTimeout(() => {
        void refresh?.('approval-return');
      }, delay),
    );
    const lastDelay = RETURN_LADDER_DELAYS_MS[RETURN_LADDER_DELAYS_MS.length - 1];
    ladderTimers.push(
      setTimeout(() => {
        ladderRunning = false;
      }, lastDelay + 1),
    );
  }

  onMount(() => {
    window.addEventListener('focus', onWindowFocus);
    document.addEventListener('visibilitychange', onWindowFocus);
    return () => {
      window.removeEventListener('focus', onWindowFocus);
      document.removeEventListener('visibilitychange', onWindowFocus);
      for (const timer of ladderTimers) clearTimeout(timer);
    };
  });

  // ---- per-state display derivation (honest: real fields only, or a
  // generic string when no natural-language field exists — see desk.ts's
  // "no fictional timestamps" ethos, extended to display copy) ----

  function activeDecision(m: DeskModel): Decision | null {
    if (m.kind === 'pending') {
      if (m.source === 'rollback') return m.decision;
      return m.provenance.kind === 'decision' ? m.provenance.decision : null;
    }
    if (m.kind === 'stamped') return m.decision;
    return null;
  }

  function pendingHeadline(m: DeskPending, tf: TranslateFn): string {
    if (m.source === 'rollback') return tf('desk.pending.rollback.headline');
    if (m.provenance.kind === 'listing' && m.provenance.approval.title) {
      return m.provenance.approval.title;
    }
    if (m.provenance.kind === 'decision') {
      const prTitle = m.provenance.decision.pr_title;
      if (typeof prTitle === 'string' && prTitle !== '') return prTitle;
    }
    return tf('desk.pending.iac.headlineFallback', { pr: m.prNumber });
  }

  function pendingSubtitleTime(m: DeskPending): string | null {
    if (m.source === 'rollback') return m.decision.created_at ?? null;
    // The listing arm's DTO carries no created_at at all (infra_graph.ts) —
    // never fabricate one; the decisions-derived arm does carry created_at.
    if (m.provenance.kind === 'decision') return m.provenance.decision.created_at ?? null;
    return null;
  }

  function stampedDetail(m: DeskStamped, tf: TranslateFn): string {
    return m.source === 'rollback' ? tf('desk.stamped.rollback.detail') : tf('desk.stamped.iac.detail');
  }

  function stampedHeadline(m: DeskStamped, tf: TranslateFn): string {
    if (m.source === 'rollback') return tf('desk.stamped.rollback.headline');
    const prTitle = m.decision.pr_title;
    if (typeof prTitle === 'string' && prTitle !== '') return prTitle;
    if (typeof m.decision.pr_number === 'number') {
      return tf('desk.stamped.iac.headlineFallback', { pr: m.decision.pr_number });
    }
    return tf('desk.stamped.iac.headlineGeneric');
  }

  function stampedAuditTime(m: DeskStamped): string | null {
    return m.source === 'rollback' ? (m.decision.approval?.resolved_at ?? null) : (m.decision.applied_at ?? null);
  }
</script>

<section class="approval-desk" data-testid="approval-desk" aria-label={$t('desk.region.ariaLabel')}>
  <InstrumentBand managed={scope.managed} drift={scope.drift} {awaiting} {onNavigate} />

  <div class="approval-desk__deskwrap" data-testid="approval-desk-state" data-state={model.kind}>
    {#if model.kind === 'resting'}
      <div class="approval-desk__calm" data-testid="approval-desk-resting">
        <h2>{$t('desk.resting.headline')}</h2>
        <p class="approval-desk__watch" data-testid="approval-desk-watch">
          <span class="approval-desk__watch-dot" aria-hidden="true"></span>
          {$t('desk.resting.watching')}
          {#if graph?.generated_at}
            ・{$t('desk.resting.lastScan', { time: fmtWhen(graph.generated_at, $locale) })}
          {:else}
            ・{$t('desk.resting.scanPending')}
          {/if}
          ・{$t('desk.resting.resourceCount', { n: scope.totalResources })}
          {#if scope.drift === 0}
            ・{$t('desk.resting.noNewDrift')}
          {/if}
        </p>
      </div>
    {:else if model.kind === 'pending'}
      <!-- {@const} must be the immediate child of a block ({:else if} qualifies,
           a plain <div> does not), so both are hoisted here rather than sitting
           next to the markup that uses them. -->
      {@const proposedAt = pendingSubtitleTime(model)}
      {@const pendingDecision = activeDecision(model)}
      <div class="approval-desk__proposal" data-testid="approval-desk-pending" data-source={model.source}>
        <div class="approval-desk__who">
          <span
            >{model.source === 'rollback'
              ? $t('desk.pending.rollback.who')
              : $t('desk.pending.iac.who')}</span
          >
          {#if proposedAt}
            <span class="approval-desk__meta"
              >{$t('desk.pending.subtitleProposedAt', {
                time: fmtWhen(proposedAt, $locale),
              })}</span
            >
          {/if}
        </div>
        <h2>{pendingHeadline(model, $t)}</h2>
        {#if model.source === 'iac'}
          <p class="approval-desk__meta">{$t('desk.pending.prMeta', { pr: model.prNumber })}</p>
        {/if}
        <!-- Wrapper gated on the decision, not rendered unconditionally: the
             pending+iac+listing arm has no decision doc, so DriftDiffCard
             self-suppresses to nothing and an unconditional wrapper would
             leave its bare 26px top margin as a gap before the CTA row —
             visible in the Task 3.6 listing-arm screenshots. -->
        {#if pendingDecision}
          <div class="approval-desk__diff">
            <DriftDiffCard decision={pendingDecision} />
          </div>
        {/if}
        <div class="approval-desk__acts">
          <a
            class="approval-desk__btn approval-desk__btn--primary"
            data-testid="approval-desk-approve"
            href={model.href}
            target="_blank"
            rel="noopener"
            onclick={armReturnLadder}>{$t('desk.pending.approveCta')}</a
          >
          <a
            class="approval-desk__btn approval-desk__btn--ghost"
            data-testid="approval-desk-reject"
            href={model.href}
            target="_blank"
            rel="noopener"
            onclick={armReturnLadder}>{$t('desk.pending.rejectCta')}</a
          >
        </div>
      </div>
    {:else if model.kind === 'unresolved'}
      <!-- A rollback whose credential was spent but which did not demonstrably
           apply. No seal, no CTA, and deliberately no decay timer: unlike a
           stamp (a receipt whose job ends once seen) this is an open loop, and
           timing it out would re-create the silent disappearance rule 2.5
           exists to prevent. The two phases keep separate copy — "unconfirmed"
           must never render as "failed". -->
      {@const failed = model.phase === 'failed'}
      <div class="approval-desk__unresolved" data-testid="approval-desk-unresolved" data-phase={model.phase}>
        <div class="approval-desk__who">
          <span>{$t('desk.unresolved.who')}</span>
          <span class="approval-desk__meta"
            >{$t(failed ? 'desk.unresolved.failed.detail' : 'desk.unresolved.unknown.detail')}</span
          >
        </div>
        <h2>{$t(failed ? 'desk.unresolved.failed.headline' : 'desk.unresolved.unknown.headline')}</h2>
        <p class="approval-desk__unresolved-body">
          {$t(failed ? 'desk.unresolved.failed.body' : 'desk.unresolved.unknown.body')}
        </p>
        <DriftDiffCard decision={model.decision} />
      </div>
    {:else if model.kind === 'stamped'}
      {@const stampedDecision = activeDecision(model)}
      {@const auditTime = stampedAuditTime(model)}
      {#key model.decision.decision_id}
        <div class="approval-desk__stamped" data-testid="approval-desk-stamped" data-source={model.source}>
          <div class="approval-desk__who approval-desk__who--done">
            <span>{$t('desk.stamped.who')}</span>
            <span class="approval-desk__meta">{stampedDetail(model, $t)}</span>
          </div>
          <h2>{stampedHeadline(model, $t)}</h2>
          {#if stampedDecision}
            <div class="approval-desk__diff">
              <DriftDiffCard decision={stampedDecision} />
            </div>
          {/if}
          {#if auditTime}
            <p class="approval-desk__aud">
              {$t('desk.stamped.audit', { time: fmtWhen(auditTime, $locale) })}
            </p>
          {/if}
          <SealStamp size="lg" animate />
        </div>
      {/key}
    {/if}
  </div>

  <LedgerStrip {decisions} />
</section>

<style>
  .approval-desk {
    max-width: 780px;
    margin: 0 auto;
    background: var(--ds-paper);
    color: var(--ds-paper-ink);
    border: 1px solid var(--ds-paper-rule);
    border-radius: var(--ds-radius, 6px);
    overflow: hidden;
  }

  .approval-desk__deskwrap {
    padding: 40px 40px 26px;
    min-height: 280px;
    box-sizing: border-box;
  }

  /* h2, not h3, even though the plan and the mockup both say "Mincho h3 31px"
     — that phrase pins the SIZE, which is separable from the semantic level.
     The page's only h1 is the brand title (App.svelte), so an h3 here would
     skip a level for anyone navigating by heading, and the sibling estate view
     already uses h2. The 31px below is what actually delivers the mockup. */
  .approval-desk h2 {
    font-family: var(--ds-font-mincho);
    font-size: 31px;
    line-height: 1.42;
    font-weight: 400;
    margin: 14px 0 0;
    letter-spacing: 0.01em;
    text-wrap: balance;
    color: var(--ds-paper-ink);
  }

  .approval-desk__who {
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    letter-spacing: 0.08em;
    color: var(--ds-gblue);
    display: flex;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .approval-desk__who--done {
    color: var(--ds-ok-green);
  }
  .approval-desk__meta {
    color: var(--ds-paper-mut);
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
  }

  .approval-desk__watch {
    margin-top: 18px;
    font-family: var(--ds-font-mono);
    font-size: 12px;
    color: var(--ds-paper-mut);
    display: flex;
    gap: 4px;
    align-items: center;
    flex-wrap: wrap;
  }
  .approval-desk__watch-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ds-ok-green);
    flex: none;
  }

  .approval-desk__acts {
    display: flex;
    gap: 12px;
    margin-top: 30px;
    align-items: center;
  }
  .approval-desk__btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    padding: 13px 34px;
    border: 0;
    border-radius: 3px;
    letter-spacing: 0.03em;
    text-decoration: none;
    cursor: pointer;
  }
  .approval-desk__btn--primary {
    background: var(--ds-navy);
    color: #fff;
  }
  .approval-desk__btn--ghost {
    background: transparent;
    color: var(--ds-paper-ink-2);
    border: 1px solid var(--ds-paper-rule);
  }

  .approval-desk__aud {
    margin-top: 26px;
    font-family: var(--ds-font-mono);
    font-size: 12px;
    color: var(--ds-paper-mut);
  }

  /* Unresolved outcome. Danger accent for `failed`, muted-warning for
     `outcome_unknown` — the visual weight has to differ, or "we could not
     confirm" reads as "it broke" at a glance, which is the exact
     over-claim this state exists to avoid. */
  .approval-desk__unresolved {
    position: relative;
    border-left: 3px solid var(--ds-warn, var(--ds-border-strong));
    padding-left: var(--ds-sp-4);
  }
  .approval-desk__unresolved[data-phase='failed'] {
    border-left-color: var(--ds-danger);
  }
  .approval-desk__unresolved h2 {
    margin: var(--ds-sp-2) 0 0;
  }
  .approval-desk__unresolved-body {
    margin: var(--ds-sp-3) 0 var(--ds-sp-4);
    color: var(--ds-muted);
    max-width: 52ch;
  }

  .approval-desk__stamped {
    position: relative;
  }
  .approval-desk__stamped :global(.seal-stamp) {
    position: absolute;
    right: -6px;
    top: -4px;
  }
  /* Reserve the seal's column. The seal is OUT OF FLOW (absolute, 76px wide,
     pulled 6px past the right edge), so without this the headline and the
     status line simply flow underneath it — and they do: the Task 3.6 visual
     gate caught "Adopt payment-demo Cloud Run service into IaC" running under
     the stamp, and 「適用完了」/"Change applied" half-hidden behind it, in both
     locales. 92px = the seal's 76px, less the 6px overhang, plus a ~22px
     gutter so a descender never kisses the ring.
     Applied to these two children only, NOT to .approval-desk__deskwrap: the
     diff card and the audit line below the seal should still use the full
     column width, and padding the whole wrapper would indent them too. */
  .approval-desk__stamped .approval-desk__who--done,
  .approval-desk__stamped h2 {
    padding-right: 92px;
  }

  /* Restyle DriftDiffCard in place (it stays a single component, used
     unmodified in the chat view too) — scoped under this desk-only wrapper
     class so the chat-view usage of DriftDiffCard is completely unaffected. */
  .approval-desk__diff {
    margin: 26px 0 0;
  }
  .approval-desk__diff :global(.drift-diff-card) {
    background: transparent;
    border: none;
    border-top: 1px solid var(--ds-paper-rule);
    border-bottom: 1px solid var(--ds-paper-rule);
    box-shadow: none;
    border-radius: 0;
    padding: 11px 0;
    margin: 0;
  }
  .approval-desk__diff :global(.drift-diff-card__label) {
    color: var(--ds-paper-mut);
  }
  .approval-desk__diff :global(.drift-diff-card__table th) {
    color: var(--ds-paper-mut);
    border-bottom-color: var(--ds-paper-rule);
  }
  .approval-desk__diff :global(.drift-diff-card__table td) {
    border-bottom-color: var(--ds-paper-rule);
    color: var(--ds-paper-ink);
  }
  .approval-desk__diff :global(.ds-code) {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--ds-paper-ink-2);
  }
</style>
