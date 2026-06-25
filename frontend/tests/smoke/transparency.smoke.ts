import { test, expect, type Page, type Route } from '@playwright/test';
import {
  TESTIDS,
  TRACE_ID,
  IAC_TRACE_ID,
  DRIFT_CARD_TRACE_ID,
  sseBody,
  traceResponse,
  iacTraceResponse,
  driftCardTraceResponse,
  decisionsResponse,
  infraGraphResponse,
  SECRET_TOKEN_VALUE_OLD,
  SECRET_TOKEN_VALUE_NEW,
  SECRET_URL_VALUE_OLD,
  SECRET_URL_VALUE_NEW,
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
    // drift trace carries a decision doc with env diffs (DriftDiffCard); the
    // iac_apply trace has no events + a decision doc; the chat trace has events.
    const url = route.request().url();
    const body = url.includes(DRIFT_CARD_TRACE_ID)
      ? driftCardTraceResponse()
      : url.includes(IAC_TRACE_ID)
        ? iacTraceResponse()
        : traceResponse();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  // InfraDiagram fetches this on mount (for the glanceable badge); mock it for
  // every test so no real infra_reader call escapes the browser.
  await page.route('**/infra/graph', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(infraGraphResponse()),
    }),
  );

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
    await page.goto('/');

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
    await page.goto('/');

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
    await page.goto('/');

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
    await page.goto('/');

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
    await page.goto('/');

    // five seeded decisions render (two rollbacks + one iac_apply + two drift_issue)
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)).toHaveCount(5);
    // the off-origin approval_url must NOT become an anchor
    await expect(page.locator('a[href*="evil.example"]')).toHaveCount(0);
    // the same-origin one DOES render an Approve link
    await expect(page.locator('a.past-approve-btn[href*="/approvals/ap-1"]')).toHaveCount(1);
  });

  test('decision github.url: valid github.com link renders, javascript: url does not', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    // Exactly one safe github link — the valid github.com issue. The
    // javascript: row is rejected by safeGithubHref and renders no anchor.
    const ghLinks = page.getByTestId('decision-github-link');
    await expect(ghLinks).toHaveCount(1);
    await expect(ghLinks.first()).toHaveAttribute('href', 'https://github.com/acme/ops/issues/99');
    await expect(ghLinks.first()).toHaveAttribute('rel', 'noopener noreferrer');
    await expect(ghLinks.first()).toHaveAttribute('target', '_blank');
    // Belt-and-suspenders: no anchor anywhere carries the javascript: payload.
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  });

  test('historical iac_apply: "historical" pill (not streaming) + decision summary + empty-timeline note', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    // Open the iac_apply decision specifically (not .first(), which is a rollback).
    await page
      .locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)
      .filter({ hasText: 'iac_apply' })
      .locator(`[data-testid="${TESTIDS.openTraceButton}"]`)
      .click();

    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeVisible();

    // (openTrace scrolls #historical-badge into view — a layout/viewport effect
    // that this short mock fixture can't meaningfully assert, since the banner is
    // already in view here. The scroll call itself is locked in App.test.ts and
    // the real-viewport behavior is confirmed by a live Playwright check.)

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

    // The empty-timeline note explains why there's no reasoning stream — and the
    // three redundant empty group accordions are suppressed in this state.
    await expect(page.locator('[data-testid="timeline-empty"]')).toBeVisible();
    await expect(page.locator('#group-coordinator')).toHaveCount(0);

    // The hero stays hidden — this decision carries no prose.
    await expect(page.locator(`[data-testid="${TESTIDS.finalResponse}"]`)).toBeHidden();
  });

  test('infrastructure panel: glanceable drift badge, then expand renders the resource cards', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    // Collapsed panel shows a glanceable drift badge (data fetched on mount).
    // Scope-aware: 1 drift in the adoptable Cloud Run type (NOT the secret, which
    // is out of scope), so the badge reads "1 drift", not the raw total of 2.
    const panel = page.locator(`[data-testid="${TESTIDS.infraPanel}"]`);
    await expect(panel).toBeVisible();
    const badge = page.locator(`[data-testid="${TESTIDS.infraDriftBadge}"]`);
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText(/1 drift/);

    // Expand → the in-scope resource card grid renders (no Mermaid on the normal
    // path). The adoptable Cloud Run services show by default.
    await page.locator(`[data-testid="${TESTIDS.infraToggle}"]`).click();
    const cards = page.locator(`[data-testid="${TESTIDS.infraCards}"]`);
    await expect(cards).toBeVisible();
    await expect(cards.locator('svg')).toHaveCount(0);
    await expect(cards).toContainText('payment-demo');
    await expect(cards).toContainText('storefront');

    // The muted context line keeps the full estate honest (3 indexed, 1 of which
    // is a type DriftScribe doesn't manage).
    await expect(panel).toContainText('3 total resources indexed');

    // The non-adoptable secret folds into the "Other resources" disclosure, not
    // the default grid; open it and confirm the counts-only card is in there.
    const other = page.locator(`[data-testid="${TESTIDS.infraOther}"]`);
    await expect(other).toBeVisible();
    await expect(cards).not.toContainText('1 secret');
    await other.locator('summary').click();
    await expect(page.locator(`[data-testid="${TESTIDS.infraOtherCards}"]`)).toContainText('1 secret');
  });

  test('open-trace enters historical mode; new chat exits', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await page.locator(`[data-testid="${TESTIDS.openTraceButton}"]`).first().click();
    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeVisible();
    // chat form is dimmed/disabled in historical mode
    await expect(page.locator('#chat-form')).toHaveClass(/historical/);
    await expect(page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`)).toBeDisabled();

    await page.locator('#new-chat-btn').click();
    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toBeHidden();
  });

  test('drift decision: env-diff card shows non-secret values, redacts secret-named + credentialed-URL values, leaks no raw secret', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    // Open d-drift-1 specifically. Filter by its exact github href so the
    // selector is unambiguous even if another row later also renders a link.
    await page
      .locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)
      .filter({ has: page.locator('a[data-testid="decision-github-link"][href="https://github.com/acme/ops/issues/99"]') })
      .locator(`[data-testid="${TESTIDS.openTraceButton}"]`)
      .click();

    const card = page.getByTestId('drift-diff-card');
    await expect(card).toBeVisible();

    // Non-secret var: both values shown verbatim.
    const logRow = card.locator('tr', { hasText: 'LOG_LEVEL' });
    await expect(logRow).toContainText('info');
    await expect(logRow).toContainText('debug');

    // Secret-by-NAME and secret-by-VALUE rows show the redaction marker.
    await expect(card.locator('tr', { hasText: 'API_TOKEN' })).toContainText('(value redacted: secret-like)');
    await expect(card.locator('tr', { hasText: 'ENDPOINT' })).toContainText('(value redacted: secret-like)');

    // Hard guarantee: no raw diff secret value appears anywhere in the rendered DOM —
    // checked both as serialized HTML (attributes included) and as visible text.
    const html = await page.content();
    const body = page.locator('body');
    for (const secret of [
      SECRET_TOKEN_VALUE_OLD, SECRET_TOKEN_VALUE_NEW, SECRET_URL_VALUE_OLD, SECRET_URL_VALUE_NEW,
    ]) {
      expect(html, `raw secret must not appear in DOM html: ${secret}`).not.toContain(secret);
      await expect(body, `raw secret must not appear in body text: ${secret}`).not.toContainText(secret);
    }
  });
});
