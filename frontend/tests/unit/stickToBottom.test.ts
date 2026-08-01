import { describe, it, expect } from 'vitest';
import {
  distanceFromBottom,
  shouldFollow,
  STICK_THRESHOLD_PX,
  type ScrollGeometry,
} from '../../src/lib/stickToBottom';

// The arm/disarm rule for the thread's follow-the-newest behaviour (ds-jns
// PR 3). Pure by design: jsdom has no layout, so every scroll property on a
// real element reads 0 — a rule expressed over an element could not be
// exercised here at all. The GEOMETRY (composer pinned, thread region scrolls,
// body does not) is pinned separately in a browser visual spec.

const g = (over: Partial<ScrollGeometry> = {}): ScrollGeometry => ({
  scrollTop: 0,
  scrollHeight: 1000,
  clientHeight: 400,
  ...over,
});

describe('distanceFromBottom', () => {
  it('is 0 when pinned to the bottom', () => {
    expect(distanceFromBottom(g({ scrollTop: 600 }))).toBe(0);
  });

  it('measures the gap when scrolled up', () => {
    expect(distanceFromBottom(g({ scrollTop: 350 }))).toBe(250);
  });

  it('clamps content SHORTER than its container to 0, not a negative', () => {
    // A brand-new thread with one turn: scrollHeight < clientHeight. The raw
    // subtraction is negative, which must not read as "somewhere other than the
    // bottom" — there is only one screen and the operator is on it.
    expect(distanceFromBottom(g({ scrollHeight: 120, clientHeight: 400 }))).toBe(0);
  });

  it('clamps rubber-band overscroll to 0', () => {
    // A touchpad flick drives scrollTop past the maximum for the length of the
    // elastic animation. Every frame of that is still pinned to the bottom.
    expect(distanceFromBottom(g({ scrollTop: 780 }))).toBe(0);
  });

  it('never returns NaN for a detached/garbage geometry', () => {
    expect(distanceFromBottom(g({ scrollHeight: NaN }))).toBe(0);
    expect(distanceFromBottom(g({ scrollTop: Infinity }))).toBe(0);
  });
});

describe('shouldFollow', () => {
  it('follows while the operator is at the bottom', () => {
    expect(shouldFollow(g({ scrollTop: 600 }))).toBe(true);
  });

  it('stops following once they scroll up past the threshold', () => {
    // The behaviour this whole module exists for: a crew turn streams for tens
    // of seconds, and an operator who scrolled up to re-read an earlier
    // decision must not be yanked back on every token.
    expect(shouldFollow(g({ scrollTop: 600 - STICK_THRESHOLD_PX - 1 }))).toBe(false);
  });

  it('treats exactly the threshold as still following (inclusive bound)', () => {
    // Pinning the boundary itself, not just a value either side of it: an
    // off-by-one here is the difference between a stray trackpad nudge
    // disarming the follow and it surviving.
    expect(shouldFollow(g({ scrollTop: 600 - STICK_THRESHOLD_PX }))).toBe(true);
    expect(distanceFromBottom(g({ scrollTop: 600 - STICK_THRESHOLD_PX }))).toBe(
      STICK_THRESHOLD_PX,
    );
  });

  it('re-arms on the way back down', () => {
    // Disarm and re-arm are one stateless question about where the operator is,
    // so returning to the bottom needs no separate "re-arm" path to go wrong.
    const scrolledUp = g({ scrollTop: 200 });
    expect(shouldFollow(scrolledUp)).toBe(false);
    expect(shouldFollow({ ...scrolledUp, scrollTop: 600 })).toBe(true);
  });

  it('follows a thread too short to scroll', () => {
    expect(shouldFollow(g({ scrollHeight: 120, clientHeight: 400 }))).toBe(true);
  });

  it('honours a caller-supplied threshold', () => {
    expect(shouldFollow(g({ scrollTop: 500 }), 200)).toBe(true);
    expect(shouldFollow(g({ scrollTop: 500 }), 50)).toBe(false);
  });

  it('fails toward FOLLOWING on a garbage geometry', () => {
    // Following when we should not is a viewport nudge. Not following when we
    // should is a live run that appears to stop updating — which reads as the
    // agent having hung, the one impression this product cannot afford.
    expect(shouldFollow(g({ scrollHeight: NaN }))).toBe(true);
  });
});
