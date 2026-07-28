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

  it('dismissing the banner marks done; the header button reopens the tour', async () => {
    const { getByTestId, queryByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-banner-dismiss'));
    expect(queryByTestId('tour-banner')).toBeNull();
    expect(window.localStorage.getItem('driftscribe_tour_done')).toBe('1');
    await fireEvent.click(getByTestId('tour-open'));
    expect(getByTestId('tour-card')).toBeTruthy();
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
  // worse still by routing steps 2 and 4 to the ESTATE view, which likewise
  // does not mount InfraDiagram — so no tour path reached a populated graph.
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
  it('a bare url defaults to the approval desk: composer absent, estate absent', () => {
    history.replaceState(null, '', '/');
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('approval-desk')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
    expect(queryByTestId('estate-view')).toBeNull();
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

  it('renders the estate placeholder for ?view=estate, with the composer absent', () => {
    history.replaceState(null, '', '/?view=estate');
    const { getByTestId } = render(App);
    expect(getByTestId('estate-view')).toBeTruthy();
    expect(document.getElementById('chat-form')).toBeNull();
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
    const { getByTestId } = render(App);
    await waitFor(() => expect(getByTestId('nav-desk')).toBeTruthy());

    for (const v of VIEWS) {
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

  it('the rails render on chat (default) and are absent on desk and estate', () => {
    const { getByTestId } = render(App);
    expect(getByTestId('rails')).toBeTruthy();
    cleanup();

    history.replaceState(null, '', '/?view=desk');
    const desk = render(App);
    expect(desk.queryByTestId('rails')).toBeNull();
    expect(desk.getByTestId('approval-desk')).toBeTruthy();
    cleanup();

    history.replaceState(null, '', '/?view=estate');
    const estate = render(App);
    expect(estate.queryByTestId('rails')).toBeNull();
    expect(estate.getByTestId('estate-view')).toBeTruthy();
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

describe('App — estate view (Task 4.1)', () => {
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
    history.replaceState(null, '', '/?view=estate');
    const { findByTestId } = render(App);
    const row = await findByTestId('estate-row');
    expect(row.textContent).toContain('shipping-topic');
  });

  it('an instrument-band numeral navigates from the desk to the estate view', async () => {
    history.replaceState(null, '', '/?view=desk');
    const { getByTestId, findByTestId } = render(App);
    await waitFor(() => expect(getByTestId('instrument-band-drift')).toBeTruthy());
    await fireEvent.click(getByTestId('instrument-band-drift'));
    expect(await findByTestId('estate-view')).toBeTruthy();
    expect(new URL(window.location.href).searchParams.get('view')).toBe('estate');
  });

  it('the nav-estate adopt-target fallback marker is present when the estate has no adoptable row, and absent when one exists', async () => {
    // Default beforeEach stub: totals.drift === 1 but `groups: []` — no
    // nameable adoptable row, so the fallback marker belongs on the nav button.
    const { getByTestId } = render(App);
    await waitFor(() =>
      expect(getByTestId('nav-estate').getAttribute('data-tour')).toBe('adopt-target'),
    );
    cleanup();

    stubFetchWithAdoptableGraph();
    const withAdopt = render(App);
    await withAdopt.findByTestId('nav-estate');
    await waitFor(() =>
      expect(withAdopt.getByTestId('nav-estate').getAttribute('data-tour')).toBeNull(),
    );
  });

  it('an adopt chip on the estate view lands the operator on a prefilled composer', async () => {
    stubFetchWithAdoptableGraph();
    history.replaceState(null, '', '/?view=estate');
    const { findByTestId } = render(App);
    const adoptBtn = await findByTestId('estate-adopt-btn');
    await fireEvent.click(adoptBtn);
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    const textarea = document.querySelector('[data-testid="chat-prompt"]') as HTMLTextAreaElement;
    expect(textarea.value).toContain('shipping-topic');
  });

  it('the tour adopt step navigates chat → estate and spotlights the first adoptable row (survives the navigate → mount delay)', async () => {
    stubFetchWithAdoptableGraph();
    const { getByTestId, findByTestId } = render(App); // default: chat view
    await fireEvent.click(getByTestId('tour-banner-start'));
    await fireEvent.click(getByTestId('tour-next')); // welcome → estate
    await fireEvent.click(getByTestId('tour-next')); // estate → controls
    await fireEvent.click(getByTestId('tour-next')); // controls → adopt (navigates to 'estate')
    const row = await findByTestId('estate-row');
    await waitFor(() => expect(row.classList.contains('tour-spotlight')).toBe(true));
  });
});
