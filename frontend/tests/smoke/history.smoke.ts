import { expect, type Page, type Route } from '@playwright/test';
import {
  test,
  TRACE_ID,
  CONVERSATION_ID,
  traceResponse,
  conversationsListResponse,
  conversationDetailResponse,
} from './fixtures';

// Mock-Playwright smoke for browser Back/Forward across the views (ds-7ag.1).
// This is the case jsdom CANNOT prove: the unit suite spies on pushState/
// replaceState and dispatches a synthetic PopStateEvent, which shows the app
// WRITES history correctly and REACTS to a pop, but never that a real browser
// traversal lands where the operator expects. Only a real engine keeps the
// entry stack, so the actual Back/Forward chain lives here.
//
// The reported failure this guards: click a desk numeral, land on the estate
// view, press Back — and leave the app entirely, because every view write used
// to be a replaceState.
//
// The 2026-07-31 merge changed what that gesture DOES rather than retiring the
// case. There are two views now, and the estate is a section of the desk, so a
// numeral scrolls instead of navigating. The invariant flips accordingly: the
// click must write NO entry at all, which is a stricter thing to prove than
// writing the right one.

const AUTONOMY = {
  mode: 'propose_apply',
  reason: null,
  actor: null,
  updated_at: null,
  read_error: false,
};
const PAUSE = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };

async function seedToken(page: Page, token = 'smoke-token') {
  await page.addInitScript((t) => {
    sessionStorage.setItem('driftscribe_token', t);
  }, token);
}

async function mockData(page: Page) {
  const json = (body: unknown) => (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/autonomy', json(AUTONOMY));
  await page.route('**/pause', json(PAUSE));
  await page.route('**/decisions**', json({ decisions: [] }));
  // ORDER-SENSITIVE: `**/conversations**` also matches the DETAIL url, and
  // Playwright tries the most recently registered handler first — so the
  // specific detail route must be registered last, not first.
  await page.route('**/conversations**', json(conversationsListResponse()));
  await page.route(
    new RegExp(`/conversations/${CONVERSATION_ID}$`),
    json(conversationDetailResponse()),
  );
  await page.route('**/trace/**', json(traceResponse()));
  // `approvals`, not `pending`: fetchPendingList() requires
  // `Array.isArray(body.approvals)` and returns ok:false otherwise, so the older
  // `{ pending: [] }` made every case here run against a FAILED approvals lane
  // while reading like a healthy empty one. The history assertions held either
  // way, which is exactly why it went unnoticed.
  await page.route('**/infra/pending-approvals**', json({ approvals: [] }));
  await page.route(
    '**/infra/graph',
    json({
      generated_at: null,
      project: 'demo',
      caveat: '',
      degraded: true,
      degraded_reason: 'mock',
      totals: { resources: 0, managed: 0, drift: 0 },
      groups: [],
      edges: [],
    }),
  );
}

test.describe('view history traversal (mock smoke)', () => {
  test('Back and Forward walk the desk → chat → desk chain', async ({ page }) => {
    await seedToken(page);
    await mockData(page);
    // Land on the bare front door — what a judge typing the domain gets — so the
    // chain below is the real one, not one seeded by an explicit ?view=.
    await page.goto('/');
    await expect(page.getByTestId('approval-desk')).toBeVisible();

    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();

    // Back to the desk by the nav, not by Back — this is a third entry, so the
    // traversal below still exercises a stack deeper than one hop.
    await page.getByTestId('nav-desk').click();
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    await expect(page.locator('#chat-form')).toHaveCount(0);

    // Back, twice: desk → chat → desk.
    await page.goBack();
    await expect(page.locator('#chat-form')).toBeVisible();

    await page.goBack();
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    await expect(page.locator('#chat-form')).toHaveCount(0);

    // Forward returns to chat rather than re-entering the app from scratch.
    await page.goForward();
    await expect(page.locator('#chat-form')).toBeVisible();
  });

  // Post-merge the estate is a SECTION of the desk, so the reported gesture is
  // no longer a navigation at all. What has to hold is the stronger statement:
  // the click moves the operator without spending a history entry, so Back
  // still means "the page before this one" rather than "undo that scroll".
  // Proven from a sentinel: one Back must leave the app entirely.
  test('an instrument-band numeral scrolls to the estate and costs no history entry', async ({
    page,
  }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('about:blank');
    await page.goto('/');
    await expect(page.getByTestId('approval-desk')).toBeVisible();

    const before = page.url();
    await page.getByTestId('instrument-band-drift').click();

    // Focus lands on the section — the part a scroll alone would not do, and
    // the reason a keyboard operator ends up where the numeral pointed.
    await expect(page.getByTestId('estate-view')).toBeFocused();
    expect(page.url()).toBe(before);

    await page.goBack();
    expect(page.url()).toBe('about:blank');
  });

  // A shared link's restoration must not COST a history entry, or the visitor's
  // first Back press looks dead — it pops to the same view they are already on.
  // Proven by landing on a sentinel page first: one Back must leave the app.
  //
  // The landing column is not decoration: since ds-jns a bare ?reasoning= names
  // a decision RECORD and ?preview_pr= an estate preview, and both of those
  // live on the desk. Asserting the destination as well as the Back behaviour
  // is what keeps this a deep-link test rather than a history-length one.
  for (const [param, value, landing] of [
    ['conversation', CONVERSATION_ID, 'chat'],
    ['ask_pr', '168', 'chat'],
    ['reasoning', TRACE_ID, 'desk'],
    ['preview_pr', '168', 'desk'],
  ] as const) {
    test(`a ?${param}= deep link lands on the ${landing} and leaves the app in ONE Back press`, async ({
      page,
    }) => {
      await seedToken(page);
      await mockData(page);
      await page.goto('about:blank');

      await page.goto(`/?${param}=${value}`);
      if (landing === 'chat') {
        await expect(page.locator('#chat-form')).toBeVisible();
      } else {
        await expect(page.getByTestId('approval-desk')).toBeVisible();
        await expect(page.locator('#chat-form')).toHaveCount(0);
      }

      await page.goBack();
      expect(page.url()).toBe('about:blank');
    });
  }

  // The retired view id is a raw-string alias now (deeplink.viewFromSearch), so
  // links shared before the merge still resolve. They must resolve the same way
  // any other entry restore does — landing on the merged page WITHOUT costing a
  // history entry, or a visitor's first Back press pops to the page they are
  // already looking at.
  test('a legacy ?view=estate link lands on the desk and leaves the app in ONE Back press', async ({
    page,
  }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('about:blank');

    await page.goto('/?view=estate');
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    // The estate is right there on the same page — the link's subject is not
    // lost, it just is not a separate destination any more.
    await expect(page.getByTestId('estate-view')).toBeAttached();

    await page.goBack();
    expect(page.url()).toBe('about:blank');
  });

  // The restore is view-only by design, so a forward entry can still name a
  // thread this session tore down. The url must stop claiming it rather than
  // sitting there pointing at content that is not on screen.
  test('a forward entry stops claiming a conversation that Back tore down', async ({ page }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('/');
    await page.getByTestId('nav-chat').click();

    // Open the thread from the rail — that writes ?conversation onto this entry.
    await page.getByTestId('conversation-open').click();
    await expect(page.getByTestId('conversation-thread')).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`conversation=${CONVERSATION_ID}`));

    await page.goBack();
    await expect(page.getByTestId('approval-desk')).toBeVisible();

    await page.goForward();
    await expect(page.locator('#chat-form')).toBeVisible();
    // View-only restore: the thread is NOT reopened, so the url drops the param
    // and states the view outright (or a reload would land on the desk).
    await expect(page.getByTestId('conversation-thread')).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get('conversation')).toBeNull();
    expect(new URL(page.url()).searchParams.get('view')).toBe('chat');
  });
});
