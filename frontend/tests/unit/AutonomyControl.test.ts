import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import AutonomyControl from '../../src/components/AutonomyControl.svelte';
import type { AutonomyDoc } from '../../src/lib/autonomy';

// Component tests for AutonomyControl — the operator autonomy dial.
//
// Mirrors PauseControl.test.ts structure: same call-stub pattern, same
// Promise-resolver busy-state approach, same seq-guard strategy via two
// synchronous dispatchEvent activations.

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Types and helpers
// ---------------------------------------------------------------------------

type CallRecord = { path: string; init?: RequestInit };

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
      return (
        postResponse ??
        makeResponse({
          mode: 'propose_apply',
          reason: null,
          actor: null,
          updated_at: null,
          read_error: false,
        })
      );
    }
    return getResponse;
  };
}

const OBSERVE_DOC: AutonomyDoc = {
  mode: 'observe',
  reason: 'new adopter test',
  actor: 'ops@example.com',
  updated_at: '2026-06-11T10:00:00Z',
  read_error: false,
};

const PROPOSE_DOC: AutonomyDoc = {
  mode: 'propose',
  reason: null,
  actor: null,
  updated_at: null,
  read_error: false,
};

const PROPOSE_APPLY_DOC: AutonomyDoc = {
  mode: 'propose_apply',
  reason: null,
  actor: null,
  updated_at: null,
  read_error: false,
};

// ---------------------------------------------------------------------------
// Tests (mirrors PauseControl's 12-test structure)
// ---------------------------------------------------------------------------

describe('AutonomyControl', () => {
  // 1. Initial render — observe mode
  it('observe mode: all three segments rendered; observe segment has aria-pressed=true; blurb shown', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(OBSERVE_DOC)) },
    });

    await waitFor(() => {
      const observeBtn = getByTestId('autonomy-mode-observe');
      expect((observeBtn as HTMLButtonElement).getAttribute('aria-pressed')).toBe('true');
      const proposeBtn = getByTestId('autonomy-mode-propose');
      expect((proposeBtn as HTMLButtonElement).getAttribute('aria-pressed')).toBe('false');
      const paBtn = getByTestId('autonomy-mode-propose_apply');
      expect((paBtn as HTMLButtonElement).getAttribute('aria-pressed')).toBe('false');
    });

    // Blurb text for observe
    const control = getByTestId('autonomy-control');
    expect(control.textContent).toContain('Watch and report only');

    // actor + reason visible in meta
    expect(control.textContent).toContain('ops@example.com');
    expect(control.textContent).toContain('new adopter test');

    // GET was called
    expect(records.filter((r) => !r.init?.method).map((r) => r.path)).toContain('/autonomy');
  });

  // 2. Propose mode
  it('propose mode: propose segment aria-pressed=true; correct blurb', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
      expect(
        (getByTestId('autonomy-mode-observe') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('false');
    });

    const control = getByTestId('autonomy-control');
    expect(control.textContent).toContain('Open pull requests and issues');
  });

  // 3. Propose+Apply mode
  it('propose_apply mode: propose_apply segment aria-pressed=true; correct blurb', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute(
          'aria-pressed',
        ),
      ).toBe('true');
    });

    const control = getByTestId('autonomy-control');
    expect(control.textContent).toContain('Propose changes and apply them');
  });

  // 4. Fetch failure → unknown + retry
  it('fetch failure → unknown state; Retry refetches and renders recovered state', async () => {
    let callCount = 0;
    const records: CallRecord[] = [];
    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      callCount++;
      if (callCount === 1) throw new Error('network error');
      return makeResponse(PROPOSE_APPLY_DOC);
    };

    const { getByTestId } = render(AutonomyControl, { props: { call } });

    await waitFor(() => {
      expect(getByTestId('autonomy-retry')).toBeTruthy();
    });

    // Click retry
    await fireEvent.click(getByTestId('autonomy-retry'));

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute(
          'aria-pressed',
        ),
      ).toBe('true');
    });

    const gets = records.filter((r) => !r.init?.method);
    expect(gets.length).toBeGreaterThanOrEqual(2);
  });

  // 5. Malformed 200 → unknown state
  it('malformed 200 (mode not a valid AutonomyMode) → unknown state (structural guard)', async () => {
    const records: CallRecord[] = [];
    const malformed = { mode: 'yolo', reason: null, actor: null, updated_at: null, read_error: false };
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(malformed)) },
    });

    await waitFor(() => {
      expect(getByTestId('autonomy-retry')).toBeTruthy();
    });
  });

  // 6. Segment click arms confirm row with correct target; confirm POSTs {mode, reason}
  it('clicking a different segment arms confirm row; confirm POSTs {mode, reason} and applies response', async () => {
    const records: CallRecord[] = [];
    const postResponse: AutonomyDoc = {
      mode: 'observe',
      reason: 'going read-only',
      actor: 'ops@example.com',
      updated_at: '2026-06-11T12:00:00Z',
      read_error: false,
    };
    const { getByTestId, queryByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC), makeResponse(postResponse)) },
    });

    // Wait for loaded state
    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // No confirm row yet
    expect(queryByTestId('autonomy-confirm')).toBeNull();

    // Click observe segment
    await fireEvent.click(getByTestId('autonomy-mode-observe'));

    // Confirm row appears
    await waitFor(() => {
      expect(getByTestId('autonomy-confirm')).toBeTruthy();
      expect(getByTestId('autonomy-cancel')).toBeTruthy();
      expect(getByTestId('autonomy-reason')).toBeTruthy();
    });

    // Enter a reason
    const reasonInput = getByTestId('autonomy-reason') as HTMLInputElement;
    await fireEvent.input(reasonInput, { target: { value: 'going read-only' } });

    // Confirm
    await fireEvent.click(getByTestId('autonomy-confirm'));

    // UI flips to observe from the response
    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-observe') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // Assert POST body
    const postRecord = records.find((r) => r.init?.method === 'POST');
    expect(postRecord).toBeTruthy();
    const body = JSON.parse(postRecord!.init!.body as string);
    expect(body).toEqual({ mode: 'observe', reason: 'going read-only' });

    // Confirm row collapsed
    expect(queryByTestId('autonomy-confirm')).toBeNull();
  });

  // 7. Cancel collapses confirm row with NO POST
  it('Cancel collapses the confirm row with no POST recorded', async () => {
    const records: CallRecord[] = [];
    const { getByTestId, queryByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // Click observe to arm confirm
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-cancel')).toBeTruthy());

    // Click cancel
    await fireEvent.click(getByTestId('autonomy-cancel'));

    await waitFor(() => {
      expect(queryByTestId('autonomy-confirm')).toBeNull();
    });

    // No POST
    const posts = records.filter((r) => r.init?.method === 'POST');
    expect(posts).toHaveLength(0);
  });

  // 8. POST failure → autonomy-error shown; state unchanged
  it('POST failure (500) → autonomy-error shown; state unchanged; segments still usable', async () => {
    const records: CallRecord[] = [];
    const call = makeCall(
      records,
      makeResponse(PROPOSE_APPLY_DOC),
      makeResponse('Server error', 500),
    );

    const { getByTestId, queryByTestId } = render(AutonomyControl, { props: { call } });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));

    await waitFor(() => {
      expect(getByTestId('autonomy-error')).toBeTruthy();
    });

    // State unchanged — still propose_apply
    expect(
      (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
    ).toBe('true');

    // Confirm row gone; segments usable
    expect(queryByTestId('autonomy-confirm')).toBeNull();
  });

  // 9. read_error:true → warn line present
  it('read_error:true response renders autonomy-read-error warn line', async () => {
    const records: CallRecord[] = [];
    const readErrorDoc: AutonomyDoc = {
      mode: 'observe',
      reason: null,
      actor: null,
      updated_at: null,
      read_error: true,
    };
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(readErrorDoc)) },
    });

    await waitFor(() => {
      const warn = getByTestId('autonomy-read-error');
      expect(warn.textContent).toContain('autonomy state could not be read');
    });
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
      return makeResponse(PROPOSE_APPLY_DOC);
    };

    const { getByTestId } = render(AutonomyControl, { props: { call } });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());

    // Click confirm — POST is in-flight
    await fireEvent.click(getByTestId('autonomy-confirm'));

    await waitFor(() => {
      expect((getByTestId('autonomy-confirm') as HTMLButtonElement).disabled).toBe(true);
    });

    // Resolve
    resolvePost(makeResponse(OBSERVE_DOC));

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-observe') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });
  });

  // 11. Single-flight: double confirm produces exactly one POST
  it('double-firing confirm while a POST is pending records exactly one POST (single-flight guard)', async () => {
    const records: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const postPromise = new Promise<Response>((res) => {
      resolvePost = res;
    });

    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      if (init?.method === 'POST') return postPromise;
      return makeResponse(PROPOSE_APPLY_DOC);
    };

    const { getByTestId } = render(AutonomyControl, { props: { call } });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());

    const confirmBtn = getByTestId('autonomy-confirm');
    void fireEvent.click(confirmBtn);
    void fireEvent.click(confirmBtn);

    // Exactly one POST
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(1);

    resolvePost(makeResponse(OBSERVE_DOC));
    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-observe') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
  });

  // ---------------------------------------------------------------------------
  // New tests — sliding pill, armed state, animated confirm row (plan §2)
  // ---------------------------------------------------------------------------

  // 13. Armed state: clicking a non-active segment arms it; aria-pressed stays on old segment
  it('clicking Propose segment arms it with .autonomy-segment--armed; aria-pressed stays on propose_apply', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    // Wait for loaded state
    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // Click propose segment (not the active one)
    await fireEvent.click(getByTestId('autonomy-mode-propose'));

    // After click: propose should be armed
    const proposeBtn = getByTestId('autonomy-mode-propose') as HTMLButtonElement;
    expect(proposeBtn.classList.contains('autonomy-segment--armed')).toBe(true);

    // aria-pressed stays on propose_apply — NO optimistic update
    expect(
      (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(proposeBtn.getAttribute('aria-pressed')).toBe('false');
  });

  // 14. Armed state: clicking Observe arms it; propose_apply remains aria-pressed=true
  it('clicking Observe arms it with .autonomy-segment--armed; propose_apply aria-pressed stays true', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));

    const observeBtn = getByTestId('autonomy-mode-observe') as HTMLButtonElement;
    expect(observeBtn.classList.contains('autonomy-segment--armed')).toBe(true);
    expect(
      (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
    ).toBe('true');
  });

  // 15. Armed state clears on cancel
  it('armed class is removed after clicking Cancel', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));

    // Observe should be armed
    const observeBtn = getByTestId('autonomy-mode-observe') as HTMLButtonElement;
    expect(observeBtn.classList.contains('autonomy-segment--armed')).toBe(true);

    // Cancel
    await fireEvent.click(getByTestId('autonomy-cancel'));

    await waitFor(() => {
      expect(observeBtn.classList.contains('autonomy-segment--armed')).toBe(false);
    });
  });

  // 16. Pill element is present in the loaded state and is aria-hidden
  it('pill element exists in the loaded state and is aria-hidden', async () => {
    const records: CallRecord[] = [];
    const { container } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      const pill = container.querySelector('.autonomy-segments__pill');
      expect(pill).not.toBeNull();
      expect(pill?.getAttribute('aria-hidden')).toBe('true');
    });
  });

  // 17. Confirm row appears on click and disappears on cancel
  it('confirm row appears after click and disappears on cancel (with tick)', async () => {
    const records: CallRecord[] = [];
    const { getByTestId, queryByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });

    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // Confirm row should not be visible yet
    expect(queryByTestId('autonomy-confirm')).toBeNull();

    // Click to arm
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    // Tick to let Svelte flush any async state updates
    await tick();

    // Confirm row should now be visible
    await waitFor(() => {
      expect(getByTestId('autonomy-confirm')).toBeTruthy();
      expect(getByTestId('autonomy-cancel')).toBeTruthy();
    });

    // Cancel — row should disappear
    await fireEvent.click(getByTestId('autonomy-cancel'));
    await tick();

    await waitFor(() => {
      expect(queryByTestId('autonomy-confirm')).toBeNull();
    });

    // No POST should have been made
    const posts = records.filter((r) => r.init?.method === 'POST');
    expect(posts).toHaveLength(0);
  });

  // 12. Stale-GET seq guard
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
      if (getCount === 2) return staleGet;
      return newerGet;
    };

    const { getByTestId } = render(AutonomyControl, { props: { call } });

    await waitFor(() => expect(getByTestId('autonomy-retry')).toBeTruthy());

    const retryBtn = getByTestId('autonomy-retry');
    retryBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    retryBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(getCount).toBe(3);

    // Newer resolves with propose_apply
    resolveNewer(makeResponse(PROPOSE_APPLY_DOC));
    await waitFor(() => {
      expect(
        (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
      ).toBe('true');
    });

    // Stale resolves with observe — must be dropped
    resolveStale(makeResponse(OBSERVE_DOC));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(
      (getByTestId('autonomy-mode-propose_apply') as HTMLButtonElement).getAttribute('aria-pressed'),
    ).toBe('true');
  });

  // ---- Current-mode caption (names the active mode in words) ----

  it('current-mode caption names the active mode (Propose + Apply)', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });
    await waitFor(() => {
      expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
    });
  });

  it('current-mode caption reflects observe mode exactly', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(OBSERVE_DOC)) },
    });
    await waitFor(() => {
      expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Observe');
    });
  });

  it('current-mode caption stays on the committed mode while a switch is armed (not the pending one)', async () => {
    const records: CallRecord[] = [];
    const { getByTestId } = render(AutonomyControl, {
      props: { call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC)) },
    });
    await waitFor(() => {
      expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
    });

    // Arm a switch to observe (clicked, not yet confirmed)
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-cancel')).toBeTruthy());

    // Caption must still name the committed mode — the dial has not changed yet
    expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
  });

  // Encodes the operator's requirement: after switching, the formerly-active tab
  // returns to normal (loses --active, so it regains the click/hover affordance).
  it('after a confirmed switch the caption + active segment move on, freeing the old tab', async () => {
    const records: CallRecord[] = [];
    const postResponse: AutonomyDoc = {
      mode: 'observe',
      reason: null,
      actor: null,
      updated_at: null,
      read_error: false,
    };
    const { getByTestId } = render(AutonomyControl, {
      props: {
        call: makeCall(records, makeResponse(PROPOSE_APPLY_DOC), makeResponse(postResponse)),
      },
    });
    await waitFor(() => {
      expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
    });

    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));

    // Caption + active state land on the new mode
    await waitFor(() => {
      expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Observe');
    });
    const observeBtn = getByTestId('autonomy-mode-observe');
    const paBtn = getByTestId('autonomy-mode-propose_apply');
    expect(observeBtn.classList.contains('autonomy-segment--active')).toBe(true);
    expect(observeBtn.getAttribute('aria-pressed')).toBe('true');

    // The formerly-active tab is no longer settled — switchable again
    expect(paBtn.classList.contains('autonomy-segment--active')).toBe(false);
    expect(paBtn.getAttribute('aria-pressed')).toBe('false');
  });
});
