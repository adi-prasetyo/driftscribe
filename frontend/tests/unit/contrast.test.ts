import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { colorTokens, contrastOver, contrastRatio, readToken, resolveColor, shadowLayers } from './contrast';

// ---------------------------------------------------------------------------
// Contrast floors, enforced (ds-dce, ds-16e).
//
// Both defects pinned here were invisible at rest and appeared only while a
// control was focused or pointed at, which is why neither survived a screenshot
// pass. They share a mechanism: a color declared below full opacity is not the
// color that renders, and nobody had done the compositing arithmetic.
//
// Every figure is derived from the CSS/component source at run time, never
// copied into the test. Re-tune a palette entry and this file re-measures it.
// ---------------------------------------------------------------------------

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(here, '../../src');

/** WCAG 1.4.11 non-text contrast, and 1.4.3's large-text floor. Same number. */
const FLOOR = 3.0;

let tokens = '';
let band = '';

beforeAll(() => {
  tokens = readFileSync(resolve(srcDir, 'styles/tokens.css'), 'utf8');
  band = readFileSync(resolve(srcDir, 'components/InstrumentBand.svelte'), 'utf8');
});

describe('focus ring', () => {
  // The grounds the ring is DRAWN on: the page, the two card surfaces, the
  // sunken well, and the four semantic tints. The outer band lands on these.
  const DRAWN_ON = [
    '--ds-bg',
    '--ds-surface',
    '--ds-surface-2',
    '--ds-neutral-surface',
    '--ds-ok-surface',
    '--ds-warn-surface',
    '--ds-danger-surface',
    '--ds-stream-surface'
  ];

  it('is opaque in every layer, so its contrast does not depend on the ground', () => {
    // The original defect exactly: a ring at 30% alpha measured 1.37-1.53
    // everywhere, because a translucent ring is not its declared color at all.
    // Pin the property rather than a number.
    for (const layer of shadowLayers(tokens, readToken(tokens, '--ds-ring'))) {
      expect(layer.alpha).toBe(1);
    }
  });

  it('has geometry that leaves both bands visible', () => {
    // Colors alone do not make a ring. Paint order is declaration order, so a
    // layer declared FIRST with a LARGER spread covers the ones behind it
    // completely: swapping the two spreads here yields a ring whose blue band
    // is entirely hidden under the white one, is invisible on a white control,
    // and still satisfies every color assertion in this file. Geometry is the
    // difference between "the right colors are declared" and "the indicator
    // renders", and only this test can tell them apart.
    const layers = shadowLayers(tokens, readToken(tokens, '--ds-ring'));
    expect(layers).toHaveLength(2);

    for (const [i, layer] of layers.entries()) {
      expect(layer.inset, `layer ${i} must not be inset`).toBe(false);
      expect([layer.offsetX, layer.offsetY, layer.blur], `layer ${i} must be a pure ring`).toEqual([
        0, 0, 0
      ]);
      // The WHOLE layer anchored: four canonical px lengths then exactly one
      // color. Validating only a stripped remainder accepts two colors in a
      // layer, and scanning for numbers accepts `1em` (reads as smaller than
      // `2px` while being 16px), `calc()`, a bare number, `%`, and a fifth
      // length. Each of those is a ring that renders nothing while parsing fine.
      expect(layer.badSyntax, `layer ${i} is not <4 px lengths> <1 color>`).toBeNull();
    }

    // Exact spreads, not bounds. Bounds invite the next clever near-miss: a
    // 3.5px inner under a 4px outer leaves half a pixel of blue, 0.01/0.02px
    // renders nothing, and 100000px/100002px satisfies any minimum while
    // putting the indicator nowhere near the control. There is one canonical
    // ring; pin it, and the whole family disappears. 2px per band is this
    // project's chosen thickness, in the spirit of WCAG 2.4.13's "area at least
    // equal to a 2px perimeter".
    expect(layers.map((l) => l.spread), 'ring is not the canonical 2px/4px').toEqual([2, 4]);
  });

  it('has an outer band clearing 3:1 on every ground it is drawn on', () => {
    const layers = shadowLayers(tokens, readToken(tokens, '--ds-ring'));
    // Widest spread, NOT last-declared: those coincide only while the geometry
    // pin above holds, and this assertion must not quietly depend on it.
    const outer = layers.reduce((a, b) => (b.spread > a.spread ? b : a));
    const failures: string[] = [];

    for (const groundToken of DRAWN_ON) {
      const ground = resolveColor(tokens, `var(${groundToken})`);
      const ratio = contrastOver(outer.hex, outer.alpha, ground);
      if (ratio < FLOOR) failures.push(`${groundToken}: ${ratio.toFixed(3)}`);
    }

    expect(failures, `outer ring below ${FLOOR}:1 on:\n  ${failures.join('\n  ')}`).toEqual([]);
  });

  it('separates its own layers, so the ring reads as a ring', () => {
    // A two-tone indicator whose tones do not contrast is one fat tone with a
    // seam, and the inner band stops doing the job it was added for.
    const layers = shadowLayers(tokens, readToken(tokens, '--ds-ring'));
    if (layers.length < 2) return; // single-tone ring: nothing to separate
    expect(contrastRatio(layers[0].hex, layers[layers.length - 1].hex)).toBeGreaterThanOrEqual(
      FLOOR
    );
  });

  it('is carried by some layer against EVERY color in the palette', () => {
    // The invariant that made a two-tone ring necessary, and the reason this
    // list is not hand-maintained.
    //
    // A ring abuts two things: the ground outside it and the control inside it.
    // This palette holds controls at both ends of the ramp - paper-light
    // borders (--ds-border-strong) and near-black fills (--ds-navy, --ds-ok,
    // --ds-seal) - so the two constraints are contradictory and NO single color
    // satisfies them: opaque --ds-stream fails 22 of these 32 and --ds-stream-ink
    // fails 17 (12 and 7 over the narrower 21-color adjacency set the design
    // record argues from). Two layers split the job; each adjacent color needs ONE of
    // them to clear the floor, which is how WCAG treats a multi-color indicator.
    //
    // Sweeping the whole palette rather than a curated list of "grounds" is
    // deliberate: a curated list cannot notice a newly introduced filled
    // control, which is precisely the kind of omission that produced ds-dce.
    const layers = shadowLayers(tokens, readToken(tokens, '--ds-ring'));
    const palette = colorTokens(tokens);
    expect(Object.keys(palette).length).toBeGreaterThan(25); // premise: parsed the palette

    const failures: string[] = [];
    for (const [name, color] of Object.entries(palette)) {
      const best = Math.max(...layers.map((l) => contrastOver(l.hex, l.alpha, color)));
      if (best < FLOOR) failures.push(`${name} (${color}): best layer ${best.toFixed(3)}`);
    }

    expect(
      failures,
      `no ring layer clears ${FLOOR}:1 against:\n  ${failures.join('\n  ')}`
    ).toEqual([]);
  });
});

describe('instrument band numerals', () => {
  /**
   * Every `--ds-*` color assigned to `.instrument-band__num`, parsed from the
   * component. Parsed rather than listed because an allowlist is how ds-16e
   * survived: its bead audited three numeral colors and there are four.
   */
  function numeralColorTokens(source: string): string[] {
    const found = new Set<string>();
    for (const m of source.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const [, selector, body] = m;
      if (!selector.includes('.instrument-band__num')) continue;
      for (const c of body.matchAll(/(?:^|[;{\s])color:\s*var\(\s*(--[\w-]+)\s*\)/g)) {
        found.add(c[1]);
      }
    }
    return [...found];
  }

  it('finds every numeral color, including the unknown placeholder', () => {
    // A premise check. Without it, a parser that silently matched nothing would
    // let the assertions below iterate an empty list and prove nothing.
    const found = numeralColorTokens(band);
    expect(found).toContain('--ds-navy'); // managed
    expect(found).toContain('--ds-warn'); // drift
    expect(found).toContain('--ds-stream'); // awaiting (static — never fades)
    expect(found).toContain('--ds-faint'); // [data-unknown] — the one ds-16e missed
  });

  it('clears 3:1 at rest', () => {
    // .approval-desk sets background: var(--ds-bg), so paper is the band's ground.
    const ground = resolveColor(tokens, 'var(--ds-bg)');
    const failures: string[] = [];

    for (const token of numeralColorTokens(band)) {
      const ratio = contrastRatio(resolveColor(tokens, `var(${token})`), ground);
      if (ratio < FLOOR) failures.push(`${token}: ${ratio.toFixed(3)}`);
    }

    expect(failures, `numerals below ${FLOOR}:1 at rest:\n  ${failures.join('\n  ')}`).toEqual([]);
  });

  it('is never attenuated, by any mechanism', () => {
    // ds-16e's fix is the ABSENCE of a rule, so this asserts absence directly
    // rather than measuring a fade that should not exist. Measuring would fail
    // open in every way the fade can come back, and there are more of those
    // than the original `opacity: .75` suggests:
    //
    //   opacity: var(--x)          unreadable value
    //   opacity: 1; filter: ...    an innocent first declaration
    //   animation-name: fade       no `opacity` property anywhere
    //   Animation-Name / -webkit-  CSS property names are case-insensitive
    //   :focus rather than :hover  same defect, different trigger
    //   --num-opacity: .75         set on the STAT, read by a base rule on the
    //                              numeral: neither rule looks like a fade
    //   color: rgba(.., .75)       translucency in the color itself
    //
    // Why absence rather than a safe value: of the three numerals that could
    // fade (awaiting is interactive:false, so it never did — despite ds-16e's
    // title), the [data-unknown] placeholder takes --ds-faint and rests at just
    // 3.083:1. It needs alpha >= ~.981 to hold 3:1, which is not a fade anyone
    // can perceive, so there is no useful value between "none" and "broken".
    // It is reachable on BOTH interactive stats: ApprovalDesk passes null for
    // managed and drift whenever the graph is unavailable.
    const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(band)?.[1] ?? '';
    expect(style, 'premise: found the component style block').not.toBe('');

    /**
     * Does this selector's SUBJECT receive the declaration?
     *
     * The subject is the last compound, not any mention: the band has a rule
     * reading `.instrument-band__stat:hover ... .instrument-band__label
     * { opacity: 0 }`, which blanks the LABEL under its hint and is correct.
     */
    function subjects(selector: string): string[] {
      return selector
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split(',')
        .map((part) => part.trim().split(/\s+/).pop() ?? '');
    }

    const offenders: string[] = [];
    for (const m of style.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const [, selector, body] = m;
      const subject = subjects(selector);
      const hitsNumeral = subject.some((s) => s.includes('instrument-band__num'));
      const hitsStat = subject.some((s) => s.includes('instrument-band__stat'));
      if (!hitsNumeral && !hitsStat) continue;
      const interactive = /:hover|:focus/.test(selector);
      const flag = (what: string) => offenders.push(`${selector.trim().split('\n').pop()} { ${what} }`);

      // Attenuating properties, case-insensitively and including vendor
      // prefixes. Checked on the numeral ALWAYS (a base rule reading a custom
      // property is half of the indirection trick) and on the stat only when
      // interactive, since a resting stat-level opacity is not a hover fade.
      if (hitsNumeral || interactive) {
        for (const d of body.matchAll(/(?:^|[;{\s])(-[a-z]+-)?(opacity|filter|animation[\w-]*)\s*:\s*([^;]+)/gi)) {
          const prop = `${d[1] ?? ''}${d[2]}`;
          const value = d[3].trim();
          if (!(/^opacity$/i.test(prop) && Number(value) === 1)) flag(`${prop}: ${value}`);
        }
      }
      // The other half of the indirection: a custom property set under :hover
      // feeds any base rule that reads it, and neither rule reads as a fade.
      if (interactive) {
        for (const d of body.matchAll(/(?:^|[;{\s])(--[\w-]+)\s*:/g)) {
          flag(`${d[1]}: … (custom property set on hover)`);
        }
        // Translucency can also live in the color itself.
        for (const d of body.matchAll(/(?:^|[;{\s])color\s*:\s*([^;]+)/gi)) {
          flag(`color: ${d[1].trim()}`);
        }
      }
    }

    expect(
      [...new Set(offenders)],
      `the band numeral can be attenuated:\n  ${[...new Set(offenders)].join('\n  ')}`
    ).toEqual([]);
  });
});
