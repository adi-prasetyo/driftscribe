// Deep-link helpers for the shareable reasoning-timeline URL (?reasoning=<id>).
//
// The SPA shell is served at "/" ONLY (agent/main.py — no catch-all route), so a
// linkable pointer to one past reasoning timeline rides a query param on that
// same root. That is deliberate: it needs NO extra backend route and NO
// Cloudflare demo-allowlist entry — the "/" shell is un-gated and the GET
// /trace/{id} the replay fetches is already on the demo allowlist. A pretty
// /reasoning/{id} PATH would have cost both (see the design discussion).

// A DriftScribe trace id is a Cloud-trace id: exactly 32 lowercase hex chars.
// This mirrors driftscribe_lib.logging._HEX32_RE (and agent/trace_fetcher.py),
// and the backend 400s GET /trace/{id} on anything else — so we only ever
// deep-link a well-formed id and never hand junk to openTrace().
const HEX32_RE = /^[0-9a-f]{32}$/;

/**
 * The trace id to replay from a `?reasoning=<hex32>` query string, or null when
 * the param is absent or malformed. Pure — the caller decides what to do with it
 * (App.svelte calls openTrace on boot; syncReasoningParam writes it back).
 */
export function reasoningTraceFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get('reasoning');
  return raw !== null && HEX32_RE.test(raw) ? raw : null;
}
