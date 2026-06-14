import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { createPauseStore } from '../../src/lib/pauseStore';

// Store-level tests for the operator pause kill-switch. The shared monotonic
// seq guard and the single-flight POST guard are the security-critical bits —
// ported verbatim from the former PauseControl and pinned here in isolation.

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
  reason: 'maint',
  actor: 'ops@x',
  updated_at: '2026-06-10T14:02:00Z',
  read_error: false,
};

describe('createPauseStore', () => {
  it('fetchPause → running', async () => {
    const s = createPauseStore(async () => res(RUNNING));
    await s.fetchPause();
    expect(get(s).kind).toBe('running');
  });

  it('fetchPause → paused carries reason/actor/updatedAt', async () => {
    const s = createPauseStore(async () => res(PAUSED));
    await s.fetchPause();
    const st = get(s);
    expect(st.kind).toBe('paused');
    expect(st.actor).toBe('ops@x');
    expect(st.reason).toBe('maint');
    expect(st.updatedAt).toBe('2026-06-10T14:02:00Z');
  });

  it('GET throws → unknown', async () => {
    const s = createPauseStore(async () => {
      throw new Error('net');
    });
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('non-ok GET → unknown', async () => {
    const s = createPauseStore(async () => res('err', 500));
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('malformed 200 (paused not boolean) → unknown', async () => {
    const s = createPauseStore(async () => res({ paused: 'yes' }));
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('read_error:true paused doc surfaces readError', async () => {
    const s = createPauseStore(async () => res({ paused: true, read_error: true }));
    await s.fetchPause();
    expect(get(s)).toMatchObject({ kind: 'paused', readError: true });
  });

  it('confirm(true, reason) POSTs {paused:true,reason} and flips from response', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(PAUSED) : res(RUNNING);
    });
    await s.fetchPause();
    const ok = await s.confirm(true, '  maint  ');
    expect(ok).toBe(true);
    expect(get(s).kind).toBe('paused');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: true, reason: 'maint' });
  });

  it('confirm(true) with blank reason omits reason key', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(PAUSED) : res(RUNNING);
    });
    await s.fetchPause();
    await s.confirm(true, '   ');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: true });
  });

  it('confirm(false) POSTs {paused:false} and flips to running', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(RUNNING) : res(PAUSED);
    });
    await s.fetchPause();
    const ok = await s.confirm(false);
    expect(ok).toBe(true);
    expect(get(s).kind).toBe('running');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: false });
  });

  it('confirm POST 500 → returns false, state preserved', async () => {
    const s = createPauseStore(async (path, init) =>
      init?.method === 'POST' ? res('err', 500) : res(RUNNING),
    );
    await s.fetchPause();
    const ok = await s.confirm(true, 'x');
    expect(ok).toBe(false);
    expect(get(s).kind).toBe('running'); // not flipped
  });

  it('confirm POST throws → returns false', async () => {
    const s = createPauseStore(async (path, init) => {
      if (init?.method === 'POST') throw new Error('net');
      return res(RUNNING);
    });
    await s.fetchPause();
    expect(await s.confirm(true, 'x')).toBe(false);
  });

  it('single-flight: overlapping confirm() makes exactly one POST', async () => {
    const recs: CallRecord[] = [];
    let resolve!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolve = r));
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(RUNNING);
    });
    await s.fetchPause();
    const a = s.confirm(true, 'x');
    const b = s.confirm(true, 'y'); // must be rejected by saving guard
    expect(recs.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
    resolve(res(PAUSED));
    expect(await a).toBe(true);
    expect(await b).toBe(false);
  });

  it('seq guard: a confirm() started after fetchPause() drops the stale GET result', async () => {
    // Stale GET resolves AFTER a newer confirm has set fresh state.
    let resolveGet!: (r: Response) => void;
    const slowGet = new Promise<Response>((r) => (resolveGet = r));
    let n = 0;
    const s = createPauseStore(async (path, init) => {
      if (init?.method === 'POST') return res(PAUSED);
      n += 1;
      return n === 1 ? slowGet : res(RUNNING);
    });
    const p = s.fetchPause(); // GET #1 in-flight (stale)
    await s.confirm(true, 'x'); // bumps seq, sets paused
    expect(get(s).kind).toBe('paused');
    resolveGet(res(RUNNING)); // stale GET now resolves running
    await p;
    expect(get(s).kind).toBe('paused'); // seq guard dropped it
  });

  it('seq guard: overlapping fetchPause() — newer GET wins, stale older GET is dropped', async () => {
    // Ports old PauseControl test 12 (GET/GET overlap) to the store level.
    let n = 0;
    let resolveStale!: (r: Response) => void;
    let resolveNewer!: (r: Response) => void;
    const stale = new Promise<Response>((r) => (resolveStale = r));
    const newer = new Promise<Response>((r) => (resolveNewer = r));
    const s = createPauseStore(async () => {
      n += 1;
      return n === 1 ? stale : newer;
    });
    const a = s.fetchPause(); // GET #1 (older / stale)
    const b = s.fetchPause(); // GET #2 (newer) — bumps seq
    resolveNewer(res(RUNNING)); // newer resolves first → running
    await b;
    expect(get(s).kind).toBe('running');
    resolveStale(res(PAUSED)); // stale resolves later → must be dropped
    await a;
    expect(get(s).kind).toBe('running');
  });

  it('commit wins: fetchPause() is a no-op while a confirm() POST is in flight', async () => {
    const recs: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(RUNNING);
    });
    await s.fetchPause(); // GET #1
    const c = s.confirm(true, 'x'); // POST in flight, saving=true
    const getsBefore = recs.filter((r) => !r.init?.method).length;
    await s.fetchPause(); // must no-op while saving
    expect(recs.filter((r) => !r.init?.method).length).toBe(getsBefore); // no new GET
    resolvePost(res(PAUSED));
    expect(await c).toBe(true);
    expect(get(s).kind).toBe('paused'); // the POST result applied, not stomped
  });
});
