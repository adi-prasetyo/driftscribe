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
    // poll lag), and the reply lands in the thread on the stream's `done`
    // frame. The assertions below are transport-agnostic: they hold for
    // both the streaming path and the legacy JSON+poll fallback.
    await page.locator('[data-testid="chat-prompt"]').fill('Check payment-demo-e2e for drift');
    await page.locator('[data-testid="chat-submit"]').click();

    // Chat-native: the reply lands in the thread's crew bubble, and the crew turn
    // carries its reasoning INLINE — expanding that disclosure is what proves the
    // trace is reachable from the message.
    //
    // Until ds-jns Task 3.3 this first asserted three page-level reasoning
    // panels (#group-coordinator / #group-tools / #group-mcp, one per event
    // kind) and opened the tools one. Those are deleted: reasoning hangs off the
    // turn that produced it and reads as ONE interleaved list, so the equivalent
    // assertion is the disclosure and the rows inside it, below. Deliberately no
    // per-KIND row assertion here — a drift-check chat reliably calls a tool but
    // is not guaranteed to emit MCP traffic, and pinning a kind would be flaky.
    await expect(page.locator('[data-testid="conversation-thread"]')).toBeVisible({ timeout: 60_000 });

    // The disclosure alone is NOT a "the reply arrived and persisted" signal
    // the way the old settled-only open-trace button was: it attaches from the
    // `meta` frame, on the optimistic turn, before anything has completed. Wait
    // for the two things that do mean it — the typing indicator gone (the
    // `done` frame landed) and ?conversation set (the turn persisted).
    await expect(page.locator('[data-testid="thread-typing"]')).toHaveCount(0, { timeout: 60_000 });
    await expect
      .poll(() => new URL(page.url()).searchParams.get('conversation'), { timeout: 60_000 })
      .not.toBeNull();

    const disclosure = page.locator('[data-testid="reasoning-disclosure"]').first();
    await expect(disclosure).toBeVisible({ timeout: 60_000 });
    await disclosure.click();
    await expect(page.locator('[data-testid="trace-detail"]').first()).toBeVisible({ timeout: 30_000 });
    // …and it holds real reasoning, not an empty shell.
    await expect(
      page.locator('[data-testid="trace-detail"]').first().locator('[data-testid^="trace-row-"]').first(),
    ).toBeVisible({ timeout: 30_000 });
  });

  test('the desk ledger renders at least one decision (seeded)', async ({ page, request }) => {
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

    // The desk, which is where decisions are listed since ds-jns Task 3.3
    // deleted the chat's decisions rail. Bare URL: the desk is DEFAULT_VIEW.
    await page.goto(`${process.env.DRIFTSCRIBE_E2E_URL}/`);

    await expect(page.locator('[data-testid="approval-desk"]')).toBeVisible();
    await expect(page.locator('[data-testid="ledger-strip-row"]').first())
      .toBeVisible({ timeout: 15_000 });
  });

  test('a ledger row opens that decision as a record', async ({ page, request }) => {
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

    await page.goto(`${process.env.DRIFTSCRIBE_E2E_URL}/`);

    // The row IS the affordance now — it is a <button> (see the ledger-row
    // smoke, which pins that it still lays out as a row).
    await page.locator('[data-testid="ledger-strip-row"]').first().click();
    await expect(page.locator('[data-testid="decision-record"]')).toBeVisible({ timeout: 10_000 });
    // A shareable link to exactly what is on screen.
    await expect.poll(() => new URL(page.url()).searchParams.get('reasoning')).not.toBeNull();
  });
});
