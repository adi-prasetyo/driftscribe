import { test, expect } from '@playwright/test';

const TOKEN = process.env.DRIFTSCRIBE_E2E_TOKEN ?? '';

test.describe('transparency UI', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/ui/transparency');
    // Phase 19.B stores the token under sessionStorage['driftscribe_token']
    // (verified agent/templates/transparency.html:609); NOT the dot-separated form.
    await page.evaluate((t) => {
      sessionStorage.setItem('driftscribe_token', t);
    }, TOKEN);
    await page.reload();
  });

  test('renders three reasoning groups after /chat fires', async ({ page }) => {
    await page.locator('[data-testid="chat-prompt"]').fill('Check payment-demo-e2e for drift');
    await page.locator('[data-testid="chat-submit"]').click();

    // Phase 19.B data-group attrs (unchanged in 20.6.0).
    await expect(page.locator('[data-group="coordinator"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('[data-group="tools"]')).toBeVisible();
    await expect(page.locator('[data-group="mcp"]')).toBeVisible();

    await expect(page.locator('[data-testid="final-response"]')).toBeVisible({ timeout: 60_000 });
  });

  test('past-decisions pane renders with at least one item (seeded)', async ({ page, request }) => {
    // Seed a decision via /recheck so the pane is non-empty independent of
    // whether the Python E2E job ran. Avoids order-dependence between jobs.
    // NOTE: this seeded decision lands in Firestore outside the Python
    // _firestore_cleanup_tracker. For the manual-dispatch cadence this is
    // acceptable (few extra docs per run); for a future nightly cadence,
    // add a periodic sweep — see "Risks & open questions" below.
    await request.post(`${process.env.DRIFTSCRIBE_E2E_URL}/recheck`, {
      headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
      data: { workload: 'drift' },
    });
    await page.reload();

    await expect(page.locator('[data-testid="past-decisions-pane"]')).toBeVisible();
    await expect(page.locator('[data-testid="past-decision-item"]').first())
      .toBeVisible({ timeout: 15_000 });
  });

  test('open-trace button opens historical mode', async ({ page, request }) => {
    // Seed (same reason as above — ensure ≥1 past-decision-item exists).
    await request.post(`${process.env.DRIFTSCRIBE_E2E_URL}/recheck`, {
      headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
      data: { workload: 'drift' },
    });
    await page.reload();

    // Click the explicit button — the row itself may also be clickable, but the
    // button is the stable hook.
    await page.locator('[data-testid="open-trace-button"]').first().click();
    await expect(page.locator('[data-testid="historical-banner"]')).toBeVisible({ timeout: 10_000 });
  });
});
