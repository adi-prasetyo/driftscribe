import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import PausePill from '../../src/components/PausePill.svelte';
import { createPauseStore } from '../../src/lib/pauseStore';

// PausePill is the COMPACT header surface: a status pill that, while running,
// doubles as the pause trigger via a popover. Paused/unknown are status-only
// (action lives in the banner). Concurrency is pinned in pauseStore.test.ts;
// here: rendering per kind, the popover flow, and the dismiss-while-saving
// guard (Codex #3).

afterEach(cleanup);

type CallRecord = { path: string; init?: RequestInit };

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const RUNNING = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };
const PAUSED = { paused: true, reason: 'm', actor: 'ops@x', updated_at: null, read_error: false };

async function mount(
  getResponse: Response | (() => Response | Promise<Response>),
  postResponse?: Response | Promise<Response>,
) {
  const records: CallRecord[] = [];
  const call = async (path: string, init?: RequestInit): Promise<Response> => {
    records.push({ path, init });
    if (init?.method === 'POST') return postResponse ?? res(PAUSED);
    return typeof getResponse === 'function' ? getResponse() : getResponse;
  };
  const store = createPauseStore(call);
  await store.fetchPause();
  const view = render(PausePill, { props: { pause: store } });
  return { ...view, records, store };
}

describe('PausePill', () => {
  it('loading → muted "Checking…" status, not interactive', () => {
    const store = createPauseStore(async () => res(RUNNING)); // never fetched → loading
    const { getByTestId, queryByTestId } = render(PausePill, { props: { pause: store } });
    expect(getByTestId('pause-pill-state').textContent).toContain('Checking');
    expect(queryByTestId('pause-pill-toggle')).toBeNull();
  });

  it('running → "Active" toggle button, popover closed initially', async () => {
    const { getByTestId, queryByTestId } = await mount(res(RUNNING));
    const toggle = getByTestId('pause-pill-toggle');
    expect(toggle.textContent).toContain('Active');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(queryByTestId('pause-popover')).toBeNull();
  });

  it('click toggle → popover opens with reason input; aria-expanded true', async () => {
    const { getByTestId } = await mount(res(RUNNING));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover')).toBeTruthy());
    expect(getByTestId('pause-popover-reason')).toBeTruthy();
    expect(getByTestId('pause-pill-toggle').getAttribute('aria-expanded')).toBe('true');
  });

  it('confirm → POSTs {paused:true,reason}; popover closes; pill shows Paused', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(RUNNING), res(PAUSED));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-reason')).toBeTruthy());
    await fireEvent.input(getByTestId('pause-popover-reason'), { target: { value: 'maint' } });
    await fireEvent.click(getByTestId('pause-popover-confirm'));

    await waitFor(() => {
      expect(queryByTestId('pause-popover')).toBeNull();
      expect(getByTestId('pause-pill-state').textContent).toContain('Paused');
    });
    const post = records.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: true, reason: 'maint' });
  });

  it('confirm POST 500 → error shown; popover stays open; toggle still running', async () => {
    const { getByTestId } = await mount(res(RUNNING), res('err', 500));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-popover-confirm'));

    await waitFor(() => expect(getByTestId('pause-popover-error')).toBeTruthy());
    expect(getByTestId('pause-popover')).toBeTruthy(); // still open
    expect(getByTestId('pause-pill-toggle').textContent).toContain('Active'); // still running
  });

  it('confirm + cancel buttons disabled while the POST is in flight', async () => {
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const { getByTestId } = await mount(res(RUNNING), pending);
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-popover-confirm'));

    await waitFor(() => {
      expect((getByTestId('pause-popover-confirm') as HTMLButtonElement).disabled).toBe(true);
      expect((getByTestId('pause-popover-cancel') as HTMLButtonElement).disabled).toBe(true);
    });
    resolvePost(res(PAUSED));
  });

  it('cancel closes the popover with no POST; reopen shows an empty reason input', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(RUNNING));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-reason')).toBeTruthy());
    await fireEvent.input(getByTestId('pause-popover-reason'), { target: { value: 'abc' } });
    await fireEvent.click(getByTestId('pause-popover-cancel'));
    await waitFor(() => expect(queryByTestId('pause-popover')).toBeNull());
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(0);

    // Reopen → reason cleared
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-reason')).toBeTruthy());
    expect((getByTestId('pause-popover-reason') as HTMLInputElement).value).toBe('');
  });

  it('reason resets across a pause→running cycle (Codex #5)', async () => {
    // GET as a thunk → a fresh Response per fetch (a Response body reads once).
    const { getByTestId, queryByTestId, store } = await mount(() => res(RUNNING), res(PAUSED));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-reason')).toBeTruthy());
    await fireEvent.input(getByTestId('pause-popover-reason'), { target: { value: 'maint' } });
    await fireEvent.click(getByTestId('pause-popover-confirm'));
    await waitFor(() => expect(getByTestId('pause-pill-state').textContent).toContain('Paused'));

    // Operator resumes elsewhere (the banner) → store flips back to running.
    await store.fetchPause(); // GET returns RUNNING
    await waitFor(() => expect(getByTestId('pause-pill-toggle')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-reason')).toBeTruthy());
    expect((getByTestId('pause-popover-reason') as HTMLInputElement).value).toBe('');
    expect(queryByTestId('pause-popover-error')).toBeNull();
  });

  it('Escape closes the popover (when not saving)', async () => {
    const { getByTestId, queryByTestId } = await mount(res(RUNNING));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover')).toBeTruthy());
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('pause-popover')).toBeNull());
  });

  it('Escape does NOT close while a POST is in flight (Codex #3)', async () => {
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const { getByTestId } = await mount(res(RUNNING), pending);
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('pause-popover-confirm'));
    await waitFor(() =>
      expect((getByTestId('pause-popover-confirm') as HTMLButtonElement).disabled).toBe(true),
    );
    await fireEvent.keyDown(window, { key: 'Escape' });
    // Popover stays — dismissal blocked mid-commit
    expect(getByTestId('pause-popover')).toBeTruthy();
    resolvePost(res(PAUSED));
  });

  it('outside pointerdown closes the popover (when not saving)', async () => {
    const { getByTestId, queryByTestId } = await mount(res(RUNNING));
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover')).toBeTruthy());
    await fireEvent.pointerDown(document.body);
    await waitFor(() => expect(queryByTestId('pause-popover')).toBeNull());
  });

  it('paused → status-only "Paused", no toggle button', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PAUSED));
    expect(getByTestId('pause-pill-state').textContent).toContain('Paused');
    expect(queryByTestId('pause-pill-toggle')).toBeNull();
  });

  it('unknown → status-only "State unknown"', async () => {
    const { getByTestId } = await mount(() => {
      throw new Error('network');
    });
    expect(getByTestId('pause-pill-state').textContent).toContain('State unknown');
  });
});
