import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TOKEN_KEY,
  apiFetch,
  clearToken,
  getStoredToken,
  setToken,
} from '../../src/lib/api';

// Re-homes the token / poll-auth guards that previously lived in the legacy
// single-file UI (transparency.html `api()` IIFE) and were pinned by
// tests/integration/test_ui_transparency.py:72-93 — the try-then-prompt /
// CF-Access fetch contract is now a unit-tested module.

const TOKEN_HEADER = 'X-DriftScribe-Token';

/** Build a minimal `Response`-like object jsdom can return from a stubbed fetch. */
function makeResponse(status: number, body = ''): Response {
  return new Response(body, { status });
}

/** Pull the X-DriftScribe-Token header off a recorded fetch call's init arg. */
function tokenHeaderOf(init: RequestInit | undefined): string | null {
  const h = new Headers(init?.headers ?? undefined);
  return h.get(TOKEN_HEADER);
}

beforeEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('TOKEN_KEY', () => {
  it('is the documented sessionStorage key', () => {
    expect(TOKEN_KEY).toBe('driftscribe_token');
  });
});

describe('token storage helpers', () => {
  it('getStoredToken reads sessionStorage[TOKEN_KEY]', () => {
    expect(getStoredToken()).toBeNull();
    sessionStorage.setItem(TOKEN_KEY, 'tok-abc');
    expect(getStoredToken()).toBe('tok-abc');
  });

  it('setToken writes sessionStorage[TOKEN_KEY]', () => {
    setToken('tok-xyz');
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('tok-xyz');
    expect(getStoredToken()).toBe('tok-xyz');
  });

  it('clearToken removes sessionStorage[TOKEN_KEY]', () => {
    sessionStorage.setItem(TOKEN_KEY, 'tok-abc');
    clearToken();
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(getStoredToken()).toBeNull();
  });
});

describe('apiFetch', () => {
  it('(1) sends the stored token as X-DriftScribe-Token', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stored-tok');
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn();
    const resp = await apiFetch('/trace/abc', undefined, onAuthRequired);

    expect(resp.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/trace/abc');
    expect(tokenHeaderOf(init)).toBe('stored-tok');
    // ok + stored token → token kept, no prompt.
    expect(onAuthRequired).not.toHaveBeenCalled();
    expect(getStoredToken()).toBe('stored-tok');
  });

  it('preserves caller-supplied headers alongside the token header', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stored-tok');
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    await apiFetch('/chat', {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
    });

    const [, init] = fetchMock.mock.calls[0];
    const h = new Headers(init.headers);
    expect(h.get('Accept')).toBe('text/event-stream');
    expect(h.get(TOKEN_HEADER)).toBe('stored-tok');
    expect(init.method).toBe('POST');
  });

  it('(2) on 401 clears token, calls onAuthRequired, retries with returned token, returns 200', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stale-tok');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(makeResponse(401, 'nope'))
      .mockResolvedValueOnce(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => {
      // The stale token must already be cleared by the time the UI prompts.
      expect(getStoredToken()).toBeNull();
      return 'fresh-tok';
    });

    const resp = await apiFetch('/trace/abc', undefined, onAuthRequired);

    expect(resp.status).toBe(200);
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Retry carried the freshly returned token.
    expect(tokenHeaderOf(fetchMock.mock.calls[1][1])).toBe('fresh-tok');
    // A returned token is persisted.
    expect(getStoredToken()).toBe('fresh-tok');
  });

  it('on 403 also clears token, prompts, and retries with returned token', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stale-tok');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(makeResponse(403, 'forbidden'))
      .mockResolvedValueOnce(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => 'fresh-tok');
    const resp = await apiFetch('/decisions', undefined, onAuthRequired);

    expect(resp.status).toBe(200);
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    expect(tokenHeaderOf(fetchMock.mock.calls[1][1])).toBe('fresh-tok');
    expect(getStoredToken()).toBe('fresh-tok');
  });

  it('(3) 401 with onAuthRequired returning null → token cleared, no retry, original 401 returned', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stale-tok');
    const resp401 = makeResponse(401, 'nope');
    const fetchMock = vi.fn().mockResolvedValue(resp401);
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => null);
    const resp = await apiFetch('/trace/abc', undefined, onAuthRequired);

    expect(resp).toBe(resp401);
    expect(resp.status).toBe(401);
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    // No retry: exactly one fetch.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Token stays cleared after the failed auth.
    expect(getStoredToken()).toBeNull();
  });

  it('401 with no onAuthRequired → token cleared, no retry, original 401 returned', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stale-tok');
    const resp401 = makeResponse(401, 'nope');
    const fetchMock = vi.fn().mockResolvedValue(resp401);
    vi.stubGlobal('fetch', fetchMock);

    const resp = await apiFetch('/trace/abc');

    expect(resp).toBe(resp401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(getStoredToken()).toBeNull();
  });

  it('(4) CF-Access path: no stored token + 200 → onAuthRequired NOT called, no token header sent', async () => {
    // CF injects Cf-Access-Jwt-Assertion server-side; the SPA sends no token
    // and the request succeeds, so we must NOT prompt.
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => 'should-not-be-used');
    const resp = await apiFetch('/decisions', undefined, onAuthRequired);

    expect(resp.status).toBe(200);
    expect(onAuthRequired).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // No token header on the wire.
    expect(tokenHeaderOf(fetchMock.mock.calls[0][1])).toBeNull();
    // Still no token stored (CF path never stores one).
    expect(getStoredToken()).toBeNull();
  });

  it('no token + 401 + onAuthRequired returns a token → retries with the new token and stores it', async () => {
    // CF not configured / not signed in: first request has no token, server
    // 401s, the UI prompts, and the retry carries the operator token.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(makeResponse(401, 'need token'))
      .mockResolvedValueOnce(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => 'typed-tok');
    const resp = await apiFetch('/chat', { method: 'POST' }, onAuthRequired);

    expect(resp.status).toBe(200);
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    // First attempt had no token header.
    expect(tokenHeaderOf(fetchMock.mock.calls[0][1])).toBeNull();
    // Retry carries the typed token.
    expect(tokenHeaderOf(fetchMock.mock.calls[1][1])).toBe('typed-tok');
    expect(getStoredToken()).toBe('typed-tok');
  });

  it('retry that still 401s → clears the (newly stored) token and returns the second 401', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stale-tok');
    const secondResp = makeResponse(401, 'still no');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(makeResponse(401, 'first'))
      .mockResolvedValueOnce(secondResp);
    vi.stubGlobal('fetch', fetchMock);

    const onAuthRequired = vi.fn(async () => 'bad-tok');
    const resp = await apiFetch('/trace/abc', undefined, onAuthRequired);

    // Final response is the retry's 401, not the original.
    expect(resp).toBe(secondResp);
    expect(resp.status).toBe(401);
    expect(onAuthRequired).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // onAuthRequired is invoked only once — no infinite prompt loop.
    // Bad token is cleared again after the second rejection.
    expect(getStoredToken()).toBeNull();
  });

  it('does not mutate the caller-supplied init.headers object', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'stored-tok');
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, 'ok'));
    vi.stubGlobal('fetch', fetchMock);

    const callerHeaders = { Accept: 'application/json' };
    await apiFetch('/decisions', { headers: callerHeaders });

    // The caller's plain object must be untouched (no token leaked into it).
    expect(callerHeaders).toEqual({ Accept: 'application/json' });
  });
});
