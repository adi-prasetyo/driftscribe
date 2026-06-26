// frontend/tests/unit/App.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';

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

describe('App — open-trace scrolls the historical region into view', () => {
  // The historical replay renders at the BOTTOM of the chat column (below the
  // estate panel + composer), so without this the click looks dead. The button
  // is in the left rail; the banner it scrolls to is #historical-badge.
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

  it('clicking open-trace scrolls #historical-badge into view (block:start, reduced-motion → auto)', async () => {
    window.sessionStorage.setItem('driftscribe_token', 'tok');
    stubFetchWithIacDecision();
    const scrollSpy = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollSpy;

    const { findByTestId, getByTestId } = render(App);

    // Wait for the rail to load the decision, then open its trace.
    const btn = await findByTestId('open-trace-button');
    await fireEvent.click(btn);

    // The banner enters the DOM (proves historicalActive flipped + tick flushed).
    await waitFor(() => expect(getByTestId('historical-banner')).toBeTruthy());

    // The scroll fired with the historical-region options. setup.ts forces
    // matchMedia('reduce') → matches:true, so prefersReducedMotion() picks 'auto'.
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    // Exactly one scroll per open-trace — locks mock.contexts.at(-1) to the one
    // expected call so a future second scroll on this path can't make the
    // receiver assertion silently test the wrong call.
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
    // ...and it scrolled the historical banner (scrollIntoView's `this` is the
    // element it was invoked on).
    const banner = document.getElementById('historical-badge');
    expect(scrollSpy.mock.contexts.at(-1)).toBe(banner);
    // Focus follows the scroll so keyboard/SR users land in the replay region
    // instead of being stranded on the rail button they just clicked.
    expect(document.activeElement).toBe(banner);
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
    expect(panel.querySelector('pre')?.textContent).toContain('Repoints payment-demo');
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
    expect(panel.querySelector('pre')?.textContent).toContain('B-BODY');

    releaseA(); // A's stale response resolves now
    await Promise.resolve();
    await Promise.resolve();
    // The runSeq guard dropped A — B's body must remain, A's must never appear.
    expect(panel.querySelector('pre')?.textContent).toContain('B-BODY');
    expect(panel.querySelector('pre')?.textContent).not.toContain('A-BODY');
  });
});
