import { test, expect, type Page, type Route } from '@playwright/test';
import {
  TESTIDS,
  TRACE_ID,
  sseBody,
  traceResponse,
  decisionsResponse,
} from './fixtures';

const ORIGIN = 'http://127.0.0.1:8765';

// Seed the operator token the way the deployed e2e does (sessionStorage), before
// any page script runs.
async function seedToken(page: Page, token = 'smoke-token') {
  await page.addInitScript((t) => {
    sessionStorage.setItem('driftscribe_token', t);
  }, token);
}

interface RouteState {
  decisionsStatus: number;
  chatHeaders: Record<string, string>;
}

async function mockData(page: Page, state: RouteState) {
  await page.route('**/decisions**', (route: Route) => {
    if (state.decisionsStatus !== 200) {
      return route.fulfill({
        status: state.decisionsStatus,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'auth required' }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(decisionsResponse(ORIGIN)),
    });
  });

  await page.route('**/trace/**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(traceResponse()),
    }),
  );

  await page.route('**/chat', (route: Route) => {
    state.chatHeaders = route.request().headers();
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'X-Trace-Id': TRACE_ID, 'Cache-Control': 'no-cache' },
      body: sseBody(),
    });
  });
}

function freshState(decisionsStatus = 200): RouteState {
  return { decisionsStatus, chatHeaders: {} };
}

test.describe('transparency UI (mock smoke)', () => {
  test('shell + built assets load with no 404 and render the chrome', async ({ page }) => {
    const bad: string[] = [];
    page.on('response', (r) => {
      if (r.url().includes('/static/') && r.status() >= 400) bad.push(`${r.status()} ${r.url()}`);
    });
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    await expect(page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionsPane}"]`)).toBeVisible();
    // three reasoning groups are real <details>
    await expect(page.locator('#group-coordinator')).toBeVisible();
    await expect(page.locator('#group-tools')).toBeVisible();
    await expect(page.locator('#group-mcp')).toBeVisible();
    expect(bad, `static assets must load with no 4xx/5xx: ${bad.join(', ')}`).toHaveLength(0);
  });

  test('chat SSE renders timeline + final response; sends Accept + token; backfills mcp', async ({ page }) => {
    const state = freshState();
    await seedToken(page);
    await mockData(page, state);
    await page.goto('/ui/transparency');

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // final response lands from the stream's `done` frame
    const final = page.locator(`[data-testid="${TESTIDS.finalResponse}"]`);
    await expect(final).toBeVisible();
    await expect(final).toContainText('Found 3 drifted env vars.');

    // the request advertised SSE + carried the token from sessionStorage
    expect(state.chatHeaders['accept'] ?? '').toContain('text/event-stream');
    expect(state.chatHeaders['x-driftscribe-token']).toBe('smoke-token');

    // tools group: open and see the worker (read_live_env_tool → "Reader (drift)")
    await page.locator('#group-tools').evaluate((el) => {
      (el as HTMLDetailsElement).open = true;
    });
    await expect(page.locator('[data-group="tools"]')).toBeVisible();
    await expect(page.locator('#group-tools')).toContainText('Reader (drift)');

    // mcp group: the side-channel mcp_call only arrives via the /trace backfill
    await page.locator('#group-mcp').evaluate((el) => {
      (el as HTMLDetailsElement).open = true;
    });
    await expect(page.locator('[data-group="mcp"]')).toBeVisible();
    await expect(page.locator('#group-mcp')).toContainText('search_documents');
  });

  test('auth-required (401) shows the inline AuthPanel instead of window.prompt', async ({ page }) => {
    // No token seeded; first /decisions returns 401.
    const state = freshState(401);
    await mockData(page, state);
    await page.goto('/ui/transparency');

    // The inline panel (role=dialog) appears — NOT a native prompt.
    await expect(page.getByRole('dialog')).toBeVisible();

    // Provide a token; subsequent calls succeed → panel closes, pill shows ok.
    state.decisionsStatus = 200;
    await page.getByRole('textbox', { name: 'Operator token' }).fill('typed-token');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.locator('#token-status')).toContainText('token ok');
  });

  test('malicious off-origin approval URL renders NO link', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    // two seeded decisions render
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)).toHaveCount(2);
    // the off-origin approval_url must NOT become an anchor
    await expect(page.locator('a[href*="evil.example"]')).toHaveCount(0);
    // the same-origin one DOES render an Approve link
    await expect(page.locator('a.past-approve-btn[href*="/approvals/ap-1"]')).toHaveCount(1);
  });

  test('open-trace enters historical mode; new chat exits', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    await page.locator(`[data-testid="${TESTIDS.openTraceButton}"]`).first().click();
    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeVisible();
    // chat form is dimmed/disabled in historical mode
    await expect(page.locator('#chat-form')).toHaveClass(/historical/);
    await expect(page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`)).toBeDisabled();

    await page.locator('#new-chat-btn').click();
    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeHidden();
  });
});
