// frontend/tests/unit/App.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';
import { VIEWS, CHAT_INTENT_PARAMS, viewFromSearch } from '../../src/lib/deeplink';

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  // openTrace scrolls the window to top; jsdom doesn't implement scrollTo.
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  // Explicit `?view=chat` since the Task 3.6 flip made a BARE url resolve to
  // the desk. Most of this file exercises chat-view behaviour (composer, SSE
  // turns, thread resume, replays), which a bare url no longer reaches. Tests
  // that care about the DEFAULT itself set their own bare `/` — see the view
  // routing suite.
  history.replaceState(null, '', '/?view=chat');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/graph'))
        return okJson({
          generated_at: null,
          project: 'demo-proj',
          caveat: '',
          degraded: false,
          degraded_reason: null,
          totals: { resources: 1, managed: 0, drift: 1 },
          groups: [],
          edges: [],
        });
      return okJson({});
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — tour wiring (smoke)', () => {
  const CONV_ID = 'conv-tour-1';

  it('offers the banner on a fresh profile; Start opens the card; close marks done', async () => {
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('tour-banner')).toBeTruthy();
    await fireEvent.click(getByTestId('tour-banner-start'));
    expect(queryByTestId('tour-banner')).toBeNull();
    expect(getByTestId('tour-card')).toBeTruthy();
    await fireEvent.click(getByTestId('tour-close'));
    expect(queryByTestId('tour-card')).toBeNull();
    expect(window.localStorage.getItem('driftscribe_tour_done')).toBe('1');
  });

  // ds-s9q: the tour borrows the desk for two of its steps and hands the
  // visitor back to chat on the last one. Wiring those steps to the full
  // navigate() applied its leave-chat teardown, so "open a conversation, click
  // Tour, press Next" discarded the open thread — it survived in the rail, but
  // the view state and `?conversation=` did not. Asserting on the param is what
  // makes this a regression test: the old wiring swept it with the rest of
  // CHAT_INTENT_PARAMS.
  it('advancing the tour to a view-changing step keeps the open conversation', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    history.replaceState(null, '', `/?conversation=${CONV_ID}`);
    const { getByTestId } = render(App);

    await fireEvent.click(getByTestId('tour-open'));
    // Step 2 of TOUR_STEPS carries view:'desk' — the first one that navigates.
    await fireEvent.click(getByTestId('tour-next'));

    expect(getByTestId('estate-view')).toBeTruthy();
    expect(new URLSearchParams(window.location.search).get('conversation')).toBe(CONV_ID);
    // And the thread is still there when the tour hands the visitor back.
    await fireEvent.click(getByTestId('tour-next'));
    expect(new URLSearchParams(window.location.search).get('conversation')).toBe(CONV_ID);
  });

  it('dismissing the banner marks done; the header button reopens the tour', async () => {
    const { getByTestId, queryByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-banner-dismiss'));
    expect(queryByTestId('tour-banner')).toBeNull();
    expect(window.localStorage.getItem('driftscribe_tour_done')).toBe('1');
    await fireEvent.click(getByTestId('tour-open'));
    expect(getByTestId('tour-card')).toBeTruthy();
  });

  // ds-5yq proper: on the DESK the popover would land on the instrument band's
  // first numeral and the resting headline — the thesis screen, and the first
  // thing a judge sees on the bare domain. It does not auto-open there; the
  // bell keeps its unread badge so the notice is still one click away, and the
  // tour offer (which no longer has anything to wait for) appears at once.
  it('does not auto-open the notice on the desk, and offers the tour banner right away', () => {
    history.replaceState(null, '', '/');
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(queryByTestId('demo-notice-popover')).toBeNull();
    expect(getByTestId('tour-banner')).toBeTruthy();
    // Still discoverable — the bell and its unread badge remain.
    expect(getByTestId('demo-notice-bell')).toBeTruthy();
    expect(getByTestId('demo-notice-badge')).toBeTruthy();
    expect(window.localStorage.getItem('driftscribe_demo_notice_dismissed')).toBeNull();
  });

  // ds-2co: the estate put the same InstrumentBand in the same top-left corner
  // the popover drops into, so a shared `?view=estate` link reproduced the
  // overlap ds-5yq removed from the desk. The rule is about the layout, not
  // about one view — and it still holds now that the link ALIASES to the desk.
  it('does not auto-open the notice on a legacy ?view=estate link either', () => {
    history.replaceState(null, '', '/?view=estate');
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(getByTestId('estate-view')).toBeTruthy();
    expect(queryByTestId('demo-notice-popover')).toBeNull();
    expect(getByTestId('demo-notice-bell')).toBeTruthy();
    expect(getByTestId('demo-notice-badge')).toBeTruthy();
  });

  // Chat is the one view whose top-left is chrome, so it is the one view the
  // notice may still cover.
  it('still auto-opens the notice on a chat landing', () => {
    history.replaceState(null, '', '/?view=chat');
    const { getByTestId } = render(App);
    expect(getByTestId('demo-notice-popover')).toBeTruthy();
  });

  it('suppresses the banner when arriving with ?ask_pr intent', () => {
    history.replaceState(null, '', '/?ask_pr=102');
    const { queryByTestId, getByTestId } = render(App);
    expect(queryByTestId('tour-banner')).toBeNull();
    // The permanent reopen path still exists.
    expect(getByTestId('tour-open')).toBeTruthy();
  });

  it('lifts the fetched graph into the tour (welcome step names the project)', async () => {
    const { getByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-banner-start'));
    await waitFor(() =>
      expect(getByTestId('tour-body').textContent).toContain('demo-proj'),
    );
  });

  // Regression, Task 4.1: the tour's graph must come from the OVERVIEW STORE,
  // not from InfraDiagram's onGraph lift. That lift only fires while the CHAT
  // view is mounted — which stopped being the front door when Task 3.6 flipped
  // DEFAULT_VIEW to 'desk'. The sibling test above passes on chat and so could
  // never see this: on the DESK (a bare url) InfraDiagram never mounts, so the
  // lifted state stayed null and every graph-dependent step degraded to its
  // "still loading" / "unavailable" copy for the whole tour. Task 4.1 made this
  // worse still by routing steps 2 and 4 onto EstateView, which likewise
  // does not mount InfraDiagram — so no tour path reached a populated graph.
  // (That target was a view of its own then; since the 2026-07-31 merge it is a
  // section of the desk, which changes the address, not the diagnosis.)
  // Task 4.1's actual feature: the estate step's target moved onto EstateView,
  // which is NOT mounted while the tour is opened from the desk. So the step
  // must navigate AND the spotlight must land — and the lookup has to wait for
  // that mount, because `navigate()` writes App state from inside TourCard's
  // effect, scheduling a SEPARATE render pass that has not run when the effect
  // body continues. A synchronous querySelector therefore finds nothing and
  // silently spotlights nothing. Without this case the whole tick() deferral
  // was unpinned: the suite passed identically with the sync version.
  // Post-merge the estate section is ALREADY on the desk, so this no longer
  // proves a cross-view mount. It still pins the deferral: the step goes
  // through navigate() and the spotlight must land, and the same case run from
  // CHAT (below) keeps the mount-delay coverage the tick() guard exists for.
  it('the estate step spotlights the estate section on the desk', async () => {
    history.replaceState(null, '', '/');
    const { getByTestId } = render(App);
    expect(getByTestId('estate-view')).toBeTruthy(); // already on the desk page
    await fireEvent.click(getByTestId('tour-open'));
    await fireEvent.click(getByTestId('tour-next')); // welcome → estate
    await waitFor(() =>
      expect(
        document.querySelector('[data-tour="estate"]')?.classList.contains('tour-spotlight'),
      ).toBe(true),
    );
  });

  // The mount-delay case the tick() deferral actually guards: starting on CHAT,
  // the estate section is NOT mounted, so a synchronous querySelector after
  // navigate() finds nothing and silently spotlights nothing.
  it('the estate step navigates chat → desk AND spotlights it after the mount', async () => {
    history.replaceState(null, '', '/?view=chat');
    const { getByTestId, queryByTestId } = render(App);
    expect(queryByTestId('estate-view')).toBeNull(); // starts on chat
    await fireEvent.click(getByTestId('tour-open'));
    await fireEvent.click(getByTestId('tour-next')); // welcome → estate
    await waitFor(() => expect(getByTestId('estate-view')).toBeTruthy());
    await waitFor(() =>
      expect(
        document.querySelector('[data-tour="estate"]')?.classList.contains('tour-spotlight'),
      ).toBe(true),
    );
  });

  it('the tour opened from the DESK still has the graph (store-fed, not InfraDiagram-fed)', async () => {
    history.replaceState(null, '', '/');
    const { getByTestId, queryByTestId } = render(App);
    // Precondition — without it this test could pass by silently sitting on chat.
    expect(queryByTestId('chat-form')).toBeNull();
    await fireEvent.click(getByTestId('tour-open'));
    await waitFor(() =>
      expect(getByTestId('tour-body').textContent).toContain('demo-proj'),
    );
  });
});

describe('App — open-trace puts the replay at the top and scrolls the window up', () => {
  // The historical replay now renders at the TOP of the chat column, and
  // openTrace scrolls the WINDOW to top (top:0) to reveal it — no jump down to
  // the bottom. The button is in the left rail; the replay region is
  // #historical-badge.
  function stubFetchWithIacDecision(): void {
    const iac = {
      decision_id: 'd1',
      trace_id: 'tid-iac-1',
      action: 'iac_apply',
      pr_number: 47,
      apply_status: 'applied',
      approver: 'op@example.com',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({ trace_id: 'tid-iac-1', complete: true, events: [], decision: iac });
        if (url.includes('/decisions')) return okJson({ decisions: [iac] });
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: null,
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 1, managed: 0, drift: 1 },
            groups: [],
            edges: [],
          });
        return okJson({});
      }),
    );
  }

  it('clicking open-trace scrolls the window to top (top:0, reduced-motion → auto) and renders the replay above the composer', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithIacDecision();
    const scrollSpy = vi.fn();
    window.scrollTo = scrollSpy as unknown as typeof window.scrollTo;

    const { findByTestId, getByTestId } = render(App);

    // Wait for the rail to load the decision, then open its trace.
    const btn = await findByTestId('open-trace-button');
    await fireEvent.click(btn);

    // The banner enters the DOM (proves historicalActive flipped + tick flushed).
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());

    // The window scrolled to the top. setup.ts forces matchMedia('reduce') →
    // matches:true, so prefersReducedMotion() picks 'auto'.
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    // Exactly one scroll per open-trace.
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(scrollSpy).toHaveBeenCalledWith({ top: 0, behavior: 'auto' });
    // Focus follows the scroll so keyboard/SR users land in the replay region
    // instead of being stranded on the rail button they just clicked.
    const banner = document.getElementById('historical-badge');
    expect(document.activeElement).toBe(banner);
    // The replay region renders ABOVE the composer in document order — i.e. at
    // the top of the chat column, not below it.
    const composer = document.getElementById('chat-form');
    expect(banner).toBeTruthy();
    expect(composer).toBeTruthy();
    expect(
      banner!.compareDocumentPosition(composer!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe('App — open-trace surfaces the PR body ("what this change did")', () => {
  // For an iac_apply replay, openTrace fetches GET /trace/{id}/pr-body and shows
  // the agent-authored PR description in a disclosure below the decision card.
  function stubFetch(opts: { body: string | null; action?: string }) {
    const action = opts.action ?? 'iac_apply';
    const iac = {
      decision_id: 'd1',
      trace_id: 'tid-iac-1',
      action,
      pr_number: 47,
      head_sha: 'a'.repeat(40),
      apply_status: 'applied',
      merge_state: 'merged',
      approver: 'op@example.com',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      // pr-body MUST be checked before the generic /trace/ branch (both match).
      if (url.includes('/pr-body'))
        return okJson({
          pr_number: 47,
          head_sha: 'a'.repeat(40),
          body: opts.body,
          body_truncated: false,
          cached: false,
        });
      if (url.includes('/trace/'))
        return okJson({ trace_id: 'tid-iac-1', complete: true, events: [], decision: iac });
      if (url.includes('/decisions')) return okJson({ decisions: [iac] });
      if (url.includes('/infra/graph'))
        return okJson({
          generated_at: null,
          project: 'demo-proj',
          caveat: '',
          degraded: false,
          degraded_reason: null,
          totals: { resources: 1, managed: 0, drift: 1 },
          groups: [],
          edges: [],
        });
      return okJson({});
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('shows the PR-body disclosure with the fetched body for an iac_apply trace', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetch({ body: '## Repoints payment-demo\n\nWhy: completes the C5f isolation.' });
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('open-trace-button'));
    const panel = await findByTestId('pr-body-disclosure');
    const md = panel.querySelector('[data-testid="pr-body-md"]');
    // Rendered as Markdown now: the `##` heading marker is gone, text survives.
    expect(md?.textContent).toContain('Repoints payment-demo');
    expect(md?.textContent).not.toContain('##');
  });

  it('hides the disclosure when the PR has no body (fail-soft)', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetch({ body: null });
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('open-trace-button'));
    // The decision card settles, but no PR-body panel renders for a null body.
    await findByTestId('decision-summary');
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('does not fetch the PR body for a non-iac trace', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const fetchMock = stubFetch({ body: 'x', action: 'drift_issue' });
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('open-trace-button'));
    await findByTestId('decision-summary'); // settle
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/pr-body'))).toBe(false);
  });

  it('drops a stale PR-body response when a newer open-trace supersedes it', async () => {
    // loadPrBody is runSeq-guarded: a slow /pr-body from an earlier open-trace
    // must NOT overwrite a newer trace's body. Open A (its /pr-body blocked),
    // open B (resolves), then release A — the guard must drop A's late response.
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const decA = {
      decision_id: 'dA', trace_id: 'tid-a', action: 'iac_apply',
      pr_number: 1, head_sha: 'a'.repeat(40), apply_status: 'applied', approver: 'op',
    };
    const decB = {
      decision_id: 'dB', trace_id: 'tid-b', action: 'iac_apply',
      pr_number: 2, head_sha: 'b'.repeat(40), apply_status: 'applied', approver: 'op',
    };
    let releaseA: () => void = () => {};
    const aGate = new Promise<void>((r) => (releaseA = r));
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/pr-body')) {
          if (url.includes('tid-a')) {
            await aGate; // A's body is held until we release it
            return okJson({ pr_number: 1, head_sha: 'a'.repeat(40), body: 'A-BODY', body_truncated: false, cached: false });
          }
          return okJson({ pr_number: 2, head_sha: 'b'.repeat(40), body: 'B-BODY', body_truncated: false, cached: false });
        }
        if (url.includes('/trace/tid-a')) return okJson({ trace_id: 'tid-a', complete: true, events: [], decision: decA });
        if (url.includes('/trace/tid-b')) return okJson({ trace_id: 'tid-b', complete: true, events: [], decision: decB });
        if (url.includes('/decisions')) return okJson({ decisions: [decA, decB] });
        if (url.includes('/infra/graph'))
          return okJson({ generated_at: null, project: 'demo-proj', caveat: '', degraded: false, degraded_reason: null, totals: { resources: 1, managed: 0, drift: 1 }, groups: [], edges: [] });
        return okJson({});
      }),
    );

    const { findAllByTestId, findByTestId } = render(App);
    const buttons = await findAllByTestId('open-trace-button');
    await fireEvent.click(buttons[0]); // open A (newest first) — loadPrBody A blocks on aGate
    await fireEvent.click(buttons[1]); // open B — supersedes; loadPrBody B resolves

    const panel = await findByTestId('pr-body-disclosure');
    expect(panel.querySelector('[data-testid="pr-body-md"]')?.textContent).toContain('B-BODY');

    releaseA(); // A's stale response resolves now
    await Promise.resolve();
    await Promise.resolve();
    // The runSeq guard dropped A — B's body must remain, A's must never appear.
    const md = panel.querySelector('[data-testid="pr-body-md"]');
    expect(md?.textContent).toContain('B-BODY');
    expect(md?.textContent).not.toContain('A-BODY');
  });
});

// ds-7ag.1 — view navigation writes REAL history entries and Back restores the
// view. Every view write used to be a replaceState, so the browser Back button
// left the app entirely: "clicked a desk numeral, landed on the estate view,
// couldn't find my way back" (the 見づらい half of the judge feedback).
//
// jsdom cannot traverse history, so these assert on the WRITE (pushState vs
// replaceState spies) and simulate the restore (set the url, dispatch
// PopStateEvent). Real Back/Forward traversal is proven in the Playwright smoke
// suite (tests/smoke/history.smoke.ts). Deliberately NO assertions on
// `history.length`: jsdom can't reset it between cases, and a Back leaves
// forward entries that a later push replaces without growing it.
describe('App — history-aware view navigation (ds-7ag.1)', () => {
  const CONV_ID = 'conv-hist-1';
  const GRAPH = {
    generated_at: null,
    project: 'demo-proj',
    caveat: '',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 1, managed: 0, drift: 1 },
    groups: [],
    edges: [],
  };

  /** Spy on both history writers, keeping the real behaviour (the url must move). */
  function historySpies() {
    return {
      push: vi.spyOn(history, 'pushState'),
      replace: vi.spyOn(history, 'replaceState'),
    };
  }

  /** Simulate the browser restoring `search` via Back/Forward. */
  function popTo(search: string): void {
    history.replaceState(null, '', search);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }

  /**
   * A thread whose GET blocks on the returned gate — the shape needed to prove
   * the teardown CANCELS in-flight work rather than merely clearing fields.
   * `turns` carries no trace_id, so nothing chases an inline timeline.
   */
  function stubGatedConversation(): { release: () => void } {
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const detail = {
      conversation_id: CONV_ID,
      workload: 'explore',
      title: 'a thread',
      turns: [{ seq: 0, role: 'user', text: 'late thread turn', workload: 'explore' }],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) {
          await gate;
          return okJson(detail);
        }
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    return { release };
  }

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('a header nav click to a different view pushes one history entry', async () => {
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId } = render(App);
    const spies = historySpies();

    await fireEvent.click(getByTestId('nav-chat'));

    expect(spies.push).toHaveBeenCalledTimes(1);
    expect(String(spies.push.mock.calls[0][2])).toContain('view=chat');
  });

  // Clicking デスク while already on the desk canonicalizes `?view=desk` to `/`
  // — the url changes but the VIEW does not, so url inequality alone can't be
  // the push test or every idle click would stack a dead entry.
  it('clicking the nav button for the view already on screen never pushes', async () => {
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId } = render(App);
    const spies = historySpies();

    await fireEvent.click(getByTestId('nav-desk'));

    expect(spies.push).not.toHaveBeenCalled();
    expect(new URL(window.location.href).searchParams.get('view')).toBeNull();
  });

  // A shared ?conversation= link boots straight into chat, and openConversation
  // calls navigate('chat') on the way. That must NOT stack a duplicate entry, or
  // the visitor's first Back press looks dead (it pops to the same view).
  it('a boot deep-link restores its view without pushing an entry', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubGatedConversation().release();
    history.replaceState(null, '', `/?conversation=${CONV_ID}`);
    const spies = historySpies();

    const { findByTestId } = render(App);
    await findByTestId('conversation-thread');

    expect(spies.push).not.toHaveBeenCalled();
  });

  it('popstate restores the view named by the restored url', async () => {
    const { findByTestId } = render(App); // suite default: ?view=chat
    expect(document.getElementById('chat-form')).toBeTruthy();

    popTo('/?view=desk');

    expect(await findByTestId('instrument-band')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  // Leaving chat has to cancel the async work too, not just clear fields: a
  // GET /conversations/{id} that lands after the departure passes its own
  // runSeq guard and repopulates the thread behind the operator's back.
  it('Back off chat cancels an in-flight thread open — no late repopulate', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const { release } = stubGatedConversation();
    history.replaceState(null, '', `/?conversation=${CONV_ID}`);
    const { findByTestId, queryByTestId } = render(App);

    popTo('/?view=desk'); // Back, while the GET is still in flight
    await findByTestId('instrument-band');

    release();
    await Promise.resolve();
    await Promise.resolve();

    popTo('/?view=chat'); // Forward into chat: the thread must not be there
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    expect(queryByTestId('conversation-thread')).toBeNull();
  });

  // Cancelling an in-flight run is only half the job: the surface it had already
  // built has to go with it. Leaving `busy` cleared but the timeline and status
  // in place left a partial run on screen whose status said `streaming` forever
  // — the guard that would have completed it is the one that just bailed.
  it('leaving chat mid-stream leaves no half-cancelled run behind', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    // A /chat response that never resolves: the run is in flight for the whole test.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return new Promise<Response>(() => {});
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/?view=chat');
    const { getByTestId, queryByTestId, findByTestId } = render(App);

    const input = document.querySelector('#prompt-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    await fireEvent.input(input, { target: { value: 'ping' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    // The live reply streams into the thread's own crew bubble, so the
    // in-flight signal is the typing bubble, not the standalone shimmer.
    await findByTestId('thread-typing');

    await fireEvent.click(getByTestId('nav-desk'));
    await fireEvent.click(getByTestId('nav-chat'));

    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    expect(queryByTestId('thread-typing')).toBeNull();
    expect(queryByTestId('reply-pending')).toBeNull();
    expect(queryByTestId('conversation-thread')).toBeNull();
    // And the composer is usable again rather than stuck disabled behind `busy`.
    const back = document.querySelector('#prompt-input') as HTMLTextAreaElement;
    expect(back.disabled).toBe(false);
  });

  // The restore is view-only, so a restored entry can still name content this
  // session deliberately tore down. The url must stop claiming it — and since
  // the chat-intent param is what forced the chat view, the same rewrite has to
  // say `view=chat` explicitly or a reload would land on the desk.
  it('a restored entry stops claiming content that is no longer open', async () => {
    history.replaceState(null, '', '/?view=desk');
    const { findByTestId } = render(App);
    await findByTestId('instrument-band');
    const spies = historySpies();

    popTo(`/?conversation=${CONV_ID}`);

    expect(spies.replace).toHaveBeenCalledTimes(2); // popTo's own write, then the canonicalization
    const written = new URL(String(spies.replace.mock.calls[1][2]), window.location.origin);
    expect(written.searchParams.get('conversation')).toBeNull();
    expect(written.searchParams.get('view')).toBe('chat');
  });

  // A live entry is not stale: the param names exactly what is on screen, so
  // the canonicalization must leave it alone.
  it('a restored entry naming the OPEN thread is left untouched', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubGatedConversation().release();
    history.replaceState(null, '', `/?conversation=${CONV_ID}`);
    const { findByTestId } = render(App);
    await findByTestId('conversation-thread');
    const spies = historySpies();

    window.dispatchEvent(new PopStateEvent('popstate'));

    expect(spies.replace).not.toHaveBeenCalled();
    expect(new URL(window.location.href).searchParams.get('conversation')).toBe(CONV_ID);
  });

  // Back while the tour is borrowing a view: the overlay closes, and its
  // view-restore must NOT fire — that would replace the very entry the operator
  // just returned to.
  it('Back during the tour closes it and lands on the restored view', async () => {
    history.replaceState(null, '', '/?view=chat');
    const { getByTestId, findByTestId, queryByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-open'));
    await fireEvent.click(getByTestId('tour-next')); // step 2 navigates to the desk
    expect(getByTestId('estate-view')).toBeTruthy();

    popTo('/?view=chat');

    // Back to chat: the tour is gone and so is the desk it borrowed. Started
    // from chat deliberately — with the estate merged into the desk, a
    // desk→desk borrow would assert nothing about the restore.
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    expect(queryByTestId('tour-card')).toBeNull();
    expect(queryByTestId('estate-view')).toBeNull();
    expect(queryByTestId('instrument-band')).toBeNull();
  });

  // Codex review of this branch: the tour borrows a view while PRESERVING the
  // chat surface, so a Back press mid-tour arrives with `view === 'desk'` (the
  // borrowed view) and an open thread still live behind it. Gating the teardown on
  // `view === 'chat'` skipped it there, leaving a hidden thread whose later
  // settle would write ?conversation= onto a DESK url — the exact state/url
  // disagreement this handler exists to prevent. Needs a REAL open thread; the
  // sibling tour case above has none, which is why it missed this.
  it('Back out of a tour-borrowed view still tears down the chat surface behind it', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubGatedConversation().release();
    history.replaceState(null, '', `/?conversation=${CONV_ID}`);
    const { getByTestId, queryByTestId, findByTestId } = render(App);
    await findByTestId('conversation-thread');

    await fireEvent.click(getByTestId('tour-open'));
    await fireEvent.click(getByTestId('tour-next')); // borrows the desk view
    expect(getByTestId('estate-view')).toBeTruthy();

    popTo('/?view=desk'); // Back — arrives while `view` is 'desk', not 'chat'
    await findByTestId('instrument-band');

    // The thread is gone, and the desk url does not claim it.
    expect(new URL(window.location.href).searchParams.get('conversation')).toBeNull();
    await fireEvent.click(getByTestId('nav-chat'));
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    expect(queryByTestId('conversation-thread')).toBeNull();
  });

  // The tour borrows views for two of its steps; it must not leave a history
  // entry per step for the operator to dig back out of.
  it('the tour never pushes entries while borrowing views', async () => {
    const { getByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-open'));
    const spies = historySpies();

    await fireEvent.click(getByTestId('tour-next')); // → desk (estate step)
    await fireEvent.click(getByTestId('tour-next')); // → controls
    await fireEvent.click(getByTestId('tour-close')); // restores the start view

    expect(spies.push).not.toHaveBeenCalled();
  });
});

describe('App — view routing (Task 2.2)', () => {
  // 32-hex trace id — the shape reasoningTraceFromSearch/HEX32_RE require.
  const TID = 'a'.repeat(32);

  function stubFetchWithTrace(): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({ trace_id: TID, complete: true, events: [], decision: null });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: null,
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 1, managed: 0, drift: 1 },
            groups: [],
            edges: [],
          });
        return okJson({});
      }),
    );
  }

  // Task 3.6 step 2 flipped DEFAULT_VIEW to 'desk'. A BARE url (no ?view=) is
  // the case that matters — it's what a judge typing the domain gets — so this
  // deliberately overrides the suite-wide `?view=chat` default set above.
  it('a bare url defaults to the approval desk, estate section and all, composer absent', () => {
    history.replaceState(null, '', '/');
    const { getByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(getByTestId('estate-view')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  it('renders the chat layout for an explicit ?view=chat', () => {
    history.replaceState(null, '', '/?view=chat');
    const { queryByTestId } = render(App);
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(queryByTestId('approval-desk')).toBeNull();
    expect(queryByTestId('estate-view')).toBeNull();
  });

  it('renders the desk placeholder for ?view=desk, with the composer absent', () => {
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  // Legacy alias: shared ?view=estate links predate the merge and must still
  // land somewhere real — the desk, which now contains the estate.
  it('renders the DESK for a legacy ?view=estate link, with the composer absent', () => {
    history.replaceState(null, '', '/?view=estate');
    const { getByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(getByTestId('estate-view')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  it('the estate section lives inside the desk view, and there is no estate nav button', () => {
    history.replaceState(null, '', '/');
    const { getByTestId, queryByTestId } = render(App);
    const desk = getByTestId('approval-desk');
    const estate = getByTestId('estate-view');
    expect(desk).toBeTruthy();
    expect(estate).toBeTruthy();
    // Siblings on one page, band → hero → ledger → estate.
    expect(desk.compareDocumentPosition(estate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(queryByTestId('nav-estate')).toBeNull();
    expect(getByTestId('nav-desk')).toBeTruthy();
    expect(getByTestId('nav-chat')).toBeTruthy();
  });

  it('a chat intent (?reasoning=) wins over an explicit ?view=desk — chat renders, not the desk placeholder', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithTrace();
    history.replaceState(null, '', `/?view=desk&reasoning=${TID}`);
    const { queryByTestId } = render(App);
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(queryByTestId('approval-desk')).toBeNull();
    // The boot deep-link replay actually opened, proving the chat view is truly live.
    await waitFor(() => expect(queryByTestId('historical-banner')).toBeTruthy());
  });

  it('header nav switches views, marks the active one with aria-current, and back to chat restores the composer', async () => {
    const { getByTestId, queryByTestId } = render(App);
    const deskBtn = getByTestId('nav-desk');
    const chatBtn = getByTestId('nav-chat');
    expect(chatBtn.getAttribute('aria-current')).toBe('page');

    await fireEvent.click(deskBtn);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
    expect(deskBtn.getAttribute('aria-current')).toBe('page');
    expect(chatBtn.getAttribute('aria-current')).not.toBe('page');
    // navigate() omits ?view= for whatever DEFAULT_VIEW is, so the polarity
    // here INVERTED at the Task 3.6 flip: the desk is now the paramless
    // canonical url (a judge typing the bare domain lands here), and chat is
    // the one that carries an explicit param.
    expect(new URL(window.location.href).searchParams.get('view')).toBeNull();

    await fireEvent.click(chatBtn);
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(queryByTestId('approval-desk')).toBeNull();
    expect(new URL(window.location.href).searchParams.get('view')).toBe('chat');
  });

  it('navigating away from chat with an open replay clears reasoning/conversation/ask_pr/preview_pr in the same write that sets view, and closes the replay', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithTrace();
    // Arrive with an open replay AND a leftover ask_pr/preview_pr to prove all
    // four params are swept, not just reasoning.
    history.replaceState(null, '', `/?reasoning=${TID}&ask_pr=9&preview_pr=9`);
    const { getByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());

    await fireEvent.click(getByTestId('nav-desk'));

    const search = new URLSearchParams(window.location.search);
    // Desk is DEFAULT_VIEW post-flip, so navigate() drops the param entirely
    // rather than writing view=desk — the assertion that matters here is that
    // the chat-intent params were swept in that SAME write (below).
    expect(search.get('view')).toBeNull();
    // Iterate the shared list rather than restating it: a fifth chat-intent
    // param added to CHAT_INTENT_PARAMS is then covered here automatically.
    for (const p of CHAT_INTENT_PARAMS) expect(search.has(p)).toBe(false);
    // The replay itself closed — returning to chat must not resurrect it.
    expect(getByTestId('approval-desk')).toBeTruthy();
    await fireEvent.click(getByTestId('nav-chat'));
    expect(queryByTestId('historical-banner')).toBeNull();
  });

  // Round-trip: whatever view you navigate to, the URL that navigate() leaves
  // behind must resolve back to that SAME view. navigate() omits the `view`
  // param for whichever view is DEFAULT_VIEW, so this property is what keeps it
  // honest — and it is deliberately written against the real DEFAULT_VIEW
  // rather than the literal 'chat'. When Task 3.6 flips DEFAULT_VIEW to 'desk',
  // an implementation that hardcoded the omission to 'chat' starts writing a
  // param-less URL for chat that reloads as the desk, and THIS test fails.
  it('every navigated view round-trips through viewFromSearch (guards the Task 3.6 flip)', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const { getByTestId, container } = render(App);
    await waitFor(() => expect(getByTestId('nav-desk')).toBeTruthy());

    // Derived from the RENDERED nav rather than from VIEWS. The two agree again
    // now that 'estate' is out of VIEWS, but reading the DOM is the stronger
    // assertion: it fails if a view gains a button without a round-trip, or
    // keeps a button after leaving the allowlist. Iterating VIEWS could only
    // ever check the views VIEWS already knows about.
    const navViews = Array.from(container.querySelectorAll('.app-header__nav button')).map((b) =>
      (b.getAttribute('data-testid') ?? '').replace(/^nav-/, ''),
    );
    expect(navViews).toEqual(['desk', 'chat']);

    for (const v of navViews) {
      await fireEvent.click(getByTestId(`nav-${v}`));
      expect(viewFromSearch(window.location.search)).toBe(v);
    }
  });

  // Superseded by the Task 3.5 "rails come off the desk" decision: the
  // decisions rail (and its open-trace-button) is no longer rendered on the
  // desk view at all (see the "App — rails come off the desk" suite below),
  // so "open a trace from the rail while on the desk" is no longer a
  // reachable interaction. This keeps the still-valid part of the old test —
  // switching to chat surfaces the rail and opening a trace from it works —
  // with the desk→chat navigation step made explicit first.
  it('the rail (and its open-trace affordance) only exists on chat; navigating there and opening a trace works', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const iac = {
      decision_id: 'd1',
      trace_id: TID,
      action: 'iac_apply',
      pr_number: 47,
      apply_status: 'applied',
      approver: 'op@example.com',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({ trace_id: TID, complete: true, events: [], decision: iac });
        if (url.includes('/decisions')) return okJson({ decisions: [iac] });
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: null,
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 1, managed: 0, drift: 1 },
            groups: [],
            edges: [],
          });
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/?view=desk');
    const { findByTestId, getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(queryByTestId('open-trace-button')).toBeNull(); // no rail on the desk

    await fireEvent.click(getByTestId('nav-chat'));
    const btn = await findByTestId('open-trace-button');
    await fireEvent.click(btn);

    expect(queryByTestId('approval-desk')).toBeNull();
    expect(document.getElementById('chat-form')).toBeTruthy();
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());
  });
});

describe('App — rails come off the desk (Task 3.5)', () => {
  // 32-hex trace id, mirrors the "view routing" describe block's own TID.
  const TID = 'b'.repeat(32);

  it('the rails render on chat (default) and are absent on the desk, estate section included', () => {
    const { getByTestId } = render(App);
    expect(getByTestId('rails')).toBeTruthy();
    cleanup();

    history.replaceState(null, '', '/?view=desk');
    const desk = render(App);
    expect(desk.queryByTestId('rails')).toBeNull();
    expect(desk.getByTestId('approval-desk')).toBeTruthy();
    expect(desk.getByTestId('estate-view')).toBeTruthy();
  });

  // Codex finding baked into the plan: the guarantee the railless desk rests
  // on is that NO chat-intent query param can ever strand a visitor on a
  // railless desk — deeplink.ts's hasChatIntent() already forces view==='chat'
  // for ?reasoning=/?conversation=/?ask_pr=/?preview_pr=, overriding even an
  // explicit ?view=desk. This test pins that a shared ?reasoning= link both
  // resolves to chat AND renders the rails — so a future refactor of either
  // the view resolver or the rails' own {#if} can't quietly break it apart.
  it('a shared ?reasoning=<hex32> link resolves to chat, with the rails rendered (never a railless stranding)', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({ trace_id: TID, complete: true, events: [], decision: null });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: null,
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 1, managed: 0, drift: 1 },
            groups: [],
            edges: [],
          });
        return okJson({});
      }),
    );
    // Even an explicit ?view=desk must lose to the reasoning intent.
    history.replaceState(null, '', `/?view=desk&reasoning=${TID}`);
    const { getByTestId, queryByTestId } = render(App);
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(queryByTestId('approval-desk')).toBeNull();
    expect(getByTestId('rails')).toBeTruthy();
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());
  });

  // …and the two intents that have NO independent redirect.
  //
  // The test above passes even if hasChatIntent is removed from the view
  // resolver entirely: `?reasoning=` also triggers App's onMount openTrace(),
  // and openTrace() itself calls navigate('chat'). `?conversation=` is
  // likewise double-protected by openConversation(). `?ask_pr=` and
  // `?preview_pr=` are NOT — they have no self-redirect anywhere in App, so
  // hasChatIntent is the only thing standing between a visitor following an
  // Adopt-flow or approval-page link and a railless desk with no composer to
  // act on. These two params are exactly where the railless-desk guarantee is
  // load-bearing, so they get their own App-level pin rather than resting on
  // deeplink.ts's unit tests alone.
  for (const param of ['ask_pr', 'preview_pr'] as const) {
    it(`a ?${param}= link resolves to chat with the rails rendered, even against an explicit ?view=desk`, () => {
      history.replaceState(null, '', `/?view=desk&${param}=168`);
      const { getByTestId, queryByTestId } = render(App);
      expect(queryByTestId('approval-desk')).toBeNull();
      expect(document.getElementById('chat-form')).toBeTruthy();
      expect(getByTestId('rails')).toBeTruthy();
    });
  }
});

describe('App — estate section (Task 4.1)', () => {
  const BUCKET = 'storage.googleapis.com/Bucket';

  // A graph with one real adoptable drift node (shipping-topic) — used by
  // every test in this block that needs an actual row / adopt chip to click,
  // as opposed to the suite-wide beforeEach stub (which returns `groups: []`,
  // so its `drift: 1` total never resolves to a nameable row).
  function stubFetchWithAdoptableGraph(): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: '2026-07-28T06:00:00Z',
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 2, managed: 0, drift: 1 },
            groups: [
              {
                asset_type: BUCKET,
                label: 'Storage bucket',
                count: 1,
                managed: 0,
                drift: 1,
                sensitive: false,
                adoptable: true,
                nodes: [
                  {
                    id: 'b0',
                    label: 'shipping-topic',
                    asset_type: BUCKET,
                    managed: false,
                    location: 'asia-northeast1',
                  },
                ],
              },
            ],
            edges: [],
          });
        return okJson({});
      }),
    );
  }

  it('renders real drift rows sourced from the overview store', async () => {
    stubFetchWithAdoptableGraph();
    history.replaceState(null, '', '/');
    const { findByTestId } = render(App);
    const row = await findByTestId('estate-row');
    expect(row.textContent).toContain('shipping-topic');
  });

  // ds-7ag.2's per-stat routing survives the merge, but the destination is on
  // this same page now: the numeral SCROLLS to the estate section. A scroll is
  // not navigation, so it must leave no history entry and not touch the URL —
  // otherwise Back would have a phantom step to undo.
  it('an instrument-band numeral scrolls to the estate section without navigating', async () => {
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId } = render(App);
    await waitFor(() => expect(getByTestId('instrument-band-drift')).toBeTruthy());
    // Spied AFTER render so boot-time writes don't count. Asserting the URL
    // string alone is not enough: a regression that pushed the SAME url would
    // leave a phantom Back step and still pass (Codex review of this branch).
    const push = vi.spyOn(history, 'pushState');
    const replace = vi.spyOn(history, 'replaceState');
    // The suite-wide beforeEach installs a fresh scrollIntoView mock per test.
    const scrollSpy = window.HTMLElement.prototype.scrollIntoView as ReturnType<typeof vi.fn>;
    const before = window.location.href;

    await fireEvent.click(getByTestId('instrument-band-drift'));

    // setup.ts forces matchMedia('reduce') → matches:true, so
    // prefersReducedMotion() picks 'auto'. Asserting the ARGUMENTS is what
    // pins the reduced-motion branch — an unconditional 'smooth' would
    // otherwise pass this test unnoticed.
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
    // A scroll is not navigation: no history entry, no url rewrite.
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    expect(window.location.href).toBe(before);
    // Focus follows the scroll, or a keyboard user stays parked on the band
    // button that just scrolled out of view.
    expect(document.activeElement).toBe(getByTestId('estate-view'));
  });

  // The design's central merge invariant: the hero's state machine and the
  // estate section's own status are INDEPENDENT. Each area reports its own
  // truth rather than one dragging the other into a shared "something is
  // wrong" state (ds-eh6, "unknown ≠ empty"). Both halves are covered at
  // component level; these pin the COMPOSITION, which is where the merge could
  // couple them.
  //
  // The two cases are deliberately the two DIRECTIONS, chosen so each can
  // actually fail: overviewStore keeps a graph failure out of `degraded`
  // (overviewStore.ts:251), so a graph outage moves ONLY the estate, and a
  // decisions outage moves ONLY the hero.
  //
  // NB a provable pending decision outranks `degraded` in deskModel (desk.ts:645
  // — absence is the only claim load state can veto), so a pending-hero fixture
  // would render `pending` no matter how the props were wired and would pin
  // nothing here. Hence resting/unknown.
  it('a failed estate fetch does not drag the hero out of its all-clear', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // Both pending-work lanes answer cleanly → the desk CAN say "all clear".
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        // …while the estate map could not be read at all.
        if (url.includes('/infra/graph')) return new Response('boom', { status: 500 });
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, getByTestId, queryByTestId } = render(App);

    // The estate admits it could not be read — never a fake-empty "all clear".
    expect(await findByTestId('estate-degraded')).toBeTruthy();
    expect(queryByTestId('estate-row')).toBeNull();
    // The hero keeps its own (separately established) truth.
    expect(getByTestId('approval-desk-resting')).toBeTruthy();
    expect(queryByTestId('approval-desk-unknown')).toBeNull();
    // …and is honest about the scan time it no longer has.
    expect(getByTestId('approval-desk-watch').textContent).toContain('scan time unavailable');
  });

  it('a degraded hero does not drag the estate section down with it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        // /decisions fails → overviewStore sets degraded → the hero cannot
        // claim the all-clear.
        if (url.includes('/decisions')) return new Response('boom', { status: 500 });
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        // …while the estate map read perfectly well.
        if (url.includes('/infra/graph'))
          return okJson({
            generated_at: '2026-07-31T06:00:00Z',
            project: 'demo-proj',
            caveat: '',
            degraded: false,
            degraded_reason: null,
            totals: { resources: 2, managed: 0, drift: 1 },
            groups: [
              {
                asset_type: BUCKET,
                label: 'Storage bucket',
                count: 1,
                managed: 0,
                drift: 1,
                sensitive: false,
                adoptable: true,
                nodes: [
                  {
                    id: 'b0',
                    label: 'shipping-topic',
                    asset_type: BUCKET,
                    managed: false,
                    location: 'asia-northeast1',
                  },
                ],
              },
            ],
            edges: [],
          });
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, getByTestId, queryByTestId } = render(App);

    // The hero admits the gap. waitFor the REASON, not just the element: the
    // unknown state is also what a still-loading first cycle renders, so
    // asserting on first sight would pass on 'loading' and prove nothing.
    await waitFor(() =>
      expect(getByTestId('approval-desk-unknown').getAttribute('data-reason')).toBe('degraded'),
    );
    expect(queryByTestId('approval-desk-resting')).toBeNull();
    // …while the estate renders the rows it genuinely read.
    const row = await findByTestId('estate-row');
    expect(row.textContent).toContain('shipping-topic');
    expect(queryByTestId('estate-degraded')).toBeNull();
    expect(queryByTestId('estate-loading')).toBeNull();
  });

  it('an adopt chip on the estate section lands the operator on a prefilled composer', async () => {
    stubFetchWithAdoptableGraph();
    history.replaceState(null, '', '/');
    const { findByTestId } = render(App);
    const adoptBtn = await findByTestId('estate-adopt-btn');
    await fireEvent.click(adoptBtn);
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    const textarea = document.querySelector('[data-testid="chat-prompt"]') as HTMLTextAreaElement;
    expect(textarea.value).toContain('shipping-topic');
  });

  it('the tour adopt step navigates chat → desk and spotlights the first adoptable row (survives the navigate → mount delay)', async () => {
    stubFetchWithAdoptableGraph();
    const { getByTestId, findByTestId } = render(App); // default: chat view
    await fireEvent.click(getByTestId('tour-banner-start'));
    await fireEvent.click(getByTestId('tour-next')); // welcome → estate
    await fireEvent.click(getByTestId('tour-next')); // estate → controls
    await fireEvent.click(getByTestId('tour-next')); // controls → adopt (navigates to 'desk')
    const row = await findByTestId('estate-row');
    await waitFor(() => expect(row.classList.contains('tour-spotlight')).toBe(true));
  });
});
