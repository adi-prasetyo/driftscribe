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
  import { notifyFailed, TERMINAL_FAILED_APPLY_STATUSES } from '../lib/approval';
  import type { Decision } from '../lib/types';
  import InstrumentBand, { type BandStat } from './InstrumentBand.svelte';
  import LedgerStrip from './LedgerStrip.svelte';
  import SealStamp from './SealStamp.svelte';
  import DriftDiffCard from './DriftDiffCard.svelte';

  let {
    graph,
    decisions,
    pendingApprovals,
    settled = true,
    degraded = false,
    lastError = null,
    onShowEstate,
    onOpenTrace,
    refresh,
  }: {
    graph: InfraGraph | null;
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    pendingApprovals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined;
    /** Has the overview store's first refresh cycle completed? Defaults to
     *  true so a test can mount the desk with a ready-made snapshot without
     *  opting into load state (ds-eh6). */
    settled?: boolean;
    /** Did the last completed cycle fail to get a trustworthy answer about
     *  pending work? See OverviewState.degraded (ds-eh6). */
    degraded?: boolean;
    /** The store's last failing fetch kind, or null. Only `'graph'` can reach
     *  the resting state (a pending/decisions failure sets `degraded`, which
     *  routes to `unknown` instead), and it is used for exactly one thing:
     *  refusing to print a fresh-looking "last scan" line for a scan that did
     *  not refresh. */
    lastError?: 'graph' | 'pending' | 'decisions' | null;
    /** Bring the estate into view. Takes no argument: since the 2026-07-31
     *  merge there is exactly one destination and it is a section of this same
     *  page, so App scrolls (and moves focus) rather than navigating. */
    onShowEstate: () => void;
    /** Opens a past reasoning timeline (App.svelte's openTrace — it switches
     *  to the chat view and syncs `?reasoning=`). Optional: when omitted the
     *  pending card's "view the reasoning" link is not rendered at all, rather
     *  than rendered inert. */
    onOpenTrace?: (traceId: string) => void;
    /** overviewStore.refresh, threaded in so the return-ladder below (fast
     *  convergence after an approval) can ask the ALREADY-OWNED store to
     *  refetch sooner — this component still performs no fetch of its own.
     *  Optional so a test can render the desk without wiring it. */
    refresh?: (reason?: string) => void | Promise<void>;
  } = $props();

  // ---- instrument band numbers (scope-totals, not re-derived) ----
  const cards = $derived(graph ? resourceCards(graph, $t) : []);
  const scope = $derived(scopeTotals(cards, graph?.totals?.resources ?? 0));
  // No graph means the estate has not been read — NOT that it is empty
  // (ds-eh6). `scopeTotals` over zero cards honestly returns zeros, and those
  // zeros are correct as arithmetic; they are only wrong as an ANSWER, because
  // nothing was counted. Gate on the graph rather than `settled` so a settled
  // cycle whose graph fetch failed also reads as unknown instead of "0".
  //
  // A DEGRADED graph counts as no graph. The backend soft-fails /infra/graph to
  // a well-formed 200 carrying `degraded: true` and ZERO totals, so a non-null
  // check alone lets an outage render "0 managed, 0 drift" with full
  // confidence — the same trap as pending-approvals' degraded 200, one endpoint
  // over. Everything derived from the graph keys off this, not off `graph`.
  const graphUsable = $derived(!!graph && graph.degraded !== true);
  const bandManaged = $derived(graphUsable ? scope.managed : null);
  const bandDrift = $derived(graphUsable ? scope.drift : null);

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
    return deskModel({ decisions, pendingApprovals, locale: $locale, settled, degraded });
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
  // `null` while the first cycle is outstanding (ds-eh6): a count of 0 derived
  // from an empty placeholder snapshot is not "nothing awaits you", it is "we
  // have not looked". Once settled the real count renders, INCLUDING a genuine
  // 0 — and including under `degraded`, where the number is the honest floor of
  // what we could see and the hero above it is already saying so.
  // Also null under `degraded`: after a cycle in which the decisions or
  // pending-approvals lane failed, an exact "0 awaiting" is a precise figure
  // derived from a snapshot we just admitted is incomplete — and it would sit
  // directly above a hero saying a waiting proposal may be missing. Two
  // statements on one screen, one of them false.
  const awaiting = $derived(
    settled && !degraded ? awaitingCount({ decisions, pendingApprovals, locale: $locale }) : null,
  );

  // ---- reasoning link (ds-wd2.15) ----
  // The mockup's `.why` line. Renders only when the model actually carries a
  // replay-able trace id AND a handler exists — never as a disabled affordance,
  // and never with the mockup's "(N steps)" count, which the desk has no way to
  // know without fetching the trace it is offering to open.
  function openReasoning(traceId: string): void {
    onOpenTrace?.(traceId);
  }

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
      // Titleless `decision` provenance means the PR already merged (desk.ts:78-84)
      // — say it is waiting to be APPLIED, never waiting for an approval the
      // operator has already given (ds-db0).
      return tf('desk.pending.iacMerged.headlineFallback', { pr: m.prNumber });
    }
    // Listing provenance with no title: only ask for an approval when no
    // decision proves one already happened (see iacApprovalRecorded).
    return iacApprovalRecorded(m.prNumber, decisions ?? [])
      ? tf('desk.pending.iacMerged.headlineFallback', { pr: m.prNumber })
      : tf('desk.pending.iac.headlineFallback', { pr: m.prNumber });
  }

  /**
   * True when some decision PROVES the operator already approved this PR AND
   * no decision contradicts that the change is still on its way to being
   * applied. Both halves are load-bearing; see the two comments inside.
   *
   * Provenance alone is not enough (Codex review). `selectPendingIac` (the
   * listing arm) is tried FIRST and returns immediately (desk.ts:627), and the
   * open-PR listing is cached for up to 60s, so for a minute after the approve
   * click the listing still carries the just-merged PR and wins. It is filtered
   * only for `applied`+`merged` (approval.ts:337) — `waiting_for_rebake`+
   * `merged` does not qualify. So "listing provenance" means "GitHub said this
   * PR is open", NOT "nobody has approved it", and the byline must key on the
   * decisions, not on which selector happened to win.
   *
   * Deliberately does NOT touch selection: desk.ts:620 records that making the
   * PR-wide and per-generation rules share one answer is what once deleted a
   * live generation from the desk (ds-0rm). Copy only.
   */
  function iacApprovalRecorded(
    prNumber: number,
    list: ReadonlyArray<Decision | null | undefined>,
  ): boolean {
    // A witness is not enough on its own — it must also be UNCONTRADICTED
    // (Codex review r3). Decision docs ACCUMULATE: the `waiting_for_rebake`+
    // `merged` row is never rewritten, so it is still sitting there after the
    // resume-apply appends `failed_state_suspect` or `ambiguous`
    // (agent/main.py:7452). With a stale listing still naming the PR — the
    // pending fetch can fail while the decisions refresh (overviewStore.ts:237),
    // and the listing arm wins regardless (desk.ts:627) — this byline would
    // report a terminally frozen apply as "approved and waiting to be applied",
    // dressing a runbook condition up as ordinary pending work. Exactly the
    // ds-2mc mistake: a spent credential is not a landed change.
    //
    // Any terminal failure for this PR disqualifies the claim, without
    // consulting timestamps. Suppressing here costs only the improved wording
    // (the neutral listing copy still renders, and the card's link still reaches
    // the page that explains the failure), whereas a wrong "all is well" is the
    // one outcome this whole change exists to prevent.
    const contradicted = list.some(
      (d) =>
        d?.action === 'iac_apply' &&
        d?.pr_number === prNumber &&
        typeof d?.apply_status === 'string' &&
        TERMINAL_FAILED_APPLY_STATUSES.has(d.apply_status),
    );
    if (contradicted) return false;

    return list.some(
      (d) =>
        d?.action === 'iac_apply' &&
        d?.pr_number === prNumber &&
        // ⚠️ This join is PR-WIDE, and the listing DTO carries no generation
        // identity to narrow it with (no event_key, no head_sha). So it must
        // only accept evidence that is impossible to misattribute across
        // generations: `waiting_for_rebake`+`merged` proves the PR ITSELF
        // merged, and a merged PR cannot simultaneously be genuinely open with
        // a newer unapproved generation — any listing row for it is therefore
        // the stale cache, which is exactly the case this exists to fix.
        //
        // A broader "any status at or past approval" test would repeat the
        // ds-0rm mistake (desk.ts:620) one layer up: after `generation A
        // applied → merge FAILED → head advances → generation B unapproved`,
        // A's terminal decision would vouch for B and the desk would tell the
        // operator they had already approved something they had not. Claiming
        // a missing approval is the dangerous direction; a merge that failed
        // never reaches merge_state 'merged', so it is excluded here.
        d?.apply_status === 'waiting_for_rebake' &&
        d?.merge_state === 'merged',
    );
  }

  /** `who` byline key. Says "already approved, waiting to be applied" whenever a
   *  decision proves the approval happened — regardless of which arm selected
   *  the row, so a stale cached listing cannot re-ask for a given approval. */
  function pendingWhoKey(m: DeskPending): 'desk.pending.iac.who' | 'desk.pending.iacMerged.who' {
    if (m.source === 'rollback') return 'desk.pending.iac.who';
    if (m.provenance.kind === 'decision') return 'desk.pending.iacMerged.who';
    return iacApprovalRecorded(m.prNumber, decisions ?? [])
      ? 'desk.pending.iacMerged.who'
      : 'desk.pending.iac.who';
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

  // ds-7ag.2 — where each band numeral goes: managed and drift point at the
  // infrastructure map, which since the 2026-07-31 merge is the estate SECTION
  // of this same page. So the click scrolls there instead of navigating, and
  // App moves focus with the scroll.
  //
  // `awaiting` is deliberately NOT routed here and never reaches this handler,
  // because the band renders it as an inert figure (ds-s61 — see
  // InstrumentBand's routing table). It used to scrollIntoView + focus this
  // page's own pending card, but that card is already on screen ~270px below
  // the numeral, so the scroll had nowhere to go and merely consumed the dead
  // 38px the old viewport calc left lying around. The number sits directly
  // above its own subject; that is the wayfinding, and it needs no click.
  function onStat(_stat: BandStat): void {
    onShowEstate();
  }
</script>

<section class="approval-desk" data-testid="approval-desk" aria-label={$t('desk.region.ariaLabel')}>
  <InstrumentBand managed={bandManaged} drift={bandDrift} {awaiting} {onStat} />

  <div class="approval-desk__deskwrap" data-testid="approval-desk-state" data-state={model.kind}>
    {#if model.kind === 'unknown'}
      <!-- ds-eh6. NOT a variant of resting: resting asserts that nothing needs
           the operator, and neither of these two states has the standing to say
           so. `loading` resolves itself in seconds; `degraded` admits a gap that
           may not. They share a shape but never share copy. -->
      {@const isLoading = model.reason === 'loading'}
      <div
        class="approval-desk__calm approval-desk__calm--slim"
        data-testid="approval-desk-unknown"
        data-reason={model.reason}
      >
        <h2>{$t(isLoading ? 'desk.unknown.loading.headline' : 'desk.unknown.degraded.headline')}</h2>
        <p class="approval-desk__watch" data-testid="approval-desk-watch">
          <span
            class="approval-desk__watch-dot approval-desk__watch-dot--unknown"
            aria-hidden="true"
          ></span>
          {$t(isLoading ? 'desk.unknown.loading.body' : 'desk.unknown.degraded.body')}
        </p>
      </div>
    {:else if model.kind === 'resting'}
      <div class="approval-desk__calm approval-desk__calm--slim" data-testid="approval-desk-resting">
        <h2>{$t('desk.resting.headline')}</h2>
        <p class="approval-desk__watch" data-testid="approval-desk-watch">
          <span class="approval-desk__watch-dot" aria-hidden="true"></span>
          {$t('desk.resting.watching')}
          <!-- A graph fetch that failed this cycle leaves `graph` at its PRIOR
               value, so `generated_at` is still populated and still parses —
               it just no longer describes a scan that happened. Printing it
               unqualified would age silently into a lie, so a stale marker
               rides along. This is `lastError`'s one consumer (ds-eh6); a
               pending/decisions failure never reaches resting, so `'graph'` is
               the only value that can be observed here. -->
          {#if graphUsable && graph?.generated_at}
            ・{$t('desk.resting.lastScan', { time: fmtWhen(graph.generated_at, $locale) })}{#if lastError === 'graph'}<span
                class="approval-desk__stale"
                data-testid="approval-desk-stale-scan">{$t('desk.resting.scanStale')}</span
              >{/if}
          {:else if !settled}
            ・{$t('desk.resting.scanPending')}
          {:else}
            <!-- The cycle FINISHED and we still have no usable graph, so the
                 scan is not "pending" — that string (JA: 走査時刻 取得中,
                 literally "acquiring scan time") promises something in flight,
                 and it would sit there until the next 45s poll. -->
            ・{$t('desk.resting.scanUnavailable')}
          {/if}
          <!-- Both segments are GRAPH-derived, so both need a usable graph.
               Without this gate an absent or degraded graph rendered
               "・0 resources ・no new drift" — two confident claims about an
               estate nothing had successfully read, sitting inside the calm
               state. `scope.drift === 0` is especially deceptive there,
               because zero-because-unread is indistinguishable from
               zero-because-clean once it reaches the copy. -->
          {#if graphUsable}
            ・{$t('desk.resting.resourceCount', { n: scope.totalResources })}
            {#if scope.drift === 0}
              ・{$t('desk.resting.noNewDrift')}
            {/if}
          {/if}
        </p>
      </div>
    {:else if model.kind === 'pending'}
      <!-- {@const} must be the immediate child of a block ({:else if} qualifies,
           a plain <div> does not), so both are hoisted here rather than sitting
           next to the markup that uses them. -->
      {@const proposedAt = pendingSubtitleTime(model)}
      {@const pendingDecision = activeDecision(model)}
      <!-- Carried an id + tabindex="-1" so the band's awaiting numeral could
           scroll and focus it (ds-7ag.2). That numeral is an inert figure now
           (ds-s61), and nothing else referenced either attribute — a focusable
           div with no focus rule and no jump aimed at it is dead weight. -->
      <div
        class="approval-desk__proposal"
        data-testid="approval-desk-pending"
        data-source={model.source}
      >
        <div class="approval-desk__who">
          <span
            >{model.source === 'rollback'
              ? $t('desk.pending.rollback.who')
              : $t(pendingWhoKey(model))}</span
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
        <!-- ds-hdt. The operator is reading this card because they opened the
             desk, not because anything told them to. Saying so is the honest
             move: without it the silence reads as "nothing needed me", which
             is exactly the impression that let a 503 webhook go unnoticed.
             Gated on a POSITIVE 'failed' (see notifyFailed) so it never fires
             on a historical row that simply predates the field. -->
        {#if pendingDecision && notifyFailed(pendingDecision)}
          <p class="approval-desk__notice" data-testid="approval-desk-notify-failed">
            {$t('desk.pending.notifyFailed')}
          </p>
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
        <!-- ds-wd2.15, the mockup's `.why` line. It matters most on the arm
             that renders NO diff above: with listing provenance there is no
             decision doc, so DriftDiffCard self-suppresses and the card is
             otherwise a title, a PR number and two buttons — the page's
             highest-stakes CTA carrying its least evidence. A button, not an
             anchor: openTrace is client-side view state, not a navigation. -->
        {#if model.traceId && onOpenTrace}
          {@const traceId = model.traceId}
          <p class="approval-desk__why">
            <button
              type="button"
              class="approval-desk__why-btn"
              data-testid="approval-desk-why"
              onclick={() => openReasoning(traceId)}>{$t('desk.pending.viewReasoning')}</button
            >
          </p>
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
    /* Explicit, though this card happens to reach 780px on its own: the band's
       44px numerals give it a max-content width past the cap, so shrink-to-fit
       lands on the same number by accident. Leaving it to that accident is what
       let EstateView render 384px wide beneath it after the 2026-07-31 merge —
       and it would come back the moment the band slimmed. The two cards share
       one column; keep this pair identical to EstateView's. */
    width: 100%;
    max-width: 780px;
    margin: 0 auto;
    background: var(--ds-bg);
    color: var(--ds-fg);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius, 6px);
    overflow: hidden;
  }

  .approval-desk__deskwrap {
    padding: 40px 40px 26px;
    /* `min-height: 280px` used to hold the hero open so a calm desk did not
       collapse to a headline over acres of nothing. With the estate section
       under it (2026-07-31 merge) that reserved blank height IS the emptiness
       we are removing, and the hero growing when a decision arrives is the
       deliberate demo beat — see the design doc's "Accepted trade-offs". */
    box-sizing: border-box;
  }
  /* The two calm states slim the whole block, not just its inner text: the
     40px top inset belongs to a hero that carries the page, and this one no
     longer does. Scoped by `data-state` on the wrapper (already there) rather
     than by padding the nested calm div, which would inset twice. */
  .approval-desk__deskwrap[data-state='resting'],
  .approval-desk__deskwrap[data-state='unknown'] {
    padding: 18px 40px;
  }

  /* One baseline-aligned row: headline, then the watch metadata beside it.
     Wraps rather than truncates — JA runs longer and the watch line grows
     conditional segments. */
  .approval-desk__calm--slim {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 6px 16px;
  }
  /* Body-size, not the 31px Mincho hero rule — that stays for the tall states.
     Still an h2 (see the heading-level note above); only its size changes.

     The `.approval-desk` prefix is load-bearing, NOT redundant nesting. Without
     it this selector and the `.approval-desk h2` rule below are BOTH (0,2,1) —
     Svelte scopes each to two classes plus the `h2` type, and `:where()` adds
     nothing — so the cascade fell to source order and the 31px rule, being
     later, won every property here. The whole rule was dead: the strip shipped
     with a 31px Mincho headline in it. The prefix makes this (0,3,1), which
     beats the base rule wherever either is moved, so a future reorder of this
     stylesheet cannot silently revive the bug. Pinned by a computed-style
     assertion in the smoke suite — jsdom does not run the cascade, so no unit
     test can see this class of failure. */
  .approval-desk .approval-desk__calm--slim h2 {
    font-family: inherit;
    font-size: 15px;
    line-height: 1.5;
    font-weight: 600;
    letter-spacing: 0;
    margin: 0;
  }
  .approval-desk__calm--slim .approval-desk__watch {
    margin-top: 0;
  }

  /* h2, not h3, even though the plan and the mockup both say "Mincho h3 31px"
     — that phrase pins the SIZE, which is separable from the semantic level.
     The page's only h1 is the brand title (App.svelte), so an h3 here would
     skip a level for anyone navigating by heading. Since the 2026-07-31 merge
     this matters more, not less: EstateView's group headings are h2 and now sit
     on the SAME page, so a heading walk runs h1 → h2 (desk) → h2 (estate) with
     no gap. The 31px below is what actually delivers the mockup. */
  .approval-desk h2 {
    font-family: var(--ds-font-mincho);
    font-size: 31px;
    line-height: 1.42;
    font-weight: 400;
    margin: 14px 0 0;
    letter-spacing: 0.01em;
    text-wrap: balance;
    color: var(--ds-fg);
  }

  /* Reads through stream-INK, not raw stream: this is the meaningful status line
     ("Anchor is proposing" / "you approved") at 11.5px, and #4285f4 is 3.42:1 on
     paper against a 4.5:1 floor. It was --ds-gblue until the ds-qbo retirement
     renamed it, which is how it slipped past the raw-stream audit one commit
     earlier — the audit grepped var(--ds-stream), and this was not that yet. */
  .approval-desk__who {
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    letter-spacing: 0.08em;
    color: var(--ds-stream-ink);
    display: flex;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .approval-desk__who--done {
    color: var(--ds-ok);
  }
  .approval-desk__meta {
    color: var(--ds-faint);
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
  }

  .approval-desk__watch {
    margin-top: 18px;
    font-family: var(--ds-font-mono);
    font-size: 12px;
    color: var(--ds-faint);
    display: flex;
    gap: 4px;
    align-items: center;
    flex-wrap: wrap;
  }
  .approval-desk__watch-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ds-ok);
    flex: none;
  }
  /* The watch dot is green because it means "watching, all clear". The unknown
     states are neither, so the dot keeps the shape (same line rhythm, no layout
     shift when the desk settles into resting) and drops the claim. */
  .approval-desk__watch-dot--unknown {
    background: var(--ds-faint);
  }

  .approval-desk__stale {
    margin-left: 6px;
    color: var(--ds-faint);
  }

  /* The "why" line sits between the evidence and the CTA row, so it reads as
     part of the case being made rather than as a third action. Muted, small,
     and underlined on hover only — it must not compete with the primary
     Approve button directly beneath it. */
  .approval-desk__why {
    margin: 22px 0 0;
  }
  .approval-desk__why-btn {
    appearance: none;
    background: none;
    border: none;
    padding: 0;
    margin: 0;
    font-family: var(--ds-font-mono);
    font-size: 12px;
    /* stream-INK — an interactive 12px control cannot sit at raw stream's
       3.42:1 (same ds-qbo reason as .approval-desk__who above). */
    color: var(--ds-stream-ink);
    cursor: pointer;
    text-decoration: none;
  }
  .approval-desk__why-btn:hover {
    text-decoration: underline;
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
    color: var(--ds-fg-soft);
    border: 1px solid var(--ds-border);
  }

  .approval-desk__aud {
    margin-top: 26px;
    font-family: var(--ds-font-mono);
    font-size: 12px;
    color: var(--ds-faint);
  }

  /* Unresolved outcome. Danger accent for `failed`, muted-warning for
     `outcome_unknown` — the visual weight has to differ, or "we could not
     confirm" reads as "it broke" at a glance, which is the exact
     over-claim this state exists to avoid. */
  /* ds-oz2: the accent rule hangs into the deskwrap's 40px gutter instead of
     indenting the text. -20 margin + 3 border + 17 padding nets to zero, so the
     headline and body sit on exactly the same left edge as every other state and
     switching into this one moves no type — the "no state jumps" rule the desk
     is built on. Spacing is px, matching the siblings above; this block used rem
     --ds-sp-* and a warm grey from the other design world, which is what made
     the newest state look bolted on.
     ds-qbo: the body ink moved off --ds-faint (3.08:1) onto --ds-fg-soft
     (6.47:1). This paragraph explains an unresolved rollback — an open loop the
     operator has to act on — so it is body copy, not decoration, and it was the
     one place on the desk where real prose read through the lightest grey. */
  .approval-desk__unresolved {
    position: relative;
    border-left: 3px solid var(--ds-warn, var(--ds-border-strong));
    margin-left: -20px;
    padding-left: 17px;
  }
  .approval-desk__unresolved[data-phase='failed'] {
    border-left-color: var(--ds-danger);
  }
  .approval-desk__unresolved-body {
    margin: 18px 0 26px;
    color: var(--ds-fg-soft);
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
    border-top: 1px solid var(--ds-border);
    border-bottom: 1px solid var(--ds-border);
    box-shadow: none;
    border-radius: 0;
    padding: 11px 0;
    margin: 0;
  }
  .approval-desk__diff :global(.drift-diff-card__label) {
    color: var(--ds-faint);
  }
  .approval-desk__diff :global(.drift-diff-card__table th) {
    color: var(--ds-faint);
    border-bottom-color: var(--ds-border);
  }
  .approval-desk__diff :global(.drift-diff-card__table td) {
    border-bottom-color: var(--ds-border);
    color: var(--ds-fg);
  }
  .approval-desk__diff :global(.ds-code) {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--ds-fg-soft);
  }
</style>
