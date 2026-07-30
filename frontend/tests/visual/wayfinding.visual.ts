import { test, type Page, type Route } from '@playwright/test';

// ── Visual walkthrough of the wayfinding polish (ds-7ag.2 / plan Tasks 3-4) ──
// Same hand-run rig as desk.visual.ts / estate.visual.ts: drives the REAL
// Svelte app (vite dev) with every backend endpoint mocked, and captures PNGs
// so a human can eyeball what jsdom cannot render — the stat numerals' hover
// hint, the estate view's inert figures, and the quiet back-to-desk link — in
// BOTH locales.
//
// NOT wired into CI (no assertions; it produces frames). Run by hand:
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     wayfinding.visual.ts

const SHOTS = process.env.VISUAL_OUT ?? '/tmp/driftscribe-wayfinding-screens';
const VIEWPORT = { width: 1280, height: 900 };

type Locale = 'en' | 'ja';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function seed(page: Page, locale: Locale) {
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', l);
    localStorage.setItem('driftscribe_tour_done', '1'); // no tour banner in frame
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
  }, locale);
}

const GRAPH = {
  generated_at: '2026-07-30T02:00:00Z',
  project: 'driftscribe-hack-2026',
  caveat: '',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 735, managed: 9, drift: 6 },
  groups: [
    {
      asset_type: 'storage.googleapis.com/Bucket',
      label: 'Storage bucket',
      count: 2,
      managed: 1,
      drift: 1,
      sensitive: false,
      adoptable: true,
      nodes: [
        {
          id: 'b0',
          label: 'ds-receipts-2026',
          asset_type: 'storage.googleapis.com/Bucket',
          managed: false,
          location: 'asia-northeast1',
        },
        {
          id: 'b1',
          label: 'ds-artifacts',
          asset_type: 'storage.googleapis.com/Bucket',
          managed: true,
          location: 'asia-northeast1',
        },
      ],
    },
  ],
  edges: [],
};

// One pending rollback: the desk's awaiting numeral must be a live control (it
// is inert at 0), which is the state whose hint we need to see.
const ROLLBACK = {
  decision_id: 'rb-visual',
  action: 'rollback',
  created_at: '2026-07-30T02:05:00Z',
  approval: {
    approval_url: '/approvals/rb-visual?t=abc',
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    resolved_at: null,
    status: 'pending',
  },
  summary: 'EXTRA env var drifted on driftscribe-agent',
};

// Route patterns are deliberately PRECISE (regex / exact suffix), matching the
// sibling visual specs: vite dev serves modules from source paths, so a loose
// `**/conversations**` also matches `/src/lib/conversations.ts` and answers a JS
// import with JSON — the app then never boots at all.
async function mock(page: Page) {
  await page.route('**/infra/graph', (r) => json(r, GRAPH));
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: [] }));
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [ROLLBACK] }));
  await page.route(/\/conversations\?/, (r) => json(r, { conversations: [] }));
  await page.route('**/autonomy', (r) =>
    json(r, { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false }),
  );
  await page.route('**/pause', (r) =>
    json(r, { paused: false, reason: null, actor: null, updated_at: null, read_error: false }),
  );
}

for (const locale of ['ja', 'en'] as const) {
  test(`desk + estate wayfinding — ${locale}`, async ({ page }) => {
    await page.setViewportSize(VIEWPORT);
    await seed(page, locale);
    await mock(page);

    // 1. Desk at rest: the three numerals, no hint showing.
    await page.goto('/?view=desk');
    await page.getByTestId('instrument-band').waitFor();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOTS}/${locale}-1-desk-rest.png` });

    // 2. Hovering the drift numeral reveals "view infrastructure".
    await page.getByTestId('instrument-band-drift').hover();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-2-desk-hover-drift.png` });

    // 3. Hovering awaiting reveals the queue-below hint instead.
    await page.getByTestId('instrument-band-awaiting').hover();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-3-desk-hover-awaiting.png` });

    // 4. Keyboard focus shows the same hint (focus-visible, not just hover).
    await page.getByTestId('instrument-band-managed').focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-4-desk-focus-managed.png` });

    // 5. Estate: the back link, and managed/drift as inert figures.
    await page.goto('/?view=estate');
    await page.getByTestId('estate-back-desk').waitFor();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOTS}/${locale}-5-estate.png` });

    // 6. Hovering an inert figure on the estate must change nothing.
    await page.getByTestId('instrument-band-managed').hover();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-6-estate-hover-inert.png` });

    // 7. Estate awaiting still leads somewhere — back to the desk.
    await page.getByTestId('instrument-band-awaiting').hover();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-7-estate-hover-awaiting.png` });
  });
}
