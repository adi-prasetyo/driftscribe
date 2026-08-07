// frontend/tests/unit/App.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';
import { VIEWS, CHAT_INTENT_PARAMS, viewFromSearch } from '../../src/lib/deeplink';

// A REAL trace id (32 lowercase hex). `?reasoning=` is validated on the way
// in, and since ds-jns the rail's open-trace writes it — so a readable token
// like 'tid-iac-1' is a fixture the affordance under test would refuse.
const TID_IAC = 'd'.repeat(32);


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
          // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
          // omitted — absent means "unverified", which pauses adoption.
          iac_snapshot_stale: false,
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

describe('App — open-trace surfaces the PR body ("what this change did")', () => {
  // For an iac_apply replay, openTrace fetches GET /trace/{id}/pr-body and shows
  // the agent-authored PR description in a disclosure below the decision card.
  function stubFetch(opts: { body: string | null; action?: string }) {
    const action = opts.action ?? 'iac_apply';
    const iac = {
      decision_id: 'd1',
      trace_id: TID_IAC,
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
        return okJson({ trace_id: TID_IAC, complete: true, events: [], decision: iac });
      if (url.includes('/decisions')) return okJson({ decisions: [iac] });
      if (url.includes('/infra/graph'))
        return okJson({
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
        });
      return okJson({});
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  // The same guarantee, re-anchored to the surface that now serves it. It used
  // to rest on openTrace's runSeq guard dropping a stale /pr-body; the desk
  // record rests on something stronger — the per-trace cache writes every
  // pr-body under the trace id it was fetched for, so a slow response cannot
  // reach a different record at all. Opening A then B is what proves it: A's
  // body arrives after B is on screen and has nowhere to land but A's entry.
  it('a slow PR body from an earlier record never lands on the one now open', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    const TID_A = 'a'.repeat(32);
    const TID_B = 'c'.repeat(32);
    const decA = {
      decision_id: 'dA', trace_id: TID_A, action: 'iac_apply', created_at: '2026-07-28T10:00:00Z',
      pr_number: 1, head_sha: 'a'.repeat(40), apply_status: 'applied', approver: 'op',
    };
    const decB = {
      decision_id: 'dB', trace_id: TID_B, action: 'iac_apply', created_at: '2026-07-28T09:00:00Z',
      pr_number: 2, head_sha: 'b'.repeat(40), apply_status: 'applied', approver: 'op',
    };
    let releaseA: () => void = () => {};
    const aGate = new Promise<void>((r) => (releaseA = r));
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/pr-body')) {
          if (url.includes(TID_A)) {
            await aGate; // A's body is held until we release it
            return okJson({ pr_number: 1, head_sha: 'a'.repeat(40), body: 'A-BODY', body_truncated: false, cached: false });
          }
          return okJson({ pr_number: 2, head_sha: 'b'.repeat(40), body: 'B-BODY', body_truncated: false, cached: false });
        }
        if (url.includes(`/trace/${TID_A}`)) return okJson({ trace_id: TID_A, complete: true, events: [], decision: decA });
        if (url.includes(`/trace/${TID_B}`)) return okJson({ trace_id: TID_B, complete: true, events: [], decision: decB });
        if (url.includes('/decisions')) return okJson({ decisions: [decA, decB] });
        if (url.includes('/infra/graph'))
          return okJson({ generated_at: null, project: 'demo-proj', caveat: '', degraded: false, degraded_reason: null, totals: { resources: 1, managed: 0, drift: 1 }, groups: [], edges: [] });
        return okJson({});
      }),
    );

    history.replaceState(null, '', '/?view=desk');
    const { findAllByTestId, findByTestId } = render(App);
    const rows = await findAllByTestId('ledger-strip-row');
    await fireEvent.click(rows[0]); // A (newest first) — its /pr-body blocks on aGate
    await fireEvent.click((await findAllByTestId('ledger-strip-row'))[1]); // B — resolves

    const panel = await findByTestId('pr-body-disclosure');
    await waitFor(() =>
      expect(panel.querySelector('[data-testid="pr-body-md"]')?.textContent).toContain('B-BODY'),
    );

    releaseA(); // A's stale response resolves now
    await Promise.resolve();
    await Promise.resolve();
    // Keyed by trace id — B's body remains, A's never appears on B's record.
    const md = (await findByTestId('pr-body-disclosure')).querySelector('[data-testid="pr-body-md"]');
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
    // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
    // omitted — absent means "unverified", which pauses adoption.
    iac_snapshot_stale: false,
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
            // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
            // omitted — absent means "unverified", which pauses adoption.
            iac_snapshot_stale: false,
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

  // ds-jns reversed this one. A BARE ?reasoning= used to force chat and replay
  // the trace there; it now names a decision RECORD, which the desk renders.
  // The explicit ?view=desk it used to lose to is now agreeing with it.
  it('a bare ?reasoning= resolves to the desk and opens the record there', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithTrace();
    history.replaceState(null, '', `/?reasoning=${TID}`);
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
    // Not merely routed — the record is actually on screen.
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
  });

  // The one ?reasoning= shape that still means the chat replay: the links this
  // app wrote before the fork carry ?view=chat, and PR 3 removes the writer.
  it('?view=chat&reasoning= lands on the record, because chat has nowhere to put a bare trace', async () => {
    // deeplink's hasChatIntent() still routes this URL to chat — that rule is
    // about the SHAPE of the link and is unchanged. What changed is what chat
    // does with a `?reasoning=` that names no message in any open thread: it
    // used to open a page-level replay, and ds-jns Task 3.3 deleted that
    // surface. So chat mounts, finds nothing to render the trace with, and
    // hands it to the record on the desk rather than dropping it.
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithTrace();
    history.replaceState(null, '', `/?view=chat&reasoning=${TID}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('decision-record');
    expect(document.getElementById('chat-form')).toBeNull();
    expect(queryByTestId('approval-desk')).toBeTruthy();
    // …and the link still describes what is on screen after the hand-off.
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBe(TID);
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

  it('leaving chat sweeps every chat-intent param in the same write that sets view', async () => {
    // What is pinned is the BEHAVIOUR — a URL that stops advertising chat state
    // the moment the operator leaves it — not whichever line currently delivers
    // it. Two arms do, and neither alone reddens this: navigate()'s
    // CHAT_INTENT_PARAMS sweep, and teardownChatSurface -> setConversationId,
    // the single writer that keeps `?conversation=` in step with the state.
    // Both were injected; both left this green, which is what defence in depth
    // looks like from a test's side.
    //
    // Direction matters and cost an earlier draft its teeth: the sweep is gated
    // on `v !== 'chat'`, so a desk -> chat version of this test asserts nothing
    // about it at all.
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithTrace();
    history.replaceState(null, '', '/?view=chat&conversation=c1');
    const { getByTestId, findByTestId } = render(App);
    await findByTestId('chat-prompt');
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('c1'),
    );

    await fireEvent.click(getByTestId('nav-desk'));

    const search = new URLSearchParams(window.location.search);
    // Desk is DEFAULT_VIEW post-flip, so navigate() drops the param entirely
    // rather than writing view=desk — the assertion that matters is that the
    // chat-intent params went in that SAME write.
    expect(search.get('view')).toBeNull();
    // Iterate the shared list rather than restating it: a fifth chat-intent
    // param added to CHAT_INTENT_PARAMS is then covered here automatically.
    for (const p of CHAT_INTENT_PARAMS) expect(search.has(p)).toBe(false);
    expect(getByTestId('approval-desk')).toBeTruthy();
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

  // Three times superseded, and worth reading as a sequence. Task 3.5 took the
  // rails off the desk. ds-jns PR 2 re-pointed the decisions rail's button at a
  // record on the desk rather than a replay in the chat column. Task 3.3 then
  // deleted that rail outright — the desk's own ledger is the decision browser,
  // and it is ON the desk, so the round trip collapsed to a single click.
  // What survives all three is the claim that matters: the ledger row opens the
  // record, and the URL says so afterwards.
  it('a ledger row opens its decision record, and the URL names it', async () => {
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
            // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
            // omitted — absent means "unverified", which pauses adoption.
            iac_snapshot_stale: false,
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
    // Nothing left over from either retired rail on this page.
    expect(queryByTestId('rails')).toBeNull();
    expect(queryByTestId('decision-record')).toBeNull();

    await fireEvent.click(await findByTestId('ledger-strip-row'));

    await findByTestId('decision-record');
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBe(TID);
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

  // Codex finding baked into the plan: no query param may strand a visitor on a
  // page that cannot serve the link they followed. That guarantee outlived its
  // original mechanism twice over. It used to be "every intent param forces
  // chat, where the rails are"; since ds-jns two of them resolve to the DESK on
  // purpose, and Task 3.3 deleted page-level replay, so a `?reasoning=` that
  // names no open thread's message now ENDS on the desk however it started.
  // What still has to hold is unchanged: whichever page it lands on, that page
  // renders the thing the param names.
  it('a ?view=chat&reasoning= link ends on the desk, showing the record it names', async () => {
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
            // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
            // omitted — absent means "unverified", which pauses adoption.
            iac_snapshot_stale: false,
            degraded: false,
            degraded_reason: null,
            totals: { resources: 1, managed: 0, drift: 1 },
            groups: [],
            edges: [],
          });
        return okJson({});
      }),
    );
    history.replaceState(null, '', `/?view=chat&reasoning=${TID}`);
    const { getByTestId, findByTestId } = render(App);
    // hasChatIntent still puts this URL on chat first — that rule is about the
    // link's shape and did not change. Chat then has nothing to render a bare
    // trace with, so it hands it on rather than dropping it.
    await findByTestId('decision-record');
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  // …and the intent that has NO independent redirect.
  //
  // The test above cannot pin hasChatIntent at all any more: its URL ends on the
  // desk either way, since that is where a bare `?reasoning=` belongs now.
  // `?conversation=` is separately protected by openConversation()'s own
  // navigate('chat').
  // `?ask_pr=` is NOT — it has no self-redirect anywhere in App, so
  // hasChatIntent is the only thing standing between a visitor following an
  // Adopt-flow link and a railless desk with no composer to act on. It is
  // exactly where the railless-desk guarantee is load-bearing, so it gets its
  // own App-level pin rather than resting on deeplink.ts's unit tests alone.
  it('a ?ask_pr= link resolves to chat with the rails rendered, even against an explicit ?view=desk', () => {
    history.replaceState(null, '', '/?view=desk&ask_pr=168');
    const { getByTestId, queryByTestId } = render(App);
    expect(queryByTestId('approval-desk')).toBeNull();
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(getByTestId('rails')).toBeTruthy();
  });

  // preview_pr left that set: the estate it previews lives on the desk, so the
  // desk is where it belongs (ds-jns Task 2.4). The railless landing is fine
  // BECAUSE the preview renders there — which is what this asserts, rather
  // than the destination alone.
  it('a ?preview_pr= link resolves to the desk and renders the preview there', async () => {
    history.replaceState(null, '', '/?preview_pr=168');
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
    await waitFor(() => expect(getByTestId('preview-banner')).toBeTruthy());
    // The estate section it previews is still below it.
    expect(queryByTestId('estate-view')).toBeTruthy();
  });

  it('renders no preview panel on a desk with no ?preview_pr=', () => {
    history.replaceState(null, '', '/?view=desk');
    const { queryByTestId } = render(App);
    expect(queryByTestId('preview-banner')).toBeNull();
    expect(queryByTestId('infra-panel')).toBeNull();
  });
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
            // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
            // omitted — absent means "unverified", which pauses adoption.
            iac_snapshot_stale: false,
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

  // ds-1vn r3. `graphStale` is a store fact threaded App -> EstateView and
  // App -> TourCard BY HAND, and a hand-threaded prop is what a refactor drops.
  // Verified by injection: removing either `graphStale={...}` from App reddened
  // NOTHING across all 1866 tests — the component-level tests pass the prop
  // themselves, so they cannot see App failing to. This is the wiring test.
  //
  // Driven through TWO cycles on purpose. One failing cycle leaves `graph`
  // null and the estate shows its degraded line, which proves nothing; the
  // dangerous state is a RETAINED graph that still says `iac_snapshot_stale:
  // false` while the refresh that would have rechecked it failed.
  it('a failed graph REFRESH retires the adopt affordance on the retained estate', async () => {
    let failGraph = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        if (url.includes('/infra/graph')) {
          if (failGraph) return new Response('boom', { status: 500 });
          return okJson({
            generated_at: '2026-07-28T06:00:00Z',
            project: 'demo-proj',
            caveat: '',
            iac_snapshot_stale: false,
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
        }
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, queryByTestId, getByTestId } = render(App);
    // Cycle 1 succeeds: the estate is fresh and Adopt is offered.
    expect(await findByTestId('estate-adopt-btn')).toBeTruthy();

    // Cycle 2 fails. The graph is RETAINED — the rows stay, which is right —
    // but its freshness claim must not be retained with it.
    failGraph = true;
    await fireEvent.focus(window);
    await waitFor(() => expect(getByTestId('estate-snapshot-unverified')).toBeTruthy());
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    // 'unverified', not 'stale': a failed refresh is no evidence of a mismatch.
    expect(getByTestId('estate-adopt-unverified')).toBeTruthy();
    // The row itself survives: a warning must not hide its own subject.
    expect(getByTestId('estate-row').textContent).toContain('shipping-topic');

    // ...and the TOUR agrees (Codex r4). I first judged this seam not worth
    // covering because App feeds both children `graphStale` from the same
    // source on adjacent lines. That reasoning is wrong twice over: both props
    // are OPTIONAL, so svelte-check accepts the omission silently, and this is
    // the exact seam whose absence let a high-severity defect through in round
    // 3 — the estate suppressing adoption while the tour beside it still
    // recommended one. TourCard's own test supplies the prop itself and
    // therefore cannot prove App forwards it.
    await fireEvent.click(getByTestId('tour-banner-start'));
    await fireEvent.click(getByTestId('tour-next')); // welcome → estate
    await fireEvent.click(getByTestId('tour-next')); // estate → controls
    await fireEvent.click(getByTestId('tour-next')); // controls → adopt
    await waitFor(() => expect(getByTestId('tour-body')).toBeTruthy());
    expect(queryByTestId('tour-adopt-btn')).toBeNull();
  });

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
            // ds-1vn: a healthy deployment's snapshot matches. Explicit, not
            // omitted — absent means "unverified", which pauses adoption.
            iac_snapshot_stale: false,
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

// ---------------------------------------------------------------------------
// ds-jns — decisions open as RECORDS on the desk that lists them, instead of
// bouncing the operator into the chat view's replay mode.
//
// App owns the one open record (`deskRecordTraceId`); the ledger row and the
// pending hero only ask. These tests pin the two things that ownership buys:
// the `?reasoning=` param never disagrees with what is on screen, and a record
// whose decision is not in the snapshot is pinned above the desk instead of
// silently vanishing.
// ---------------------------------------------------------------------------
describe('App — desk decision records (ds-jns)', () => {
  const TID = 'c'.repeat(32);

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

  function stubDesk(decisions: unknown[], opts: { hangDecisions?: boolean } = {}) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({
            trace_id: TID,
            complete: true,
            events: [],
            // A WAITING iac_apply: the one shape whose approval label is
            // actionable, so the tests below can tell "no link" from "no
            // linkable decision".
            decision: {
              decision_id: 'dt-1',
              trace_id: TID,
              action: 'iac_apply',
              created_at: '2026-07-28T09:00:00Z',
              pr_number: 68,
              event_key: 'ek-1',
              apply_status: 'waiting_for_rebake',
              merge_state: 'merged',
              // Its PR link is the witness that the card renders links at all,
              // so "no approval link" reads as withheld rather than as blank.
              github: { url: 'https://github.com/acme/ops/pull/68' },
            },
          });
        if (url.includes('/decisions')) {
          // A cycle that never completes leaves `settled` false — the state in
          // which the desk must not yet call anything out of window.
          if (opts.hangDecisions) return new Promise<Response>(() => {});
          return okJson({ decisions });
        }
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
  }

  const rowDecision = (trace_id: string) => ({
    decision_id: 'dr-1',
    trace_id,
    action: 'rollback',
    created_at: '2026-07-28T09:00:00Z',
  });

  // ds-3em. The strip is the ESTATE-WIDE decision ledger — rollbacks, no-action
  // notes, adoptions applied across every resource. Mounted inside
  // ApprovalDesk's bordered card it rendered directly under the Approve/Reject
  // buttons, and the card border grouped it with the one pending proposal: an
  // operator reading "PR #168" in the hero read the rows beneath it as that
  // PR's history. Containment is what carried the misreading, so containment is
  // what breaks — a clarifying heading was considered and rejected.
  //
  // ORDER is asserted, not just containment. "Outside the desk" is equally true
  // of the below-the-estate variant (mockup tab B), which was rejected because
  // it splits the approve → 判子 stamp → new-ledger-row beat across two
  // scrolls. Sibling-between-them is the whole placement decision.
  it('mounts the record as its own card between the desk and the estate', async () => {
    stubDesk([rowDecision(TID)]);
    history.replaceState(null, '', '/');
    const { findByTestId, findAllByTestId } = render(App);

    // Premise first: the strip renders nothing on an empty list, so wait for
    // the row the fixture supplies before asserting where its card sits.
    await findAllByTestId('ledger-strip-row');
    const desk = await findByTestId('approval-desk');
    const ledger = await findByTestId('ledger-strip');
    const estate = await findByTestId('estate-view');

    expect(desk.contains(ledger)).toBe(false);
    expect(desk.compareDocumentPosition(ledger) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(ledger.compareDocumentPosition(estate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  // The cap is a decision made AT THE MOUNT (LedgerStrip's own default is 4),
  // so it is pinned here rather than in the component's suite. Three rows plus
  // the one-way show-all is what keeps the card short enough that the estate
  // below it is still on the same page.
  it('caps the desk ledger at three rows, with the show-all still offered', async () => {
    stubDesk(
      Array.from({ length: 6 }, (_, i) => ({
        decision_id: `dc-${i}`,
        action: 'no_op',
        created_at: `2026-07-28T0${i}:00:00Z`,
      })),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, findAllByTestId } = render(App);

    await findByTestId('ledger-show-more');
    expect(await findAllByTestId('ledger-strip-row')).toHaveLength(3);
  });

  it('pins the record above the desk when no decision in the snapshot carries it', async () => {
    stubDesk([]);
    history.replaceState(null, '', `/?reasoning=${TID}`);
    const { getByTestId } = render(App);
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
    await waitFor(() =>
      expect(getByTestId('decision-record-outofwindow').textContent).toContain(
        'not in the recent decisions',
      ),
    );
    // Pinned ABOVE the desk, not inside the ledger it is not part of.
    const record = getByTestId('decision-record');
    const desk = getByTestId('approval-desk');
    expect(desk.contains(record)).toBe(false);
    expect(record.compareDocumentPosition(desk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('withholds the out-of-window verdict while the list is still loading', async () => {
    // "Older than the records listed below" is a claim about a list. Until the
    // first cycle settles there is no list — only an empty placeholder — and
    // the claim would be a guess (the ds-eh6 rule, one surface over).
    stubDesk([], { hangDecisions: true });
    history.replaceState(null, '', `/?reasoning=${TID}`);
    const { getByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
    expect(queryByTestId('decision-record-outofwindow')).toBeNull();
  });

  it('never offers an ACTION on a pinned record, whatever the decisions list says', async () => {
    // End to end through the real store, for the claim the component pins in
    // isolation: `apply`/`continue` rest on "no newer terminal row was found",
    // and a pinned record is by definition outside the `limit=50` window that
    // finding was made in — so the absence proves nothing (Codex rounds 3-4).
    //
    // `hangDecisions` is the harshest version: the store's pre-first-fetch
    // value is NO_DECISIONS_YET, a NON-NULL empty array meaning "we have not
    // looked", which App must not pass off as a list.
    stubDesk([], { hangDecisions: true });
    history.replaceState(null, '', `/?reasoning=${TID}`);
    const { findByTestId } = render(App);

    // PREMISE FIRST. Asserting anything about the label before the record has
    // resolved its decision is unfailable — the fourth time in this PR that a
    // negative assertion had to be taught to establish what it is negating.
    // The action row renders only once `doc` is loaded, and this trace's
    // decision is a WAITING iac_apply: exactly the shape whose label WOULD be
    // actionable if this card were willing to make that claim.
    await findByTestId('decision-record-action');

    // The plan and the history stay one click away…
    const link = await findByTestId('iac-approve-link');
    expect(link.getAttribute('href')).toContain('/iac-approvals/68');
    // …and the copy claims nothing about what is left to do.
    expect(link.textContent).not.toMatch(/apply this change|continue/i);
  });

  it('expands the ledger row instead of pinning, when the snapshot does carry it', async () => {
    stubDesk([rowDecision(TID)]);
    history.replaceState(null, '', `/?reasoning=${TID}`);
    const { getByTestId, getAllByTestId } = render(App);
    // The record shows immediately, pinned, because the list has not arrived
    // yet — a deep link should not wait on a fetch to show what it names. Wait
    // for the LIST, then assert where the record ended up; asserting on the
    // record alone would pass against that first pinned frame.
    await waitFor(() => expect(getAllByTestId('ledger-strip-row')).toHaveLength(1));
    await waitFor(() => expect(getAllByTestId('decision-record')).toHaveLength(1));
    const record = getAllByTestId('decision-record')[0];
    expect(getByTestId('ledger-strip').contains(record)).toBe(true);
    expect(getAllByTestId('ledger-strip-row')[0].getAttribute('aria-expanded')).toBe('true');
  });

  it('keeps ?reasoning= in step with the row the operator opens and closes', async () => {
    stubDesk([rowDecision(TID)]);
    history.replaceState(null, '', '/?view=desk');
    const { getAllByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getAllByTestId('ledger-strip-row')).toHaveLength(1));
    const row = getAllByTestId('ledger-strip-row')[0];

    await fireEvent.click(row);
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBe(TID);
    await waitFor(() => expect(queryByTestId('decision-record')).toBeTruthy());

    await fireEvent.click(getAllByTestId('ledger-strip-row')[0]);
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
    await waitFor(() => expect(queryByTestId('decision-record')).toBeNull());
  });

  it('the pending hero opens the record here, without leaving the desk', async () => {
    stubDesk([
      {
        decision_id: 'dr-2',
        trace_id: TID,
        action: 'rollback',
        created_at: '2026-07-28T09:00:00Z',
        approval: { approval_url: '/approvals/dr-2', status: 'pending' },
      },
    ]);
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getByTestId('approval-desk-why')).toBeTruthy());
    await fireEvent.click(getByTestId('approval-desk-why'));
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
    expect(queryByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
  });

  it('a view gesture drops the open record AND its param', async () => {
    stubDesk([rowDecision(TID)]);
    history.replaceState(null, '', '/?view=desk');
    const { getAllByTestId, getByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getAllByTestId('ledger-strip-row')).toHaveLength(1));
    await fireEvent.click(getAllByTestId('ledger-strip-row')[0]);
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBe(TID);

    await fireEvent.click(getByTestId('nav-chat'));
    // Both, or the state and the URL disagree — and since a bare ?reasoning=
    // resolves to the desk, a survivor would bounce a reload back off chat.
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
    await fireEvent.click(getByTestId('nav-desk'));
    expect(queryByTestId('decision-record')).toBeNull();
  });

  it('a view gesture drops the estate preview AND its param', async () => {
    stubDesk([]);
    history.replaceState(null, '', '/?preview_pr=168');
    const { getByTestId, queryByTestId } = render(App);
    await waitFor(() => expect(getByTestId('preview-banner')).toBeTruthy());
    await fireEvent.click(getByTestId('nav-chat'));
    expect(new URLSearchParams(window.location.search).get('preview_pr')).toBeNull();
    expect(queryByTestId('preview-banner')).toBeNull();
    await fireEvent.click(getByTestId('nav-desk'));
    expect(queryByTestId('preview-banner')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ds-jns — the popstate transition table for the desk's own deep state.
//
// The policy is unchanged and deliberate: a restore is VIEW-ONLY. Back never
// re-resolves a deep resource — that would fire fetches the operator did not
// ask for and race whatever they do next — so the entry is canonicalized
// instead, until the URL stops claiming content that is not on screen. What
// ds-jns adds is that the desk now HAS deep state to tear down.
// ---------------------------------------------------------------------------
describe('App — popstate and the desk record (ds-jns)', () => {
  const TID = 'c'.repeat(32);
  const GRAPH_BODY = {
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

  function popTo(search: string): void {
    history.replaceState(null, '', search);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }

  function stub(decisions: unknown[] = []) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/trace/'))
          return okJson({ trace_id: TID, complete: true, events: [], decision: null });
        if (url.includes('/decisions')) return okJson({ decisions });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH_BODY);
        return okJson({});
      }),
    );
  }

  const row = {
    decision_id: 'dr-1',
    trace_id: TID,
    action: 'rollback',
    created_at: '2026-07-28T09:00:00Z',
  };

  it('collapses an open record on a pop and stops the url claiming it', async () => {
    stub([row]);
    history.replaceState(null, '', '/?view=desk');
    const { findAllByTestId, queryByTestId } = render(App);
    await fireEvent.click((await findAllByTestId('ledger-strip-row'))[0]);
    await waitFor(() => expect(queryByTestId('decision-record')).toBeTruthy());

    // Back to an entry that still names the record: view-only restore, so the
    // record closes and the param goes with it.
    popTo(`/?view=desk&reasoning=${TID}`);
    await waitFor(() => expect(queryByTestId('decision-record')).toBeNull());
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
  });

  it('clears an open preview on a pop and stops the url claiming it', async () => {
    stub();
    history.replaceState(null, '', '/?preview_pr=168');
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('preview-banner');

    popTo('/?preview_pr=168');
    await waitFor(() => expect(queryByTestId('preview-banner')).toBeNull());
    expect(new URLSearchParams(window.location.search).get('preview_pr')).toBeNull();
  });

  it('does not re-open a record named only by the popped entry', async () => {
    // The other direction of the same rule: arriving at an entry that names a
    // record this session never opened must not resolve it.
    stub([row]);
    history.replaceState(null, '', '/?view=chat');
    const { queryByTestId } = render(App);

    popTo(`/?reasoning=${TID}`);
    await waitFor(() => expect(queryByTestId('approval-desk')).toBeTruthy());
    expect(queryByTestId('decision-record')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
  });
});

// ds-jk9. App hand-threads the overview store's two lane-freshness flags into
// ApprovalDesk. This composition needs its OWN tests for the reason #289 round
// 3 found the hard way: dropping a hand-threaded prop at the parent reddened
// NOTHING across the whole suite. Component tests supply the prop themselves
// and are structurally blind to App failing to, and an optional prop passes
// svelte-check when omitted, so nothing else can catch the omission.
//
// TWO scenarios, not one, and each is good-then-failed:
//
//   - One decisions-failure test could not mutation-prove both props: deleting
//     approvalsStale at the mount would not redden it. So each lane gets a
//     scenario in which ONLY that lane fails, and each prop therefore has a
//     test that fails only for it.
//   - A first-cycle failure leaves the initial empty sentinel with no retained
//     card to show, so no stale notice could ever appear and the test would
//     pass for the wrong reason. Each scenario lands a good cycle first, then
//     fails the refresh — which is also the real-world shape of the bug.
describe('App — lane freshness reaches the desk (ds-jk9)', () => {
  const GRAPH_OK = {
    generated_at: '2026-08-03T06:00:00Z',
    project: 'demo-proj',
    caveat: '',
    iac_snapshot_stale: false,
    degraded: false,
    degraded_reason: null,
    totals: { resources: 2, managed: 2, drift: 0 },
    groups: [],
    edges: [],
  };

  /** Second and later cycles are driven by the store's focus listener. */
  async function refetch(): Promise<void> {
    window.dispatchEvent(new Event('focus'));
  }

  it('threads decisionsStale: a retained decisions-derived card stops being actionable', async () => {
    const waiting = {
      decision_id: 'iac-1',
      action: 'iac_apply',
      created_at: '2026-08-03T05:00:00Z',
      pr_number: 42,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      event_key: 'iac-apply-42-gen1',
    };
    let decisionsOk = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/decisions')) {
          return decisionsOk
            ? okJson({ decisions: [waiting] })
            : new Response('boom', { status: 500 });
        }
        if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH_OK);
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, queryByTestId } = render(App);

    // Cycle 1: the card is live and carries a real Apply.
    expect(await findByTestId('approval-desk-apply')).toBeTruthy();
    expect(queryByTestId('approval-desk-stale-notice')).toBeNull();

    // Cycle 2: /decisions fails. The store RETAINS the row, so the card stays…
    decisionsOk = false;
    await refetch();
    await waitFor(() => expect(queryByTestId('approval-desk-stale-notice')).toBeTruthy());
    // …but stops offering the action it can no longer vouch for.
    expect(queryByTestId('approval-desk-pending')).toBeTruthy();
    expect(queryByTestId('approval-desk-apply')).toBeNull();
  });

  it('threads approvalsStale: a retained listing card stops being actionable', async () => {
    // Only the LISTING lane fails here, so this test is red only if
    // `approvalsStale` specifically is missing at the mount.
    let approvalsOk = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/pending-approvals')) {
          return approvalsOk
            ? okJson({
                approvals: [
                  {
                    pr_number: 7,
                    title: 'Adopt orders-sub into IaC',
                    url: 'https://github.com/x/y/pull/7',
                    asset_type: 'pubsub.googleapis.com/Subscription',
                    resource_name: 'orders-sub',
                  },
                ],
              })
            : new Response('boom', { status: 500 });
        }
        if (url.includes('/infra/graph')) return okJson(GRAPH_OK);
        return okJson({});
      }),
    );
    history.replaceState(null, '', '/');
    const { findByTestId, queryByTestId } = render(App);

    expect(await findByTestId('approval-desk-review')).toBeTruthy();
    expect(queryByTestId('approval-desk-stale-notice')).toBeNull();

    approvalsOk = false;
    await refetch();
    await waitFor(() => expect(queryByTestId('approval-desk-stale-notice')).toBeTruthy());
    // Identity survives: it still names WHICH change it last saw.
    expect(queryByTestId('approval-desk-pending')?.textContent).toContain(
      'Adopt orders-sub into IaC',
    );
    expect(queryByTestId('approval-desk-review')).toBeNull();
  });
});
