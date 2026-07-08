import { test, expect, type Page, type Route } from '@playwright/test';
import { resolve } from 'node:path';

// ── Visual walkthrough of the composer New-chat button + crew lock ──
// Drives the REAL Svelte app (vite dev) with every backend endpoint mocked, then
// captures a PNG at each state so a human can eyeball the feature:
//   1. fresh composer      — no lock, no New-chat button
//   2. resumed thread      — Anchor pinned, other 3 cards greyed, New-chat shown
//   3. blocked-click nudge — a refused click force-shows the lock tooltip
//   4. after New chat      — lock released, button gone, back to a clean slate
//
// Screenshots land OUTSIDE the repo (scratchpad) so the branch stays clean.

const SHOTS =
  process.env.VISUAL_OUT ??
  '/tmp/claude-1000/-home-adi-driftscribe/099cf626-6748-477f-8823-dea624123fec/scratchpad/composer-lock-screens';

const CONVERSATION_ID = 'conv-visual-0001';
const TRACE_ID = 'abc123abc123abc123abc123abc12300';

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

// One prior Anchor (drift) conversation in the rail — clicking it resumes the
// thread and engages the crew lock on Anchor.
async function mockData(page: Page) {
  await page.route('**/decisions**', (r) => json(r, { decisions: [] }));
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
      workload: 'drift',
      title: 'Why did payment-demo drift?',
      created_at: '2026-07-08T09:00:00Z',
      updated_at: '2026-07-08T09:05:00Z',
      turn_count: 2,
      last_trace_id: TRACE_ID,
      turns: [
        { seq: 0, role: 'user', text: 'Why did payment-demo drift?', workload: 'drift' },
        {
          seq: 1,
          role: 'crew',
          text: 'Three env vars drifted from the ops contract; I opened an issue.',
          workload: 'drift',
          trace_id: TRACE_ID,
        },
      ],
    }),
  );
  await page.route(/\/conversations\?/, (r) =>
    json(r, {
      conversations: [
        {
          conversation_id: CONVERSATION_ID,
          workload: 'drift',
          title: 'Why did payment-demo drift?',
          created_at: '2026-07-08T09:00:00Z',
          updated_at: '2026-07-08T09:05:00Z',
          turn_count: 2,
          last_trace_id: TRACE_ID,
        },
      ],
    }),
  );
}

test('composer New-chat + crew-lock walkthrough', async ({ page }) => {
  await seedToken(page);
  await mockData(page);
  await page.goto('/');

  const form = page.locator('#chat-form');
  await expect(form).toBeVisible();
  const newChatBtn = page.getByTestId('composer-new-chat');
  const lockedCards = page.locator('.crew-card--locked');

  // ── 1. Fresh composer: no lock, no New-chat button ─────────────────────────
  await expect(newChatBtn).toHaveCount(0);
  await expect(lockedCards).toHaveCount(0);
  await form.screenshot({ path: resolve(SHOTS, '1-fresh-composer.png'), animations: 'disabled' });

  // ── 2. Resume the Anchor thread from the rail → lock engages ───────────────
  await page.getByTestId('conversation-open').first().click();
  // Three of the four crew cards grey out (all but Anchor/drift)…
  await expect(lockedCards).toHaveCount(3);
  // …and the New-chat button appears at the trailing edge of the crew row.
  await expect(newChatBtn).toBeVisible();
  await form.screenshot({ path: resolve(SHOTS, '2-locked-composer.png'), animations: 'disabled' });
  // Whole chat column for context (composer + rehydrated thread + timeline).
  await page.locator('#chat-area').screenshot({
    path: resolve(SHOTS, '2b-locked-full-column.png'),
    animations: 'disabled',
  });

  // ── 3. Blocked click on a locked card → nudge force-shows the lock tooltip ──
  const patch = page.getByTestId('crew-card-upgrade'); // Patch — a locked card
  await patch.locator('input').click({ force: true });
  await expect(patch).toHaveClass(/crew-card--nudged/);
  // The tooltip floats ABOVE the card, so shoot the chat column (an element clip
  // of #chat-form alone would crop it).
  await page.locator('#chat-area').screenshot({
    path: resolve(SHOTS, '3-blocked-click-nudge.png'),
    animations: 'disabled',
  });

  // ── 4. New chat → lock released, button gone, clean slate ──────────────────
  await newChatBtn.click();
  await expect(lockedCards).toHaveCount(0);
  await expect(newChatBtn).toHaveCount(0);
  await form.screenshot({ path: resolve(SHOTS, '4-after-new-chat.png'), animations: 'disabled' });
});
