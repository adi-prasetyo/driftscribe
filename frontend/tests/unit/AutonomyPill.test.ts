import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import AutonomyPill from '../../src/components/AutonomyPill.svelte';
import { createAutonomyStore } from '../../src/lib/autonomyStore';
import { HEADER_POPOVER_EVENT } from '../../src/lib/headerPopover';
import { AUTONOMY_EXPLAINER_HEADING } from '../../src/lib/autonomy';

// AutonomyPill is the COMPACT header surface: a status pill that, while loaded,
// doubles as the trigger via an anchored popover hosting the full dial.
// Concurrency is pinned in autonomyStore.test.ts; here: rendering per kind, the
// popover flow, dismiss-while-saving, header-popover coordination, and focus.

afterEach(cleanup);

type CallRecord = { path: string; init?: RequestInit };
function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
const PA = { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false };
const OBSERVE = { mode: 'observe', reason: 'r', actor: 'ops@x', updated_at: '2026-06-11T10:00:00Z', read_error: false };

async function mount(
  getResp: Response | (() => Response | Promise<Response>),
  postResp?: Response | Promise<Response>,
) {
  const records: CallRecord[] = [];
  const call = async (path: string, init?: RequestInit): Promise<Response> => {
    records.push({ path, init });
    if (init?.method === 'POST') return postResp ?? res(OBSERVE);
    return typeof getResp === 'function' ? getResp() : getResp;
  };
  const store = createAutonomyStore(call);
  await store.fetchAutonomy();
  const view = render(AutonomyPill, { props: { autonomy: store } });
  return { ...view, records, store };
}

describe('AutonomyPill', () => {
  it('loading → muted state, not interactive', () => {
    const store = createAutonomyStore(async () => res(PA)); // never fetched
    const { getByTestId, queryByTestId } = render(AutonomyPill, { props: { autonomy: store } });
    expect(getByTestId('autonomy-pill-state').textContent?.toLowerCase()).toContain('checking');
    expect(queryByTestId('autonomy-pill-toggle')).toBeNull();
  });

  it('loaded → pill names the mode; popover closed initially', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    const toggle = getByTestId('autonomy-pill-toggle');
    expect(toggle.textContent).toContain('Propose + Apply');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(queryByTestId('autonomy-popover')).toBeNull();
  });

  it('click opens the popover with the three segments', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    expect(getByTestId('autonomy-mode-observe')).toBeTruthy();
    expect(getByTestId('autonomy-mode-propose')).toBeTruthy();
    expect(getByTestId('autonomy-mode-propose_apply')).toBeTruthy();
    expect(getByTestId('autonomy-pill-toggle').getAttribute('aria-expanded')).toBe('true');
  });

  it('arm + confirm POSTs {mode,reason}; popover closes; pill shows new mode', async () => {
    const post = { mode: 'observe', reason: 'going read-only', actor: 'ops@x', updated_at: null, read_error: false };
    const { getByTestId, queryByTestId, records } = await mount(res(PA), res(post));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.input(getByTestId('autonomy-reason'), { target: { value: 'going read-only' } });
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => {
      expect(queryByTestId('autonomy-popover')).toBeNull();
      expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Observe');
    });
    const p = records.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(p.init!.body as string)).toEqual({ mode: 'observe', reason: 'going read-only' });
  });

  it('POST 500 → autonomy-error shown; popover stays open; mode unchanged', async () => {
    const { getByTestId } = await mount(res(PA), res('e', 500));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => expect(getByTestId('autonomy-error')).toBeTruthy());
    expect(getByTestId('autonomy-popover')).toBeTruthy();
    expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Propose + Apply');
  });

  it('Escape does NOT close mid-POST; auto-closes on success', async () => {
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const { getByTestId, queryByTestId } = await mount(res(PA), pending);
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => expect((getByTestId('autonomy-confirm') as HTMLButtonElement).disabled).toBe(true));
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(getByTestId('autonomy-popover')).toBeTruthy(); // blocked mid-commit
    resolvePost(res(OBSERVE));
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
  });

  it('Escape closes when not saving', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
  });

  it('outside pointerdown closes the popover', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    await fireEvent.pointerDown(document.body);
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
  });

  it('opening announces on the header bus', async () => {
    const { getByTestId } = await mount(res(PA));
    const seen: string[] = [];
    const h = (e: Event) => seen.push((e as CustomEvent).detail?.id);
    window.addEventListener(HEADER_POPOVER_EVENT, h);
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    expect(seen).toContain('autonomy');
    window.removeEventListener(HEADER_POPOVER_EVENT, h);
  });

  it('a foreign open closes us WITHOUT returning focus to our toggle (plan-review #1)', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    const toggle = getByTestId('autonomy-pill-toggle');
    await fireEvent.click(toggle);
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    window.dispatchEvent(new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id: 'pause' } }));
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
    expect(document.activeElement).not.toBe(toggle);
  });

  it('opening focuses the active segment (plan-review #2)', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    await waitFor(() => expect(document.activeElement).toBe(getByTestId('autonomy-mode-propose_apply')));
  });

  it('unknown → visible retry button refetches', async () => {
    let n = 0;
    const store = createAutonomyStore(async () => { n += 1; return n === 1 ? res('e', 500) : res(PA); });
    await store.fetchAutonomy(); // → unknown
    const { getByTestId } = render(AutonomyPill, { props: { autonomy: store } });
    const retry = getByTestId('autonomy-pill-retry');
    expect(retry.textContent?.toLowerCase()).toContain('retry');
    await fireEvent.click(retry);
    await waitFor(() => expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Propose + Apply'));
  });

  it('read_error loaded → pill shows fail-closed; popover shows the warning', async () => {
    const { getByTestId } = await mount(res({ mode: 'observe', read_error: true }));
    expect(getByTestId('autonomy-pill-toggle').textContent?.toLowerCase()).toContain('fail-closed');
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-read-error')).toBeTruthy());
  });

  it('current-mode caption stays committed while a switch is armed', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-cancel')).toBeTruthy());
    expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
  });

  it('explainer is collapsed by default and toggles open', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    const toggle = await waitFor(() => getByTestId('autonomy-explainer-toggle'));
    expect(toggle.textContent).toContain(AUTONOMY_EXPLAINER_HEADING);
    expect(queryByTestId('autonomy-explainer-body')).toBeNull();
    await fireEvent.click(toggle);
    await waitFor(() => expect(getByTestId('autonomy-explainer-body')).toBeTruthy());
  });

  it('cancel closes the arm row with no POST; reopen clears reason', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-reason')).toBeTruthy());
    await fireEvent.input(getByTestId('autonomy-reason'), { target: { value: 'abc' } });
    await fireEvent.click(getByTestId('autonomy-cancel'));
    await waitFor(() => expect(queryByTestId('autonomy-confirm')).toBeNull());
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(0);
    // close + reopen → arm again → reason cleared
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-reason')).toBeTruthy());
    expect((getByTestId('autonomy-reason') as HTMLInputElement).value).toBe('');
  });
});
