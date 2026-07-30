import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import InstrumentBand from '../../src/components/InstrumentBand.svelte';

// InstrumentBand is the desk/estate pulse: three big numbers (managed / drift
// / awaiting) plus a proportional flex meter (docs/plans/2026-07-28-composite-
// mockup.html "instrument band"). It computes nothing — the consumer feeds it
// scopeTotals()-derived numbers via props; here we only exercise rendering,
// the per-stat interactivity contract, and the meter's flex proportions.
//
// ds-7ag.2 replaced the old "every stat is a button that goes to the estate"
// contract. The band no longer decides destinations at all: it emits
// `onStat(stat)` and the CONSUMER routes it (awaiting belongs on the desk, not
// the estate). `context` is what a callback alone cannot express — which stats
// are interactive at all on this consumer, and how their accessible names read.

afterEach(cleanup);

function props(over: Record<string, unknown> = {}) {
  return {
    managed: 9,
    drift: 6,
    awaiting: 1,
    context: 'desk' as const,
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
describe('InstrumentBand — per-stat interactivity by context', () => {
  it('on the desk, managed and drift are buttons that emit their own key', async () => {
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
  it('on the desk, awaiting is an inert figure — its subject is already on screen', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, { props: props({ onStat }) });

    const el = getByTestId('instrument-band-awaiting');
    expect(el.tagName).not.toBe('BUTTON');
    await fireEvent.click(el);
    expect(onStat).not.toHaveBeenCalled();
  });

  // The hover/focus hint is the VISIBLE promise that a click goes somewhere. An
  // inert figure must not carry one, or the band advertises a control it isn't.
  it('on the desk, awaiting shows no destination hint', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    expect(getByTestId('instrument-band-awaiting').textContent).not.toMatch(/queue|↓|→/);
  });

  // On the estate view the operator is already looking at the infrastructure
  // map, so managed/drift have nowhere to send them. They become figures. A
  // <span>, NOT a disabled <button>: a disabled button drops out of keyboard
  // navigation and helps nobody.
  it('on the estate view, managed and drift are inert figures, not buttons', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: props({ context: 'estate', onStat }),
    });

    for (const key of ['managed', 'drift']) {
      const el = getByTestId(`instrument-band-${key}`);
      expect(el.tagName).not.toBe('BUTTON');
      await fireEvent.click(el);
    }
    expect(onStat).not.toHaveBeenCalled();
  });

  it('on the estate view, awaiting stays a button (the desk is where its work lives)', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: props({ context: 'estate', onStat }),
    });
    const awaiting = getByTestId('instrument-band-awaiting');
    expect(awaiting.tagName).toBe('BUTTON');
    await fireEvent.click(awaiting);
    expect(onStat).toHaveBeenCalledTimes(1);
    expect(onStat).toHaveBeenCalledWith('awaiting');
  });

  // Nothing pending means there is nothing to land on, in either context — a
  // control that goes nowhere is worse than a figure.
  for (const context of ['desk', 'estate'] as const) {
    it(`awaiting at 0 is inert on the ${context} (nothing to jump to)`, async () => {
      const onStat = vi.fn();
      const { getByTestId } = render(InstrumentBand, {
        props: props({ context, awaiting: 0, onStat }),
      });
      const awaiting = getByTestId('instrument-band-awaiting');
      expect(awaiting.tagName).not.toBe('BUTTON');
      await fireEvent.click(awaiting);
      expect(onStat).not.toHaveBeenCalled();
    });

    it(`awaiting at null (not yet known) is inert on the ${context}`, async () => {
      const onStat = vi.fn();
      const { getByTestId } = render(InstrumentBand, {
        props: props({ context, awaiting: null, onStat }),
      });
      const awaiting = getByTestId('instrument-band-awaiting');
      expect(awaiting.tagName).not.toBe('BUTTON');
      await fireEvent.click(awaiting);
      expect(onStat).not.toHaveBeenCalled();
    });
  }
});

// A button's aria-label overrides ALL of its descendant text, so the visible
// hover hint (ds-7ag.2 / plan Task 3) is invisible to screen readers unless the
// accessible name carries the destination itself.
describe('InstrumentBand — accessible names name their destination', () => {
  it('desk: managed/drift say where they go; the inert awaiting figure promises nothing', () => {
    const { getByTestId } = render(InstrumentBand, { props: props() });
    // Asserted as EXACT strings, not a loose /9/ match: these are
    // operator-facing copy under a freeze, and a substring match would still
    // pass if the label degraded to the bare numeral or the {n} placeholder
    // leaked through uninterpolated.
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      '9 managed by IaC — view infrastructure map',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      '6 drift detected — view infrastructure map',
    );
    // Plain wording, no destination clause: an aria-label that named one would
    // promise a screen-reader user a jump that no longer exists (ds-s61).
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      '1 awaiting your approval',
    );
  });

  it('estate: awaiting points back at the desk; the inert figures keep plain labels', () => {
    const { getByTestId } = render(InstrumentBand, { props: props({ context: 'estate' }) });
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      '1 awaiting your approval — view on desk',
    );
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      '9 managed by IaC',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe('6 drift detected');
  });

  it('an inert awaiting stat keeps the plain label (it promises no destination)', () => {
    const { getByTestId } = render(InstrumentBand, { props: props({ awaiting: 0 }) });
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      '0 awaiting your approval',
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
      'Managed by IaC: not yet known — view infrastructure map',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      'Drift detected: not yet known — view infrastructure map',
    );
    // awaiting is inert whenever it is unknown, so it promises nothing.
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      'Awaiting your approval: not yet known',
    );
  });

  it('an unknown figure that is INERT keeps the plain unknown wording', () => {
    // Same null figures on the estate, where managed/drift lead nowhere.
    const { getByTestId } = render(InstrumentBand, {
      props: props({ context: 'estate', managed: null, drift: null, awaiting: null }),
    });
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      'Managed by IaC: not yet known',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      'Drift detected: not yet known',
    );
  });

  it('null collapses the meter to the bare "no data" track', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null }),
    });
    expect(getByTestId('instrument-band-meter-managed').getAttribute('style')).toContain('flex: 0');
    expect(getByTestId('instrument-band-meter-drift').getAttribute('style')).toContain('flex: 0');
  });

  it('an unknown managed/drift stat is still clickable on the desk (the map is where you go to find out)', async () => {
    const onStat = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: props({ managed: null, drift: null, awaiting: null, onStat }),
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    expect(onStat).toHaveBeenCalledTimes(1);
    expect(onStat).toHaveBeenCalledWith('managed');
  });
});
