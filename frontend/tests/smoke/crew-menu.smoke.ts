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

// ── The Tab contract, which only a real browser can check ───────────────────
//
// jsdom moves focus for .focus() and for nothing else — it has no tab order at
// all — so every unit test in CrewMenu.test.ts is blind to this. That blindness
// hid a real defect: the roving tab stop was pinned to the SELECTED option
// rather than the FOCUSED one, so after arrowing away, Tab found another
// tabbable option still inside the menu, focus never left the root, the
// focusout dismissal never fired, and the operator was bounced back to the row
// they had just navigated away from. Nothing else here could see it: the
// focus-ring state walks with ArrowDown by construction, and the no-reflow spec
// never leaves the list.
test('Tab out of the open menu closes it and lands in the prompt', async ({ page, baseURL }) => {
  await seed(page, 'en');
  await mock(page, baseURL!);
  await page.goto('/?view=chat');

  const trigger = page.getByTestId('crew-menu-trigger');
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('crew-menu-popup')).toBeVisible();

  // Arrow AWAY from the selected option — that is the whole precondition. Tab
  // from the selected row would leave correctly even with the stop misplaced,
  // which is exactly how this would have shipped looking fine.
  await page.keyboard.press('ArrowDown');
  const focusedTestId = () =>
    page.evaluate(() => document.activeElement?.getAttribute('data-testid') ?? null);
  expect(await focusedTestId()).toBe('crew-menu-option-drift');

  await page.keyboard.press('Tab');
  await expect(page.getByTestId('crew-menu-popup')).toBeHidden();
  expect(await focusedTestId()).toBe('chat-prompt');
});

// Shift+Tab is deliberately NOT symmetrical, and that is worth pinning rather
// than leaving to be re-litigated: it lands on the trigger, which is part of
// the same control and steps back into the list with ArrowDown. The menu stays
// up. Leaving the control entirely is the NEXT Shift+Tab, and that closes it.
test('Shift+Tab returns to the trigger with the menu up, then leaves', async ({ page, baseURL }) => {
  await seed(page, 'en');
  await mock(page, baseURL!);
  await page.goto('/?view=chat');

  const trigger = page.getByTestId('crew-menu-trigger');
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('crew-menu-popup')).toBeVisible();
  await page.keyboard.press('ArrowDown');

  await page.keyboard.press('Shift+Tab');
  await expect(page.getByTestId('crew-menu-popup')).toBeVisible();
  await expect(trigger).toBeFocused();

  await page.keyboard.press('Shift+Tab');
  await expect(page.getByTestId('crew-menu-popup')).toBeHidden();
});

// ── The trigger stands as tall as the field beside it ───────────────────────
//
// Geometry again, so again a real browser: jsdom computes no box at all, and
// the unit suite would happily pass a trigger that renders as a stub.
//
// The trigger's height is derived from the prompt field's box arithmetic in a
// DIFFERENT file, which is precisely the arrangement that goes stale quietly —
// nothing errors when .chat-form__input's padding changes, the row just starts
// looking wrong again. Measuring the two against each other is what makes the
// derivation load-bearing instead of decorative.
//
// Both locales: the crew names differ in width per locale and Send's label
// changes size, so a row that balances in EN is not thereby balanced in JA.
for (const locale of ['en', 'ja'] as const) {
  test(`the composer's resting controls share one height (${locale})`, async ({ page, baseURL }) => {
    await seed(page, locale);
    await mock(page, baseURL!);
    await page.goto('/?view=chat');

    const trigger = page.getByTestId('crew-menu-trigger');
    await expect(trigger).toBeVisible();

    const box = async (id: string) => {
      const b = await page.getByTestId(id).boundingBox();
      expect(b, `${id} has no box`).not.toBeNull();
      return b!;
    };
    const t = await box('crew-menu-trigger');
    const input = await box('chat-prompt');
    const send = await box('chat-submit');

    // Within a pixel, not equal to the decimal: the field's resting height is
    // set by autoResize() from scrollHeight, which is an INTEGER, while the
    // trigger's comes from a calc that lands on a fraction. A pixel of slack is
    // the rounding; nine pixels was the defect.
    expect(Math.abs(t.height - input.height), 'crew trigger vs prompt field').toBeLessThanOrEqual(1);
    expect(Math.abs(send.height - input.height), 'Send vs prompt field').toBeLessThanOrEqual(1);
    // Tops flush too — a matched height that started 9px lower would still read
    // as a broken row.
    expect(Math.abs(t.y - input.y), 'crew trigger vs prompt field, top edge').toBeLessThanOrEqual(1);

    // The reason neither is simply stretched to the row: the FIELD is what grows
    // with a multi-line prompt. Stretching its neighbours to match turned Send
    // into a 126px navy slab sized by how much the operator happened to type.
    // Pinning it here keeps a later "just let them stretch" from silently
    // undoing the two rules above.
    await page.getByTestId('chat-prompt').click();
    await page.keyboard.type('one');
    for (let n = 0; n < 4; n++) {
      await page.keyboard.press('Shift+Enter');
      await page.keyboard.type(`line ${n + 2}`);
    }
    const grownInput = await box('chat-prompt');
    expect(grownInput.height, 'the prompt field did not actually grow').toBeGreaterThan(input.height + 20);
    const grownTrigger = await box('crew-menu-trigger');
    const grownSend = await box('chat-submit');
    expect(grownTrigger.height, 'the pill grew with the prompt').toBeCloseTo(t.height, 0);
    expect(grownSend.height, 'Send grew with the prompt').toBeCloseTo(send.height, 0);
    // Still one row: all three tops flush while the field grows downward out of
    // it. A height that held while the control drifted down the row would look
    // no better than the original defect.
    expect(Math.abs(grownTrigger.y - grownInput.y), 'pill top vs grown field').toBeLessThanOrEqual(1);
    expect(Math.abs(grownSend.y - grownInput.y), 'Send top vs grown field').toBeLessThanOrEqual(1);
  });
}
