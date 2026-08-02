import { test, expect, type Page, type Route } from '@playwright/test';

// ── Visual walkthrough of the wayfinding polish (ds-7ag.2 / plan Tasks 3-4) ──
// Same hand-run rig as desk.visual.ts / estate.visual.ts: drives the REAL
// Svelte app (vite dev) with every backend endpoint mocked, and captures PNGs
// so a human can eyeball what jsdom cannot render — the stat numerals' hover
// hint, and where a numeral actually takes you — in BOTH locales.
//
// Reframed by the 2026-07-31 desk+estate merge. The frames that used to cross
// between two views (the estate's inert figures, the quiet back-to-desk link)
// have nothing to photograph any more: there is one page, the numerals scroll
// within it, and the back link is gone. What replaces them is the gesture
// itself — the scroll landing and its focus ring — which is the thing a still
// frame can genuinely judge and jsdom cannot.
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

function node(id: string, label: string, assetType: string, managed: boolean) {
  return { id, label, asset_type: assetType, managed, location: 'asia-northeast1' };
}

// Deliberately BIGGER than this rig needs for the band alone. Frames 5-7 below
// photograph a scroll, and a scroll needs a page taller than the viewport: with
// the single two-node bucket group this spec used to carry, the estate section
// fit entirely above the fold, clicking a numeral moved nothing, and the three
// "scrolled" frames came back byte-identical to the resting one. The spec ran
// green and showed nothing — the same trap estate.visual.ts guards against by
// asserting each fixture's SHAPE before shooting it. The assertions in the test
// body are the actual fix; this fixture is what lets them hold.
const GRAPH = {
  generated_at: '2026-07-30T02:00:00Z',
  project: 'driftscribe-hack-2026',
  caveat: '',
  // ds-1vn: a healthy deployment's snapshot matches. Explicit, not omitted —
  // absent means "unverified", which draws a freshness notice and mutes the
  // Adopt controls in frames that are not about freshness at all.
  iac_snapshot_stale: false,
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
      drift_adoptable: 1,
      sensitive: false,
      adoptable: true,
      nodes: [
        node('b0', 'ds-receipts-2026', 'storage.googleapis.com/Bucket', false),
        node('b1', 'ds-artifacts', 'storage.googleapis.com/Bucket', true),
      ],
    },
    {
      asset_type: 'pubsub.googleapis.com/Topic',
      label: 'Pub/Sub topic',
      count: 4,
      managed: 3,
      drift: 1,
      drift_adoptable: 1,
      sensitive: false,
      adoptable: true,
      nodes: [
        node('t0', 'shipping-topic', 'pubsub.googleapis.com/Topic', false),
        node('t1', 'audit-topic', 'pubsub.googleapis.com/Topic', true),
        node('t2', 'drift-events', 'pubsub.googleapis.com/Topic', true),
        node('t3', 'adopt-probe-topic', 'pubsub.googleapis.com/Topic', true),
      ],
    },
    {
      // Non-adoptable TYPE with a managed member: primary card, and its two
      // unmanaged rows land in the UNTRACKED fold (frame 7's subject).
      asset_type: 'iam.googleapis.com/ServiceAccount',
      label: 'Service account',
      count: 3,
      managed: 1,
      drift: 2,
      sensitive: false,
      adoptable: false,
      nodes: [
        node('sa1', 'driftscribe-agent-sa', 'iam.googleapis.com/ServiceAccount', true),
        node('sa2', 'legacy-deploy-sa', 'iam.googleapis.com/ServiceAccount', false),
        node('sa3', 'compute-default-sa', 'iam.googleapis.com/ServiceAccount', false),
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
  test(`desk wayfinding — ${locale}`, async ({ page }) => {
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

    // 3. Hovering awaiting must change NOTHING — it is an inert figure whose
    //    subject (the queue) is already on screen below it (ds-s61). The label
    //    must not fade, and no hint may appear in its place.
    await page.getByTestId('instrument-band-awaiting').hover();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-3-desk-hover-awaiting-inert.png` });

    // 4. Keyboard focus shows the same hint (focus-visible, not just hover).
    await page.getByTestId('instrument-band-managed').focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-4-desk-focus-managed.png` });

    // 5. The gesture the merge changed: clicking drift SCROLLS to the estate
    //    section of this same page instead of navigating. Judge two things in
    //    this frame — that the section is what landed under the viewport top,
    //    and that focus went with it (the section takes tabindex="-1" and
    //    suppresses its own outline, so what you should see is a normal
    //    section heading, NOT a stray focus ring).
    //    The scroll must actually MOVE the page, or this frame and the two
    //    below come back identical to the resting one and the rig reports
    //    success while showing nothing. Asserted, not assumed.
    await page.getByTestId('instrument-band-drift').click();
    await page.waitForTimeout(700); // smooth scroll settles
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    await page.screenshot({ path: `${SHOTS}/${locale}-5-scrolled-to-estate.png` });

    // 6. The whole landing in one frame — band, hero, ledger, estate. Frame 5
    //    proves the gesture arrives; this one is where you judge whether it
    //    arrived somewhere that belongs to the same document. Deliberately
    //    fullPage: re-shooting the viewport here would only reproduce frame 5,
    //    since the click already left the section under the viewport top.
    await page.screenshot({ path: `${SHOTS}/${locale}-6-landing-full.png`, fullPage: true });

    // 7. The untracked group is folded shut by default — the merge's one
    //    density concession. Open it and check the disclosure reads as a
    //    deliberate control rather than a collapsed row. `expect` rather than an
    //    `if (count)` guard: a fixture that stopped producing untracked rows
    //    would otherwise skip this frame in silence.
    // ds-1vn (Codex r4). These frames are about wayfinding, not freshness, so
    // a stray "could not verify" banner and muted Adopt controls would quietly
    // change what every shot depicts — and this spec's assertions cover the
    // scroll and the fold, so the gate would stay green while the frames drifted.
    // Exactly the trap the header comment above describes, one field over.
    await expect(page.getByTestId('estate-snapshot-unverified')).toHaveCount(0);
    await expect(page.getByTestId('estate-snapshot-stale')).toHaveCount(0);
    await expect(page.getByTestId('estate-adopt-btn').first()).toBeVisible();

    const fold = page.getByTestId('estate-untracked-fold');
    await expect(fold).toBeVisible();
    await fold.locator('summary').click();
    await page.waitForTimeout(300);
    await fold.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${SHOTS}/${locale}-7-untracked-open.png` });
  });
}
