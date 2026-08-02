import { test, expect, type Page, type Route } from '@playwright/test';
import { resolve } from 'node:path';

// ── Visual walkthrough of the estate SECTION (Task 4.1) ─────────────────────
// Same rig shape as desk.visual.ts: drives the REAL Svelte app (vite dev) with
// every backend endpoint mocked, then captures a PNG in BOTH locales so a human
// can eyeball screen two against docs/plans/2026-07-28-composite-mockup.html
// ("SCREEN 2 — 推定図", lines 360-392).
//
// The 2026-07-31 merge made the estate a section of the desk rather than a view
// of its own. The rig entered through `/?view=estate`, which now ALIASES to the
// desk — so it would have kept running, kept passing, and kept photographing
// something other than what it claimed. It enters at the desk and scrolls to
// the section instead; the element-scoped screenshots below are unchanged,
// because a section screenshot never showed the surrounding chrome anyway.
//
// The shot list covers the states that change the view's SHAPE, not merely its
// numbers:
//   1. populated — drift rows (one already PR'd), managed rows, the system fold
//   2. all-clear — zero drift: the drift group must be absent, not an empty
//                  header, and the band's amber numeral falls to 0
//   3. degraded  — /infra/graph reports degraded: an honest line, never a
//                  fabricated "all managed" that would read as good news
//
// Everything is produced ONLY by changing the three GETs the overview store
// owns, so what you eyeball here is the same data path prod renders.
// Screenshots land OUTSIDE the repo (scratchpad) so the branch stays clean.
//
// Run by hand (NOT wired into CI, same as the sibling rigs):
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     estate.visual.ts

const SHOTS =
  process.env.VISUAL_OUT ??
  '/tmp/claude-1000/-home-adi-driftscribe/b84d6b6e-d1da-4ea2-a670-7d8432ad1683/scratchpad/estate-screens';

// Matches the desk rig: the estate section is the same centered 780px column.
const VIEWPORT = { width: 1280, height: 800 };

// The desk IS the estate's page now. Deliberately not `/?view=estate` — that
// still resolves here through the legacy alias, which is exactly why using it
// would hide a regression rather than catch one.
const ESTATE_URL = '/';

type Locale = 'en' | 'ja';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function seed(page: Page, locale: Locale) {
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', l);
  }, locale);
}

// Node helper — `control_plane: true` rows are the ones that must land in the
// folded system-managed disclosure rather than the inline rows.
function node(
  id: string,
  label: string,
  assetType: string,
  managed: boolean,
  controlPlane = false,
) {
  return {
    id,
    label,
    asset_type: assetType,
    managed,
    control_plane: controlPlane,
    location: 'asia-northeast1',
  };
}

// A realistic multi-type estate mirroring the mockup: drift spread across three
// adoptable types, nine managed resources, and a pile of control-plane rows that
// must stay folded. `drift_adoptable` is set explicitly on every group — leaving
// it off would make resourceCards() fall back to raw `drift` and silently change
// what the band and the "N drift" grouping report.
// IaC declarations with no live resource (ds-zld moved this group here from
// InfraDiagram's chat panel). Two entries against a `truncated: 1`, so the shot
// covers both the row shape and the "+N not shown" trailer, and a long HCL
// address that has to wrap under its name rather than push the Investigate
// button off a card `.estate-view` would silently CLIP (overflow:hidden).
const UNMATCHED = {
  count: 3,
  truncated: 1,
  entries: [
    {
      id: 'u0',
      asset_type: 'run.googleapis.com/Service',
      type_label: 'Cloud Run service',
      label: 'storefront-old',
      address: 'google_cloud_run_v2_service.storefront_old',
    },
    {
      id: 'u1',
      asset_type: 'pubsub.googleapis.com/Subscription',
      type_label: 'Pub/Sub subscription',
      label: 'orders-retry-subscription-legacy',
      address: 'module.messaging.google_pubsub_subscription.orders_retry_subscription_legacy',
    },
  ],
};

function graphBody(opts: {
  drift: boolean;
  degraded?: boolean;
  unmatched?: boolean;
  /** ds-1vn — undefined means a fresh snapshot (no notice). `true` is the
   *  stale banner; `null` is the quieter "could not verify" line. */
  snapshotStale?: boolean | null;
}) {
  if (opts.degraded) {
    return {
      generated_at: null,
      project: 'driftscribe-hack-2026',
      caveat: null,
      iac_snapshot_sha: null,
      degraded: true,
      degraded_reason: 'Cloud Asset Inventory export unavailable',
      totals: { resources: 0, managed: 0, drift: 0 },
      groups: [],
      edges: [],
      truncated: { per_type_sample: 10 },
      // Carried on the degraded payload ON PURPOSE. A degraded read's
      // declaration list is as untrustworthy as its resource list, so the
      // interesting case is a body that HAS declarations and still renders
      // none — an empty payload here would pass for the wrong reason.
      ...(opts.unmatched ? { unmatched_declarations: UNMATCHED } : {}),
    };
  }
  const driftTopics = opts.drift ? [node('t9', 'shipping-topic', 'pubsub.googleapis.com/Topic', false)] : [];
  const driftSubs = opts.drift
    ? [node('s9', 'shipping-sub', 'pubsub.googleapis.com/Subscription', false)]
    : [];
  // `receipts` is the row that already has an open adoption PR (#268) — it must
  // render the non-interactive "PR #268 reviewing" chip INSTEAD of an Adopt
  // button, and must never be the tour's adopt-target.
  const driftBuckets = opts.drift ? [node('b9', 'receipts', 'storage.googleapis.com/Bucket', false)] : [];
  const driftCount = opts.drift ? 1 : 0;

  return {
    generated_at: new Date(Date.now() - 3 * 60_000).toISOString(),
    project: 'driftscribe-hack-2026',
    caveat: null,
    iac_snapshot_sha: 'cafef00d',
    // ds-1vn. Default EXPLICITLY fresh: an omitted field means "unverified",
    // which would put the freshness line into every unrelated estate shot and
    // stop those shots representing the state they are named for.
    // `=== undefined`, NOT `?? false` — `??` treats null as nullish, so a
    // deliberate `null` ("could not verify") would collapse into `false`
    // ("verified fresh"). That is the exact conflation ds-1vn exists to stop,
    // and it appeared here first, in the fixture helper.
    iac_snapshot_stale: opts.snapshotStale === undefined ? false : opts.snapshotStale,
    iac_snapshot_reason: opts.snapshotStale === true ? 'tree_hash_mismatch' : null,
    degraded: false,
    degraded_reason: null,
    totals: { resources: 735, managed: 9, drift: opts.drift ? 3 : 0 },
    groups: [
      {
        asset_type: 'pubsub.googleapis.com/Topic',
        label: 'Pub/Sub topic',
        adoptable: true,
        count: 2 + driftCount,
        managed: 2,
        drift: driftCount,
        drift_adoptable: driftCount,
        sensitive: false,
        nodes: [
          node('t1', 'audit-topic', 'pubsub.googleapis.com/Topic', true),
          node('t2', 'drift-events', 'pubsub.googleapis.com/Topic', true),
          ...driftTopics,
        ],
      },
      {
        asset_type: 'pubsub.googleapis.com/Subscription',
        label: 'Pub/Sub subscription',
        adoptable: true,
        count: 2 + driftCount,
        managed: 2,
        drift: driftCount,
        drift_adoptable: driftCount,
        sensitive: false,
        nodes: [
          node('s1', 'audit-sub', 'pubsub.googleapis.com/Subscription', true),
          node('s2', 'adopt-probe-sub', 'pubsub.googleapis.com/Subscription', true),
          ...driftSubs,
        ],
      },
      {
        asset_type: 'storage.googleapis.com/Bucket',
        label: 'Cloud Storage bucket',
        adoptable: true,
        // Two control-plane buckets (Google-auto-created) alongside the drift
        // one: they must fold, never appear as adoptable amber rows.
        count: 3 + driftCount,
        managed: 1,
        drift: driftCount + 2,
        drift_adoptable: driftCount,
        sensitive: false,
        nodes: [
          node('b1', 'driftscribe-tofu-state', 'storage.googleapis.com/Bucket', true),
          node('b2', 'gcf-sources-1234', 'storage.googleapis.com/Bucket', false, true),
          node('b3', 'driftscribe-tofu-artifacts', 'storage.googleapis.com/Bucket', false, true),
          ...driftBuckets,
        ],
      },
      {
        asset_type: 'run.googleapis.com/Service',
        label: 'Cloud Run service',
        adoptable: true,
        count: 6,
        managed: 4,
        drift: 2,
        drift_adoptable: 0, // both unmanaged Run services are DriftScribe's own
        sensitive: false,
        nodes: [
          node('r1', 'driftscribe-agent', 'run.googleapis.com/Service', true),
          node('r2', 'infra-reader', 'run.googleapis.com/Service', true),
          node('r3', 'rollback-worker', 'run.googleapis.com/Service', true),
          node('r4', 'upgrade-worker', 'run.googleapis.com/Service', true),
          node('r5', 'eventarc-trigger-sa', 'run.googleapis.com/Service', false, true),
          node('r6', 'cloud-build-runner', 'run.googleapis.com/Service', false, true),
        ],
      },
      {
        // A counts-only sensitive card — must never list row labels.
        asset_type: 'secretmanager.googleapis.com/Secret',
        label: 'Secret',
        adoptable: false,
        count: 4,
        managed: 0,
        drift: 4,
        sensitive: true,
        nodes: [],
      },
      {
        // Feeds the UNTRACKED fold, which nothing else in this rig produced.
        // Non-adoptable TYPE with a managed member, so isPrimaryCard's
        // `managed > 0` arm keeps the card in the default view while its two
        // unmanaged rows classify as `untracked` — neutral, never amber, never
        // an Adopt button. That is the exact combination the fold exists for.
        asset_type: 'iam.googleapis.com/ServiceAccount',
        label: 'Service account',
        adoptable: false,
        count: 3,
        managed: 1,
        drift: 2,
        sensitive: false,
        nodes: [
          node('sa1', 'driftscribe-agent-sa', 'iam.googleapis.com/ServiceAccount', true),
          node('sa2', 'legacy-deploy-sa', 'iam.googleapis.com/ServiceAccount', false),
          node('sa3', 'compute-default-sa', 'iam.googleapis.com/ServiceAccount', false),
        ],
      },
    ],
    edges: [],
    truncated: { per_type_sample: 10 },
    ...(opts.unmatched ? { unmatched_declarations: UNMATCHED } : {}),
  };
}

// The open adoption PR that turns `receipts` into a "reviewing" chip.
const PENDING_APPROVALS = [
  {
    pr_number: 268,
    title: 'Adopt Cloud Storage bucket receipts',
    url: 'https://github.com/example/driftscribe/pull/268',
    asset_type: 'storage.googleapis.com/Bucket',
    resource_name: 'receipts',
  },
];

interface Fixture {
  drift: boolean;
  degraded?: boolean;
  unmatched?: boolean;
  pendingApprovals: typeof PENDING_APPROVALS | [];
  snapshotStale?: boolean | null;
}

const FIXTURES: Record<string, Fixture> = {
  populated: { drift: true, unmatched: true, pendingApprovals: PENDING_APPROVALS },
  // Deliberately NO declarations here: "all clear" has to mean all clear, and a
  // stray unmatched group under a zero-drift estate is exactly the kind of
  // leftover that would go unnoticed in a shot nobody diffs.
  'all-clear': { drift: false, pendingApprovals: [] },
  degraded: { drift: false, degraded: true, unmatched: true, pendingApprovals: [] },
  // ds-1vn — the estate still renders in full, with the notice ABOVE the rows
  // it qualifies. The whole point is that the warning does not hide its
  // subject: an operator must be able to read the drift rows AND know the
  // snapshot they came from is behind this deployment.
  'stale-snapshot': {
    drift: true,
    unmatched: true,
    pendingApprovals: PENDING_APPROVALS,
    snapshotStale: true,
  },
  'unverified-snapshot': {
    drift: true,
    unmatched: true,
    pendingApprovals: PENDING_APPROVALS,
    snapshotStale: null,
  },
};

async function mockEstate(page: Page, fx: Fixture) {
  // Regex, NOT `**/decisions**` — that glob also matches the vite source module
  // `/src/locales/decisions.ts`, serving JSON for a JS module and breaking the
  // whole app mount with a strict-MIME error (the trap desk.visual.ts documents).
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [] }));
  await page.route('**/pause', (r) => json(r, { paused: false }));
  await page.route('**/autonomy', (r) =>
    json(r, { mode: 'propose_apply', reason: null, actor: null }),
  );
  await page.route('**/capabilities', (r) =>
    json(r, { version: 1, workloads: [], human_gates: [], denylist: { rules: [] } }),
  );
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: fx.pendingApprovals }));
  await page.route('**/infra/graph', (r) =>
    json(
      r,
      graphBody({
        drift: fx.drift,
        degraded: fx.degraded,
        unmatched: fx.unmatched,
        snapshotStale: fx.snapshotStale,
      }),
    ),
  );
  await page.route(/\/conversations(\?|$)/, (r) => json(r, { conversations: [] }));
}

for (const locale of ['en', 'ja'] as Locale[]) {
  for (const [name, fx] of Object.entries(FIXTURES)) {
    test(`estate — ${name} (${locale})`, async ({ page }) => {
      await page.setViewportSize(VIEWPORT);
      await seed(page, locale);
      await mockEstate(page, fx);
      await page.goto(ESTATE_URL);

      const estate = page.getByTestId('estate-view');
      await expect(estate).toBeVisible();

      // Same first-visit popover the desk rig dismisses — it floats over the
      // top-left and would otherwise sit in every shot.
      const dismiss = page.getByTestId('demo-notice-dismiss');
      if (await dismiss.isVisible()) await dismiss.click();

      // The section sits below the hero and the ledger now, so bring it into
      // view before shooting. This also renders the same landing an operator
      // gets from a band numeral, minus the focus ring.
      await estate.scrollIntoViewIfNeeded();

      // Prove each fixture actually produced the SHAPE it claims before
      // shooting it — otherwise a fixture that silently collapsed to the empty
      // model hands back three near-identical screenshots and the rig looks
      // green while showing nothing.
      // ds-1vn — same "prove the fixture produced its shape" rule as below.
      // A stale/unverified shot that captured a quiet estate would look fine
      // in review and pin nothing.
      const staleNote = page.getByTestId('estate-snapshot-stale');
      const unverifiedNote = page.getByTestId('estate-snapshot-unverified');
      if (fx.degraded) {
        // Degraded already replaces the estate; no freshness line rides along.
        await expect(staleNote).toHaveCount(0);
        await expect(unverifiedNote).toHaveCount(0);
      } else if (fx.snapshotStale === true) {
        await expect(staleNote).toBeVisible();
        await expect(unverifiedNote).toHaveCount(0);
      } else if (fx.snapshotStale === null) {
        await expect(unverifiedNote).toBeVisible();
        await expect(staleNote).toHaveCount(0);
      } else {
        await expect(staleNote).toHaveCount(0);
        await expect(unverifiedNote).toHaveCount(0);
      }

      if (fx.degraded) {
        await expect(page.getByTestId('estate-degraded')).toBeVisible();
        await expect(page.getByTestId('estate-row')).toHaveCount(0);
        // …and the declarations the payload DID carry are gone with them.
        await expect(page.getByTestId('estate-unmatched-row')).toHaveCount(0);
        await expect(page.getByTestId('estate-group-unmatched')).toHaveCount(0);
      } else if (fx.drift) {
        await expect(page.getByTestId('estate-group-drift')).toBeVisible();
        // `receipts` has an open PR, so exactly one PR chip and one FEWER
        // adopt button than drift rows — this is the assertion that pins the
        // "one action per row, and a PR'd row is not adoptable" rule.
        await expect(page.getByTestId('estate-pr-chip')).toHaveCount(1);
        if (fx.snapshotStale === true) {
          // ds-1vn r2: a stale snapshot suppresses adoption — "not declared"
          // is read off a tree we just proved is not this deployment's, and an
          // unsupported absence must not drive an action. The rows stay.
          await expect(page.getByTestId('estate-adopt-btn')).toHaveCount(0);
          await expect(page.getByTestId('estate-adopt-stale')).toHaveCount(2);
        } else {
          await expect(page.getByTestId('estate-adopt-btn')).toHaveCount(2);
          await expect(page.getByTestId('estate-adopt-stale')).toHaveCount(0);
        }
        await expect(page.getByTestId('estate-system-fold')).toBeVisible();
        // The merge's density concession: the untracked group folds shut, so
        // its COUNT is what the section spends space on. Assert it is closed —
        // a fold that ships open is the same wall of rows under a new tag.
        const untracked = page.getByTestId('estate-untracked-fold');
        await expect(untracked).toBeVisible();
        expect(await untracked.evaluate((el) => (el as HTMLDetailsElement).open)).toBe(false);
        // The two untracked service accounts must NOT have picked up an Adopt
        // button on the way in — the count above already pins that globally,
        // but this says which rows it is about.
        await expect(untracked.getByTestId('estate-adopt-btn')).toHaveCount(0);

        // The unmatched-declarations group (ds-zld). Its old home measured the
        // band against its OWN box for a reason worth restating: `.estate-view`
        // is `overflow: hidden`, so a control pushed past the card's right edge
        // is CLIPPED rather than scrolled to, and a document-level
        // scrollWidth === clientWidth check calls the page clean while the
        // Investigate button sits half off the card (ds-cmc). Measure the
        // descendants against the card.
        await expect(page.getByTestId('estate-group-unmatched')).toBeVisible();
        await expect(page.getByTestId('estate-unmatched-row')).toHaveCount(2);
        // Heading carries the SERVER count (3), not the two rows shown, and the
        // trailer states the difference rather than dropping it silently.
        await expect(page.getByTestId('estate-group-unmatched')).toContainText('3');
        await expect(page.getByTestId('estate-unmatched-more')).toBeVisible();
        // No status dot is PAINTED on a declaration row: it is not a live
        // resource, so neither the managed fill nor the drift ring applies.
        // jsdom pins the class; only a browser can say what it renders.
        const dotPaint = await page
          .getByTestId('estate-unmatched-row')
          .first()
          .locator('.estate-view__dot')
          .evaluate((el) => {
            const s = getComputedStyle(el);
            return { bg: s.backgroundColor, borderWidth: s.borderTopWidth };
          });
        expect(dotPaint.bg).toBe('rgba(0, 0, 0, 0)');
        expect(parseFloat(dotPaint.borderWidth)).toBe(0);

        for (const vp of [
          { width: 1280, height: 800 },
          { width: 768, height: 900 },
          { width: 390, height: 844 },
        ]) {
          await page.setViewportSize(vp);
          const overflow = await estate.evaluate((el) => {
            const right = el.getBoundingClientRect().right;
            return Math.max(
              0,
              ...[...el.querySelectorAll('[data-testid="estate-unmatched-row"] *')].map(
                (c) => c.getBoundingClientRect().right - right,
              ),
            );
          });
          expect(
            overflow,
            `declaration row content overflows the estate card at ${vp.width}px`,
          ).toBeLessThanOrEqual(1);
        }
        await page.setViewportSize(VIEWPORT);
      } else {
        // Zero drift: the group header must be ABSENT, not rendered empty.
        await expect(page.getByTestId('estate-group-drift')).toHaveCount(0);
        await expect(page.getByTestId('estate-adopt-btn')).toHaveCount(0);
        await expect(page.getByTestId('estate-group-managed')).toBeVisible();
        await expect(page.getByTestId('estate-group-unmatched')).toHaveCount(0);
      }

      await estate.screenshot({
        path: resolve(SHOTS, `${locale}-${name}.png`),
        animations: 'disabled',
      });

      // Second frame for the populated case only: both folds open. The resting
      // shot above proves the section is calm; this one proves the detail is
      // still legible once asked for, which is the half a fold can get wrong.
      if (fx.drift) {
        for (const id of ['estate-untracked-fold', 'estate-system-fold']) {
          await page.getByTestId(id).locator('summary').click();
        }
        await estate.screenshot({
          path: resolve(SHOTS, `${locale}-${name}-folds-open.png`),
          animations: 'disabled',
        });
      }
    });
  }
}
