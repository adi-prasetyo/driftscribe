import { test, expect, type Page, type Route } from '@playwright/test';

// ── The chat app shell: pinned composer, scrolling transcript (ds-jns PR 3) ──
//
// This is a GEOMETRY spec, and geometry is precisely what the unit suite cannot
// see: jsdom has no layout engine, so every scroll property reads 0 and the CSS
// cascade is never resolved (the ds-akf lesson — a scoped modifier tied its base
// rule and the "fix" shipped as dead CSS with every unit test green). The
// arm/disarm rule in stickToBottom.ts is unit-tested as a pure function and the
// action's wiring is tested against a shadowed geometry; what is pinned HERE is
// the part that only a real browser can answer:
//
//   1. the window does not scroll on chat — the transcript does
//   2. the composer is on screen at the bottom, always
//   3. the desk still scrolls the window (the modifier did not leak)
//   4. following the newest content, and yielding when the operator scrolls up
//
// Same hand-run rig as the other *.visual.ts specs — vite dev server, every
// endpoint mocked, no backend, never touches GCP:
//
//   npx playwright test --config tests/visual/playwright.visual.config.ts \
//     chat-shell.visual.ts

function json(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function seed(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', 'en');
    localStorage.setItem('driftscribe_tour_done', '1');
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
  });
}

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
  await page.route(/\/conversations\?/, (r) => json(r, { conversations: [] }));
  await page.route('**/autonomy', (r) =>
    json(r, { mode: 'propose_apply', reason: null, actor: null }),
  );
  await page.route('**/pause', (r) => json(r, { paused: false }));
  await page.route('**/capabilities', (r) =>
    json(r, {
      autonomy: { mode: 'propose_apply' },
      gates: [{ name: 'HITL approval', detail: 'Rollbacks need a single-use signed link.' }],
      denylist: { rules: [] },
      workloads: [],
    }),
  );
}

/**
 * Push tall content into the transcript.
 *
 * Deliberately injected rather than driven through a mocked conversation: the
 * contract under test belongs to the CONTAINER (does it scroll instead of the
 * window, does it follow its own tail) and holds for any content at all. Tying
 * it to a particular fixture's rendered height would make the spec fail the day
 * a turn's padding changes, for a reason that has nothing to do with the shell.
 */
async function growThread(page: Page, blocks = 6) {
  await page.evaluate((n) => {
    const thread = document.querySelector('[data-testid="chat-thread"]');
    if (!thread) throw new Error('no thread region');
    for (let i = 0; i < n; i++) {
      const d = document.createElement('div');
      d.style.height = '400px';
      d.textContent = `filler ${i}`;
      thread.appendChild(d);
    }
  }, blocks);
  // One frame for the MutationObserver to deliver and the follow to land.
  await page.waitForTimeout(120);
}

const geometry = (page: Page) =>
  page.evaluate(() => {
    const thread = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    const composer = document.querySelector('#chat-form') as HTMLElement;
    const box = composer.getBoundingClientRect();
    return {
      docScrollHeight: document.documentElement.scrollHeight,
      viewport: window.innerHeight,
      windowScrollY: window.scrollY,
      threadOverflowY: getComputedStyle(thread).overflowY,
      threadScrollHeight: thread.scrollHeight,
      threadClientHeight: thread.clientHeight,
      threadScrollTop: thread.scrollTop,
      composerTop: box.top,
      composerBottom: box.bottom,
    };
  });

// NOTE: no `page.emulateMedia({ reducedMotion: 'reduce' })` here, deliberately.
// An earlier draft of this file set it in a beforeEach "for determinism", which
// silently selected the ONE motion path that worked: the follow was written as
// `prefersReducedMotion() ? 'auto' : 'smooth'`, and smooth was catastrophically
// broken (see the streaming test below). Every unit test agreed, because jsdom's
// scrollTo stub is instant too. The default media state IS the production path,
// so it is the one that has to be measured.

test('chat: the transcript scrolls, the window does not, the composer stays put', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.locator('#chat-form').waitFor();

  const empty = await geometry(page);
  expect(empty.threadOverflowY).toBe('auto');
  // Nothing to scroll yet, and the whole app already fits the viewport.
  expect(empty.docScrollHeight).toBeLessThanOrEqual(empty.viewport + 1);
  expect(empty.composerBottom).toBeLessThanOrEqual(empty.viewport);

  await growThread(page, 6);
  const full = await geometry(page);

  // 1. The transcript overflows...
  expect(full.threadScrollHeight).toBeGreaterThan(full.threadClientHeight + 100);
  // 2. ...and the DOCUMENT still does not. This is the whole point: 2400px of
  //    filler landed inside the shell without the page growing an inch.
  expect(full.docScrollHeight).toBeLessThanOrEqual(full.viewport + 1);
  expect(full.windowScrollY).toBe(0);
  // 3. The composer did not move and is still on screen.
  expect(full.composerBottom).toBeLessThanOrEqual(full.viewport);
  expect(Math.abs(full.composerTop - empty.composerTop)).toBeLessThan(2);
});

test('chat: follows the newest content, and yields once the operator scrolls up', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.locator('#chat-form').waitFor();

  await growThread(page, 6);
  const followed = await geometry(page);
  // Guard against a VACUOUS pass. If the shell ever stops constraining its
  // height the thread cannot overflow, every distance-from-bottom is 0, and
  // every assertion below passes while following is doing nothing at all.
  expect(followed.threadScrollHeight).toBeGreaterThan(followed.threadClientHeight + 500);

  // Pinned to the tail: this is the behaviour a streaming reply depends on.
  const distance = (g: Awaited<ReturnType<typeof geometry>>) =>
    g.threadScrollHeight - g.threadScrollTop - g.threadClientHeight;
  expect(distance(followed)).toBeLessThanOrEqual(2);

  // The operator scrolls up to re-read something older.
  await page.evaluate(() => {
    const thread = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
  });
  await page.waitForTimeout(50);
  await growThread(page, 3);

  const held = await geometry(page);
  // Still where they left it — the arriving content did not drag them away.
  expect(held.threadScrollTop).toBe(0);

  // Back to the bottom, and the follow re-arms.
  await page.evaluate(() => {
    const thread = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    thread.scrollTop = thread.scrollHeight;
  });
  await page.waitForTimeout(50);
  await growThread(page, 2);
  const rearmed = await geometry(page);
  expect(distance(rearmed)).toBeLessThanOrEqual(2);
});

test('chat: a streaming reply never outruns the follow', async ({ page }) => {
  // The test that catches an ANIMATED follow, which nothing else can.
  //
  // A single grow-then-check passes with a smooth scroll: one animation, given
  // time to land, arrives. What breaks is the real shape of a streaming reply —
  // chunk after chunk, faster than an animation completes. Each new chunk
  // re-targets a scroll still in flight, and the intermediate scroll events it
  // fires carry the animation's own backlog into the arm/disarm rule, which
  // reads it as "the operator scrolled up" and latches off. Measured on the
  // broken version: 11 → 131 → 276 → … → 3808px, never recovering.
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.locator('#chat-form').waitFor();

  const distances = await page.evaluate(async () => {
    const thread = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    const out: number[] = [];
    for (let i = 0; i < 25; i++) {
      const d = document.createElement('div');
      d.style.height = '120px';
      d.textContent = `chunk ${i}`;
      thread.appendChild(d);
      await new Promise((r) => setTimeout(r, 60));
      out.push(thread.scrollHeight - thread.scrollTop - thread.clientHeight);
    }
    return out;
  });

  // Non-vacuity: 25 × 120px must genuinely have overflowed a ~620px region.
  const overflowed = await page.evaluate(() => {
    const t = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    return t.scrollHeight - t.clientHeight;
  });
  expect(overflowed).toBeGreaterThan(1500);

  // Every sample pinned. Not just the last one: an animated follow eventually
  // settles if you stop feeding it, so a final-state assertion would pass on
  // the broken version the moment the stream ended.
  expect(Math.max(...distances)).toBeLessThanOrEqual(2);
});

test('chat: a ?reasoning= replay opens at its banner, not at its bottom', async ({ page }) => {
  // A historical replay is a static record, and openTrace scrolls the thread to
  // the top so the "you are reading a past run" banner is the first thing seen.
  // The follow used to win that race outright — measured before the fix: the
  // region opened at its own bottom with the banner at -11px, scrolled off the
  // top edge. stickToBottom is handed `!historicalActive` so the mode has no
  // follow to race in the first place.
  //
  // (Task 3.3 deletes replay mode; this test and the `enabled` parameter go
  // with it. Until then the URL shape is live and so is the guarantee.)
  const TID = 'e'.repeat(32);
  const decision = {
    decision_id: 'd1',
    trace_id: TID,
    action: 'drift_detected',
    summary: 'EXTRA was set by hand on the running revision',
    created_at: '2026-07-30T01:30:00Z',
  };
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.route(/\/decisions(\?|$)/, (r) => json(r, { decisions: [decision] }));
  // Delayed and tall: the trace body has to land AFTER the initial scroll, which
  // is the real shape of the boot path and the only way the race is reachable.
  await page.route(/\/trace\/[a-f0-9]+$/, async (r) => {
    await new Promise((res) => setTimeout(res, 250));
    return json(r, {
      trace_id: TID,
      complete: true,
      decision,
      events: Array.from({ length: 20 }, (_, i) => ({
        event: 'tool_call',
        trace_id: TID,
        insert_id: `c${i}`,
        timestamp: `2026-07-30T01:30:${String(i).padStart(2, '0')}Z`,
        tool_name: `read_live_env_tool_${i}`,
        tool_args: { service: 'driftscribe-agent' },
      })),
    });
  });

  await page.goto(`/?view=chat&reasoning=${TID}`);
  await page.getByTestId('historical-banner').waitFor();
  await page.waitForTimeout(1000);

  const where = await page.evaluate(() => {
    const t = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    const banner = document.getElementById('historical-badge') as HTMLElement;
    return {
      scrollTop: t.scrollTop,
      overflow: t.scrollHeight - t.clientHeight,
      bannerTop: banner.getBoundingClientRect().top,
      threadTop: t.getBoundingClientRect().top,
      threadBottom: t.getBoundingClientRect().bottom,
    };
  });
  // Non-vacuity: the replay must actually be taller than the region, or there
  // is no bottom to be wrongly parked at and this asserts nothing.
  expect(where.overflow).toBeGreaterThan(50);
  expect(where.scrollTop).toBe(0);
  // The banner is visible INSIDE the region, not scrolled above its top edge.
  expect(where.bannerTop).toBeGreaterThanOrEqual(where.threadTop);
  expect(where.bannerTop).toBeLessThan(where.threadBottom);
});

test('desk: still a document — the window scrolls, nothing is pinned', async ({ page }) => {
  // The chat shell is a MODIFIER, not a change to .layout. The desk is a
  // landing page read top to bottom; if the constraint leaked onto it the
  // estate section below the fold would be unreachable, or would open a second
  // nested scrollbar inside the first.
  await page.setViewportSize({ width: 1280, height: 700 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=desk');
  await page.getByTestId('approval-desk').waitFor();
  await page.waitForTimeout(400);

  // Filler rather than a fat fixture, for the same reason growThread() uses it:
  // the claim is that content in the desk layout grows the DOCUMENT, and it
  // holds whatever that content is. (An all-empty desk is genuinely shorter
  // than the viewport, so measuring the mocked page as-is would assert nothing.)
  await page.evaluate(() => {
    const d = document.createElement('div');
    d.style.height = '1500px';
    document.querySelector('main.layout')!.appendChild(d);
  });
  await page.waitForTimeout(50);

  const desk = await page.evaluate(() => ({
    docScrollHeight: document.documentElement.scrollHeight,
    viewport: window.innerHeight,
    threadPresent: !!document.querySelector('[data-testid="chat-thread"]'),
    layoutOverflow: getComputedStyle(document.querySelector('main.layout') as HTMLElement).overflowY,
    appHeight: getComputedStyle(document.getElementById('app') as HTMLElement).height,
  }));
  expect(desk.threadPresent).toBe(false);
  expect(desk.layoutOverflow).not.toBe('hidden');
  // Neither half of the chat clamp reached the desk: #app is not pinned to the
  // viewport, and the document grew past it.
  expect(parseFloat(desk.appHeight)).toBeGreaterThan(desk.viewport);
  expect(desk.docScrollHeight).toBeGreaterThan(desk.viewport);
});

test('the tour offer spans the page instead of the rails column', async ({ page }) => {
  // Grid auto-placement was dropping the banner into the first cell — the 280px
  // rails track — and pushing the rails and the chat area onto separate rows.
  // Moving it out of .layout is also what keeps it from landing in an implicit
  // second row that the chat shell's `overflow: hidden` would swallow whole.
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.addInitScript(() => {
    sessionStorage.setItem('driftscribe_token', 'visual-token');
    localStorage.setItem('driftscribe.locale', 'en');
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
    // deliberately NO driftscribe_tour_done — that is what offers the tour
  });
  await mock(page);
  await page.goto('/?view=chat');
  const banner = page.getByTestId('tour-banner');
  await banner.waitFor();

  const measured = await page.evaluate(() => {
    const b = document.querySelector('[data-testid="tour-banner"]') as HTMLElement;
    const rails = document.querySelector('[data-testid="rails"]') as HTMLElement;
    const chat = document.querySelector('#chat-area') as HTMLElement;
    return {
      bannerWidth: b.getBoundingClientRect().width,
      railsWidth: rails.getBoundingClientRect().width,
      railsTop: rails.getBoundingClientRect().top,
      chatTop: chat.getBoundingClientRect().top,
      bannerBottom: b.getBoundingClientRect().bottom,
    };
  });
  // Wider than the rails column it used to be crammed into...
  expect(measured.bannerWidth).toBeGreaterThan(measured.railsWidth * 2);
  // ...the banner is above both...
  expect(measured.bannerBottom).toBeLessThanOrEqual(measured.railsTop + 1);
  // ...and the rails and the chat area share a row again.
  expect(Math.abs(measured.railsTop - measured.chatTop)).toBeLessThan(2);
});
