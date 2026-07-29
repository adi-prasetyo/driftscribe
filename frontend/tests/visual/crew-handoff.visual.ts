import { test, expect, type Page, type Route } from '@playwright/test';
import { resolve } from 'node:path';

// ── Visual walkthrough of the single-door crew handoff ──
// Drives the REAL Svelte app (vite dev) with every backend endpoint mocked, then
// captures a PNG at each state so a human can eyeball the feature:
//   1. fresh composer   — prompt + Send, and NO crew picker to get wrong
//   2. resumed thread   — Explore's reply plus the confirmation chip it earned
//   3. after confirming — the transition row, and Provision's first reply
//   4. after New chat   — clean slate, chip gone with the thread
//
// This replaces the composer crew-lock rig. The lock still exists; it stopped
// being something the operator can see or fight, so there is nothing left to
// photograph — the chip is what took its place on screen.
//
// Screenshots land OUTSIDE the repo (scratchpad) so the branch stays clean.

const SHOTS =
  process.env.VISUAL_OUT ??
  '/tmp/claude-1000/-home-adi-driftscribe/07adce54-8954-4f8c-b904-5c413e5b055c/scratchpad/crew-handoff-screens';

const CONVERSATION_ID = 'conv-visual-0001';
const TRACE_ID = 'abc123abc123abc123abc123abc12300';
const HANDOFF_REASON =
  'This needs a bucket created and put under IaC, which is Provision\u2019s job, not mine.';

// Seed the operator token the way the deployed app does (sessionStorage), before
// any page script runs, so the SPA mounts straight past the auth gate.
async function seedToken(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
  });
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

// One prior Explore conversation in the rail, holding an OPEN handoff proposal:
// clicking it resumes the thread and renders the confirmation chip. `redeemed`
// flips when POST /chat/handoff lands, so the same detail route serves the
// before and after states — which is also how the real refetch sees it.
let redeemed = false;

async function mockData(page: Page) {
  // Regex, not `**/decisions**` — see the `**/conversations**` note below for
  // the same class of trap. This one arrived later: `src/locales/decisions.ts`
  // (the i18n namespace split) postdates this rig, and once it existed the glob
  // started serving JSON for that JS module, breaking the whole app mount with
  // a strict-MIME error. Requiring `?` or end-of-URL right after `/decisions`
  // excludes any `.ts` module path. NB the smoke rigs still use the glob safely
  // — they drive the BUILT bundle, where source modules have no own URL.
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [] }));
  await page.route('**/pause', (r) => json(r, { paused: false }));
  await page.route('**/autonomy', (r) => json(r, { mode: 'propose_apply', reason: null, actor: null }));
  await page.route('**/capabilities', (r) =>
    json(r, { version: 1, workloads: [], human_gates: [], denylist: { rules: [] } }),
  );
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: [] }));
  await page.route('**/infra/graph', (r) =>
    json(r, {
      generated_at: '2026-07-08T00:00:00Z',
      project: 'driftscribe-hack-2026',
      caveat: null,
      iac_snapshot_sha: 'cafef00d',
      degraded: false,
      degraded_reason: null,
      totals: { resources: 1, managed: 1, drift: 0 },
      groups: [
        {
          asset_type: 'run.googleapis.com/Service',
          label: 'Cloud Run service',
          adoptable: true,
          count: 1,
          managed: 1,
          drift: 0,
          sensitive: false,
          nodes: [
            {
              id: 'g0n0',
              label: 'payment-demo',
              asset_type: 'run.googleapis.com/Service',
              managed: true,
              location: 'asia-northeast1',
            },
          ],
        },
      ],
      edges: [],
      truncated: { per_type_sample: 10 },
    }),
  );

  await page.route('**/trace/**', (r) =>
    json(r, {
      trace_id: TRACE_ID,
      events: [
        {
          event: 'llm_thought',
          trace_id: TRACE_ID,
          workload: 'drift',
          thought_text: 'Comparing the live env to the ops contract.',
          insert_id: 'i1',
          timestamp: '2026-07-08T00:00:01Z',
        },
        {
          event: 'tool_call',
          trace_id: TRACE_ID,
          workload: 'drift',
          tool_name: 'read_live_env_tool',
          tool_args: { service: 'payment-demo' },
          insert_id: 'i2',
          timestamp: '2026-07-08T00:00:02Z',
        },
      ],
      decision: null,
      complete: true,
      fetched_from_cache: false,
    }),
  );

  // The rail list (metadata only) and the full detail used to rehydrate + lock.
  // NB: use REGEXES, not `**/conversations**` — that glob also matches the vite
  // source module `/src/lib/conversations.ts`, and serving JSON for a JS module
  // breaks the whole app mount (MIME error). The detail regex needs the id; the
  // list regex needs the `?query`, so neither can match the `.ts` module URL.
  await page.route(new RegExp('/conversations/' + CONVERSATION_ID + '$'), (r) =>
    json(r, {
      conversation_id: CONVERSATION_ID,
      workload: redeemed ? 'provision' : 'explore',
      crews: redeemed ? ['explore', 'provision'] : ['explore'],
      title: 'Can you create a bucket for the export job?',
      created_at: '2026-07-08T09:00:00Z',
      updated_at: '2026-07-08T09:05:00Z',
      user_turn_count: 1,
      turn_count: redeemed ? 4 : 2,
      last_trace_id: TRACE_ID,
      turns: [
        {
          seq: 0,
          role: 'user',
          text: 'Can you create a bucket for the export job?',
          workload: 'explore',
        },
        {
          seq: 1,
          role: 'crew',
          text: 'I can read the estate and explain it, but I cannot create anything.',
          workload: 'explore',
          trace_id: TRACE_ID,
        },
        ...(redeemed
          ? [
              {
                seq: 2,
                role: 'crew_change',
                text: HANDOFF_REASON,
                workload: 'provision',
                handoff: { from: 'explore', to: 'provision' },
              },
              {
                seq: 3,
                role: 'crew',
                text: 'Opened PR #42 adding the bucket to iac/. Nothing is applied until you approve it.',
                workload: 'provision',
                trace_id: TRACE_ID,
              },
            ]
          : []),
      ],
      // The server-side half of the proposal. The nonce is NOT here — the SPA
      // holds that in sessionStorage, seeded below the way a real `done` frame
      // would have.
      pending_handoff: redeemed
        ? null
        : {
            from: 'explore',
            to: 'provision',
            reason: HANDOFF_REASON,
            expires_at: '2999-01-01T00:00:00Z',
          },
    }),
  );
  await page.route(/\/conversations\?/, (r) =>
    json(r, {
      conversations: [
        {
          conversation_id: CONVERSATION_ID,
          workload: redeemed ? 'provision' : 'explore',
          crews: redeemed ? ['explore', 'provision'] : ['explore'],
          title: 'Can you create a bucket for the export job?',
          created_at: '2026-07-08T09:00:00Z',
          updated_at: '2026-07-08T09:05:00Z',
          user_turn_count: 1,
          turn_count: redeemed ? 4 : 2,
          last_trace_id: TRACE_ID,
        },
      ],
    }),
  );

  // Confirming IS the turn: the joining crew runs immediately.
  await page.route(/\/chat\/handoff$/, (r) => {
    redeemed = true;
    return json(r, {
      reply: 'Opened PR #42 adding the bucket to iac/. Nothing is applied until you approve it.',
      tool_calls: [],
      session_id: '',
      conversation_id: CONVERSATION_ID,
    });
  });
}

test('single-door crew handoff walkthrough', async ({ page }) => {
  redeemed = false;
  await seedToken(page);
  // Take custody of the nonce the way a real `done` frame would have: the
  // server persists the proposal but keeps only a digest of the nonce, so the
  // chip is only actionable on the client that received it. No custody, no chip
  // — which is a deliberate property, not a gap (see lib/handoff.ts).
  await page.addInitScript(
    ([cid, reason]) => {
      sessionStorage.setItem(
        'ds.handoff.' + cid,
        JSON.stringify({
          from: 'explore',
          to: 'provision',
          reason,
          nonce: 'visual-nonce',
          expires_at: '2999-01-01T00:00:00Z',
        }),
      );
    },
    [CONVERSATION_ID, HANDOFF_REASON],
  );
  await mockData(page);
  // Explicit ?view=chat: since Task 3.6 step 2 flipped DEFAULT_VIEW to 'desk',
  // a bare url renders the approval desk, which has no composer to walk through.
  await page.goto('/?view=chat');

  // The demo-notice popover auto-opens at boot on the chat view and drops into
  // the top-left, where it overlaps the conversations rail and intercepts the
  // click below. The desk rig dismisses it for the same reason. NB this is a
  // real overlap a first-time visitor hits on chat too, not a test artifact —
  // tracked separately; this rig is about the handoff.
  const notice = page.getByTestId('demo-notice-dismiss');
  if (await notice.isVisible()) await notice.click();

  const form = page.locator('#chat-form');
  await expect(form).toBeVisible();
  const newChatBtn = page.getByTestId('composer-new-chat');
  const chip = page.getByTestId('handoff-chip');

  // ── 1. Fresh composer: a prompt and a Send button, nothing to choose ───────
  // The whole point of the redesign: the operator is never asked to name a
  // specialist before they have said what they want.
  await expect(page.locator('#chat-form input[type="radio"]')).toHaveCount(0);
  await expect(newChatBtn).toHaveCount(0);
  await expect(chip).toHaveCount(0);
  await form.screenshot({ path: resolve(SHOTS, '1-fresh-composer.png'), animations: 'disabled' });

  // ── 2. Resume the Explore thread → the confirmation chip is waiting ────────
  await page.getByTestId('conversation-open').first().click();
  await expect(chip).toBeVisible();
  // It names both crews and carries Explore's own reason, verbatim.
  await expect(chip).toContainText('Explore');
  await expect(chip).toContainText('Provision');
  await expect(page.getByTestId('handoff-chip-reason')).toContainText('Provision');
  await expect(newChatBtn).toBeVisible();
  await chip.screenshot({ path: resolve(SHOTS, '2-handoff-chip.png'), animations: 'disabled' });
  // Whole chat column for context (composer + thread + the chip below it).
  await page.locator('#chat-area').screenshot({
    path: resolve(SHOTS, '2b-chip-full-column.png'),
    animations: 'disabled',
  });

  // ── 3. Confirm → the transition row, then Provision's first reply ──────────
  await page.getByTestId('handoff-confirm').click();
  await expect(page.getByTestId('thread-turn-crew-change')).toBeVisible();
  await expect(chip).toHaveCount(0); // the proposal is spent
  await page.locator('#chat-area').screenshot({
    path: resolve(SHOTS, '3-after-handoff.png'),
    animations: 'disabled',
  });

  // ── 4. New chat → clean slate, the thread and its history left in the rail ─
  await newChatBtn.click();
  await expect(newChatBtn).toHaveCount(0);
  await expect(page.getByTestId('conversation-thread')).toHaveCount(0);
  await form.screenshot({ path: resolve(SHOTS, '4-after-new-chat.png'), animations: 'disabled' });
});
