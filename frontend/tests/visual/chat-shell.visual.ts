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
//   5. a FRESH chat centres the composer instead, and the first send flips it
//      into the pinned shell
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
  // The real DTO shape. The previous body here was a guess from an older API
  // (`gates`, no `version`) and CapabilityCard's structural check rejects it —
  // harmless while nothing in this file opened the card, and a guaranteed error
  // row the moment the capability-modal test below does.
  await page.route('**/capabilities', (r) =>
    json(r, {
      version: 1,
      provenance: 'Generated from the same constants the enforcement code imports.',
      iam_note: 'Each worker runs as its own least-privilege service account.',
      workloads: [],
      human_gates: [
        {
          id: 'iac_apply',
          title: 'IaC plan apply',
          description: 'Applying an OpenTofu plan needs a single-use signed link.',
        },
      ],
      denylist: {
        summary: 'It cannot touch its own control plane, or change who has access.',
        enforced_at: ['plan', 'apply'],
        rules: [
          {
            id: 'iam-change-forbidden-v1',
            description: 'Any IAM change at all (v1 floor).',
            category: 'iam',
          },
        ],
      },
    }),
  );
}

/**
 * Boot chat on an OPEN thread.
 *
 * The pinned shell only exists once there is a transcript. A FRESH chat is the
 * empty state: the composer is centred, and the thread region is not rendered
 * at all (its own tests are at the bottom of this file). So every geometry spec
 * below opens a one-turn conversation first and grows THAT — the growth is
 * still injected, still a claim about the container rather than about any
 * fixture's rendered height; the container just has to be on screen before it
 * can be measured.
 */
async function openThread(page: Page) {
  await page.route(/\/conversations\/c1$/, (r) =>
    json(r, {
      conversation_id: 'c1',
      workload: 'drift',
      title: 'a prior thread',
      turns: [
        { seq: 0, role: 'user', text: 'what changed?', workload: 'drift' },
        { seq: 1, role: 'crew', text: 'EXTRA was set by hand', workload: 'drift' },
      ],
    }),
  );
  await page.goto('/?conversation=c1');
  await page.getByTestId('conversation-thread').waitFor();
  await page.locator('#chat-form').waitFor();
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
  await openThread(page);

  const short = await geometry(page);
  expect(short.threadOverflowY).toBe('auto');
  // Two turns do not fill the region, and the whole app already fits the viewport.
  expect(short.docScrollHeight).toBeLessThanOrEqual(short.viewport + 1);
  expect(short.composerBottom).toBeLessThanOrEqual(short.viewport);

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
  expect(Math.abs(full.composerTop - short.composerTop)).toBeLessThan(2);
});

test('chat: follows the newest content, and yields once the operator scrolls up', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await openThread(page);

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
  await openThread(page);

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

// ── The empty new-chat state ─────────────────────────────────────────────────
// A fresh chat has no transcript, so the pinned shell would leave the operator
// looking at a screen of nothing with a lone input bar under it. The composer
// moves to the middle instead and brings a greeting and four example questions
// with it. The centring is done with auto margins rather than
// `justify-content: center`, and the pause test below is what makes that choice
// a checked claim instead of a comment.

/** What sits where, in the empty state. */
const emptyGeometry = (page: Page) =>
  page.evaluate(() => {
    const box = (sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      return el ? el.getBoundingClientRect() : null;
    };
    const thread = document.querySelector('[data-testid="chat-thread"]') as HTMLElement;
    const area = box('#chat-area')!;
    const greeting = box('[data-testid="chat-empty-greeting"]')!;
    const chips = box('[data-testid="chat-empty-chips"]')!;
    // The link's WRAPPER, not the button: the wrapper is what carries the
    // closing auto margin and the group's trailing padding, so its box is where
    // the group actually ends. (The chips were measured the same way — the old
    // `ul` rect included its own padding-bottom.)
    const more = box('.chat-empty__more')!;
    const form = box('#chat-form')!;
    return {
      threadDisplay: getComputedStyle(thread).display,
      docScrollHeight: document.documentElement.scrollHeight,
      viewport: window.innerHeight,
      areaTop: area.top,
      areaBottom: area.bottom,
      greetingTop: greeting.top,
      greetingBottom: greeting.bottom,
      chipCount: document.querySelectorAll('[data-testid="chat-empty-chip"]').length,
      chipsTop: chips.top,
      chipsBottom: chips.bottom,
      moreTop: more.top,
      moreBottom: more.bottom,
      formTop: form.top,
      formBottom: form.bottom,
    };
  });

test('chat: a fresh chat centres the composer instead of pinning it to an empty page', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.getByTestId('chat-empty-chips').waitFor();

  const g = await emptyGeometry(page);

  // The scroll container is not merely empty, it is not rendered — otherwise
  // its padding would leave a band of dead space above the greeting.
  expect(g.threadDisplay).toBe('none');

  // Reading order: greeting, then the box you type in, then things you could
  // type. Chips BELOW, so four sentences never push the input down the page.
  expect(g.greetingBottom).toBeLessThanOrEqual(g.formTop);
  expect(g.chipsTop).toBeGreaterThanOrEqual(g.formBottom);
  expect(g.chipCount).toBe(4);
  // The capability link is the quiet footnote under all of it — last, so it
  // never comes between the operator and the box.
  expect(g.moreTop).toBeGreaterThanOrEqual(g.chipsBottom);

  // Centred: the space above the group and the space below it are the same.
  // This is the assertion that fails if the auto margins are dropped — without
  // them the group sits at the TOP of the column and `below` is the whole
  // remaining height. Measured to the LAST member of the group (the link, not
  // the chips): the auto margin has to travel with whatever ends up last, and
  // measuring the old last element would keep passing while the new one hung
  // off the bottom edge on its own. Two lines of tolerance, not zero:
  // sub-pixel layout.
  const above = g.greetingTop - g.areaTop;
  const below = g.areaBottom - g.moreBottom;
  expect(Math.abs(above - below)).toBeLessThan(2);
  // Non-vacuity: there has to BE free space, or "centred" and "at the top" are
  // the same measurement and this proves nothing.
  expect(above).toBeGreaterThan(80);

  // And the window still does not scroll — the shell holds with no transcript.
  expect(g.docScrollHeight).toBeLessThanOrEqual(g.viewport + 1);
});

test('chat: the pause banner stays at the top of an empty chat, not centred with the composer', async ({
  page,
}) => {
  // Why the centring uses auto margins. PauseBanner is a flex child of the same
  // column, so `justify-content: center` would centre IT too — floating the
  // explanation for why the composer is refusing input into the middle of the
  // page. Auto margins split only the space BETWEEN them, which leaves anything
  // above the greeting exactly where it was.
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.route('**/pause', (r) => json(r, { paused: true }));
  await page.goto('/?view=chat');
  await page.getByTestId('pause-banner').waitFor();
  await page.getByTestId('chat-empty-chips').waitFor();

  const where = await page.evaluate(() => {
    const area = (document.querySelector('#chat-area') as HTMLElement).getBoundingClientRect();
    const banner = (
      document.querySelector('[data-testid="pause-banner"]') as HTMLElement
    ).getBoundingClientRect();
    const greeting = (
      document.querySelector('[data-testid="chat-empty-greeting"]') as HTMLElement
    ).getBoundingClientRect();
    return {
      gapAboveBanner: banner.top - area.top,
      gapBannerToGreeting: greeting.top - banner.bottom,
    };
  });
  // Flush against the top of the column...
  expect(where.gapAboveBanner).toBeLessThan(4);
  // ...with the centred group well below it. (If the banner were centred too,
  // this gap would be a handful of pixels instead of most of the column.)
  expect(where.gapBannerToGreeting).toBeGreaterThan(100);
});

test('chat: the first send flips the empty state into the pinned shell', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  // A one-frame SSE reply, so the turn settles instead of spinning forever.
  await page.route(/\/chat$/, (r) =>
    r.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body:
        'event: done\ndata: {"reply":"nothing has drifted","trace_id":"' +
        'a'.repeat(32) +
        '","conversation_id":"c9"}\n\n',
    }),
  );
  await page.goto('/?view=chat');
  await page.getByTestId('chat-empty-chips').waitFor();

  // A chip PREFILLS; it must not send. The composer carries exactly the text
  // that was on the chip, and nothing has gone to the wire yet.
  const chip = page.getByTestId('chat-empty-chip').first();
  const chipText = (await chip.textContent())!.trim();
  await chip.click();
  await expect(page.locator('#prompt-input')).toHaveValue(chipText);
  await expect(page.getByTestId('chat-empty-chips')).toBeVisible();

  await page.locator('#send-btn').click();
  await page.getByTestId('conversation-thread').waitFor();

  // The front door is gone and the shell is back: thread on screen, composer
  // pinned to the bottom of the viewport.
  await expect(page.getByTestId('chat-empty-chips')).toHaveCount(0);
  await expect(page.getByTestId('chat-empty-greeting')).toHaveCount(0);

  const g = await geometry(page);
  expect(g.threadOverflowY).toBe('auto');
  expect(g.docScrollHeight).toBeLessThanOrEqual(g.viewport + 1);
  // Pinned low: the composer is against the bottom of the viewport, not sitting
  // in the middle of it where the empty state had it.
  expect(g.composerBottom).toBeLessThanOrEqual(g.viewport);
  expect(g.composerBottom).toBeGreaterThan(g.viewport - 120);
});

test('chat: a viewport too short for the empty state scrolls it instead of clipping it', async ({
  page,
}) => {
  // Why `.chat-area--empty` carries `overflow-y: auto`. The auto margins that
  // centre the group collapse to 0 the moment the content stops fitting, and
  // from there the group grows past the column into `.layout--chat`'s
  // `overflow: hidden` — which does not scroll, it CUTS. Measured with the rule
  // removed: the chips ended 125px below the fold with nothing able to reach
  // them; with it, the same 125px is scrollable and they land on screen.
  await page.setViewportSize({ width: 800, height: 300 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.getByTestId('chat-empty-chips').waitFor();

  const r = await page.evaluate(() => {
    const area = document.querySelector('#chat-area') as HTMLElement;
    const before = area.scrollHeight - area.clientHeight;
    area.scrollTop = 99999;
    // The LAST thing in the column, which is the capability link — measuring
    // the chips would keep passing with the link still cut off below them.
    const last = document
      .querySelector('[data-testid="capability-link"]')!
      .getBoundingClientRect();
    return { overflow: before, scrolledTo: area.scrollTop, lastBottom: last.bottom, vh: window.innerHeight };
  });
  // Non-vacuity: the state must genuinely not fit, or "reachable" is free.
  expect(r.overflow).toBeGreaterThan(50);
  // The column is a scroll container, and scrolling it brings the tail in.
  // (+1: the column starts on a fractional y, so the box lands a half-pixel
  // past the fold — 300.47 against a 300px viewport. Clipping is 125px, not
  // half of one.)
  expect(r.scrolledTo).toBe(r.overflow);
  expect(r.lastBottom).toBeLessThanOrEqual(r.vh + 1);
});

test('chat: the capability link opens a dialog that is already showing the answer', async ({
  page,
}) => {
  // Browser-only on three counts: <dialog>.showModal() puts the panel in the top
  // layer (jsdom has no top layer and no ::backdrop), the body scroll-lock is a
  // measurement, and "the content is on screen" is a rect. The unit suite pins
  // the wiring; this pins that the wiring produces something you can read.
  await page.setViewportSize({ width: 1280, height: 800 });
  await seed(page);
  await mock(page);
  await page.goto('/?view=chat');
  await page.getByTestId('capability-link').click();

  const card = page.getByTestId('capability-card');
  await expect(card).toBeVisible();
  // Open on arrival — not a disclosure inside a dialog waiting for a second
  // click. `cap-summary` is the standalone card's toggle; there must be none.
  await expect(page.getByTestId('cap-summary')).toHaveCount(0);
  await expect(page.getByTestId('cap-gates')).toBeVisible();
  await expect(card).toContainText('IaC plan apply');

  const r = await page.evaluate(() => {
    const dlg = document.querySelector('[data-testid="capability-card"]')!.closest('dialog')!;
    const body = dlg.querySelector('.modal__body') as HTMLElement;
    const rect = dlg.getBoundingClientRect();
    return {
      open: dlg.open,
      // The <dialog> is the app's only modal-mode dialog; matches() proves the
      // top-layer path (showModal), not merely `open`.
      modalMode: dlg.matches(':modal'),
      bodyOverflow: getComputedStyle(document.body).overflow,
      modalBodyOverflowY: getComputedStyle(body).overflowY,
      onScreen: rect.top >= 0 && rect.bottom <= window.innerHeight && rect.width > 200,
    };
  });
  expect(r.open).toBe(true);
  expect(r.modalMode).toBe(true);
  expect(r.onScreen).toBe(true);
  // The page behind does not scroll; the long read scrolls inside the panel.
  expect(r.bodyOverflow).toBe('hidden');
  expect(r.modalBodyOverflowY).toBe('auto');

  // Escape gives it back, and the front door is still there.
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('capability-card')).toHaveCount(0);
  await expect(page.getByTestId('chat-empty-chips')).toBeVisible();
});
