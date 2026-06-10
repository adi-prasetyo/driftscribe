import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import PauseControl from '../../src/components/PauseControl.svelte';

// Component tests for PauseControl — the operator kill switch.
//
// Fake `call` pattern mirrors CapabilityCard.test.ts: each test constructs a
// call stub that records (path, init) tuples. Most tests also expose a Promise
// resolver so the test can control when the POST settles (busy-state cases).
//
// jsdom caveats:
//  - No native <details> toggling (not relevant — PauseControl has no details).
//  - Microtask queue drains between await steps; `waitFor` polls until stable.

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Types and helpers
// ---------------------------------------------------------------------------

type CallRecord = { path: string; init?: RequestInit };

interface PauseDoc {
  paused: boolean;
  reason?: string | null;
  actor?: string | null;
  updated_at?: string | null;
  read_error?: boolean;
}

function makeResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Build a call stub that returns `getResponse` on GET and `postResponse` on POST. */
function makeCall(
  records: CallRecord[],
  getResponse: Response,
  postResponse?: Response,
): (path: string, init?: RequestInit) => Promise<Response> {
  return async (path: string, init?: RequestInit) => {
    records.push({ path, init });
    if (init?.method === 'POST') {
      return postResponse ?? makeResponse({ paused: false, reason: null, actor: null, updated_at: null, read_error: false });
    }
    return getResponse;
  };
}

/** Running state fixture */
const RUNNING_DOC: PauseDoc = {
  paused: false,
  reason: null,
  actor: null,
  updated_at: null,
  read_error: false,
};

/** Paused state fixture */
const PAUSED_DOC: PauseDoc = {
  paused: true,
  reason: 'investigating alert',
  actor: 'ops@example.com',
  updated_at: '2026-06-10T14:02:00Z',
  read_error: false,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PauseControl', () => {
  // 1. Running state
  it('running state renders active copy + Pause button (and NO banner)', async () => {
    const records: CallRecord[] = [];
    const { getByTestId, queryByText } = render(PauseControl, {
      props: { call: makeCall(records, makeResponse(RUNNING_DOC)) },
    });

    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toBe(
        'DriftScribe is active — it can act only within the guardrails below.',
      );
    });

    // Pause button present
    const toggle = getByTestId('pause-toggle');
    expect(toggle.textContent?.trim()).toBe('Pause');

    // No paused banner text
    expect(queryByText(/DriftScribe is paused/)).toBeNull();

    // GET was called
    expect(records.filter((r) => !r.init?.method).map((r) => r.path)).toContain('/pause');
  });

  // 2. Paused state
  it('paused state renders banner with actor, reason, and <time> datetime attr; Resume button present', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(PauseControl, {
      props: { call: makeCall(records, makeResponse(PAUSED_DOC)) },
    });

    // Exact-string pin (mutation detection): the pause-state element renders
    // the FULL banner copy as a single text node — not substring-soup.
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toBe(
        '⏸ DriftScribe is paused — no new agent activity will start.',
      );
    });

    // Actor and reason visible somewhere in the control
    const control = getByTestId('pause-control');
    expect(control.textContent).toContain('ops@example.com');
    expect(control.textContent).toContain('investigating alert');

    // <time> element with datetime attr
    const timeEl = getByTestId('pause-control').querySelector('time');
    expect(timeEl).not.toBeNull();
    expect(timeEl!.getAttribute('datetime')).toBe('2026-06-10T14:02:00Z');

    // Resume button
    const toggle = getByTestId('pause-toggle');
    expect(toggle.textContent?.trim()).toBe('Resume');
  });

  // 3. Fetch failure → unknown/fail-closed + Retry refetches
  it('fetch failure → fail-closed copy + pause-retry; clicking Retry refetches and renders recovered state', async () => {
    let callCount = 0;
    const records: CallRecord[] = [];
    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      callCount++;
      if (callCount === 1) throw new Error('network error');
      return makeResponse(RUNNING_DOC);
    };

    const { getByTestId } = render(PauseControl, { props: { call } });

    // Unknown state renders
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('Pause state unknown');
      expect(state.textContent).toContain('fails closed');
    });

    // Retry button present
    const retryBtn = getByTestId('pause-retry');
    expect(retryBtn).toBeTruthy();

    // Clicking Retry issues a second GET
    await fireEvent.click(retryBtn);

    // Recovered state
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toBe(
        'DriftScribe is active — it can act only within the guardrails below.',
      );
    });

    // Assert second GET was made
    const gets = records.filter((r) => !r.init?.method);
    expect(gets.length).toBeGreaterThanOrEqual(2);
  });

  // 4. Malformed 200 → unknown state
  it('malformed 200 (paused is not boolean) → unknown/fail-closed state (structural guard)', async () => {
    const records: CallRecord[] = [];
    const malformed = { paused: 'yes', reason: null };
    const { getByTestId } = render(PauseControl, {
      props: { call: makeCall(records, makeResponse(malformed)) },
    });

    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('Pause state unknown');
    });
  });

  // 5. Pause click → confirm row; confirm POSTs correct body; UI flips to paused from response
  it('Pause → confirm row shows reason input; confirm POSTs {paused:true,reason:…} and flips to paused from response; reason cleared', async () => {
    const records: CallRecord[] = [];
    const pausedResponse: PauseDoc = {
      paused: true,
      reason: 'scheduled maintenance',
      actor: 'ops@example.com',
      updated_at: '2026-06-10T15:00:00Z',
      read_error: false,
    };
    const resumedResponse: PauseDoc = {
      paused: false,
      reason: null,
      actor: null,
      updated_at: '2026-06-10T15:05:00Z',
      read_error: false,
    };
    // Sequenced POST fake: first POST (pause) → paused doc, second POST
    // (resume) → running doc — lets the test flip back and reopen the
    // confirm row to assert the reason input was cleared.
    let postCount = 0;
    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      if (init?.method === 'POST') {
        postCount++;
        return makeResponse(postCount === 1 ? pausedResponse : resumedResponse);
      }
      return makeResponse(RUNNING_DOC);
    };

    const { getByTestId, queryByTestId } = render(PauseControl, { props: { call } });

    // Wait for running state
    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
    });

    // No confirm row yet
    expect(queryByTestId('pause-confirm')).toBeNull();

    // Click Pause
    await fireEvent.click(getByTestId('pause-toggle'));

    // Confirm row appears
    await waitFor(() => {
      expect(getByTestId('pause-confirm')).toBeTruthy();
      expect(getByTestId('pause-cancel')).toBeTruthy();
      expect(getByTestId('pause-reason')).toBeTruthy();
    });

    // Enter a reason
    const reasonInput = getByTestId('pause-reason') as HTMLInputElement;
    await fireEvent.input(reasonInput, { target: { value: 'scheduled maintenance' } });

    // Confirm
    await fireEvent.click(getByTestId('pause-confirm'));

    // UI flips to paused
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('DriftScribe is paused');
    });

    // Assert POST body
    const postRecord = records.find((r) => r.init?.method === 'POST');
    expect(postRecord).toBeTruthy();
    const body = JSON.parse(postRecord!.init!.body as string);
    expect(body).toEqual({ paused: true, reason: 'scheduled maintenance' });

    // Confirm row collapsed
    expect(queryByTestId('pause-confirm')).toBeNull();

    // Reason cleared after the successful POST — exercised for real: flip back
    // to running (Resume → confirm), then click Pause to reopen the confirm
    // row and assert the input's value is empty, not the stale reason.
    await fireEvent.click(getByTestId('pause-toggle')); // Resume
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-confirm'));
    await waitFor(() => {
      expect(getByTestId('pause-state').textContent).toBe(
        'DriftScribe is active — it can act only within the guardrails below.',
      );
    });
    await fireEvent.click(getByTestId('pause-toggle')); // Pause again
    await waitFor(() => expect(getByTestId('pause-reason')).toBeTruthy());
    expect((getByTestId('pause-reason') as HTMLInputElement).value).toBe('');
  });

  // 6. Cancel collapses confirm row with NO POST
  it('Cancel collapses the confirm row with no POST recorded', async () => {
    const records: CallRecord[] = [];
    const { getByTestId, queryByTestId } = render(PauseControl, {
      props: { call: makeCall(records, makeResponse(RUNNING_DOC)) },
    });

    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
    });

    // Click Pause
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-cancel')).toBeTruthy());

    // Click Cancel
    await fireEvent.click(getByTestId('pause-cancel'));

    // Confirm row gone
    await waitFor(() => {
      expect(queryByTestId('pause-confirm')).toBeNull();
    });

    // No POST
    const posts = records.filter((r) => r.init?.method === 'POST');
    expect(posts).toHaveLength(0);
  });

  // 7. POST failure → pause-error shown; still running state; Pause button usable again
  it('POST failure (500) → pause-error shown; still running state; Pause button usable again', async () => {
    const records: CallRecord[] = [];
    const call = makeCall(records, makeResponse(RUNNING_DOC), makeResponse('Server error', 500));

    const { getByTestId, queryByTestId } = render(PauseControl, { props: { call } });

    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
    });

    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-confirm'));

    // pause-error appears
    await waitFor(() => {
      expect(getByTestId('pause-error')).toBeTruthy();
    });

    // Still in running state (old state preserved)
    const state = getByTestId('pause-state');
    expect(state.textContent).toContain('DriftScribe is active');

    // Confirm row is gone; Pause toggle still present and usable
    expect(queryByTestId('pause-confirm')).toBeNull();
    expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
  });

  // 8. Resume confirm POSTs {paused:false} and flips to running
  it('Resume → confirm POSTs {paused:false} and UI flips to running from response', async () => {
    const records: CallRecord[] = [];
    const postResponse: PauseDoc = {
      paused: false,
      reason: null,
      actor: null,
      updated_at: '2026-06-10T15:30:00Z',
      read_error: false,
    };
    const call = makeCall(records, makeResponse(PAUSED_DOC), makeResponse(postResponse));

    const { getByTestId, queryByTestId } = render(PauseControl, { props: { call } });

    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume');
    });

    // Click Resume
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());

    // Confirm
    await fireEvent.click(getByTestId('pause-confirm'));

    // UI flips to running
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('DriftScribe is active');
    });

    // Assert POST body
    const postRecord = records.find((r) => r.init?.method === 'POST');
    expect(postRecord).toBeTruthy();
    const body = JSON.parse(postRecord!.init!.body as string);
    expect(body).toEqual({ paused: false });

    // Confirm row gone
    expect(queryByTestId('pause-confirm')).toBeNull();
  });

  // 9. read_error:true → fail-closed copy in paused banner
  it('read_error:true paused response renders fail-closed copy instead of actor/time', async () => {
    const records: CallRecord[] = [];
    const readErrorDoc: PauseDoc = {
      paused: true,
      reason: null,
      actor: null,
      updated_at: null,
      read_error: true,
    };
    const { getByTestId } = render(PauseControl, {
      props: { call: makeCall(records, makeResponse(readErrorDoc)) },
    });

    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('DriftScribe is paused');
    });

    const control = getByTestId('pause-control');
    expect(control.textContent).toContain('pause state could not be read — failing closed');
  });

  // 10. Busy state: confirm button disabled while POST is pending
  it('confirm button disabled while the POST is in-flight', async () => {
    const records: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const postPromise = new Promise<Response>((res) => {
      resolvePost = res;
    });

    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      if (init?.method === 'POST') return postPromise;
      return makeResponse(RUNNING_DOC);
    };

    const { getByTestId } = render(PauseControl, { props: { call } });

    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
    });

    // Open confirm row
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());

    // Click confirm — POST is now in-flight
    await fireEvent.click(getByTestId('pause-confirm'));

    // While pending: confirm button disabled
    await waitFor(() => {
      expect((getByTestId('pause-confirm') as HTMLButtonElement).disabled).toBe(true);
    });

    // Resolve the POST
    resolvePost(makeResponse({ paused: true, reason: null, actor: null, updated_at: null, read_error: false }));

    // After resolution the state flips
    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('DriftScribe is paused');
    });
  });

  // 11. Single-flight: rapid double-confirm produces exactly ONE POST
  it('double-firing confirm while a POST is pending records exactly one POST (single-flight guard)', async () => {
    const records: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const postPromise = new Promise<Response>((res) => {
      resolvePost = res;
    });

    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      if (init?.method === 'POST') return postPromise;
      return makeResponse(RUNNING_DOC);
    };

    const { getByTestId } = render(PauseControl, { props: { call } });

    await waitFor(() => {
      expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Pause');
    });
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());

    // Two SYNCHRONOUS activations — both handlers run before Svelte flushes
    // the disabled attribute, so this exercises onConfirm's `if (saving)
    // return;` guard itself, not the disabled attribute.
    const confirmBtn = getByTestId('pause-confirm');
    void fireEvent.click(confirmBtn);
    void fireEvent.click(confirmBtn);

    // Exactly ONE POST in flight
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(1);

    // Resolve it; the flip happens and the count stays 1
    resolvePost(makeResponse(PAUSED_DOC));
    await waitFor(() => {
      expect(getByTestId('pause-state').textContent).toContain('DriftScribe is paused');
    });
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
  });

  // 12. Stale-response guard (sequence counter): a STALE GET resolving after a
  // newer one must NOT clobber the fresher state. Note: through the rendered
  // UI two requests cannot normally overlap (loading/saving states remove or
  // disable every trigger), so this test forces the overlap with two
  // synchronous Retry activations before Svelte flushes the re-render — the
  // closest constructible pin on the seq guard.
  it('stale GET resolving after a newer GET does not clobber fresher state (seq guard)', async () => {
    const records: CallRecord[] = [];
    let getCount = 0;
    let resolveStale!: (r: Response) => void;
    let resolveNewer!: (r: Response) => void;
    const staleGet = new Promise<Response>((res) => {
      resolveStale = res;
    });
    const newerGet = new Promise<Response>((res) => {
      resolveNewer = res;
    });

    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      getCount++;
      if (getCount === 1) throw new Error('network error'); // mount → unknown
      if (getCount === 2) return staleGet; // first Retry click (older run)
      return newerGet; // second Retry click (newer run)
    };

    const { getByTestId } = render(PauseControl, { props: { call } });

    // Mount GET fails → unknown state with Retry
    await waitFor(() => expect(getByTestId('pause-retry')).toBeTruthy());

    // Two synchronous Retry activations → two OVERLAPPING in-flight GETs.
    // Raw dispatchEvent (NOT testing-library fireEvent): fireEvent flushes
    // Svelte state synchronously after dispatch, which removes the Retry
    // button (loading state) before a second fireEvent could reach it.
    const retryBtn = getByTestId('pause-retry');
    retryBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    retryBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(getCount).toBe(3);

    // The NEWER GET resolves first: running
    resolveNewer(makeResponse(RUNNING_DOC));
    await waitFor(() => {
      expect(getByTestId('pause-state').textContent).toBe(
        'DriftScribe is active — it can act only within the guardrails below.',
      );
    });

    // The STALE GET then resolves paused — the seq guard must drop it
    resolveStale(makeResponse(PAUSED_DOC));
    // Drain microtasks so the stale callback fully runs (and bails)
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(getByTestId('pause-state').textContent).toBe(
      'DriftScribe is active — it can act only within the guardrails below.',
    );
  });
});
