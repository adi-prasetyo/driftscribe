import { test, expect, type Page, type Route } from '@playwright/test';
import {
  TESTIDS,
  TRACE_ID,
  IAC_TRACE_ID,
  sseBody,
  traceResponse,
  iacTraceResponse,
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
  // When > 0, the /chat route holds the response open this long before
  // fulfilling — long enough to observe the in-flight loading shimmer.
  chatDelayMs: number;
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

  await page.route('**/trace/**', (route: Route) => {
    // Branch by URL so each decision's trace resolves to its OWN payload — the
    // iac_apply trace has no events + a decision doc; the chat trace has events.
    const body = route.request().url().includes(IAC_TRACE_ID)
      ? iacTraceResponse()
      : traceResponse();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  await page.route('**/chat', async (route: Route) => {
    state.chatHeaders = route.request().headers();
    if (state.chatDelayMs > 0) {
      await new Promise((r) => setTimeout(r, state.chatDelayMs));
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'X-Trace-Id': TRACE_ID, 'Cache-Control': 'no-cache' },
      body: sseBody(),
    });
  });
}

function freshState(decisionsStatus = 200): RouteState {
  return { decisionsStatus, chatHeaders: {}, chatDelayMs: 0 };
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

  test('loading shimmer fills the hero until the reply lands, then is replaced', async ({ page }) => {
    const state = freshState();
    state.chatDelayMs = 800; // hold /chat open so the in-flight state is observable
    await seedToken(page);
    await mockData(page, state);
    await page.goto('/ui/transparency');

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // While the coordinator is working (request in flight, no reply yet) the
    // shimmer placeholder occupies the hero slot and the real hero stays hidden.
    const pending = page.locator(`[data-testid="${TESTIDS.replyPending}"]`);
    const final = page.locator(`[data-testid="${TESTIDS.finalResponse}"]`);
    await expect(pending).toBeVisible();
    await expect(final).toBeHidden();

    // Once the reply lands, the shimmer is replaced by the real answer.
    await expect(final).toBeVisible();
    await expect(final).toContainText('Found 3 drifted env vars.');
    await expect(pending).toBeHidden();
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

    // three seeded decisions render (two rollbacks + one iac_apply)
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)).toHaveCount(3);
    // the off-origin approval_url must NOT become an anchor
    await expect(page.locator('a[href*="evil.example"]')).toHaveCount(0);
    // the same-origin one DOES render an Approve link
    await expect(page.locator('a.past-approve-btn[href*="/approvals/ap-1"]')).toHaveCount(1);
  });

  test('historical iac_apply: "historical" pill (not streaming) + decision summary + empty-timeline note', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    // Open the iac_apply decision specifically (not .first(), which is a rollback).
    await page
      .locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)
      .filter({ hasText: 'iac_apply' })
      .locator(`[data-testid="${TESTIDS.openTraceButton}"]`)
      .click();

    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeVisible();

    // The status pill reads "historical" — NOT the regressed "streaming".
    const pill = page.locator('#status-pill');
    await expect(pill).toHaveText(/historical/);
    await expect(pill).not.toHaveText(/streaming/);

    // The DecisionSummary card renders the curated, safe fields.
    const summary = page.locator('[data-testid="decision-summary"]');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('Infra apply');
    await expect(summary).toContainText('#47');
    await expect(summary).toContainText('op@example.com');

    // The empty-timeline note explains why there's no reasoning stream.
    await expect(page.locator('[data-testid="timeline-empty"]')).toBeVisible();

    // The hero stays hidden — this decision carries no prose.
    await expect(page.locator(`[data-testid="${TESTIDS.finalResponse}"]`)).toBeHidden();
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
