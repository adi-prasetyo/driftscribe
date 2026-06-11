// frontend/tests/unit/TourBanner.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import TourBanner from '../../src/components/TourBanner.svelte';

afterEach(cleanup);

describe('TourBanner', () => {
  it('renders the offer copy', () => {
    const { getByTestId } = render(TourBanner, { props: {} });
    expect(getByTestId('tour-banner').textContent).toContain('5-minute tour');
  });

  it('Start fires onStart; Dismiss fires onDismiss', async () => {
    const onStart = vi.fn();
    const onDismiss = vi.fn();
    const { getByTestId } = render(TourBanner, { props: { onStart, onDismiss } });
    await fireEvent.click(getByTestId('tour-banner-start'));
    expect(onStart).toHaveBeenCalledTimes(1);
    await fireEvent.click(getByTestId('tour-banner-dismiss'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
