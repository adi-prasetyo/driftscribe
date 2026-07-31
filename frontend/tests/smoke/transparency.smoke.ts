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

// Task 3.6 step 2 flipped DEFAULT_VIEW to 'desk', so a BARE url is now the
// approval desk, which renders no composer, no timeline and no decisions rail.
// Everything in this file exercises those, so it navigates explicitly to the
// chat view. The bare front door gets its own dedicated test below rather than
// being every test's incidental starting point.
const CHAT_URL = '/?view=chat';

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
  // The Task 3.6 flip in one test: what a judge typing the bare domain gets.
  // Runs against the BUILT bundle served by the real FastAPI shell, so it
  // catches a desk that unit tests render happily but that dies on the real
  // asset/route path.
  test('the bare url is the approval desk, not the chat composer', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await expect(page.getByTestId('approval-desk')).toBeVisible();
    await expect(page.getByTestId('instrument-band')).toBeVisible();
    // The chat shell must be genuinely absent, not merely hidden.
    await expect(page.locator('#chat-form')).toHaveCount(0);
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionsPane}"]`)).toHaveCount(0);

    // …and the header nav still reaches chat from there.
    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();
    await expect(page.getByTestId('approval-desk')).toHaveCount(0);
  });

  // The 2026-07-31 merge put the desk card and the estate section in one grid
  // column, and a real layout engine is the only thing that can check they
  // landed in the same one — jsdom has none, so the unit suite cannot see this
  // at all. It is not hypothetical: both declared `max-width: 780px; margin: 0
  // auto`, but as auto-margin grid items they were sized SHRINK-TO-FIT, and the
  // estate's narrower content gave it 384px at left 448 against the desk's
  // 780px at left 250. Two mismatched centered cards stacked in one column is
  // precisely the "the frontend looks bolted on" reading the merge answers.
  test('the desk card and the estate section share one column', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');

    const desk = await page.getByTestId('approval-desk').boundingBox();
    const estate = await page.getByTestId('estate-view').boundingBox();
    expect(desk, 'desk card must render').not.toBeNull();
    expect(estate, 'estate section must render').not.toBeNull();
    // Exact, not approximate: they are the same column, so any difference is a
    // bug rather than a rounding artifact.
    expect(Math.round(estate!.x)).toBe(Math.round(desk!.x));
    expect(Math.round(estate!.width)).toBe(Math.round(desk!.width));
  });

  // Phone width. The estate is on the FRONT DOOR since the merge, so a visitor
  // on a phone meets these rows without clicking anything.
  //
  // Measured against the CARD, not the document, and that distinction is the
  // whole point: `.estate-view` is `overflow: hidden`, so a chip that runs past
  // the card is CLIPPED, never scrolled to. A document-level `scrollWidth ===
  // clientWidth` check — the narrow-width check the plan prescribes, and the one
  // this suite would naturally reach for — passes happily with a control sitting
  // half off the card. It did: at 390px the adopt button was cut by 4px in ja
  // and 16px in en until the row learned to restack (EstateView.svelte's 460px
  // query). Both checks are below; only the first can see that failure.
  //
  // Every chip, not one testid: a drift row ends in exactly one of three (the
  // adopt button, the "PR #n awaiting review" marker, or the mute "adoption
  // status unknown" — which is what THIS fixture produces, since mockData
  // leaves /infra/pending-approvals unrouted and an unsupported absence must
  // not offer an Adopt). They share `.estate-view__chip`, they are the widest
  // thing in a row, and the invariant belongs to all of them equally.
  // Both locales, because the widest string is a DIFFERENT one in each: the EN
  // chip is the widest element in its row, the JA type label is. A single-locale
  // pin here would have been half a guard.
  for (const locale of ['ja', 'en'] as const) {
    test(`at phone width the estate row stays inside its card and does not collide (${locale})`, async ({
      page,
    }) => {
      await seedToken(page);
      await page.addInitScript((l) => localStorage.setItem('driftscribe.locale', l), locale);
      await mockData(page, freshState());
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto('/');

      const estate = page.getByTestId('estate-view');
      await expect(estate).toBeVisible();

      const chips = estate.locator('.estate-view__chip');
      // The fixture's own premise. Without it, a graph fixture that stopped
      // producing chip-bearing rows would leave this passing on an empty set.
      await expect(chips.first()).toBeVisible();

      const card = (await estate.boundingBox())!;
      const padRight = await estate.evaluate((el) => parseFloat(getComputedStyle(el).paddingRight));
      const limit = Math.round(card.x + card.width - padRight);
      for (let i = 0; i < (await chips.count()); i++) {
        const box = (await chips.nth(i).boundingBox())!;
        expect(Math.round(box.x + box.width), `chip ${i} escapes the card`).toBeLessThanOrEqual(
          limit,
        );
      }

      // Fitting inside the card is not enough: the first restack attempt put the
      // type label and the chip on one line, where both are `nowrap` and the
      // type simply ran UNDER the chip. Every element stayed within the card, so
      // the check above passed while the row was unreadable. Overlap is its own
      // failure and needs its own assertion — stated as 2D rect intersection so
      // it holds whatever arrangement the breakpoint chooses.
      const rows = await estate.locator('.estate-view__row').all();
      let checked = 0;
      for (const row of rows) {
        const type = row.locator('.estate-view__type');
        const chip = row.locator('.estate-view__chip');
        if ((await type.count()) === 0 || (await chip.count()) === 0) continue;
        const t = (await type.first().boundingBox())!;
        const c = (await chip.first().boundingBox())!;
        const xOverlap = Math.min(t.x + t.width, c.x + c.width) - Math.max(t.x, c.x);
        const yOverlap = Math.min(t.y + t.height, c.y + c.height) - Math.max(t.y, c.y);
        const label = `${(await type.first().textContent())?.trim()} / ${(
          await chip.first().textContent()
        )?.trim()}`;
        expect(xOverlap > 0 && yOverlap > 0, `type overlaps chip: ${label}`).toBe(false);
        checked++;
      }
      expect(checked, 'no row carried both a type and a chip').toBeGreaterThan(0);

      // …and the page itself still does not scroll sideways.
      const { scrollWidth, clientWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(scrollWidth).toBe(clientWidth);
    });
  }

  // The slim hero is a TYPOGRAPHY change, and typography is the one thing none
  // of the other gates can see: jsdom does not run the cascade, so a unit test
  // asserting the --slim class passes whether or not the rule inside it applies,
  // and the visual rigs measure geometry, not computed font. This shipped dead
  // for exactly that reason — `.approval-desk__calm--slim h2` and
  // `.approval-desk h2` are BOTH (0,2,1) once Svelte scopes them, so the later
  // (31px Mincho) rule won every property and the "slim" strip carried a full
  // hero headline. Assert the computed values, not the class.
  test('the slim hero headline is body-size, not the 31px Mincho hero rule', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // freshState's decisions fixture carries a PENDING approval, which renders
    // the tall pending card and no calm strip at all. Override both lanes to
    // reach the resting state — the default a judge actually lands on. Routes
    // registered later win, so these supersede mockData's.
    await page.route('**/decisions**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ decisions: [] }) }),
    );
    await page.route('**/infra/pending-approvals**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ approvals: [] }) }),
    );
    await page.goto('/');

    // Wait for RESTING specifically. Both slim states carry the class, and the
    // store starts `settled: false` — which deskModel reports as
    // `unknown/loading` (desk.ts) — so a bare class check can be satisfied by
    // the loading strip before the three lanes land. The CSS pin below would
    // still bite either way, but "this is the resting state a judge lands on"
    // would be an unproven claim about which strip was measured.
    await expect(page.getByTestId('approval-desk-resting')).toBeVisible();
    const slim = page.locator('.approval-desk__calm--slim');
    // Fixture premise: the slim wrapper must really be there to measure.
    await expect(slim).toHaveCount(1);
    const h2 = slim.locator('h2');
    await expect(h2).toBeVisible();

    const cs = await h2.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        fontSize: s.fontSize,
        fontWeight: s.fontWeight,
        marginTop: s.marginTop,
        fontFamily: s.fontFamily,
      };
    });
    expect(cs.fontSize, 'slim headline size').toBe('15px');
    expect(cs.fontWeight, 'slim headline weight').toBe('600');
    expect(cs.marginTop, 'slim headline margin').toBe('0px');
    expect(cs.fontFamily, 'slim headline must not use the Mincho hero face').not.toMatch(/Mincho/i);
  });

  test('shell + built assets load with no 404 and render the chrome', async ({ page }) => {
    const bad: string[] = [];
    page.on('response', (r) => {
      if (r.url().includes('/static/') && r.status() >= 400) bad.push(`${r.status()} ${r.url()}`);
    });
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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

  // ds-jns re-pointed the rail's view-reasoning: an iac_apply opens as a RECORD
  // on the desk that lists it, not as a replay in the chat column. Everything
  // this test was about survives the move — the curated decision fields, and
  // the note explaining that an empty timeline here is expected rather than a
  // failure to load — so the assertions follow the content to its new home.
  test('historical iac_apply: desk record with the decision summary + "recorded directly" note', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

    // Open the iac_apply decision specifically (not .first(), which is a rollback).
    await page
      .locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)
      .filter({ hasText: 'iac_apply' })
      .locator(`[data-testid="${TESTIDS.openTraceButton}"]`)
      .click();

    // The desk, with the record open on it. No replay, no status pill: a record
    // is not a run, so there is no lifecycle to label.
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    const record = page.getByTestId('decision-record');
    await expect(record).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.historicalBanner}"]`)).toHaveCount(0);

    // The DecisionSummary card renders the curated, safe fields.
    const summary = record.locator('[data-testid="decision-summary"]');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('Infra apply');
    await expect(summary).toContainText('#47');
    await expect(summary).toContainText('op@example.com');

    // The empty-trace note still explains WHY there is no reasoning: an
    // iac_apply is recorded directly by the approval handler.
    await expect(record.getByTestId('trace-detail-empty')).toContainText('recorded directly');

    // No prose section — this decision carries no rationale or rendered body.
    await expect(record.getByTestId('decision-record-prose')).toHaveCount(0);
  });

  test('infrastructure panel: glanceable drift badge, then expand renders the resource cards', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

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

  test('unmatched-declarations band: separate badge, Investigate prefills a Provision draft (no /chat on click), Adopt still present, layout holds', async ({
    page,
  }) => {
    await seedToken(page);
    await mockData(page, freshState());
    let chatPosts = 0;
    page.on('request', (req) => {
      if (req.url().includes('/chat') && req.method() === 'POST') chatPosts++;
    });
    await page.goto(CHAT_URL);

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
    // The click prefilled a draft only — no chat turn was sent.
    expect(chatPosts).toBe(0);

    // ...and the draft is addressed to PROVISION. This used to read the checked
    // radio, but the crew picker is gone (a thread opens on Explore and moves by
    // handoff), so the draft's crew is no longer rendered anywhere — Investigate
    // stays a deep link that carries explicit intent, and its intent is now
    // invisible until the turn is sent. So send it and read the crew off the
    // request. That is strictly the better assertion: the radio only proved the
    // UI DISPLAYED "provision", while the body proves the turn is really
    // addressed to Provision, which is the thing that mattered all along.
    const chatReq = page.waitForRequest(
      (r) => r.url().includes('/chat') && r.method() === 'POST',
    );
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();
    expect(JSON.parse((await chatReq).postData() ?? '{}').workload).toBe('provision');
  });

  // The old shape of this test — open-trace dims the composer, new chat exits —
  // described a MODE the chat column entered. ds-jns replaced the mode with a
  // destination: the record opens on the desk, and the way back is the nav. No
  // disabled composer, because the composer is not on that page at all.
  test('the rail’s view-reasoning opens the record on the desk; the nav comes back', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

    await page.locator(`[data-testid="${TESTIDS.openTraceButton}"]`).first().click();
    await expect(page.getByTestId('decision-record')).toBeVisible();
    await expect(page.locator('#chat-form')).toHaveCount(0);
    await expect(page).toHaveURL(/reasoning=/);

    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();
    await expect(page.getByTestId('decision-record')).toHaveCount(0);
    await expect(page).not.toHaveURL(/reasoning=/);
  });

  test('drift decision: env-diff card shows non-secret values, redacts secret-named + credentialed-URL values, leaks no raw secret', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

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
    await page.goto(CHAT_URL);

    await expect(page.locator('html')).toHaveAttribute('lang', 'ja');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('送信');

    await page.getByTestId('locale-en').click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('Send');
  });
});
