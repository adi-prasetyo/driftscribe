import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { createAutonomyStore, autonomyNoteFor } from '../../src/lib/autonomyStore';
import { modeBlurb } from '../../src/lib/autonomy';
import { translate } from '../../src/lib/i18n';

// EN-bound translator — autonomyNoteFor now threads a TranslateFn; assertions
// below stay English (tests/unit/setup.ts pins the whole suite to the EN catalog).
const t = (key: Parameters<typeof translate>[1], params?: Record<string, string | number>) =>
  translate('en', key, params);

// Store-level tests for the operator autonomy dial. The shared monotonic seq
// guard, the single-flight POST guard, and the commit-wins guard are the
// security-critical bits — ported from the former AutonomyControl and pinned
// here in isolation (modelled on pauseStore.test.ts).

type CallRecord = { path: string; init?: RequestInit };
function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}
const OBSERVE = { mode: 'observe', reason: 'r', actor: 'ops@x', updated_at: '2026-06-11T10:00:00Z', read_error: false };
const PA = { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false };

describe('createAutonomyStore', () => {
  it('fetch → loaded carries mode/reason/actor/updatedAt', async () => {
    const s = createAutonomyStore(async () => res(OBSERVE));
    await s.fetchAutonomy();
    expect(get(s)).toMatchObject({
      kind: 'loaded', mode: 'observe', reason: 'r', actor: 'ops@x',
      updatedAt: '2026-06-11T10:00:00Z', readError: false,
    });
  });

  it('GET throws → unknown', async () => {
    const s = createAutonomyStore(async () => { throw new Error('net'); });
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('non-ok GET → unknown', async () => {
    const s = createAutonomyStore(async () => res('e', 500));
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('malformed 200 (mode invalid) → unknown', async () => {
    const s = createAutonomyStore(async () => res({ mode: 'yolo' }));
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('read_error:true surfaces readError on the loaded state', async () => {
    const s = createAutonomyStore(async () => res({ mode: 'observe', read_error: true }));
    await s.fetchAutonomy();
    expect(get(s)).toMatchObject({ kind: 'loaded', mode: 'observe', readError: true });
  });

  it('confirm POSTs {mode,reason} and applies the response', async () => {
    const recs: CallRecord[] = [];
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res({ ...OBSERVE, mode: 'observe' }) : res(PA);
    });
    await s.fetchAutonomy();
    const ok = await s.confirm('observe', '  going read-only  ');
    expect(ok).toBe(true);
    expect(get(s).mode).toBe('observe');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ mode: 'observe', reason: 'going read-only' });
  });

  it('confirm with blank reason omits reason key', async () => {
    const recs: CallRecord[] = [];
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(OBSERVE) : res(PA);
    });
    await s.fetchAutonomy();
    await s.confirm('observe', '   ');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ mode: 'observe' });
  });

  it('confirm POST 500 → false, state preserved', async () => {
    const s = createAutonomyStore(async (path, init) =>
      init?.method === 'POST' ? res('e', 500) : res(PA));
    await s.fetchAutonomy();
    expect(await s.confirm('observe')).toBe(false);
    expect(get(s).mode).toBe('propose_apply');
  });

  it('single-flight: overlapping confirm makes exactly one POST', async () => {
    const recs: CallRecord[] = [];
    let resolve!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolve = r));
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(PA);
    });
    await s.fetchAutonomy();
    const a = s.confirm('observe', 'x');
    const b = s.confirm('propose', 'y');
    expect(recs.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
    resolve(res(OBSERVE));
    expect(await a).toBe(true);
    expect(await b).toBe(false);
  });

  it('seq guard: a stale GET resolving after a confirm is dropped', async () => {
    let resolveGet!: (r: Response) => void;
    const slow = new Promise<Response>((r) => (resolveGet = r));
    let n = 0;
    const s = createAutonomyStore(async (path, init) => {
      if (init?.method === 'POST') return res(OBSERVE);
      n += 1;
      return n === 1 ? slow : res(PA);
    });
    const p = s.fetchAutonomy();
    await s.confirm('observe', 'x');
    expect(get(s).mode).toBe('observe');
    resolveGet(res(PA));
    await p;
    expect(get(s).mode).toBe('observe'); // stale GET dropped
  });

  it('commit wins: fetchAutonomy is a no-op while a confirm POST is in flight', async () => {
    const recs: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(PA);
    });
    await s.fetchAutonomy();
    const c = s.confirm('observe', 'x');
    const getsBefore = recs.filter((r) => !r.init?.method).length;
    await s.fetchAutonomy(); // must no-op
    expect(recs.filter((r) => !r.init?.method).length).toBe(getsBefore);
    resolvePost(res(OBSERVE));
    expect(await c).toBe(true);
    expect(get(s).mode).toBe('observe');
  });
});

describe('autonomyNoteFor', () => {
  const base = { reason: null, actor: null, updatedAt: null };
  it('loading → null (silent)', () => {
    expect(autonomyNoteFor({ kind: 'loading', mode: 'observe', readError: false, ...base }, t)).toBeNull();
  });
  it('unknown → null (silent, Codex #1)', () => {
    expect(autonomyNoteFor({ kind: 'unknown', mode: 'observe', readError: false, ...base }, t)).toBeNull();
  });
  it('loaded + propose_apply → null', () => {
    expect(autonomyNoteFor({ kind: 'loaded', mode: 'propose_apply', readError: false, ...base }, t)).toBeNull();
  });
  it('loaded + observe → observe note (no "write-capable tools are disabled")', () => {
    const note = autonomyNoteFor({ kind: 'loaded', mode: 'observe', readError: false, ...base }, t);
    expect(note).toBe(
      'The autonomy dial is currently set to Observe. Tools that open pull requests, issues, or approvals, and anything that merges or applies, are disabled until you raise the dial.',
    );
    expect(note).not.toContain('write-capable tools are disabled');
  });
  it('loaded + propose → propose note', () => {
    expect(autonomyNoteFor({ kind: 'loaded', mode: 'propose', readError: false, ...base }, t)).toBe(
      'The autonomy dial is currently set to Propose. Pull requests and issues are enabled; anything that merges or applies is disabled until you raise the dial.',
    );
  });
  it('loaded + read_error → fail-closed note (never says "set to")', () => {
    const note = autonomyNoteFor({ kind: 'loaded', mode: 'observe', readError: true, ...base }, t);
    expect(note).toBe(
      'Autonomy state could not be read. The effective mode is Observe (failing closed) until the dial can be read again.',
    );
    expect(note).not.toContain('currently set to');
  });
});

describe('Propose + Apply blurb honesty (Codex #4)', () => {
  it('names dependency autonomy AND keeps infra gated', () => {
    const blurb = modeBlurb('propose_apply', t).toLowerCase();
    expect(blurb).toContain('dependency');
    expect(blurb).toContain('approval');
    expect(blurb).toContain('infrastructure');
  });
});
