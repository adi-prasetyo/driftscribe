import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { colorTokens, composite, contrastOver, contrastRatio, readToken, resolveColor, shadowLayers } from './contrast';

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

    // The numeral and every element that can CONTAIN it: opacity on an ancestor
    // fades the numeral just as surely as opacity on the numeral, and
    // `.instrument-band` / `.instrument-band__stats` are both live ancestors.
    // Siblings are deliberately excluded — `.instrument-band__label` carries a
    // legitimate `opacity: 0` under :hover for the hint swap (ds-7ag.2), and it
    // is never an ancestor of the numeral, so it cannot attenuate it.
    const CONTAINS_NUMERAL = /instrument-band(?:__(?:stats?|num))?(?:--[\w-]+)?(?![\w-])/;

    const offenders: string[] = [];
    for (const m of style.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const [, selector, body] = m;
      const subject = subjects(selector);
      if (!subject.some((s) => CONTAINS_NUMERAL.test(s))) continue;
      // Pseudo-class names are case-insensitive in CSS; this regex must be too,
      // or `:HOVER` walks straight past the transient-color check below.
      const interactive = /:hover|:focus/i.test(selector);
      const flag = (what: string) => offenders.push(`${selector.trim().split('\n').pop()} { ${what} }`);

      // Attenuating properties, case-insensitively and including vendor
      // prefixes — on EVERY containing subject, interactive or not.
      //
      // An earlier version checked ancestors only when the selector carried
      // :hover/:focus, reasoning that a resting ancestor opacity is not a hover
      // fade. That reasoning is backwards: `.instrument-band__stat { opacity:
      // .75 }` attenuates the numeral permanently rather than transiently,
      // which is worse, and it evades the rest-contrast test too because that
      // test measures the numeral's declared color and knows nothing about an
      // ancestor's opacity.
      for (const d of body.matchAll(/(?:^|[;{\s])(-[a-z]+-)?(opacity|filter|animation[\w-]*)\s*:\s*([^;]+)/gi)) {
        const prop = `${d[1] ?? ''}${d[2]}`;
        const value = d[3].trim();
        if (!(/^opacity$/i.test(prop) && Number(value) === 1)) flag(`${prop}: ${value}`);
      }
      // The other half of the indirection: a custom property set under :hover
      // feeds any base rule that reads it, and neither rule reads as a fade.
      if (interactive) {
        for (const d of body.matchAll(/(?:^|[;{\s])(--[\w-]+)\s*:/g)) {
          flag(`${d[1]}: … (custom property set on hover)`);
        }
      }

      // Translucency can live in the color itself, and a RESTING one is the
      // color equivalent of the resting-ancestor-opacity gap above:
      //
      //   color: var(--ds-faint);
      //   color: rgba(138, 144, 153, .75);   <- CSS takes the last declaration
      //
      // The rest-contrast test does not see it either, because it collects
      // `color: var(--ds-*)` and measures the TOKEN. So every color declared on
      // a containing subject must be an opaque palette token — which is what
      // makes the rest test's measurement the whole truth — and anything else
      // fails closed, interactive or not.
      for (const d of body.matchAll(/(?:^|[;{\s])color\s*:\s*([^;]+)/gi)) {
        const value = d[1].trim();
        try {
          resolveColor(tokens, value);
        } catch {
          flag(`color: ${value} (not an opaque palette token)`);
        }
      }
    }

    expect(
      [...new Set(offenders)],
      `the band numeral can be attenuated:\n  ${[...new Set(offenders)].join('\n  ')}`
    ).toEqual([]);
  });

  it('sets no inline style that could attenuate the numeral', () => {
    // The attenuation scan above deliberately reads the <style> block only, so
    // template markup is never brace-matched as CSS. That leaves inline styling
    // as a blind spot: `<span class="instrument-band__num" style="opacity:.75">`
    // fades the numeral and no CSS rule exists to find.
    //
    // The band sets no inline styles today except `style:flex` on the two meter
    // bars, which cannot attenuate anything, so the guard is simply: no `style=`
    // attribute at all, and no attenuating `style:` directive.
    const markup = band
      .replace(/<style[^>]*>[\s\S]*?<\/style>/g, '')
      .replace(/<script[^>]*>[\s\S]*?<\/script>/g, '');

    // Match the ATTRIBUTE, not a quoted literal after it: Svelte accepts an
    // expression, and `style={'opacity: .75'}` compiles fine while sailing past
    // a regex that expects a quote.
    expect([...markup.matchAll(/\bstyle\s*=/gi)].map((m) => m[0])).toEqual([]);
    expect(
      [...markup.matchAll(/\bstyle:(opacity|filter|color)\b/gi)].map((m) => m[0])
    ).toEqual([]);
  });

  it('names an instrument-band class as the subject of every rule', () => {
    // The BEM contract, and the reason the checks above can key on class names.
    //
    // A rule can reach the numeral without ever naming it:
    //
    //   .instrument-band__stat[data-unknown] > :first-child { color: … }
    //
    // The numeral IS the first child, so this wins by source order at equal
    // specificity and renders at 1.043:1 — while `numeralColorTokens` skips it
    // (no `__num` in the selector) and the attenuation scan skips it (subject is
    // `:first-child`). Every subject-keyed guard in this file has that blind
    // spot, so rather than patch each one, pin the property they all rely on:
    // in this component's scoped styles, the subject of every rule names an
    // instrument-band class. Positional and element subjects are then simply
    // not expressible, and the blind spot closes for all of them at once.
    const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(band)?.[1] ?? '';
    const withoutComments = style.replace(/\/\*[\s\S]*?\*\//g, '');

    const anonymous: string[] = [];
    for (const m of withoutComments.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const selector = m[1].trim();
      // At-rule preludes and keyframe steps are not element subjects.
      if (!selector || selector.startsWith('@') || /^(from|to|[\d.]+%)$/.test(selector)) continue;
      for (const part of selector.split(',')) {
        const subject = part.trim().split(/\s+/).pop() ?? '';
        if (subject && !subject.includes('instrument-band')) anonymous.push(part.trim());
      }
    }

    expect(
      anonymous,
      `rule subjects that do not name an instrument-band class:\n  ${anonymous.join('\n  ')}`
    ).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The INSET ring's premise (ds-2fp).
//
// --ds-ring-inset-on-light is a SINGLE tone, where --ds-ring above needs two.
// That is only sound because it is drawn inside a control whose every possible
// fill is light. The numbers are not the fragile part — the premise is. So this
// sweeps every background an autonomy segment can actually take and fails if any
// of them stops being light, or if one appears in a form it cannot resolve.
// ---------------------------------------------------------------------------

describe('inset focus ring (autonomy segmented dial)', () => {
  let pill = '';
  let style = '';

  beforeAll(() => {
    pill = readFileSync(resolve(srcDir, 'components/AutonomyPill.svelte'), 'utf8');
    style = /<style[^>]*>([\s\S]*)<\/style>/.exec(pill)?.[1] ?? '';
  });

  /** Every `selector { ... }` pair in the component's scoped styles. */
  const rules = (css: string) =>
    [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
      selector: m[1].trim().replace(/\s+/g, ' '),
      body: m[2],
    }));

  const declaration = (body: string, prop: string) =>
    new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, 'i').exec(body)?.[1].trim() ?? null;

  /**
   * The fill a rule paints. `background` and `background-color` in one rule is
   * rejected rather than resolved: reading the first and ignoring the second is
   * how `background: var(--ds-surface); background-color: var(--ds-navy)` would
   * render navy while measuring white. Same for a second declaration of either.
   */
  const fill = (body: string, where: string) => {
    const shorthand = [...body.matchAll(/(?:^|;)\s*background\s*:/gi)].length;
    const longhand = [...body.matchAll(/(?:^|;)\s*background-color\s*:/gi)].length;
    if (shorthand + longhand > 1) {
      throw new Error(`more than one background declaration in ${where} — the last one wins in the browser, so this test would measure the wrong fill`);
    }
    return declaration(body, 'background') ?? declaration(body, 'background-color');
  };

  /** The last compound of each comma-separated selector — what the rule styles.
   *  All of them, not just the first: `.something, .autonomy-segment--x { … }`
   *  targets the segment just as surely, and reading only `split(',')[0]` lets a
   *  fill in through the second half of a selector list. */
  const subjects = (selector: string) =>
    selector.split(',').map((part) => part.trim().split(/\s+/).pop() ?? '').filter(Boolean);

  it('declares the token with the exact grammar the component relies on', () => {
    // Width and style are load-bearing: the component pairs this with
    // outline-offset -3px, and both numbers were chosen against the container's
    // r=4px corner arc and the 1px armed stroke it must not cover.
    const raw = readToken(tokens, '--ds-ring-inset-on-light');
    expect(raw).toBe('2px solid var(--ds-stream-ink)');
  });

  it('is drawn on nothing but light, which is the whole reason one tone suffices', () => {
    const ink = resolveColor(tokens, /var\(\s*(--[\w-]+)\s*\)/.exec(readToken(tokens, '--ds-ring-inset-on-light'))![0]);

    // Backgrounds that are not a plain colour depend on what is BEHIND them, so
    // a declaration parser cannot resolve them alone. Each is resolved here
    // explicitly, against a value also read from the source rather than assumed.
    // LAST matching rule, not first: a later override is what the browser
    // paints, and `.find()` would keep measuring the superseded one.
    const lastFillFor = (name: string) => {
      const matches = rules(style).filter((r) => subjects(r.selector).includes(name) && fill(r.body, r.selector));
      expect(matches.length, `no background rule found for ${name}`).toBeGreaterThan(0);
      return resolveColor(tokens, fill(matches[matches.length - 1].body, name)!);
    };
    const popoverBg = lastFillFor('.autonomy-popover');
    const pillBg = lastFillFor('.autonomy-segments__pill');

    const substrates: { where: string; subject: string; hex: string }[] = [];
    for (const rule of rules(style)) {
      const subj = subjects(rule.selector).find((x) => /^\.autonomy-segment(--|:|$)/.test(x));
      if (!subj) continue;
      const bg = fill(rule.body, rule.selector);
      if (!bg) continue;

      if (bg === 'transparent') {
        // `.autonomy-segments--measured .autonomy-segment--active` goes
        // transparent so the sliding pill shows through. The pill is what the
        // ring is actually drawn over.
        substrates.push({ where: `${rule.selector} (pill shows through)`, subject: subj, hex: pillBg });
        continue;
      }
      const mix = /^color-mix\(\s*in srgb\s*,\s*(var\(\s*--[\w-]+\s*\)|#[0-9a-fA-F]{3,8})\s+(\d+)%\s*,\s*transparent\s*\)$/.exec(bg);
      if (mix) {
        // A translucent fill composites over the popover behind it.
        substrates.push({
          where: `${rule.selector} (over the popover)`,
          subject: subj,
          hex: composite(resolveColor(tokens, mix[1]), Number(mix[2]) / 100, popoverBg),
        });
        continue;
      }
      // Anything else must resolve to an opaque colour, or resolveColor throws.
      // Failing closed is the point: a background form this test does not
      // understand must not be silently dropped from the sweep.
      substrates.push({ where: rule.selector, subject: subj, hex: resolveColor(tokens, bg) });
    }

    // Counting is not coverage, and neither is substring matching. Two failed
    // versions of this check, both caught by injection:
    //   "at least four substrates" — renaming `--active` out of the sweep still
    //   left four, so a state vanished from the premise with the test green.
    //   then `selector.includes('--active')` — which the HOVER rule satisfies,
    //   because its selector is `.autonomy-segment:not(.autonomy-segment--active)
    //   :not(--armed):not(:disabled):hover`. The modifier appears inside a
    //   `:not()`, naming the state it EXCLUDES.
    // So: match the rule's SUBJECT with `:not(...)` stripped out — what the rule
    // actually styles, not what it happens to mention.
    const styledStates = substrates.map((s) => s.subject.replace(/:not\([^)]*\)/g, ''));
    for (const required of ['--active', '--armed', ':hover']) {
      expect(
        styledStates.join(' | '),
        `no rule STYLES the segment's "${required}" state — a renamed state leaves the premise unproven`,
      ).toContain(required);
    }
    // Plus the resting segment itself, whose subject carries no modifier at all.
    expect(
      styledStates.some((s) => s === '.autonomy-segment'),
      'no background found for the resting `.autonomy-segment` itself',
    ).toBe(true);

    const failures = substrates
      .map((s) => ({ ...s, ratio: contrastRatio(ink, s.hex) }))
      .filter((s) => s.ratio < FLOOR)
      .map((s) => `${s.where} -> ${s.hex} = ${s.ratio.toFixed(3)}:1`);
    expect(failures, `inset ring below ${FLOOR}:1 on a segment fill\n${failures.join('\n')}`).toEqual([]);
  });

  it('is used ONLY on controls whose own fill is light, wherever it is consumed', () => {
    // The state sweep above proves the premise for the autonomy dial. It says
    // nothing about the NEXT consumer, and the token grew one: CapabilityCard's
    // workload summary. A precondition proven for one caller and assumed for the
    // rest is exactly the shape of bug this whole file exists to stop, so find
    // every consumer from the source and prove each one.
    const ink = resolveColor(tokens, 'var(--ds-stream-ink)');
    const dir = resolve(srcDir, 'components');
    const consumers: { where: string; hex: string; ratio: number }[] = [];

    for (const file of readdirSync(dir).filter((f) => f.endsWith('.svelte'))) {
      const src = readFileSync(resolve(dir, file), 'utf8');
      const block = /<style[^>]*>([\s\S]*)<\/style>/.exec(src)?.[1] ?? '';
      if (!block.includes('--ds-ring-inset-on-light')) continue;
      const rs = rules(block);

      for (const rule of rs.filter((r) => /--ds-ring-inset-on-light/.test(r.body))) {
        for (const subj of subjects(rule.selector)) {
          // The control the ring is drawn ON, minus the focus pseudo-class.
          const base = subj.replace(/:focus-visible/g, '');
          const painting = rs.filter((r) => subjects(r.selector).includes(base) && fill(r.body, r.selector));
          expect(
            painting.length,
            `${file}: ${base} takes the inset ring but declares no background, so what the ring is drawn on cannot be checked`,
          ).toBeGreaterThan(0);
          const hex = resolveColor(tokens, fill(painting[painting.length - 1].body, base)!);
          consumers.push({ where: `${file} ${base}`, hex, ratio: contrastRatio(ink, hex) });
        }
      }
    }

    // Premise of the premise: if nobody consumes the token, this passes without
    // measuring anything, and the day someone adds a consumer it stays quiet.
    expect(consumers.length, 'no consumer of --ds-ring-inset-on-light found — did it get renamed?').toBeGreaterThanOrEqual(2);

    const failures = consumers
      .filter((c) => c.ratio < FLOOR)
      .map((c) => `${c.where} -> ${c.hex} = ${c.ratio.toFixed(3)}:1`);
    expect(failures, `inset ring used on a fill it cannot clear\n${failures.join('\n')}`).toEqual([]);
  });

  it('also clears the floor against the container border it sits beside', () => {
    // The outermost pixel of the band sits next to the segment separators and
    // the container's own border, both --ds-border-strong. This is the WORST
    // adjacency of the set, so it is asserted separately rather than buried.
    const ink = resolveColor(tokens, 'var(--ds-stream-ink)');
    const border = resolveColor(tokens, 'var(--ds-border-strong)');
    expect(contrastRatio(ink, border)).toBeGreaterThanOrEqual(FLOOR);
  });
});
