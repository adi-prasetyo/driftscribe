// frontend/tests/unit/DemoNoticeBanner.test.ts
import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import DemoNoticeBanner, {
  DEMO_NOTICE_DISMISSED_KEY,
} from '../../src/components/DemoNoticeBanner.svelte';

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

describe('DemoNoticeBanner', () => {
  it('renders by default with the load-bearing copy', () => {
    const { getByTestId } = render(DemoNoticeBanner, { props: {} });
    const banner = getByTestId('demo-notice-banner');
    expect(banner.textContent).toContain('This is a live sandbox.');
    expect(banner.textContent).toContain('heals itself every couple of hours');
    expect(banner.textContent).toContain('upgrade demo resets every morning');
  });

  it('dismiss hides the banner and persists the localStorage key', async () => {
    const { getByTestId, queryByTestId } = render(DemoNoticeBanner, { props: {} });
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBeNull();
    await fireEvent.click(getByTestId('demo-notice-dismiss'));
    expect(queryByTestId('demo-notice-banner')).toBeNull();
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });

  it('does not render when the dismissed key is already set', () => {
    window.localStorage.setItem(DEMO_NOTICE_DISMISSED_KEY, '1');
    const { queryByTestId } = render(DemoNoticeBanner, { props: {} });
    expect(queryByTestId('demo-notice-banner')).toBeNull();
  });
});
