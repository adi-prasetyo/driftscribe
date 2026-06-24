import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import PauseBanner from '../../src/components/PauseBanner.svelte';
import { createPauseStore } from '../../src/lib/pauseStore';

// PauseBanner is the LOUD content surface: it renders nothing while running,
// the prominent banner when paused, and a fail-closed note when unknown. The
// concurrency contract lives in pauseStore.test.ts; here we pin rendering +
// the resume-confirm UI (incl. the dismiss-while-saving guard, Codex #3/#5).

afterEach(cleanup);

type CallRecord = { path: string; init?: RequestInit };

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const RUNNING = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };
const PAUSED = {
  paused: true,
  reason: 'investigating alert',
  actor: 'ops@example.com',
  updated_at: '2026-06-10T14:02:00Z',
  read_error: false,
};

/** Create a store + fake call, await the initial fetch, then render the banner. */
async function mount(
  getResponse: Response | (() => Response | Promise<Response>),
  postResponse?: Response | Promise<Response>,
) {
  const records: CallRecord[] = [];
  const call = async (path: string, init?: RequestInit): Promise<Response> => {
    records.push({ path, init });
    if (init?.method === 'POST') {
      return postResponse ?? res(RUNNING);
    }
    return typeof getResponse === 'function' ? getResponse() : getResponse;
  };
  const store = createPauseStore(call);
  await store.fetchPause();
  const view = render(PauseBanner, { props: { pause: store } });
  return { ...view, records, store };
}

describe('PauseBanner', () => {
  it('running → renders nothing (no banner element)', async () => {
    const { queryByTestId } = await mount(res(RUNNING));
    expect(queryByTestId('pause-banner')).toBeNull();
  });

  it('paused → banner with exact copy, actor, reason, <time datetime>, Resume', async () => {
    const { getByTestId } = await mount(res(PAUSED));
    await waitFor(() => {
      expect(getByTestId('pause-state').textContent?.trim()).toBe(
        'DriftScribe is paused. No new agent activity will start.',
      );
    });
    const banner = getByTestId('pause-banner');
    expect(banner.textContent).toContain('ops@example.com');
    expect(banner.textContent).toContain('investigating alert');
    const timeEl = banner.querySelector('time');
    expect(timeEl).not.toBeNull();
    expect(timeEl!.getAttribute('datetime')).toBe('2026-06-10T14:02:00Z');
    expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume');
  });

  it('read_error:true paused → fail-closed copy instead of actor/time', async () => {
    const { getByTestId } = await mount(res({ paused: true, read_error: true }));
    await waitFor(() => {
      expect(getByTestId('pause-state').textContent).toContain('DriftScribe is paused');
    });
    expect(getByTestId('pause-banner').textContent).toContain(
      'pause state could not be read, failing closed',
    );
  });

  it('unknown → fail-closed note + Retry; clicking Retry refetches and clears the banner', async () => {
    let n = 0;
    const records: CallRecord[] = [];
    const call = async (path: string, init?: RequestInit): Promise<Response> => {
      records.push({ path, init });
      n += 1;
      if (n === 1) throw new Error('network');
      return res(RUNNING);
    };
    const store = createPauseStore(call);
    await store.fetchPause(); // → unknown
    const { getByTestId, queryByTestId } = render(PauseBanner, { props: { pause: store } });

    await waitFor(() => {
      const state = getByTestId('pause-state');
      expect(state.textContent).toContain('Pause state unknown');
      expect(state.textContent).toContain('fails closed');
    });
    await fireEvent.click(getByTestId('pause-retry'));

    // Recovered to running → banner disappears
    await waitFor(() => expect(queryByTestId('pause-banner')).toBeNull());
    expect(records.filter((r) => !r.init?.method).length).toBeGreaterThanOrEqual(2);
  });

  it('Resume → confirm row → confirm POSTs {paused:false} and the banner clears', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(PAUSED), res(RUNNING));
    await waitFor(() => expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume'));

    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-confirm'));

    await waitFor(() => expect(queryByTestId('pause-banner')).toBeNull());
    const post = records.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: false });
  });

  it('Cancel collapses the confirm row with no POST', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(PAUSED));
    await waitFor(() => expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume'));
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-cancel')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-cancel'));
    await waitFor(() => expect(queryByTestId('pause-confirm')).toBeNull());
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(0);
  });

  it('Resume POST 500 → pause-error shown, still paused, Resume usable again', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PAUSED), res('err', 500));
    await waitFor(() => expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume'));
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-confirm'));

    await waitFor(() => expect(getByTestId('pause-error')).toBeTruthy());
    // Still paused, confirm row collapsed, Resume usable again
    expect(getByTestId('pause-state').textContent).toContain('DriftScribe is paused');
    expect(queryByTestId('pause-confirm')).toBeNull();
    expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume');
  });

  it('confirm button disabled while the resume POST is in flight', async () => {
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const { getByTestId } = await mount(res(PAUSED), pending);
    await waitFor(() => expect(getByTestId('pause-toggle').textContent?.trim()).toBe('Resume'));
    await fireEvent.click(getByTestId('pause-toggle'));
    await waitFor(() => expect(getByTestId('pause-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-confirm'));

    await waitFor(() => {
      expect((getByTestId('pause-confirm') as HTMLButtonElement).disabled).toBe(true);
      // Cancel is also disabled mid-commit (no dismissal while saving)
      expect((getByTestId('pause-cancel') as HTMLButtonElement).disabled).toBe(true);
    });
    resolvePost(res(RUNNING));
  });
});
