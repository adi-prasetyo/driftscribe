import { test, expect, type Page, type Route } from '@playwright/test';

// Mock-Playwright smoke for the autonomy header pill (header redesign). Boots the
// REAL FastAPI shell + /static (see playwright.smoke.config.ts) and mocks the data
// endpoints. Covers: the loaded pill opens its popover dial; Pause and Autonomy
// popovers are mutually exclusive; and — the case unit tests in jsdom can't catch
// because jsdom has no layout — the popover does NOT overflow the viewport on a
// narrow (mobile) screen.

const AUTONOMY_LOADED = {
  mode: 'propose_apply',
  reason: null,
  actor: null,
  updated_at: null,
  read_error: false,
};
const PAUSE_RUNNING = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };

async function seedToken(page: Page, token = 'smoke-token') {
  await page.addInitScript((t) => {
    sessionStorage.setItem('driftscribe_token', t);
  }, token);
}

async function mockData(page: Page) {
  const json = (body: unknown) => (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/autonomy', json(AUTONOMY_LOADED));
  await page.route('**/pause', json(PAUSE_RUNNING));
  await page.route('**/decisions**', json({ decisions: [] }));
  await page.route('**/conversations**', json({ conversations: [] }));
  await page.route('**/infra/graph', json({
    generated_at: null, project: 'demo', caveat: '', degraded: true, degraded_reason: 'mock',
    totals: { resources: 0, managed: 0, drift: 0 }, groups: [], edges: [],
  }));
}

test.describe('autonomy header pill (mock smoke)', () => {
  test('loaded pill opens the popover dial; segments render', async ({ page }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('/');

    const toggle = page.getByTestId('autonomy-pill-toggle');
    await expect(toggle).toBeVisible();
    await expect(toggle).toContainText('Propose + Apply');

    await toggle.click();
    await expect(page.getByTestId('autonomy-popover')).toBeVisible();
    await expect(page.getByTestId('autonomy-mode-observe')).toBeVisible();
    await expect(page.getByTestId('autonomy-mode-propose')).toBeVisible();
    await expect(page.getByTestId('autonomy-mode-propose_apply')).toBeVisible();
  });

  test('Pause and Autonomy popovers are mutually exclusive', async ({ page }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('/');

    await page.getByTestId('autonomy-pill-toggle').click();
    await expect(page.getByTestId('autonomy-popover')).toBeVisible();

    // Opening Pause closes Autonomy.
    await page.getByTestId('pause-pill-toggle').click();
    await expect(page.getByTestId('pause-popover')).toBeVisible();
    await expect(page.getByTestId('autonomy-popover')).toBeHidden();

    // And opening Autonomy again closes Pause.
    await page.getByTestId('autonomy-pill-toggle').click();
    await expect(page.getByTestId('autonomy-popover')).toBeVisible();
    await expect(page.getByTestId('pause-popover')).toBeHidden();
  });

  // The popover is right:0-anchored to the LEFT-most action pill, so it overflows
  // the viewport across the whole sub-desktop range until the screen is wide
  // enough that the pill's right edge clears the popover width. Check both the
  // sub-desktop (bottom-sheet) range and the desktop (dropdown) range.
  for (const width of [360, 600, 768, 900, 1280]) {
    test(`popover stays within the viewport at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await seedToken(page);
      await mockData(page);
      await page.goto('/');

      await page.getByTestId('autonomy-pill-toggle').click();
      const popover = page.getByTestId('autonomy-popover');
      await expect(popover).toBeVisible();

      const box = await popover.boundingBox();
      expect(box).not.toBeNull();
      // No horizontal overflow: left edge on-screen, right edge within the viewport.
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1); // +1 for sub-pixel rounding
    });
  }
});
