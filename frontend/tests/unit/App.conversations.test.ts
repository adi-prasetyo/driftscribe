// App-level wiring for multi-turn conversations (P2): resuming a thread from
// the rail, and a chat turn settling into the thread once the coordinator
// echoes a conversation_id. The SSE transport is covered by the smoke; here we
// drive the JSON fallback path, which runs the same settle logic.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor, within } from '@testing-library/svelte';
import App from '../../src/App.svelte';

// A REAL trace id (32 lowercase hex). `?reasoning=` is validated on the way
// in, and since ds-jns the rail's open-trace writes it — so a readable token
// like 'tid-iac-1' is a fixture the affordance under test would refuse.
const TID_IAC = 'd'.repeat(32);


function okJson(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

/**
 * The crew the composer last sent to.
 *
 * With the crew picker gone there is no rendered control holding the composer's
 * workload, so the only place it is observable is the wire. That is arguably
 * the better assertion anyway: what matters is which crew the coordinator is
 * asked for, not which radio looked checked.
 */
function lastChatPostWorkload(): string | undefined {
  const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } })
    .mock.calls;
  const post = calls
    .filter(([u, i]) => String(u).includes('/chat') && i?.method === 'POST')
    .at(-1);
  if (!post?.[1]?.body) return undefined;
  return JSON.parse(String(post[1].body)).workload;
}

/** Type something and press Send, once the composer is actually enabled. */
async function sendPrompt(container: HTMLElement, text = 'ping'): Promise<void> {
  const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
  await waitFor(() => expect(input.disabled).toBe(false));
  await fireEvent.input(input, { target: { value: text } });
  await fireEvent.submit(document.getElementById('chat-form')!);
}

// A /chat Response the App will treat as an SSE stream: content-type
// text/event-stream + a ReadableStream body of `event:`/`data:` frames.
// Modeled on sseResponse in sse.test.ts:16-28, but sets the content-type
// header so App.svelte's stream-branch check (ctype.includes(...)) takes it.
function sseChatResponse(frames: string, headers: Record<string, string> = {}): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(frames));
      c.close();
    },
  });
  return new Response(stream as unknown as BodyInit, {
    headers: { 'content-type': 'text/event-stream', ...headers },
  });
}

// A /chat SSE Response that delivers `doneFrame` on the first read, then
// errors the stream on the next pull — simulating onDone firing (setting
// doneConversationId) followed by a post-done transport death (streamErrored).
// The initial chunk fills the default highWaterMark-1 queue, so `pull` only
// re-fires once the consumer's first read() has drained it — i.e. after
// consumeSse has already dispatched the done frame.
function sseChatResponseThenTransportError(doneFrame: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(doneFrame));
    },
    pull(c) {
      c.error(new Error('simulated transport failure'));
    },
  });
  return new Response(stream as unknown as BodyInit, {
    headers: { 'content-type': 'text/event-stream' },
  });
}

const GRAPH = {
  generated_at: null,
  project: 'demo-proj',
  caveat: '',
  // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
  // omitted — absent means "unverified", which pauses adoption.
  iac_snapshot_stale: false,
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 0, drift: 1 },
  groups: [],
  edges: [],
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  // A token keeps `call` from opening the auth panel mid-test.
  window.sessionStorage.setItem('driftscribe_token', 'tok');
  // Mark the tour done so its banner doesn't intercept the view.
  window.localStorage.setItem('driftscribe_tour_done', '1');
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  // openTrace scrolls the window to top; jsdom doesn't implement scrollTo.
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  // Explicit `?view=chat` since the Task 3.6 flip made a bare url resolve to
  // the desk. Every test in this file exercises chat-view behaviour (thread
  // resume, SSE turns, the composer's crew lock), none of which the desk
  // renders. The `?conversation=`-bearing tests below would reach chat anyway
  // via hasChatIntent, but they set their own url and are unaffected by this.
  history.replaceState(null, '', '/?view=chat');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — resume a conversation from the rail', () => {
  it('opens the thread, renders its turns, and snaps the composer to its crew', async () => {
    // Deliberately NOT Explore: Explore is the composer's own default, so a
    // thread on that crew could not tell "snapped to the thread" apart from
    // "never moved".
    const list = {
      conversations: [
        {
          conversation_id: 'c1',
          workload: 'provision',
          title: 'prior chat about drift',
          updated_at: new Date().toISOString(),
          turn_count: 2,
        },
      ],
    };
    const detail = {
      conversation_id: 'c1',
      workload: 'provision',
      title: 'prior chat about drift',
      turns: [
        { seq: 0, role: 'user', text: 'what changed?', workload: 'provision' },
        { seq: 1, role: 'crew', text: 'the env var EXTRA drifted', workload: 'provision', trace_id: 't1' },
      ],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) return okJson(detail);
        if (url.includes('/conversations')) return okJson(list);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, container } = render(App);

    // The rail lists the prior conversation; open it.
    await fireEvent.click(await findByTestId('conversation-open'));

    // The thread rehydrates with both turns.
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the env var EXTRA drifted')).toBeTruthy());
    expect(getByText('what changed?')).toBeTruthy();

    // The composer snapped to the thread's crew: the next prompt goes to
    // Provision, not to the Explore default it booted with.
    await sendPrompt(container);
    await waitFor(() => expect(lastChatPostWorkload()).toBe('provision'));
  });

  it('fetches no trace on open, and each turn still reaches its own reasoning', async () => {
    const list = {
      conversations: [
        {
          conversation_id: 'c1',
          workload: 'drift',
          title: 'why did EXTRA drift?',
          updated_at: new Date().toISOString(),
          turn_count: 2,
          last_trace_id: 't1',
        },
      ],
    };
    const detail = {
      conversation_id: 'c1',
      workload: 'drift',
      title: 'why did EXTRA drift?',
      last_trace_id: 't1',
      turns: [
        { seq: 0, role: 'user', text: 'why did EXTRA drift?', workload: 'drift', trace_id: 't1' },
        { seq: 1, role: 'crew', text: 'someone set it in the console', workload: 'drift', trace_id: 't1' },
      ],
    };
    // The latest turn's trace carries real reasoning: a coordinator thought, a
    // paired tool call, and an MCP call — one displayable event per group.
    const trace = {
      trace_id: 't1',
      complete: true,
      events: [
        { event: 'llm_thought', trace_id: 't1', thought_text: 'weighing the region tradeoff', timestamp: '2026-07-02T00:00:00Z' },
        { event: 'tool_call', trace_id: 't1', tool_name: 'load_iac_plan_tool', tool_args: {}, timestamp: '2026-07-02T00:00:01Z' },
        { event: 'tool_result', trace_id: 't1', tool_name: 'load_iac_plan_tool', result_ok: true, result_preview: 'ok', timestamp: '2026-07-02T00:00:02Z' },
        { event: 'mcp_call', trace_id: 't1', mcp_tool: 'search_docs', mcp_server: 'ctx7', latency_ms: 12, timestamp: '2026-07-02T00:00:03Z' },
      ],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) return okJson(detail);
        if (url.includes('/conversations')) return okJson(list);
        if (url.includes('/trace/')) return okJson(trace);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    // Opening a thread used to pull the latest turn's trace into a page-level
    // timeline, unasked. That panel is gone (ds-jns Task 3.3) and every crew
    // bubble carries its own disclosure, so the fetch went with it: one GET per
    // thread open, for one turn's reasoning nobody requested, buying nothing
    // the disclosures don't already give.
    const { findByTestId, getByText, queryByText } = render(App);

    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    expect(getByText('someone set it in the console')).toBeTruthy();

    // Several ticks, so this is the settled state and not a race the assertion
    // happened to win before an in-flight prefetch landed.
    for (let i = 0; i < 5; i++) await Promise.resolve();
    const traceCalls = (fetch as unknown as { mock: { calls: [RequestInfo | URL][] } }).mock.calls
      .filter(([u]) => String(u).includes('/trace/'));
    expect(traceCalls).toHaveLength(0);
    expect(queryByText('weighing the region tradeoff')).toBeNull();

    // …and the reasoning is one click away on the turn it belongs to, which is
    // the trade this made: nothing fetched until asked, and then exactly the
    // turn asked about.
    // Scoped to the expanded body: once the trace lands the disclosure's own
    // collapsed subtitle quotes the same thought, so a document-wide text query
    // matches twice and would pass on the subtitle alone.
    await fireEvent.click(await findByTestId('reasoning-disclosure'));
    const expanded = await findByTestId('trace-detail');
    await waitFor(() =>
      expect(within(expanded).getByText('weighing the region tradeoff')).toBeTruthy(),
    );
  });
});

describe('App — a chat turn settles into the thread', () => {
  it('appends the exchange to the thread when conversation_id is echoed', async () => {
    let listCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          return okJson(
            { reply: 'here is the answer', tool_calls: [], conversation_id: 'new-conv' },
            { 'X-Trace-Id': 'trace-xyz' },
          );
        }
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'new-conv', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) {
          listCalls += 1;
          return okJson({ conversations: [] });
        }
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-xyz', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, getAllByText, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // The exchange folds into the thread (user prompt + crew reply bubbles).
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('here is the answer')).toBeTruthy());
    expect(getByText('why did it drift?')).toBeTruthy();

    // Exactly ONE copy of the reply. There used to be a standalone hero beside
    // the thread showing the same text, kept hidden on this path; ds-jns Task
    // 3.3 deleted it, so "not shown twice" is now a statement about the DOM
    // rather than about an attribute on a second renderer.
    expect(queryByTestId('final-response')).toBeNull();
    expect(getAllByText('here is the answer')).toHaveLength(1);

    // The rail was refreshed after the turn (mount + post-settle). Awaited:
    // settle fires this without blocking the render, and the hero assertion
    // this test used to end on happened to provide the wait.
    await waitFor(() => expect(listCalls).toBeGreaterThanOrEqual(2));
  });

  it('does NOT settle a paused refusal that echoes conversation_id (no turn persisted)', async () => {
    // The kill-switch reply carries conversation_id for crew-lock symmetry but
    // persists nothing; settling it would append a bubble that vanishes on
    // reload. Since ds-jns it renders as an EPHEMERAL turn instead — in the
    // thread, so the page keeps its shape, but never folded into
    // conversationTurns and never given a ?conversation param.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          return okJson({
            reply: 'DriftScribe is paused (operator kill switch active).',
            tool_calls: [],
            paused: true,
            conversation_id: 'echoed-but-not-persisted',
          });
        }
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: 't', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => {
      expect(getByTestId('conversation-thread').textContent).toContain('paused');
    });
    // Not settled: no ?conversation. And the reply is in the thread and ONLY
    // the thread — the standalone hero that used to render it beside the
    // transcript is deleted (ds-jns Task 3.3), so there is no second surface a
    // non-persisted reply could turn up on.
    expect(new URLSearchParams(window.location.search).get('conversation')).toBeNull();
    expect(queryByTestId('final-response')).toBeNull();
  });

  it('shows an optimistic thinking bubble while the reply is in flight, then settles it in place', async () => {
    // Hold /chat open so the in-flight state (prompt bubble + "thinking" crew
    // bubble, hero suppressed) is observable, then release the reply.
    let releaseChat!: (r: Response) => void;
    const chatPromise = new Promise<Response>((res) => {
      releaseChat = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return chatPromise;
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'new-conv', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-xyz', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // In flight: the exchange renders through the thread — the operator's prompt
    // bubble plus a live "thinking" crew bubble — and the standalone hero is
    // suppressed entirely (the reply lands in the bubble; there is no hero).
    await findByTestId('conversation-thread');
    await findByTestId('thread-typing');
    expect(getByText('why did it drift?')).toBeTruthy();
    expect(queryByTestId('final-response')).toBeNull();
    // Both live bubbles are already stamped, from the client's own clock: the
    // server has persisted nothing yet, so waiting for its `created_at` would
    // make the time appear out of nowhere on the very bubble the operator is
    // watching, at the moment the reply lands (design §2).
    expect(document.querySelectorAll('[data-testid="turn-time"]')).toHaveLength(2);

    // Release the reply → it fills that same bubble, the typing indicator goes
    // away, and the turn settles into the thread. The persisted crew bubble's
    // open-trace link only appears once the turn settles, so awaiting it pins
    // the post-settle state.
    releaseChat(
      okJson(
        { reply: 'because someone set it in the console', tool_calls: [], conversation_id: 'new-conv' },
        { 'X-Trace-Id': 'trace-xyz' },
      ),
    );
    // ?conversation is written by settleConversation, so it is the honest
    // post-settle signal now that the reasoning line renders pre-settle too.
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('new-conv'),
    );
    expect(getByText('because someone set it in the console')).toBeTruthy();
    expect(queryByTestId('thread-typing')).toBeNull();
    // …in the thread, and nowhere else: no hero to clear the reply out of.
    expect(queryByTestId('final-response')).toBeNull();
  });
});

// Fast-path composer release (docs/plans/2026-07-09-chat-composer-early-unblock.md):
// on a clean, persistable SSE `done` frame, the turn settles + composer
// re-enables + ?conversation is set IMMEDIATELY, with backfillTrace/loadDecisions
// backgrounded — instead of holding the composer disabled through those two
// post-answer round-trips.
describe('App — SSE chat turn releases the composer at the done frame', () => {
  it('fast path: settles the thread and re-enables the composer without waiting for /trace', async () => {
    const frames =
      'event: meta\ndata: {"trace_id":"trace-fast"}\n\n' +
      'event: done\ndata: {"reply":"the answer arrived fast","tool_calls":[],"conversation_id":"conv-fast"}\n\n';
    let releaseTrace!: (r: Response) => void;
    const tracePromise = new Promise<Response>((res) => {
      releaseTrace = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/conversations/'))
          return okJson({ conversation_id: 'conv-fast', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        // /trace never resolves during this test — proves settle doesn't wait on it.
        if (url.includes('/trace/')) return tracePromise;
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // Settled into the thread, composer re-enabled, and ?conversation set —
    // all while /trace is still pending.
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the answer arrived fast')).toBeTruthy());
    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('conv-fast'),
    );

    releaseTrace(okJson({ trace_id: 'trace-fast', events: [], complete: true }));
  });

  it('slow path: a done-less stream still fires the recovery guard', async () => {
    const frames = 'event: meta\ndata: {"trace_id":"trace-nodone"}\n\n';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/trace/'))
          return okJson({ trace_id: 'trace-nodone', events: [], complete: true });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => {
      const thread = getByTestId('conversation-thread');
      expect(thread.textContent).toContain(
        'The reasoning stream ended before a final reply arrived.',
      );
    });
    // Unlike the paused case, this stream DID open with a meta frame, so the
    // ephemeral turn keeps its trace — whatever reasoning arrived before the
    // stream died is still reachable, and the line says the run was cut short.
    expect(getByTestId('reasoning-disclosure')).toBeTruthy();
    expect(getByTestId('reasoning-stream-error')).toBeTruthy();
    expect(getByTestId('thread-turn-error')).toBeTruthy();
    expect(queryByTestId('final-response')).toBeNull();
  });

  it('paused refusal over SSE renders as an ephemeral turn with no reasoning line', async () => {
    // FIXTURE CORRECTION (ds-jns): this used to open with a `meta` frame, which
    // prod never sends for a paused refusal — `_paused_chat_response` returns a
    // ONE-frame stream (a lone `done`, no meta, no X-Trace-Id) because no LLM
    // ran and there is no trace. The old fixture handed the SPA a trace id for
    // a turn that has none, hiding exactly the case the disclosure must not
    // render: a test sharing its subject's blind spot.
    const frames =
      'event: done\ndata: {"reply":"DriftScribe is paused (operator kill switch active).","tool_calls":[],"paused":true,"conversation_id":"echoed"}\n\n';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/trace/'))
          return okJson({ trace_id: 'trace-paused', events: [], complete: true });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // The refusal stays in the thread as an ephemeral turn — the page keeps its
    // shape — but nothing is persisted and no reasoning line appears, because
    // the turn genuinely has no trace.
    await waitFor(() => {
      expect(getByTestId('conversation-thread').textContent).toContain('paused');
    });
    expect(queryByTestId('reasoning-disclosure')).toBeNull();
    expect(queryByTestId('final-response')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('conversation')).toBeNull();
  });

  it('hybrid: a done frame followed by a transport error takes the slow path (composer stays blocked on backfill)', async () => {
    // onDone fires (doneConversationId set) and THEN the transport dies before
    // clean EOF — streamErrored ends up true alongside a set doneConversationId.
    // persistableDone excludes streamErrored, so this must NOT fast-settle: the
    // composer should stay disabled until backfillTrace/loadDecisions resolve,
    // same as any other slow-path outcome. This is the regression guard for the
    // `!streamErrored` clause in the fast-path predicate (App.svelte).
    const doneFrame =
      'event: meta\ndata: {"trace_id":"trace-hybrid"}\n\n' +
      'event: done\ndata: {"reply":"the answer landed but the transport died","tool_calls":[],"conversation_id":"conv-hybrid"}\n\n';
    let releaseTrace!: (r: Response) => void;
    const tracePromise = new Promise<Response>((res) => {
      releaseTrace = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST')
          return sseChatResponseThenTransportError(doneFrame);
        if (url.includes('/conversations/'))
          return okJson({ conversation_id: 'conv-hybrid', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return tracePromise;
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // The done frame landed (reply known) but the transport then errored — the
    // slow path awaits backfillTrace first, so settle hasn't run yet: the
    // composer stays disabled and ?conversation is unset while /trace is
    // pending. (The optimistic liveExchange bubble already renders under
    // conversation-thread at this point regardless of path — see the
    // "shows an optimistic thinking bubble" test — so it isn't a useful signal
    // here; disabled state + the settle-only ?conversation param are.)
    await new Promise((r) => setTimeout(r, 20));
    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
    expect(new URLSearchParams(window.location.search).get('conversation')).toBeNull();

    // Release /trace — the slow path's recovery guard is skipped (finalReply
    // was already set by onDone), so it proceeds straight to settle.
    releaseTrace(okJson({ trace_id: 'trace-hybrid', events: [], complete: true }));
    await findByTestId('conversation-thread');
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('conv-hybrid'),
    );
  });
});

// EXACT DTO shape from InfraDiagram.test.ts::adoptGraph() (the source of
// truth): groups carry asset_type/adoptable/count/managed/drift/sensitive;
// nodes carry id/label/asset_type/managed/location. Do NOT invent fields —
// lib/infra_graph.resourceCards() reads these exact names, and a wrong shape
// silently renders no card-adopt-btn.
const ADOPT_GRAPH = {
  generated_at: null,
  project: 'demo-proj',
  caveat: 'test caveat',
  // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
  // omitted — absent means "unverified", which pauses adoption.
  iac_snapshot_stale: false,
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 0, drift: 1 },
  groups: [
    {
      asset_type: 'storage.googleapis.com/Bucket',
      label: 'Storage bucket',
      adoptable: true,
      count: 1,
      managed: 0,
      drift: 1,
      sensitive: false,
      nodes: [
        {
          id: 'g0n0',
          label: 'my-old-uploads',
          asset_type: 'storage.googleapis.com/Bucket',
          managed: false,
          location: 'asia-northeast1',
        },
      ],
    },
  ],
  edges: [],
};

// ADOPT_GRAPH + an unmatched IaC declaration → drives the Investigate bridge.
const UNMATCHED_GRAPH = {
  ...ADOPT_GRAPH,
  unmatched_declarations: {
    count: 1,
    truncated: 0,
    entries: [
      {
        id: 'u0',
        asset_type: 'storage.googleapis.com/Bucket',
        type_label: 'Storage bucket',
        label: 'bucket-old',
        address: 'google_storage_bucket.bucket_old',
      },
    ],
  },
};

// A resumable thread held by a crew that is NOT the composer's default, so
// "the composer followed the thread" and "the composer never moved" produce
// different observable results.
function resumeFixtures() {
  const list = {
    conversations: [
      {
        conversation_id: 'c1',
        workload: 'drift',
        title: 'prior chat about drift',
        updated_at: new Date().toISOString(),
        turn_count: 2,
      },
    ],
  };
  const detail = {
    conversation_id: 'c1',
    workload: 'drift',
    title: 'prior chat about drift',
    turns: [
      { seq: 0, role: 'user', text: 'what changed?', workload: 'drift' },
      // A REAL trace id (32 lowercase hex), not a token like 't1': `?reasoning=`
      // is validated by reasoningTraceFromSearch, so a fixture that cannot
      // appear in a URL cannot exercise the deep link that names this turn.
      { seq: 1, role: 'crew', text: 'the env var EXTRA drifted', workload: 'drift', trace_id: HEX32 },
    ],
  };
  return { list, detail };
}

function stubResumeFetch(graph: unknown = GRAPH) {
  const { list, detail } = resumeFixtures();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/conversations/')) return okJson(detail);
      if (url.includes('/conversations')) return okJson(list);
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(graph);
      return okJson({});
    }),
  );
}

// A DriftScribe trace id is 32 lowercase hex chars (see lib/deeplink.ts).
const HEX32 = 'eba334f9211d46cabc79e50ed200a5a1';

// Same as stubResumeFetch, plus a generic /trace/ response — needed for tests
// that open a reasoning replay (?reasoning=<id> boot, or "view reasoning" on a
// thread turn) alongside the resumed thread.
function stubResumeFetchWithTrace(graph: unknown = GRAPH, decisions: unknown[] = []) {
  const { list, detail } = resumeFixtures();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
      if (url.includes('/conversations/')) return okJson(detail);
      if (url.includes('/conversations')) return okJson(list);
      if (url.includes('/decisions')) return okJson({ decisions });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(graph);
      return okJson({});
    }),
  );
}

describe('App — New chat + crew lock', () => {
  it('offers New chat from the rail on a fresh boot, when there is no thread to leave', async () => {
    // The control moved out of the composer (ds-jns PR 3) and stopped being
    // conditional in the same move. Both halves are asserted here, because the
    // second is what the composer version got wrong: it appeared only once
    // there was something to clear, so the operator had to learn where it
    // WOULD be before they could aim at it.
    stubResumeFetch();
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await findByTestId('rail-new-chat');
    expect(queryByTestId('composer-new-chat')).toBeNull();
  });

  it('an open thread carries the conversation and nothing else', async () => {
    // The state that USED to render the estate diagram and the capability card:
    // a fresh chat hid them behind the empty state, so a test that only checked
    // the front door would have passed with both mounts still in the tree.
    // Neither belongs to a conversation — the desk owns the estate, the modal
    // owns the cage — and Task 3.3 deleted both mounts.
    stubResumeFetch();
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    expect(queryByTestId('infra-panel')).toBeNull();
    expect(queryByTestId('capability-card')).toBeNull();
    // …and no leftover route to them either.
    expect(queryByTestId('capability-link')).toBeNull();
  });

  it('keeps sending to the resumed thread\'s crew', async () => {
    // The crew lock survived the picker's removal — it just stopped being
    // something the operator has to see and work around. There is nothing to
    // grey out any more, so the lock is observable only in where the next
    // prompt goes.
    stubResumeFetch();
    const { findByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await sendPrompt(container);
    await waitFor(() => expect(lastChatPostWorkload()).toBe('drift'));
  });

  it('New chat drops the thread and returns the composer to Explore', async () => {
    stubResumeFetch();
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('rail-new-chat'));
    await waitFor(() => {
      expect(queryByTestId('conversation-thread')).toBeNull();
    });
    // A clean slate is a clean slate: the next prompt must not inherit the
    // abandoned thread's crew, it goes back to the routing crew.
    await sendPrompt(container);
    await waitFor(() => expect(lastChatPostWorkload()).toBe('explore'));
  });

  it('an Adopt click on an open thread starts a clean slate before prefilling', async () => {
    // Driven through the desk since Task 3.3 deleted the chat's copy of the
    // diagram: resume a thread on chat, walk to the desk, adopt from there.
    // That is the real route now, and it makes the claim STRICTER rather than
    // weaker — the thread is not merely on screen when Adopt is clicked, it is
    // on a different view, and it still has to be gone when the composer
    // reappears.
    stubResumeFetch(ADOPT_GRAPH);
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('nav-desk'));
    await fireEvent.click(await findByTestId('estate-adopt-btn'));
    await waitFor(() => {
      // Thread dropped (clean slate), composer prefilled.
      expect(queryByTestId('conversation-thread')).toBeNull();
      const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
      expect(input.value).toContain('my-old-uploads');
    });
  });

  it('an Adopt click still opens a PROVISION thread, not an Explore one', async () => {
    // Adopt stays a second door into Provision even though the composer no
    // longer offers a crew choice: the click carries explicit intent from a
    // specific resource, so routing it through Explore would cost the operator
    // an extra turn and a confirmation to say what they already said. The
    // picker used to prove this with a checked radio; the wire proves it now.
    //
    // Driven from the DESK's estate section, which is where Adopt lives since
    // Task 4.1 — and the only place it lives on a FRESH chat, because the chat
    // view's copy of the diagram yields to the empty state (and PR 3 Task 3.3
    // deletes it outright). Both buttons call the same handleAdopt, so the
    // claim under test is unchanged; the door it is driven through is now the
    // one an operator would actually find.
    stubResumeFetch(ADOPT_GRAPH);
    history.replaceState(null, '', '/?view=desk');
    const { findByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('estate-adopt-btn'));
    await waitFor(() => {
      const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
      expect(input.value).toContain('my-old-uploads');
    });
    await sendPrompt(container, 'yes please');
    await waitFor(() => expect(lastChatPostWorkload()).toBe('provision'));
  });

  it('an Investigate click starts a clean Provision draft and sends no /chat', async () => {
    // Same relocation as the Adopt test above: Investigate lives in the desk's
    // estate section now (ds-zld), not in a diagram above the transcript.
    stubResumeFetch(UNMATCHED_GRAPH);
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('nav-desk'));
    await fireEvent.click(await findByTestId('estate-unmatched-investigate'));
    await waitFor(() => {
      // Fresh Provision draft (same handleAdopt bridge): thread dropped, composer
      // prefilled with the investigation prompt. The crew it targets is asserted
      // by the Adopt test above — here the point is that NOTHING is sent, so
      // this test cannot submit to observe it.
      expect(queryByTestId('conversation-thread')).toBeNull();
      const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
      expect(input.value).toContain('bucket-old');
      expect(input.value).toContain('do not assume a rename');
    });
    // The click prefills a DRAFT ONLY — it must never POST /chat.
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls;
    const chatPosts = calls.filter(
      ([input, init]) => String(input).includes('/chat') && init?.method === 'POST',
    );
    expect(chatPosts).toHaveLength(0);
  });
});

// ?conversation=<id> deep-link (docs/plans/2026-07-09-conversation-url-deeplink.md):
// bookmarkable/shareable open thread, mirroring the shipped ?reasoning=<id>
// replay param. setConversationId() is the sole writer of conversationId, so
// these wiring tests assert the URL invariant holds at every transition.
describe('App — ?conversation boot deep-link', () => {
  it('rehydrates the thread from ?conversation=<id> on boot', async () => {
    stubResumeFetch();
    history.replaceState(null, '', '/?conversation=c1');
    const { findByTestId, getByText } = render(App);
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the env var EXTRA drifted')).toBeTruthy());
  });

  it('sets ?conversation when a thread opens from the rail', async () => {
    stubResumeFetch();
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('c1'),
    );
  });

  it('clears both ?conversation and ?reasoning on New chat, preserving unrelated params + hash', async () => {
    stubResumeFetchWithTrace();
    // Both params live from the boot deep link. Since ds-jns that is the only
    // way `?reasoning=` is live ON THE CHAT VIEW: the rail's button now opens a
    // desk record, and the thread's own disclosures write no param. What the
    // test is about is unchanged — New chat sweeps its own two params and
    // leaves `unrelated=1` and the hash alone.
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}&unrelated=1#frag`);
    const { findByTestId } = render(App);
    await findByTestId('conversation-thread');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe(HEX32);
    });

    // The COMPOSER's New chat, not the replay banner's: with no replay on
    // screen there is no banner, and the composer's button is the exit the
    // operator actually has.
    await fireEvent.click(await findByTestId('rail-new-chat'));

    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBeNull();
      expect(p.get('reasoning')).toBeNull();
      expect(p.get('unrelated')).toBe('1');
    });
    expect(window.location.hash).toBe('#frag');
  });

  // ds-jns: `?reasoning=` inside a `?conversation=` names a MESSAGE in that
  // thread, so the thread opens THAT turn's disclosure rather than stacking a
  // page-level replay over the thread the message belongs to. Same URL, same
  // two params kept — a different, and much less startling, landing.
  it('restores the thread and auto-expands the named message from ?conversation&reasoning', async () => {
    stubResumeFetchWithTrace();
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('conversation-thread');
    // Expanded IN PLACE: the thread is still on screen underneath, which is the
    // whole difference from the page-level replay this replaced (and which
    // ds-jns Task 3.3 has since deleted outright).
    await findByTestId('trace-detail');
    expect(queryByTestId('conversation-thread')).toBeTruthy();
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe(HEX32);
    });
  });

  it('expands only the named message, leaving the thread’s other turns collapsed', async () => {
    // TWO crew turns with DIFFERENT trace ids — the premise the assertion needs.
    // The shared resume fixture has one, against which "exactly one expanded"
    // holds even if the thread expands every turn it can.
    const OTHER = 'f'.repeat(32);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
        if (url.includes('/conversations/'))
          return okJson({
            conversation_id: 'c1',
            workload: 'drift',
            title: 'prior chat about drift',
            turns: [
              { seq: 0, role: 'user', text: 'what changed?', workload: 'drift' },
              { seq: 1, role: 'crew', text: 'first answer', workload: 'drift', trace_id: OTHER },
              { seq: 2, role: 'user', text: 'and then?', workload: 'drift' },
              { seq: 3, role: 'crew', text: 'the env var EXTRA drifted', workload: 'drift', trace_id: HEX32 },
            ],
          });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId, getAllByTestId, queryAllByTestId } = render(App);
    await findByTestId('conversation-thread');
    await findByTestId('trace-detail');
    expect(getAllByTestId('reasoning-disclosure')).toHaveLength(2);
    expect(queryAllByTestId('trace-detail')).toHaveLength(1);
    const expanded = getAllByTestId('reasoning-disclosure').filter(
      (b) => b.getAttribute('aria-expanded') === 'true',
    );
    expect(expanded).toHaveLength(1);
  });

  // The deep link is consumed once. New chat is a clean slate, so reopening the
  // very thread it named must not silently re-expand the message — nor write
  // `?reasoning=` back onto a URL the operator just cleared.
  it('New chat consumes the deep link — reopening that thread does not re-expand it', async () => {
    stubResumeFetchWithTrace();
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('trace-detail');

    await fireEvent.click(await findByTestId('rail-new-chat'));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull(),
    );

    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    expect(queryByTestId('trace-detail')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
  });

  // canonicalizeRestoredEntry compares `?reasoning=` against what is LIVE on the
  // current surface. Before ds-jns there was one candidate — the page-level
  // replay — so an auto-expanded message would have read as stale and had its
  // param swept off a URL that describes it exactly.
  it('a pop keeps ?reasoning= when it names the message the thread has expanded', async () => {
    stubResumeFetchWithTrace();
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId } = render(App);
    await findByTestId('conversation-thread');
    await findByTestId('trace-detail');

    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe(HEX32);
    });
    await findByTestId('trace-detail');
  });

  // A `?reasoning=` inside a `?conversation=` names a MESSAGE. When the thread
  // opens successfully and contains no such message — a hand-edited URL, a link
  // outliving the turn it pointed at — the address bar must stop claiming it.
  it('drops ?reasoning= when the opened thread contains no such message', async () => {
    const ORPHAN = 'f'.repeat(32);
    stubResumeFetchWithTrace();
    history.replaceState(null, '', `/?conversation=c1&reasoning=${ORPHAN}`);
    const { findByTestId, queryByTestId, getAllByTestId } = render(App);
    await findByTestId('conversation-thread');
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull(),
    );
    expect(new URLSearchParams(window.location.search).get('conversation')).toBe('c1');
    expect(queryByTestId('trace-detail')).toBeNull();
    for (const b of getAllByTestId('reasoning-disclosure')) {
      expect(b.getAttribute('aria-expanded')).toBe('false');
    }
  });

  // A `trace_id` alone does not make a turn the one a `?reasoning=` names. The
  // thread mounts a disclosure only on a CREW bubble — a user turn carries the
  // trace of the run it started, and a declined handoff records a transition
  // trace with no crew response after it. Both are normal persisted shapes, and
  // both left the URL claiming a message that renders nothing to open.
  for (const [name, turn] of [
    [
      'a declined-handoff transition',
      { seq: 1, role: 'handoff_declined', text: 'not my area', workload: 'drift' },
    ],
    ['a user turn', { seq: 1, role: 'user', text: 'what changed?', workload: 'drift' }],
  ] as const) {
    it(`drops ?reasoning= when the only matching turn is ${name}`, async () => {
      const T = 'f'.repeat(32);
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          const url = String(input);
          if (url.includes('/trace/')) return okJson({ trace_id: T, complete: true, events: [] });
          if (url.includes('/conversations/'))
            return okJson({
              conversation_id: 'c1',
              workload: 'drift',
              title: 'prior chat',
              turns: [{ ...turn, trace_id: T, handoff: { from: 'drift', to: 'provision' } }],
            });
          if (url.includes('/conversations')) return okJson({ conversations: [] });
          if (url.includes('/decisions')) return okJson({ decisions: [] });
          if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
          if (url.includes('/infra/graph')) return okJson(GRAPH);
          return okJson({});
        }),
      );
      history.replaceState(null, '', `/?conversation=c1&reasoning=${T}`);
      const { findByTestId, queryByTestId } = render(App);
      await findByTestId('conversation-thread');
      await waitFor(() =>
        expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull(),
      );
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('c1');
      // The row renders — it is a real turn — but it mounts no disclosure.
      expect(queryByTestId('reasoning-disclosure')).toBeNull();
    });
  }

  it('clears ?conversation and honours the ?reasoning half on the desk when the boot conversation 404s', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/bad')) return new Response('not found', { status: 404 });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    // Half the link is unhonourable: there is no such thread. The other half
    // still is — the reasoning exists and is readable as a desk record. That
    // half used to open as a page-level replay inside the chat column; ds-jns
    // retired that surface, so the operator is sent to the record instead of
    // being left on an empty chat with a `?reasoning=` they can't cash in.
    history.replaceState(null, '', `/?conversation=bad&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('decision-record');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBeNull();
      expect(p.get('reasoning')).toBe(HEX32);
      expect(p.get('view')).toBeNull(); // desk is DEFAULT_VIEW — no param needed
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
    expect(queryByTestId('chat-form')).toBeNull();
  });

  it('does not open the boot reasoning replay on top if New chat interrupts the boot conversation fetch (the Codex race)', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);

    // The boot conversation fetch is in flight — conversationId was already
    // set synchronously (before the awaited fetch), so New chat is showing.
    await fireEvent.click(await findByTestId('rail-new-chat'));

    // Release the stalled boot fetch AFTER the interruption; let openConversation's
    // own runSeq guard (drops the stale detail) and the boot continuation's guard
    // (skips the queued openTrace) both run.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));

    expect(queryByTestId('conversation-thread')).toBeNull();
    expect(queryByTestId('decision-record')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
  });

  // ds-jns reversed this one. The rail's "view reasoning" used to swap the chat
  // column into replay mode ALONGSIDE the open thread, which is why both params
  // had to survive together. It now leaves chat for the desk, where the decision
  // is listed — so the thread's param goes with the thread, and the record's
  // arrives in its place. One URL, one thing on screen.
  it('leaving an open thread for a decision record swaps the URL as well as the screen', async () => {
    // The route changed with ds-jns Task 3.3: this used to be one click on a
    // decisions rail beside the chat. The rail is gone (the desk's ledger is
    // the only decision browser now), so the walk is nav -> ledger row. What is
    // under test is unchanged and is the part that was easy to get wrong: the
    // two params describe two different surfaces, and BOTH have to end up
    // matching what is on screen — a `?conversation=` left behind here would
    // reopen a thread the operator navigated away from.
    stubResumeFetchWithTrace(GRAPH, [
      { decision_id: 'd1', action: 'rollback', trace_id: HEX32, created_at: '2026-07-28T09:00:00Z' },
    ]);
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('nav-desk'));
    await fireEvent.click(await findByTestId('ledger-strip-row'));
    await findByTestId('approval-desk');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBeNull();
      expect(p.get('reasoning')).toBe(HEX32);
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
    await findByTestId('decision-record');
  });

  // Codex review 019f46e8 must-fix: conversationId is set (synchronously) before
  // GET /conversations/{id} resolves, so there's a window where the thread is
  // "open" but conversationWorkload (and therefore the crew lock) is still null.
  // Without disabling Send there, a submit during that window rides the
  // half-open thread's id with whatever crew happens to be picked — the
  // crew-switch-reset guard in submitChat can't see the mismatch because it
  // requires conversationWorkload !== null. The boot deep-link makes this easier
  // to hit than the rail-click path (a cold fetch is slower than a warm one).
  it('disables Send while a resumed thread is still rehydrating (no crew-switch race)', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
  });

  // Codex review 019f46e8 (round 2): the first fix left resumingConversation
  // stuck true forever whenever something OTHER than openConversation itself
  // supersedes a pending resume — newChat/openTrace bump runSeq, so the stale
  // openConversation's own `if (myRun === runSeq)` guard correctly refuses to
  // clear a flag it no longer owns, but nothing else cleared it either. Both
  // superseding entry points now reset it themselves.
  it('New chat during a pending resume re-enables Send instead of leaving it stuck disabled', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    // Interrupt the pending resume with New chat — Send must re-enable now,
    // not stay disabled waiting for a resume that no longer matters.
    await fireEvent.click(await findByTestId('rail-new-chat'));
    await waitFor(() => expect(sendBtn.disabled).toBe(false));

    // The stale detail landing afterwards (openConversation's own runSeq guard
    // drops it) must not re-disable Send either.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(sendBtn.disabled).toBe(false);
  });

  it('opening a reasoning replay during a pending resume does not leave Send stuck disabled after returning to chat', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    const iac = {
      decision_id: 'd1',
      trace_id: TID_IAC,
      action: 'iac_apply',
      pr_number: 47,
      apply_status: 'applied',
      approver: 'op@example.com',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/trace/')) return okJson({ trace_id: TID_IAC, complete: true, events: [], decision: iac });
        if (url.includes('/decisions')) return okJson({ decisions: [iac] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    // Interrupt the pending resume by LEAVING chat. That is the trap: the
    // departure has to clear `resumingConversation` itself, because
    // openConversation's own finally is gated on a runSeq the departure just
    // superseded. (This used to be driven by opening a decision's record from
    // the decisions rail; the rail is gone, the departure is the same one.)
    await fireEvent.click(await findByTestId('nav-desk'));
    await findByTestId('approval-desk');

    // The stale resume detail landing after the departure must not corrupt
    // anything either.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));

    // Back to chat — Send must not be stuck disabled by a leftover flag.
    // Re-queried, not reused: the chat branch unmounted on the way to the desk,
    // so the old node is detached and frozen on whatever it last said.
    await fireEvent.click(await findByTestId('nav-chat'));
    await waitFor(async () =>
      expect(((await findByTestId('chat-submit')) as HTMLButtonElement).disabled).toBe(false),
    );
  });
});

// ---------------------------------------------------------------------------
// Per-trace cache wiring (ds-jns Task 1.7).
//
// The disclosure reads a cache keyed by trace id, which App fills from the SSE
// stream. What these pin is the ORDERING and the KEYING: the `meta` frame has
// to create the entry before any timeline event, and a backfill has to land on
// the trace it belongs to even when a newer turn has already superseded the
// run that started it.
// ---------------------------------------------------------------------------

describe('App — the live stream fills the per-trace cache', () => {
  beforeEach(() => {
    history.replaceState(null, '', '/?view=chat');
  });

  it('attaches the disclosure from the meta frame, before any event, and never fetches /trace mid-stream', async () => {
    // A stream held open after `meta`: the disclosure must already be there and
    // expandable, and expanding it must NOT ask /trace for a trace that is
    // still being produced (it is not in Cloud Logging yet).
    let push!: (chunk: string) => void;
    let close!: () => void;
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        push = (chunk) => c.enqueue(new TextEncoder().encode(chunk));
        close = () => c.close();
      },
    });
    const traceCalls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          return new Response(stream as unknown as BodyInit, {
            headers: { 'content-type': 'text/event-stream' },
          });
        }
        if (url.includes('/trace/')) {
          traceCalls.push(url);
          return okJson({ trace_id: 'trace-live', events: [], complete: true });
        }
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { container, findByTestId, getByTestId, getAllByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'why did it drift?');

    push('event: meta\ndata: {"trace_id":"trace-live"}\n\n');
    // Present from `meta` alone — no timeline event has arrived yet.
    const line = await findByTestId('reasoning-disclosure');
    await fireEvent.click(line);
    await findByTestId('trace-detail');
    expect(traceCalls).toEqual([]);

    // Events stream into the OPEN disclosure.
    push('event: llm_thought\ndata: {"event":"llm_thought","trace_id":"trace-live","thought_text":"Reading the service"}\n\n');
    await waitFor(() => expect(getAllByTestId('trace-row-thought')).toHaveLength(1));
    expect(getByTestId('reasoning-subtitle').textContent).toBe('Reading the service');
    expect(traceCalls).toEqual([]);

    push('event: done\ndata: {"reply":"someone edited it in the console","tool_calls":[],"conversation_id":"c-live"}\n\n');
    close();
    // Only now — the post-`done` backfill — does /trace get asked.
    await waitFor(() => expect(traceCalls.length).toBeGreaterThan(0));
  });

  it('mirrors llm_usage into the cache so the omitted-summaries note can appear', async () => {
    // llm_usage renders no row, but omittedThoughtTokens reads it. Filtering it
    // out at the stream boundary would silently retire the PR #241 note.
    const frames =
      'event: meta\ndata: {"trace_id":"trace-usage"}\n\n' +
      'event: llm_usage\ndata: {"event":"llm_usage","trace_id":"trace-usage","thoughts_token_count":714}\n\n' +
      'event: done\ndata: {"reply":"done","tool_calls":[],"conversation_id":"c1"}\n\n';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-usage', events: [], complete: true });
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { container, findByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container);
    await fireEvent.click(await findByTestId('reasoning-disclosure'));
    // Scoped INSIDE the expanded disclosure on purpose: PR 1 is additive, so
    // the page-level Timeline panel is still mounted below and renders the same
    // note from the same events. A document-wide query would be satisfied by
    // that copy and would pass even if the disclosure never saw the usage event
    // at all.
    const detail = await findByTestId('trace-detail');
    await waitFor(() =>
      expect(within(detail).getByTestId('thought-omitted-note').textContent).toContain('714'),
    );
  });

  it('settles the backfill onto the OLD trace after a fast follow-up turn supersedes it', async () => {
    // The runSeq guard protects the GLOBAL timeline, which belongs to whatever
    // run is on screen. The cache entry is keyed, so riding that same guard
    // would orphan the previous trace — the one an operator scrolling back
    // expands. Here turn 1's backfill is deliberately stalled until turn 2 has
    // already bumped runSeq.
    let releaseTrace1!: (r: Response) => void;
    const trace1 = new Promise<Response>((r) => {
      releaseTrace1 = r;
    });
    const sse = (tid: string, cid: string) =>
      sseChatResponse(
        `event: meta\ndata: {"trace_id":"${tid}"}\n\n` +
          `event: llm_thought\ndata: {"event":"llm_thought","trace_id":"${tid}","thought_text":"streamed ${tid}"}\n\n` +
          `event: done\ndata: {"reply":"reply ${tid}","tool_calls":[],"conversation_id":"${cid}"}\n\n`,
      );
    let chatPosts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          chatPosts += 1;
          return chatPosts === 1 ? sse('trace-one', 'c1') : sse('trace-two', 'c1');
        }
        if (url.includes('/trace/trace-one')) return trace1;
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-two', events: [], complete: true });
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { container, findByTestId, getAllByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'first question');
    await waitFor(() => expect(chatPosts).toBe(1));
    // Turn 2 goes out while turn 1's backfill is still stalled → runSeq bumps.
    await sendPrompt(container, 'second question');
    await waitFor(() => expect(chatPosts).toBe(2));

    // Turn 1's /trace finally answers, carrying the trace-only mcp side-channel.
    releaseTrace1(
      okJson({
        trace_id: 'trace-one',
        complete: true,
        events: [
          {
            event: 'mcp_call',
            trace_id: 'trace-one',
            insert_id: 'm1',
            mcp_server: 'developer_knowledge',
            mcp_tool: 'search_documents',
          },
        ],
      }),
    );

    // Expanding the FIRST turn's disclosure shows the streamed thought AND the
    // backfilled mcp row — proof the settle landed on the superseded trace.
    await waitFor(() => expect(getAllByTestId('reasoning-disclosure').length).toBeGreaterThan(1));
    const first = getAllByTestId('reasoning-disclosure')[0];
    await fireEvent.click(first);
    await waitFor(() => expect(getAllByTestId('trace-row-mcp').length).toBe(1));
    expect(getAllByTestId('trace-row-thought')[0].textContent).toContain('streamed trace-one');
  });
});

describe('App — ephemeral (non-persisted) exchanges', () => {
  beforeEach(() => {
    history.replaceState(null, '', '/?view=chat');
  });

  it('renders a network failure as an error turn in the thread with no reasoning line', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') throw new Error('offline');
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, getByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'anything');
    await waitFor(() => expect(getByTestId('thread-turn-error')).toBeTruthy());
    // The request never reached the coordinator, so there is no trace at all.
    expect(queryByTestId('reasoning-disclosure')).toBeNull();
    // The operator's own prompt stays on screen beside the failure.
    expect(getByTestId('conversation-thread').textContent).toContain('anything');
  });

  it('clears the ephemeral turn on New chat', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') throw new Error('offline');
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'anything');
    await findByTestId('thread-turn-error');
    await fireEvent.click(await findByTestId('rail-new-chat'));
    await waitFor(() => expect(queryByTestId('conversation-thread')).toBeNull());
  });

  it('clears the ephemeral turn when a conversation is opened from the rail', async () => {
    let failChat = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          if (failChat) throw new Error('offline');
          return okJson({ reply: 'ok', tool_calls: [] });
        }
        if (url.includes('/conversations/')) {
          return okJson({
            conversation_id: 'c1',
            workload: 'drift',
            title: 'earlier thread',
            turns: [{ seq: 0, role: 'user', text: 'the persisted prompt' }],
          });
        }
        if (url.includes('/conversations')) {
          return okJson({
            conversations: [{ conversation_id: 'c1', workload: 'drift', title: 'earlier thread' }],
          });
        }
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, getByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'the failed prompt');
    await findByTestId('thread-turn-error');
    failChat = false;
    await fireEvent.click(await findByTestId('conversation-open'));
    await waitFor(() =>
      expect(getByTestId('conversation-thread').textContent).toContain('the persisted prompt'),
    );
    expect(queryByTestId('thread-turn-error')).toBeNull();
    expect(getByTestId('conversation-thread').textContent).not.toContain('the failed prompt');
  });

  it('does not fold an ephemeral turn into the persisted thread on the next successful send', async () => {
    // The whole reason these do not go through appendLocalTurns: a turn nobody
    // stored must not be sitting in conversationTurns when a real one settles.
    let failChat = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          if (failChat) return new Response('boom', { status: 500 });
          return okJson({ reply: 'the real reply', tool_calls: [], conversation_id: 'c9' });
        }
        if (url.includes('/trace/')) return okJson({ trace_id: 't', events: [], complete: true });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, getByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'the failed prompt');
    await findByTestId('thread-turn-error');
    failChat = false;
    await sendPrompt(container, 'the good prompt');
    await waitFor(() =>
      expect(getByTestId('conversation-thread').textContent).toContain('the real reply'),
    );
    const thread = getByTestId('conversation-thread');
    expect(queryByTestId('thread-turn-error')).toBeNull();
    expect(thread.textContent).not.toContain('the failed prompt');
    expect(thread.textContent).toContain('the good prompt');
  });
});

describe('App — a past decision still shows its reasoning after a failed exchange', () => {
  beforeEach(() => {
    history.replaceState(null, '', '/?view=chat');
  });

  it('opening a past decision still renders its rationale after a failed exchange', async () => {
    // The guarantee is unchanged; the surface that keeps it is not. The replay
    // hero was gated on there being no ephemeral exchange, so openTrace had to
    // clear one or a network failure silently blanked every replay opened
    // afterwards. The desk record has no such gate — it is a different page —
    // but it MUST still carry the decision's prose, which is the whole reason
    // the old hero existed. Losing that in the re-route would have been the
    // same defect with a nicer view transition.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') throw new Error('offline');
        if (url.includes('/trace/'))
          return okJson({
            trace_id: HEX32,
            complete: true,
            events: [],
            decision: { decision_id: 'd1', action: 'rollback', rationale: 'PORT drifted on the agent service' },
          });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions'))
          return okJson({
            decisions: [
              { decision_id: 'd1', action: 'rollback', trace_id: HEX32, created_at: '2026-07-28T09:00:00Z' },
            ],
          });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, getByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'anything');
    await findByTestId('thread-turn-error');

    await fireEvent.click(await findByTestId('nav-desk'));
    await fireEvent.click(await findByTestId('ledger-strip-row'));
    await waitFor(() => {
      expect(getByTestId('decision-record-prose').textContent).toContain(
        'PORT drifted on the agent service',
      );
    });
  });
});

// ── ds-uyo: the composer's crew menu, wired to a real thread ────────────────
//
// CrewMenu.test.ts covers the control. What can only be seen here is what a
// selection COSTS, because the cost lives in App: `newChat()` cancels an
// in-flight stream, drops the open conversation, and — the part that has to be
// tested rather than commented — ends by resetting the composer to Explore.
describe('App — choosing a crew from the composer (ds-uyo)', () => {
  const PROVISION_THREAD = {
    conversations: [
      {
        conversation_id: 'c1',
        workload: 'provision',
        title: 'adopt the bucket',
        updated_at: new Date().toISOString(),
        turn_count: 2,
      },
    ],
  };
  const PROVISION_DETAIL = {
    conversation_id: 'c1',
    workload: 'provision',
    title: 'adopt the bucket',
    turns: [
      { seq: 0, role: 'user', text: 'adopt the bucket', workload: 'provision' },
      { seq: 1, role: 'crew', text: 'opened PR #42', workload: 'provision', trace_id: 't1' },
    ],
  };

  function stubThreadFetch() {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) return okJson(PROVISION_DETAIL);
        if (url.includes('/conversations')) return okJson(PROVISION_THREAD);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
  }

  /** Resume the Provision thread from the rail and wait for it to settle. */
  async function openProvisionThread(findByTestId: (id: string) => Promise<HTMLElement>) {
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await waitFor(() =>
      expect((document.getElementById('prompt-input') as HTMLTextAreaElement).disabled).toBe(false),
    );
  }

  async function chooseCrew(crew: string) {
    await fireEvent.click(document.querySelector('[data-testid="crew-menu-trigger"]')!);
    await waitFor(() =>
      expect(document.querySelector('[data-testid="crew-menu-popup"]')).not.toBeNull(),
    );
    await fireEvent.click(document.querySelector(`[data-testid="crew-menu-option-${crew}"]`)!);
  }

  it('names the resumed thread’s crew on the trigger', async () => {
    // The defect, made visible. Before this control an Adopt click armed
    // Provision and nothing on screen said so until the reply came back.
    stubThreadFetch();
    const { findByTestId, getByTestId } = render(App);
    await openProvisionThread(findByTestId);
    expect(getByTestId('crew-menu-trigger').textContent).toContain('Provision');
  });

  it('starts a clean thread on the chosen crew, keeping the draft', async () => {
    // Anchor, not Explore, and that choice is the assertion. `newChat()` ends
    // with `composerWorkload = 'explore'`, so applying the crew FIRST and
    // clearing the thread second would silently land on Explore and still look
    // like a working control. Only a non-Explore target can tell the two apart.
    stubThreadFetch();
    const { findByTestId, queryByTestId, container } = render(App);
    await openProvisionThread(findByTestId);

    const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'why did PORT change?' } });

    await chooseCrew('drift');

    // The thread is gone from the screen — but not destroyed: its id is still
    // in /conversations, which is what the row said before it was clicked.
    await waitFor(() => expect(queryByTestId('conversation-thread')).toBeNull());
    // The half-typed question survived the transition, which is the point of
    // redirecting it rather than starting over.
    expect(input.value).toBe('why did PORT change?');

    await fireEvent.submit(document.getElementById('chat-form')!);
    await waitFor(() => expect(lastChatPostWorkload()).toBe('drift'));
  });

  it('warns before clearing a turn that NO thread is holding', async () => {
    // The case the first version of this got wrong. The hint was gated on
    // `conversationWorkload !== null` — "a thread is open" — but a paused,
    // one-shot or failed turn renders with no conversation behind it at all.
    // Choosing another crew calls newChat(), which clears that only in-memory
    // copy, and the row said nothing. It is the case that most needed the
    // warning: a persisted thread comes back from the rail and this does not.
    //
    // The draft is already gone by then (submit clears it), so the rendered
    // exchange is the whole of what is at stake.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') throw new Error('offline');
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { container, findByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    await sendPrompt(container, 'what is drifting?');
    await findByTestId('thread-turn-error');

    await fireEvent.click(document.querySelector('[data-testid="crew-menu-trigger"]')!);
    await waitFor(() =>
      expect(document.querySelector('[data-testid="crew-menu-popup"]')).not.toBeNull(),
    );
    expect(
      document.querySelector('[data-testid="crew-menu-option-drift"]')!.textContent,
    ).toContain('starts new chat');

    // And it does what it said: the exchange goes.
    await fireEvent.click(document.querySelector('[data-testid="crew-menu-option-drift"]')!);
    await waitFor(() => expect(queryByTestId('thread-turn-error')).toBeNull());
  });

  it('promises nothing on a screen with nothing on it', async () => {
    // The other side of the same rule, and the reason it is not just "always
    // show the hint": on an empty new chat a crew choice costs nothing, so
    // saying it starts a new chat would be noise attached to every row.
    stubThreadFetch();
    const { findByTestId } = render(App);
    await findByTestId('chat-prompt');

    await fireEvent.click(document.querySelector('[data-testid="crew-menu-trigger"]')!);
    await waitFor(() =>
      expect(document.querySelector('[data-testid="crew-menu-popup"]')).not.toBeNull(),
    );
    expect(
      document.querySelector('[data-testid="crew-menu-option-drift"]')!.textContent,
    ).not.toContain('starts new chat');
  });

  it('does nothing at all when the chosen crew is the one already open', async () => {
    // Confirming what you are looking at must not cost you your thread.
    stubThreadFetch();
    const { findByTestId, getByTestId } = render(App);
    await openProvisionThread(findByTestId);

    await chooseCrew('provision');

    // Still the same thread, with its turns: nothing was reset.
    expect(getByTestId('conversation-thread')).toBeTruthy();
    expect(getByTestId('crew-menu-trigger').textContent).toContain('Provision');
    await waitFor(() => expect(document.querySelector('[data-testid="crew-menu-popup"]')).toBeNull());
  });
});
