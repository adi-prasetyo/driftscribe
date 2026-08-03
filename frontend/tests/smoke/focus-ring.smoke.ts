import { expect, type Page, type Route } from '@playwright/test';
import { test, decisionsResponse, infraGraphResponse } from './fixtures';

// ── ds-2fp: in these states, no focus ring is clipped and every control has one ─
//
// A geometric invariant, so it runs in a real browser: jsdom has no layout, and
// a CSS-text guard cannot see that an ancestor four levels up removes 4px of a
// ring. This Tabs through the BUILT app and, at every focus stop, measures the
// settled focus indicator against the padding box of every ancestor that
// actually clips it.
//
// It replaced a grep for `overflow: hidden`, which flagged twelve components of
// which eleven never clip anything. The measurement is the finding.
//
// SCOPE, because the title overclaims if read loosely: Chromium only, and only
// the states enumerated below. A control reachable solely through some other
// state is unmeasured. Rounded-corner clipping, occlusion by siblings, clipping
// at the visual viewport, and transform scaling are NOT modelled — axis-aligned
// boxes against DOM ancestors cannot see any of them.
//
// AND ONE BLIND SPOT WORTH NAMING, because it is caused by the fix for (5)
// below: everything here runs under prefers-reduced-motion, so a ring clipped
// only DURING an animation cannot be seen by this suite. That is not
// hypothetical — AutonomyPill focuses its reason input on the tick after the
// confirm row mounts, so on the normal 200ms path the input is focused while
// Svelte's own injected `overflow: hidden` is still on the row (ds-b74).
//
// FIVE ways this check reported confidently while being wrong. All five are
// fixed below, and they are why the assertions carry positive controls. Note
// they run in BOTH directions — a guard that only fails safe is still lying:
//   1. FALSE NEGATIVE — `.ds-btn` transitions box-shadow, so a ring read
//      straight after Tab is mid-flight and computes to ~0px spread, which
//      scores as "fits". Every transitioned control was silently cleared.
//   2. FALSE POSITIVE — a DOM ancestor is not necessarily a CLIPPING ancestor:
//      a top-layer <dialog> escapes ancestor overflow entirely. Without
//      `controlFits`, the search modal's rows all reported as "clipped by
//      .rails by 658px".
//   3. FALSE NEGATIVE — any persistent shadow counted as a focus indicator, so
//      an elevation shadow or an armed stroke satisfied a control that does
//      nothing at all on focus. The indicator must now DIFFER from the same
//      element's resting style (SNAPSHOT_RESTING).
//   4. FALSE POSITIVE — that resting snapshot was taken while a modal had
//      already autofocused its input, recording the FOCUSED style as "resting"
//      and then accusing the control of not changing. Hence the blur() first.
//   5. FALSE POSITIVE — measuring a popover mid-slide. A CSS-only motion
//      override does not stop Svelte's JS transitions, so the pause popover was
//      measured 34px tall on its way to 186px and its buttons looked clipped.
//      Three runs gave three different heights and three different figures; the
//      fix is the app's own reduced-motion path (see seed()/settle()).
//
// A transparent indicator also used to count; alpha must now be > 0.

const AUTONOMY = { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false };
const PAUSE = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };

// Controls knowingly shipping without a focus indicator, keyed by testid and
// carrying the bead that owns the decision, so the exemption stays visible
// rather than quietly widening. Adding a name here is a design decision.
const NO_INDICATOR_ALLOWED: Record<string, string> = {
  // `.chat-form__input:focus-visible { box-shadow: none }` is deliberate: the
  // composer should look identical focused and at rest, leaving the caret as the
  // only cue. Defensible for a text field under WCAG 2.4.7. Tracked for a design
  // call rather than changed inside a clipping fix.
  'chat-prompt': 'ds-tr5',
};

async function seed(page: Page, locale: 'en' | 'ja' = 'en') {
  // Set BEFORE any navigation or interaction — see freezeMotion().
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript((l) => {
    sessionStorage.setItem('driftscribe_token', 'smoke-token');
    localStorage.setItem('driftscribe_tour_done', '1');
    localStorage.setItem('driftscribe_demo_notice_dismissed', '1');
    localStorage.setItem('driftscribe.locale', l);
  }, locale);
}

// Measure the SETTLED indicator, never one mid-flight (header note 1).
//
// This uses the app's OWN reduced-motion path rather than a test-only override,
// which matters because a CSS-only override does not stop Svelte's JS-driven
// transitions. `base.css` zeroes every CSS transition/animation under
// `prefers-reduced-motion: reduce`, and `motionMs()` returns 0 so `transition:
// slide` completes instantly. An earlier `addStyleTag` version handled only the
// CSS half and produced FLAKY FALSE POSITIVES: the pause popover was measured
// while still sliding open, 34px tall on its way to 186px, so its buttons sat at
// the container's edge and their rings looked clipped. Three runs, three
// different heights, three different "cut" figures.
//
// Emulation is applied in seed(); this waits for layout to actually settle.
async function settle(page: Page) {
  await page.evaluate(
    () => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
  );
}

async function mock(page: Page, origin: string) {
  const json = (body: unknown) => (route: Route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/autonomy', json(AUTONOMY));
  await page.route('**/pause', json(PAUSE));
  await page.route('**/infra/graph', json(infraGraphResponse()));
  await page.route('**/infra/pending-approvals', json({ approvals: [] }));
  await page.route('**/decisions**', json(decisionsResponse(origin)));
  await page.route('**/capabilities', json({ capabilities: [] }));
}

const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex]';

// Stamp every focusable element with a stable index and record its RESTING
// indicator. Two jobs: it gives the sweep an exact identity to detect a
// completed cycle (a truncated selector+text key collides between sibling rows),
// and it is what makes "focus actually changes something" checkable.
// Run AFTER the state is reached, since opening a popover adds elements.
const SNAPSHOT_RESTING = (sel: string) => {
  const w = window as unknown as { __resting: Record<string, string> };
  // A modal autofocuses its input, so snapshotting straight away records that
  // control's FOCUSED style as its resting style — after which "focus changes
  // nothing" is true by construction and the control is falsely accused. Blur
  // first; it also makes the Tab traversal start from a known place.
  (document.activeElement as HTMLElement | null)?.blur();
  w.__resting = {};
  let i = 0;
  for (const el of Array.from(document.querySelectorAll(sel))) {
    const idx = String(i++);
    el.setAttribute('data-focus-probe', idx);
    const cs = getComputedStyle(el);
    w.__resting[idx] = `${cs.boxShadow}|${cs.outlineStyle}|${cs.outlineWidth}|${cs.outlineOffset}|${cs.outlineColor}`;
  }
  return i;
};

// Runs in the page against document.activeElement.
const PROBE = () => {
  const el = document.activeElement as HTMLElement | null;
  if (!el || el === document.body || el === document.documentElement) return null;

  const describe = (n: Element) => {
    const t = n.getAttribute('data-testid');
    const cls = (n.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 2).join('.');
    return `${n.tagName.toLowerCase()}${t ? `[${t}]` : ''}${cls ? `.${cls}` : ''}`;
  };

  // Paren-aware top-level split: box-shadow layers contain rgb(a, b, c).
  const splitLayers = (v: string) => {
    const out: string[] = [];
    let depth = 0;
    let cur = '';
    for (const ch of v) {
      if (ch === '(') depth++;
      else if (ch === ')') depth--;
      if (ch === ',' && depth === 0) {
        out.push(cur);
        cur = '';
      } else cur += ch;
    }
    if (cur.trim()) out.push(cur);
    return out;
  };

  /**
   * Does this colour paint anything? Named for what it checks — an earlier
   * version was called `isOpaque` while actually meaning "alpha > 0", and it
   * only understood rgb()/rgba(). This app already emits
   * `color(srgb 0.92 0.95 0.99 / 0.6)` for a color-mix fill, so a modern
   * notation at zero alpha would have sailed through as a visible indicator.
   * Unrecognised notations FAIL CLOSED (reported as painting nothing) so the
   * gap surfaces as a test failure rather than a silent pass.
   */
  const paintsSomething = (v: string) => {
    if (/\btransparent\b/i.test(v)) return false;
    const fn = /\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(([^)]*)\)/i.exec(v);
    if (fn) {
      const slash = /\/\s*([\d.]+%?)\s*$/.exec(fn[2]);
      if (slash) return parseFloat(slash[1]) > 0;
      const parts = fn[2].split(',').map((x) => x.trim());
      if (/^rgba|hsla$/i.test(fn[1]) && parts.length === 4) return parseFloat(parts[3]) > 0;
      return true; // recognised notation, no alpha component -> opaque
    }
    if (/#[0-9a-fA-F]{3,8}\b/.test(v)) {
      const hex = /#([0-9a-fA-F]{3,8})\b/.exec(v)![1];
      if (hex.length === 4) return parseInt(hex[3] + hex[3], 16) > 0;
      if (hex.length === 8) return parseInt(hex.slice(6), 16) > 0;
      return true;
    }
    if (/\b(currentcolor|[a-z]{3,20})\b/i.test(v)) return true; // named colour
    throw new Error(`cannot tell whether this colour paints anything: ${v}`);
  };

  const cs = getComputedStyle(el);

  // `outset` is how far the indicator paints OUTSIDE the border box — the part
  // an ancestor's overflow can remove. `inset` records an indicator drawn
  // inward, which is valid and which THIS check cannot fault. Not "unclippable":
  // a rounded or shaped clip reaching inside the border box, or another painted
  // layer on top, can still swallow it — see the scope note in the header.
  // Conflating the two would score an inset ring as "no indicator".
  let outset = 0;
  let inset = false;
  const ow = parseFloat(cs.outlineWidth) || 0;
  if (cs.outlineStyle !== 'none' && ow > 0 && paintsSomething(cs.outlineColor)) {
    const eff = ow + (parseFloat(cs.outlineOffset) || 0);
    if (eff > 0) outset = Math.max(outset, eff);
    else inset = true;
  }
  if (cs.boxShadow && cs.boxShadow !== 'none') {
    for (const layer of splitLayers(cs.boxShadow)) {
      if (!paintsSomething(layer)) continue;
      const px = (layer.match(/-?\d*\.?\d+px/g) || []).map(parseFloat);
      const [ox = 0, oy = 0, blur = 0, spread = 0] = px;
      const extent = spread + blur + Math.max(Math.abs(ox), Math.abs(oy));
      if (/\binset\b/.test(layer)) {
        if (extent > 0) inset = true;
      } else {
        outset = Math.max(outset, extent);
      }
    }
  }

  // Does focus CHANGE anything? A control can carry a permanent elevation shadow
  // and do nothing at all on focus; without this it would pass (header note 3).
  const idx = el.getAttribute('data-focus-probe');
  const resting = (window as unknown as { __resting?: Record<string, string> }).__resting?.[idx ?? ''];
  const nowKey = `${cs.boxShadow}|${cs.outlineStyle}|${cs.outlineWidth}|${cs.outlineOffset}|${cs.outlineColor}`;
  const changesOnFocus = resting === undefined ? null : resting !== nowKey;

  const r = el.getBoundingClientRect();
  const need = { left: r.left - outset, top: r.top - outset, right: r.right + outset, bottom: r.bottom + outset };
  const EPS = 0.5;

  const clippers: string[] = [];
  let a = el.parentElement;
  while (a) {
    const acs = getComputedStyle(a);
    const ox = acs.overflowX;
    const oy = acs.overflowY;
    const clipsX = ox !== 'visible';
    const clipsY = oy !== 'visible';
    const clipPath = acs.clipPath && acs.clipPath !== 'none';
    const paintContain = /paint|strict|content/.test(acs.contain || '');
    if (clipsX || clipsY || clipPath || paintContain) {
      const ar = a.getBoundingClientRect();
      const pb = {
        left: ar.left + (parseFloat(acs.borderLeftWidth) || 0),
        top: ar.top + (parseFloat(acs.borderTopWidth) || 0),
        right: ar.right - (parseFloat(acs.borderRightWidth) || 0),
        bottom: ar.bottom - (parseFloat(acs.borderBottomWidth) || 0),
      };
      // Containment precondition (header note 2). Only an ancestor the control
      // genuinely sits inside can be clipping its ring. If the control itself
      // sticks out it escaped the clip (fixed / absolute / top-layer) or is
      // scrolled out — neither is a focus-ring defect. This is deliberately
      // conservative: it can miss a control that is BOTH partly scrolled out and
      // clipped, in exchange for not inventing 658px phantoms.
      const controlFits =
        r.left >= pb.left - EPS && r.right <= pb.right + EPS &&
        r.top >= pb.top - EPS && r.bottom <= pb.bottom + EPS;
      if (controlFits) {
        const sides: string[] = [];
        if (clipsX || clipPath || paintContain) {
          if (need.left < pb.left - EPS) sides.push(`left ${(pb.left - need.left).toFixed(1)}px`);
          if (need.right > pb.right + EPS) sides.push(`right ${(need.right - pb.right).toFixed(1)}px`);
        }
        if (clipsY || clipPath || paintContain) {
          if (need.top < pb.top - EPS) sides.push(`top ${(pb.top - need.top).toFixed(1)}px`);
          if (need.bottom > pb.bottom + EPS) sides.push(`bottom ${(need.bottom - pb.bottom).toFixed(1)}px`);
        }
        if (sides.length) clippers.push(`${describe(a)} (overflow ${ox}/${oy}) cuts ${sides.join(', ')}`);
      }
    }
    a = a.parentElement;
  }

  return {
    idx,
    sel: describe(el),
    testid: el.getAttribute('data-testid'),
    text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
    focusVisible: el.matches(':focus-visible'),
    outset,
    inset,
    changesOnFocus,
    clippers,
  };
};

type Stop = NonNullable<ReturnType<typeof PROBE>>;

const TAB_CAP = 160;

async function sweep(page: Page): Promise<{ rows: Stop[]; cycled: boolean; total: number }> {
  const total = await page.evaluate(SNAPSHOT_RESTING, FOCUSABLE);
  const seen = new Set<string>();
  const rows: Stop[] = [];
  let cycled = false;
  let first: string | null = null;
  for (let i = 0; i < TAB_CAP; i++) {
    await page.keyboard.press('Tab');
    const stop = await page.evaluate(PROBE);
    if (!stop) continue;
    // Identity is the stamped index, not selector+text: sibling rows share a
    // truncated label and would end the traversal on the second one.
    const key = stop.idx ?? `${stop.sel}|${stop.text}`;
    if (seen.has(key)) {
      // Only a return to the FIRST stop proves the whole cycle was walked. Any
      // repeat used to count, so a focus trap bouncing between two late controls
      // ended the sweep with everything before them unvisited — and passing.
      cycled = key === first;
      break;
    }
    if (first === null) first = key;
    seen.add(key);
    rows.push(stop);
  }
  return { rows, cycled, total };
}

async function assertFocusRingsIntact(
  page: Page,
  label: string,
  opts: { minStops: number; sentinel?: string },
) {
  const { rows, cycled, total } = await sweep(page);

  // ── Positive controls, FIRST. Every assertion below is satisfied by an empty
  // sweep, so without these a broken probe reads as a clean app.
  expect(rows.length, `${label}: sweep found ${rows.length} focus stops, expected ≥${opts.minStops}`).toBeGreaterThanOrEqual(opts.minStops);
  expect(
    cycled,
    `${label}: Tab traversal never returned to its first stop (visited ${rows.length} of ${total} focusable elements in ${TAB_CAP} presses), so coverage is unknown and a pass here would be silent truncation`,
  ).toBe(true);
  expect(
    rows.filter((r) => r.outset > 0 || r.inset).length,
    `${label}: not one control reported a focus indicator — the probe cannot see rings, so a pass proves nothing`,
  ).toBeGreaterThan(0);
  if (opts.sentinel) {
    expect(
      rows.map((r) => r.testid),
      `${label}: traversal never reached ${opts.sentinel}, so the part of the UI this state exists to cover was not measured`,
    ).toContain(opts.sentinel);
  }
  // Reaching a control by Tab must genuinely put it in :focus-visible, or every
  // focus-styled assertion below is measuring the resting state.
  const notFocusVisible = rows.filter((r) => !r.focusVisible).map((r) => `  ${r.sel} "${r.text}"`);
  expect(notFocusVisible, `${label}: Tab-reached control did not match :focus-visible\n${notFocusVisible.join('\n')}`).toEqual([]);

  // ── The invariant.
  const clipped = rows
    .filter((r) => r.clippers.length > 0)
    .map((r) => `  ${r.sel} "${r.text}" (ring ${r.outset}px) — ${r.clippers.join('; ')}`);
  expect(clipped, `${label}: focus ring clipped away by an ancestor\n${clipped.join('\n')}`).toEqual([]);

  const allowed = (r: Stop) => r.testid !== null && r.testid in NO_INDICATOR_ALLOWED;
  const missing = rows
    // `null` means the element was not present at snapshot time, so whether
    // focus changes it is UNKNOWN — treated as missing, not as fine.
    .filter((r) => (r.outset === 0 && !r.inset) || r.changesOnFocus !== true)
    .filter((r) => !allowed(r))
    .map((r) => `  ${r.sel} "${r.text}"${r.changesOnFocus === false ? ' (identical focused and at rest)' : ''}${r.changesOnFocus === null ? ' (appeared after the resting snapshot — focus delta unknown)' : ''}`);
  expect(missing, `${label}: control has no focus indicator\n${missing.join('\n')}`).toEqual([]);
}

const emptyConversations = (page: Page) =>
  page.route('**/conversations**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ conversations: [] }) }),
  );

test.describe('focus rings are never clipped (ds-2fp)', () => {
  test('desk', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=desk');
    await expect(page.getByTestId('autonomy-pill-toggle')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'desk', { minStops: 10, sentinel: 'autonomy-pill-toggle' });
  });

  // The app DEFAULTS to Japanese; every other smoke pins English. Different copy
  // means different control widths, so a ring that fits in EN can clip in JA.
  test('desk in Japanese', async ({ page, baseURL }) => {
    await seed(page, 'ja');
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=desk');
    await expect(page.getByTestId('autonomy-pill-toggle')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'desk (ja)', { minStops: 10, sentinel: 'autonomy-pill-toggle' });
  });

  test('chat', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=chat');
    await expect(page.getByTestId('chat-prompt')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'chat', { minStops: 8, sentinel: 'chat-submit' });
  });

  // The reason this spec exists: `.autonomy-segments` is overflow:hidden and cut
  // 4px off all three segment rings — top and bottom on every one, plus the
  // outer edge on the first and last.
  test('autonomy popover — the segmented dial', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=desk');
    await page.getByTestId('autonomy-pill-toggle').click();
    await expect(page.getByTestId('autonomy-popover')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'autonomy popover', {
      minStops: 10,
      sentinel: 'autonomy-mode-observe',
    });
  });

  // Armed is a state you can be focused IN (activate, then Shift+Tab back), and
  // it is the one state where the focus ring shares the control with another
  // indicator. Nothing else in the suite arms a segment.
  test('autonomy popover — a segment armed and focused', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=desk');
    await page.getByTestId('autonomy-pill-toggle').click();
    await expect(page.getByTestId('autonomy-popover')).toBeVisible();
    await page.getByTestId('autonomy-mode-observe').click();
    await expect(page.getByTestId('autonomy-mode-observe')).toHaveClass(/autonomy-segment--armed/);
    await settle(page);
    await assertFocusRingsIntact(page, 'autonomy popover (armed)', {
      minStops: 8,
      sentinel: 'autonomy-mode-observe',
    });
  });

  // PauseBanner is a different component from PausePill and only renders while
  // the estate is paused, so nothing else in the suite reaches its confirm row —
  // which carried the identical `overflow: hidden` defect as AutonomyPill's.
  test('pause banner — confirm row open', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.route('**/pause', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          paused: true,
          reason: 'focus-ring smoke',
          actor: 'smoke',
          updated_at: '2026-08-03T00:00:00Z',
          read_error: false,
        }),
      }),
    );
    // PauseBanner renders inside .chat-area, so it exists on the chat view only.
    await page.goto('/?view=chat');
    await page.getByTestId('pause-toggle').click();
    await expect(page.getByTestId('pause-confirm-row')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'pause banner (confirming)', {
      minStops: 8,
      sentinel: 'pause-confirm',
    });
  });

  test('pause popover', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    await page.goto('/?view=desk');
    await page.getByTestId('pause-pill-toggle').click();
    await expect(page.getByTestId('pause-popover-confirm')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'pause popover', { minStops: 10, sentinel: 'pause-popover-confirm' });
  });

  // A SCROLL container clips too, and at its own edge: scrollIntoView aligns a
  // focused row's border box, so the ring lands outside the scrollport.
  test('conversations search modal — scrolled list', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    // The rail only offers search once it actually caps the list, so a short
    // fixture never renders the trigger and this state would silently not exist.
    await page.route('**/conversations**', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          conversations: Array.from({ length: 30 }, (_, i) => ({
            conversation_id: `conv-focus-${String(i).padStart(4, '0')}`,
            workload: 'explore',
            title: `focus smoke conversation ${i}`,
            created_at: '2026-06-27T10:00:00Z',
            updated_at: '2026-06-27T10:05:00Z',
            turn_count: 2,
            last_trace_id: 'abcdef0123456789abcdef0123456789',
          })),
        }),
      }),
    );
    await page.goto('/?view=chat');
    await page.getByTestId('conversations-search-open').click();
    await expect(page.getByTestId('conversations-search-input')).toBeVisible();
    await settle(page);
    await assertFocusRingsIntact(page, 'search modal', {
      minStops: 8,
      sentinel: 'conversations-search-input',
    });
  });

  // The OTHER Modal consumer. It shares `.modal__body` with the search modal, so
  // the scroll fix covers it, but CapabilityCard brings its own clipping
  // containers (`.cap-workload` hidden, `.cap-prompt-pre` scrolling) that
  // nothing else in the suite reaches.
  test('capabilities modal', async ({ page, baseURL }) => {
    await seed(page);
    await mock(page, baseURL!);
    await emptyConversations(page);
    // A capabilities payload with a real workload. The shared `{ capabilities:
    // [] }` mock renders no `<summary>` rows at all, so this state would have
    // swept the modal chrome and silently missed the defect it exists to catch.
    await page.route('**/capabilities', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          version: 1,
          provenance: 'focus-ring smoke',
          iam_note: 'focus-ring smoke',
          workloads: [
            {
              name: 'drift',
              display_name: 'Anchor',
              descriptor: 'Cloud Run config',
              description: 'focus-ring smoke',
              autonomous: true,
              tools: [{ name: 'drift_read_live_env', description: 'smoke', write_capable: false }],
              workers: [{ name: 'drift_reader', description: 'smoke' }],
              actions: [{ name: 'no_op', display_name: 'No action needed', requires_approval: false }],
            },
          ],
          // Required by the DTO. CapabilityCard deliberately routes a payload
          // that parses as JSON but is missing structure to its error/retry row
          // (Svelte 5 has no error boundary), and an error row renders NO
          // workload summaries — which is exactly how the first version of this
          // state passed while covering nothing.
          human_gates: [],
          denylist: { summary: 'focus-ring smoke', enforced_at: [], rules: [] },
        }),
      }),
    );
    // The trigger lives in the chat EMPTY state, so the thread must stay empty.
    await page.goto('/?view=chat');
    await page.getByTestId('capability-link').click();
    await expect(page.getByTestId('capability-card')).toBeVisible();
    await settle(page);
    // Two, and that is the complete tab cycle: the dialog traps focus, so only
    // its close button and the first workload's <summary> are reachable. The
    // SENTINEL is what proves coverage here, not the count.
    await assertFocusRingsIntact(page, 'capabilities modal', {
      minStops: 2,
      sentinel: 'cap-workload-drift-summary',
    });
  });

  for (const view of ['desk', 'chat'] as const) {
    test(`${view} at a narrow viewport`, async ({ page, baseURL }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await seed(page);
      await mock(page, baseURL!);
      await emptyConversations(page);
      await page.goto(`/?view=${view}`);
      await settle(page);
      await assertFocusRingsIntact(page, `${view} @390`, { minStops: 6 });
    });
  }
});
