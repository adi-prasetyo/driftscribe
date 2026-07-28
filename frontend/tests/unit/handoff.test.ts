// Client-side custody of a crew-handoff nonce (lib/handoff.ts).
//
// The thing under test is an asymmetry, not a data structure: the server can
// always say whether a proposal is OPEN (it persists one), but it can never
// re-serve the nonce that redeems it (it stores only a digest). So a chip needs
// the server's word AND local custody, and every test here is really asking
// "which half is missing, and does the code fail closed?".
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  rememberOffer,
  forgetOffer,
  recallOffer,
  isOfferExpired,
  readHandoffOffer,
} from '../../src/lib/handoff';
import type { HandoffOffer } from '../../src/lib/sse';
import type { PendingHandoff } from '../../src/lib/types';

const NOW = new Date('2026-07-28T12:00:00Z');
const LATER = '2026-07-28T12:15:00Z'; // +15m, the real HANDOFF_TTL_MINUTES
const EARLIER = '2026-07-28T11:45:00Z';

function offer(over: Partial<HandoffOffer> = {}): HandoffOffer {
  return {
    from: 'explore',
    to: 'provision',
    reason: 'the operator wants a bucket created',
    nonce: 'n0nc3',
    expires_at: LATER,
    ...over,
  };
}

function pending(over: Partial<PendingHandoff> = {}): PendingHandoff {
  return {
    from: 'explore',
    to: 'provision',
    reason: 'the operator wants a bucket created',
    expires_at: LATER,
    ...over,
  };
}

beforeEach(() => window.sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe('readHandoffOffer — same validation for both transports', () => {
  it('accepts a complete offer', () => {
    expect(readHandoffOffer(offer())).toEqual(offer());
  });

  it.each([
    ['no nonce', { ...offer(), nonce: '' }],
    ['no target crew', { ...offer(), to: '' }],
    ['no source crew', { ...offer(), from: '' }],
    ['not an object', 'explore->provision'],
    ['null', null],
  ])('rejects %s — a partial offer would render a button that cannot work', (_label, raw) => {
    expect(readHandoffOffer(raw)).toBeUndefined();
  });

  it('allows an empty reason: a crew may propose without justifying at length', () => {
    expect(readHandoffOffer({ ...offer(), reason: '' })?.nonce).toBe('n0nc3');
  });

  it('coerces non-string fields away rather than passing them through', () => {
    // The JSON transport is untyped; a number where a crew name belongs must
    // not reach crewName() or the POST body.
    expect(readHandoffOffer({ ...offer(), to: 7 })).toBeUndefined();
    expect(readHandoffOffer({ ...offer(), reason: 7 })?.reason).toBe('');
  });
});

describe('isOfferExpired', () => {
  it('is expired once the window has passed', () => {
    expect(isOfferExpired({ expires_at: EARLIER }, NOW)).toBe(true);
  });

  it('is live before the window closes', () => {
    expect(isOfferExpired({ expires_at: LATER }, NOW)).toBe(false);
  });

  it('fails OPEN on an unreadable date — the server owns expiry, not our clock', () => {
    // A skewed client clock or a garbled timestamp must not hide an offer that
    // still works. If it really has expired the redemption answers 410 and the
    // chip says so.
    expect(isOfferExpired({ expires_at: 'not a date' }, NOW)).toBe(false);
    expect(isOfferExpired({ expires_at: null }, NOW)).toBe(false);
    expect(isOfferExpired({}, NOW)).toBe(false);
  });

  it('treats a missing offer as expired', () => {
    expect(isOfferExpired(null, NOW)).toBe(true);
  });
});

describe('recallOffer — both halves required', () => {
  it('rebuilds an actionable offer when the server still reports it open', () => {
    rememberOffer('c1', offer());
    expect(recallOffer('c1', pending(), NOW)).toEqual(offer());
  });

  it('returns null with custody but NO server-side proposal', () => {
    // The proposal was burned elsewhere (redeemed in another tab, superseded).
    // A stored nonce is not evidence that anything is still awaiting an answer.
    rememberOffer('c1', offer());
    expect(recallOffer('c1', null, NOW)).toBeNull();
  });

  it('returns null with a server-side proposal but NO custody', () => {
    // This is the cross-device case, and it is the whole reason the chip is
    // allowed to be absent: the nonce is a capability, not a view, so a shared
    // ?conversation= link cannot carry it.
    expect(recallOffer('c1', pending(), NOW)).toBeNull();
  });

  it('does not leak custody across conversations', () => {
    rememberOffer('c1', offer());
    expect(recallOffer('c2', pending(), NOW)).toBeNull();
  });

  it('refuses a stored nonce minted for a DIFFERENT route, and drops it', () => {
    // Explore→Patch superseded by Explore→Provision: the server would refuse
    // the stale digest anyway, but not before the chip had promised the wrong
    // crew on its button.
    rememberOffer('c1', offer({ to: 'upgrade' }));
    expect(recallOffer('c1', pending({ to: 'provision' }), NOW)).toBeNull();
    // And the mismatched nonce is gone, not left to fail again.
    expect(recallOffer('c1', pending({ to: 'upgrade' }), NOW)).toBeNull();
  });

  it('drops an expired offer instead of showing a doomed button', () => {
    rememberOffer('c1', offer({ expires_at: EARLIER }));
    expect(recallOffer('c1', pending({ expires_at: EARLIER }), NOW)).toBeNull();
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
  });

  it('prefers the SERVER copy of from/to/reason over anything stale in storage', () => {
    rememberOffer('c1', offer({ reason: 'stale local text' }));
    const got = recallOffer('c1', pending({ reason: 'what the crew committed' }), NOW);
    expect(got?.reason).toBe('what the crew committed');
  });

  it('survives corrupt storage by discarding it', () => {
    window.sessionStorage.setItem('ds.handoff.c1', '{not json');
    expect(recallOffer('c1', pending(), NOW)).toBeNull();
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
  });

  it('forgetOffer revokes custody', () => {
    rememberOffer('c1', offer());
    forgetOffer('c1');
    expect(recallOffer('c1', pending(), NOW)).toBeNull();
  });

  it('a fresh proposal overwrites the previous one, mirroring server supersession', () => {
    rememberOffer('c1', offer({ to: 'upgrade', nonce: 'old' }));
    rememberOffer('c1', offer({ to: 'provision', nonce: 'new' }));
    expect(recallOffer('c1', pending({ to: 'provision' }), NOW)?.nonce).toBe('new');
  });
});

describe('recallOffer — storage is unavailable', () => {
  it('degrades to "no chip" instead of throwing', () => {
    // Private-browsing modes throw on sessionStorage access. Losing the chip is
    // an acceptable outcome; taking the chat view down with it is not.
    const boom = () => {
      throw new Error('SecurityError');
    };
    vi.stubGlobal('sessionStorage', {
      getItem: boom,
      setItem: boom,
      removeItem: boom,
    });
    expect(() => rememberOffer('c1', offer())).not.toThrow();
    expect(recallOffer('c1', pending(), NOW)).toBeNull();
    expect(() => forgetOffer('c1')).not.toThrow();
  });
});
