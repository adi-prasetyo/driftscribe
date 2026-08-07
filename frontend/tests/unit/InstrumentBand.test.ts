import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import InstrumentBand from '../../src/components/InstrumentBand.svelte';

// InstrumentBand is the desk's pulse: three big numbers (managed / drift /
// awaiting) plus a proportional flex meter (docs/plans/2026-07-28-composite-
// mockup.html "instrument band"). It computes nothing — the consumer feeds it
// scopeTotals()-derived numbers via props; here we only exercise rendering,
// the per-stat interactivity contract, and the meter's flex proportions.
//
// ds-7ag.2 replaced the old "every stat is a button that goes to the estate"
// contract. The band no longer decides destinations at all: it emits
// `onStat(stat)` and the CONSUMER routes it.
//
// The desk+estate merge (2026-07-31) then removed the `context` prop entirely.
// There is one page now, so there is one routing table: managed/drift are
// controls that name where they lead, awaiting is always a figure. The cases
// that used to run twice — once per context — collapse to one each.

afterEach(cleanup);

function props(over: Record<string, unknown> = {}) {
  return {
    managed: 9,
    drift: 6,
    awaiting: 1,
    onStat: vi.fn(),
    ...over,
  };
}

describe('InstrumentBand', () => {
  it('renders all three numbers', () => {
    const { getByText } = render(InstrumentBand, { props: props() });
    expect(getByText('9')).toBeTruthy();
    expect(getByText('6')).toBeTruthy();
    expect(getByText('1')).toBeTruthy();
  });

  it('a real 0 renders as "0", not falsy-hidden (classic {#if}/&& bug)', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: 0, drift: 0, awaiting: 0 }),
    });
    expect(getByTestId('instrument-band-managed').textContent).toContain('0');
    expect(getByTestId('instrument-band-drift').textContent).toContain('0');
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('0');
  });

  it('meter segments carry flex proportional to managed/drift', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    const managedSeg = getByTestId('instrument-band-meter-managed') as HTMLElement;
    const driftSeg = getByTestId('instrument-band-meter-drift') as HTMLElement;
    expect(managedSeg.style.flexGrow).toBe('9');
    expect(driftSeg.style.flexGrow).toBe('6');
  });

  it('both managed and drift at 0 renders a bare track, not a misleading full/half bar', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: 0, drift: 0, awaiting: 0 }),
    });
    const managedSeg = getByTestId('instrument-band-meter-managed') as HTMLElement;
    const driftSeg = getByTestId('instrument-band-meter-drift') as HTMLElement;
    // flex: 0 on both collapses each segment to zero width (flex-basis 0%,
    // flex-grow 0, no content) — the track's own background shows through as
    // a bare line rather than either color painting a full-width bar.
    expect(managedSeg.style.flexGrow).toBe('0');
    expect(driftSeg.style.flexGrow).toBe('0');
  });

  it('a large managed count still yields a correctly proportional meter', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: 9000, drift: 1, awaiting: 0 }),
    });
    const managedSeg = getByTestId('instrument-band-meter-managed') as HTMLElement;
    const driftSeg = getByTestId('instrument-band-meter-drift') as HTMLElement;
    expect(managedSeg.style.flexGrow).toBe('9000');
    expect(driftSeg.style.flexGrow).toBe('1');
  });

  it('numerals use tabular-nums so digits do not jitter on tick (class hook present)', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    // The numeral element carries a dedicated class the component's scoped
    // CSS pins to font-variant-numeric: tabular-nums (see the .svelte file).
    const numeral = getByTestId('instrument-band-managed').querySelector('.instrument-band__num');
    expect(numeral).toBeTruthy();
  });
});

// ds-7ag.2 — per-stat routing. Before this every numeral called
// onNavigate('estate'), including あなたの承認待ち, whose content (the approval
// queue) is on the DESK. Clicking the number that says "you have work" walked
// away from the work.
describe('InstrumentBand — per-stat interactivity', () => {
  it('managed and drift are buttons that emit their own key', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, { props: props({ onStat }) });

    for (const key of ['managed', 'drift']) {
      expect(getByTestId(`instrument-band-${key}`).tagName).toBe('BUTTON');
      await fireEvent.click(getByTestId(`instrument-band-${key}`));
    }
    expect(onStat.mock.calls.map(([k]) => k)).toEqual(['managed', 'drift']);
  });

  // ds-s61 — awaiting is a FIGURE on the desk, not a control. ds-7ag.2 pointed
  // it at the desk's own pending card via scrollIntoView, but that card sits
  // ~270px below the numeral on the same screen: the jump had nowhere to go and
  // only spent the dead scroll a stale viewport calc left behind, which read as
  // an unexplained twitch. A number whose subject is directly beneath it does
  // not need to be clickable.
  it('awaiting is an inert figure — its subject is already on screen', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, { props: props({ onStat }) });

    const el = getByTestId('instrument-band-awaiting');
    expect(el.tagName).not.toBe('BUTTON');
    await fireEvent.click(el);
    expect(onStat).not.toHaveBeenCalled();
  });

  // The hover/focus hint is the VISIBLE promise that a click goes somewhere. An
  // inert figure must not carry one, or the band advertises a control it isn't.
  it('awaiting shows no destination hint', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    expect(getByTestId('instrument-band-awaiting').textContent).not.toMatch(/queue|↓|→/);
  });

  // awaiting's inertness no longer depends on its VALUE either — the merge
  // removed the one context where it led somewhere, so there is nothing left
  // for a count to enable. Both value cases stay pinned anyway: a future edit
  // that reintroduces a value-gated jump has to fail here first.
  for (const [name, awaiting] of [
    ['0 (nothing pending)', 0],
    ['null (not yet known)', null],
  ] as const) {
    it(`awaiting at ${name} is inert too`, async () => {
      const onStat = vi.fn();
      const { getByTestId } = render(InstrumentBand, {
        props: props({ awaiting, onStat }),
      });
      const el = getByTestId('instrument-band-awaiting');
      expect(el.tagName).not.toBe('BUTTON');
      await fireEvent.click(el);
      expect(onStat).not.toHaveBeenCalled();
    });
  }

  // An inert stat renders as a <span role="img">, NEVER a disabled <button>: a
  // disabled button drops out of keyboard navigation and helps nobody. The role
  // is what makes the aria-label replace the descendant text, exactly as a
  // button's would.
  it('the inert figure is a span with role="img", not a disabled button', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    const el = getByTestId('instrument-band-awaiting');
    expect(el.tagName).toBe('SPAN');
    expect(el.getAttribute('role')).toBe('img');
    expect(el.hasAttribute('disabled')).toBe(false);
  });
});

// A button's aria-label overrides ALL of its descendant text, so the visible
// hover hint (ds-7ag.2 / plan Task 3) is invisible to screen readers unless the
// accessible name carries the destination itself.
describe('InstrumentBand — accessible names name their destination', () => {
  it('managed/drift say where they go; the inert awaiting figure promises nothing', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    // Asserted as EXACT strings, not a loose /9/ match: these are
    // operator-facing copy under a freeze, and a substring match would still
    // pass if the label degraded to the bare numeral or the {n} placeholder
    // leaked through uninterpolated.
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      '9 declared in IaC — view infrastructure map',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      '6 drift detected — view infrastructure map',
    );
    // Plain wording, no destination clause: an aria-label that named one would
    // promise a screen-reader user a jump that no longer exists (ds-s61).
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      '1 needing your decision',
    );
  });

  // No stat names the estate as a place you NAVIGATE to any more — it is a
  // section of this same page, so the two destination clauses that spoke of
  // crossing between views ("view on desk") are gone with their keys. This is
  // the assertion that would catch one being reinstated by a stale catalog.
  it('no accessible name promises a trip to another view', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    for (const id of ['managed', 'drift', 'awaiting']) {
      expect(getByTestId(`instrument-band-${id}`).getAttribute('aria-label')).not.toMatch(
        /view on desk/i,
      );
    }
  });

  it('an inert awaiting stat keeps the plain label (it promises no destination)', () => {
    const { getByTestId } = render(InstrumentBand, { props: props({ awaiting: 0 }) });
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      '0 needing your decision',
    );
  });
});

// ds-eh6 — a figure the consumer has not established yet arrives as `null`.
// Before this the store's pre-fetch snapshot (graph:null, approvals:[]) came
// through as three zeros, so a cold desk confidently announced an estate that
// nothing had looked at.
describe('InstrumentBand — unknown figures (ds-eh6)', () => {
  it('renders a placeholder, not 0, for a null figure', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null }),
    });
    for (const id of ['managed', 'drift', 'awaiting']) {
      const el = getByTestId(`instrument-band-${id}`);
      expect(el.textContent).toContain('—');
      expect(el.textContent).not.toContain('0');
      expect(el.getAttribute('data-unknown')).toBe('true');
    }
  });

  it('a known figure carries no unknown marker', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: 0, drift: 0, awaiting: 0 }),
    });
    for (const id of ['managed', 'drift', 'awaiting']) {
      expect(getByTestId(`instrument-band-${id}`).getAttribute('data-unknown')).toBeNull();
    }
  });

  it('the accessible name says "not yet known" instead of reading a bare dash', () => {
    // An em dash is announced as nothing at all, so a screen-reader user would
    // otherwise hear only the label and infer zero.
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null }),
    });
    // managed/drift stay CLICKABLE while unknown, so their name must carry the
    // destination too — the visible hint is aria-hidden, and a button that never
    // says what it opens is the gap that leaves (Codex review).
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      'Declared in IaC: not yet known — view infrastructure map',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      'Drift detected: not yet known — view infrastructure map',
    );
    // awaiting is inert whenever it is unknown, so it promises nothing.
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      'Needs your decision: not yet known',
    );
  });

  // The inert-and-unknown pairing used to be exercised on the estate, where
  // managed/drift led nowhere. awaiting is the only inert stat left, and its
  // unknown wording is asserted above — there is no second case to run.

  it('null collapses the meter to the bare "no data" track', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null }),
    });
    expect(getByTestId('instrument-band-meter-managed').getAttribute('style')).toContain('flex: 0');
    expect(getByTestId('instrument-band-meter-drift').getAttribute('style')).toContain('flex: 0');
  });

  it('an unknown managed/drift stat is still clickable (the map is where you go to find out)', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null, onStat }),
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    expect(onStat).toHaveBeenCalledTimes(1);
    expect(onStat).toHaveBeenCalledWith('managed');
  });
});

// ── ds-wd2.13: the numeral tick ───────────────────────────────────────────
// The mockup's `pop` keyframe fires when a band numeral changes — the demo's
// spine (drift 6→7, awaiting 0→1 as drift lands; both falling back on approve).
// What is asserted here is the CHANGE DETECTION, not the animation: the pop is
// scoped CSS, and jsdom neither lays out nor animates. `data-pop` is the count
// of qualifying changes that numeral has seen, and it is also what keys the
// {#key} block that restarts the animation — so a correct counter IS a correct
// number of animation restarts.
//
// `null` means NOT YET KNOWN (ds-eh6), which is why two of these cases exist:
// the first real reading landing after mount is the page loading, not news, and
// a value regressing to unknown must not pop an em dash.

function numeral(el: HTMLElement): HTMLElement {
  const n = el.querySelector('.instrument-band__num');
  if (!n) throw new Error('numeral span not found');
  return n as HTMLElement;
}

function popCount(getByTestId: (id: string) => HTMLElement, stat: string): string | null {
  return numeral(getByTestId(`instrument-band-${stat}`)).getAttribute('data-pop');
}

describe('InstrumentBand — numeral pop (ds-wd2.13)', () => {
  it('does not pop on first mount', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    expect(popCount(getByTestId, 'managed')).toBe('0');
    expect(popCount(getByTestId, 'drift')).toBe('0');
    expect(popCount(getByTestId, 'awaiting')).toBe('0');
  });

  it('does not pop when the first KNOWN value arrives after unknown', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null }),
    });
    await rerender(props({ managed: 9, drift: 6, awaiting: 0 }));
    expect(popCount(getByTestId, 'managed')).toBe('0');
    expect(popCount(getByTestId, 'drift')).toBe('0');
    expect(popCount(getByTestId, 'awaiting')).toBe('0');
  });

  it('pops the numeral whose known value changed', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 6, awaiting: 0 }),
    });
    await rerender(props({ managed: 9, drift: 7, awaiting: 1 }));
    expect(popCount(getByTestId, 'drift')).toBe('1');
    expect(popCount(getByTestId, 'awaiting')).toBe('1');
  });

  it('leaves an UNCHANGED numeral alone (only the news pops)', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 6, awaiting: 0 }),
    });
    await rerender(props({ managed: 9, drift: 7, awaiting: 1 }));
    expect(popCount(getByTestId, 'managed')).toBe('0');
  });

  it('pops on the way back down too (the approve beat)', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 6, awaiting: 0 }),
    });
    await rerender(props({ managed: 9, drift: 7, awaiting: 1 }));
    await rerender(props({ managed: 9, drift: 6, awaiting: 0 }));
    expect(popCount(getByTestId, 'drift')).toBe('2');
    expect(popCount(getByTestId, 'awaiting')).toBe('2');
  });

  it('a repeated poll delivering identical numbers does not pop', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 6, awaiting: 0 }),
    });
    await rerender(props({ managed: 9, drift: 6, awaiting: 0 }));
    await rerender(props({ managed: 9, drift: 6, awaiting: 0 }));
    expect(popCount(getByTestId, 'drift')).toBe('0');
  });

  it('does not pop when a known value regresses to unknown', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 7, awaiting: 1 }),
    });
    await rerender(props({ managed: null, drift: null, awaiting: null }));
    expect(popCount(getByTestId, 'drift')).toBe('0');
    expect(numeral(getByTestId('instrument-band-drift')).textContent).toBe('—');
  });

  it('carries the pop class only once it has actually popped', async () => {
    const { getByTestId, rerender } = render(InstrumentBand, {
      props: props({ managed: 9, drift: 6, awaiting: 0 }),
    });
    expect(numeral(getByTestId('instrument-band-drift')).className).not.toContain('--pop');
    await rerender(props({ managed: 9, drift: 7, awaiting: 0 }));
    expect(numeral(getByTestId('instrument-band-drift')).className).toContain('--pop');
  });
});
