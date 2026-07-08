<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    apiFetch,
    getStoredToken,
    setToken,
    clearToken,
    type TokenState,
  } from './lib/api';
  import { consumeSse } from './lib/sse';
  import {
    groupOf,
    reconcileBackfill,
    type TraceEvent,
    type TimelineStatus,
  } from './lib/timeline';
  import type {
    Conversation,
    ConversationDetail,
    ConversationTurn,
    ConversationsResponse,
    Decision,
    PrBody,
    TraceResponse,
  } from './lib/types';
  import { nextAppliedWatermark, type AppliedWatermark } from './lib/decision';
  import type { Workload } from './lib/workloads';

  import TokenStatus from './components/TokenStatus.svelte';
  import AuthPanel from './components/AuthPanel.svelte';
  import ChatForm from './components/ChatForm.svelte';
  import TraceBadge from './components/TraceBadge.svelte';
  import FinalResponse from './components/FinalResponse.svelte';
  import IacApprovalCta from './components/IacApprovalCta.svelte';
  import ReplyPending from './components/ReplyPending.svelte';
  import DecisionSummary from './components/DecisionSummary.svelte';
  import PrBodyDisclosure from './components/PrBodyDisclosure.svelte';
  import DriftDiffCard from './components/DriftDiffCard.svelte';
  import HistoricalBanner from './components/HistoricalBanner.svelte';
  import DecisionsRail from './components/DecisionsRail.svelte';
  import ConversationsRail from './components/ConversationsRail.svelte';
  import ConversationThread from './components/ConversationThread.svelte';
  import InfraDiagram from './components/InfraDiagram.svelte';
  import { previewPrFromSearch } from './lib/infra_graph';
  import { initialChatPrefill } from './lib/workloads';
  import type { ChatPrefill } from './lib/workloads';
  import CapabilityCard from './components/CapabilityCard.svelte';
  import PausePill from './components/PausePill.svelte';
  import PauseBanner from './components/PauseBanner.svelte';
  import { createPauseStore } from './lib/pauseStore';
  import AutonomyPill from './components/AutonomyPill.svelte';
  import { createAutonomyStore, autonomyNoteFor } from './lib/autonomyStore';
  import { prefersReducedMotion } from './lib/motion';
  import Timeline from './components/Timeline.svelte';
  import TourBanner from './components/TourBanner.svelte';
  import TourCard from './components/TourCard.svelte';
  import DemoNoticeBell from './components/DemoNoticeBell.svelte';
  import { announceHeaderPopoverOpen } from './lib/headerPopover';
  import { tourDone, markTourDone, shouldOfferTour } from './lib/tour';
  import type { InfraGraph, PendingApproval } from './lib/infra_graph';
  import Icon from './components/Icon.svelte';

  // ---- state ----
  let tokenState = $state<TokenState>(getStoredToken() ? 'ok' : 'missing');
  let events = $state<TraceEvent[]>([]);
  let traceId = $state<string | null>(null);
  let status = $state<TimelineStatus>('pending');
  let finalReply = $state<string | null>(null);
  let finalIsError = $state(false);
  // Set from the `done` frame's `iac_pr` when a run just opened an infra PR —
  // drives the clickable first-authoring "Review & approve" CTA.
  let iacPr = $state<{ pr_number: number; pr_url: string } | null>(null);

  let decisions = $state<Decision[]>([]);

  // ---- multi-turn conversations (P2) ----
  // The history rail's list (metadata only). The currently-open thread's id +
  // crew-lock + rehydrated turns. `conversationId === null` = a fresh, not-yet-
  // persisted chat (today's one-shot behaviour until the first reply lands).
  let conversations = $state<Conversation[]>([]);
  let conversationId = $state<string | null>(null);
  let conversationWorkload = $state<Workload | null>(null);
  let conversationTurns = $state<ConversationTurn[]>([]);
  // The composer's selected crew, lifted out of ChatForm so resuming a thread
  // can snap it to that thread's locked crew (bind:workload on ChatForm).
  let composerWorkload = $state<Workload>('drift');

  // Bumps when a freshly-`applied` iac_apply decision is observed in /decisions
  // — drives InfraDiagram's delayed resource-map re-fetches (rides out CAI lag).
  // The watermark SEEDS on the first load without bumping (lib/decision —
  // a boot-time historical applied decision must not ride the refresh ladder).
  let appliedEpoch = $state(0);
  let appliedWatermark: AppliedWatermark = { id: null, seeded: false };

  // ?preview_pr=N (linked from the IaC approval page) → the Infrastructure panel
  // opens in ghost-node preview mode. Parsed once at boot; only ever cleared.
  let previewPr = $state(previewPrFromSearch(window.location.search));

  // Clear the preview and remove ONLY the preview_pr param (preserve any other
  // query params and the hash) so a reload/share doesn't resurrect the preview.
  function exitPreview() {
    previewPr = null;
    const u = new URL(window.location.href);
    u.searchParams.delete('preview_pr');
    history.replaceState(null, '', u);
  }

  let historicalActive = $state(false);
  let historicalTraceId = $state<string | null>(null);
  let activeTraceId = $state<string | null>(null);
  // The decision doc of the trace being replayed — drives the DecisionSummary
  // card when the replayed decision carries no prose (e.g. an iac_apply).
  let historicalDecision = $state<Decision | null>(null);
  // The agent-authored PR body for an iac_apply replay (fetched lazily from
  // /trace/{id}/pr-body) — drives the "what this change did" disclosure. null
  // hides the panel (no description / fail-soft miss).
  let historicalPrBody = $state<string | null>(null);
  let historicalPrBodyTruncated = $state(false);

  let authPanelOpen = $state(false);
  let authResolver: ((t: string | null) => void) | null = null;
  // Single-flight: concurrent callers (loadDecisions + InfraDiagram both fetch
  // on mount, and either may 401) share ONE prompt and one resolution. Without
  // this, a second requestToken() overwrites the first's resolver and strands
  // the first in-flight request forever (Codex review).
  let authPromise: Promise<string | null> | null = null;

  // Concurrency guard: a monotonically-incrementing run id. submitChat /
  // openTrace / newChat each bump it; in-flight callbacks bail at every await
  // boundary when their captured id is stale, so a slow first stream can't
  // append into (or backfill over) a newer run. `busy` also disables Send.
  let runSeq = 0;
  let busy = $state(false);

  // The ONE chat-disabled condition (busy live stream OR historical replay), shared
  // by ChatForm.disabled AND InfraDiagram.adoptDisabled so the two can never diverge
  // — an Adopt click can never mutate a disabled composer or strand a stale draft
  // behind a historical view (Codex review 019eb572 must-fix 3).
  const chatDisabled = $derived(historicalActive || busy);

  // ---- chat-native live exchange ----
  // While a live /chat turn is in flight (or its reply just landed but hasn't
  // settled into the thread yet), render the exchange THROUGH the thread as an
  // optimistic user + crew bubble pair instead of the standalone hero. The crew
  // bubble reads `finalReply` live, so the reply fills that same bubble the
  // instant the `done` frame arrives — the existing backfill/loadDecisions
  // latency before settle is no longer visible (no blue→green swap, no upward
  // hop). Captured (not reactive) at submit time so the bubble keys/labels stay
  // stable for the whole run. Cleared the MOMENT a non-persistable outcome is
  // known (paused / one-shot / error) so those fall back to the hero without
  // flashing in a bubble first — see the clear points in submitChat.
  type LiveExchange = { prompt: string; workload: Workload; baseSeq: number };
  let liveExchange = $state<LiveExchange | null>(null);

  // The thread's rendered turns: the persisted turns plus, during a live run,
  // the optimistic exchange. `baseSeq` mirrors appendLocalTurns
  // (conversationTurns.length at submit), so when settle appends the real turns
  // the keys are identical and the keyed {#each} updates in place instead of
  // remounting — the transient bubble becomes the persisted one with no visual
  // change.
  const displayTurns = $derived.by((): ConversationTurn[] => {
    if (liveExchange == null) return conversationTurns;
    const { prompt, workload, baseSeq } = liveExchange;
    return [
      ...conversationTurns,
      { seq: baseSeq, role: 'user', text: prompt, workload, trace_id: traceId, optimistic: true },
      {
        seq: baseSeq + 1,
        role: 'crew',
        text: finalReply ?? '',
        workload,
        trace_id: traceId,
        iac_pr: iacPr,
        optimistic: true,
        pending: finalReply == null,
      },
    ];
  });
  // Historical replay must always show the hero, never a live bubble; openTrace
  // clears liveExchange, so both-true is impossible — this guard is belt.
  const liveExchangeActive = $derived(!historicalActive && liveExchange != null);

  // Crew-lock context: an open thread — or the in-flight first exchange — pins
  // the composer to one crew; CrewPicker greys out the rest. null = no lock.
  const lockedCrew = $derived<Workload | null>(
    conversationWorkload ?? liveExchange?.workload ?? null,
  );
  // The composer's New chat button shows whenever a clean slate would clear
  // something. displayTurns already unifies "persisted thread + optimistic
  // in-flight exchange" (reuse it — one source of thread visibility, no drift);
  // finalReply/busy/events cover the hero + timeline-only states (a paused /
  // one-shot / error reply persists no thread but still occupies the hero), and
  // conversationId is a belt for an open-but-empty thread edge. Hidden in
  // historical replay — the banner owns the exit there.
  const composerNewChat = $derived(
    !historicalActive &&
      (conversationId !== null ||
        displayTurns.length > 0 ||
        finalReply !== null ||
        busy ||
        events.length > 0),
  );

  // Adopt-button bridge + ?ask_pr boot seed (item 12): an Adopt click — or
  // arriving from the approval page's "ask about this change" link — prefills
  // (NOT sends) the composer. epoch bumps so the same/another Adopt re-applies
  // after an edit; a boot seed starts at epoch 1, so a later Adopt bumps to 2.
  let chatPrefill = $state<ChatPrefill | null>(
    initialChatPrefill(window.location.search)
  );
  function handleAdopt(text: string) {
    // Adopt starts a NEW provisioning task, so ALWAYS drop to a clean slate
    // first: on an open thread the provision prefill would otherwise fight the
    // crew lock (CrewPicker snaps the value straight back), and leftover
    // one-shot output shouldn't sit around a fresh task either. On an already-
    // fresh composer this is a harmless no-op (Adopt is disabled during busy/
    // historical, so there is never a live stream to cancel). The old thread
    // stays reachable from the rail.
    newChat();
    chatPrefill = { text, workload: 'provision', epoch: (chatPrefill?.epoch ?? 0) + 1 };
    // Bring the composer into view so the prefilled draft is obvious. Best-effort:
    // the element exists in the live tree; guarded for the historical/SSR-less case.
    document.getElementById('chat-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // Onboarding tour (item 14). The offer is decided ONCE at boot — before
  // onMount strips the intent params — and the header Tour button is the
  // permanent reopen path. Closing OR dismissing marks the tour done; the
  // flag is a UI preference, so localStorage (not sessionStorage) is right.
  let tourGraph = $state<InfraGraph | null>(null);
  // Lifted alongside tourGraph (InfraDiagram.onPending) so the tour's first-adoption
  // suggestion skips a resource that already has an open adoption PR.
  let tourPending = $state<PendingApproval[]>([]);
  let tourOpen = $state(false);
  let tourOffered = $state(shouldOfferTour(window.location.search, tourDone()));
  function startTour(): void {
    tourOffered = false;
    tourOpen = true;
    // The tour's "controls" step spotlights the header corner the popovers hang
    // from — close them (notice included) before the spotlight lands. Pause and
    // Autonomy keep their saving exception: mid-POST they ignore this, and that
    // transient overlap is accepted (documented invariant).
    announceHeaderPopoverOpen('tour');
  }
  function dismissTourOffer(): void {
    tourOffered = false;
    markTourDone();
  }
  function closeTour(): void {
    tourOpen = false;
    markTourDone();
  }

  // ---- auth plumbing (replaces window.prompt) ----
  function requestToken(): Promise<string | null> {
    if (authPromise) return authPromise; // reuse the in-flight prompt
    authPanelOpen = true;
    authPromise = new Promise((resolve) => {
      authResolver = resolve;
    });
    return authPromise;
  }
  function settleAuth(token: string | null) {
    const r = authResolver;
    authResolver = null;
    authPromise = null;
    r?.(token);
  }
  function onAuthSubmit(token: string) {
    authPanelOpen = false;
    setToken(token);
    tokenState = 'ok';
    settleAuth(token);
  }
  function onAuthCancel() {
    authPanelOpen = false;
    tokenState = getStoredToken() ? 'ok' : 'missing';
    settleAuth(null);
  }
  function onChangeToken() {
    clearToken();
    tokenState = 'missing';
    void requestToken();
  }

  // ---- request wrapper that keeps the token pill honest ----
  async function call(path: string, init?: RequestInit): Promise<Response> {
    const resp = await apiFetch(path, init, requestToken);
    if (resp.ok) {
      if (getStoredToken()) tokenState = 'ok';
    } else if (resp.status === 401 || resp.status === 403) {
      tokenState = getStoredToken() ? 'invalid' : 'missing';
    }
    return resp;
  }

  // ---- pause kill-switch (one shared store → header PausePill + content
  // PauseBanner, so the two surfaces can never diverge or double-fetch) ----
  const pause = createPauseStore(call);

  // ---- autonomy dial (one shared store → header AutonomyPill + the capability
  // card note, so the two surfaces never diverge or double-fetch) ----
  const autonomy = createAutonomyStore(call);
  const capabilityAutonomyNote = $derived(autonomyNoteFor($autonomy));

  // ---- decisions rail ----
  async function loadDecisions() {
    try {
      const resp = await call('/decisions?limit=50');
      if (!resp.ok) return;
      const body = await resp.json();
      if (Array.isArray(body?.decisions)) {
        decisions = body.decisions as Decision[];
        noteApplied(decisions);
      }
    } catch {
      /* best-effort */
    }
  }

  // Detect a freshly-`applied` iac_apply decision (decisions arrive newest-first)
  // so the Infrastructure panel can refresh the resource map after an apply lands.
  // Pure logic lives in lib/decision.nextAppliedWatermark (boot-seed semantics).
  function noteApplied(ds: Decision[]) {
    const { next, bump } = nextAppliedWatermark(appliedWatermark, ds);
    appliedWatermark = next;
    if (bump) appliedEpoch += 1;
  }

  const asString = (v: unknown): string | null =>
    typeof v === 'string' && v.length > 0 ? v : null;

  // ---- conversations rail + thread (P2) ----
  // List of recent conversations for the rail (metadata only). Mirrors
  // loadDecisions: best-effort, single-flight-friendly, refreshed at mount and
  // after each successful chat turn (a new/updated thread re-sorts to the top).
  async function loadConversations() {
    try {
      const resp = await call('/conversations?limit=50');
      if (!resp.ok) return;
      const body = (await resp.json()) as ConversationsResponse;
      if (Array.isArray(body?.conversations)) {
        conversations = body.conversations;
      }
    } catch {
      /* best-effort */
    }
  }

  // Resume a conversation from the rail: load its full ordered turns and snap
  // the composer to its locked crew so the next prompt continues the thread.
  // Bumps runSeq (cancels any in-flight live stream / historical replay) and
  // clears the live-run surfaces, then scrolls the thread into view. Guarded so
  // a superseding open/newChat drops a late response.
  async function openConversation(id: string) {
    const myRun = ++runSeq;
    busy = false;
    historicalActive = false;
    historicalTraceId = null;
    activeTraceId = null;
    historicalDecision = null;
    historicalPrBody = null;
    historicalPrBodyTruncated = false;
    traceId = null;
    events = [];
    finalReply = null;
    finalIsError = false;
    iacPr = null;
    liveExchange = null; // cancel any in-flight optimistic exchange
    status = 'pending';
    conversationId = id;
    // Clear the prior thread's crew NOW so a failed rehydrate can't leave a
    // stale lock paired with the new id (which would slip the crew-change guard
    // and 409 on the next submit). Re-set from the detail on success.
    conversationWorkload = null;
    conversationTurns = [];
    try {
      const resp = await call('/conversations/' + encodeURIComponent(id));
      if (myRun !== runSeq) return;
      if (!resp.ok) {
        // Abandon the half-open thread — don't leave an id with no crew/turns.
        conversationId = null;
        return;
      }
      const detail = (await resp.json()) as ConversationDetail;
      if (myRun !== runSeq) return;
      conversationTurns = Array.isArray(detail.turns) ? detail.turns : [];
      const wl = detail.workload as Workload | undefined;
      if (wl) {
        conversationWorkload = wl;
        composerWorkload = wl; // land the composer on this thread's locked crew
      }
      // Auto-load the latest turn's reasoning into the INLINE timeline so a
      // resumed conversation shows its coordinator reasoning / tools / MCP
      // without the extra per-turn "open trace" click. Prefer the last CREW turn
      // that carries a trace (the "open trace" affordance lives on crew bubbles),
      // then any turn, then the conversation's last_trace_id. Fire-and-forget:
      // it fills the timeline in a beat later (like a lazy load) while the
      // tick()+scroll below runs; runSeq-guarded so a superseding open drops it.
      const crewTid = [...conversationTurns].reverse().find((t) => t.role === 'crew' && t.trace_id)?.trace_id;
      const anyTid = crewTid ?? [...conversationTurns].reverse().find((t) => t.trace_id)?.trace_id;
      const inlineTid = anyTid ?? detail.last_trace_id ?? null;
      if (inlineTid) void loadConversationTrace(inlineTid, myRun);
    } catch {
      // A failed rehydrate abandons the thread rather than leaving it half-open.
      if (myRun === runSeq) conversationId = null;
    }
    await tick();
    if (myRun !== runSeq) return;
    // Scroll the COMPOSER into view (not the thread top) so it stays on screen —
    // the rehydrated history flows directly below it. Then move focus into the
    // thread region (tabindex=-1) so keyboard / screen-reader users are told the
    // conversation loaded, instead of being stranded on the rail button — the
    // same scroll-then-focus pattern openTrace uses for #historical-badge.
    const reduced = prefersReducedMotion();
    document
      .getElementById('chat-form')
      ?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    document.getElementById('conversation-thread')?.focus({ preventScroll: true });
  }

  // Load a resumed conversation's latest-turn trace into the INLINE timeline
  // (the thread stays visible — this is NOT the full-page historical replay that
  // openTrace does). historicalActive stays false; the two are independent axes
  // ("inline vs full replay" vs "live vs snapshot"), so status='historical' here
  // only labels the snapshot (green pill) — it does not relocate the output or
  // hide the thread. Fail-soft + runSeq-guarded like loadPrBody. Promote ONLY
  // when the trace carries a DISPLAYABLE event (groupOf !== null): Timeline drops
  // final_response / unknown kinds, so a reply-only trace has events.length > 0
  // yet would render three empty group accordions — the exact confusion we fix.
  async function loadConversationTrace(tid: string, myRun: number) {
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid));
      if (myRun !== runSeq || !resp.ok) return;
      const t = (await resp.json()) as TraceResponse;
      if (myRun !== runSeq) return;
      const evts = Array.isArray(t.events) ? t.events : [];
      if (!evts.some((e) => groupOf(e) !== null)) return; // no reasoning to show
      // Set the three together after the final guard so a superseding run can
      // never see a half-applied inline trace.
      events = evts;
      traceId = tid;
      status = 'historical';
    } catch {
      /* fail-soft — leave the empty-timeline affordance (same as the homepage) */
    }
  }

  // Append the just-completed exchange to the open thread optimistically — we
  // already hold the prompt + reply, so there's no need to re-fetch the whole
  // conversation. Called ONLY when persistence succeeded (the coordinator
  // echoed conversation_id), so the local thread never drifts ahead of the
  // store. The user+crew pair mirrors the backend's persisted turn shape.
  function appendLocalTurns(prompt: string, reply: string | null, tid: string | null) {
    const base = conversationTurns.length;
    const crew = conversationWorkload ?? composerWorkload;
    const userTurn: ConversationTurn = {
      seq: base, role: 'user', text: prompt, workload: crew, trace_id: tid,
    };
    const crewTurn: ConversationTurn = {
      seq: base + 1, role: 'crew', text: reply ?? '', workload: crew,
      trace_id: tid, iac_pr: iacPr,
    };
    conversationTurns = [...conversationTurns, userTurn, crewTurn];
  }

  // ---- live chat (SSE) ----
  async function submitChat(prompt: string, workload: Workload) {
    if (historicalActive || busy) return;
    const myRun = ++runSeq;
    busy = true;
    events = [];
    traceId = null;
    finalReply = null;
    finalIsError = false;
    iacPr = null;
    historicalDecision = null;
    historicalPrBody = null;
    historicalPrBodyTruncated = false;
    status = 'pending';

    // Threads are crew-locked. If the operator switched crews on an open
    // thread, start a NEW conversation instead of sending the locked id (which
    // the backend would 409) — the old thread stays in the rail. Detect before
    // we send; clear the displayed thread since it belonged to the other crew.
    if (
      conversationId !== null &&
      conversationWorkload !== null &&
      workload !== conversationWorkload
    ) {
      conversationId = null;
      conversationWorkload = null;
      conversationTurns = [];
    }
    const sendConversationId = conversationId;

    // Render this turn through the thread from the moment Send is pressed: an
    // optimistic user bubble + a "thinking" crew bubble that fills with the
    // reply in place. baseSeq is captured AFTER the crew-switch reset above so
    // it reflects the (possibly cleared) thread and matches appendLocalTurns.
    liveExchange = { prompt, workload, baseSeq: conversationTurns.length };

    // Once the coordinator echoes a conversation_id (persist succeeded), fold
    // the exchange into the open thread and clear the optimistic overlay — the
    // transient bubble becomes the persisted one with no visual change. rcid
    // absent (one-shot / paused / error) → the optimistic bubble was already
    // dropped at the terminal point, so just belt-and-suspenders clear here.
    const settleConversation = (rcid: string | undefined) => {
      if (myRun !== runSeq) return;
      if (typeof rcid !== 'string' || rcid.length === 0) {
        liveExchange = null;
        return;
      }
      conversationId = rcid;
      conversationWorkload = workload;
      appendLocalTurns(prompt, finalReply, traceId);
      // Clear the overlay right after the real turns are appended, BEFORE
      // clearing finalReply/iacPr, so a mid-settle read of displayTurns is never
      // half-applied (the persisted turns already carry the reply).
      liveExchange = null;
      finalReply = null; // now the last bubble in the thread above
      finalIsError = false;
      iacPr = null; // the thread's crew bubble carries the PR CTA
      void loadConversations(); // the new/updated thread floats to the rail top
    };

    try {
      let resp: Response;
      try {
        resp = await call('/chat', {
          method: 'POST',
          headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
          body: JSON.stringify(
            sendConversationId
              ? { prompt, workload, conversation_id: sendConversationId }
              : { prompt, workload },
          ),
        });
      } catch {
        if (myRun !== runSeq) return;
        status = 'error';
        finalReply = 'Network error contacting the coordinator.';
        finalIsError = true;
        liveExchange = null; // nothing persisted → the error belongs in the hero
        return;
      }
      if (myRun !== runSeq) return;

      if (!resp.ok) {
        status = 'error';
        // 429 comes from the demo-window per-IP rate limiter (CF Worker);
        // judges should see "wait", not a bare status code.
        finalReply =
          resp.status === 429
            ? 'Rate limit reached. The demo allows a few chat runs per minute per visitor. Please wait a moment and try again.'
            : `Request failed (${resp.status}).`;
        finalIsError = true;
        liveExchange = null; // nothing persisted → the error belongs in the hero
        return;
      }

      const ctype = resp.headers.get('content-type') ?? '';
      if (!ctype.includes('text/event-stream')) {
        // Fallback: non-streaming JSON {reply, tool_calls}. The backfill below
        // still pulls the full timeline (incl. mcp_call) from /trace.
        try {
          const body = await resp.json();
          if (myRun !== runSeq) return;
          traceId = resp.headers.get('X-Trace-Id');
          finalReply = typeof body?.reply === 'string' ? body.reply : JSON.stringify(body);
          // Best-effort: the JSON path mirrors the SSE done frame's iac_pr.
          const ip = body?.iac_pr;
          iacPr =
            ip && typeof ip === 'object' && typeof ip.pr_number === 'number'
              ? {
                  pr_number: ip.pr_number,
                  pr_url: typeof ip.pr_url === 'string' ? ip.pr_url : '',
                }
              : null;
          status = 'complete';
          // Mirror the SSE done frame: the JSON path echoes conversation_id
          // when the turn persisted. Decide persistability NOW (a paused refusal
          // echoes conversation_id but persists nothing; a one-shot has none) so
          // a non-persistable reply drops the optimistic bubble immediately and
          // falls back to the hero, instead of flashing in a bubble across the
          // backfill/decisions round-trips that precede settle.
          const jsonRcid =
            !body?.paused &&
            typeof body?.conversation_id === 'string' &&
            body.conversation_id.length > 0
              ? body.conversation_id
              : undefined;
          if (jsonRcid === undefined) liveExchange = null;
          await backfillTrace(myRun);
          if (myRun !== runSeq) return;
          await loadDecisions();
          settleConversation(jsonRcid);
          return;
        } catch {
          if (myRun !== runSeq) return;
          status = 'error';
          finalReply = 'Malformed response.';
          finalIsError = true;
          liveExchange = null; // nothing persisted → the error belongs in the hero
        }
        await backfillTrace(myRun);
        if (myRun === runSeq) await loadDecisions();
        return;
      }

      let streamErrored = false;
      let doneConversationId: string | undefined;
      try {
        await consumeSse(resp, {
          onMeta: (m) => {
            if (myRun !== runSeq) return;
            traceId = m.trace_id;
            status = 'streaming';
          },
          onEvent: (e) => {
            if (myRun !== runSeq) return;
            events = [...events, e as unknown as TraceEvent];
          },
          onDone: (d) => {
            if (myRun !== runSeq) return;
            finalReply = d.reply;
            finalIsError = false;
            iacPr = d.iac_pr ?? null;
            // A paused refusal echoes conversation_id for crew-lock symmetry but
            // persists NO turn — never settle it into the thread (it would
            // vanish on reload); leave the calm paused reply in the hero.
            doneConversationId = d.paused ? undefined : d.conversation_id;
            // Non-persistable (paused refusal or one-shot with no
            // conversation_id): drop the optimistic bubble now so the reply
            // lands in the hero and never flashes in a bubble during the
            // post-stream backfill. The persistable case keeps the bubble (the
            // reply fills it in place) until settle promotes it.
            if (typeof doneConversationId !== 'string' || doneConversationId.length === 0) {
              liveExchange = null;
            }
            status = 'complete';
          },
          onError: (er) => {
            if (myRun !== runSeq) return;
            finalReply = er.detail || 'The coordinator returned an error.';
            finalIsError = true;
            status = 'error';
            liveExchange = null; // errored turn persists nothing → hero
          },
        });
      } catch {
        // Stream transport error (reader threw / body errored mid-stream).
        if (myRun !== runSeq) return;
        streamErrored = true;
      }

      // One post-stream backfill (also the recovery path on transport error):
      // pulls side-channel mcp_call events not carried on the stream +
      // reconciles ordering (mirrors the legacy UI).
      await backfillTrace(myRun);
      if (myRun !== runSeq) return;
      // finalReply is set by both onDone and onError. If we reach here with it
      // still null, the stream produced neither a `done` nor an `error` frame —
      // either it broke mid-transport (streamErrored) or it closed cleanly on
      // EOF without ever emitting a final reply. Either way, never leave the
      // answer area empty: surface a recoverable error after the backfill so the
      // loading shimmer resolves to a message instead of a blank hero.
      if (finalReply == null) {
        status = 'error';
        finalReply = streamErrored
          ? 'The reasoning stream was interrupted. Showing the recovered trace.'
          : 'The reasoning stream ended before a final reply arrived.';
        finalIsError = true;
        liveExchange = null; // interrupted stream persists nothing → hero
      }
      await loadDecisions();
      // Settle AFTER the recovery guard so a stream that produced a real reply
      // (doneConversationId set) folds into the thread; an interrupted stream
      // leaves doneConversationId undefined and the error stays in the hero.
      settleConversation(doneConversationId);
    } finally {
      if (myRun === runSeq) busy = false;
    }
  }

  async function backfillTrace(myRun: number) {
    const tid = traceId;
    if (!tid || myRun !== runSeq) return;
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid));
      if (myRun !== runSeq || !resp.ok) return;
      const t = (await resp.json()) as TraceResponse;
      if (myRun !== runSeq) return;
      // MERGE the ingestion-lagged /trace snapshot into the live timeline,
      // never overwrite it: the live stream already rendered every kind except
      // the trace-only mcp_call side-channel, and a too-early /trace can be
      // incomplete (or hold only log lines). reconcileBackfill pulls the
      // mcp_call events and falls back to /trace only when the stream produced
      // nothing displayable. See lib/timeline.ts.
      events = reconcileBackfill(events, Array.isArray(t.events) ? t.events : []);
    } catch {
      /* backfill is best-effort — the live stream already populated the timeline */
    }
  }

  // ---- historical replay ----
  async function openTrace(tid: string) {
    const myRun = ++runSeq; // cancels any in-flight live stream
    busy = false;
    historicalActive = true;
    historicalTraceId = tid;
    activeTraceId = tid;
    traceId = tid;
    events = [];
    finalReply = null;
    finalIsError = false;
    iacPr = null;
    liveExchange = null; // a replay always shows the hero, never a live bubble
    historicalDecision = null;
    historicalPrBody = null;
    historicalPrBodyTruncated = false;
    status = 'pending';
    // When historicalActive flips true the replay renders at the TOP of the chat
    // column (see the {#if historicalActive}{@render traceOutput()} branch), so
    // bringing the page to the top reveals it — no jarring jump down. Scroll the
    // WINDOW (the chat column is page-flow, not its own scroll container) rather
    // than scrollIntoView. await tick() first so the {#if active} block has
    // flushed and #historical-badge exists for the focus() below. Scrolling here
    // (pre-fetch) gives instant feedback; the trace body fills in above as it
    // resolves.
    await tick();
    if (myRun !== runSeq) return; // a newer run superseded us during the tick
    window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    // Move focus into the replay region (banner is tabindex=-1) so keyboard/SR
    // users land in the new content instead of being stranded on the rail button.
    // preventScroll: the window.scrollTo above already positioned the viewport;
    // don't let focus fight it with a second jump.
    const banner = document.getElementById('historical-badge');
    banner?.focus({ preventScroll: true });
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid));
      if (myRun !== runSeq) return;
      if (resp.ok) {
        const t = (await resp.json()) as TraceResponse;
        if (myRun !== runSeq) return;
        events = Array.isArray(t.events) ? t.events : [];
        // Surface the decision's prose in the hero card (legacy parity).
        const d = t.decision as Record<string, unknown> | null | undefined;
        finalReply = d ? asString(d.rationale) ?? asString(d.rendered_body) : null;
        finalIsError = false;
        // The replayed decision drives the DecisionSummary card when it has no
        // prose (e.g. an iac_apply — see the {#if} in the template).
        historicalDecision = (t.decision as Decision) ?? null;
        // A replay is a snapshot, NOT a live stream — always 'historical'.
        // (Deriving from t.complete would mislabel as 'streaming': iac_apply
        // traces never have a final_response, and a cold post-restart
        // observation cache returns complete=false on a single fetch.)
        status = 'historical';
        // Open-trace PR-body disclosure (iac_apply only): fetch the agent-authored
        // PR description for the "what this change did" panel. A separate,
        // fail-soft call — a miss/error just leaves the panel hidden. Fire-and-
        // forget (the panel fills in when it resolves), runSeq-guarded so a
        // superseding open-trace/newChat drops a late response.
        if (historicalDecision?.action === 'iac_apply') {
          void loadPrBody(tid, myRun);
        }
      } else {
        historicalDecision = null;
        status = 'error';
      }
    } catch {
      if (myRun === runSeq) {
        historicalDecision = null;
        status = 'error';
      }
    }
  }

  // Fetch the agent-authored PR body for an iac_apply replay. Fail-soft: any
  // miss/non-ok/error/throw just leaves historicalPrBody null (panel hidden).
  // runSeq-guarded at every await so a superseding open-trace/newChat wins.
  async function loadPrBody(tid: string, myRun: number) {
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid) + '/pr-body');
      if (myRun !== runSeq || !resp.ok) return;
      const b = (await resp.json()) as PrBody;
      if (myRun !== runSeq) return;
      // Trim ONLY for the empty-check (a whitespace-only body has no content to
      // show); keep the original body so edge whitespace in a real description
      // survives in the <pre>.
      historicalPrBody =
        typeof b.body === 'string' && b.body.trim() ? b.body : null;
      historicalPrBodyTruncated = b.body_truncated === true;
    } catch {
      /* fail-soft — leave the disclosure hidden */
    }
  }

  function newChat() {
    ++runSeq; // cancel any in-flight live stream
    busy = false;
    historicalActive = false;
    historicalTraceId = null;
    activeTraceId = null;
    traceId = null;
    events = [];
    finalReply = null;
    finalIsError = false;
    iacPr = null;
    liveExchange = null; // clean slate — no lingering optimistic exchange
    historicalDecision = null;
    historicalPrBody = null;
    historicalPrBodyTruncated = false;
    status = 'pending';
    // Drop out of the open thread too — "new chat" is a clean slate. The thread
    // is still reachable from the rail (its id lives in /conversations).
    conversationId = null;
    conversationWorkload = null;
    conversationTurns = [];
  }

  onMount(() => {
    void loadDecisions();
    void loadConversations();
    void pause.fetchPause();
    void autonomy.fetchAutonomy();
    if (chatPrefill !== null) {
      // Remove ONLY ask_pr (preserve other params + hash) so reload/share
      // doesn't re-prefill — mirrors exitPreview()'s surgical removal.
      const u = new URL(window.location.href);
      u.searchParams.delete('ask_pr');
      history.replaceState(null, '', u);
      document.getElementById('chat-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });
</script>

<header class="app-header">
  <div class="app-header__brand">
    <span class="app-logo-mark" aria-hidden="true">
      <Icon name="radar" size={16} extraClass="app-logo-mark__icon" />
    </span>
    <h1 class="app-title">DriftScribe<span class="app-title__sub">. The agent proposes, you approve.</span></h1>
  </div>
  <div class="app-header__actions">
    <!-- Judging-window notice bell (replaces the in-flow DemoNoticeBanner; see
         docs/plans/2026-07-07-demo-notice-bell.md). Deleted whole at
         close-window time. -->
    <DemoNoticeBell />
    <!-- data-tour="controls" lives on this always-rendered wrapper (not the
         loaded-only pill button) so the tour spotlight resolves even while
         /autonomy is loading or unknown. -->
    <div class="header-tour-anchor" data-tour="controls">
      <AutonomyPill {autonomy} />
    </div>
    <PausePill {pause} />
    <button
      class="ds-btn ds-btn--ghost app-tour-btn"
      type="button"
      data-testid="tour-open"
      onclick={startTour}><Icon name="compass" size={14} />Tour</button
    >
    <TokenStatus state={tokenState} onChange={onChangeToken} />
  </div>
</header>

<!-- The trace/replay output cluster. Each child's own {#if} already gates on
     historicalActive, so the same snippet renders correctly whether it sits at
     the TOP (historical replay) or the BOTTOM (live chat) of the chat column —
     the only thing that changes is WHERE it mounts. Mutually-exclusive call
     sites below ({#if historicalActive} vs {#if !historicalActive}) mean only
     one instance is ever live, so there is no double-mount. Rendered inline, so
     the components stay direct children of .chat-area (the margin rule applies). -->
{#snippet traceOutput()}
  <HistoricalBanner active={historicalActive} traceId={historicalTraceId} onNewChat={newChat} />
  <TraceBadge {traceId} {status} />
  <!-- During a live chat the reply lands in the thread's crew bubble (see
       displayTurns), so the standalone hero + its loading shimmer yield. They
       stay for historical replay and the non-persist fallback (paused / one-shot
       / error), where liveExchange is cleared and there is no bubble to hold the
       reply. -->
  {#if !liveExchangeActive}
    <FinalResponse reply={finalReply} isError={finalIsError} />
  {/if}
  {#if historicalActive && historicalDecision}
    <DriftDiffCard decision={historicalDecision} />
  {/if}
  {#if iacPr && !historicalActive}
    <IacApprovalCta prNumber={iacPr.pr_number} />
  {/if}
  {#if busy && finalReply == null && !liveExchangeActive}
    <ReplyPending />
  {/if}
  {#if historicalActive && finalReply == null && historicalDecision}
    <DecisionSummary decision={historicalDecision} />
    <PrBodyDisclosure body={historicalPrBody} truncated={historicalPrBodyTruncated} />
  {/if}
  <!-- directlyRecorded gates the empty-timeline copy: only an iac_apply is
       recorded directly (worker-written, no coordinator reasoning run), so an
       empty trace there is expected. Chat turns / other decisions have no
       decision doc or a real reasoning run, so their empty trace means
       "couldn't load", not "never reasoned". -->
  <Timeline {events} {status} directlyRecorded={historicalDecision?.action === 'iac_apply'} />
{/snippet}

<main class="layout">
  <div class="rails">
    <ConversationsRail
      {conversations}
      activeConversationId={conversationId}
      onOpen={openConversation}
    />
    <DecisionsRail {decisions} {activeTraceId} onOpenTrace={openTrace} />
  </div>

  <section id="chat-area" class="chat-area" aria-label="Chat and reasoning timeline">
    <!-- Historical replay renders FIRST so an opened trace lands at the top of
         the chat column (openTrace scrolls the window to top to reveal it). -->
    {#if historicalActive}
      {@render traceOutput()}
    {/if}
    {#if tourOffered && !tourOpen}
      <TourBanner onStart={startTour} onDismiss={dismissTourOffer} />
    {/if}
    <!-- The autonomy dial moved to the header pill; the "controls" spotlight
         marker moved with it. PauseBanner stays here (only shown when paused). -->
    <PauseBanner {pause} />
    <div class="tour-target" data-tour="estate">
      <InfraDiagram
        {call}
        {appliedEpoch}
        {previewPr}
        onExitPreview={exitPreview}
        onAdopt={handleAdopt}
        adoptDisabled={chatDisabled}
        onGraph={(g) => (tourGraph = g)}
        onPending={(a) => (tourPending = a)}
      />
    </div>
    <CapabilityCard {call} autonomyNote={capabilityAutonomyNote} />
    <div class="tour-target" data-tour="composer">
      <ChatForm
        disabled={chatDisabled}
        onSubmit={submitChat}
        prefill={chatPrefill}
        bind:workload={composerWorkload}
        lockedCrew={lockedCrew}
        showNewChat={composerNewChat}
        onNewChat={newChat}
      />
    </div>
    {#if !historicalActive && displayTurns.length > 0}
      <ConversationThread turns={displayTurns} onOpenTrace={openTrace} />
    {/if}
    <!-- Live chat output stays BELOW the composer (the natural type-then-stream
         flow); the historical branch above relocates it to the top instead. -->
    {#if !historicalActive}
      {@render traceOutput()}
    {/if}
  </section>
</main>

<AuthPanel open={authPanelOpen} onSubmit={onAuthSubmit} onCancel={onAuthCancel} />

{#if tourOpen}
  <TourCard
    graph={tourGraph}
    pendingApprovals={tourPending}
    adoptDisabled={chatDisabled}
    onAdoptPrefill={handleAdopt}
    onClose={closeTour}
  />
{/if}

<style>
  .app-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-3) var(--ds-sp-6);
    border-bottom: 1px solid var(--ds-border);
    background: var(--ds-surface);
    box-shadow: var(--ds-shadow-sm);
  }
  .app-header__brand {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }
  .app-logo-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 6px;
    background: var(--ds-stream-surface);
    color: var(--ds-stream-ink);
    flex-shrink: 0;
  }
  .app-logo-mark :global(.app-logo-mark__icon) {
    display: block;
  }
  .app-header__actions {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
    /* Several controls now live here (notice bell, autonomy, pause, tour,
       token) — let the cluster wrap to a second line on narrow viewports rather
       than overflow (Codex #6). */
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .header-tour-anchor {
    display: inline-flex;
    align-items: center;
  }
  /* Give the safety controls room before shrinking them: drop the title
     subtitle first on narrow screens (Codex #6). */
  @media (max-width: 640px) {
    .app-title__sub {
      display: none;
    }
  }
  .app-tour-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
  }
  .app-title {
    font-size: var(--ds-fs-3);
    font-weight: var(--ds-fw-bold);
    letter-spacing: -0.01em;
    margin: 0;
  }
  .app-title__sub {
    color: var(--ds-muted);
    font-weight: var(--ds-fw-normal);
  }
  .layout {
    display: grid;
    grid-template-columns: 280px minmax(0, 1fr);
    align-items: start;
    min-height: calc(100vh - 56px);
  }
  /* Left column holds two stacked rails: conversation history above past
     decisions. Each owns its own internal scroll; the column spaces + insets
     them so neither hugs the very edge. */
  .rails {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-6);
    padding: var(--ds-sp-5) var(--ds-sp-3) var(--ds-sp-8) var(--ds-sp-5);
    min-height: 0;
  }
  .chat-area {
    padding: var(--ds-sp-5) var(--ds-sp-6) var(--ds-sp-8);
    max-width: var(--ds-page-max);
  }
  .chat-area > :global(*) {
    margin-bottom: var(--ds-sp-4);
  }
  /* Wrappers exist only as [data-tour] spotlight targets. flow-root makes
     each wrapper a BFC so child margins cannot collapse outside it — the
     spotlight outline must hug the real panels (Codex MF4). The `* + *`
     rule restores the inter-component spacing the children lost by no
     longer being .chat-area direct children. */
  .tour-target {
    display: flow-root;
  }
  .tour-target > :global(* + *) {
    margin-top: var(--ds-sp-4);
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
    /* Single column: put the chat + composer FIRST so the operator isn't forced
       to scroll past the full conversations + decisions lists to reach it. The
       rails (history / past decisions) drop below as secondary navigation. */
    .chat-area {
      order: 1;
    }
    .rails {
      order: 2;
    }
  }
</style>
