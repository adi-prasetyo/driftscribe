import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import CoverageMeter from '../../src/components/CoverageMeter.svelte';

afterEach(cleanup);

const totals = (managed: number, resources: number, drift: number) => ({
  managed,
  resources,
  drift,
});

describe('CoverageMeter', () => {
  it('renders headline, progressbar and detail line', () => {
    const { getByTestId, getByRole } = render(CoverageMeter, {
      props: { totals: totals(13, 50, 37) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('26%');
    expect(getByTestId('coverage-meter').textContent).toContain(
      'of your infrastructure is under IaC management',
    );
    const bar = getByRole('progressbar');
    expect(bar.getAttribute('aria-valuenow')).toBe('26');
    expect(bar.getAttribute('aria-valuemin')).toBe('0');
    expect(bar.getAttribute('aria-valuemax')).toBe('100');
    expect(bar.getAttribute('aria-valuetext')).toBe('26%, 13 of 50 resources managed');
    expect(getByTestId('coverage-detail').textContent).toContain(
      '13 of 50 resources managed · 37 not yet in IaC',
    );
  });

  it('renders the fill at the percentage width', () => {
    const { getByTestId } = render(CoverageMeter, {
      props: { totals: totals(13, 50, 37) },
    });
    const fill = getByTestId('coverage-fill') as HTMLElement;
    expect(fill.style.width).toBe('26%');
  });

  it('renders nothing when totals is null', () => {
    const { queryByTestId } = render(CoverageMeter, { props: { totals: null } });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('renders nothing for a zero-resource estate', () => {
    const { queryByTestId } = render(CoverageMeter, {
      props: { totals: totals(0, 0, 0) },
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('omits the "not yet in IaC" segment at 100%', () => {
    const { getByTestId } = render(CoverageMeter, {
      props: { totals: totals(7, 7, 0) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('100%');
    expect(getByTestId('coverage-detail').textContent).toContain('7 of 7 resources managed');
    expect(getByTestId('coverage-detail').textContent).not.toContain('not yet in IaC');
  });

  it('shows an honest 0% when nothing is managed yet', () => {
    const { getByTestId, getByRole } = render(CoverageMeter, {
      props: { totals: totals(0, 12, 12) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('0%');
    expect(getByRole('progressbar').getAttribute('aria-valuenow')).toBe('0');
    const fill = getByTestId('coverage-fill') as HTMLElement;
    expect(fill.style.width).toBe('0%');
  });
});
