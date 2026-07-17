import { expect, type Page, type Route } from '@playwright/test';
import {
  test,
  TESTIDS,
  TRACE_ID,
  CONVERSATION_ID,
  IAC_TRACE_ID,
  DRIFT_CARD_TRACE_ID,
  sseBody,
  traceResponse,
  iacTraceResponse,
  driftCardTraceResponse,
  decisionsResponse,
  infraGraphResponse,
  conversationsListResponse,
  conversationDetailResponse,
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

  // The rail's conversation list. Empty by default (the chat-native settle after
  // a turn calls loadConversations to refresh the rail); the resume smoke
  // registers its own richer /conversations routes AFTER this, so they win there.
  await page.route('**/conversations**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ conversations: [] }),
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
      // Echo a conversation_id so the reply settles into the thread's crew
      // bubble (chat-native), matching prod's persisted-turn path.
      body: sseBody(TRACE_ID, { conversationId: CONVERSATION_ID }),
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

  test('chat SSE renders timeline + threaded reply; sends Accept + token; backfills mcp', async ({ page }) => {
    const state = freshState();
    await seedToken(page);
    await mockData(page, state);
    await page.goto('/');

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // The reply lands in the thread's crew bubble (chat-native), alongside the
    // operator's own prompt bubble — NOT the standalone hero, which stays hidden.
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    await expect(thread).toBeVisible();
    await expect(thread).toContainText('Check payment-demo for drift');
    await expect(thread).toContainText('Found 3 drifted env vars.');
    await expect(page.locator(`[data-testid="${TESTIDS.finalResponse}"]`)).toBeHidden();

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

  test('a thinking bubble streams in the thread until the reply lands, then fills in place', async ({ page }) => {
    const state = freshState();
    state.chatDelayMs = 800; // hold /chat open so the in-flight state is observable
    await seedToken(page);
    await mockData(page, state);
    await page.goto('/');

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // While the coordinator is working (request in flight, no reply yet) the
    // exchange is already in the thread: the prompt bubble + a live "thinking"
    // crew bubble. The standalone hero stays out of the way.
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    const typing = page.locator(`[data-testid="${TESTIDS.threadTyping}"]`);
    const final = page.locator(`[data-testid="${TESTIDS.finalResponse}"]`);
    await expect(thread).toBeVisible();
    await expect(typing).toBeVisible();
    await expect(final).toBeHidden();

    // Once the reply lands, the typing indicator is replaced by the prose in the
    // SAME bubble — no separate hero, no position hop.
    await expect(thread).toContainText('Found 3 drifted env vars.');
    await expect(typing).toBeHidden();
    await expect(final).toBeHidden();
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

    // (openTrace renders the replay at the top of the chat column and scrolls the
    // window to top — a layout/viewport effect this short mock fixture can't
    // meaningfully assert. The scroll call + above-the-composer DOM order are
    // locked in App.test.ts and the real-viewport behavior is confirmed by a
    // live Playwright check.)

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

  test('unmatched-declarations band: separate badge, Investigate prefills a Provision draft (no /chat), Adopt still present, layout holds', async ({
    page,
  }) => {
    await seedToken(page);
    await mockData(page, freshState());
    let chatPosts = 0;
    page.on('request', (req) => {
      if (req.url().includes('/chat') && req.method() === 'POST') chatPosts++;
    });
    await page.goto('/');

    // The collapsed summary carries a SEPARATE "N IaC unmatched" badge, distinct
    // from the drift badge (both present, never merged into one number).
    await expect(page.locator(`[data-testid="${TESTIDS.infraUnmatchedBadge}"]`)).toHaveText(
      /1 IaC unmatched/,
    );
    await expect(page.locator(`[data-testid="${TESTIDS.infraDriftBadge}"]`)).toHaveText(/1 drift/);

    // Expand → the band AND the live unmanaged resource are both visible.
    await page.locator(`[data-testid="${TESTIDS.infraToggle}"]`).click();
    const band = page.locator(`[data-testid="${TESTIDS.infraUnmatched}"]`);
    await expect(band).toBeVisible();
    await expect(band).toContainText('storefront-old');
    await expect(band).toContainText('google_cloud_run_v2_service.storefront_old');
    await expect(band).toContainText('did not match the latest Cloud Asset Inventory snapshot');
    const cards = page.locator(`[data-testid="${TESTIDS.infraCards}"]`);
    await expect(cards).toContainText('storefront');
    // The live drift resource keeps its normal Adopt button (band adds none).
    await expect(page.locator('[data-testid="card-adopt-btn"]').first()).toBeVisible();

    // Layout holds at real viewports: the band + all its content (long names,
    // mono HCL addresses, the Investigate button) fit within the viewport width
    // and the grid stays visible. Scoped to the band on purpose — the app rail's
    // own mobile-width behavior is a separate concern, not this feature's.
    for (const [name, vp] of [
      ['desktop', { width: 1280, height: 900 }],
      ['mobile', { width: 390, height: 844 }],
    ] as const) {
      await page.setViewportSize(vp);
      await expect(band).toBeVisible();
      await expect(cards).toBeVisible();
      await page.screenshot({ path: `test-results/infra-unmatched-${name}.png`, fullPage: true });
      // No band descendant (long name, mono HCL address, Investigate button) may
      // extend past the band's OWN right edge — i.e. everything wraps within the
      // space the band is given, adding no horizontal overflow of its own. Scoped
      // to the band's box (not the viewport) because the desktop-first app shell's
      // rail is already wider than a 390px viewport, which is a separate concern.
      const bandOverflow = await band.evaluate((el) => {
        const boxRight = el.getBoundingClientRect().right;
        return Math.max(
          0,
          ...[...el.querySelectorAll('*')].map((c) => c.getBoundingClientRect().right - boxRight),
        );
      });
      expect(bandOverflow, `band content overflows the band box at ${name}`).toBeLessThanOrEqual(1);
    }

    // Investigate → a fresh Provision draft, prefilled, NOT submitted.
    await page.locator(`[data-testid="${TESTIDS.infraUnmatchedInvestigate}"]`).click();
    const prompt = page.locator('#prompt-input');
    await expect(prompt).toHaveValue(/storefront-old/);
    await expect(prompt).toHaveValue(/do not assume a rename/);
    await expect(page.locator('input[type="radio"]:checked')).toHaveValue('provision');
    // The click prefilled a draft only — no chat turn was sent.
    expect(chatPosts).toBe(0);
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

  test('conversations: resume a thread from the rail; it survives a reload (P2)', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // Detail route is registered LAST so it wins for `/conversations/<id>`;
    // the list glob (registered first here) handles `/conversations?limit=...`
    // (the `**/conversations/**` glob needs a trailing `/`, which the query
    // form doesn't have, so the two never collide).
    await page.route('**/conversations**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(conversationsListResponse()),
      }),
    );
    await page.route('**/conversations/**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(conversationDetailResponse()),
      }),
    );
    await page.goto('/');

    // The rail lists the persisted conversation.
    const pane = page.locator(`[data-testid="${TESTIDS.conversationsPane}"]`);
    await expect(pane).toBeVisible();
    await expect(pane).toContainText('prior chat about drift');

    // Resume it → the thread rehydrates with both turns.
    await page.locator(`[data-testid="${TESTIDS.conversationOpen}"]`).first().click();
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    await expect(thread).toBeVisible();
    await expect(thread).toContainText('what changed on payment-demo?');
    await expect(thread).toContainText('the env var EXTRA drifted from the contract');

    // Resume-after-reload: the rail rehydrates from /conversations and the
    // thread is reachable again (the durable-thread contract P2 is about).
    await page.reload();
    await expect(pane).toContainText('prior chat about drift');
    await page.locator(`[data-testid="${TESTIDS.conversationOpen}"]`).first().click();
    await expect(
      page.locator(`[data-testid="${TESTIDS.conversationThread}"]`),
    ).toContainText('the env var EXTRA drifted from the contract');
  });

  test('locale defaults to Japanese; the header toggle switches the UI to English', async ({ page }) => {
    // Every other test in this suite runs EN-pinned (see fixtures.ts); this is
    // the one deliberate exception, re-pinning to `ja` (init scripts run in
    // registration order, so this later call overrides the suite-wide pin).
    await page.addInitScript(() => {
      try {
        localStorage.setItem('driftscribe.locale', 'ja');
      } catch {
        /* ignore */
      }
    });
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await expect(page.locator('html')).toHaveAttribute('lang', 'ja');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('送信');

    await page.getByTestId('locale-en').click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('Send');
  });
});
