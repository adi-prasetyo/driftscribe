// Client-side custody of a crew-handoff nonce.
//
// The asymmetry this module exists to manage: the server persists a handoff
// proposal on the conversation (so `GET /conversations/{id}` can tell any
// client that one is OPEN), but it stores only a DIGEST of the redemption
// nonce. The plaintext nonce is transmitted exactly once, on the `done` frame
// of the turn that proposed it. The server therefore cannot re-serve it, and a
// reload cannot recover it from the API.
//
// So the chip needs BOTH halves and they come from different places:
//
//   is the proposal still open?  -> the server (persisted `pending_handoff`)
//   may THIS client act on it?   -> local custody of the nonce (here)
//
// Keeping the nonce in sessionStorage makes a reload work, which is the common
// case. It deliberately does NOT make a `?conversation=` link work on another
// device: the nonce is a capability, not a view, and shipping it in a URL is
// exactly what single-use credentials are supposed to prevent. On a device
// with no nonce the chip simply does not render — the crew's reply still says
// what it wants to do, and the operator can answer in the composer, which is
// the pre-handoff behavior and no worse than it.
//
// Losing the nonce is cheap by construction: the worst outcome is that the
// operator asks again and the crew re-proposes (which supersedes and burns the
// stale proposal server-side anyway).

import type { HandoffOffer } from './sse';
import type { PendingHandoff } from './types';

const KEY_PREFIX = 'ds.handoff.';

/**
 * Validate a `handoff` payload off either chat transport.
 *
 * The SSE frame is typed but the non-streaming JSON fallback is `any`, and
 * both feed the same custody path — so they get the same check rather than one
 * being trusted more than the other. Anything missing a usable route or nonce
 * returns undefined: a partial offer would render a chip whose button cannot
 * work. `reason` is allowed to be empty (a crew may propose without one).
 */
export function readHandoffOffer(raw: unknown): HandoffOffer | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const o = raw as Record<string, unknown>;
  const str = (v: unknown): string => (typeof v === 'string' ? v : '');
  const from = str(o.from);
  const to = str(o.to);
  const nonce = str(o.nonce);
  if (!from || !to || !nonce) return undefined;
  return {
    from,
    to,
    reason: str(o.reason),
    nonce,
    expires_at: str(o.expires_at),
  };
}

function storageKey(conversationId: string): string {
  return KEY_PREFIX + conversationId;
}

/** sessionStorage is unavailable in some privacy modes and throws on quota.
 *  Every path here fails soft: no custody just means no chip. */
function readRaw(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * Take custody of a freshly-minted nonce for `conversationId`.
 *
 * Called at the moment the `done` frame lands — the only moment the nonce
 * exists client-side. A new proposal overwrites any older one for the same
 * conversation, mirroring the server, where a fresh proposal supersedes and
 * burns its predecessor.
 */
export function rememberOffer(conversationId: string, offer: HandoffOffer): void {
  if (!conversationId || !offer?.nonce) return;
  try {
    window.sessionStorage.setItem(storageKey(conversationId), JSON.stringify(offer));
  } catch {
    /* No custody — the chip won't survive a reload. Not worth failing a turn. */
  }
}

/** Drop custody: the nonce was spent (confirmed or declined), or the proposal
 *  is gone. Safe to call when nothing is stored. */
export function forgetOffer(conversationId: string): void {
  if (!conversationId) return;
  try {
    window.sessionStorage.removeItem(storageKey(conversationId));
  } catch {
    /* fail-soft */
  }
}

/**
 * Has this offer's window closed?
 *
 * Fail-OPEN on an absent or unparseable `expires_at`: the server is the
 * authority on expiry (it answers 410) and the client's clock may be skewed,
 * so a date we cannot read must not silently hide an offer that still works.
 * A date we CAN read and that has passed does hide it — showing a button that
 * is certain to be refused is worse than showing nothing.
 */
export function isOfferExpired(
  offer: { expires_at?: string | null } | null | undefined,
  now: Date,
): boolean {
  if (!offer) return true;
  const raw = offer.expires_at;
  if (!raw) return false;
  const ts = Date.parse(raw);
  if (Number.isNaN(ts)) return false;
  return now.getTime() >= ts;
}

/**
 * Rebuild an actionable offer for a resumed conversation.
 *
 * `pending` is the server's projection — its presence is the authority that
 * the proposal is still open, and its `from`/`to` are authoritative over
 * anything stale in local custody. Returns null unless every condition holds:
 * the server still reports it open, this client holds a nonce, that nonce was
 * minted for the SAME route (a stored offer for a superseded proposal must not
 * be redeemed against a newer one), and the window has not closed.
 */
export function recallOffer(
  conversationId: string,
  pending: PendingHandoff | null | undefined,
  now: Date,
): HandoffOffer | null {
  if (!conversationId || !pending?.to) return null;
  const raw = readRaw(storageKey(conversationId));
  if (!raw) return null;
  let stored: Partial<HandoffOffer>;
  try {
    stored = JSON.parse(raw) as Partial<HandoffOffer>;
  } catch {
    forgetOffer(conversationId);
    return null;
  }
  if (typeof stored?.nonce !== 'string' || stored.nonce.length === 0) return null;
  // Route match. Without this a nonce held for an Explore→Provision proposal
  // could be posted against a later Explore→Patch one; the server would refuse
  // it (the digest wouldn't match), but the chip would have promised the wrong
  // crew in the meantime.
  if (stored.from !== pending.from || stored.to !== pending.to) {
    forgetOffer(conversationId);
    return null;
  }
  const offer: HandoffOffer = {
    from: pending.from,
    to: pending.to,
    // Prefer the server's copy: it is the one the crew actually committed.
    reason: pending.reason || stored.reason || '',
    nonce: stored.nonce,
    expires_at: pending.expires_at || stored.expires_at || '',
  };
  if (isOfferExpired(offer, now)) {
    forgetOffer(conversationId);
    return null;
  }
  return offer;
}
