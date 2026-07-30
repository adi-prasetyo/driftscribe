import { test, type Page, type Route } from '@playwright/test';

// ── Visual walkthrough of the header (ds-7ag.3 / plan Tasks 5-6) ─────────────
// Same hand-run rig as the other *.visual.ts specs: the REAL Svelte app (vite
// dev) with every backend endpoint mocked, captured as PNGs.
//
// The widths are the point. The header was a WRAPPING flex row, so the nav's
// position was a function of how wide the actions cluster happened to be; the
// 861-1024px band is where that cluster fights the grid hardest, and 860px is
// the breakpoint where the nav takes a row of its own. 390 and 1440 bracket it.
//
// NOT wired into CI (no assertions; it produces frames). Run by hand:
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     header.visual.ts

const SHOTS = process.env.VISUAL_OUT ?? '/tmp/driftscribe-header-screens';
const WIDTHS = [390, 640, 760, 861, 900, 1024, 1440, 1600];

type Locale = 'en' | 'ja';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function seed(page: Page, locale: Locale) {
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', l);
    localStorage.setItem('driftscribe_tour_done', '1');
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
  groups: [],
  edges: [],
};

// Precise route patterns — see the note in wayfinding.visual.ts (a loose
// `**/conversations**` also matches vite's own /src/lib/conversations.ts).
async function mock(page: Page) {
  await page.route('**/infra/graph', (r) => json(r, GRAPH));
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: [] }));
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [] }));
  await page.route(/\/conversations\?/, (r) => json(r, { conversations: [] }));
  await page.route('**/autonomy', (r) =>
    json(r, { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false }),
  );
  await page.route('**/pause', (r) =>
    json(r, { paused: false, reason: null, actor: null, updated_at: null, read_error: false }),
  );
  await page.route('**/capabilities', (r) => json(r, { capabilities: [] }));
}

for (const locale of ['ja', 'en'] as const) {
  test(`header across widths — ${locale}`, async ({ page }) => {
    await seed(page, locale);
    await mock(page);

    // The desk is the front door, so its header is the one a judge meets first.
    // Chat is captured too: an all-healthy header there is where the utility
    // cluster is widest, and it is the view whose nav used to drift.
    for (const view of ['desk', 'chat'] as const) {
      await page.goto(`/?view=${view}`);
      await page.locator('.app-header').waitFor();
      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 420 });
        await page.waitForTimeout(250);
        await page.locator('.app-header').screenshot({
          path: `${SHOTS}/${locale}-${view}-${String(width).padStart(4, '0')}.png`,
        });
      }
    }
  });
}
