import { test, type Page, type Route } from '@playwright/test';

// ── Visual walkthrough of the chat-view hierarchy (ds-7ag.5 / plan Tasks 7-9,
// 11) ────────────────────────────────────────────────────────────────────────
// Same hand-run rig as the other *.visual.ts specs. What needs eyeballing here
// is a COUNT, not a pixel: the chat page carried ~6 boxed cards of equal weight,
// and after this it should read composer-first with two boxes (composer +
// reasoning) and everything else a quiet disclosure.
//
// ds-jns PR 3 took that further for a FRESH chat, which is now the empty
// new-chat state: greeting, composer, four example questions, and nothing else
// at all. The estate diagram and the capability drawer that used to open here
// are gone from it (the desk owns the estate; the drawer becomes a modal in
// Task 3.2), so the first capture below is a one-box page.
//
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     chat-hierarchy.visual.ts

const SHOTS = process.env.VISUAL_OUT ?? '/tmp/driftscribe-chat-screens';

function json(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function seed(page: Page, locale: 'en' | 'ja') {
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', l);
    localStorage.setItem('driftscribe_tour_done', '1');
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
  }, locale);
}

// Two threads: one with a single operator message (its count must NOT render)
// and one with several (its count must).
const CONVERSATIONS = {
  conversations: [
    {
      conversation_id: 'c-solo',
      workload: 'explore',
      title: 'what does DriftScribe watch?',
      updated_at: '2026-07-30T02:10:00Z',
      turn_count: 2,
      user_turn_count: 1,
    },
    {
      conversation_id: 'c-many',
      workload: 'drift',
      title: 'why did EXTRA drift on the agent service?',
      updated_at: '2026-07-30T01:30:00Z',
      turn_count: 8,
      user_turn_count: 4,
    },
  ],
};

async function mock(page: Page) {
  await page.route('**/infra/graph', (r) =>
    json(r, {
      generated_at: null,
      project: 'demo',
      caveat: '',
      degraded: false,
      degraded_reason: null,
      totals: { resources: 0, managed: 0, drift: 0 },
      groups: [],
      edges: [],
    }),
  );
  await page.route('**/infra/pending-approvals', (r) => json(r, { approvals: [] }));
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [] }));
  await page.route(/\/conversations\?/, (r) => json(r, CONVERSATIONS));
  await page.route('**/autonomy', (r) => json(r, { mode: 'propose_apply', reason: null, actor: null }));
  await page.route('**/pause', (r) => json(r, { paused: false }));
  await page.route('**/capabilities', (r) =>
    json(r, {
      autonomy: { mode: 'propose_apply' },
      gates: [{ name: 'HITL approval', detail: 'Rollbacks need a single-use signed link.' }],
      denylist: { rules: [{ pattern: 'projects/*/secrets/*', reason: 'secrets are never touched' }] },
      workloads: [],
    }),
  );
}

for (const locale of ['ja', 'en'] as const) {
  test(`chat hierarchy — ${locale}`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 1100 });
    await seed(page, locale);
    await mock(page);
    await page.goto('/?view=chat');
    await page.getByTestId('chat-empty-chips').waitFor();
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${SHOTS}/${locale}-chat-empty.png`, fullPage: true });

    // A chip fills the composer without sending, so the capture shows the state
    // an operator lands in after one click: their question in the box, ready to
    // edit, nothing on the wire.
    await page.getByTestId('chat-empty-chip').first().click();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-chat-empty-prefilled.png`, fullPage: true });
  });
}

// ── Inline reasoning disclosure (ds-jns) ─────────────────────────────────────
// jsdom cannot see the CSS cascade (the ds-akf lesson: a scoped modifier TIED
// the base rule and the "fix" shipped as dead CSS while every unit test stayed
// green). The unit suites only assert that the disclosure's testids exist, so
// the claim that it reads as a QUIET footnote under the reply — smaller, muted,
// italic — is unverifiable there by construction. It gets pinned here, against
// real computed styles.

const THREAD_TRACE = 'a'.repeat(32);

const THREAD_DETAIL = {
  conversation_id: 'c-many',
  workload: 'drift',
  title: 'why did EXTRA drift on the agent service?',
  turns: [
    { seq: 0, role: 'user', text: 'why did EXTRA drift on the agent service?' },
    {
      seq: 1,
      role: 'crew',
      text: 'Someone set EXTRA directly on the running revision, so the live service no longer matches ops-contract.yaml.',
      workload: 'drift',
      trace_id: THREAD_TRACE,
    },
  ],
};

const TRACE = {
  trace_id: THREAD_TRACE,
  complete: true,
  decision: null,
  events: [
    {
      event: 'llm_thought',
      trace_id: THREAD_TRACE,
      insert_id: 't1',
      timestamp: '2026-07-30T01:30:01Z',
      thought_text: '**Assessing the drift**\nComparing the live revision against the contract.',
    },
    {
      event: 'tool_call',
      trace_id: THREAD_TRACE,
      insert_id: 'c1',
      timestamp: '2026-07-30T01:30:03Z',
      tool_name: 'read_live_env_tool',
      tool_args: { service: 'driftscribe-agent' },
    },
    {
      event: 'tool_result',
      trace_id: THREAD_TRACE,
      insert_id: 'r1',
      timestamp: '2026-07-30T01:30:05Z',
      tool_name: 'read_live_env_tool',
      result_ok: true,
      result_preview: '{"EXTRA": "set-by-hand"}',
      latency_ms: 1840,
    },
    {
      event: 'mcp_call',
      trace_id: THREAD_TRACE,
      insert_id: 'm1',
      timestamp: '2026-07-30T01:30:02Z',
      mcp_server: 'developer_knowledge',
      mcp_tool: 'search_documents',
      latency_ms: 420,
    },
  ],
};

for (const locale of ['ja', 'en'] as const) {
  test(`inline reasoning disclosure — ${locale}`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 1100 });
    await seed(page, locale);
    await mock(page);
    await page.route(/\/conversations\/c-many/, (r) => json(r, THREAD_DETAIL));
    await page.route(/\/trace\/[a-f0-9]+$/, (r) => json(r, TRACE));

    await page.goto(`/?view=chat&conversation=c-many`);
    const line = page.getByTestId('reasoning-disclosure');
    await line.waitFor();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOTS}/${locale}-chat-disclosure-collapsed.png`, fullPage: true });

    // The line must be QUIETER than the reply it belongs to. Comparing against
    // the sibling reply body rather than a hard-coded px value: the claim is
    // about hierarchy, and a token change that moved both together would not
    // break it, while dead CSS (the ds-akf failure) makes them equal and does.
    const measured = await page.evaluate(() => {
      const sub = document.querySelector('[data-testid="reasoning-subtitle"]') as HTMLElement;
      const body = document.querySelector('.bubble--crew .turn__text') as HTMLElement;
      const line = document.querySelector('[data-testid="reasoning-disclosure"]') as HTMLElement;
      const px = (v: string) => parseFloat(v);
      const cs = getComputedStyle(sub);
      const cb = getComputedStyle(body);
      return {
        subFont: px(cs.fontSize),
        bodyFont: px(cb.fontSize),
        subStyle: cs.fontStyle,
        subColor: cs.color,
        bodyColor: cb.color,
        // The collapsed line must not be a second heavyweight action next to
        // the reply: no card border, no filled background.
        lineBorder: getComputedStyle(line).borderTopWidth,
        subTop: sub.getBoundingClientRect().top,
        bodyTop: body.getBoundingClientRect().top,
      };
    });
    if (!(measured.subFont < measured.bodyFont)) {
      throw new Error(
        `reasoning line is not quieter than the reply: ${measured.subFont}px vs ${measured.bodyFont}px`,
      );
    }
    if (measured.subStyle !== 'italic') {
      throw new Error(`reasoning line should be italic, got ${measured.subStyle}`);
    }
    if (measured.subColor === measured.bodyColor) {
      throw new Error(`reasoning line shares the reply's ink (${measured.subColor}) — no hierarchy`);
    }
    if (parseFloat(measured.lineBorder) !== 0) {
      throw new Error(`collapsed line should carry no box, got border ${measured.lineBorder}`);
    }
    // It sits ABOVE the reply body — thinking, then answer.
    if (!(measured.subTop < measured.bodyTop)) {
      throw new Error('reasoning line must render above the reply body');
    }

    // Expanded: the interleaved rows render in chronological order. The mcp
    // event is LAST in the payload (reconcileBackfill appends the trace-only
    // side-channel) but stamped :02, before the tool call at :03 — so it has to
    // lift above the tool row. The tool row itself anchors at its CALL, not at
    // its result at :05.
    await line.click();
    await page.getByTestId('trace-detail').waitFor();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${SHOTS}/${locale}-chat-disclosure-open.png`, fullPage: true });

    const kinds = await page.evaluate(() =>
      [...document.querySelectorAll('[data-testid="trace-detail"] [data-testid^="trace-row-"]')].map(
        (n) => n.getAttribute('data-testid'),
      ),
    );
    const expected = ['trace-row-thought', 'trace-row-mcp', 'trace-row-tool'];
    if (JSON.stringify(kinds) !== JSON.stringify(expected)) {
      throw new Error(`interleaved rows out of order: ${JSON.stringify(kinds)}`);
    }
  });
}
