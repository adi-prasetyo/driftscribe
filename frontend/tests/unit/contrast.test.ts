import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { composite, contrastOver, readToken, resolveColor, shadowColor } from './contrast';

// ---------------------------------------------------------------------------
// Contrast floors, enforced (ds-dce, ds-16e).
//
// Both defects this file pins were INVISIBLE at rest and appeared only while a
// control was focused or pointed at, which is why neither showed up in a
// screenshot pass or a design review. They also shared a mechanism: a color
// declared at less than full opacity is not the color that renders, and nobody
// had done the compositing arithmetic.
//
// Every figure these tests assert is derived from the CSS/component source at
// run time, never copied into the test. Re-tune --ds-stream and this file
// re-measures it; it does not compare against a remembered number.
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
  // Every ground the ring is DRAWN on (page, cards, wells, the four semantic
  // tints) plus the component colors it renders ADJACENT to. 1.4.11 asks for
  // 3:1 against adjacent colors, and a focused navy CTA has the ring on paper
  // on one side and navy on the other, so both sides are grounds here.
  //
  // --ds-seal is deliberately ABSENT. The ring measures 1.526 against
  // vermilion, which would fail - but --ds-seal is only ever a `color` or a
  // `border` in this codebase, never a `background`, so no ring is ever drawn
  // on it. The day a filled vermilion surface appears, add it here and expect
  // this pin to go red; that is the intended behavior, not a false alarm.
  const RING_GROUNDS = [
    '--ds-bg',
    '--ds-surface',
    '--ds-surface-2',
    '--ds-neutral-surface',
    '--ds-ok-surface',
    '--ds-warn-surface',
    '--ds-danger-surface',
    '--ds-stream-surface',
    '--ds-navy'
  ];

  it('clears 3:1 on every ground it renders against', () => {
    const { hex, alpha } = shadowColor(tokens, readToken(tokens, '--ds-ring'));
    const failures: string[] = [];

    for (const groundToken of RING_GROUNDS) {
      const ground = resolveColor(tokens, `var(${groundToken})`);
      // The ring is what RENDERS, not what is declared: at alpha < 1 it is the
      // declared blue mixed with this particular ground.
      // `rendered` is for the message only; the ratio is measured unrounded.
      const rendered = composite(hex, alpha, ground);
      const ratio = contrastOver(hex, alpha, ground);
      if (ratio < FLOOR) {
        failures.push(`${groundToken}: ${ratio.toFixed(3)} (ring renders ${rendered})`);
      }
    }

    expect(failures, `focus ring below ${FLOOR}:1 on:\n  ${failures.join('\n  ')}`).toEqual([]);
  });

  it('is opaque, so its contrast does not depend on what is behind it', () => {
    // The regression this guards is the original defect exactly: a ring at 30%
    // alpha measured 1.374-1.536 everywhere. Any alpha < 1 reintroduces a ring
    // whose real color is a mix, so pin the property rather than the number.
    const { alpha } = shadowColor(tokens, readToken(tokens, '--ds-ring'));
    expect(alpha).toBe(1);
  });
});

describe('instrument band numerals', () => {
  /**
   * Every `--ds-*` color assigned to `.instrument-band__num`, parsed from the
   * component. Parsed rather than listed because an allowlist is exactly how
   * ds-16e survived: its bead audited three numeral colors and there were four.
   * A fifth added later is covered by this test the moment it is written.
   */
  function numeralColorTokens(source: string): string[] {
    const found = new Set<string>();
    const ruleRe = /([^{}]*)\{([^{}]*)\}/g;
    let m: RegExpExecArray | null;
    while ((m = ruleRe.exec(source))) {
      const [, selector, body] = m;
      if (!selector.includes('.instrument-band__num')) continue;
      const colorRe = /(?:^|[;{\s])color:\s*var\(\s*(--[\w-]+)\s*\)/g;
      let c: RegExpExecArray | null;
      while ((c = colorRe.exec(body))) found.add(c[1]);
    }
    return [...found];
  }

  /**
   * The opacity applied to the numeral on hover, or 1 when no such rule exists.
   *
   * Defaulting to 1 is what makes DELETING the fade the way to pass: the fix
   * for ds-16e is the absence of a rule, and a test that only checked a number
   * could not tell "no fade" from "no rule found".
   */
  function hoverAlpha(source: string): number {
    const ruleRe = /([^{}]*)\{([^{}]*)\}/g;
    let m: RegExpExecArray | null;
    while ((m = ruleRe.exec(source))) {
      const [, selector, body] = m;
      if (!selector.includes(':hover') || !selector.includes('.instrument-band__num')) continue;
      const o = /(?:^|[;{\s])opacity:\s*([\d.]+)/.exec(body);
      if (o) return Number(o[1]);
    }
    return 1;
  }

  it('finds every numeral color, including the unknown placeholder', () => {
    // A premise check. If the parser silently matched nothing, the contrast
    // assertions below would pass over an empty list and prove nothing at all.
    const found = numeralColorTokens(band);
    expect(found).toContain('--ds-navy'); // managed
    expect(found).toContain('--ds-warn'); // drift
    expect(found).toContain('--ds-stream'); // awaiting
    expect(found).toContain('--ds-faint'); // [data-unknown] — the one ds-16e missed
  });

  it('clears 3:1 at rest AND while hovered', () => {
    // The band's own ground: .approval-desk sets background: var(--ds-bg).
    const ground = resolveColor(tokens, 'var(--ds-bg)');
    const alpha = hoverAlpha(band);
    const failures: string[] = [];

    for (const token of numeralColorTokens(band)) {
      const declared = resolveColor(tokens, `var(${token})`);
      for (const [state, a] of [
        ['rest', 1],
        ['hover', alpha]
      ] as const) {
        const rendered = composite(declared, a, ground);
        const ratio = contrastOver(declared, a, ground);
        if (ratio < FLOOR) {
          failures.push(`${token} @${state} (alpha ${a}): ${ratio.toFixed(3)} — renders ${rendered}`);
        }
      }
    }

    expect(failures, `numerals below ${FLOOR}:1:\n  ${failures.join('\n  ')}`).toEqual([]);
  });
});
