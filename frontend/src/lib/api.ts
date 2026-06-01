/**
 * Token + CF-Access fetch wrapper (try-then-prompt).
 *
 * Re-homes the auth plumbing from the legacy single-file UI
 * (`agent/templates/transparency.html` `api()` IIFE), previously pinned by
 * `tests/integration/test_ui_transparency.py:72-93`.
 *
 * Auth contract (plan Appendix A): every JSON endpoint requires either an
 * `X-DriftScribe-Token` header OR a `Cf-Access-Jwt-Assertion` header that
 * Cloudflare Access injects server-side. The SPA therefore tries the request
 * WITHOUT a token first when none is stored — that lets the CF-Access path
 * succeed silently. Only when the server explicitly demands a token (401/403)
 * do we clear any stale value and signal the UI to prompt, then retry once.
 */

export const TOKEN_KEY = 'driftscribe_token';

export type TokenState = 'ok' | 'missing' | 'invalid';

const TOKEN_HEADER = 'X-DriftScribe-Token';

/** Read the operator token from sessionStorage, or null if absent. */
export function getStoredToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

/** Persist the operator token to sessionStorage. */
export function setToken(t: string): void {
  sessionStorage.setItem(TOKEN_KEY, t);
}

/** Remove the operator token from sessionStorage. */
export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

/**
 * Fetch `path`, attaching the stored `X-DriftScribe-Token` when present.
 *
 * Behaviour:
 *  - Build `Headers` from `init.headers` (caller's header object is never
 *    mutated) and add the token header iff a token is stored.
 *  - On 401/403: clear the (now-rejected) token. If `onAuthRequired` is
 *    provided, await it; if it returns a token, persist it and retry the
 *    request ONCE with the token header. If the retry still 401/403s, clear
 *    the token again and return that (second) response. If `onAuthRequired`
 *    is absent or returns null, return the original 401/403 with no retry.
 *  - CF-Access path: no stored token + a 200 means CF injected its own header
 *    server-side — `onAuthRequired` is NOT called and no token is stored.
 *
 * Resolves with the final `Response`.
 */
export async function apiFetch(
  path: string,
  init?: RequestInit,
  onAuthRequired?: () => Promise<string | null>,
): Promise<Response> {
  const stored = getStoredToken();
  const headers = new Headers(init?.headers ?? undefined);
  if (stored) headers.set(TOKEN_HEADER, stored);

  const resp = await fetch(path, { ...init, headers });

  if (resp.status !== 401 && resp.status !== 403) {
    // Success (or a non-auth error like 500/503): leave the stored token as
    // is. The CF-Access path lands here with no stored token and never
    // prompts.
    return resp;
  }

  // Server demands a token: CF Access is disabled or failed. Drop the stale
  // value so the UI never re-sends a known-bad token.
  clearToken();

  if (!onAuthRequired) {
    return resp;
  }

  const fresh = await onAuthRequired();
  if (!fresh) {
    // Operator cancelled / declined — surface the original 401/403.
    return resp;
  }

  // Persist + retry ONCE with the freshly supplied token.
  setToken(fresh);
  const retryHeaders = new Headers(init?.headers ?? undefined);
  retryHeaders.set(TOKEN_HEADER, fresh);
  const retryResp = await fetch(path, { ...init, headers: retryHeaders });

  if (retryResp.status === 401 || retryResp.status === 403) {
    // Still rejected: clear the bad token. No second prompt (avoids a loop).
    clearToken();
  }
  return retryResp;
}
