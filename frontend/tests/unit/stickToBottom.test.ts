import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  distanceFromBottom,
  shouldFollow,
  stickToBottom,
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

// ── The action's wiring ──────────────────────────────────────────────────────
// The rule above is pure and easy; what is actually load-bearing is WHEN the
// action consults it. jsdom reports every scroll property as 0, so a bare
// element can only ever sit at the bottom — these tests shadow the three
// getters with an own-property view onto a mutable struct, which is enough to
// drive the arm/disarm state machine without a layout engine. The follow itself
// is observed through a spy on scrollTo (setup.ts stubs it; jsdom has no
// Element.scrollTo), never through a scroll POSITION, which stays 0 regardless.

interface Region {
  el: HTMLElement;
  geo: ScrollGeometry;
  scrollTo: ReturnType<typeof vi.spyOn>;
}

function region(over: Partial<ScrollGeometry> = {}): Region {
  const geo = g(over);
  const el = document.createElement('div');
  for (const key of ['scrollTop', 'scrollHeight', 'clientHeight'] as const) {
    Object.defineProperty(el, key, {
      configurable: true,
      get: () => geo[key],
      set: (v: number) => {
        geo[key] = v;
      },
    });
  }
  document.body.appendChild(el);
  return { el, geo, scrollTo: vi.spyOn(el, 'scrollTo') };
}

/** Let jsdom deliver the queued MutationObserver records. */
const flush = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

/** Content arriving — a streamed token, a thought row, a tool call. */
function grow(r: Region, byPx = 200): void {
  r.geo.scrollHeight += byPx;
  r.el.appendChild(document.createElement('p'));
}

/** The operator dragging the scrollbar / spinning the wheel. */
function userScrollTo(r: Region, top: number): void {
  r.geo.scrollTop = top;
  r.el.dispatchEvent(new Event('scroll'));
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('stickToBottom action', () => {
  it('follows content that arrives while the operator is at the bottom', async () => {
    const r = region({ scrollTop: 600 });
    const handle = stickToBottom(r.el);
    grow(r);
    await flush();
    expect(r.scrollTo).toHaveBeenCalledTimes(1);
    handle.destroy();
  });

  it('leaves the viewport alone once the operator has scrolled up', async () => {
    // The behaviour the module exists for. A crew turn streams for tens of
    // seconds; an operator reading an earlier decision must not be dragged
    // back to the bottom on every token that lands.
    const r = region({ scrollTop: 600 });
    const handle = stickToBottom(r.el);
    userScrollTo(r, 200);
    grow(r);
    await flush();
    expect(r.scrollTo).not.toHaveBeenCalled();
    handle.destroy();
  });

  it('does NOT let content growth disarm the follow', async () => {
    // The subtle one, and the reason the arm/disarm rule is consulted on user
    // scrolls ONLY. Growth raises scrollHeight before the follow scroll lands,
    // so an observer that re-read the geometry would measure a distance it had
    // just created itself, conclude the operator had scrolled up, and disarm —
    // permanently, after the very first chunk of a streaming reply.
    const r = region({ scrollTop: 600 });
    const handle = stickToBottom(r.el);
    for (let i = 0; i < 3; i++) {
      grow(r, 500);
      await flush();
    }
    expect(r.scrollTo).toHaveBeenCalledTimes(3);
    handle.destroy();
  });

  it('re-arms when the operator scrolls back down', async () => {
    const r = region({ scrollTop: 600 });
    const handle = stickToBottom(r.el);
    userScrollTo(r, 200);
    grow(r);
    await flush();
    expect(r.scrollTo).not.toHaveBeenCalled();

    userScrollTo(r, r.geo.scrollHeight - r.geo.clientHeight);
    grow(r);
    await flush();
    expect(r.scrollTo).toHaveBeenCalledTimes(1);
    handle.destroy();
  });

  it('follows content nested deep inside a turn, not just new turns', async () => {
    // subtree/characterData, not just childList: a streaming reply grows an
    // EXISTING turn's text node. A childList-only watch on the region would
    // see nothing at all for the whole run.
    const r = region({ scrollTop: 600 });
    const turn = document.createElement('div');
    const text = document.createTextNode('half a sen');
    turn.appendChild(text);
    r.el.appendChild(turn);
    const handle = stickToBottom(r.el);

    text.data = 'half a sentence more';
    await flush();
    expect(r.scrollTo).toHaveBeenCalledTimes(1);
    handle.destroy();
  });

  it('stops observing and listening once destroyed', async () => {
    const r = region({ scrollTop: 600 });
    stickToBottom(r.el).destroy();
    grow(r);
    await flush();
    expect(r.scrollTo).not.toHaveBeenCalled();
    // And the scroll listener is gone too: a post-destroy scroll must not be
    // able to leave state behind that a later observer could act on.
    userScrollTo(r, 0);
    grow(r);
    await flush();
    expect(r.scrollTo).not.toHaveBeenCalled();
  });
});
