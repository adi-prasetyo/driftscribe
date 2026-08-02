import { test, expect, type Page, type Route } from '@playwright/test';
import { resolve } from 'node:path';

// ── Visual walkthrough of the approval desk (Task 3.6, step 1) ──────────────
// Drives the REAL Svelte app (vite dev) with every backend endpoint mocked,
// then captures a PNG of each desk state in BOTH locales so a human can eyeball
// them against docs/plans/2026-07-28-composite-mockup.html before Task 3.6 step
// 2 flips DEFAULT_VIEW to 'desk'.
//
// The desk has exactly three states (lib/desk.ts `deskModel`), and `pending`
// has two independent sources plus two provenance arms, so the shot list is:
//   1. resting            — the thesis screen ("nothing needs you")
//   2. pending / rollback  — Anchor proposing a fix (rule 1)
//   3. pending / iac 2a    — open-PR listing arm, carries a real PR title
//   4. pending / iac 2b    — decisions-derived arm, PR-number fallback headline
//   5. stamped            — the 判子 receipt, seal animation disabled for a
//                           deterministic frame
//
// Each state is produced ONLY by changing the three GETs the overview store
// owns (`/infra/graph`, `/infra/pending-approvals`, `/decisions?limit=50`) —
// exactly the data path the deployed app uses, so what you eyeball here is what
// prod renders. Screenshots land OUTSIDE the repo (scratchpad) so the branch
// stays clean.
//
// The 2026-07-31 merge changed what the shot list has to judge. Two states got
// materially smaller — `resting` and `unknown` slimmed to a one-line strip,
// because a half-screen of whitespace saying "nothing needs you" was most of
// what made the page read as empty — and the desk gained the estate section
// below the ledger. So each fixture now shoots three frames: the hero element,
// the viewport (how the hero sits in the shell), and the WHOLE page, which is
// the only one that can show whether band → hero → ledger → estate reads as one
// document or as four stacked panels.
//
// Run by hand (NOT wired into CI, same as crew-handoff.visual.ts):
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     desk.visual.ts

const SHOTS =
  process.env.VISUAL_OUT ??
  '/tmp/claude-1000/-home-adi-driftscribe/b3a6b226-5ab8-4b71-86c6-50f89fdecd72/scratchpad/desk-screens';

// The plan pins the desk shot at 1280×800 (the visual config's own default is
// 1280×900 for the two-column chat view; the desk is a centered 780px column).
const VIEWPORT = { width: 1280, height: 800 };

// `?view=desk` is required until Task 3.6 step 2 flips DEFAULT_VIEW — and it
// stays valid after that flip, so this rig does not need editing then.
const DESK_URL = '/?view=desk';

type Locale = 'en' | 'ja';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

// Seed the operator token (sessionStorage, as the deployed app does) AND the
// locale (localStorage key `driftscribe.locale`, i18n.ts:17) before any page
// script runs, so the SPA mounts straight past the auth gate already in the
// locale under test — i18n.detectInitial() defaults to 'ja', so EN must be set
// explicitly rather than assumed.
async function seed(page: Page, locale: Locale) {
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', l);
  }, locale);
}

// ── graph: shared across states ────────────────────────────────────────────
// `totals.resources` feeds the resting line's 「{n} リソース」 and, via
// scopeTotals(), the instrument band's managed/drift numerals.
function graphBody(opts: { generatedAt: string | null; drift: number }) {
  const driftNodes = Array.from({ length: opts.drift }, (_, i) => ({
    id: `g1n${i}`,
    label: `unmanaged-svc-${i + 1}`,
    asset_type: 'run.googleapis.com/Service',
    managed: false,
    location: 'asia-northeast1',
  }));
  return {
    generated_at: opts.generatedAt,
    project: 'driftscribe-hack-2026',
    caveat: null,
    iac_snapshot_sha: 'cafef00d',
    // ds-1vn: a healthy deployment's snapshot matches. Explicit, not omitted —
    // absent means "unverified", which draws a freshness notice and mutes the
    // Adopt controls in frames that are not about freshness at all.
    iac_snapshot_stale: false,
    degraded: false,
    degraded_reason: null,
    totals: { resources: 9 + opts.drift, managed: 9, drift: opts.drift },
    groups: [
      {
        asset_type: 'run.googleapis.com/Service',
        label: 'Cloud Run service',
        adoptable: true,
        count: 9 + opts.drift,
        managed: 9,
        drift: opts.drift,
        sensitive: false,
        nodes: [
          ...Array.from({ length: 9 }, (_, i) => ({
            id: `g0n${i}`,
            label: `payment-demo-${i + 1}`,
            asset_type: 'run.googleapis.com/Service',
            managed: true,
            location: 'asia-northeast1',
          })),
          ...driftNodes,
        ],
      },
    ],
    edges: [],
    truncated: { per_type_sample: 10 },
  };
}

// A representative env-var drift so DriftDiffCard has real rows to render on
// the pending/stamped cards (the desk restyles that card in place).
const DIFFS = [
  {
    name: 'LOG_LEVEL',
    expected: 'info',
    live: 'debug',
    contract_status: 'present_disallow_manual',
  },
  { name: 'FEATURE_FLAG_X', expected: null, live: 'on', contract_status: 'absent' },
];

// ── decisions fixtures ─────────────────────────────────────────────────────
// The stamped window is 10 minutes from the REAL clock (desk.ts
// STAMP_WINDOW_MS), so its timestamps must be generated at run time — a
// hard-coded ISO string would fall out of the window and silently shoot the
// resting state instead. `minsAgo` keeps that explicit at each call site.
function isoMinsAgo(mins: number): string {
  return new Date(Date.now() - mins * 60_000).toISOString();
}

// Rule 1 (pending rollback): a decision whose approval is still `pending`, not
// expired, with a same-origin /approvals/ href. Relative URL on purpose — it
// resolves against whatever port the dev server got, so safeApprovalHref's
// same-origin check passes without hard-coding 127.0.0.1:5199.
function rollbackPending() {
  return {
    decision_id: 'dec-visual-rollback',
    trace_id: 'aaaa1111aaaa1111aaaa1111aaaa1111',
    action: 'rollback',
    created_at: isoMinsAgo(4),
    diffs: DIFFS,
    approval: {
      approval_id: 'appr-visual-0001',
      approval_url: '/approvals/appr-visual-0001?t=visualtoken',
      status: 'pending',
      expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
      resolved_at: null,
    },
  };
}

// Rule 2b (pending iac, decisions-derived): merged but still awaiting the
// operator's post-rebake Apply, so it has dropped out of the open-PR listing.
// Deliberately carries NO pr_title — this is the arm that must fall back to the
// PR-number headline, and the shot exists to prove that fallback reads well.
function iacPendingFromDecision() {
  return {
    decision_id: 'dec-visual-iac-2b',
    trace_id: 'bbbb2222bbbb2222bbbb2222bbbb2222',
    action: 'iac_apply',
    created_at: isoMinsAgo(11),
    apply_status: 'waiting_for_rebake',
    merge_state: 'merged',
    pr_number: 312,
    head_sha: 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
    diffs: DIFFS,
  };
}

// Rule 3 (stamped), ROLLBACK lane (ds-2mc). Requires `phase: 'applied'` — a
// confirmed traffic shift — on top of `status: 'used'`. The pre-existing
// `stamped` fixture exercises only the iac lane, so without this one the
// rollback seal gate has no visual coverage at all.
function rollbackApplied() {
  return {
    decision_id: 'dec-visual-rb-applied',
    trace_id: 'dddd4444dddd4444dddd4444dddd4444',
    action: 'rollback',
    created_at: isoMinsAgo(9),
    diffs: DIFFS,
    approval: {
      approval_id: 'appr-visual-0002',
      approval_url: '/approvals/appr-visual-0002?t=visualtoken',
      status: 'used',
      phase: 'applied',
      resolved_at: isoMinsAgo(2),
    },
  };
}

// Rule 2.5 (unresolved). `phase` is the ONLY difference between these two, and
// they must not read alike: one states a fact, the other states an absence of
// knowledge. Both shots exist so the distinction can be eyeballed in both
// locales rather than trusted to the copy keys.
function rollbackUnresolved(phase: 'failed' | 'outcome_unknown') {
  return {
    decision_id: `dec-visual-rb-${phase}`,
    trace_id: 'eeee5555eeee5555eeee5555eeee5555',
    action: 'rollback',
    created_at: isoMinsAgo(6),
    diffs: DIFFS,
    approval: {
      approval_id: 'appr-visual-0003',
      approval_url: '/approvals/appr-visual-0003?t=visualtoken',
      status: 'used',
      phase,
      // No resolved_at: it means confirmed success and these are not that.
      resolved_at: null,
    },
  };
}

// Rule 3 (stamped), iac lane: keyed off `applied_at`, NOT created_at.
function iacApplied() {
  return {
    decision_id: 'dec-visual-stamped',
    trace_id: 'cccc3333cccc3333cccc3333cccc3333',
    action: 'iac_apply',
    created_at: isoMinsAgo(9),
    applied_at: isoMinsAgo(2),
    apply_status: 'applied',
    merge_state: 'merged',
    pr_number: 308,
    pr_title: 'Adopt payment-demo Cloud Run service into IaC',
    head_sha: 'feedfacefeedfacefeedfacefeedfacefeedface',
    diffs: DIFFS,
  };
}

// Ledger filler: resolved rows that are NOT stamped candidates (their
// resolved_at/applied_at is well outside the 10-minute window), so the ledger
// strip has history to render without dragging the desk into `stamped`.
function ledgerHistory() {
  return [
    {
      decision_id: 'dec-visual-hist-1',
      action: 'iac_apply',
      created_at: isoMinsAgo(140),
      applied_at: isoMinsAgo(138),
      apply_status: 'applied',
      pr_number: 301,
      pr_title: 'Pin lodash to 4.17.21',
      merge_state: 'merged',
    },
    // A `no_op` row — the "checked, all clear" receipt. The ONLY action
    // decisionActionLabel maps to prose, so this row is the control against
    // the one below.
    {
      decision_id: 'dec-visual-hist-2',
      action: 'no_op',
      created_at: isoMinsAgo(190),
    },
    // A rollback whose approval EXPIRED unused: classified `noted` (a record,
    // not a solicitation). Its title falls to decisionActionLabel, which maps
    // only `no_op` and passes every other action through raw — so this row is
    // the probe for whether a bare `rollback` enum reaches the desk.
    {
      decision_id: 'dec-visual-hist-4',
      action: 'rollback',
      created_at: isoMinsAgo(220),
      approval: {
        approval_id: 'appr-visual-expired',
        approval_url: '/approvals/appr-visual-expired?t=expiredtoken',
        status: 'pending',
        expires_at: isoMinsAgo(200),
        resolved_at: null,
      },
    },
    {
      decision_id: 'dec-visual-hist-3',
      action: 'rollback',
      created_at: isoMinsAgo(260),
      approval: {
        approval_id: 'appr-visual-old',
        approval_url: '/approvals/appr-visual-old?t=oldtoken',
        status: 'used',
        resolved_at: isoMinsAgo(255),
      },
    },
  ];
}

interface DeskFixture {
  decisions: unknown[];
  pendingApprovals: unknown[];
  generatedAt: string | null;
  drift: number;
}

// The three GETs the overview store owns (lib/overviewStore.ts:105/116/128) —
// nothing else distinguishes the desk states.
async function mockDesk(page: Page, fx: DeskFixture) {
  await page.route('**/infra/graph', (r) =>
    json(r, graphBody({ generatedAt: fx.generatedAt, drift: fx.drift })),
  );
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: fx.pendingApprovals }));
  // Regex, NOT `**/decisions**` — that glob also matches the vite source module
  // `/src/locales/decisions.ts`, and serving JSON for a JS module breaks the
  // whole app mount with a strict-MIME error, so the desk never renders at all.
  // Same class of trap as the `**/conversations**` one below, but a newer one:
  // the locale namespace file postdates the composer rig, which is why
  // that older rig could use the glob safely and this one cannot. Requiring
  // `?` or end-of-URL right after `/decisions` excludes any `.ts` module path.
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: fx.decisions }));

  // Shell endpoints the app mounts with, held constant across every state.
  await page.route('**/pause', (r) => json(r, { paused: false }));
  await page.route('**/autonomy', (r) =>
    json(r, { mode: 'propose_apply', reason: null, actor: null }),
  );
  await page.route('**/capabilities', (r) =>
    json(r, { version: 1, workloads: [], human_gates: [], denylist: { rules: [] } }),
  );
  // Regex, not `**/conversations**` — that glob also matches the vite source
  // module `/src/lib/conversations.ts`, and serving JSON for a JS module breaks
  // the whole app mount with a MIME error (learned in the composer rig).
  await page.route(/\/conversations\?/, (r) => json(r, { conversations: [] }));
}

const FIXTURES: Record<string, DeskFixture> = {
  // Nothing pending, nothing recently resolved: the product's promise kept.
  // `drift: 0` so the resting line's conditional 「新規ドリフトなし」 segment is
  // exercised — it renders ONLY when scope.drift === 0.
  resting: {
    decisions: ledgerHistory(),
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 0,
  },
  'pending-rollback': {
    decisions: [rollbackPending(), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 6,
  },
  // Rule 2a wins over 2b by rule ordering whenever the listing is non-empty.
  'pending-iac-listing': {
    decisions: ledgerHistory(),
    pendingApprovals: [
      {
        pr_number: 315,
        title: 'Adopt orders-topic Pub/Sub topic into IaC',
        url: 'https://github.com/adi-prasetyo/driftscribe/pull/315',
        asset_type: 'pubsub.googleapis.com/Topic',
        resource_name: 'orders-topic',
      },
    ],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 6,
  },
  // Empty listing forces the decisions-derived arm.
  'pending-iac-decision': {
    decisions: [iacPendingFromDecision(), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 6,
  },
  stamped: {
    decisions: [iacApplied(), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 5,
  },
  'stamped-rollback': {
    decisions: [rollbackApplied(), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 5,
  },
  'unresolved-failed': {
    decisions: [rollbackUnresolved('failed'), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 5,
  },
  'unresolved-unknown': {
    decisions: [rollbackUnresolved('outcome_unknown'), ...ledgerHistory()],
    pendingApprovals: [],
    generatedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
    drift: 5,
  },
};

// The state each fixture must actually produce. Asserted before the shot so a
// fixture that silently falls through to `resting` (an expired approval, a
// stamped timestamp aged out of its window) fails loudly instead of quietly
// handing back five identical screenshots of the calm state.
const EXPECTED_STATE: Record<string, 'resting' | 'pending' | 'stamped' | 'unresolved'> = {
  resting: 'resting',
  'pending-rollback': 'pending',
  'pending-iac-listing': 'pending',
  'pending-iac-decision': 'pending',
  stamped: 'stamped',
  // A rollback seals ONLY on a confirmed apply. If the phase gate regressed to
  // sealing on `status: used` alone this would still pass — but the two
  // `unresolved` rows below would then flip to `stamped` and fail loudly,
  // which is the actual guard.
  'stamped-rollback': 'stamped',
  'unresolved-failed': 'unresolved',
  'unresolved-unknown': 'unresolved',
};

for (const locale of ['en', 'ja'] as Locale[]) {
  for (const [name, fx] of Object.entries(FIXTURES)) {
    test(`desk — ${name} (${locale})`, async ({ page }) => {
      await page.setViewportSize(VIEWPORT);
      await seed(page, locale);
      await mockDesk(page, fx);
      await page.goto(DESK_URL);

      const desk = page.getByTestId('approval-desk');
      await expect(desk).toBeVisible();

      // The notice no longer auto-opens here: ds-5yq suppressed it on the desk
      // (and ds-2co extended that to every non-chat view) precisely because it
      // floated over the hero — the finding this comment used to record has
      // since been fixed. The dismiss is kept as a belt-and-braces no-op so the
      // shots stay deterministic if that suppression ever regresses.
      const dismiss = page.getByTestId('demo-notice-dismiss');
      if (await dismiss.isVisible()) await dismiss.click();

      // Prove the fixture landed in the state it claims before shooting it.
      await expect(page.getByTestId('approval-desk-state')).toHaveAttribute(
        'data-state',
        EXPECTED_STATE[name],
      );

      // `animations: 'disabled'` also freezes the seal's stampIn keyframe at its
      // end frame, which is the resting picture by construction (SealStamp pins
      // stampIn's 100% opacity to the same --seal-rest-opacity as its base rule),
      // so the stamped shot is deterministic AND correct rather than a caught
      // mid-stamp frame.
      await desk.screenshot({
        path: resolve(SHOTS, `${locale}-${name}.png`),
        animations: 'disabled',
      });

      // Full viewport too: the desk is a centered 780px column, and how it sits
      // in the 1280px shell (header nav, no rails) is half of what Task 3.6 is
      // verifying. Post-merge this is also the ABOVE-THE-FOLD test — whether
      // the first screen still leads with the operator's decision now that the
      // estate shares the page.
      await page.screenshot({
        path: resolve(SHOTS, `${locale}-${name}-full.png`),
        animations: 'disabled',
      });

      // The whole landing page in one frame. This is the merge's actual
      // deliverable and the only frame that can show it: whether the four bands
      // read as one document, and where the estate section sits relative to the
      // fold in each hero state (a pending hero pushes it further down than a
      // resting one, which is the point of slimming resting).
      await page.screenshot({
        path: resolve(SHOTS, `${locale}-${name}-page.png`),
        animations: 'disabled',
        fullPage: true,
      });
    });
  }
}
