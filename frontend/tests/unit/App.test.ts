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
  history.replaceState(null, '', '/');
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

  it('defaults to the chat layout: composer present, desk/estate placeholders absent', () => {
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
    expect(new URL(window.location.href).searchParams.get('view')).toBe('desk');

    await fireEvent.click(chatBtn);
    expect(document.getElementById('chat-form')).toBeTruthy();
    expect(queryByTestId('approval-desk')).toBeNull();
    // Switching TO chat restores nothing — the view param is simply dropped
    // (chat is the default; a bare "/" already means chat).
    expect(new URL(window.location.href).searchParams.get('view')).toBeNull();
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
    expect(search.get('view')).toBe('desk');
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

  it('opening a trace from the decisions rail while on the desk view navigates to chat first', async () => {
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

    const btn = await findByTestId('open-trace-button');
    await fireEvent.click(btn);

    expect(queryByTestId('approval-desk')).toBeNull();
    expect(document.getElementById('chat-form')).toBeTruthy();
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());
  });
});
