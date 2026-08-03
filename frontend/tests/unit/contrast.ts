/**
 * WCAG contrast arithmetic for the token pins.
 *
 * These helpers exist so the accessibility floors in `tokens.css` are ENFORCED
 * rather than merely documented. Before this, every contrast figure in the
 * codebase lived in a CSS comment ("#656c7a is the lightest step that clears
 * all of them - paper 5.06, white 5.28..."), which records what was true when
 * someone last did the arithmetic and cannot notice when it stops being true.
 *
 * The pins read the REAL declared values out of `tokens.css` source rather than
 * a copy of them, so re-tuning a palette entry re-runs the arithmetic against
 * the new value instead of silently drifting away from a hardcoded expectation.
 */

/** sRGB channel -> linear, per WCAG 2.x relative luminance. */
function channel(c8: number): number {
  const c = c8 / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function rgb(hex: string): [number, number, number] {
  const h = hex.trim().replace(/^#/, '');
  const full =
    h.length === 3
      ? h
          .split('')
          .map((c) => c + c)
          .join('')
      : h;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`not a hex color: ${hex}`);
  return [
    parseInt(full.slice(0, 2), 16),
    parseInt(full.slice(2, 4), 16),
    parseInt(full.slice(4, 6), 16)
  ];
}

function luminanceOf([r, g, b]: [number, number, number]): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function relativeLuminance(hex: string): number {
  return luminanceOf(rgb(hex));
}

function ratioOf(la: number, lb: number): number {
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

export function contrastRatio(a: string, b: string): number {
  return ratioOf(relativeLuminance(a), relativeLuminance(b));
}

/**
 * Contrast of `fg` at `alpha` against the OPAQUE `bg` it composites onto.
 *
 * Deliberately does NOT quantize the composited channels to 8 bits first.
 * Rounding a channel that lands on exactly x.5 is a coin flip between
 * languages - Python's banker's rounding and JS's round-half-up disagree, and
 * that alone moved one of these measurements from 2.235 to 2.217. Neither
 * number is more true than the other, so the pin measures the unrounded
 * composite and no rounding policy can shift a verdict.
 */
export function contrastOver(fg: string, alpha: number, bg: string): number {
  const f = rgb(fg);
  const b = rgb(bg);
  const mixed = f.map((fc, i) => fc * alpha + b[i] * (1 - alpha)) as [number, number, number];
  return ratioOf(luminanceOf(mixed), luminanceOf(b));
}

/**
 * Source-over compositing of `fg` at `alpha` onto an OPAQUE `bg`.
 *
 * This is what both contrast defects had in common: a translucent ring and a
 * faded numeral are not their declared color, they are their declared color
 * mixed with whatever is behind them. Contrast has to be measured on the result.
 */
export function composite(fg: string, alpha: number, bg: string): string {
  const f = rgb(fg);
  const b = rgb(bg);
  const out = f.map((fc, i) => Math.round(fc * alpha + b[i] * (1 - alpha)));
  return '#' + out.map((c) => c.toString(16).padStart(2, '0')).join('');
}

/** Read a single `--token: value;` declaration out of CSS source. */
export function readToken(css: string, name: string): string {
  const m = new RegExp(`^\\s*${name}\\s*:\\s*([^;]+);`, 'm').exec(css);
  if (!m) throw new Error(`token not declared: ${name}`);
  return m[1].trim().replace(/\s+/g, ' ');
}

/**
 * Resolve a token to a hex color, following `var(--x)` indirection.
 *
 * Throws on a non-opaque result, which is deliberate: a caller asking for "the
 * color of this thing" cannot be handed something whose real color depends on
 * what is behind it. Composite it explicitly instead.
 */
export function resolveColor(css: string, value: string, depth = 0): string {
  if (depth > 8) throw new Error(`var() indirection too deep: ${value}`);
  const v = value.trim();
  const varRef = /^var\(\s*(--[\w-]+)\s*\)$/.exec(v);
  if (varRef) return resolveColor(css, readToken(css, varRef[1]), depth + 1);
  if (/^#[0-9a-fA-F]{3,8}$/.test(v)) {
    if (v.length === 9 || v.length === 5) throw new Error(`color has alpha: ${v}`);
    return v;
  }
  throw new Error(`not an opaque color: ${v}`);
}

/**
 * Pull the color out of a `box-shadow` value, as {hex, alpha}.
 *
 * Handles the two forms the ring has taken: a bare `rgba(...)` and a
 * `var(--token)` reference. Alpha is surfaced rather than resolved away so the
 * ring pin can composite it over each ground and report what actually renders.
 */
export function shadowColor(css: string, shadow: string): { hex: string; alpha: number } {
  const rgbaMatch = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/.exec(shadow);
  if (rgbaMatch) {
    const [, r, g, b, a] = rgbaMatch;
    const hex = '#' + [r, g, b].map((c) => Number(c).toString(16).padStart(2, '0')).join('');
    return { hex, alpha: a === undefined ? 1 : Number(a) };
  }
  const varMatch = /var\(\s*--[\w-]+\s*\)/.exec(shadow);
  if (varMatch) return { hex: resolveColor(css, varMatch[0]), alpha: 1 };
  const hexMatch = /#[0-9a-fA-F]{6}\b/.exec(shadow);
  if (hexMatch) return { hex: hexMatch[0], alpha: 1 };
  throw new Error(`no color found in box-shadow: ${shadow}`);
}
