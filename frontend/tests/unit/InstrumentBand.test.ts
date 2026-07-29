import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import InstrumentBand from '../../src/components/InstrumentBand.svelte';

// InstrumentBand is the desk/estate pulse: three big numbers (managed / drift
// / awaiting) plus a proportional flex meter (docs/plans/2026-07-28-composite-
// mockup.html "instrument band"). It computes nothing — the consumer feeds it
// scopeTotals()-derived numbers via props; here we only exercise rendering,
// the button-per-stat click contract, and the meter's flex proportions.

afterEach(cleanup);

describe('InstrumentBand', () => {
  it('renders all three numbers', () => {
    const { getByText } = render(InstrumentBand, {
      props: { managed: 9, drift: 6, awaiting: 1, onNavigate: vi.fn() },
    });
    expect(getByText('9')).toBeTruthy();
    expect(getByText('6')).toBeTruthy();
    expect(getByText('1')).toBeTruthy();
  });

  it('a real 0 renders as "0", not falsy-hidden (classic {#if}/&& bug)', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 0, drift: 0, awaiting: 0, onNavigate: vi.fn() },
    });
    expect(getByTestId('instrument-band-managed').textContent).toContain('0');
    expect(getByTestId('instrument-band-drift').textContent).toContain('0');
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('0');
  });

  it('each stat is a <button> with an accessible name that includes its number', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 9, drift: 6, awaiting: 1, onNavigate: vi.fn() },
    });
    const managed = getByTestId('instrument-band-managed');
    const drift = getByTestId('instrument-band-drift');
    const awaiting = getByTestId('instrument-band-awaiting');
    expect(managed.tagName).toBe('BUTTON');
    expect(drift.tagName).toBe('BUTTON');
    expect(awaiting.tagName).toBe('BUTTON');
    // A bare number is a useless accessible name read aloud — each button's
    // name must carry both the figure and what it means. Asserted as EXACT
    // strings, not a loose /9/ match: these are operator-facing copy under a
    // freeze, and a substring match would still pass if the label degraded to
    // the bare numeral or the {n} placeholder leaked through uninterpolated.
    expect(managed.getAttribute('aria-label')).toBe('9 managed by IaC');
    expect(drift.getAttribute('aria-label')).toBe('6 drift detected');
    expect(awaiting.getAttribute('aria-label')).toBe('1 awaiting your approval');
  });

  it('clicking any stat fires onNavigate("estate")', async () => {
    const onNavigate = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 9, drift: 6, awaiting: 1, onNavigate },
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    await fireEvent.click(getByTestId('instrument-band-drift'));
    await fireEvent.click(getByTestId('instrument-band-awaiting'));
    expect(onNavigate).toHaveBeenCalledTimes(3);
    expect(onNavigate).toHaveBeenNthCalledWith(1, 'estate');
    expect(onNavigate).toHaveBeenNthCalledWith(2, 'estate');
    expect(onNavigate).toHaveBeenNthCalledWith(3, 'estate');
  });

  it('meter segments carry flex proportional to managed/drift', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 9, drift: 6, awaiting: 1, onNavigate: vi.fn() },
    });
    const managedSeg = getByTestId('instrument-band-meter-managed') as HTMLElement;
    const driftSeg = getByTestId('instrument-band-meter-drift') as HTMLElement;
    expect(managedSeg.style.flexGrow).toBe('9');
    expect(driftSeg.style.flexGrow).toBe('6');
  });

  it('both managed and drift at 0 renders a bare track, not a misleading full/half bar', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 0, drift: 0, awaiting: 0, onNavigate: vi.fn() },
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
      props: { managed: 9000, drift: 1, awaiting: 0, onNavigate: vi.fn() },
    });
    const managedSeg = getByTestId('instrument-band-meter-managed') as HTMLElement;
    const driftSeg = getByTestId('instrument-band-meter-drift') as HTMLElement;
    expect(managedSeg.style.flexGrow).toBe('9000');
    expect(driftSeg.style.flexGrow).toBe('1');
  });

  it('numerals use tabular-nums so digits do not jitter on tick (class hook present)', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: 9, drift: 6, awaiting: 1, onNavigate: vi.fn() },
    });
    // The numeral element carries a dedicated class the component's scoped
    // CSS pins to font-variant-numeric: tabular-nums (see the .svelte file).
    const numeral = getByTestId('instrument-band-managed').querySelector('.instrument-band__num');
    expect(numeral).toBeTruthy();
  });
});

// ds-eh6 — a figure the consumer has not established yet arrives as `null`.
// Before this the store's pre-fetch snapshot (graph:null, approvals:[]) came
// through as three zeros, so a cold desk confidently announced an estate that
// nothing had looked at.
describe('InstrumentBand — unknown figures (ds-eh6)', () => {
  it('renders a placeholder, not 0, for a null figure', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: null, drift: null, awaiting: null, onNavigate: vi.fn() },
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
      props: { managed: 0, drift: 0, awaiting: 0, onNavigate: vi.fn() },
    });
    for (const id of ['managed', 'drift', 'awaiting']) {
      expect(getByTestId(`instrument-band-${id}`).getAttribute('data-unknown')).toBeNull();
    }
  });

  it('the accessible name says "not yet known" instead of reading a bare dash', () => {
    // An em dash is announced as nothing at all, so a screen-reader user would
    // otherwise hear only the label and infer zero.
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: null, drift: null, awaiting: null, onNavigate: vi.fn() },
    });
    expect(getByTestId('instrument-band-managed').getAttribute('aria-label')).toBe(
      'Managed by IaC: not yet known',
    );
    expect(getByTestId('instrument-band-drift').getAttribute('aria-label')).toBe(
      'Drift detected: not yet known',
    );
    expect(getByTestId('instrument-band-awaiting').getAttribute('aria-label')).toBe(
      'Awaiting your approval: not yet known',
    );
  });

  it('null collapses the meter to the bare "no data" track', () => {
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: null, drift: null, awaiting: null, onNavigate: vi.fn() },
    });
    expect(getByTestId('instrument-band-meter-managed').getAttribute('style')).toContain('flex: 0');
    expect(getByTestId('instrument-band-meter-drift').getAttribute('style')).toContain('flex: 0');
  });

  it('stats stay clickable while unknown', () => {
    const onNavigate = vi.fn();
    const { getByTestId } = render(InstrumentBand, {
      props: { managed: null, drift: null, awaiting: null, onNavigate },
    });
    fireEvent.click(getByTestId('instrument-band-managed'));
    expect(onNavigate).toHaveBeenCalledWith('estate');
  });
});
