import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, relative, join } from 'node:path';

// ---------------------------------------------------------------------------
// Design-system contract guard (P3, plan §3 "Design system (Editorial Clarity)").
//
// tokens.css + base.css are NOT framework-scoped: base.css is <link>ed by BOTH
// the Svelte shell AND the Jinja approval pages (P5b). So the *names* of the
// custom properties and the shared `ds-*` component classes are a cross-file
// contract — a Svelte component, an approval template, and another stylesheet
// all reference them by string. Renaming `--ds-fs-3` or dropping `.ds-btn`
// silently would break a parallel-authored consumer with no type checker to
// catch it. This test pins that contract at the source level (the same posture
// as workloads.test.ts pinning the API value/label strings).
//
// It does NOT assert visual rendering (jsdom has no layout/cascade engine); it
// asserts the declared tokens + selectors exist and the files parse cleanly
// (balanced braces, no stray unterminated blocks).
// ---------------------------------------------------------------------------

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(here, '../../src');
const stylesDir = resolve(srcDir, 'styles');

/** Every .svelte / .css file under src/, recursively. */
function svelteAndCssSources(dir: string = srcDir): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...svelteAndCssSources(full));
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(full);
  }
  return out;
}

let tokens = '';
let base = '';

beforeAll(() => {
  tokens = readFileSync(resolve(stylesDir, 'tokens.css'), 'utf8');
  base = readFileSync(resolve(stylesDir, 'base.css'), 'utf8');
});

/** Strip /* *​/ comments so a token name mentioned in prose can't false-pass. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** A custom property is *declared* when it appears as `--name:` (a definition). */
function declaresVar(css: string, name: string): boolean {
  const re = new RegExp(`${name.replace(/[-]/g, '\\-')}\\s*:`, '');
  return re.test(stripComments(css));
}

/** A selector class is *defined* when `.cls` heads a rule block (`.cls {` or in a
 *  comma list before `{`). We accept `.cls` followed by space/comma/`{`/`:`. */
function definesClass(css: string, cls: string): boolean {
  const esc = cls.replace(/[-]/g, '\\-').replace(/[.]/g, '\\.');
  // `.ds-btn` but not `.ds-btn--approve`: require a non-class-char boundary.
  const re = new RegExp(`\\.${esc}(?![\\w-])`, '');
  return re.test(stripComments(css));
}

describe('tokens.css — design-system custom properties', () => {
  it('defines the :root token layer', () => {
    expect(stripComments(tokens)).toMatch(/:root\s*\{/);
  });

  it('declares the warm-neutral color palette', () => {
    for (const v of [
      '--ds-bg',
      '--ds-surface',
      '--ds-fg',
      '--ds-muted',
      '--ds-border',
    ]) {
      expect(declaresVar(tokens, v), `missing ${v}`).toBe(true);
    }
  });

  it('declares the four semantic accents (green/amber/red/blue)', () => {
    for (const v of ['--ds-ok', '--ds-warn', '--ds-danger', '--ds-stream']) {
      expect(declaresVar(tokens, v), `missing ${v}`).toBe(true);
    }
  });

  it('declares the four crew identity colors with their pinned hues', () => {
    // Identity, not status: one primary hue per crew agent (consumed by
    // CrewGlyph). Pinned by value, not just name — drifting Anchor off blue or
    // Patch off terracotta would silently re-skin the glyphs. Kept distinct from
    // the status accents above so a status re-tune can't mutate crew identity —
    // and ds-qbo was exactly that re-tune: --ds-stream moved to Google blue
    // while --ds-crew-drift stayed on the editorial blue pinned here.
    const expected: Record<string, string> = {
      '--ds-crew-drift': '#1f6feb', // Anchor — blue
      '--ds-crew-upgrade': '#a8432e', // Patch — brick red (terracotta)
      '--ds-crew-provision': '#6f42c1', // Provision — violet
      '--ds-crew-explore': '#0f8a8a', // Explore — teal
    };
    const stripped = stripComments(tokens);
    for (const [name, hex] of Object.entries(expected)) {
      expect(declaresVar(tokens, name), `missing ${name}`).toBe(true);
      const re = new RegExp(`${name.replace(/-/g, '\\-')}\\s*:\\s*${hex}`, 'i');
      expect(re.test(stripped), `${name} should be ${hex}`).toBe(true);
    }
  });

  it('declares a full type scale --ds-fs-1 .. --ds-fs-6', () => {
    for (let i = 1; i <= 6; i++) {
      expect(declaresVar(tokens, `--ds-fs-${i}`), `missing --ds-fs-${i}`).toBe(
        true,
      );
    }
  });

  it('declares a 4px-based spacing scale --ds-sp-1 .. --ds-sp-6', () => {
    for (let i = 1; i <= 6; i++) {
      expect(declaresVar(tokens, `--ds-sp-${i}`), `missing --ds-sp-${i}`).toBe(
        true,
      );
    }
  });

  it('declares radii, shadow, and motion (duration + easing) tokens', () => {
    for (const v of [
      '--ds-radius',
      '--ds-radius-lg',
      '--ds-shadow',
      '--ds-dur',
      '--ds-ease',
    ]) {
      expect(declaresVar(tokens, v), `missing ${v}`).toBe(true);
    }
  });

  it('declares humanist UI + monospace font-stack tokens', () => {
    expect(declaresVar(tokens, '--ds-font')).toBe(true);
    expect(declaresVar(tokens, '--ds-font-mono')).toBe(true);
  });

  it('pins the paper page background — one world (#fbfaf8)', () => {
    // ds-qbo: the legacy warm-neutral ground (#fcfcfb) and the composite
    // redesign's paper ground (#fbfaf8) were near-identical duplicates of the
    // same idea. There is now ONE ground and it is paper. If this pin drifts
    // back toward #fcfcfb, the desk and the chat view have split in two again.
    expect(stripComments(tokens)).toMatch(/--ds-bg\s*:\s*#fbfaf8/i);
  });

  it('keeps the composite redesign vocabulary that is NOT a duplicate', () => {
    // The 2026-07-28 composite redesign introduced a second token world
    // alongside the legacy one. ds-qbo collapsed them: the legacy names took
    // the paper world's VALUES and the duplicate names were deleted. These
    // three were never duplicates — they are vocabulary the legacy side never
    // had — so they survive, promoted into the main palette.
    for (const t of ['--ds-navy:', '--ds-seal:', '--ds-font-mincho:']) {
      expect(declaresVar(tokens, t.replace(':', '')), `missing ${t}`).toBe(true);
    }
  });

  it('has retired the duplicate paper-world tokens — one vocabulary', () => {
    // ds-qbo Phase 3. Each of these was a literal duplicate of a canonical
    // token (--ds-paper == --ds-bg, --ds-ok-green == --ds-ok, and so on), and
    // maintaining two names for one value is what let the desk and the chat
    // view drift into looking like two different products. If any of these
    // comes back, the split has started again — re-point the consumer at the
    // canonical token instead of re-adding the alias.
    for (const dead of [
      '--ds-paper',
      '--ds-paper-ink',
      '--ds-paper-ink-2',
      '--ds-paper-mut',
      '--ds-paper-rule',
      '--ds-ok-green',
      '--ds-drift-amber',
      '--ds-gblue',
    ]) {
      expect(declaresVar(tokens, dead), `${dead} should be retired`).toBe(false);
    }
  });

  it('never inks TEXT with raw --ds-stream (3.42:1) — that is stream-ink’s job', () => {
    // ds-qbo design decision 3b: blue does three jobs and they are not
    // interchangeable. --ds-navy fills, --ds-stream-ink inks text (5.40:1 on
    // paper), and raw --ds-stream (#4285f4, 3.42:1) is for NON-TEXT accents
    // only — borders, rails, meters, glows.
    //
    // This guard exists because the Phase 2 audit grepped `var(--ds-stream)`
    // and passed, and then Phase 3's rename turned two --ds-gblue consumers on
    // the approval desk into raw-stream TEXT consumers — 11.5px status copy and
    // a 12px control, both landing under the floor. An audit is a moment; this
    // is the invariant. Same lesson as #216 and #258: a value reaches a surface
    // through more paths than the grep you happened to write.
    //
    // The one sanctioned exception is the instrument band's awaiting numeral,
    // which is 44px/600 — large text, so its floor is 3:1, which 3.42:1 clears.
    const ALLOWED = new Set(['InstrumentBand.svelte']);
    const offenders: string[] = [];
    for (const file of svelteAndCssSources()) {
      const src = stripComments(readFileSync(file, 'utf8'));
      // `color:` but not `border-color:` / `border-left-color:` / `outline-color:`
      if (/(?<![\w-])color\s*:\s*var\(--ds-stream\)/.test(src)) {
        const base = file.split('/').pop()!;
        if (!ALLOWED.has(base)) offenders.push(relative(srcDir, file));
      }
    }
    expect(
      offenders,
      `raw --ds-stream used as text color (use --ds-stream-ink):\n${offenders.join('\n')}`,
    ).toEqual([]);
  });

  it('references no --ds-* custom property that is declared nowhere', () => {
    // ds-b42: three popovers read `var(--ds-shadow-md, var(--ds-shadow-sm))`.
    // --ds-shadow-md has never existed, so all three silently took the FALLBACK
    // — the lightest shadow in the set — while tokens.css names popovers as one
    // of the three things that earn real elevation. Nothing failed; they just
    // rendered a tier lighter than intended, forever.
    //
    // This generalizes the retired-token test above: that one needs a human to
    // add each dead name, and a token that never existed was never on the list.
    // Here anything referenced but undeclared is an offender, whether it was
    // retired, renamed, or simply mistyped. A `var(--x, fallback)` is still an
    // offender: the fallback makes the mistake invisible, not correct.
    const declared = new Set(
      [...stripComments(tokens).matchAll(/(--ds-[\w-]+)\s*:/g)].map((m) => m[1]),
    );
    expect(declared.size).toBeGreaterThan(50); // premise: the parse found the palette

    const offenders: string[] = [];
    for (const file of svelteAndCssSources()) {
      const src = stripComments(readFileSync(file, 'utf8'));
      for (const m of src.matchAll(/var\(\s*(--ds-[\w-]+)/g)) {
        if (!declared.has(m[1])) offenders.push(`${relative(srcDir, file)}: ${m[1]}`);
      }
    }
    expect(
      [...new Set(offenders)],
      `var() reads an undeclared --ds-* token:\n${[...new Set(offenders)].join('\n')}`,
    ).toEqual([]);
  });

  it('has no component still referencing a retired paper-world token', () => {
    // The declarations being gone is only half of it: a component reading
    // var(--ds-paper-mut) after the block was deleted would silently render an
    // UNSET custom property (transparent / inherited), which no unit test that
    // renders markup would necessarily catch. This walks the real source.
    const offenders: string[] = [];
    for (const file of svelteAndCssSources()) {
      const src = stripComments(readFileSync(file, 'utf8'));
      for (const dead of [
        '--ds-paper',
        '--ds-ok-green',
        '--ds-drift-amber',
        '--ds-gblue',
      ]) {
        // `--ds-paper` also covers -ink/-ink-2/-mut/-rule by prefix.
        if (src.includes(dead)) offenders.push(`${relative(srcDir, file)}: ${dead}`);
      }
    }
    expect(offenders, `retired tokens still referenced:\n${offenders.join('\n')}`).toEqual([]);
  });

  it('reserves vermilion for the seal only', () => {
    // #c0392b must appear exactly once — as --ds-seal. The 判子 stamp is the
    // only red thing on the page, so it reads as the moment of approval.
    expect(tokens.match(/#c0392b/gi)?.length).toBe(1);
  });

  // ds-s61 — the bug CLASS, not the one instance. `.layout` carried
  // `min-height: calc(100vh - 56px)` from the first SPA commit, written when the
  // header was 56px tall. The header later grew to 94px (its two-row grid below
  // the 1560px breakpoint) or 62px (one row above it) and the constant stayed,
  // so every view stood exactly `headerHeight - 56` taller than the viewport:
  // a permanent 38px of scroll with nothing in it. Clicking the desk's awaiting
  // numeral spent all of it on a scrollIntoView that could not arrive, which
  // read as the page twitching for no reason.
  //
  // A magic number that has to track another element's RENDERED height cannot be
  // kept correct by review — the two live in different files and nothing fails
  // when they diverge. So the guard forbids the shape: derive the remainder
  // (flex/grid), don't subtract a guess.
  it('never subtracts a hard-coded element height from a viewport unit', () => {
    const offenders: string[] = [];
    // Matches calc(100vh - 56px) / calc(100dvh - 3rem) and the svh/lvh variants,
    // in either order, since `calc(56px - 100vh)` is the same latent coupling.
    const VIEWPORT_MINUS_CONSTANT =
      /calc\(\s*(?:100(?:d|l|s)?vh\s*-\s*[\d.]+(?:px|rem|em)|[\d.]+(?:px|rem|em)\s*-\s*100(?:d|l|s)?vh)\s*\)/i;
    for (const file of svelteAndCssSources()) {
      const src = stripComments(readFileSync(file, 'utf8'));
      const hit = src.match(VIEWPORT_MINUS_CONSTANT);
      if (hit) offenders.push(`${relative(srcDir, file)}: ${hit[0]}`);
    }
    expect(
      offenders,
      `hard-coded height subtracted from a viewport unit — derive it instead ` +
        `(see #app in base.css):\n${offenders.join('\n')}`,
    ).toEqual([]);
  });

  it('the app shell owns the viewport so the layout can derive its height', () => {
    // The other half of the guard above: forbidding the bad shape is only useful
    // if the good one is actually in place. #app is a flex column at 100vh and
    // `.layout` takes the remainder, so no future header change can re-open it.
    expect(base).toMatch(/#app\s*\{[^}]*min-height:\s*100vh/);
    expect(base).toMatch(/#app\s*\{[^}]*flex-direction:\s*column/);
    const app = readFileSync(resolve(here, '../../src/App.svelte'), 'utf8');
    expect(app).toMatch(/\.layout\s*\{[^}]*flex:\s*1/);
  });
});

describe('base.css — shared ds-* component classes (consumed by Svelte + Jinja)', () => {
  // The exact roster the task + plan require the approval pages to use in P5b.
  const REQUIRED_CLASSES = [
    'ds-page',
    'ds-card',
    'ds-field',
    'ds-label',
    'ds-btn',
    'ds-btn--approve',
    'ds-btn--reject',
    'ds-btn--ghost',
    'ds-pre',
    'ds-note',
    'ds-blocked',
    'ds-pill',
    'ds-pill--ok',
    'ds-pill--warn',
    'ds-pill--danger',
    'ds-pill--muted',
    'ds-code',
    'ds-h1',
    'ds-h2',
    'ds-subtle',
    'ds-ok',
    'ds-bad',
  ];

  it.each(REQUIRED_CLASSES)('defines .%s', (cls) => {
    expect(definesClass(base, cls), `base.css must define .${cls}`).toBe(true);
  });

  // ds-7ag.5 — the roster above pins only that `.ds-btn--reject` EXISTS, which a
  // solid-red fill satisfies as happily as an outline. Reject is destructive but
  // it is not the primary action on either approval page (Approve is), and a
  // filled red button competing with a filled Approve is the "everything shouts"
  // texture the redesign answers. But it must stay unmistakably present: on the
  // rollback page under pause/autonomy lockout, Reject is the ONLY enabled
  // control (agent/templates/approval.html) — so this pins outline, not ghost.
  it('renders .ds-btn--reject as outline-danger, not a filled primary', () => {
    const m = stripComments(base).match(/\.ds-btn--reject\s*\{([\s\S]*?)\}/);
    expect(m, '.ds-btn--reject rule not found').not.toBeNull();
    const decls = m![1];
    expect(decls).toMatch(/background\s*:\s*transparent/);
    expect(decls).toMatch(/color\s*:\s*var\(--ds-danger-ink\)/);
    // The danger ink stays on the border too — this is a demotion in weight,
    // not a removal of the affordance.
    expect(decls).toMatch(/border-color\s*:\s*var\(--ds-danger-border\)/);
    // The hover state must still fill, so the control reads as live on a page
    // where it may be the only thing the operator can press.
    const hover = stripComments(base).match(/\.ds-btn--reject:hover\s*\{([\s\S]*?)\}/);
    expect(hover, '.ds-btn--reject:hover rule not found').not.toBeNull();
    expect(hover![1]).toMatch(/background\s*:\s*var\(--ds-danger-surface\)/);
  });

  it('resets the box model and base body element', () => {
    const stripped = stripComments(base);
    expect(stripped).toMatch(/box-sizing\s*:\s*border-box/);
    expect(stripped).toMatch(/\bbody\b/);
  });

  it('uses design tokens (not hard-coded literals) for the body chrome', () => {
    const stripped = stripComments(base);
    expect(stripped).toMatch(/var\(--ds-bg\)/);
    expect(stripped).toMatch(/var\(--ds-fg\)/);
    expect(stripped).toMatch(/var\(--ds-font\)/);
  });

  it('reserves monospace for code/trace via --ds-font-mono', () => {
    expect(stripComments(base)).toMatch(/var\(--ds-font-mono\)/);
  });

  it('constrains the readable column on .ds-page', () => {
    // a max-width must appear in the .ds-page rule for the centered column.
    const m = stripComments(base).match(/\.ds-page[^{]*\{([\s\S]*?)\}/);
    expect(m, '.ds-page rule not found').not.toBeNull();
    expect(m![1]).toMatch(/max-width/);
    expect(m![1]).toMatch(/margin/); // centered
  });

  it('honors prefers-reduced-motion (disables transitions/animations)', () => {
    const stripped = stripComments(base) + '\n' + stripComments(tokens);
    expect(stripped).toMatch(/@media[^{]*prefers-reduced-motion\s*:\s*reduce/);
    // inside that block, transitions/animations must be neutralized.
    const block = stripped.match(
      /@media[^{]*prefers-reduced-motion[^{]*\{([\s\S]*?\}\s*)\}/,
    );
    expect(block, 'reduced-motion media block not found').not.toBeNull();
    expect(block![1]).toMatch(/animation[\s-][\s\S]*?(none|0)/i);
    expect(block![1]).toMatch(/transition[\s\S]*?(none|0)/i);
  });
});

describe('CSS structural sanity (balanced braces, no stray @import)', () => {
  it.each([
    ['tokens.css', () => tokens],
    ['base.css', () => base],
  ])('%s has balanced braces', (_name, get) => {
    const css = stripComments(get());
    const open = (css.match(/\{/g) ?? []).length;
    const close = (css.match(/\}/g) ?? []).length;
    expect(open).toBe(close);
    expect(open).toBeGreaterThan(0);
  });

  it('base.css does not @import (tokens come via main.ts import order)', () => {
    expect(stripComments(base)).not.toMatch(/@import/);
  });
});
