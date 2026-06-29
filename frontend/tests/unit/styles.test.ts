import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

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
const stylesDir = resolve(here, '../../src/styles');

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
    // Patch off green would silently re-skin the glyphs. Kept distinct from the
    // status accents above so a future status re-tune can't mutate crew identity.
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

  it('pins the warm-neutral page background per design §3 (#fcfcfb)', () => {
    expect(stripComments(tokens)).toMatch(/--ds-bg\s*:\s*#fcfcfb/i);
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
