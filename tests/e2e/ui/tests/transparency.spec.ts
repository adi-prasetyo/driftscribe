import { test, expect } from '@playwright/test';

const TOKEN = process.env.DRIFTSCRIBE_E2E_TOKEN ?? '';

test.describe('transparency UI', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Phase 19.B stores the token under sessionStorage['driftscribe_token']
    // (verified agent/templates/transparency.html:609); NOT the dot-separated form.
    await page.evaluate((t) => {
      sessionStorage.setItem('driftscribe_token', t);
    }, TOKEN);
    await page.reload();
  });

  test('renders three reasoning panels and tools events after /chat', async ({ page }) => {
    // Phase 22: the UI now sends `Accept: text/event-stream`, so this
    // exercises the SSE streaming path against the live agent — timeline
    // rows populate live as the agent emits them (no ~15s Cloud Logging
    // poll lag), and the final-response card lands on the stream's `done`
    // frame. The assertions below are transport-agnostic: they hold for
    // both the streaming path and the legacy JSON+poll fallback.
    await page.locator('[data-testid="chat-prompt"]').fill('Check payment-demo-e2e for drift');
    await page.locator('[data-testid="chat-submit"]').click();

    // The three reasoning panels are <details> elements (transparency.html:564-577).
    // Only #group-coordinator opens by default; #group-tools and #group-mcp are
    // collapsed. The outer panels always render; the inner `data-group` divs
    // are hidden under a collapsed parent until the user expands them.
    //
    // We assert the three outer panels render. For tools we additionally open
    // the <details> and verify the inner event row — a drift-check chat
    // reliably calls read_live_env_tool. We do NOT open MCP because the chat
    // path is not guaranteed to emit MCP traffic for this prompt; asserting
    // its inner content would be flaky.
    await expect(page.locator('#group-coordinator')).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('#group-tools')).toBeVisible();
    await expect(page.locator('#group-mcp')).toBeVisible();

    await page.locator('#group-tools').evaluate((el) => { (el as HTMLDetailsElement).open = true; });
    await expect(page.locator('[data-group="tools"]')).toBeVisible();

    // Chat-native: the reply lands in the thread's crew bubble, not the standalone
    // hero. A real (non-ephemeral) chat persists, so the exchange settles into the
    // thread — the settled crew turn exposes its "open trace" link, which is a
    // reliable "the reply arrived and persisted" signal.
    await expect(page.locator('[data-testid="conversation-thread"]')).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('[data-testid="thread-open-trace"]').first()).toBeVisible({ timeout: 60_000 });
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
