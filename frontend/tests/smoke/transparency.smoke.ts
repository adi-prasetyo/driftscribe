import { expect, type Page, type Route } from '@playwright/test';
import {
  test,
  TESTIDS,
  TRACE_ID,
  CONVERSATION_ID,
  IAC_TRACE_ID,
  DRIFT_CARD_TRACE_ID,
  sseBody,
  traceResponse,
  iacTraceResponse,
  driftCardTraceResponse,
  decisionsResponse,
  infraGraphResponse,
  conversationsListResponse,
  conversationDetailResponse,
  SECRET_TOKEN_VALUE_OLD,
  SECRET_TOKEN_VALUE_NEW,
  SECRET_URL_VALUE_OLD,
  SECRET_URL_VALUE_NEW,
} from './fixtures';

const ORIGIN = 'http://127.0.0.1:8765';

// Task 3.6 step 2 flipped DEFAULT_VIEW to 'desk', so a BARE url is now the
// approval desk, which renders no composer, no timeline and no decisions rail.
// Everything in this file exercises those, so it navigates explicitly to the
// chat view. The bare front door gets its own dedicated test below rather than
// being every test's incidental starting point.
const CHAT_URL = '/?view=chat';

/**
 * Chat, on an OPEN thread.
 *
 * A FRESH chat is the empty new-chat state (ds-jns PR 3): a greeting, the
 * composer, and four example questions, with the whole transcript region
 * unrendered. Anything that lives IN the transcript — the reasoning groups, the
 * infrastructure panel — is therefore not on a bare `?view=chat` any more, so
 * the specs that exercise those open a persisted thread first. Registration
 * order matches the resume test below: the list glob first, the `/<id>` glob
 * last so it wins for the detail request.
 */
async function gotoOpenThread(page: Page) {
  await page.route('**/conversations**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(conversationsListResponse()),
    }),
  );
  await page.route('**/conversations/**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(conversationDetailResponse()),
    }),
  );
  await page.goto(`/?conversation=${CONVERSATION_ID}`);
  await expect(page.locator(`[data-testid="${TESTIDS.conversationThread}"]`)).toBeVisible();
}

// Seed the operator token the way the deployed e2e does (sessionStorage), before
// any page script runs.
async function seedToken(page: Page, token = 'smoke-token') {
  await page.addInitScript((t) => {
    sessionStorage.setItem('driftscribe_token', t);
  }, token);
}

interface RouteState {
  decisionsStatus: number;
  chatHeaders: Record<string, string>;
  // When > 0, the /chat route holds the response open this long before
  // fulfilling — long enough to observe the in-flight loading shimmer.
  chatDelayMs: number;
}

async function mockData(page: Page, state: RouteState) {
  await page.route('**/decisions**', (route: Route) => {
    if (state.decisionsStatus !== 200) {
      return route.fulfill({
        status: state.decisionsStatus,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'auth required' }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(decisionsResponse(ORIGIN)),
    });
  });

  await page.route('**/trace/**', (route: Route) => {
    // Branch by URL so each decision's trace resolves to its OWN payload — the
    // drift trace carries a decision doc with env diffs (DriftDiffCard); the
    // iac_apply trace has no events + a decision doc; the chat trace has events.
    const url = route.request().url();
    const body = url.includes(DRIFT_CARD_TRACE_ID)
      ? driftCardTraceResponse()
      : url.includes(IAC_TRACE_ID)
        ? iacTraceResponse()
        : traceResponse();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  // InfraDiagram fetches this on mount (for the glanceable badge); mock it for
  // every test so no real infra_reader call escapes the browser.
  await page.route('**/infra/graph', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(infraGraphResponse()),
    }),
  );

  // The rail's conversation list. Empty by default (the chat-native settle after
  // a turn calls loadConversations to refresh the rail); the resume smoke
  // registers its own richer /conversations routes AFTER this, so they win there.
  await page.route('**/conversations**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ conversations: [] }),
    }),
  );

  await page.route('**/chat', async (route: Route) => {
    state.chatHeaders = route.request().headers();
    if (state.chatDelayMs > 0) {
      await new Promise((r) => setTimeout(r, state.chatDelayMs));
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'X-Trace-Id': TRACE_ID, 'Cache-Control': 'no-cache' },
      // Echo a conversation_id so the reply settles into the thread's crew
      // bubble (chat-native), matching prod's persisted-turn path.
      body: sseBody(TRACE_ID, { conversationId: CONVERSATION_ID }),
    });
  });
}

function freshState(decisionsStatus = 200): RouteState {
  return { decisionsStatus, chatHeaders: {}, chatDelayMs: 0 };
}

test.describe('transparency UI (mock smoke)', () => {
  // The Task 3.6 flip in one test: what a judge typing the bare domain gets.
  // Runs against the BUILT bundle served by the real FastAPI shell, so it
  // catches a desk that unit tests render happily but that dies on the real
  // asset/route path.
  test('the bare url is the approval desk, not the chat composer', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await expect(page.getByTestId('approval-desk')).toBeVisible();
    await expect(page.getByTestId('instrument-band')).toBeVisible();
    // The chat shell must be genuinely absent, not merely hidden.
    await expect(page.locator('#chat-form')).toHaveCount(0);
    await expect(page.locator(`[data-testid="${TESTIDS.conversationsPane}"]`)).toHaveCount(0);

    // …and the header nav still reaches chat from there.
    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();
    await expect(page.getByTestId('approval-desk')).toHaveCount(0);
  });

  // The 2026-07-31 merge put the desk card and the estate section in one grid
  // column, and a real layout engine is the only thing that can check they
  // landed in the same one — jsdom has none, so the unit suite cannot see this
  // at all. It is not hypothetical: both declared `max-width: 780px; margin: 0
  // auto`, but as auto-margin grid items they were sized SHRINK-TO-FIT, and the
  // estate's narrower content gave it 384px at left 448 against the desk's
  // 780px at left 250. Two mismatched centered cards stacked in one column is
  // precisely the "the frontend looks bolted on" reading the merge answers.
  //
  // ds-3em adds the ledger card between them, and it is the one of the three
  // most exposed to that failure: its rows are the narrowest content in the
  // column, so shrink-to-fit would put it well under 780 while its two
  // siblings stayed put. This is the browser-level pin of that triple.
  test('the desk, the record and the estate share one column', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');

    const desk = await page.getByTestId('approval-desk').boundingBox();
    const ledger = await page.getByTestId('ledger-strip').boundingBox();
    const estate = await page.getByTestId('estate-view').boundingBox();
    expect(desk, 'desk card must render').not.toBeNull();
    expect(ledger, 'record card must render').not.toBeNull();
    expect(estate, 'estate section must render').not.toBeNull();
    // Exact, not approximate: they are the same column, so any difference is a
    // bug rather than a rounding artifact.
    expect(Math.round(ledger!.x)).toBe(Math.round(desk!.x));
    expect(Math.round(ledger!.width)).toBe(Math.round(desk!.width));
    expect(Math.round(estate!.x)).toBe(Math.round(desk!.x));
    expect(Math.round(estate!.width)).toBe(Math.round(desk!.width));

    // Order, and equal seams — measured against the gap the column DECLARES,
    // which is the only assertion that can fail for the right reason.
    //
    // `.layout` is `flex: 1` and its rows are auto-sized, so before ds-3em the
    // initial `align-content: normal` (stretch) handed the leftover viewport
    // height to the rows: the desk↔estate seam measured 236px here at 1280 wide
    // and 196px at 700, moving with the window rather than with any rule.
    // Equal-gap and non-zero checks both PASS against that behaviour — slack
    // distributes evenly — so neither can tell a designed seam from an
    // accidental one. Comparing to the computed row-gap can.
    const rowGap = await page.evaluate(() =>
      parseFloat(getComputedStyle(document.querySelector('main')!).rowGap),
    );
    expect(rowGap).toBeGreaterThan(0);
    expect(Math.round(ledger!.y - (desk!.y + desk!.height))).toBe(Math.round(rowGap));
    expect(Math.round(estate!.y - (ledger!.y + ledger!.height))).toBe(Math.round(rowGap));
  });

  // Phone width. The estate is on the FRONT DOOR since the merge, so a visitor
  // on a phone meets these rows without clicking anything.
  //
  // Measured against the CARD, not the document, and that distinction is the
  // whole point: `.estate-view` is `overflow: hidden`, so a chip that runs past
  // the card is CLIPPED, never scrolled to. A document-level `scrollWidth ===
  // clientWidth` check — the narrow-width check the plan prescribes, and the one
  // this suite would naturally reach for — passes happily with a control sitting
  // half off the card. It did: at 390px the adopt button was cut by 4px in ja
  // and 16px in en until the row learned to restack (EstateView.svelte's 460px
  // query). Both checks are below; only the first can see that failure.
  //
  // Every chip, not one testid: a drift row ends in exactly one of three (the
  // adopt button, the "PR #n awaiting review" marker, or the mute "adoption
  // status unknown" — which is what THIS fixture produces, since mockData
  // leaves /infra/pending-approvals unrouted and an unsupported absence must
  // not offer an Adopt). They share `.estate-view__chip`, they are the widest
  // thing in a row, and the invariant belongs to all of them equally.
  // Both locales, because the widest string is a DIFFERENT one in each: the EN
  // chip is the widest element in its row, the JA type label is. A single-locale
  // pin here would have been half a guard.
  for (const locale of ['ja', 'en'] as const) {
    test(`at phone width the estate row stays inside its card and does not collide (${locale})`, async ({
      page,
    }) => {
      await seedToken(page);
      await page.addInitScript((l) => localStorage.setItem('driftscribe.locale', l), locale);
      await mockData(page, freshState());
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto('/');

      const estate = page.getByTestId('estate-view');
      await expect(estate).toBeVisible();

      const chips = estate.locator('.estate-view__chip');
      // The fixture's own premise. Without it, a graph fixture that stopped
      // producing chip-bearing rows would leave this passing on an empty set.
      await expect(chips.first()).toBeVisible();

      const card = (await estate.boundingBox())!;
      const padRight = await estate.evaluate((el) => parseFloat(getComputedStyle(el).paddingRight));
      const limit = Math.round(card.x + card.width - padRight);
      for (let i = 0; i < (await chips.count()); i++) {
        const box = (await chips.nth(i).boundingBox())!;
        expect(Math.round(box.x + box.width), `chip ${i} escapes the card`).toBeLessThanOrEqual(
          limit,
        );
      }

      // Fitting inside the card is not enough: the first restack attempt put the
      // type label and the chip on one line, where both are `nowrap` and the
      // type simply ran UNDER the chip. Every element stayed within the card, so
      // the check above passed while the row was unreadable. Overlap is its own
      // failure and needs its own assertion — stated as 2D rect intersection so
      // it holds whatever arrangement the breakpoint chooses.
      const rows = await estate.locator('.estate-view__row').all();
      let checked = 0;
      for (const row of rows) {
        const type = row.locator('.estate-view__type');
        const chip = row.locator('.estate-view__chip');
        if ((await type.count()) === 0 || (await chip.count()) === 0) continue;
        const t = (await type.first().boundingBox())!;
        const c = (await chip.first().boundingBox())!;
        const xOverlap = Math.min(t.x + t.width, c.x + c.width) - Math.max(t.x, c.x);
        const yOverlap = Math.min(t.y + t.height, c.y + c.height) - Math.max(t.y, c.y);
        const label = `${(await type.first().textContent())?.trim()} / ${(
          await chip.first().textContent()
        )?.trim()}`;
        expect(xOverlap > 0 && yOverlap > 0, `type overlaps chip: ${label}`).toBe(false);
        checked++;
      }
      expect(checked, 'no row carried both a type and a chip').toBeGreaterThan(0);

      // …and the page itself still does not scroll sideways.
      const { scrollWidth, clientWidth } = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(scrollWidth).toBe(clientWidth);
    });
  }

  // The ledger card, same width, same reason, and it had no pin of its own until
  // ds-wd2.18 widened its time column from 58px to 104px to fit a date. That
  // column is the row's FIRST grid track and the title's is `1fr` — which is
  // really `minmax(auto, 1fr)`, so it floors at the title's min-content. The
  // strip's narrowest subjects are single unbreakable tokens (a revision or
  // branch name from `rollbackSubject`), which offer no break opportunity, so
  // past that floor the row stops shrinking and runs off a card that is
  // `overflow: hidden` — clipped, never scrolled to.
  //
  // Measured before the fix, with the fixture's `applied` row putting the
  // SealStamp's 30px fourth track in play: 2px over the card at 390 and 72px at
  // 320, against a 58px column that was clean at 390 and already 26px over at
  // 320. `min-width: 0` + `overflow-wrap: anywhere` on the title takes all four
  // widths to 0.
  //
  // Deliberately measures each CELL against the card rather than the document,
  // for the reason the estate test above spells out: the document-level check is
  // blind to a cell sitting off the edge of an overflow-hidden card. Both checks
  // are here; only the first can see that failure.
  test('at phone width the ledger row stays inside its card', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // The default decisions fixture CANNOT see this failure, and that is the
    // whole reason for the override: every subject it produces is prose with
    // spaces in it, so the title's min-content is already a single short word
    // and the `1fr` floor is never reached. Verified — with the shared fixture
    // this test passes with the fix reverted.
    // What reaches the floor is an unbreakable token, which is exactly what
    // `rollbackSubject` yields: it names the env VARIABLES a rollback concerns,
    // and prod holds `PAYMENT_MODE` rows today. Paired with an `applied` row so
    // the SealStamp's 30px fourth track is in play — without it the fourth
    // track is 0px and the row has 30px of slack that hides the overflow.
    // Routes registered later win, so this supersedes mockData's.
    await page.route('**/decisions**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          decisions: [
            {
              decision_id: 'ln-1',
              action: 'iac_apply',
              apply_status: 'applied',
              merge_state: 'merged',
              pr_number: 308,
              pr_title: 'FEATURE_NEW_CHECKOUT_ROLLOUT',
              created_at: '2026-08-08T01:09:00Z',
            },
            {
              decision_id: 'ln-2',
              action: 'rollback',
              created_at: '2026-08-07T22:56:00Z',
              diffs: [
                {
                  name: 'FEATURE_NEW_CHECKOUT',
                  expected: 'off',
                  live: 'on',
                  contract_status: 'present_disallow_manual',
                },
              ],
            },
          ],
        }),
      }),
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    const ledger = page.getByTestId('ledger-strip');
    await expect(ledger).toBeVisible();

    const rows = ledger.locator('.ledger-strip__row');
    // The fixture's own premise, stated so a decisions fixture that stopped
    // producing rows cannot leave this passing on an empty set.
    await expect(rows.first()).toBeVisible();

    // Rect geometry, measured with descendant TRANSFORMS NEUTRALISED, then
    // restored. The SealStamp on an applied row is `transform: rotate(-11deg)`
    // (SealStamp.svelte) and a client rect is the TRANSFORMED box: a 30px square
    // at 11° measures 35.17px, so the seal's rect reads 2.59px past its own cell
    // on each side while sitting ~37px inside the card border. Measured, not
    // assumed — a naive rect sweep fails on a perfectly correct layout, and
    // `offsetLeft` is no escape either, since it is relative to the offsetParent
    // rather than to the padding box this compares against. Suppressing the
    // rotation for the duration of the measurement leaves the layout box, which
    // is the thing the invariant is about.
    const escapes = await ledger.evaluate((card) => {
      const wrap = card.querySelector('.ledger-strip__rows') as HTMLElement;
      const transformed = Array.from(card.querySelectorAll<HTMLElement>('*')).filter(
        (el) => getComputedStyle(el).transform !== 'none',
      );
      const saved = transformed.map((el) => el.style.transform);
      transformed.forEach((el) => (el.style.transform = 'none'));
      try {
        const wrapBox = wrap.getBoundingClientRect();
        const padRight = parseFloat(getComputedStyle(wrap).paddingRight);
        const limit = wrapBox.right - padRight;
        const out: string[] = [];
        Array.from(card.querySelectorAll<HTMLElement>('.ledger-strip__row')).forEach((row, i) => {
          Array.from(row.children).forEach((cell, c) => {
            const box = (cell as HTMLElement).getBoundingClientRect();
            if (box.width === 0) return; // the empty 4th-column placeholder
            if (box.right > limit + 0.5)
              out.push(`row ${i} cell ${c}: ${box.right.toFixed(1)} > ${limit.toFixed(1)}`);
          });
        });
        return out;
      } finally {
        transformed.forEach((el, i) => (el.style.transform = saved[i]));
      }
    });
    expect(escapes, 'a ledger cell escapes its card').toEqual([]);

    // And the card itself does not become horizontally scrollable — the check
    // that catches an overflow the per-cell sweep could miss.
    const card = await ledger.evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }));
    expect(card.scrollWidth).toBeLessThanOrEqual(card.clientWidth);

    const { scrollWidth, clientWidth } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(scrollWidth).toBe(clientWidth);
  });

  // ds-wd2.21: the other half of the same column. ds-wd2.18 gave the strip two
  // time shapes — `May 30, 19:52` on the first row of a day's run and `01:09` on
  // the rest — and left-aligned they shared a LEFT edge, so the clock itself
  // moved ~48px sideways between consecutive rows. `.ledger-strip__time` is
  // right-aligned to put every clock on one x.
  //
  // Only a browser can see this: jsdom runs no cascade and has no layout, so the
  // unit suite would pass with the declaration deleted.
  //
  // Measured with a RANGE over each cell's text, never the cell's own box. The
  // cell is a grid item in a fixed 104px track and `justify-self` defaults to
  // stretch, so its rect is 104px wide with or without the fix — an element-rect
  // version of this test is vacuous by construction. Verified both ways: the
  // range form fails on the reverted CSS, the element-rect form does not.
  test('the ledger clocks share one right edge, dated row or not', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());

    // Built from the RUNNER's own wall clock rather than written as literal `Z`
    // strings, because `sameDay` compares calendar days in the reader's zone: a
    // pair two hours apart in UTC lands on two different local days somewhere in
    // [-12, +14], and the fixture's whole premise is that rows 1-2 share a day
    // and row 3 does not. Anchored on YESTERDAY noon so every row is in the past
    // whatever time the suite runs, and noon ± 2h cannot cross a local midnight
    // even across a DST shift.
    const anchor = new Date();
    anchor.setDate(anchor.getDate() - 1);
    anchor.setHours(12, 0, 0, 0);
    const at = (hoursBack: number) =>
      new Date(anchor.getTime() - hoursBack * 3_600_000).toISOString();

    await page.route('**/decisions**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          decisions: [
            { decision_id: 'lc-1', action: 'no_op', created_at: at(0) },
            { decision_id: 'lc-2', action: 'no_op', created_at: at(2) },
            { decision_id: 'lc-3', action: 'no_op', created_at: at(26) },
          ],
        }),
      }),
    );
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/');

    const ledger = page.getByTestId('ledger-strip');
    await expect(ledger).toBeVisible();

    const runs = await ledger.evaluate((card) =>
      Array.from(card.querySelectorAll('.ledger-strip__time')).map((cell) => {
        const r = document.createRange();
        r.selectNodeContents(cell);
        const box = r.getBoundingClientRect();
        return { text: (cell.textContent ?? '').trim(), left: box.left, right: box.right };
      }),
    );

    // The fixture's premise, asserted rather than assumed: a build whose
    // day-boundary rule stopped firing would render three identical shapes and
    // pass an alignment check trivially.
    const bare = runs.filter((r) => /^\d{1,2}:\d{2}$/.test(r.text));
    const dated = runs.filter((r) => !/^\d{1,2}:\d{2}$/.test(r.text));
    expect(bare.length, `no bare-clock row: ${runs.map((r) => r.text).join(' | ')}`).toBe(1);
    expect(dated.length, `no dated row: ${runs.map((r) => r.text).join(' | ')}`).toBe(2);

    // The invariant. Sub-pixel tolerance only — these are the same five glyphs
    // in the same tabular-nums mono, so anything past a rounding hair means the
    // column is anchored on the wrong side.
    const rights = runs.map((r) => r.right);
    const spread = Math.max(...rights) - Math.min(...rights);
    expect(spread, `clocks do not share a right edge: ${JSON.stringify(runs)}`).toBeLessThan(0.5);

    // And the visible consequence the operator asked for: the dateless row is
    // INDENTED to the clock, not flush with the dates above it.
    expect(bare[0].left).toBeGreaterThan(Math.max(...dated.map((r) => r.left)) + 8);
  });

  // The slim hero is a TYPOGRAPHY change, and typography is the one thing none
  // of the other gates can see: jsdom does not run the cascade, so a unit test
  // asserting the --slim class passes whether or not the rule inside it applies,
  // and the visual rigs measure geometry, not computed font. This shipped dead
  // for exactly that reason — `.approval-desk__calm--slim h2` and
  // `.approval-desk h2` are BOTH (0,2,1) once Svelte scopes them, so the later
  // (31px Mincho) rule won every property and the "slim" strip carried a full
  // hero headline. Assert the computed values, not the class.
  test('the slim hero headline is body-size, not the 31px Mincho hero rule', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // freshState's decisions fixture carries a PENDING approval, which renders
    // the tall pending card and no calm strip at all. Override both lanes to
    // reach the resting state — the default a judge actually lands on. Routes
    // registered later win, so these supersede mockData's.
    await page.route('**/decisions**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ decisions: [] }) }),
    );
    await page.route('**/infra/pending-approvals**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ approvals: [] }) }),
    );
    await page.goto('/');

    // Wait for RESTING specifically. Both slim states carry the class, and the
    // store starts `settled: false` — which deskModel reports as
    // `unknown/loading` (desk.ts) — so a bare class check can be satisfied by
    // the loading strip before the three lanes land. The CSS pin below would
    // still bite either way, but "this is the resting state a judge lands on"
    // would be an unproven claim about which strip was measured.
    await expect(page.getByTestId('approval-desk-resting')).toBeVisible();
    const slim = page.locator('.approval-desk__calm--slim');
    // Fixture premise: the slim wrapper must really be there to measure.
    await expect(slim).toHaveCount(1);
    const h2 = slim.locator('h2');
    await expect(h2).toBeVisible();

    const cs = await h2.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        fontSize: s.fontSize,
        fontWeight: s.fontWeight,
        marginTop: s.marginTop,
        fontFamily: s.fontFamily,
      };
    });
    expect(cs.fontSize, 'slim headline size').toBe('15px');
    expect(cs.fontWeight, 'slim headline weight').toBe('600');
    expect(cs.marginTop, 'slim headline margin').toBe('0px');
    expect(cs.fontFamily, 'slim headline must not use the Mincho hero face').not.toMatch(/Mincho/i);
  });

  test('shell + built assets load with no 404 and render the chrome', async ({ page }) => {
    const bad: string[] = [];
    page.on('response', (r) => {
      if (r.url().includes('/static/') && r.status() >= 400) bad.push(`${r.status()} ${r.url()}`);
    });
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

    await expect(page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.conversationsPane}"]`)).toBeVisible();
    // The front door a fresh chat actually shows (ds-jns PR 3): greeting plus
    // four example questions. This replaced the three empty reasoning groups
    // that used to be asserted here — they live inside the transcript, which a
    // fresh chat does not render, and they are checked with real content in the
    // SSE test below rather than as empty <details> nobody has filled yet.
    await expect(page.getByTestId('chat-empty-greeting')).toBeVisible();
    await expect(page.getByTestId('chat-empty-chip')).toHaveCount(4);
    expect(bad, `static assets must load with no 4xx/5xx: ${bad.join(', ')}`).toHaveLength(0);
  });

  test('chat SSE threads the reply and its reasoning; sends Accept + token; backfills mcp', async ({ page }) => {
    const state = freshState();
    await seedToken(page);
    await mockData(page, state);
    await page.goto(CHAT_URL);

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // The reply lands in the thread's crew bubble (chat-native), alongside the
    // operator's own prompt bubble. There is no standalone hero any more — ds-jns
    // Task 3.3 deleted it — so this is the only place the reply renders at all.
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    await expect(thread).toBeVisible();
    await expect(thread).toContainText('Check payment-demo for drift');
    await expect(thread).toContainText('Found 3 drifted env vars.');

    // the request advertised SSE + carried the token from sessionStorage
    expect(state.chatHeaders['accept'] ?? '').toContain('text/event-stream');
    expect(state.chatHeaders['x-driftscribe-token']).toBe('smoke-token');

    // The reasoning hangs off the turn that produced it now, not off three
    // page-level group accordions. Expanding it is what asks for the trace.
    await page.getByTestId('reasoning-disclosure').click();
    const detail = page.getByTestId('trace-detail');
    await expect(detail).toBeVisible();

    // One INTERLEAVED list, in run order, rather than three sibling panels
    // binned by kind — which is the whole point of the disclosure: what the
    // coordinator thought, then what it called, reads as one sequence.
    await expect(detail.getByTestId('trace-row-thought')).toContainText(
      'Comparing live env to the ops contract',
    );
    // The tool row names the WORKER, not the raw tool symbol
    // (read_live_env_tool -> "Reader (drift)").
    await expect(detail.getByTestId('trace-row-tool')).toContainText('Reader (drift)');
    // …and the side-channel mcp_call, which the STREAM never carries — it only
    // arrives via the /trace backfill, so its presence here proves the merge.
    // Named by SERVER, not by the raw `search_documents` symbol: this surface
    // says what was consulted, not which function was called (the tool-name
    // grounding rule).
    await expect(detail.getByTestId('trace-row-mcp')).toContainText('Developer Knowledge');
  });

  test('a thinking bubble streams in the thread until the reply lands, then fills in place', async ({ page }) => {
    const state = freshState();
    state.chatDelayMs = 800; // hold /chat open so the in-flight state is observable
    await seedToken(page);
    await mockData(page, state);
    await page.goto(CHAT_URL);

    await page.locator(`[data-testid="${TESTIDS.chatPrompt}"]`).fill('Check payment-demo for drift');
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();

    // While the coordinator is working (request in flight, no reply yet) the
    // exchange is already in the thread: the prompt bubble + a live "thinking"
    // crew bubble.
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    const typing = page.locator(`[data-testid="${TESTIDS.threadTyping}"]`);
    await expect(thread).toBeVisible();
    await expect(typing).toBeVisible();

    // Once the reply lands, the typing indicator is replaced by the prose in the
    // SAME bubble — no position hop. The standalone hero this used to also
    // assert against is gone (ds-jns Task 3.3), which is the stronger version
    // of the same claim: there is no second place for a reply to appear.
    await expect(thread).toContainText('Found 3 drifted env vars.');
    await expect(typing).toBeHidden();
  });

  test('auth-required (401) shows the inline AuthPanel instead of window.prompt', async ({ page }) => {
    // No token seeded; first /decisions returns 401.
    const state = freshState(401);
    await mockData(page, state);
    await page.goto(CHAT_URL);

    // The inline panel (role=dialog) appears — NOT a native prompt.
    await expect(page.getByRole('dialog')).toBeVisible();

    // Provide a token; subsequent calls succeed → panel closes, pill shows ok.
    state.decisionsStatus = 200;
    await page.getByRole('textbox', { name: 'Operator token' }).fill('typed-token');
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.locator('#token-status')).toContainText('token ok');
  });

  test('malicious off-origin approval URL renders NO link', async ({ page }) => {
    // Moved to the desk with the decisions themselves: ds-jns Task 3.3 deleted
    // the chat's decisions rail, and the desk's pending hero is what offers an
    // operator the Approve link now. The claim is unchanged and is the reason
    // this test exists — a decision document is coordinator-shaped data, and an
    // `approval_url` pointing off-origin must never become a clickable anchor.
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await expect(page.getByTestId('approval-desk')).toBeVisible();
    // The seeded decisions reached the page (the ledger lists them)…
    await expect(page.locator(`[data-testid="${TESTIDS.ledgerRow}"]`).first()).toBeVisible();
    // …and the off-origin approval_url did not become an anchor ANYWHERE on it.
    await expect(page.locator('a[href*="evil.example"]')).toHaveCount(0);
    // The same-origin one DOES render as a real link. Not pinned to a count:
    // the desk offers it from more than one place (the pending hero and the
    // ledger row for the same decision), and how many doors it has is a layout
    // question, not the security claim under test.
    await expect(page.locator('a[href*="/approvals/ap-1"]').first()).toBeVisible();
  });

  test('a suppressed / dry-run decision says so on the record, above its own rationale', async ({
    page,
  }) => {
    // The record has to be able to contradict itself out loud. This decision's
    // headline is "drift_issue" and its rationale reads like work happened;
    // both are true of the REQUEST and false of the OUTCOME, because the dial
    // was in Observe and the GitHub call was a dry run.
    //
    // Driven by deep link rather than by hunting a row: three seeded rows share
    // the same "drift" ledger line, and this one is named by trace id.
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/?reasoning=cc11bb22cc33dd44ee55ff6600112233');

    const record = page.getByTestId('decision-record');
    await expect(record).toBeVisible();
    await expect(record.getByTestId('decision-autonomy-suppressed')).toContainText('Observe');
    await expect(record.getByTestId('decision-dry-run')).toContainText('dry run');

    // Both read ABOVE the rationale they qualify. A "nothing was created" note
    // placed under the paragraph claiming otherwise is a footnote on something
    // the operator has already believed. Measured, not inferred from DOM order:
    // this is exactly the kind of claim jsdom cannot make.
    const [caveatBottom, proseTop] = await Promise.all([
      record.getByTestId('decision-dry-run').evaluate((el) => el.getBoundingClientRect().bottom),
      record
        .getByTestId('decision-record-prose')
        .evaluate((el) => el.getBoundingClientRect().top),
    ]);
    expect(caveatBottom).toBeLessThanOrEqual(proseTop);
  });

  test('decision github.url: valid github.com link renders, javascript: url does not', async ({ page }) => {
    // Also re-homed by Task 3.3: the rail listed every decision at once and
    // could show all their links side by side. A record shows ONE decision, so
    // the two halves of this claim are now two openings — which is a better
    // test of the gate anyway, since each row is judged on its own.
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    const rows = page.locator(`[data-testid="${TESTIDS.ledgerRow}"]`).filter({ hasText: 'drift' });
    await expect(rows.first()).toBeVisible();

    // Three seeded drift_issue rows: one with a real github.com issue, one with
    // a `javascript:` payload, and one dry run that created nothing and so
    // carries no url at all. Open each and collect what its record offered,
    // rather than indexing by position — the pass/fail must not depend on which
    // way the ledger happens to sort rows a minute apart.
    const hrefs: (string | null)[] = [];
    const n = await rows.count();
    expect(n, 'every drift_issue row must reach the ledger').toBe(3);
    for (let i = 0; i < n; i++) {
      await rows.nth(i).click();
      await expect(page.getByTestId('decision-record')).toBeVisible();
      const link = page.getByTestId('decision-github-link');
      hrefs.push((await link.count()) === 1 ? await link.getAttribute('href') : null);
      if ((await link.count()) === 1) {
        await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
        await expect(link).toHaveAttribute('target', '_blank');
      }
      await rows.nth(i).click(); // collapse before opening the next
    }
    // Exactly one anchor, and it is the github.com one. The javascript: row is
    // rejected by safeGithubHref and renders nothing at all.
    expect(hrefs.filter(Boolean)).toEqual(['https://github.com/acme/ops/issues/99']);
    // Belt: no anchor ANYWHERE on the page ever carried the payload.
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  });

  // ds-jns re-pointed every route to a past decision at a RECORD on the desk
  // that lists it, rather than a replay in the chat column — first the rail's
  // view-reasoning (PR 2), then the rail itself (Task 3.3), which leaves the
  // ledger row as the one door. Everything this test was about survives the
  // move: the curated decision fields, and the note explaining that an empty
  // timeline here is expected rather than a failure to load.
  test('historical iac_apply: desk record with the decision summary + "recorded directly" note', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // Reached by DEEP LINK rather than by a ledger row, because that is the
    // route this decision actually has: the strip caps its rows, and this
    // iac_apply is older than the four the fixture puts above it. A
    // `?reasoning=` naming a decision the list does not show is exactly what
    // the desk's PINNED record exists for, so this doubles as its smoke.
    await page.goto(`/?reasoning=${IAC_TRACE_ID}`);

    // The desk, with the record open on it. No status pill: a record is not a
    // run, so there is no lifecycle to label.
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    const record = page.getByTestId('decision-record');
    await expect(record).toBeVisible();

    // The DecisionSummary card renders the curated, safe fields.
    const summary = record.locator('[data-testid="decision-summary"]');
    await expect(summary).toBeVisible();
    // One name for the action, shared with the record header above it (ds-jns).
    await expect(summary).toContainText('Infrastructure change');
    await expect(summary).toContainText('#47');
    await expect(summary).toContainText('op@example.com');

    // The empty-trace note still explains WHY there is no reasoning: an
    // iac_apply is recorded directly by the approval handler.
    await expect(record.getByTestId('trace-detail-empty')).toContainText('recorded directly');

    // No prose section — this decision carries no rationale or rendered body.
    await expect(record.getByTestId('decision-record-prose')).toHaveCount(0);
  });

  test('the estate section is the resource inventory: drift named, out-of-scope types accounted for, no diagram', async ({
    page,
  }) => {
    // This used to drive InfraDiagram's collapsed panel in the chat transcript.
    // ds-jns Task 3.3 deleted that mount, and it was the panel's last one on the
    // normal (non-preview) path — the desk mounts InfraDiagram only under
    // `?preview_pr=`, where it draws the Mermaid ghost map instead. So the
    // claims move to the surface that carries them now.
    //
    // They are the SAME claims, deliberately: what drifted is named, the count
    // is scope-aware rather than a raw total, resources in types DriftScribe
    // does not manage are accounted for instead of quietly dropped, and nothing
    // here costs a Mermaid import.
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    const estate = page.locator(`[data-testid="${TESTIDS.estateView}"]`);
    await expect(estate).toBeVisible();

    // Scope-aware: the fixture has totals.drift = 2, but one of those is the
    // SECRET — a type DriftScribe doesn't manage. The drift group counts 1 and
    // names it; a "2" here would be the raw total leaking through.
    const driftGroup = page.locator(`[data-testid="${TESTIDS.estateGroupDrift}"]`);
    await expect(driftGroup).toBeVisible();
    await expect(driftGroup).toHaveText(/\b1\b/);
    await expect(estate).toContainText('storefront');
    // …and the managed one is still on the list, under its own group.
    await expect(estate).toContainText('payment-demo');

    // The out-of-scope secret is COUNTED, not listed and not forgotten — and
    // its name never appears (the group is `sensitive`, carries no nodes).
    await expect(page.locator(`[data-testid="${TESTIDS.estateOther}"]`)).toContainText('1');

    // A plain DOM row list. Mermaid stays the PR-preview overlay's business,
    // and a diagram here would be a regression nothing else would catch.
    // Deliberately not `svg` count 0 — the Investigate chip carries an inline
    // icon, so a blanket svg ban would be a rule about icons wearing the name
    // of a rule about diagrams, and the next icon added would "break" it.
    await expect(page.locator('[data-testid="infra-diagram"]')).toHaveCount(0);
    await expect(estate.locator('svg:not([aria-hidden="true"])')).toHaveCount(0);
  });

  test('declared in IaC, not found live: Investigate prefills a Provision draft (no /chat on click), layout holds', async ({
    page,
  }) => {
    // Moved to the desk with the group itself (ds-zld). The claims are the
    // band's own, unchanged: the declaration is shown with its HCL address, the
    // copy says it is evidence rather than proof, Investigate opens a Provision
    // DRAFT and sends nothing, and the row survives narrow viewports on a card
    // that would CLIP an overflow rather than scroll it.
    await seedToken(page);
    await mockData(page, freshState());
    let chatPosts = 0;
    page.on('request', (req) => {
      if (req.url().includes('/chat') && req.method() === 'POST') chatPosts++;
    });
    // mockData leaves /infra/pending-approvals unrouted, which the estate reads
    // as "could not ask GitHub" and answers by SUPPRESSING Adopt (ds-eh6). This
    // test wants both affordances on screen at once, so the lane has to be
    // positively empty rather than unavailable.
    await page.route('**/infra/pending-approvals**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ approvals: [] }),
      }),
    );
    await page.goto('/');

    const estate = page.locator(`[data-testid="${TESTIDS.estateView}"]`);
    await expect(estate).toBeVisible();
    await expect(page.locator(`[data-testid="${TESTIDS.estateGroupUnmatched}"]`)).toBeVisible();
    const row = page.locator(`[data-testid="${TESTIDS.estateUnmatchedRow}"]`);
    await expect(row).toContainText('storefront-old');
    await expect(row).toContainText('google_cloud_run_v2_service.storefront_old');
    await expect(estate).toContainText('did not match the latest Cloud Asset Inventory snapshot');

    // The live drift resource keeps its own Adopt button: a declaration with no
    // resource and a resource with no declaration are different findings, and
    // this group must not have swallowed the other one.
    await expect(page.locator('[data-testid="estate-adopt-btn"]').first()).toBeVisible();

    // Layout holds at real viewports. Scoped to the estate card, NOT the
    // document: `.estate-view` is `overflow: hidden`, so a control pushed past
    // its right edge is clipped rather than scrolled to, and a page-level
    // scrollWidth check would call this clean while Investigate sits half off
    // the card (ds-cmc).
    for (const [name, vp] of [
      ['desktop', { width: 1280, height: 900 }],
      ['tablet', { width: 768, height: 900 }],
      ['phone', { width: 390, height: 844 }],
    ] as const) {
      await page.setViewportSize(vp);
      await expect(row.first()).toBeVisible();
      const overflow = await estate.evaluate((el) => {
        const right = el.getBoundingClientRect().right;
        return Math.max(
          0,
          ...[...el.querySelectorAll('[data-testid="estate-unmatched-row"] *')].map(
            (c) => c.getBoundingClientRect().right - right,
          ),
        );
      });
      expect(overflow, `declaration row overflows the estate card at ${name}`).toBeLessThanOrEqual(1);
    }
    await page.setViewportSize({ width: 1280, height: 900 });

    // Investigate → a fresh Provision draft, prefilled, NOT submitted. It also
    // has to WALK the operator to the composer: the button is on the desk and
    // the composer is not, so a prefill without the navigation would drop the
    // draft into a box nobody can see.
    await page.locator(`[data-testid="${TESTIDS.estateUnmatchedInvestigate}"]`).click();
    const prompt = page.locator('#prompt-input');
    await expect(prompt).toHaveValue(/storefront-old/);
    await expect(prompt).toHaveValue(/do not assume a rename/);
    expect(chatPosts).toBe(0);

    // …and the draft is addressed to PROVISION. This used to read the checked
    // radio, but the crew picker is gone (a thread opens on Explore and moves by
    // handoff), so the draft's crew is no longer rendered anywhere — Investigate
    // stays a deep link that carries explicit intent, and its intent is now
    // invisible until the turn is sent. So send it and read the crew off the
    // request. That is strictly the better assertion: the radio only proved the
    // UI DISPLAYED "provision", while the body proves the turn is really
    // addressed to Provision, which is the thing that mattered all along.
    const chatReq = page.waitForRequest(
      (r) => r.url().includes('/chat') && r.method() === 'POST',
    );
    await page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`).click();
    expect(JSON.parse((await chatReq).postData() ?? '{}').workload).toBe('provision');
  });

  // The old shape of this test — open-trace dims the composer, new chat exits —
  // described a MODE the chat column entered. ds-jns replaced the mode with a
  // destination: the record opens on the desk, and the way back is the nav. No
  // disabled composer, because the composer is not on that page at all.
  test('a ledger row opens its record and puts it in the URL; the nav takes it back off', async ({ page }) => {
    // The route, twice re-pointed and now one click: the decisions rail this
    // used to start from is gone (Task 3.3) and the ledger is on the same page
    // as the record it opens. The round trip is what matters — an operator has
    // to be able to share what they are looking at, and to stop looking at it.
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/');

    await page.locator(`[data-testid="${TESTIDS.ledgerRow}"]`).first().click();
    await expect(page.getByTestId('decision-record')).toBeVisible();
    await expect(page).toHaveURL(/reasoning=/);

    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();
    await expect(page.getByTestId('decision-record')).toHaveCount(0);
    await expect(page).not.toHaveURL(/reasoning=/);
  });

  // ds-akf: jsdom cannot see the cascade, so "the row is a BUTTON now" is the
  // only thing the unit test can prove — not that it still LOOKS like a row.
  // Turning a grid <div> into a <button> puts UA button styles in play, and a
  // ledger row that lost its four-column grid would be a real regression that
  // every green unit test would miss. Pinned on computed style, in a browser.
  test('an openable ledger row is a button that still lays out as a row', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/?view=desk');

    const row = page.getByTestId('ledger-strip-row').first();
    await expect(row).toBeVisible();
    await expect(row).toHaveJSProperty('tagName', 'BUTTON');

    const box = await row.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        display: cs.display,
        tracks: cs.gridTemplateColumns.split(' ').filter(Boolean).length,
        align: cs.alignItems,
        textAlign: cs.textAlign,
        width: el.getBoundingClientRect().width,
        // The parent's CONTENT width, not its border box: `.ledger-strip__rows`
        // carries 40px of horizontal padding, so a border-box comparison would
        // fail against a row that is filling its container correctly.
        parentWidth: (() => {
          const parent = el.parentElement as HTMLElement;
          const pcs = getComputedStyle(parent);
          return (
            parent.clientWidth - parseFloat(pcs.paddingLeft) - parseFloat(pcs.paddingRight)
          );
        })(),
      };
    });
    expect(box.display).toBe('grid');
    expect(box.tracks).toBe(4); // time · glyph · title · stamp
    expect(box.align).toBe('baseline');
    expect(box.textAlign).toBe('left'); // UA buttons center their text
    expect(box.width).toBeCloseTo(box.parentWidth, 0);

    // And opening it puts the record inside the strip, under that row.
    await row.click();
    const record = page.getByTestId('decision-record');
    await expect(record).toBeVisible();
    const inStrip = await record.evaluate(
      (el) => el.closest('[data-testid="ledger-strip"]') !== null,
    );
    expect(inStrip).toBe(true);
  });

  test('drift decision: env-diff card shows non-secret values, redacts secret-named + credentialed-URL values, leaks no raw secret', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // d-drift-1's record, by deep link. Both seeded drift_issue rows render an
    // identical ledger line (same time-and-action shape), so clicking "the
    // drift row" is genuinely ambiguous — and this decision is named by trace
    // id everywhere else in the fixture. Unlike the iac_apply deep link above,
    // this decision IS in the listed rows, so it opens INLINE under its own row
    // rather than pinned above the desk: the same URL, the two placements.
    await page.goto(`/?reasoning=${DRIFT_CARD_TRACE_ID}`);

    const card = page.getByTestId('drift-diff-card');
    await expect(card).toBeVisible();

    // Non-secret var: both values shown verbatim.
    const logRow = card.locator('tr', { hasText: 'LOG_LEVEL' });
    await expect(logRow).toContainText('info');
    await expect(logRow).toContainText('debug');

    // Secret-by-NAME and secret-by-VALUE rows show the redaction marker.
    await expect(card.locator('tr', { hasText: 'API_TOKEN' })).toContainText('(value redacted: secret-like)');
    await expect(card.locator('tr', { hasText: 'ENDPOINT' })).toContainText('(value redacted: secret-like)');

    // Hard guarantee: no raw diff secret value appears anywhere in the rendered DOM —
    // checked both as serialized HTML (attributes included) and as visible text.
    const html = await page.content();
    const body = page.locator('body');
    for (const secret of [
      SECRET_TOKEN_VALUE_OLD, SECRET_TOKEN_VALUE_NEW, SECRET_URL_VALUE_OLD, SECRET_URL_VALUE_NEW,
    ]) {
      expect(html, `raw secret must not appear in DOM html: ${secret}`).not.toContain(secret);
      await expect(body, `raw secret must not appear in body text: ${secret}`).not.toContainText(secret);
    }
  });

  test('conversations: resume a thread from the rail; it survives a reload (P2)', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    // Detail route is registered LAST so it wins for `/conversations/<id>`;
    // the list glob (registered first here) handles `/conversations?limit=...`
    // (the `**/conversations/**` glob needs a trailing `/`, which the query
    // form doesn't have, so the two never collide).
    await page.route('**/conversations**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(conversationsListResponse()),
      }),
    );
    await page.route('**/conversations/**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(conversationDetailResponse()),
      }),
    );
    await page.goto(CHAT_URL);

    // The rail lists the persisted conversation.
    const pane = page.locator(`[data-testid="${TESTIDS.conversationsPane}"]`);
    await expect(pane).toBeVisible();
    await expect(pane).toContainText('prior chat about drift');

    // Resume it → the thread rehydrates with both turns.
    await page.locator(`[data-testid="${TESTIDS.conversationOpen}"]`).first().click();
    const thread = page.locator(`[data-testid="${TESTIDS.conversationThread}"]`);
    await expect(thread).toBeVisible();
    await expect(thread).toContainText('what changed on payment-demo?');
    await expect(thread).toContainText('the env var EXTRA drifted from the contract');

    // Resume-after-reload: the rail rehydrates from /conversations and the
    // thread is reachable again (the durable-thread contract P2 is about).
    await page.reload();
    await expect(pane).toContainText('prior chat about drift');
    await page.locator(`[data-testid="${TESTIDS.conversationOpen}"]`).first().click();
    await expect(
      page.locator(`[data-testid="${TESTIDS.conversationThread}"]`),
    ).toContainText('the env var EXTRA drifted from the contract');
  });

  test('locale defaults to Japanese; the header toggle switches the UI to English', async ({ page }) => {
    // Every other test in this suite runs EN-pinned (see fixtures.ts); this is
    // the one deliberate exception, re-pinning to `ja` (init scripts run in
    // registration order, so this later call overrides the suite-wide pin).
    await page.addInitScript(() => {
      try {
        localStorage.setItem('driftscribe.locale', 'ja');
      } catch {
        /* ignore */
      }
    });
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto(CHAT_URL);

    await expect(page.locator('html')).toHaveAttribute('lang', 'ja');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('送信');

    await page.getByTestId('locale-en').click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await expect(page.locator(`[data-testid="${TESTIDS.chatSubmit}"]`)).toHaveText('Send');
  });
});
