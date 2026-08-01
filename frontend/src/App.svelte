<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    apiFetch,
    getStoredToken,
    setToken,
    clearToken,
    type TokenState,
  } from './lib/api';
  import { consumeSse } from './lib/sse';
  import {
    type TraceEvent,
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
  import { t } from './lib/i18n';

  import TokenStatus from './components/TokenStatus.svelte';
  import AuthPanel from './components/AuthPanel.svelte';
  import ChatForm from './components/ChatForm.svelte';
  import PrBodyDisclosure from './components/PrBodyDisclosure.svelte';
  import DriftDiffCard from './components/DriftDiffCard.svelte';
  import ConversationsRail from './components/ConversationsRail.svelte';
  import ConversationThread from './components/ConversationThread.svelte';
  import HandoffChip from './components/HandoffChip.svelte';
  import { recallOffer, rememberOffer, forgetOffer, readHandoffOffer } from './lib/handoff';
  import type { HandoffOffer } from './lib/sse';
  import InfraDiagram from './components/InfraDiagram.svelte';
  import ApprovalDesk from './components/ApprovalDesk.svelte';
  import DecisionRecord from './components/DecisionRecord.svelte';
  import EstateView from './components/EstateView.svelte';
  import { previewPrFromSearch } from './lib/infra_graph';
  import { reconcileApprovals } from './lib/estate';
  import {
    reasoningTraceFromSearch,
    isReplayableTraceId,
    conversationIdFromSearch,
    viewFromSearch,
    DEFAULT_VIEW,
    CHAT_INTENT_PARAMS,
    type AppView,
  } from './lib/deeplink';
  import { initialChatPrefill, crewName, WORKLOADS } from './lib/workloads';
  import type { ChatPrefill } from './lib/workloads';
  import CapabilityCard from './components/CapabilityCard.svelte';
  import Modal from './components/Modal.svelte';
  import PausePill from './components/PausePill.svelte';
  import PauseBanner from './components/PauseBanner.svelte';
  import { createPauseStore } from './lib/pauseStore';
  import AutonomyPill from './components/AutonomyPill.svelte';
  import { createAutonomyStore, autonomyNoteFor } from './lib/autonomyStore';
  import { createOverviewStore, NO_DECISIONS_YET } from './lib/overviewStore';
  import { createTraceCache } from './lib/traceCache';
  import { hasDecisionForTrace } from './lib/ledger';
  import { turnOwnsReasoning } from './lib/conversations';
  import { prefersReducedMotion } from './lib/motion';
  import { stickToBottom } from './lib/stickToBottom';
  import TourBanner from './components/TourBanner.svelte';
  import TourCard from './components/TourCard.svelte';
  import DemoNoticeBell from './components/DemoNoticeBell.svelte';
  import LocaleToggle from './components/LocaleToggle.svelte';
  import { announceHeaderPopoverOpen } from './lib/headerPopover';
  import { tourDone, markTourDone, shouldOfferTour } from './lib/tour';
  import type { InfraGraph, PendingApproval } from './lib/infra_graph';
  import Icon from './components/Icon.svelte';

  // ---- state ----
  let tokenState = $state<TokenState>(getStoredToken() ? 'ok' : 'missing');
  let traceId = $state<string | null>(null);
  let finalReply = $state<string | null>(null);
  // Set from the `done` frame's `iac_pr` when a run just opened an infra PR —
  // drives the clickable first-authoring "Review & approve" CTA.
  let iacPr = $state<{ pr_number: number; pr_url: string } | null>(null);

  // ---- multi-turn conversations (P2) ----
  // The history rail's list (metadata only). The currently-open thread's id +
  // crew-lock + rehydrated turns. `conversationId === null` = a fresh, not-yet-
  // persisted chat (today's one-shot behaviour until the first reply lands).
  let conversations = $state<Conversation[]>([]);
  let conversationId = $state<string | null>(null);
  let conversationWorkload = $state<Workload | null>(null);
  let conversationTurns = $state<ConversationTurn[]>([]);
  // The crew the composer will send to, lifted out of ChatForm so resuming a
  // thread — or completing a handoff — can snap it to whichever crew now holds
  // the conversation (bind:workload on ChatForm).
  //
  // A fresh chat starts at Explore, not Anchor. With the crew picker gone the
  // operator no longer declares a specialist before they have said what they
  // want, so the default has to be the crew whose job is working that out; a
  // crew that finds the question belongs to a sibling proposes the handoff.
  let composerWorkload = $state<Workload>('explore');

  // ---- crew handoff ----
  // The live, redeemable proposal for the OPEN conversation, or null. Set from
  // the `done` frame that minted it (the only time the nonce is transmitted)
  // and restored on resume by recallOffer, which pairs the server's persisted
  // proposal with this client's stored nonce — see lib/handoff.ts for why both
  // halves are needed and why a chip can be absent on another device.
  let handoffOffer = $state<HandoffOffer | null>(null);
  // A confirm/decline POST is in flight. Separate from `busy`: the accepted
  // turn sets `busy` too, but the chip must lock the instant it is clicked,
  // before the stream starts.
  let handoffBusy = $state(false);
  // A refusal, already mapped to display text. Cleared on the next attempt and
  // whenever the offer changes.
  let handoffError = $state<string | null>(null);
  // The proposal was refused in a way that cannot be retried (already used,
  // superseded, expired). The chip stays on screen carrying its explanation,
  // but its buttons lock: a second click is guaranteed to fail the same way.
  let handoffDead = $state(false);

  // Drop the open proposal from screen AND from custody. Called wherever the
  // conversation the proposal belongs to stops being the one on screen.
  function clearHandoff(conversationIdToForget: string | null = null) {
    if (conversationIdToForget) forgetOffer(conversationIdToForget);
    handoffOffer = null;
    handoffBusy = false;
    handoffError = null;
    handoffDead = false;
  }

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

  // ?reasoning=<hex32> deep-links a shareable/bookmarkable replay of ONE past
  // reasoning timeline. Parsed once at boot (before onMount), then kept in sync
  // with the open replay by syncReasoningParam(). See lib/deeplink.
  const bootReasoningTid = reasoningTraceFromSearch(window.location.search);

  // Keep the shareable ?reasoning param in step with the open replay: set it when
  // a trace opens, drop it on a return to chat / a thread. Surgical (preserves
  // other params + the hash) and replaceState (no history spam) — mirrors
  // exitPreview() so a copied address bar always points at what is on screen.
  function syncReasoningParam(tid: string | null) {
    const u = new URL(window.location.href);
    if (tid) u.searchParams.set('reasoning', tid);
    else u.searchParams.delete('reasoning');
    history.replaceState(null, '', u);
  }

  // ?conversation=<id> deep-links a shareable/bookmarkable open thread. Parsed
  // once at boot (before onMount), then kept in sync by setConversationId(). See
  // lib/deeplink.
  const bootConversationId = conversationIdFromSearch(window.location.search);

  // Keep ?conversation in step with the open thread: set it when a thread opens/
  // persists, drop it on new-chat / a failed open. Surgical (preserves other params
  // + hash) and replaceState (no history entry) — a copied address bar always points
  // at the thread on screen. Mirrors syncReasoningParam.
  function syncConversationParam(id: string | null) {
    const u = new URL(window.location.href);
    if (id) u.searchParams.set('conversation', id);
    else u.searchParams.delete('conversation');
    history.replaceState(null, '', u);
  }

  // The ONLY writer of conversationId. Keeps the ?conversation param in lockstep
  // with the state so the invariant (param present iff a thread is open, and equal
  // to conversationId) holds at every transition, incl. future ones. Replace every
  // `conversationId = X` assignment with this.
  function setConversationId(id: string | null) {
    conversationId = id;
    syncConversationParam(id);
  }

  // The SPA's two client-side views (composite-redesign Task 2.2; the estate
  // stopped being the third in the ds-cmc merge and is a section of the desk
  // now). No router
  // — same pure-function-over-location.search pattern as the deep-link helpers
  // above, this time picking a view instead of a resource id. See lib/deeplink
  // for viewFromSearch/AppView/DEFAULT_VIEW (Task 2.1).
  // Captured separately from the `view` state below because the boot
  // deep-link work in onMount runs AFTER navigation may already have moved
  // `view` (openConversation navigates to chat). Boot decisions must read the
  // view the URL asked for, not wherever the app has since arrived.
  const bootView: AppView = viewFromSearch(window.location.search);
  let view = $state<AppView>(bootView);

  // Navigate to view `v`. AWAY from chat, this writes `view` + clears every
  // chat-intent param (reasoning/conversation/ask_pr/preview_pr) in ONE
  // replaceState — a copied desk URL must never carry a leftover chat
  // errand that would silently pull a later visitor back into chat on reload
  // (viewFromSearch's hasChatIntent treats any of the four as "go to chat").
  // TO chat, this restores nothing: it's a plain destination, not an undo.
  //
  // The `view` param is omitted for whichever view is DEFAULT_VIEW (a bare URL
  // already means that one) — keyed off the constant, NOT hardcoded to 'chat',
  // because Task 3.6 flips DEFAULT_VIEW to 'desk'. Hardcoding would mean that
  // after the flip, navigating to chat wrote a param-less URL that reloaded as
  // the desk. Reading the constant keeps this correct in both eras.
  //
  // `preserveChatState` is for the TOUR (ds-s9q). Everything in
  // teardownChatSurface() exists because a DELIBERATE departure from chat should
  // not leave an invisible thread behind. The tour is not a departure — it
  // borrows the desk for two steps and hands the visitor back to chat on
  // its last one, so applying the teardown there meant "open a conversation,
  // click Tour, press Next" silently discarded the open thread (it survived in
  // the rail; the view, scroll position and `?conversation=` did not). The tour
  // keeps its chat errand: the URL it leaves reloads into the conversation,
  // which is the honest durable intent.
  //
  // `history` (ds-7ag.1) decides whether the URL write creates a history entry.
  // A view switch is a NAVIGATION — the browser Back button has to undo it, or
  // an operator carried somewhere they did not choose has no way back. The
  // reported wayfinding failure was a desk numeral landing on the then-separate
  // estate view; the 2026-07-31 merge retired that particular trip (the numeral
  // now scrolls within one page and writes no history at all), but the rule is
  // about view switches, not that one gesture. Default 'push'; 'replace' is for writes
  // that continue the current entry rather than making one (the tour borrowing a
  // view, and its restore on close).
  function navigate(
    v: AppView,
    opts: { preserveChatState?: boolean; history?: 'push' | 'replace' } = {},
  ) {
    const fromView = view; // capture BEFORE the assignment — the push test needs it
    view = v;
    const u = new URL(window.location.href);
    // A view gesture is not a record or preview gesture — neither piece of desk
    // state travels, and neither may survive as invisible state. Done here
    // rather than in teardownChatSurface() because that runs only for non-chat
    // targets, while this must also cover the two gestures it would miss:
    // clicking Chat (which preserves every param below, both of these included)
    // and clicking Desk while already on it (a no-op re-entry that still strips
    // them). Either would leave the state and the URL contradicting — and since
    // ds-jns both params RESOLVE to the desk, a survivor would bounce a reload
    // of the chat URL straight back here.
    if (deskRecordTraceId !== null) {
      deskRecordTraceId = null;
      u.searchParams.delete('reasoning');
    }
    if (previewPr !== null) {
      previewPr = null;
      u.searchParams.delete('preview_pr');
    }
    if (v === DEFAULT_VIEW) u.searchParams.delete('view');
    else u.searchParams.set('view', v);
    // CHAT_INTENT_PARAMS is the shared list — see its comment in lib/deeplink.
    if (v !== 'chat' && !opts.preserveChatState) {
      for (const p of CHAT_INTENT_PARAMS) u.searchParams.delete(p);
    }
    // A push needs BOTH a real view transition and a real URL change. URL
    // inequality alone is not enough: clicking デスク while already on the desk
    // canonicalizes `?view=desk` to `/`, which changes the URL without going
    // anywhere, and stacking an entry there makes Back look dead. The view test
    // alone is not enough either — it would push on a no-op re-entry the URL
    // already describes. And this is what keeps the programmatic
    // navigate('chat') calls inside openConversation/openTrace honest on a boot
    // deep-link: `view` is ALREADY 'chat' there (hasChatIntent decided it before
    // mount), so restoring a shared link continues its entry instead of stacking
    // a duplicate one in front of it.
    const shouldPush =
      opts.history !== 'replace' && v !== fromView && u.href !== window.location.href;
    if (shouldPush) history.pushState(null, '', u);
    else history.replaceState(null, '', u);
    // Arriving at chat re-reads the rail — see the same call in onPopstate for
    // why (a cancelled turn skips the post-turn refresh, so its conversation
    // would be missing from the already-mounted rail until a reload).
    if (v === 'chat' && fromView !== 'chat') void loadConversations();
    // Chat is a destination, not an undo: nothing to reset (see the doc above).
    if (v === 'chat' || opts.preserveChatState) return;
    teardownChatSurface();
  }

  // The live-run surfaces, reset to "nothing is running here". Shared by
  // newChat() (a deliberate clean slate) and teardownChatSurface() (a departure
  // from the chat view), because the overlap between them is total: in both
  // cases whatever run was on screen stops being this screen's business.
  //
  // Bumping runSeq is what makes the reset actually CANCEL rather than merely
  // clear. That matters most for an async open: a GET /conversations/{id} that
  // lands after a departure still passes its own runSeq guard (:645/:684) and
  // repopulates conversationTurns behind the operator's back — on a Back press
  // that is a thread reappearing on a view whose URL says it has none.
  //
  // And once the run is cancelled the whole surface has to go with it. Clearing
  // only `busy` leaves the timeline the cancelled run had built so far, with a
  // status that says `streaming` forever — the guard that would have completed
  // it is exactly the one that just bailed. A half-cancelled run left on screen
  // reads worse than no run at all.
  function resetChatRun(): void {
    ++runSeq; // supersede every in-flight run — see the doc above
    busy = false;
    resumingConversation = false;
    traceId = null;
    finalReply = null;
    iacPr = null;
    liveExchange = null;
    ephemeralExchange = null;
  }

  // Drop the chat surface on a departure from the chat view, so in-memory state
  // stays in lockstep with the URL: an open replay or thread would otherwise sit
  // there invisibly (the chat branch isn't mounted on the desk) and reappear
  // out of step with the now-paramless address bar on a later return.
  //
  // Extracted from navigate() at ds-7ag.1 because the popstate handler needs the
  // same teardown.
  function teardownChatSurface(): void {
    resetChatRun();
    if (conversationId !== null) {
      setConversationId(null); // the only writer of conversationId — see its own doc
      conversationWorkload = null;
      conversationTurns = [];
      composerWorkload = 'explore';
    }
    // A pending handoff belongs to the thread just dropped, and redeeming it
    // needs that thread's id — leaving the chip on screen past this point would
    // render a Confirm button whose handler returns immediately on the null id.
    // View only: custody survives, so reopening the thread from the rail finds
    // its chip intact (same reasoning as newChat).
    clearHandoff();
    // The deep link's landing belongs to the thread just dropped. Leaving it
    // set would re-expand a message in whatever thread opens next that happens
    // to carry the same trace — and, more simply, would keep `?reasoning=`
    // looking live to canonicalizeRestoredEntry below.
    autoExpandTraceId = null;
    if (previewPr !== null) previewPr = null; // preview_pr already dropped above
  }

  // The desk counterpart of teardownChatSurface: the two pieces of desk state
  // that a URL can name. Not called from navigate() — that clears them inline
  // because it must also delete their params from the SAME URL write.
  function teardownDeskSurface(): void {
    deskRecordTraceId = null;
    previewPr = null;
  }

  // Browser Back/Forward. The restore is VIEW-ONLY by design: reopening deep
  // state (a thread, a replay) on a pop would fire fetches the operator did not
  // ask for and race whatever they do next, so the entry is canonicalized
  // instead — the URL is made to stop claiming content that is not on screen.
  function onPopstate(): void {
    const target = viewFromSearch(window.location.search);
    // Back during the tour dismisses the overlay. Clear tourReturnView FIRST:
    // closeTour()'s own view-restore would otherwise navigate away from — and
    // replaceState over — the very entry the operator just came back to.
    if (tourOpen) {
      tourReturnView = null;
      closeTour();
    }
    // Unconditional on a non-chat target, NOT gated on `view === 'chat'` (Codex
    // review of this branch). The tour borrows a view while PRESERVING the chat
    // surface, so a Back press mid-tour arrives with a borrowed non-chat view
    // and an open thread still live behind it — that gate skipped the teardown and left
    // a hidden thread whose later settle would write `?conversation=` onto a DESK
    // url. Exactly the state/url disagreement this handler exists to prevent.
    // On a target that never had chat state the call is a cheap no-op.
    if (target !== 'chat') teardownChatSurface();
    // The desk's own deep state gets the same view-only treatment as chat's:
    // a popped entry can name a record or a preview this session never opened
    // (or has since closed), and re-resolving it here would fire fetches the
    // operator did not ask for — boot is the only deep-resolver. Collapse
    // FIRST so canonicalizeRestoredEntry below sees the truth and drops the
    // params rather than measuring them against state we are about to clear.
    teardownDeskSurface();
    // Entering chat re-reads the rail. Cancelling a run (above, or via newChat)
    // skips the post-turn loadConversations() at :976, so a conversation the
    // server created for that cancelled turn would be missing from the
    // already-mounted rail until a reload — the one case where "the turn
    // persists and the thread is still reachable from the rail" was not true.
    if (target === 'chat' && view !== 'chat') void loadConversations();
    view = target;
    canonicalizeRestoredEntry();
  }

  // Make a restored entry describe what is actually on screen. A popped entry can
  // still carry chat-intent params for content this session tore down (e.g.
  // `?conversation=c1` after a departure from chat); the view-only restore won't
  // reopen them, so the URL must stop advertising them or copying the address bar
  // shares a lie. Only STALE params are dropped — an entry naming the open thread
  // or replay is left untouched. Because those params are exactly what forced
  // viewFromSearch to 'chat', the same write states the view explicitly, or a
  // reload of the cleaned URL would land on the desk instead.
  function canonicalizeRestoredEntry(): void {
    const u = new URL(window.location.href);
    let stale = false;
    const drop = (p: string) => {
      u.searchParams.delete(p);
      stale = true;
    };
    const conv = u.searchParams.get('conversation');
    if (conv !== null && conv !== conversationId) drop('conversation');
    // `?reasoning=` names one of two live things, depending on which surface is
    // up: the desk's open record, or the chat thread's auto-expanded message.
    // (There was a third until ds-jns retired page-level replay.)
    const reasoning = u.searchParams.get('reasoning');
    const liveReasoning = view === 'desk' ? deskRecordTraceId : autoExpandTraceId;
    if (reasoning !== null && reasoning !== liveReasoning) drop('reasoning');
    // ask_pr is a one-shot composer prefill consumed at boot (onMount strips it);
    // a restored one has nothing left to hand over, so it is always stale.
    if (u.searchParams.get('ask_pr')) drop('ask_pr');
    const preview = u.searchParams.get('preview_pr');
    if (preview !== null && String(previewPr ?? '') !== preview) drop('preview_pr');
    if (!stale) return;
    if (view === DEFAULT_VIEW) u.searchParams.delete('view');
    else u.searchParams.set('view', view);
    history.replaceState(null, '', u);
  }

  let authPanelOpen = $state(false);
  let authResolver: ((t: string | null) => void) | null = null;
  // Single-flight: concurrent callers (the overview store's creation-time
  // refresh + InfraDiagram both fetch on mount, and either may 401) share ONE
  // prompt and one resolution. Without this, a second requestToken() overwrites
  // the first's resolver and strands the first in-flight request forever
  // (Codex review).
  let authPromise: Promise<string | null> | null = null;

  // Concurrency guard: a monotonically-incrementing run id. submitChat /
  // openTrace / newChat each bump it; in-flight callbacks bail at every await
  // boundary when their captured id is stale, so a slow first stream can't
  // append into (or backfill over) a newer run. `busy` also disables Send.
  let runSeq = 0;
  let busy = $state(false);
  // True for the span of openConversation: conversationId is set (synchronously,
  // before the GET) but conversationWorkload is still null — without this, Send
  // is live during that window and submitChat's crew-switch-reset guard
  // (`conversationWorkload !== null`) can't see the mismatch yet, so a submit
  // rides the half-open thread's id carrying whatever crew the composer last
  // held (Codex review 019f46e8 must-fix). Still required with the crew picker
  // gone: composerWorkload survives from the PREVIOUS thread until the GET
  // lands, so the mismatch it guards is the same one. Cleared in
  // openConversation's finally, guarded so a superseded run can't clear a
  // newer run's flag.
  let resumingConversation = $state(false);

  // The ONE chat-disabled condition (busy live stream OR a
  // resume still rehydrating), shared by ChatForm.disabled AND
  // InfraDiagram.adoptDisabled so the two can never diverge — an Adopt click can
  // never mutate a disabled composer or strand a stale draft behind a historical
  // view (Codex review 019eb572 must-fix 3).
  const chatDisabled = $derived(busy || resumingConversation);

  // ---- chat-native live exchange ----
  // While a live /chat turn is in flight (or its reply just landed but hasn't
  // settled into the thread yet), render the exchange THROUGH the thread as an
  // optimistic user + crew bubble pair instead of the standalone hero. The crew
  // bubble reads `finalReply` live, so the reply fills that same bubble the
  // instant the `done` frame arrives — the existing backfill/decisions-refresh
  // latency before settle is no longer visible (no blue→green swap, no upward
  // hop). Captured (not reactive) at submit time so the bubble keys/labels stay
  // stable for the whole run. Cleared the MOMENT a non-persistable outcome is
  // known (paused / one-shot / error), which since ds-jns hands the exchange to
  // `ephemeralExchange` rather than to the standalone hero — see setEphemeral.
  type LiveExchange = {
    prompt: string;
    workload: Workload;
    baseSeq: number;
    /** Client-side timestamp, stamped when Send is pressed. Nothing has been
     *  persisted yet, so there is no server `created_at` — and without this the
     *  turn's time would be blank for the whole run and then appear out of
     *  nowhere when the reply lands, which is the one moment the operator is
     *  watching that bubble. Same reason EphemeralExchange carries one. */
    createdAt: string;
    /** A confirmed handoff runs a turn the operator never typed (the backend
     *  sets `omit_user_turn` for exactly this reason), so the live exchange is
     *  a lone crew bubble. Rendering the synthetic brief as an operator prompt
     *  would put words in their mouth on screen, the same way persisting it
     *  would in the durable transcript. */
    omitUserTurn?: boolean;
  };
  let liveExchange = $state<LiveExchange | null>(null);

  // ---- ephemeral (non-persisted) exchanges ----
  // A turn the backend did NOT persist — a paused refusal, a one-shot with no
  // conversation, a network/transport failure. These used to fall out of the
  // thread into the standalone hero, so the page changed shape at exactly the
  // moment something went wrong. They now render as in-memory thread turns with
  // the same anatomy (design §2).
  //
  // Deliberately NOT routed through appendLocalTurns: that function is
  // documented "called ONLY when persistence succeeded", and its optimistic
  // `seq` arithmetic plus reload semantics assume the server agrees the turn
  // exists. Feeding it a turn nobody stored would put the local thread ahead of
  // the store and survive into the next real settle.
  //
  // Mutually exclusive with `liveExchange` by construction: every branch that
  // creates one clears the other, because they are the same exchange before and
  // after its outcome is known.
  type EphemeralExchange = {
    prompt: string;
    workload: Workload;
    /** conversationTurns.length at creation — keeps the rendered keys stable
     *  for the life of the exchange, exactly like LiveExchange.baseSeq. */
    baseSeq: number;
    reply: string | null;
    isError: boolean;
    /** Null whenever the outcome carried no trace: a network failure never
     *  reached the coordinator, and a PAUSED stream emits a lone `done` frame
     *  with no `meta` and no X-Trace-Id (agent/main.py `_paused_chat_response`).
     *  A null id means the turn shows no reasoning line, because there is
     *  genuinely no reasoning to show. */
    traceId: string | null;
    /** Client-side timestamp: nothing was persisted, so there is no server
     *  `created_at` to render and the time would otherwise be blank. */
    createdAt: string;
    /** A confirmed handoff runs a turn the operator never typed. */
    omitUserTurn?: boolean;
  };
  let ephemeralExchange = $state<EphemeralExchange | null>(null);

  /** The carried ephemeral turn, re-based against the refetched rows ONLY if
   *  they actually claim a slot it renders.
   *
   *  Re-basing unconditionally to `turns.length` looks harmless and is not:
   *  `baseSeq` decides the turn's `seq`, `seq` is the keyed {#each}'s key
   *  (ConversationThread.svelte), so moving it remounts the bubble — silently
   *  collapsing any reasoning disclosure the operator had opened on it, on the
   *  one turn whose whole job is to explain itself. It also leaves a hole in the
   *  key sequence.
   *
   *  Usually there is nothing to avoid: an `omitUserTurn` overlay renders only
   *  `baseSeq + 1` and deliberately leaves `baseSeq` free for the server's
   *  transition row, so the refetch that adds that row lands exactly in the gap
   *  the overlay reserved for it. Only a genuine collision forces a move. */
  function reseated(
    carry: EphemeralExchange,
    turns: ConversationTurn[],
  ): EphemeralExchange {
    const taken = new Set(turns.map((t) => t.seq));
    const rendered = carry.omitUserTurn
      ? [carry.baseSeq + 1]
      : [carry.baseSeq, carry.baseSeq + 1];
    if (!rendered.some((s) => taken.has(s))) return carry;
    return { ...carry, baseSeq: turns.length };
  }

  /** Record a non-persisted outcome as a thread turn. Always clears
   *  `liveExchange` — the optimistic bubble and this are the same exchange. */
  function setEphemeral(
    e: Omit<EphemeralExchange, 'baseSeq' | 'createdAt'> & { baseSeq?: number },
  ): void {
    ephemeralExchange = {
      baseSeq: e.baseSeq ?? conversationTurns.length,
      createdAt: new Date().toISOString(),
      ...e,
    };
    liveExchange = null;
  }

  // The thread's rendered turns: the persisted turns plus, during a live run,
  // the optimistic exchange. `baseSeq` mirrors appendLocalTurns
  // (conversationTurns.length at submit), so when settle appends the real turns
  // the keys are identical and the keyed {#each} updates in place instead of
  // remounting — the transient bubble becomes the persisted one with no visual
  // change.
  const displayTurns = $derived.by((): ConversationTurn[] => {
    if (liveExchange == null) {
      if (ephemeralExchange == null) return conversationTurns;
      // The exchange is over and nothing was stored. It stays in the thread
      // until the next send / New chat / thread open rather than dropping the
      // page into a different layout at the worst moment.
      const { prompt, workload, baseSeq, reply, isError, traceId: etid, createdAt, omitUserTurn } =
        ephemeralExchange;
      return [
        ...conversationTurns,
        ...(omitUserTurn
          ? []
          : [
              {
                seq: baseSeq,
                role: 'user',
                text: prompt,
                workload,
                trace_id: etid,
                created_at: createdAt,
                optimistic: true,
              } satisfies ConversationTurn,
            ]),
        {
          seq: baseSeq + 1,
          role: 'crew',
          text: reply ?? '',
          workload,
          trace_id: etid,
          created_at: createdAt,
          optimistic: true,
          isError,
        } satisfies ConversationTurn,
      ];
    }
    const { prompt, workload, baseSeq, createdAt, omitUserTurn } = liveExchange;
    return [
      ...conversationTurns,
      ...(omitUserTurn
        ? []
        : [
            {
              seq: baseSeq,
              role: 'user',
              text: prompt,
              workload,
              trace_id: traceId,
              created_at: createdAt,
              optimistic: true,
            } satisfies ConversationTurn,
          ]),
      {
        seq: baseSeq + 1,
        role: 'crew',
        text: finalReply ?? '',
        workload,
        trace_id: traceId,
        iac_pr: iacPr,
        created_at: createdAt,
        optimistic: true,
        pending: finalReply == null,
      },
    ];
  });
  // Something a clean slate would clear is on this screen. displayTurns already
  // unifies "persisted thread + optimistic in-flight exchange" (reuse it — one
  // source of thread visibility, no drift); finalReply and busy cover a reply
  // that is still arriving, and conversationId is a belt for an open-but-empty
  // thread edge.
  //
  // This used to gate the composer's New chat button, whose whole point was to
  // appear only when it had something to do; that button now lives in the
  // conversations rail and is unconditional (see ConversationsRail.onNewChat
  // for why). Its one remaining reader is the emptiness rule below, which is
  // the same question asked from the other side.
  const chatOccupied = $derived(
    conversationId !== null || displayTurns.length > 0 || finalReply !== null || busy,
  );

  // A fresh chat with nothing on it yet: the greeting + suggestion chips show
  // and the composer sits in the MIDDLE of the column instead of pinned to the
  // bottom of an empty one (ds-jns PR 3).
  //
  // Derived from chatOccupied rather than re-listing its terms, so "there is
  // something here to clear" and "there is nothing here" cannot drift apart —
  // one expression, two readings. handoffOffer and iacPr are deliberately NOT
  // extra terms: neither can exist before a turn has landed, and a turn is
  // something chatOccupied already sees.
  //
  // resumingConversation is a SECOND arm over the same window, and measured
  // against today's code it never fires: openConversation writes
  // setConversationId(id) BEFORE awaiting the detail, so chatOccupied is
  // already true for the whole fetch and a `?conversation=` deep link cannot
  // flash the greeting in front of the thread it is loading. Injecting the
  // reorder (id written after the fetch) proves the arm real rather than
  // decorative: with it, still no flash; with it removed as well, the greeting
  // and four chips appear for the length of the request and are then yanked
  // away. That ordering is load-bearing for a different reason of its own (a
  // failed rehydrate must not leave a stale crew lock), which makes it exactly
  // the kind of thing a later change moves without thinking about this rule.
  const chatEmpty = $derived(!resumingConversation && !chatOccupied);

  // Suggestion chips. Frozen order, one per crew's flavour, broadest first —
  // see locales/chat.ts for why none of them names its crew.
  const EMPTY_CHIP_KEYS = [
    'chat.empty.chip.explore',
    'chat.empty.chip.anchor',
    'chat.empty.chip.patch',
    'chat.empty.chip.provision',
  ] as const;

  /**
   * Clicking a chip PREFILLS the composer; it does not send. The operator reads
   * what they are about to ask, edits it if they want, and presses Send — the
   * same contract Adopt has had since Phase 4 (design §6, "the operator stays
   * in charge"), reusing the same mechanism rather than inventing a second one.
   *
   * No `workload`: a chip is an example question, not a routing decision. See
   * ChatPrefill.workload for why absent and 'explore' are different claims.
   */
  function useSuggestion(text: string): void {
    chatPrefill = { text, epoch: (chatPrefill?.epoch ?? 0) + 1 };
  }

  // Adopt-button bridge + ?ask_pr boot seed (item 12): an Adopt click — or
  // arriving from the approval page's "ask about this change" link — prefills
  // (NOT sends) the composer. epoch bumps so the same/another Adopt re-applies
  // after an edit; a boot seed starts at epoch 1, so a later Adopt bumps to 2.
  let chatPrefill = $state<ChatPrefill | null>(
    initialChatPrefill(window.location.search, $t)
  );
  function handleAdopt(text: string) {
    // Adopt starts a NEW provisioning task, so ALWAYS drop to a clean slate
    // first: on an open thread the provision prefill would otherwise fight the
    // crew lock — the thread's own crew still owns it, and the server answers
    // 409 — and leftover one-shot output shouldn't sit around a fresh task
    // either. On an already-
    // fresh composer this is a harmless no-op (Adopt is disabled during busy/
    // historical, so there is never a live stream to cancel). The old thread
    // stays reachable from the rail.
    newChat();
    // Adopt stays a direct door into Provision even though the composer no
    // longer offers one. This is not a picker: it is a deep link carrying
    // explicit intent from a specific resource, and it opens a NEW thread, so
    // it never bypasses an existing lock. Routing it through Explore would turn
    // one deliberate click into an Explore turn plus a confirmation.
    chatPrefill = { text, workload: 'provision', epoch: (chatPrefill?.epoch ?? 0) + 1 };
    // Adopt is reachable from the estate section (Task 4.1) as well as chat,
    // but the composer only exists on chat — navigate there first, or an Adopt
    // click on the desk would silently prefill a composer nobody can
    // see. `navigate('chat')` is a plain destination (see its own doc), so
    // this is safe even when already on chat.
    navigate('chat');
    // The chat view mounts (or, if already mounted, re-renders) on the NEXT
    // tick — #chat-form doesn't exist yet in the DOM synchronously after a
    // navigate from the desk, so the scroll must wait for it too.
    void tick().then(() => {
      document.getElementById('chat-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  // Onboarding tour (item 14). The offer is decided ONCE at boot — before
  // onMount strips the intent params — and the header Tour button is the
  // permanent reopen path. Closing OR dismissing marks the tour done; the
  // flag is a UI preference, so localStorage (not sessionStorage) is right.
  // The tour's graph + open-adoption-PR list come from the OVERVIEW STORE, not
  // from InfraDiagram's onGraph/onPending lift. That lift only fired while the
  // CHAT view was mounted, which stopped being the front door when Task 3.6
  // flipped DEFAULT_VIEW to 'desk': a first-run visitor who opens the tour from
  // the desk (which is also where steps 2 and 4 send them, since Task 4.1 moved
  // those targets onto EstateView — and the desk never mounts InfraDiagram in
  // any of its sections) would leave `tourGraph` null forever, so the
  // estate step read "still loading" and the adopt step "unavailable" for the
  // whole tour. The store already owns exactly these two snapshots and fetches
  // eagerly on creation, so it is the correct source on every view.
  let tourOpen = $state(false);
  // ds-5yq: the demo-notice popover is bell-anchored and drops into the
  // top-left, which on the DESK is exactly where the instrument band's first
  // numeral and the resting headline sit — the product's thesis screen, and the
  // first thing a judge sees on the bare domain since Task 3.6 made the desk the
  // front door. It does not auto-open there; the bell keeps its unread badge, so
  // the notice stays one click away. Boot-time only (never reactive): the notice
  // decides once, at mount, so a later navigation to the desk must not
  // retroactively suppress a notice that already opened.
  //
  // ds-2co: the constraint is the LAYOUT, not the desk. The estate used to be a
  // second view rendering the same InstrumentBand in the same top-left corner,
  // so a shared `?view=estate` link reproduced exactly the overlap ds-5yq
  // removed. That link now aliases to the desk and the rule is unchanged. Chat
  // is the only view whose top-left is chrome the popover may cover, so the
  // test is `!== 'chat'` — which also means a future view is covered by default
  // rather than needing to be remembered here.
  const coversPrimaryContent = viewFromSearch(window.location.search) !== 'chat';
  let tourOffered = $state(shouldOfferTour(window.location.search, tourDone()));
  // The tour spotlights views; it does not navigate away from them. See the
  // `preserveChatState` note on `navigate` (ds-s9q).
  //
  // Because it preserves the chat errand, the URL it writes mid-tour can read
  // `?view=desk&conversation=…` — and viewFromSearch gives chat intent
  // precedence, so RELOADING that exact address lands on chat, not the desk.
  // Harmless while the tour is on screen (nobody reloads mid-spotlight) but it
  // must not outlive it, or a link copied afterwards resolves somewhere the
  // operator was not. So the tour remembers where it started and puts the view
  // back on close, which is also the behavior a visitor expects from a thing
  // that overlays their screen and then goes away.
  //
  // It borrows views without leaving history entries behind either (ds-7ag.1):
  // an operator who finished the tour should not have to press Back once per
  // spotlighted step to get out of the app.
  let tourReturnView: AppView | null = null;
  function tourNavigate(v: AppView): void {
    navigate(v, { preserveChatState: true, history: 'replace' });
  }
  function startTour(): void {
    tourOffered = false;
    tourReturnView = view;
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
    // Back to where the operator was before the tour borrowed the screen, so no
    // view/URL disagreement survives it. Still preserveChatState — an open
    // thread must come back too, which is the whole point of ds-s9q.
    if (tourReturnView !== null && tourReturnView !== view) {
      navigate(tourReturnView, { preserveChatState: true, history: 'replace' });
    }
    tourReturnView = null;
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

  // ---- per-trace cache (ds-jns, design §0) — the state the inline reasoning
  // disclosures ride on. Created ONCE here and passed down, so a live stream
  // and every expanded historical disclosure share one source of truth per
  // trace id instead of fighting over the single global timeline below.
  const traceCache = createTraceCache(call);

  // ---- pause kill-switch (one shared store → header PausePill + content
  // PauseBanner, so the two surfaces can never diverge or double-fetch) ----
  const pause = createPauseStore(call);

  // ---- autonomy dial (one shared store → header AutonomyPill + the capability
  // card note, so the two surfaces never diverge or double-fetch) ----
  const autonomy = createAutonomyStore(call);
  const capabilityAutonomyNote = $derived(autonomyNoteFor($autonomy, $t));

  /** The capability detail, opened from the empty chat's link (ds-jns Task 3.2).
   *  A modal rather than a panel in the column: "what is this thing allowed to
   *  do" is a question asked once, on the way in, and answered at length — it
   *  does not want to be re-read past on every visit, which is what the inline
   *  card had it doing. The card mounts only while this is true (Modal renders
   *  its children under `{#if open}`), so nothing is fetched until asked for. */
  let showCapabilities = $state(false);

  // ---- landing-page overview store (Task 3.0a) — single owner of the
  // graph/pending-approvals/decisions refresh triple (lib/overviewStore.ts).
  // `decisions` here is a thin derived alias so the many existing readers
  // below (noteApplied, the desk record lookups) don't all need
  // rewriting to `$overview.decisions`. Torn down on component destroy so its
  // focus/visibilitychange listeners and poll timer don't leak (matters for
  // test-suite isolation — a component mounted per-test that never destroys
  // its listeners would leave them firing against later tests' DOM/timers).
  const overview = createOverviewStore(call);
  const decisions = $derived($overview.decisions);
  onDestroy(() => overview.destroy());

  // ---- desk decision record (ds-jns, design §3) ----
  // THE single source of truth for "which decision is open on the desk". The
  // ledger row and the pending hero both only ASK (onRecordChange); neither
  // holds a copy, so at most one record can be open by construction rather
  // than by two components agreeing to stay in step.
  // Seeded from the URL: a `?reasoning=` that resolved to the DESK (i.e. one
  // with no `?conversation=` framing it — lib/deeplink's hasChatIntent) is a
  // request to open that decision's record here. Gated on `bootView` rather
  // than on "no conversation" so the one URL shape that still means chat
  // replay, `?view=chat&reasoning=`, is not quietly claimed by the desk.
  let deskRecordTraceId = $state<string | null>(bootView === 'desk' ? bootReasoningTid : null);

  // The chat-side half of the same fork. A `?reasoning=` FRAMED by a
  // `?conversation=` names a MESSAGE in that thread, so the thread opens that
  // turn's disclosure in place instead of stacking a page-level replay over
  // the thread the message belongs to. Cleared on any departure from the chat
  // surface (teardownChatSurface) so a later thread cannot inherit it.
  let autoExpandTraceId = $state<string | null>(
    bootConversationId !== null ? bootReasoningTid : null,
  );

  // Open a decision's record on the desk, from anywhere. It used to be
  // openTrace, which swapped the chat column into replay mode; ds-jns retired
  // that surface and the record on the desk that LISTS the decision is where
  // every route ends up now. navigate() FIRST — it clears the record and drops
  // `?reasoning=` as part of the view gesture, so setting the record afterwards
  // is what survives.
  function openDeskRecord(tid: string): void {
    navigate('desk');
    setDeskRecord(tid);
  }

  // The only writer, so the `?reasoning=` param and the state can never drift —
  // same discipline as setConversationId. A shared/reloaded desk URL therefore
  // always reopens exactly the record that was on screen.
  function setDeskRecord(tid: string | null): void {
    // A trace id the `?reasoning=` parser would reject can still reach here
    // from a caller reading a raw `Decision.trace_id` (an open shape). Writing
    // it would open a record whose URL evaporates on reload — the exact
    // non-round-trip `isReplayableTraceId` exists to prevent — so it is
    // refused at the one writer rather than guarded at each caller.
    const next = tid !== null && isReplayableTraceId(tid) ? tid : null;
    deskRecordTraceId = next;
    syncReasoningParam(next);
  }

  // Is the open record among the decisions we actually hold? `ledgerRows`'
  // keepTraceId guarantees a held decision always gets a row (cap or no cap),
  // so "not here" is exactly "no ledger row will render it" — which is what
  // makes the pinned card below safe from rendering a second copy of a row
  // that "show more" would reveal.
  // ...and will the strip actually RENDER it? Two conditions, because they are
  // two questions. `keepTraceId` makes "in the snapshot" survive the row cap,
  // but LedgerStrip also refuses an affordance to a row whose trace_id is not a
  // well-formed one — so a decision with a malformed id is in the list and has
  // no row to open. Asking only the first question left such a record rendering
  // NOWHERE: not pinned (the list has it) and not inline (no row will open it).
  // The same "unknown is not empty" trap as ds-mml, one surface over.
  const deskRecordHasRow = $derived(
    isReplayableTraceId(deskRecordTraceId) && hasDecisionForTrace(decisions, deskRecordTraceId),
  );
  const deskPinnedRecord = $derived(
    deskRecordTraceId !== null && !deskRecordHasRow ? deskRecordTraceId : null,
  );

  // The band's managed/drift numerals point at the estate, which since the
  // 2026-07-31 merge is a section of THIS page. Scroll to it rather than
  // navigating: no history entry, no URL change, nothing for Back to undo.
  //
  // Focus moves with the scroll (the section carries tabindex="-1") or a
  // keyboard/screen-reader user stays parked on the band button that just
  // scrolled out of the viewport — the page would have moved and their cursor
  // would not have. Same scroll-then-focus pattern as the conversation resume
  // above; `preventScroll` keeps the focus call from fighting the smooth scroll.
  function scrollToEstate(): void {
    const el = document.getElementById('estate');
    if (!el) return;
    el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
    el.focus({ preventScroll: true });
  }

  // Detect a freshly-`applied` iac_apply decision (decisions arrive newest-first)
  // so the Infrastructure panel can refresh the resource map after an apply lands.
  // Pure logic lives in lib/decision.nextAppliedWatermark (boot-seed semantics).
  function noteApplied(ds: Decision[]) {
    const { next, bump } = nextAppliedWatermark(appliedWatermark, ds);
    appliedWatermark = next;
    if (bump) appliedEpoch += 1;
  }

  // Advance the watermark from the store's decisions payload.
  //
  // This effect re-runs on EVERY overview refresh cycle, not only when
  // `decisions` changed: its one reactive read is `$overview`, and a Svelte
  // store subscription is tracked at whole-object level (no per-property
  // granularity like a $state proxy), while refresh() publishes a fresh state
  // object each cycle. So noteApplied() IS re-invoked with an unchanged array
  // reference — e.g. a cycle where the graph refreshed but the decisions fetch
  // soft-failed and kept the prior array.
  //
  // That is safe, but NOT because of anything this effect does: the guarantee
  // lives in nextAppliedWatermark (lib/decision.ts), which bumps only when the
  // newest applied decision_id DIFFERS from the watermark, so a repeat call on
  // the same payload always resolves to bump:false. Keep that idempotence if
  // you ever touch it — it, not the effect's dependency scoping, is what stops
  // appliedEpoch from double-bumping.
  //
  // The NO_DECISIONS_YET guard below is the part that does real work here: it
  // skips the pre-fetch placeholder so the store's eager creation-time fetch
  // (still pending when this effect first runs) never SEEDS the watermark on
  // empty data. A genuinely-empty server payload is a distinct fresh [] and is
  // correctly seeded. See overviewStore.ts's sentinel comment and the boot-seed
  // incident lib/decision.ts documents (a false bump there DDOSed the coordinator).
  $effect(() => {
    const ds = $overview.decisions;
    if (ds !== NO_DECISIONS_YET) noteApplied(ds);
  });

  const asString = (v: unknown): string | null =>
    typeof v === 'string' && v.length > 0 ? v : null;

  // ---- conversations rail + thread (P2) ----
  // List of recent conversations for the rail (metadata only). Best-effort,
  // single-flight-friendly, refreshed at mount and after each successful chat
  // turn (a new/updated thread re-sorts to the top).
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
    navigate('chat'); // the rail is visible in every view — resuming a thread reads chat
    const myRun = ++runSeq;
    busy = false;
    resumingConversation = true;
    traceId = null;
    finalReply = null;
    iacPr = null;
    liveExchange = null; // cancel any in-flight optimistic exchange
    ephemeralExchange = null; // the failed/paused turn belonged to the old screen
    // Whatever chip was on screen belonged to the thread we're leaving. Drop
    // it from view WITHOUT forgetting its nonce (that proposal may still be
    // open); the incoming thread's chip is rebuilt from its own detail below.
    clearHandoff();
    // `?reasoning=` on the chat surface describes whatever is still named here:
    // since ds-jns that is the message a boot deep link asked the thread to
    // auto-expand, and otherwise nothing (leaving a replay for a live thread).
    // Clearing it unconditionally, as this did when a replay was the only
    // meaning, deleted the param out of the very link that opened this thread.
    if (id !== bootConversationId) autoExpandTraceId = null;
    syncReasoningParam(autoExpandTraceId);
    setConversationId(id);
    // Clear the prior thread's crew NOW so a failed rehydrate can't leave a
    // stale lock paired with the new id (which would slip the crew-change guard
    // and 409 on the next submit). Re-set from the detail on success.
    conversationWorkload = null;
    conversationTurns = [];
    try {
      try {
        const resp = await call('/conversations/' + encodeURIComponent(id));
        if (myRun !== runSeq) return;
        if (!resp.ok) {
          // Abandon the half-open thread — don't leave an id with no crew/turns.
          setConversationId(null);
          return;
        }
        const detail = (await resp.json()) as ConversationDetail;
        if (myRun !== runSeq) return;
        conversationTurns = Array.isArray(detail.turns) ? detail.turns : [];
        // A `?conversation=&reasoning=` pair can name a trace this thread does
        // not contain — a hand-edited URL, or a link outliving the turn it
        // pointed at. The thread then expands nothing while the address bar
        // goes on claiming that message, so the param stops claiming it. Only
        // reachable on a SUCCESSFUL open; a failed one keeps its existing
        // replay fallback.
        if (
          autoExpandTraceId !== null &&
          !conversationTurns.some(
            (t) => t.trace_id === autoExpandTraceId && turnOwnsReasoning(t),
          )
        ) {
          autoExpandTraceId = null;
          syncReasoningParam(null);
        }
        const wl = detail.workload as Workload | undefined;
        if (wl) {
          // `workload` is who holds the thread NOW, which a confirmed handoff
          // rewrites — so this lands the composer on the CURRENT crew, not the
          // one that started the conversation.
          conversationWorkload = wl;
          composerWorkload = wl;
        }
        // Rebuild the confirmation chip for a proposal this thread still has
        // open. Returns null unless this client also holds the nonce — see
        // lib/handoff.ts on why a proposal can be visibly open on the server
        // and still not actionable here.
        handoffOffer = recallOffer(id, detail.pending_handoff, new Date());
        // No trace prefetch here any more. A resumed thread used to pull its
        // latest turn's trace into a page-level timeline; since PR 1 every crew
        // bubble carries its OWN reasoning disclosure, fetched by traceCache the
        // moment it is expanded. One fetch per thread open, for one turn's
        // reasoning nobody asked for, bought nothing the disclosures don't.
      } catch {
        // A failed rehydrate abandons the thread rather than leaving it half-open.
        if (myRun === runSeq) setConversationId(null);
      }
      await tick();
      if (myRun !== runSeq) return;
      // Scroll the COMPOSER into view (not the thread top) so it stays on screen —
      // the rehydrated history flows directly below it. Then move focus into the
      // thread region (tabindex=-1) so keyboard / screen-reader users are told the
      // conversation loaded, instead of being stranded on the rail button — the
      // same scroll-then-focus pattern the retired replay used for its banner.
      const reduced = prefersReducedMotion();
      document
        .getElementById('chat-form')
        ?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
      document.getElementById('conversation-thread')?.focus({ preventScroll: true });
    } finally {
      // Only the run that's still current clears the flag — a superseded run's
      // finally must not stomp on a newer run's in-flight resume.
      if (myRun === runSeq) resumingConversation = false;
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
    // The server stamped these a moment ago; we just don't have its value until
    // the next refetch replaces these rows wholesale. Our own clock is close
    // enough for a turn that is seconds old, and far better than a blank time
    // on the turn that just landed (design §2).
    const now = new Date().toISOString();
    const userTurn: ConversationTurn = {
      seq: base, role: 'user', text: prompt, workload: crew, trace_id: tid,
      created_at: now,
    };
    const crewTurn: ConversationTurn = {
      seq: base + 1, role: 'crew', text: reply ?? '', workload: crew,
      trace_id: tid, iac_pr: iacPr, created_at: now,
    };
    conversationTurns = [...conversationTurns, userTurn, crewTurn];
  }

  // ---- live chat (SSE) ----
  async function submitChat(prompt: string, workload: Workload) {
    // resumingConversation is a belt: the composer is already disabled while it's
    // true, so this should be unreachable via the UI — but a stray submit must
    // never ride a half-open thread's id past the crew-switch guard before its
    // crew has loaded (Codex review 019f46e8).
    if (busy || resumingConversation) return;
    const myRun = ++runSeq;
    busy = true;
    traceId = null;
    ephemeralExchange = null; // a new send retires the previous unstored turn
    // THIS stream's trace id, captured locally. The global `traceId` is whatever
    // run is on screen; a fast follow-up can null it while this run's backfill
    // and cache writes are still outstanding, and those must still land on the
    // trace they belong to.
    let liveTraceId: string | null = null;
    finalReply = null;
    iacPr = null;

    // Typing IS an answer to an outstanding suggestion: the operator was asked
    // whether to bring in another crew and chose to keep talking to this one.
    // So a new prompt retires the chip AND its nonce — including on reload,
    // which is why custody is dropped and not just the view. The proposal may
    // still be open server-side (a plain turn doesn't burn it; it expires), but
    // leaving a clickable chip below a newer reply would attach the suggestion
    // to a question it was never about. If the crew still thinks a sibling is
    // needed, it proposes again — and that supersedes the old one anyway.
    if (conversationId !== null) clearHandoff(conversationId);
    else clearHandoff();

    // Threads are crew-locked. If the composer's crew no longer matches the
    // open thread's, start a NEW conversation instead of sending the locked id
    // (which the backend would 409) — the old thread stays in the rail. With
    // the picker gone the operator cannot cause this directly; it survives for
    // the paths that still set the crew for them (an Adopt prefill, a boot
    // deep link that carries both ?conversation and ?ask_pr).
    if (
      conversationId !== null &&
      conversationWorkload !== null &&
      workload !== conversationWorkload
    ) {
      setConversationId(null);
      conversationWorkload = null;
      conversationTurns = [];
    }
    const sendConversationId = conversationId;

    // Render this turn through the thread from the moment Send is pressed: an
    // optimistic user bubble + a "thinking" crew bubble that fills with the
    // reply in place. baseSeq is captured AFTER the crew-switch reset above so
    // it reflects the (possibly cleared) thread and matches appendLocalTurns.
    liveExchange = {
      prompt,
      workload,
      baseSeq: conversationTurns.length,
      createdAt: new Date().toISOString(),
    };

    // The proposal this turn minted, if the crew made one. Both transports set
    // it; settleConversation takes custody, because the backend only emits it
    // alongside the conversation_id of a turn that actually persisted, and a
    // nonce for an uncommitted proposal would be unredeemable.
    let doneHandoff: HandoffOffer | undefined;

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
      setConversationId(rcid);
      conversationWorkload = workload;
      // Take custody of the nonce before anything else can bump runSeq: this
      // frame is the only time the server will ever hand it over.
      if (doneHandoff?.nonce) {
        rememberOffer(rcid, doneHandoff);
        handoffOffer = doneHandoff;
        handoffError = null;
      }
      ephemeralExchange = null; // it persisted after all
      appendLocalTurns(prompt, finalReply, traceId);
      // Clear the overlay right after the real turns are appended, BEFORE
      // clearing finalReply/iacPr, so a mid-settle read of displayTurns is never
      // half-applied (the persisted turns already carry the reply).
      liveExchange = null;
      finalReply = null; // now the last bubble in the thread above
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
        finalReply = $t('header.chatError.network');
        // Never reached the coordinator, so there is no trace and no reasoning
        // line — just the failed exchange, in the thread where it happened.
        setEphemeral({ prompt, workload, reply: finalReply, isError: true, traceId: null });
        return;
      }
      if (myRun !== runSeq) return;

      if (!resp.ok) {
        // 429 comes from the demo-window per-IP rate limiter (CF Worker);
        // judges should see "wait", not a bare status code.
        finalReply =
          resp.status === 429
            ? $t('header.chatError.rateLimit')
            : $t('header.chatError.requestFailed', { status: resp.status });
        setEphemeral({ prompt, workload, reply: finalReply, isError: true, traceId: null });
        // The crew lock refused this turn and said who holds the thread. Adopt
        // it. This is the LAST line of defence, not the usual route: every
        // path that moves a conversation already moves the composer with it,
        // and each of those can fail independently. Without this, a client
        // that ended up out of step had no way back — every attempt refused,
        // by a message naming the very crew it should have switched to.
        adoptCrew(resp.headers.get('X-Conversation-Crew') ?? undefined);
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
          liveTraceId = traceId;
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
          // Mirror the SSE done frame: the JSON path echoes conversation_id
          // when the turn persisted. Decide persistability NOW (a paused refusal
          // echoes conversation_id but persists nothing; a one-shot has none) so
          // a non-persistable reply becomes an ephemeral thread turn
          // immediately, instead of sitting in an optimistic bubble across the
          // backfill/decisions round-trips that precede settle.
          const jsonRcid =
            !body?.paused &&
            typeof body?.conversation_id === 'string' &&
            body.conversation_id.length > 0
              ? body.conversation_id
              : undefined;
          if (jsonRcid === undefined) {
            setEphemeral({
              prompt,
              workload,
              reply: finalReply,
              isError: false,
              traceId: liveTraceId,
            });
          }
          doneHandoff = readHandoffOffer(body?.handoff);
          await backfillTrace(myRun, liveTraceId);
          if (myRun !== runSeq) return;
          await overview.refresh('chat-turn');
          settleConversation(jsonRcid);
          return;
        } catch {
          if (myRun !== runSeq) return;
          finalReply = $t('header.chatError.malformed');
          // liveTraceId is still null here: the header read happens AFTER
          // resp.json(), so a malformed body means we never learned the trace.
          setEphemeral({ prompt, workload, reply: finalReply, isError: true, traceId: liveTraceId });
        }
        await backfillTrace(myRun, liveTraceId);
        if (myRun === runSeq) await overview.refresh('chat-turn');
        return;
      }

      let streamErrored = false;
      let sawDone = false;
      let sawErrorFrame = false;
      let doneConversationId: string | undefined;
      try {
        await consumeSse(resp, {
          onMeta: (m) => {
            // The cache write is NOT runSeq-guarded, and that is the point: it
            // is keyed by trace id, so it can only ever touch THIS trace's
            // entry. A superseded run that skipped it would leave the entry
            // absent (or later, stuck 'streaming' — and a streaming entry is
            // never evicted). The GLOBAL writes below keep their guard.
            liveTraceId = m.trace_id;
            traceCache.beginLive(m.trace_id);
            if (myRun !== runSeq) return;
            traceId = m.trace_id;
          },
          onEvent: (e) => {
            const te = e as unknown as TraceEvent;
            // EVERY kind, llm_usage included — omittedThoughtTokens reads it,
            // and interleaveTimeline drops what it doesn't render. Filtering
            // here would throw the information away before either could look.
            //
            // The cache is the ONLY sink. A page-level `events` array used to
            // shadow it for the retired timeline panel; nothing rendered it
            // after ds-jns Task 3.3, and a write-only copy of the stream is a
            // second source of truth waiting to disagree with the first.
            if (liveTraceId) traceCache.appendLive(liveTraceId, te);
          },
          onDone: (d) => {
            sawDone = true;
            if (myRun !== runSeq) return;
            finalReply = d.reply;
            iacPr = d.iac_pr ?? null;
            // A paused refusal echoes conversation_id for crew-lock symmetry but
            // persists NO turn — never settle it into conversationTurns (it
            // would vanish on reload); it becomes an ephemeral turn below.
            doneConversationId = d.paused ? undefined : d.conversation_id;
            // Non-persistable (paused refusal or one-shot with no
            // conversation_id): convert the optimistic bubble into an ephemeral
            // turn now, so the reply stops claiming it is about to persist. The
            // persistable case keeps the bubble (the reply fills it in place)
            // until settle promotes it.
            if (typeof doneConversationId !== 'string' || doneConversationId.length === 0) {
              // A PAUSED refusal arrives here with liveTraceId still null: the
              // backend answers it with a lone `done` frame and no `meta`, so
              // the turn genuinely has no reasoning to offer.
              setEphemeral({
                prompt,
                workload,
                reply: d.reply,
                isError: false,
                traceId: liveTraceId,
              });
            }
            doneHandoff = readHandoffOffer(d.handoff);
          },
          onError: (er) => {
            sawErrorFrame = true;
            if (myRun !== runSeq) return;
            finalReply = er.detail || $t('header.chatError.coordinatorError');
            setEphemeral({
              prompt,
              workload,
              reply: finalReply,
              isError: true,
              traceId: liveTraceId,
            });
          },
        });
      } catch {
        // Stream transport error (reader threw / body errored mid-stream).
        streamErrored = true;
        if (myRun !== runSeq) {
          if (liveTraceId) traceCache.endLive(liveTraceId, 'error');
          return;
        }
      }
      // Close the cache entry whatever happened, and unguarded for the same
      // reason as beginLive: a 'streaming' entry left open never settles and is
      // never evicted. A `done` frame that arrived before the transport died
      // still counts as an interrupted RUN — some reasoning may be missing.
      if (liveTraceId) {
        traceCache.endLive(liveTraceId, streamErrored || sawErrorFrame || !sawDone ? 'error' : 'complete');
      }

      // FAST PATH — a clean, persistable `done` frame (a real reply the backend
      // persisted). The reply is already on screen and the conversation_id is
      // known, so fold the turn + set ?conversation + release the composer NOW,
      // and refresh the timeline/decisions in the BACKGROUND — instead of holding
      // the composer disabled through the post-answer /trace backfill (up to ~25s
      // on a slow ingestion day) + /decisions round-trip.
      //
      // Predicate mirrors settleConversation's own persistability check (rcid a
      // non-empty string) AND excludes streamErrored: onDone dispatches mid-loop
      // (sse.ts reads frames incrementally), so a `done` frame can land and THEN
      // the transport can die before clean EOF, leaving doneConversationId set
      // with streamErrored === true. That hybrid — like every non-clean outcome —
      // takes the byte-identical slow path below. finalReply is non-null here by
      // construction: onDone sets it (:691) before doneConversationId (:697), and
      // the backend only echoes a conversation_id for a persisted reply, so the
      // recovery guard on the slow path is unreachable from this branch.
      const persistableDone =
        !streamErrored &&
        typeof doneConversationId === 'string' &&
        doneConversationId.length > 0;
      if (persistableDone) {
        settleConversation(doneConversationId);
        // Background, best-effort, runSeq-guarded (backfillTrace :753/:756;
        // overview.refresh() is internally try/catch'd per fetch — see
        // overviewStore.ts). A fast follow-up bumps runSeq and makes the stale
        // backfill no-op cleanly; the only cost is this turn's side-channel
        // mcp_call rows not filling inline if the operator leaves immediately —
        // the persisted trace survives and reopening refetches it.
        void backfillTrace(myRun, liveTraceId);
        void overview.refresh('chat-turn');
        return; // → finally clears busy (guarded), so the composer releases now
      }

      // SLOW PATH (unchanged) — streamErrored / no-terminal-frame / onError /
      // paused refusal / one-shot. Backfill is awaited first because it is ALSO
      // the transport-error recovery path (the "Showing the recovered reasoning"
      // message wants events already populated); then the recovery guard, then
      // decisions, then settle (a no-op when doneConversationId is undefined).
      await backfillTrace(myRun, liveTraceId);
      if (myRun !== runSeq) return;
      if (finalReply == null) {
        finalReply = streamErrored
          ? $t('header.chatError.streamInterrupted')
          : $t('header.chatError.streamEnded');
        // The stream may still have carried reasoning before it died, so this
        // ephemeral turn keeps its trace id and its disclosure.
        setEphemeral({ prompt, workload, reply: finalReply, isError: true, traceId: liveTraceId });
      }
      await overview.refresh('chat-turn');
      settleConversation(doneConversationId);
    } finally {
      if (myRun === runSeq) busy = false;
    }
  }

  // ---- crew handoff: confirm / decline ----

  // Refusal → operator-facing copy, and whether the proposal is DEAD (the chip
  // locks) or merely blocked (the chip stays clickable). The token comes from
  // the X-Handoff-Refusal header because the status alone can't separate the
  // two 409s. An unknown/absent token falls to the retryable branch: guessing
  // "dead" would strand a chip that might still work.
  function handoffRefusal(
    resp: Response,
    crews: { from: string; to: string },
  ): { text: string; dead: boolean } {
    const reason = resp.headers.get('X-Handoff-Refusal') ?? '';
    if (reason === 'expired') {
      return { text: $t('conversations.handoff.error.expired'), dead: true };
    }
    if (reason === 'busy') {
      return { text: $t('conversations.handoff.error.busy'), dead: false };
    }
    if (reason === 'no_pending' || reason === 'invalid_nonce' || reason === 'stale'
        || reason === 'not_found') {
      return { text: $t('conversations.handoff.error.gone'), dead: true };
    }
    return { text: $t('conversations.handoff.error.failed', crews), dead: false };
  }

  /** Move this client onto the crew that now owns the conversation.
   *
   *  Ownership is what the composer submits under and what the crew lock
   *  checks, so a client that is wrong about it cannot type its way out: every
   *  attempt is refused by a lock naming a crew the operator was never told
   *  about. Both fields move together — `conversationWorkload` is what the
   *  thread claims, `composerWorkload` is what the next turn would use, and a
   *  split between them is the bug wearing a disguise. */
  function adoptCrew(wl: string | undefined): void {
    if (!wl || !WORKLOADS.some((w) => w.value === wl)) return;
    conversationWorkload = wl as Workload;
    composerWorkload = wl as Workload;
  }

  /** Ask the server who owns the conversation now, and move to it.
   *
   *  For the branches where the client genuinely does not know. A refusal of
   *  `no_pending` is ambiguous by construction — the proposal may have been
   *  accepted in another tab (crew moved), declined there (crew did not), or
   *  simply expired — so unlike the committed paths below, this one cannot
   *  assume `offer.to` and has to ask. Deliberately narrower than
   *  `reloadConversationTurns`: it touches ownership ONLY, leaving the chip and
   *  its explanation standing, because the explanation is the only thing
   *  telling the operator why their click did nothing. */
  async function reconcileCrew(
    id: string,
    myRun: number,
  ): Promise<ConversationDetail | null> {
    try {
      const resp = await call('/conversations/' + encodeURIComponent(id));
      if (myRun !== runSeq || !resp.ok) return null;
      const detail = (await resp.json()) as ConversationDetail;
      if (myRun !== runSeq) return null;
      adoptCrew(detail.workload);
      // Returned, not just applied: the same answer that settles ownership
      // also settles whether the proposal is still open, and a caller that
      // ignored it would keep offering a button for a spent nonce.
      return detail;
    } catch {
      /* Fail-soft: ownership stays as-is, same as before this call existed. */
      return null;
    }
  }

  // Re-read the thread from the store after a redemption.
  //
  // Deliberately NOT an optimistic append like a normal turn: an accepted
  // handoff writes rows the client cannot reconstruct correctly — a
  // server-authored transition row, no operator row at all, and a crew reply
  // attributed to the crew that JOINED. Refetching is one round-trip and gets
  // the sequence, attribution and roles right by construction. The reply is
  // already on screen in the live bubble throughout, so nothing stalls.
  //
  // `carry` is an ephemeral turn the caller wants to KEEP across the refetch —
  // a non-persisted outcome (a failed join) whose explanation must survive the
  // rows arriving. It is re-seated here rather than by the caller after the
  // await, so the clear and the re-seat land in the SAME update: an overlay
  // re-added a tick later leaves one render without that turn, and the render
  // in between destroys any disclosure the operator had expanded on it (a
  // disclosure's open/closed state lives in the component, not the cache).
  async function reloadConversationTurns(
    id: string,
    myRun: number,
    carry: EphemeralExchange | null = null,
  ) {
    try {
      const resp = await call('/conversations/' + encodeURIComponent(id));
      if (myRun !== runSeq || !resp.ok) return;
      const detail = (await resp.json()) as ConversationDetail;
      if (myRun !== runSeq) return;
      // ONE synchronous block. The persisted rows must replace the optimistic
      // overlay in the same update: the live crew bubble is keyed at the seq
      // the joining crew's real turn lands on, so a render that sees BOTH
      // throws each_key_duplicate — and a render that sees NEITHER flashes an
      // empty thread. Same ordering discipline as settleConversation.
      conversationTurns = Array.isArray(detail.turns) ? detail.turns : [];
      liveExchange = null;
      ephemeralExchange = carry == null ? null : reseated(carry, conversationTurns);
      finalReply = null;
      iacPr = null; // the persisted crew turn carries the PR CTA now
      adoptCrew(detail.workload);
      // The redeemed proposal is burned; anything here is a NEW one the joining
      // crew made on its own first turn (it can, and the nonce for it arrived
      // on this stream's done frame).
      handoffOffer = recallOffer(id, detail.pending_handoff, new Date());
    } catch {
      /* Fail-soft: the live bubble still carries the reply, and the rail
         reopen path does a full rehydrate. */
    }
  }

  /**
   * Redeem the open proposal — confirm (`accept`) or decline.
   *
   * Confirming IS the turn: the joining crew runs immediately against the
   * handing crew's brief, which is the friction this whole mechanism removes.
   * So this drives the same stream machinery as submitChat, minus the operator
   * prompt (there isn't one) and minus the crew-lock reset (the server moves
   * the lock itself, transactionally, as it burns the nonce).
   */
  async function redeemHandoff(accept: boolean) {
    const offer = handoffOffer;
    const cid = conversationId;
    if (offer == null || cid == null) return;
    if (busy || resumingConversation || handoffBusy || handoffDead) return;

    const crews = { from: crewName(offer.from), to: crewName(offer.to) };
    handoffBusy = true;
    handoffError = null;
    // Reset the stream state up front, exactly like submitChat: one code shape
    // for "a turn is starting". The cost is that a REFUSED confirm leaves the
    // inline timeline empty (it had the previous turn's reasoning). Accepted as
    // the simpler behaviour — the thread's own "view reasoning" link puts it
    // back in one click, and the chip says plainly that nothing changed.
    const myRun = ++runSeq;
    busy = true;
    traceId = null;
    ephemeralExchange = null;
    // See submitChat: the joining crew's stream owns its own trace id, and its
    // disclosure must stream exactly like a first crew's.
    let liveTraceId: string | null = null;
    finalReply = null;
    iacPr = null;
    // Only an ACCEPT runs a crew, so only an accept gets a thinking bubble. A
    // decline is a bookkeeping POST with a one-line canned reply; showing it
    // "generating" would dramatize a write that involves no model at all.
    if (accept) {
      liveExchange = {
        prompt: '',
        workload: offer.to as Workload,
        baseSeq: conversationTurns.length,
        createdAt: new Date().toISOString(),
        omitUserTurn: true,
      };
    }

    try {
      let resp: Response;
      try {
        resp = await call('/chat/handoff', {
          method: 'POST',
          headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: cid, nonce: offer.nonce, accept }),
        });
      } catch {
        if (myRun !== runSeq) return;
        liveExchange = null;
            handoffError = $t('conversations.handoff.error.failed', crews);
        return;
      }
      if (myRun !== runSeq) return;

      if (!resp.ok) {
        liveExchange = null;
            // A failure AFTER the redemption committed is a different animal from
        // a refusal, and shares its status class — hence the explicit marker
        // (see `X-Handoff-Redeemed` in agent/main.py). The crew has already
        // moved and the nonce is spent, so treating it as retryable would keep
        // a dead chip on screen AND leave the composer bound to the crew that
        // left, whose next typed turn the crew lock then refuses. The failure
        // is reported as an ephemeral turn; the transition still has to land in
        // the thread.
        if (resp.headers.get('X-Handoff-Redeemed') === '1') {
          clearHandoff(cid);
          // The marker is stamped only past the burn, and only the ACCEPT
          // branch gets that far — so the new owner is known here without
          // asking. Move now rather than letting the refetch below do it: that
          // refetch can fail, and if it does, this message says the crew
          // changed while the composer still submits as the crew that left.
          adoptCrew(offer.to);
          finalReply = $t('conversations.handoff.error.joinFailed', crews);
          setEphemeral({
            prompt: '',
            workload: offer.to as Workload,
            reply: finalReply,
            isError: true,
            traceId: null, // nothing streamed — the failure is the response itself
            omitUserTurn: true,
          });
          // Carried, not dropped: this ephemeral turn IS the explanation for a
          // crew change with no reply behind it.
          await reloadConversationTurns(cid, myRun, ephemeralExchange);
          return;
        }
        // Otherwise nothing happened: no crew moved, no turn ran. Not a chat
        // error — report it on the chip the operator clicked and leave the
        // transcript alone.
        const refusal = handoffRefusal(resp, crews);
        handoffError = refusal.text;
        if (refusal.dead) forgetOffer(cid); // spent server-side; don't restore it on reload
        handoffDead = refusal.dead;
        // Reconcile after ANY refusal, not just a dead one. A dead refusal is
        // the obvious case — the proposal was answered somewhere else, and if
        // that answer was "accept", this tab is still pointed at the crew that
        // left. But an unmarked 5xx is the same problem wearing a different
        // status: the marker is stamped only once `redeem_handoff` RETURNS, so
        // a commit that applied while its acknowledgement failed reaches here
        // looking exactly like a refusal. Asking costs one GET on a click that
        // already failed, and is a no-op whenever nothing actually moved.
        const settled = await reconcileCrew(cid, myRun);
        if (myRun !== runSeq) return;
        // Reconciling already learned whether anything is still awaiting an
        // answer, so say so now rather than making the operator click a live
        // -looking button to find out. Only downgrades: a refusal already read
        // as dead stays dead.
        if (settled && !settled.pending_handoff && !refusal.dead) {
          forgetOffer(cid);
          handoffDead = true;
          handoffError = $t('conversations.handoff.error.gone');
        }
        return;
      }

      // The joining crew may propose a handoff of its OWN on this first turn
      // (a crew that finds the question belongs elsewhere again). That is a
      // brand-new proposal with a brand-new nonce riding this stream's done
      // frame — the same one-shot delivery as any other turn.
      let joinHandoff: HandoffOffer | undefined;
      // The kill switch is checked BEFORE redemption (agent/main.py), so a
      // paused answer is a 200 in which nothing was redeemed: the proposal is
      // still open and the nonce still valid. Forgetting custody here — which
      // "any 2xx means the nonce is spent" would do — would hide the chip for
      // good on a conversation whose proposal is alive, and no reload could
      // bring it back.
      let refusedByPause = false;
      // Whether the response ever told us how it ended. A 2xx alone does not:
      // the pause check answers 200 BEFORE redeeming, so "paused, and the one
      // frame saying so was lost" and "redeemed, and the frames were lost" are
      // the same status code with opposite meanings. Reading a silent stream
      // as success would forget a nonce for a proposal that is still open —
      // and the server keeps only a digest, so nothing could restore it.
      let sawTerminal = false;

      const ctype = resp.headers.get('content-type') ?? '';
      if (!ctype.includes('text/event-stream')) {
        try {
          const body = await resp.json();
          if (myRun !== runSeq) return;
          traceId = resp.headers.get('X-Trace-Id');
          liveTraceId = traceId;
          finalReply = typeof body?.reply === 'string' ? body.reply : JSON.stringify(body);
          const ip = body?.iac_pr;
          iacPr =
            ip && typeof ip === 'object' && typeof ip.pr_number === 'number'
              ? { pr_number: ip.pr_number, pr_url: typeof ip.pr_url === 'string' ? ip.pr_url : '' }
              : null;
          joinHandoff = readHandoffOffer(body?.handoff);
          refusedByPause = body?.paused === true;
          sawTerminal = true;
        } catch {
          if (myRun !== runSeq) return;
          finalReply = $t('header.chatError.malformed');
          setEphemeral({
            prompt: '',
            workload: offer.to as Workload,
            reply: finalReply,
            isError: true,
            traceId: null,
            omitUserTurn: true,
          });
        }
      } else {
        let streamErrored = false;
        let sawDone = false;
        let sawErrorFrame = false;
        try {
          await consumeSse(resp, {
            onMeta: (m) => {
              // Unguarded and keyed — see the identical note in submitChat.
              liveTraceId = m.trace_id;
              traceCache.beginLive(m.trace_id);
              if (myRun !== runSeq) return;
              traceId = m.trace_id;
            },
            onEvent: (e) => {
              const te = e as unknown as TraceEvent;
              if (liveTraceId) traceCache.appendLive(liveTraceId, te);
              if (myRun !== runSeq) return;
            },
            onDone: (d) => {
              sawDone = true;
              if (myRun !== runSeq) return;
              finalReply = d.reply;
              iacPr = d.iac_pr ?? null;
              joinHandoff = readHandoffOffer(d.handoff);
              refusedByPause = d.paused === true;
              sawTerminal = true;
            },
            onError: (er) => {
              sawErrorFrame = true;
              if (myRun !== runSeq) return;
              finalReply = er.detail || $t('header.chatError.coordinatorError');
              setEphemeral({
                prompt: '',
                workload: offer.to as Workload,
                reply: finalReply,
                isError: true,
                traceId: liveTraceId,
                omitUserTurn: true,
              });
              // An error FRAME is still a terminal answer, and it can only
              // arrive after the burn: the stream begins downstream of the
              // redemption, so anything streamed at all means it committed.
              sawTerminal = true;
            },
          });
        } catch {
          streamErrored = true;
          if (myRun !== runSeq) {
            if (liveTraceId) traceCache.endLive(liveTraceId, 'error');
            return;
          }
        }
        if (liveTraceId) {
          traceCache.endLive(
            liveTraceId,
            streamErrored || sawErrorFrame || !sawDone ? 'error' : 'complete',
          );
        }
        if (myRun !== runSeq) return;
        if (finalReply == null) {
          finalReply = streamErrored
            ? $t('header.chatError.streamInterrupted')
            : $t('header.chatError.streamEnded');
          setEphemeral({
            prompt: '',
            workload: offer.to as Workload,
            reply: finalReply,
            isError: true,
            traceId: liveTraceId,
            omitUserTurn: true,
          });
        }
      }

      if (myRun !== runSeq) return;
      if (refusedByPause) {
        // Nothing moved. Leave the chip and its nonce exactly as they were so
        // the operator can confirm the same suggestion once they resume,
        // instead of having to coax the crew into making it again.
        setEphemeral({
          prompt: '',
          workload: offer.to as Workload,
          reply: finalReply,
          isError: false,
          traceId: liveTraceId,
          omitUserTurn: true,
        });
        return;
      }
      if (!sawTerminal) {
        // The response never said how it ended, so this client does not know
        // whether the nonce was spent. Both mistakes are recoverable except
        // one: discarding custody for a proposal that is still open cannot be
        // undone from the server, which holds only a digest. So keep the
        // credential — a retry that turns out to be too late gets `no_pending`
        // and lands on the reconciling branch above — and ASK who owns the
        // conversation, since a committed redemption would have moved it.
        liveExchange = null;
        // The same answer settles BOTH open questions. If the server reports no
        // proposal, the redemption committed and the nonce is spent, so custody
        // is worthless — drop it. If it reports one, this was the paused reply
        // whose frame went missing, and the credential is exactly what the
        // operator needs once they resume. Only a fetch that fails leaves us
        // guessing, and then keeping it is the recoverable guess.
        const detail = await reconcileCrew(cid, myRun);
        if (myRun !== runSeq) return;
        if (detail && !detail.pending_handoff) clearHandoff(cid);
        return;
      }

      // The nonce is spent from here: the server burned it inside the
      // redemption transaction, before it ran anything. Retire the chip NOW
      // rather than leaving it to the refetch — a refetch that fails (or is
      // superseded) would otherwise leave a clickable chip holding a credential
      // that is already gone.
      clearHandoff(cid);
      // Ownership moves with the same certainty as the burn, and from the same
      // knowledge: accepting installs `offer.to`, declining leaves `offer.from`
      // exactly where it was. Both are known here, so neither needs the refetch
      // below to be reached — and the refetch is the one step in this sequence
      // that is allowed to fail silently. Before this, a redemption the server
      // COMMITTED could leave the composer bound to the departed crew whenever
      // that GET failed, and the only symptom was the operator's next message
      // being refused by a crew lock naming a crew they were never shown.
      adoptCrew(accept ? offer.to : offer.from);
      // Then take custody of any NEW proposal the joining crew made on its own
      // first turn. Order matters both ways round: after the clear, so it is
      // not immediately forgotten, and before the refetch, because
      // reloadConversationTurns rebuilds the chip by pairing the server's
      // persisted proposal with local custody.
      if (joinHandoff?.nonce) rememberOffer(cid, joinHandoff);
      // The transition committed regardless of how the turn itself went, so
      // the thread is refetched either way — a crew change the operator
      // confirmed must be visible even if the joining crew's first reply
      // errored.
      await backfillTrace(myRun, liveTraceId);
      if (myRun !== runSeq) return;
      await reloadConversationTurns(cid, myRun, ephemeralExchange);
      if (myRun !== runSeq) return;
      void overview.refresh('chat-turn');
      void loadConversations(); // the thread's crew + message count just changed
    } finally {
      // runSeq-guarded like submitChat's `busy`, and for handoffBusy the guard
      // is load-bearing rather than tidy: a superseded run that released it
      // unconditionally could free the flag a LATER redemption is holding
      // (supersede this run, open another thread, confirm its chip, then this
      // run's fetch finally settles). Every path that supersedes a redemption
      // — newChat, openConversation, navigate away, submitChat — goes through
      // clearHandoff, which releases it, so nothing is stranded either way.
      if (myRun === runSeq) {
        busy = false;
        handoffBusy = false;
      }
    }
  }

  // `tid` is passed EXPLICITLY rather than read off the global `traceId`: on the
  // fast path this runs in the background after the composer has already been
  // released, so a quick follow-up turn can null the global out from under it.
  //
  // Two different guards, on purpose. The CACHE settle is keyed by trace id and
  // therefore unconditional — a superseding run bumping runSeq must not orphan
  // the previous trace's entry, which is the one an operator scrolling back
  // will expand. The GLOBAL timeline write keeps its runSeq guard exactly as it
  // was: that array belongs to whatever run is on screen now.
  async function backfillTrace(myRun: number, tid: string | null) {
    if (!tid) return;
    let fetched: TraceResponse | null = null;
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid));
      if (resp.ok) fetched = (await resp.json()) as TraceResponse;
    } catch {
      /* backfill is best-effort — the live stream already populated both */
    }
    // Null is meaningful: settleBackfill keeps the live events and leaves the
    // enrichment state alone, so a failed backfill can still be retried by a
    // later expand rather than being recorded as loaded-and-empty.
    traceCache.settleBackfill(tid, fetched);
  }

  function newChat() {
    // Cancels any in-flight live stream and clears every live-run surface,
    // including the in-flight resume a new chat supersedes (see openTrace's
    // identical reset for why the superseding run must clear that itself —
    // Codex review 019f46e8). Shared with the leave-chat teardown; see its doc.
    resetChatRun();
    // Drop out of the open thread too — "new chat" is a clean slate. The thread
    // is still reachable from the rail (its id lives in /conversations).
    // A pending handoff belongs to THAT thread, so it leaves the screen with
    // it — but custody of the nonce is kept, not forgotten: the proposal is
    // still open server-side, and reopening the thread from the rail should
    // find its chip intact rather than punish the operator for looking away.
    clearHandoff();
    setConversationId(null);
    conversationWorkload = null;
    conversationTurns = [];
    // Back to the crew that fields an unrouted question.
    composerWorkload = 'explore';
    // No replay and no auto-expanded message on screen anymore — clear both the
    // state and the shareable param that describes them.
    autoExpandTraceId = null;
    syncReasoningParam(null);
  }

  onMount(() => {
    // No explicit decisions/graph/pending-approvals kickoff here — `overview`
    // (createOverviewStore) already fired its own eager fetch at store
    // creation (script setup, before this callback ever runs).
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
    // Boot deep-links. A shared ?conversation=<id> resumes that thread; a
    // ?reasoning=<id> alongside it replays that timeline on top (mirroring the
    // on-screen state that produced the URL: openTrace leaves conversationId
    // intact). AWAIT the conversation open before openTrace bumps runSeq, or its
    // in-flight fetch would be cancelled by the guard.
    //
    // A BARE ?reasoning= never reaches this branch: it resolves to the desk
    // (lib/deeplink) and was already seeded into deskRecordTraceId at setup, so
    // the record is on screen before this callback runs. `bootView` is what
    // separates the two — the URL's own answer, taken before anything navigated.
    void (async () => {
      if (bootConversationId) {
        const bootRun = runSeq; // openConversation's ++runSeq will make this bootRun+1
        await openConversation(bootConversationId);
        // If the operator interacted during the awaited open (New chat, another
        // thread), a newer run bumped runSeq past bootRun+1 — do NOT then drag
        // them somewhere else. Bail out of the boot continuation.
        if (runSeq !== bootRun + 1) return;
        // The thread opened: its own disclosure carries the reasoning
        // (autoExpandTraceId, seeded at setup), so there is nothing to do here.
        // A thread that FAILED to open (404, unreadable) has no turn to expand,
        // and the link would otherwise land on an empty chat column with
        // `?reasoning=` still in the address bar. The reasoning itself is still
        // readable — as a record on the desk — so send them there rather than
        // dropping the half of the link that CAN be honoured. This used to open
        // a page-level replay in the chat column; ds-jns retired that surface,
        // and the desk record is what replaced it everywhere else.
        if (conversationId === null && bootReasoningTid) openDeskRecord(bootReasoningTid);
        return;
      }
      // `?view=chat&reasoning=` with no thread around it. The chat column has
      // nothing to render a bare trace with any more, and the desk does. (A BARE
      // `?reasoning=` never reaches here — it resolves to the desk in
      // lib/deeplink and was seeded into deskRecordTraceId at setup.)
      if (bootReasoningTid && bootView === 'chat') openDeskRecord(bootReasoningTid);
    })();
    // Browser Back/Forward across the two views (ds-7ag.1). Registered here
    // rather than in the module body so it is torn down with the component.
    window.addEventListener('popstate', onPopstate);
    return () => window.removeEventListener('popstate', onPopstate);
  });
</script>

<header class="app-header">
  <a class="app-header__brand" href="/" aria-label={$t('header.brand.ariaLabel')}>
    <span class="app-logo-mark" aria-hidden="true">
      <Icon name="radar" size={16} extraClass="app-logo-mark__icon" />
    </span>
    <h1 class="app-title">DriftScribe<span class="app-title__sub">{$t('header.brand.tagline')}</span></h1>
  </a>
  <!-- The SPA's two-view nav (composite-redesign Task 2.2): desk is the front
       door and DEFAULT_VIEW since the Task 3.6 flip (see lib/deeplink), chat is
       the conversation view. The estate was the third tab until the ds-cmc
       merge folded it into the desk as a section; `?view=estate` still resolves
       here through the legacy alias in viewFromSearch. -->
  <nav class="app-header__nav" aria-label={$t('desk.nav.ariaLabel')}>
    <button
      type="button"
      class="app-header__nav-btn"
      class:is-active={view === 'desk'}
      aria-current={view === 'desk' ? 'page' : undefined}
      data-testid="nav-desk"
      onclick={() => navigate('desk')}>{$t('desk.nav.desk')}</button
    >
    <button
      type="button"
      class="app-header__nav-btn"
      class:is-active={view === 'chat'}
      aria-current={view === 'chat' ? 'page' : undefined}
      data-testid="nav-chat"
      onclick={() => navigate('chat')}>{$t('desk.nav.chat')}</button
    >
  </nav>
  <div class="app-header__actions">
    <!-- Language toggle (EN / 日本語). Leads the actions cluster; writing it
         re-renders the whole app via the $t/$locale stores. -->
    <LocaleToggle />
    <!-- Judging-window notice bell (replaces the in-flow DemoNoticeBanner; see
         docs/plans/2026-07-07-demo-notice-bell.md). Deleted whole at
         close-window time. -->
    <DemoNoticeBell {coversPrimaryContent} />
    <!-- data-tour="controls" lives on this always-rendered wrapper (not the
         loaded-only pill button) so the tour spotlight resolves even while
         /autonomy is loading or unknown. -->
    <div class="header-tour-anchor" data-tour="controls">
      <AutonomyPill {autonomy} />
    </div>
    <PausePill {pause} />
    <!-- Quiet text button, not a ds-btn (ds-7ag.3): a bordered ghost button put
         "take the tour" at the same weight as the view nav and the autonomy
         dial. The compass icon stays — it is what makes it findable without
         chrome. -->
    <button class="app-tour-btn" type="button" data-testid="tour-open" onclick={startTour}
      ><Icon name="compass" size={14} />{$t('header.tourButton')}</button
    >
    <TokenStatus state={tokenState} onChange={onChangeToken} />
  </div>
</header>

<!-- The rail comes off the desk (composite-redesign Task 3.5 decision): the
     desk is a 780px centered column, and a rail beside it would repeat what the
     page already shows. `.rails` only renders (and only takes a grid column) on
     the chat view; `.layout` collapses to a single full-width column otherwise.
     Nothing becomes unreachable: deeplink.ts's hasChatIntent() already forces
     view==='chat' for ?reasoning=/?conversation=/?ask_pr=/?preview_pr=, so no
     shared link can strand a visitor on a railless desk (see App.test.ts's
     "rails come off the desk" suite, which pins that guarantee). -->
<!-- Renders on ANY view (Task 3.5 flipped the bare-URL default to desk, so a
     first-run visitor who never touches chat must still be offered the tour).
     shouldOfferTour's own errand-suppression semantics are untouched — this
     only moved WHERE the offer renders, not when.

     It sits OUTSIDE `.layout`, as a sibling in the #app column, for two
     reasons. Grid auto-placement was dropping it into the first cell — the
     280px rails column — and pushing the rails and the chat area onto separate
     rows. And `.layout--chat` is now a height-constrained single-row grid
     (ds-jns PR 3): a banner inside it would land in an implicit second row
     that `overflow: hidden` would simply swallow. -->
{#if tourOffered && !tourOpen}
  <TourBanner onStart={startTour} onDismiss={dismissTourOffer} />
{/if}

<main class="layout" class:layout--full={view !== 'chat'} class:layout--chat={view === 'chat'}>
  {#if view === 'chat'}
  <div class="rails" data-testid="rails">
    <ConversationsRail
      {conversations}
      activeConversationId={conversationId}
      onOpen={openConversation}
      onNewChat={newChat}
    />
  </div>
  {/if}

  {#if view === 'chat'}
  <!-- Chat is a chat APP now, not a document (ds-jns PR 3): the composer is
       pinned to the bottom of the viewport and the transcript scrolls above it,
       oldest-first, newest against the composer. Before this the composer sat
       near the top and the reply grew DOWNWARD below it, so the thing the
       operator was waiting for walked off the bottom of the window while they
       watched — on a long run they had to chase it by scrolling. -->
  <section
    id="chat-area"
    class="chat-area"
    class:chat-area--empty={chatEmpty}
    aria-label={$t('header.chatArea.ariaLabel')}
  >
    <!-- Deliberately OUTSIDE the scroll region. The pause banner is the reason
         the composer below it is refusing input; if it could scroll away, the
         operator would be left looking at a dead Send button with the
         explanation parked somewhere off-screen. (The autonomy dial moved to
         the header pill; the "controls" spotlight marker moved with it.) -->
    <PauseBanner {pause} />
    <!-- The chat view's ONE scroll container — see .chat-thread. Everything
         that accumulates lives in here; the composer is a sibling pinned
         beneath it, so it never moves and never has to be scrolled back to. -->
    <div class="chat-thread" data-testid="chat-thread" use:stickToBottom>
      <!-- The estate diagram and the capability card used to sit here, above
           every transcript. Neither belongs to a conversation: the estate is
           the desk's subject and has a section of its own there (the
           unmatched-declarations group moved with it, ds-zld), and "what is
           this thing allowed to do" is a question asked once on the way in,
           now a link under the empty state's chips. What is left in this
           container is the conversation and nothing else. -->
      {#if displayTurns.length > 0}
        <ConversationThread turns={displayTurns} cache={traceCache} {conversationId} {autoExpandTraceId} />
      {/if}
      <!-- The confirmation sits at the END of the transcript, directly under the
           crew reply that proposed it and directly above the composer — where
           the operator's eye already is. -->
      {#if handoffOffer !== null}
        <HandoffChip
          offer={handoffOffer}
          pending={handoffBusy}
          disabled={chatDisabled || handoffDead}
          errorText={handoffError}
          onConfirm={() => void redeemHandoff(true)}
          onDecline={() => void redeemHandoff(false)}
        />
      {/if}
    </div>
    <!-- The empty state's greeting. Outside .chat-composer so the tour's
         "composer" spotlight keeps hugging the form itself rather than growing
         to swallow a greeting and four chips the moment the chat is fresh. -->
    {#if chatEmpty}
      <h2 class="chat-empty__greeting" data-testid="chat-empty-greeting">
        {$t('chat.empty.greeting')}
      </h2>
    {/if}
    <div class="chat-composer tour-target" data-tour="composer">
      <ChatForm
        disabled={chatDisabled}
        onSubmit={submitChat}
        prefill={chatPrefill}
        bind:workload={composerWorkload}
      />
    </div>
    <!-- Below the composer, not above it: the greeting introduces the box and
         the chips elaborate on it, so reading order is greeting -> where you
         type -> things you could type. Above, they would push the input the
         operator came here to use down the page behind four sentences. -->
    {#if chatEmpty}
      <ul class="chat-empty__chips" data-testid="chat-empty-chips"
        aria-label={$t('chat.empty.chipsAriaLabel')}>
        {#each EMPTY_CHIP_KEYS as key (key)}
          <li>
            <button
              type="button"
              class="chat-empty__chip"
              data-testid="chat-empty-chip"
              onclick={() => useSuggestion($t(key))}
            >{$t(key)}</button>
          </li>
        {/each}
      </ul>
      <!-- The last thing on the front door, and the quietest. Its label IS the
           modal's title — you click a sentence and get the thing that sentence
           names, with no guessing in between. -->
      <p class="chat-empty__more">
        <button
          type="button"
          class="chat-empty__more-link"
          data-testid="capability-link"
          onclick={() => (showCapabilities = true)}
        ><Icon name="shield" size={13} />{$t('capability.card.title')}</button>
      </p>
    {/if}
    <!-- Outside the {#if chatEmpty} that owns the link: the dialog traps focus,
         so nothing can flip chatEmpty while it is up, but a card yanked
         mid-render by a state change that arrives from elsewhere (a rail New
         chat, a deep link) is not a failure mode worth leaving open. -->
    <Modal
      open={showCapabilities}
      title={$t('capability.card.title')}
      onClose={() => (showCapabilities = false)}
    >
      <CapabilityCard {call} autonomyNote={capabilityAutonomyNote} />
    </Modal>
  </section>
  {:else if view === 'desk'}
  <!-- The real approval desk (Task 3.5). Data comes exclusively from the
       overview store's current snapshot — ApprovalDesk performs no fetches
       of its own. `refresh={overview.refresh}` lets the desk arm its own
       short, bounded re-check burst after sending the operator to an
       approval page (see ApprovalDesk's "fast convergence" comment). -->
  <!-- A record whose decision is NOT in the snapshot we hold — a `?reasoning=`
       link older than the listed rows, or one naming a chat turn's trace. It
       is pinned above the desk because there is no row for it to open under.
       The out-of-window NOTE waits for `settled`: while the first cycle is
       outstanding the list is empty for a reason that has nothing to do with
       this record's age, and "older than the records listed below" would be a
       guess about a list we have not read yet (ds-eh6's rule). -->
  {#if deskPinnedRecord !== null}
    <div class="desk-pinned-record">
      <DecisionRecord
        traceId={deskPinnedRecord}
        cache={traceCache}
        note={$overview.settled ? 'outOfWindow' : null}
      />
    </div>
  {/if}
  <ApprovalDesk
    graph={$overview.graph}
    decisions={$overview.decisions}
    pendingApprovals={$overview.pendingApprovals}
    settled={$overview.settled}
    degraded={$overview.degraded}
    lastError={$overview.lastError}
    refresh={overview.refresh}
    onShowEstate={scrollToEstate}
    recordTraceId={deskRecordTraceId}
    cache={traceCache}
    onRecordChange={setDeskRecord}
  />
  <!-- The estate, directly beneath the decision area (2026-07-31 merge — one
       landing page: band → hero → ledger → estate). Data comes exclusively
       from the overview store's current snapshot; EstateView performs no
       fetches of its own, same discipline as ApprovalDesk. Adopt routes
       through the SAME handleAdopt bridge as InfraDiagram's own Adopt buttons.

       Its loading/degraded state is deliberately INDEPENDENT of the hero's:
       a pending approval can coexist with a failed graph fetch, and each area
       reports its own truth (ds-eh6). -->
  <!-- `?preview_pr=N` — the ghost overlay the IaC approval page links to
       (agent/templates/iac_approval.html). It renders on the DESK now, above
       the estate it is a preview OF, instead of in the chat view where the
       operator had to scroll past a composer to reach it. Mounted only when
       there is a preview: the desk's own estate section below is the resting
       state, and a second full diagram would just repeat it. -->
  {#if previewPr !== null}
    <div class="desk-preview">
      <InfraDiagram
        {call}
        {appliedEpoch}
        {previewPr}
        onExitPreview={exitPreview}
        onAdopt={handleAdopt}
        adoptDisabled={chatDisabled}
      />
    </div>
  {/if}
  <EstateView
    graph={$overview.graph}
    decisions={$overview.decisions}
    pendingApprovals={$overview.pendingApprovals}
    settled={$overview.settled}
    approvalsStale={$overview.approvalsStale}
    adoptDisabled={chatDisabled}
    onAdopt={handleAdopt}
    onInvestigate={handleAdopt}
  />
  {/if}
</main>

<AuthPanel open={authPanelOpen} onSubmit={onAuthSubmit} onCancel={onAuthCancel} />

{#if tourOpen}
  <TourCard
    graph={$overview.graph}
    pendingApprovals={reconcileApprovals($overview.pendingApprovals, $overview.decisions)}
    approvalsStale={$overview.approvalsStale}
    adoptDisabled={chatDisabled}
    onAdoptPrefill={handleAdopt}
    onNavigate={tourNavigate}
    onClose={closeTour}
  />
{/if}

<style>
  /* Named regions on an explicit grid (ds-7ag.3). This was a wrapping flex row
     with space-between, which made the nav's position a function of how wide the
     actions cluster happened to be: it sat hard right when the cluster wrapped
     to its own line and slid inward when it didn't, so the app's primary
     navigation moved on you between views (compare desk-live-00181.png with
     chat-1440.png).
     The default is TWO rows — identity + navigation on top, operational
     utilities beneath. The plan asked for a single centred row, and for a
     two-row fallback with the nav BELOW the actions; both were changed after
     measuring the real regions at 1920px (brand 539 / nav 271 / actions 637 in
     JA): a single row needs ~1530px, so at the 1440px this pitch is shot at the
     tracks were over-committed and the brand tagline wrapped or the nav
     collided with its neighbours. Two rows is also what production already
     renders at 1440 — the difference is that the rows now MEAN something, which
     is the whole point of the demotion in this commit: navigation shares the
     brand's row, and the six utility chips sit on the quieter one. Above 1560px
     everything genuinely fits and it collapses to the single row. */
  .app-header {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    grid-template-areas:
      'brand nav'
      'actions actions';
    align-items: center;
    column-gap: var(--ds-sp-4);
    row-gap: var(--ds-sp-2);
    padding: var(--ds-sp-3) var(--ds-sp-6);
    /* ds-qbo: the header sits ON the page rather than floating above it — paper
       ground, one hairline rule, no shadow. It was --ds-surface + --ds-shadow-sm,
       which read as a raised bar in a world that has since decided structure is
       carried by rules. The rule below is the only edge it needs. */
    border-bottom: 1px solid var(--ds-border);
    background: var(--ds-bg);
  }
  /* One row once there is room for all three regions side by side (measured:
     ~1530px in JA, the wider of the two locales). */
  @media (min-width: 1560px) {
    .app-header {
      grid-template-columns: auto minmax(0, 1fr) auto;
      grid-template-areas: 'brand nav actions';
    }
  }
  .app-header__brand {
    display: inline-flex;
    align-items: center;
    grid-area: brand;
    justify-self: start;
    gap: var(--ds-sp-3);
    text-decoration: none;
    color: inherit;
    cursor: pointer;
    border-radius: 8px;
  }
  .app-header__brand:hover .app-title {
    /* subtle: only a slight emphasis; do not underline the wordmark */
    opacity: 0.85;
  }
  .app-header__brand:focus-visible {
    /* match the app's global focus affordance (base.css:89 uses box-shadow
       var(--ds-ring)), NOT a nonexistent --ds-focus token. */
    outline: none;
    box-shadow: var(--ds-ring);
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
  /* Desk / Estate / Chat — segmented pill, same recipe as LocaleToggle's
     is-active fill so the two header controls read as one family.
     ds-7ag.3 gave it weight: at 13px among six utility chips it read as one
     more chip rather than as the app's primary navigation. #272 stopped short of
     a navy fill because the control had to serve two design worlds at once
     (paper desk, legacy chat) and navy belonged to only one of them. ds-qbo
     removed that constraint — there is one world now — so the active segment
     below finally takes the fill. */
  .app-header__nav {
    display: inline-flex;
    align-items: stretch;
    grid-area: nav;
    justify-self: center;
    gap: 2px;
    padding: 2px;
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-pill);
  }
  .app-header__nav-btn {
    appearance: none;
    /* Transparent on EVERY segment, not just the active one: the base was
       `border: 0`, so an active-only border added 2px to the row and jiggled
       the whole nav on every view switch. */
    border: 1px solid transparent;
    background: transparent;
    color: var(--ds-muted);
    font-family: inherit;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    padding: 0.35em 1em;
    border-radius: var(--ds-radius-pill);
    cursor: pointer;
    white-space: nowrap;
    transition:
      background-color var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease),
      color var(--ds-dur) var(--ds-ease);
  }
  .app-header__nav-btn:hover {
    color: var(--ds-fg);
  }
  /* The active view is the one thing in the header that must be unmissable, so
     it is a filled navy chip (white on navy = 15.7:1) rather than the old
     white-surface + border + shadow, which was a slightly-lighter chip among
     chips. Navy is this world's navigation/identity color; the shadow is gone
     because nothing in the header floats. Border stays navy so the 1px
     transparent border every segment carries does not change the box size. */
  .app-header__nav-btn.is-active {
    background: var(--ds-navy);
    border-color: var(--ds-navy);
    color: #fff;
  }
  .app-header__actions {
    display: inline-flex;
    align-items: center;
    grid-area: actions;
    justify-self: end;
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
  /* Phone widths: the brand and the nav stop fitting on one line together, so
     the nav takes a row of its own. It must never wrap mid-cluster — the view
     buttons broken across two lines is the "後付け" texture itself. */
  @media (max-width: 640px) {
    .app-header {
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas:
        'brand'
        'nav'
        'actions';
    }
    .app-header__actions {
      justify-self: start;
    }
  }
  /* Give the nav room before shrinking anything else: the tagline goes first.
     Raised from 640px to 900px at ds-7ag.3, measured — brand-with-tagline (539)
     plus nav (271) plus the gutters needs ~875px, so between 640 and 900 the
     tagline was what pushed the nav off its own row. The positioning copy it
     repeats is on the desk's resting screen and the homepage. */
  @media (max-width: 900px) {
    .app-title__sub {
      display: none;
    }
  }
  .app-tour-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    appearance: none;
    border: 0;
    background: none;
    /* Vertical padding keeps the hit area ≥ 24px now that the button chrome
       that used to provide it is gone. */
    padding: 0.35em 0.2em;
    margin: 0;
    font-family: inherit;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    line-height: 1.2;
    color: var(--ds-muted);
    white-space: nowrap;
    cursor: pointer;
    border-radius: var(--ds-radius-sm);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .app-tour-btn:hover {
    color: var(--ds-fg);
  }
  .app-tour-btn:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
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
    /* Takes the space the header and banners leave, rather than subtracting a
       hard-coded header height from 100vh — see the `#app` flex column in
       base.css for why that constant was a standing 38px of dead scroll
       (ds-s61). */
    flex: 1;
  }
  /* Desk: no rails column at all (see the `.rails` {#if} above) —
     collapse the grid to one full-width column instead of leaving a bare
     280px gap where the rails used to sit. */
  .layout--full {
    grid-template-columns: minmax(0, 1fr);
  }
  /* Chat is a fixed-height app shell (ds-jns PR 3): the window does not scroll,
     the transcript does. Desk keeps document flow — it is a landing page you
     read top to bottom, and pinning it would only invent a second scrollbar.
     Hence the modifier rather than a change to `.layout` itself.

     The definite height comes from base.css (`#app:has(> .layout--chat)`); what
     these two declarations add is passing it DOWN. `align-items: stretch`
     overrides `.layout`'s `start` so .chat-area takes the row's full height
     instead of shrink-wrapping its content, and `min-height: 0` opts out of the
     flex item's default "never smaller than my content".

     Deliberately NOT an explicit `grid-template-rows: minmax(0, 1fr)`: measured
     against the real cascade, the implicit auto row already resolves to exactly
     the container height (align-content defaults to stretch, and .chat-thread's
     own min-height: 0 keeps the track from being pushed up by the transcript).
     Pinning ONE explicit row would buy nothing and cost something — a future
     third child of `.layout` would land in an implicit auto row and be swallowed
     whole by the overflow: hidden below, where today it merely squeezes the
     thread and is at least visible.

     Desktop only: below the breakpoint the rails stack BELOW the chat, and
     clipping the layout would make them unreachable. Mobile gets its own shell
     when the rail becomes a modal. */
  @media (min-width: 761px) {
    .layout--chat {
      align-items: stretch;
      min-height: 0;
      overflow: hidden;
    }
    /* The rail column is height-constrained too, so it needs somewhere for a
       long history to go. The rail already caps its own list; this catches
       anything that outgrows the viewport anyway. */
    .layout--chat .rails {
      overflow-y: auto;
    }
  }
  /* Left column. It held two stacked rails until ds-jns retired the decisions
     one — the desk's ledger is the decision browser now — so it wraps a single
     rail today, and keeps its own box because the padding, the height
     constraint and the mobile reordering below all belong to the COLUMN, not to
     whatever is currently in it. */
  .rails {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-6);
    padding: var(--ds-sp-5) var(--ds-sp-3) var(--ds-sp-8) var(--ds-sp-5);
    min-height: 0;
  }
  .chat-area {
    display: flex;
    flex-direction: column;
    /* A flex item's default min-height is `auto` — "never smaller than my
       content" — which lets the transcript push this column past the viewport
       and hands the scroll back to the window, the exact thing the pinned
       composer exists to prevent. THIS one is load-bearing (removing it is the
       one min-height: 0 in the chain that reddens the geometry spec); the
       siblings on .layout--chat and .chat-thread are the same canonical guard,
       kept because their redundancy is a fact about today's descendants'
       min-content sizes, not a property of the shell. */
    min-height: 0;
    padding: 0 var(--ds-sp-6);
    max-width: var(--ds-page-max);
  }
  /* The chat view's single scroll container. Vertical padding lives here rather
     than on .chat-area so the scrollbar runs the full height of the column
     instead of floating inside an inset box. */
  .chat-thread {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: var(--ds-sp-5) 0 var(--ds-sp-4);
  }
  /* flex: 0 0 auto — the composer keeps its natural height no matter how long
     the transcript gets. Without it the composer would be the flex item that
     yields, shrinking a multi-line draft to nothing as the thread grows. */
  .chat-composer {
    flex: 0 0 auto;
    padding-bottom: var(--ds-sp-5);
  }

  /* ── The empty new-chat state ──────────────────────────────────────────────
     A fresh chat has no transcript, so pinning the composer to the bottom would
     leave the operator looking at a screen of nothing with a lone input bar
     under it. Instead the composer moves to the MIDDLE and brings a greeting
     and four example questions with it — the front door, not the tail of an
     empty log.

     Centred with auto margins rather than `justify-content: center`, and that
     distinction is load-bearing: PauseBanner is a flex child of this same
     column, and justify-content would centre it too — floating the explanation
     for why the composer is refusing input into the middle of the page instead
     of leaving it at the top where an alert belongs. Two auto margins split the
     free space between them, so only what sits BETWEEN them is centred.

     overflow-y because auto margins collapse to 0 when the content no longer
     fits: on a short viewport the group would otherwise grow past the column
     and be clipped by `.layout--chat`'s overflow: hidden, taking the chips (and
     on a really short one, the Send button) with it. */
  .chat-area--empty {
    overflow-y: auto;
  }
  /* Not `flex: 0 0 auto` + zero height: the thread still carries `padding` and
     would leave a band of dead space between the greeting and whatever is above
     it. It has no children in this state anyway. */
  .chat-area--empty .chat-thread {
    display: none;
  }
  .chat-area--empty .chat-empty__greeting {
    margin-top: auto;
  }
  /* The auto margin that closes the centring pair lives on the LAST thing in
     the group, which is now the capability link rather than the chips. Left on
     the chips it would eat the free space between them and the link and shove
     the link alone onto the bottom edge of the column. */
  .chat-area--empty .chat-empty__more {
    margin-bottom: auto;
  }
  .chat-empty__greeting {
    margin: 0 0 var(--ds-sp-4);
    font-size: var(--ds-fs-4);
    font-weight: var(--ds-fw-semibold);
    letter-spacing: -0.01em;
    color: var(--ds-fg);
    text-align: center;
  }
  .chat-empty__chips {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: var(--ds-sp-2);
    list-style: none;
    margin: 0;
    /* Was sp-5, the whole gap to the bottom of the group. The link now sits in
       that space, so this is only the gap to the link. */
    padding: 0 0 var(--ds-sp-3);
  }
  /* Third rank on this screen, under the chips which are already under the
     Send button: no border, no fill, muted until pointed at. It is a footnote
     for the operator who wants to know the cage before they use the tool, not
     something to steer the other 90% away from the box. */
  .chat-empty__more {
    margin: 0;
    padding: 0 0 var(--ds-sp-5);
    text-align: center;
  }
  .chat-empty__more-link {
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 0.4em;
    border: none;
    background: none;
    padding: 0.2em 0.4em;
    color: var(--ds-muted);
    font: inherit;
    font-size: var(--ds-fs-1);
    text-decoration: underline;
    text-underline-offset: 0.25em;
    text-decoration-color: var(--ds-border-strong);
    cursor: pointer;
    transition:
      color var(--ds-dur) var(--ds-ease),
      text-decoration-color var(--ds-dur) var(--ds-ease);
  }
  .chat-empty__more-link:hover {
    color: var(--ds-fg-soft);
    text-decoration-color: currentColor;
  }
  .chat-empty__more-link:focus-visible {
    outline: none;
    border-radius: var(--ds-radius-sm);
    box-shadow: var(--ds-ring);
  }
  /* Quiet by construction: one saturated control on this screen is the Send
     button, and four blue pills either side of it would turn the front door
     into a menu of things the agent would rather you asked. */
  .chat-empty__chip {
    appearance: none;
    display: block;
    text-align: left;
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-pill);
    background: var(--ds-surface);
    color: var(--ds-fg-soft);
    font-family: inherit;
    font-size: var(--ds-fs-1);
    line-height: 1.35;
    padding: 0.45em 0.95em;
    cursor: pointer;
    transition:
      background-color var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease),
      color var(--ds-dur) var(--ds-ease);
  }
  /* No :disabled treatment, deliberately. A chip is only ever on screen while
     chatEmpty holds, and chatEmpty implies !busy / !resuming / !historical —
     which is the whole of chatDisabled. A `disabled={chatDisabled}` here (and
     the greyed-out rule to match) would be a state that cannot be reached and
     therefore cannot be tested. */
  .chat-empty__chip:hover {
    background: var(--ds-surface-2);
    border-color: var(--ds-border-strong);
    color: var(--ds-fg);
  }
  /* The out-of-window record, pinned above the desk. Width + centring copied
     from ApprovalDesk/EstateView rather than left to shrink-to-fit: those two
     share one column and pin it explicitly for exactly the reason ds-cmc
     found — a card that happens to land on 780px by accident stops doing so
     the moment its contents change. */
  /* Same column as ApprovalDesk/EstateView — the preview sits between them. */
  .desk-preview {
    width: 100%;
    max-width: 780px;
    margin: var(--ds-sp-5) auto 0;
  }
  .desk-pinned-record {
    width: 100%;
    max-width: 780px;
    margin: 0 auto var(--ds-sp-5);
    padding: var(--ds-sp-5) var(--ds-sp-6);
    background: var(--ds-bg);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
  }
  .chat-thread > :global(*) {
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
       to scroll past the whole conversation list to reach it. The rail drops
       below as secondary navigation. */
    .chat-area {
      order: 1;
    }
    .rails {
      order: 2;
    }
  }
</style>
