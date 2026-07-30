import { expect, type Page, type Route } from '@playwright/test';
import {
  test,
  TRACE_ID,
  CONVERSATION_ID,
  traceResponse,
  conversationsListResponse,
  conversationDetailResponse,
} from './fixtures';

// Mock-Playwright smoke for browser Back/Forward across the three views
// (ds-7ag.1). This is the case jsdom CANNOT prove: the unit suite spies on
// pushState/replaceState and dispatches a synthetic PopStateEvent, which shows
// the app WRITES history correctly and REACTS to a pop, but never that a real
// browser traversal lands where the operator expects. Only a real engine keeps
// the entry stack, so the actual Back/Forward chain lives here.
//
// The reported failure this guards: click a desk numeral, land on the estate
// view, press Back — and leave the app entirely, because every view write used
// to be a replaceState.

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
  await page.route('**/infra/pending-approvals**', json({ pending: [] }));
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
  test('Back and Forward walk the desk → chat → estate chain', async ({ page }) => {
    await seedToken(page);
    await mockData(page);
    // Land on the bare front door — what a judge typing the domain gets — so the
    // chain below is the real one, not one seeded by an explicit ?view=.
    await page.goto('/');
    await expect(page.getByTestId('approval-desk')).toBeVisible();

    await page.getByTestId('nav-chat').click();
    await expect(page.locator('#chat-form')).toBeVisible();

    await page.getByTestId('nav-estate').click();
    await expect(page.getByTestId('estate-view')).toBeVisible();

    // Back, twice: estate → chat → desk.
    await page.goBack();
    await expect(page.locator('#chat-form')).toBeVisible();
    await expect(page.getByTestId('estate-view')).toHaveCount(0);

    await page.goBack();
    await expect(page.getByTestId('approval-desk')).toBeVisible();
    await expect(page.locator('#chat-form')).toHaveCount(0);

    // Forward returns to chat rather than re-entering the app from scratch.
    await page.goForward();
    await expect(page.locator('#chat-form')).toBeVisible();
  });

  test('Back returns to the desk after an instrument-band numeral sends you to the estate', async ({
    page,
  }) => {
    await seedToken(page);
    await mockData(page);
    await page.goto('/');

    // The exact reported gesture: a desk numeral, then Back.
    await page.getByTestId('instrument-band-drift').click();
    await expect(page.getByTestId('estate-view')).toBeVisible();

    await page.goBack();
    await expect(page.getByTestId('approval-desk')).toBeVisible();
  });

  // A shared link's restoration must not COST a history entry, or the visitor's
  // first Back press looks dead — it pops to the same view they are already on.
  // Proven by landing on a sentinel page first: one Back must leave the app.
  for (const [param, value] of [
    ['conversation', CONVERSATION_ID],
    ['reasoning', TRACE_ID],
    ['ask_pr', '168'],
    ['preview_pr', '168'],
  ] as const) {
    test(`a ?${param}= deep link leaves the app in ONE Back press`, async ({ page }) => {
      await seedToken(page);
      await mockData(page);
      await page.goto('about:blank');

      await page.goto(`/?${param}=${value}`);
      await expect(page.locator('#chat-form')).toBeVisible();

      await page.goBack();
      expect(page.url()).toBe('about:blank');
    });
  }

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
