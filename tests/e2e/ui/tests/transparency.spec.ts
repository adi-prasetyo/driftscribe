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

    // The three reasoning panels are <details> elements (transparency.html:564-577).
    // Only #group-coordinator opens by default; #group-tools and #group-mcp are
    // collapsed. Their inner `<div data-group="...">` reports hidden under a
    // collapsed parent even after tool_call events are appended — so we assert
    // the outer panel exists, then programmatically open it, then verify the
    // inner event row is visible. This preserves the "events rendered" intent
    // without coupling the test to the UI's default-collapsed presentation.
    await expect(page.locator('#group-coordinator')).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('#group-tools')).toBeVisible();
    await expect(page.locator('#group-mcp')).toBeVisible();

    await page.locator('#group-tools').evaluate((el) => { (el as HTMLDetailsElement).open = true; });
    await expect(page.locator('[data-group="tools"]')).toBeVisible();

    await expect(page.locator('[data-testid="final-response"]')).toBeVisible({ timeout: 60_000 });
  });

  test('past-decisions pane renders with at least one item (seeded)', async ({ page, request }) => {
    // Seed a decision via /recheck so the pane is non-empty independent of
    // whether the Python E2E job ran. `?force=true` derives a brand-new
    // event_key (agent/main.py:1049-1052) so the seed cannot collide with a
    // stale event_key left over from the Python session's deterministic
    // /recheck call. The seeded decision lands in Firestore outside the
    // Python _firestore_cleanup_tracker — acceptable for manual-dispatch
    // cadence; nightly cadence would need a UI-side sweep.
    const seed = await request.post(
      `${process.env.DRIFTSCRIBE_E2E_URL}/recheck?force=true`,
      {
        headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
        data: { workload: 'drift' },
      },
    );
    expect(seed.ok()).toBeTruthy();
    const seedBody = await seed.json();
    expect(seedBody.decision_id).toBeTruthy();

    await page.reload();

    await expect(page.locator('[data-testid="past-decisions-pane"]')).toBeVisible();
    await expect(page.locator('[data-testid="past-decision-item"]').first())
      .toBeVisible({ timeout: 15_000 });
  });

  test('open-trace button opens historical mode', async ({ page, request }) => {
    // Seed (same reason + force=true rationale as the previous test).
    const seed = await request.post(
      `${process.env.DRIFTSCRIBE_E2E_URL}/recheck?force=true`,
      {
        headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
        data: { workload: 'drift' },
      },
    );
    expect(seed.ok()).toBeTruthy();
    const seedBody = await seed.json();
    expect(seedBody.decision_id).toBeTruthy();

    await page.reload();

    // Click the explicit button — the row itself may also be clickable, but the
    // button is the stable hook.
    await page.locator('[data-testid="open-trace-button"]').first().click();
    await expect(page.locator('[data-testid="historical-banner"]')).toBeVisible({ timeout: 10_000 });
  });
});
