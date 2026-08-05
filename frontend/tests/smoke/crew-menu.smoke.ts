import { expect, type Page, type Route } from '@playwright/test';
import { test, decisionsResponse, infraGraphResponse } from './fixtures';

// ── ds-uyo: the crew menu's rows do not move ────────────────────────────────
//
// A geometric invariant, so it runs in a real browser. jsdom has no layout, so
// the unit suite can prove the four lifecycle lines exist and which one is
// painted, and cannot prove the one thing that actually breaks the control: a
// detail region that grows as the operator moves down the list shifts every row
// below it, and the row under the cursor stops being the row they aimed at.
//
// "Grows away from the anchor" is not a fix on its own — the rows still move.
// The implementation stacks all four lines in ONE grid cell so the region is
// always as tall as the tallest of them, and this is what checks that claim
// rather than trusting it: walk every row, and require each row's box to be
// byte-identical before and after the region is populated.
//
// Both locales, because the lines are translated and JA wraps differently: a
// height reserved by EN content is not a height reserved by JA content. (This
// is exactly why the region reserves its height from CONTENT rather than from a
// magic number — a number would need re-tuning per locale and would go stale
// silently.)

async function seed(page: Page, locale: 'en' | 'ja') {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'smoke-token');
    localStorage.setItem('driftscribe_tour_done', '1');
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
    localStorage.setItem('driftscribe.locale', l);
  }, locale);
}

async function mock(page: Page, origin: string) {
  const json = (body: unknown) => (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/autonomy', json({ mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false }));
  await page.route('**/pause', json({ paused: false, reason: null, actor: null, updated_at: null, read_error: false }));
  await page.route('**/infra/graph', json(infraGraphResponse()));
  await page.route('**/infra/pending-approvals', json({ approvals: [] }));
  await page.route('**/decisions**', json(decisionsResponse(origin)));
  await page.route('**/capabilities', json({ capabilities: [] }));
  await page.route('**/conversations**', json({ conversations: [] }));
}

const CREWS = ['drift', 'upgrade', 'provision', 'explore'] as const;

/** Every option's border box, rounded to a tenth of a pixel. */
async function rowBoxes(page: Page): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const c of CREWS) {
    const box = await page.getByTestId(`crew-menu-option-${c}`).boundingBox();
    expect(box, `crew-menu-option-${c} has no box`).not.toBeNull();
    out[c] = `${box!.x.toFixed(1)},${box!.y.toFixed(1)},${box!.width.toFixed(1)},${box!.height.toFixed(1)}`;
  }
  return out;
}

for (const locale of ['en', 'ja'] as const) {
  test(`crew menu rows never move as the detail region fills (${locale})`, async ({ page, baseURL }) => {
    await seed(page, locale);
    await mock(page, baseURL!);
    await page.goto('/?view=chat');

    const trigger = page.getByTestId('crew-menu-trigger');
    await expect(trigger).toBeVisible();
    await trigger.click();
    await expect(page.getByTestId('crew-menu-popup')).toBeVisible();

    // The premise. Opening focuses the SELECTED option, so exactly one line is
    // showing and it is Explore's — the SHORTEST of the four. That ordering is
    // what gives the walk below its teeth: a region sized to whatever is
    // showing when it opens would be sized to the short line, and would then
    // have to grow for Anchor's. Baseline first, longer lines after.
    const painted = page.locator('.crew-menu__lifecycle--on');
    await expect(painted).toHaveCount(1);
    const opening = (await painted.textContent())!.trim();
    expect(opening.length).toBeGreaterThan(0);

    const before = await rowBoxes(page);

    // Walk every row. Each paints a DIFFERENT line, and that is asserted rather
    // than assumed: if the region stopped switching lines, every comparison
    // here would pass while comparing one picture to itself.
    const seen = new Set<string>([opening]);
    for (const c of CREWS) {
      await page.getByTestId(`crew-menu-option-${c}`).hover();
      await expect(painted).toHaveCount(1);
      seen.add((await painted.textContent())!.trim());
      expect(await rowBoxes(page), `rows moved while ${c}'s line was showing`).toEqual(before);
    }
    expect(seen.size, 'the detail region never changed what it was showing').toBe(CREWS.length);
  });
}
