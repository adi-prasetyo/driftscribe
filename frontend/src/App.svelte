<script lang="ts">
  import { onMount } from 'svelte';
  import {
    apiFetch,
    getStoredToken,
    setToken,
    clearToken,
    type TokenState,
  } from './lib/api';
  import { consumeSse } from './lib/sse';
  import type { TraceEvent, TimelineStatus } from './lib/timeline';
  import type { Decision, TraceResponse } from './lib/types';
  import type { Workload } from './lib/workloads';

  import TokenStatus from './components/TokenStatus.svelte';
  import AuthPanel from './components/AuthPanel.svelte';
  import ChatForm from './components/ChatForm.svelte';
  import TraceBadge from './components/TraceBadge.svelte';
  import FinalResponse from './components/FinalResponse.svelte';
  import IacApprovalCta from './components/IacApprovalCta.svelte';
  import ReplyPending from './components/ReplyPending.svelte';
  import DecisionSummary from './components/DecisionSummary.svelte';
  import DriftDiffCard from './components/DriftDiffCard.svelte';
  import HistoricalBanner from './components/HistoricalBanner.svelte';
  import DecisionsRail from './components/DecisionsRail.svelte';
  import InfraDiagram from './components/InfraDiagram.svelte';
  import { previewPrFromSearch } from './lib/infra_graph';
  import type { ChatPrefill } from './lib/workloads';
  import CapabilityCard from './components/CapabilityCard.svelte';
  import PauseControl from './components/PauseControl.svelte';
  import Timeline from './components/Timeline.svelte';

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

  // Bumps when a freshly-`applied` iac_apply decision is observed in /decisions
  // — drives InfraDiagram's delayed resource-map re-fetches (rides out CAI lag).
  let appliedEpoch = $state(0);
  let lastAppliedId: string | null = null;

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

  // Adopt-button bridge: an Adopt click on the resource map prefills (NOT sends)
  // the composer. epoch bumps so the same/another Adopt re-applies after an edit.
  let chatPrefill = $state<ChatPrefill | null>(null);
  function handleAdopt(text: string) {
    chatPrefill = { text, workload: 'provision', epoch: (chatPrefill?.epoch ?? 0) + 1 };
    // Bring the composer into view so the prefilled draft is obvious. Best-effort:
    // the element exists in the live tree; guarded for the historical/SSR-less case.
    document.getElementById('chat-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
  function noteApplied(ds: Decision[]) {
    const applied = ds.find(
      (d) => d.action === 'iac_apply' && d.apply_status === 'applied',
    );
    const id = applied?.decision_id ?? null;
    if (id && id !== lastAppliedId) {
      lastAppliedId = id;
      appliedEpoch += 1;
    }
  }

  const asString = (v: unknown): string | null =>
    typeof v === 'string' && v.length > 0 ? v : null;

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
    status = 'pending';

    try {
      let resp: Response;
      try {
        resp = await call('/chat', {
          method: 'POST',
          headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, workload }),
        });
      } catch {
        if (myRun !== runSeq) return;
        status = 'error';
        finalReply = 'Network error contacting the coordinator.';
        finalIsError = true;
        return;
      }
      if (myRun !== runSeq) return;

      if (!resp.ok) {
        status = 'error';
        finalReply = `Request failed (${resp.status}).`;
        finalIsError = true;
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
        } catch {
          if (myRun !== runSeq) return;
          status = 'error';
          finalReply = 'Malformed response.';
          finalIsError = true;
        }
        await backfillTrace(myRun);
        if (myRun === runSeq) await loadDecisions();
        return;
      }

      let streamErrored = false;
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
            status = 'complete';
          },
          onError: (er) => {
            if (myRun !== runSeq) return;
            finalReply = er.detail || 'The coordinator returned an error.';
            finalIsError = true;
            status = 'error';
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
      }
      await loadDecisions();
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
      if (Array.isArray(t.events) && t.events.length > 0) {
        events = t.events;
      }
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
    historicalDecision = null;
    status = 'pending';
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
    historicalDecision = null;
    status = 'pending';
  }

  onMount(() => {
    void loadDecisions();
  });
</script>

<header class="app-header">
  <h1 class="app-title">DriftScribe <span class="app-title__sub">— Reasoning Timeline</span></h1>
  <TokenStatus state={tokenState} onChange={onChangeToken} />
</header>

<main class="layout">
  <DecisionsRail {decisions} {activeTraceId} onOpenTrace={openTrace} />

  <section id="chat-area" class="chat-area" aria-label="Chat and reasoning timeline">
    <PauseControl {call} />
    <InfraDiagram
      {call}
      {appliedEpoch}
      {previewPr}
      onExitPreview={exitPreview}
      onAdopt={handleAdopt}
      adoptDisabled={chatDisabled}
    />
    <CapabilityCard {call} />
    <ChatForm disabled={chatDisabled} onSubmit={submitChat} prefill={chatPrefill} />
    <HistoricalBanner active={historicalActive} traceId={historicalTraceId} onNewChat={newChat} />
    <TraceBadge {traceId} {status} />
    <FinalResponse reply={finalReply} isError={finalIsError} />
    {#if historicalActive && historicalDecision}
      <DriftDiffCard decision={historicalDecision} />
    {/if}
    {#if iacPr && !historicalActive}
      <IacApprovalCta prNumber={iacPr.pr_number} />
    {/if}
    {#if busy && finalReply == null}
      <ReplyPending />
    {/if}
    {#if historicalActive && finalReply == null && historicalDecision}
      <DecisionSummary decision={historicalDecision} />
    {/if}
    <Timeline {events} {status} />
  </section>
</main>

<AuthPanel open={authPanelOpen} onSubmit={onAuthSubmit} onCancel={onAuthCancel} />

<style>
  .app-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-3) var(--ds-sp-6);
    border-bottom: 1px solid var(--ds-border);
    background: var(--ds-surface);
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
  .chat-area {
    padding: var(--ds-sp-5) var(--ds-sp-6) var(--ds-sp-8);
    max-width: var(--ds-page-max);
  }
  .chat-area > :global(*) {
    margin-bottom: var(--ds-sp-4);
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }
</style>
