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
      expect(layer.spread, `layer ${i} must have a positive spread`).toBeGreaterThan(0);
    }
    // Strictly increasing: each layer painted behind must extend past the one
    // in front of it, or it contributes no visible pixels at all.
    expect(layers[1].spread).toBeGreaterThan(layers[0].spread);
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
    // satisfies them: opaque --ds-stream fails 12 of these and --ds-stream-ink
    // fails 7. Two layers split the job; each adjacent color needs only ONE of
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

  it('is never attenuated on hover, at any strength', () => {
    // ds-16e's fix is the ABSENCE of a rule, so this asserts absence directly
    // rather than measuring a fade that should not exist. Measuring instead
    // would fail open in every way the fade could come back: `opacity: var(..)`,
    // a percentage, a `filter: opacity()`, an animation, or the opacity moving
    // up to the parent stat.
    //
    // Why absence rather than a safe value: of the three numerals that could
    // fade (awaiting is interactive:false, so it never did — despite ds-16e's
    // title), the [data-unknown] placeholder takes --ds-faint and rests at just
    // 3.083:1. It needs alpha >= ~.981 to hold 3:1, which is not a fade anyone
    // can perceive, so there is no useful value between "none" and "broken".
    // It is reachable on both interactive stats: ApprovalDesk passes null for
    // managed and drift whenever the graph is unavailable.
    /**
     * Does this selector's SUBJECT receive the declaration?
     *
     * The subject is the last compound, not any mention: the band already has a
     * rule reading `.instrument-band__stat:hover ... .instrument-band__label
     * { opacity: 0 }`, which blanks the LABEL under its hint and is correct. A
     * looser `includes()` flags it, so match the numeral itself or the stat
     * (whose opacity would inherit down to the numeral).
     */
    function attenuatesNumeral(selector: string): boolean {
      return selector
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split(',')
        .some((part) => {
          const compounds = part.trim().split(/\s+/);
          const subject = compounds[compounds.length - 1] ?? '';
          const interactiveState = /:hover|:focus-visible/.test(part);
          const hits =
            subject.includes('instrument-band__num') || subject.includes('instrument-band__stat');
          return interactiveState && hits;
        });
    }

    const offenders: string[] = [];
    for (const m of band.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const [, selector, body] = m;
      if (!attenuatesNumeral(selector)) continue;
      // EVERY declaration, not the first: `opacity: 1; filter: opacity(.5)` has
      // an innocent first match and a fade right behind it.
      for (const decl of body.matchAll(/(?:^|[;{\s])(opacity|filter|animation)\s*:\s*([^;]+)/g)) {
        const [, prop, raw] = decl;
        const value = raw.trim();
        // Fail CLOSED: a value we cannot read is not a value we can clear. Only
        // a literal `opacity: 1` is provably harmless; `filter` and `animation`
        // are listed because both can attenuate without the word "opacity"
        // appearing as a property at all.
        const provablyHarmless = prop === 'opacity' && Number(value) === 1;
        if (!provablyHarmless) offenders.push(`${selector.trim()} { ${prop}: ${value} }`);
      }
    }

    expect(
      offenders,
      `a hover rule attenuates the band numeral:\n  ${offenders.join('\n  ')}`
    ).toEqual([]);
  });
});
