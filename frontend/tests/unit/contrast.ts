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

/**
 * sRGB channel -> linear, for WCAG relative luminance.
 *
 * The breakpoint is sRGB's 0.04045. WCAG's own prose says 0.03928, a rounding
 * of the same number that survived into the spec; the two disagree only for
 * channel values strictly between 10.01 and 10.31 out of 255, so no 8-bit color
 * can tell them apart. Compositing produces fractional channels that could land
 * in that window, which is the only reason it is worth being deliberate here.
 */
function channel(c8: number): number {
  const c = c8 / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
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
 * Split on top-level commas, ignoring those inside `rgba(...)` / `var(...)`.
 *
 * Empty entries are KEPT, not filtered. A trailing comma (`..., 4px var(--x),`)
 * is invalid CSS — the browser drops the whole declaration and renders no ring
 * — but dropping the empty entry here would leave two perfectly valid-looking
 * layers and the pin would pass over an indicator that does not exist.
 * Unbalanced parentheses are surfaced the same way, as one unparseable layer.
 */
function splitLayers(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = '';
  for (const c of value) {
    if (c === '(') depth++;
    else if (c === ')') depth--;
    if (c === ',' && depth === 0) {
      out.push(current);
      current = '';
    } else current += c;
  }
  out.push(current);
  if (depth !== 0) return [value];
  return out.map((s) => s.trim());
}

export type ShadowLayer = {
  hex: string;
  alpha: number;
  offsetX: number;
  offsetY: number;
  blur: number;
  spread: number;
  inset: boolean;
  /** Non-null when the layer is not four canonical lengths — see `layerGeometry`. */
  badSyntax: string | null;
};

/** `0`, or a px length. Nothing else is a length this design system writes. */
const LENGTH = String.raw`(?:0|-?\d*\.?\d+px)`;
/**
 * Exactly one color, in one of the three notations the ring has ever used.
 *
 * The rgb()/rgba() form is spelled out rather than allowed as "anything between
 * the parentheses", because CSS is stricter than JavaScript about numbers:
 * `rgba(255,255,255,1.)` has no digit after the point, so the browser rejects
 * the whole declaration and renders no ring — while `Number('1.')` is 1 and a
 * lenient parser reports a perfectly opaque white layer.
 */
const CHANNEL = String.raw`\d{1,3}`;
const ALPHA = String.raw`(?:0|1|0?\.\d+|1\.0+)`;
const COLOR = String.raw`(?:var\(\s*--[\w-]+\s*\)|#[0-9a-fA-F]{3,8}|rgba?\(\s*${CHANNEL}\s*,\s*${CHANNEL}\s*,\s*${CHANNEL}\s*(?:,\s*${ALPHA}\s*)?\))`;
/**
 * A whole layer: four lengths then one color, and nothing else.
 *
 * Anchoring the ENTIRE layer rather than just its lengths is what closes the
 * last family of false passes. Stripping colors and then validating only the
 * remainder accepts `0 0 0 2px var(--a) var(--b)` — two colors, invalid CSS,
 * the whole declaration dropped and no ring rendered — because the leftover
 * lengths still look canonical.
 */
const CANONICAL_LAYER = new RegExp(String.raw`^${LENGTH}(?:\s+${LENGTH}){3}\s+${COLOR}$`);

/**
 * The lengths of one layer, in `<offset-x> <offset-y> <blur> <spread>` order.
 *
 * The whole remainder is matched ANCHORED against exactly four canonical
 * lengths, and anything else is REJECTED rather than normalised. Scanning for
 * numbers instead is how a geometry check gets fooled, in more ways than one:
 *
 *   `1em` inner vs `2px` outer   reads as 1 < 2 and "increasing" — 1em is 16px
 *                                and buries the outer band completely
 *   `2%` / a bare `2`            invalid CSS; the browser drops the whole
 *                                declaration and there is no ring at all, while
 *                                the numbers still parse as sensible
 *   `calc(2px + 3em)`            a lenient scan reads some digits out of it
 *   a fifth length               silently ignored by a positional read
 *   `0 0 0 2px var(--a) var(--b)` two colors, also invalid, and invisible to a
 *                                check that strips colors before validating
 *
 * Matching the whole layer is also what keeps digits inside a token name
 * (`var(--ds-fs-1)`) from ever being read as a length.
 */
function layerGeometry(layer: string): Omit<ShadowLayer, 'hex' | 'alpha'> {
  const inset = /\binset\b/i.test(layer);
  const normalized = layer.replace(/\binset\b/gi, ' ').trim().replace(/\s+/g, ' ');

  if (!CANONICAL_LAYER.test(normalized)) {
    return { offsetX: 0, offsetY: 0, blur: 0, spread: 0, inset, badSyntax: normalized || '(empty)' };
  }
  // Safe now that the whole layer matched: the first four tokens are the lengths.
  const [offsetX, offsetY, blur, spread] = normalized.split(' ').slice(0, 4).map(parseFloat);
  return { offsetX, offsetY, blur, spread, inset, badSyntax: null };
}

/**
 * The color of one `box-shadow` layer, as {hex, alpha}.
 *
 * Uses the SAME strict channel/alpha grammar as `COLOR`, so a value that the
 * layer grammar rejects cannot be quietly re-read here as something valid.
 */
function layerColor(css: string, layer: string): { hex: string; alpha: number } {
  const rgbaMatch = new RegExp(
    String.raw`rgba?\(\s*(${CHANNEL})\s*,\s*(${CHANNEL})\s*,\s*(${CHANNEL})\s*(?:,\s*(${ALPHA})\s*)?\)`
  ).exec(layer);
  if (rgbaMatch) {
    const [, r, g, b, a] = rgbaMatch;
    const hex = '#' + [r, g, b].map((c) => Number(c).toString(16).padStart(2, '0')).join('');
    return { hex, alpha: a === undefined ? 1 : Number(a) };
  }
  const varMatch = /var\(\s*--[\w-]+\s*\)/.exec(layer);
  if (varMatch) return { hex: resolveColor(css, varMatch[0]), alpha: 1 };
  const hexMatch = /#[0-9a-fA-F]{6}\b/.exec(layer);
  if (hexMatch) return { hex: hexMatch[0], alpha: 1 };
  throw new Error(`no color found in box-shadow layer: ${layer}`);
}

/**
 * Every layer of a `box-shadow`, in DECLARATION order, with geometry.
 *
 * Declaration order is PAINT order: the first layer is drawn on top of the
 * later ones. That alone does NOT make the first layer the inner band — it does
 * so only when each subsequent layer spreads further. Give the first layer the
 * larger spread and it covers the ones behind it completely, which is why
 * geometry is parsed here and not discarded: a ring whose colors are all
 * correct can still be an invisible ring.
 */
export function shadowLayers(css: string, shadow: string): ShadowLayer[] {
  const layers = splitLayers(shadow).map((l) => ({ ...layerColor(css, l), ...layerGeometry(l) }));
  if (!layers.length) throw new Error(`no layers in box-shadow: ${shadow}`);
  return layers;
}

/**
 * Every `--ds-*` declaration in the source, comments stripped, in file order.
 *
 * Terminated by `;` OR by the closing `}`, because a final declaration may
 * legally omit its semicolon. That matters more than it looks: this one
 * function feeds the ring sweep, alias resolution and the scope guard, so
 * anything it cannot see is invisible to all three at once.
 *
 * Duplicates THROW. `readToken` returns the first match and the cascade uses
 * the last, so a duplicated name means an alias could be swept against a color
 * the browser never paints.
 */
export function tokenDeclarations(css: string): { name: string; value: string }[] {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: { name: string; value: string }[] = [];
  const seen = new Set<string>();
  for (const m of stripped.matchAll(/(--ds-[\w-]+)\s*:\s*([^;{}]+)(?=[;}])/g)) {
    const name = m[1];
    if (seen.has(name)) {
      throw new Error(
        `${name} is declared more than once; the cascade uses the last and readToken() reads the first`
      );
    }
    seen.add(name);
    out.push({ name, value: m[2].trim().replace(/\s+/g, ' ') });
  }
  return out;
}

/** The palette as a lookup, so alias resolution and accounting share one source. */
export type Palette = Map<string, string>;

export function paletteOf(css: string): Palette {
  return new Map(tokenDeclarations(css).map((d) => [d.name, d.value]));
}

export type TokenClass = { kind: 'color'; hex: string } | { kind: 'not-a-color'; why: string };

/**
 * A value's top-level components, splitting on whitespace and commas outside
 * `()` and outside quotes.
 *
 * The discrimination this rests on: **a color is exactly ONE top-level CSS
 * component value.** Every notation is a single token or a single function
 * call, and the commas inside those functions are nested. So a top-level space
 * or comma means a list or a shorthand, whatever colors it may contain —
 * `2px solid var(--ds-stream-ink)` and `0 1px 2px rgba(18, 21, 28, 0.04)` are
 * both settled here, with no allowlist of "non-color shapes" to maintain.
 *
 * A source scanner, NOT a CSS tokenizer, and the difference is real:
 * `r\67 b(18, 21, 28)` is one token to a browser and two to this. Callers
 * therefore reject backslashes outright rather than pretend to tokenize.
 */
function topLevelParts(value: string): {
  parts: string[];
  hasTopLevelComma: boolean;
  unbalanced: boolean;
} {
  const parts: string[] = [];
  let cur = '';
  let depth = 0;
  let quote: string | null = null;
  let hasTopLevelComma = false;
  const flush = () => {
    if (cur.trim()) parts.push(cur.trim());
    cur = '';
  };
  for (const c of value.trim()) {
    if (quote) {
      cur += c;
      if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
      cur += c;
      continue;
    }
    if (c === '(') depth++;
    else if (c === ')') depth--;
    if (depth === 0 && (c === ',' || /\s/.test(c))) {
      if (c === ',') hasTopLevelComma = true;
      flush();
      continue;
    }
    cur += c;
  }
  flush();
  return { parts, hasTopLevelComma, unbalanced: depth !== 0 || quote !== null };
}

/**
 * What a `--ds-*` declaration is, for the ring sweep.
 *
 * TOTAL by construction: a color, a reasoned not-a-color, or a THROW naming
 * the token and the notation. No fourth outcome, and in particular no silent
 * skip — that is the defect this closes (ds-spu). The previous implementation
 * kept `/^#[0-9a-fA-F]{6}$/` and dropped the rest on the floor, so a palette
 * entry written as `rgb()`, `oklch()` or `color-mix()` left the sweep and the
 * ring stopped being proven against it, with the test green.
 */
export function classifyTokenValue(
  palette: Palette,
  name: string,
  value: string,
  seen: readonly string[] = []
): TokenClass {
  const v = value
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/\s*!\s*important$/i, '')
    .trim();
  if (v.includes('\\')) {
    throw new Error(
      `${name}: "${v}" contains a backslash escape; this scanner is not a CSS tokenizer and will not guess at its token boundaries.`
    );
  }
  const { parts, hasTopLevelComma, unbalanced } = topLevelParts(v);
  if (unbalanced) throw new Error(`${name}: unbalanced parenthesis or quote in "${v}"`);
  if (!parts.length) throw new Error(`${name}: empty value`);
  if (hasTopLevelComma || parts.length > 1) {
    return {
      kind: 'not-a-color',
      why: hasTopLevelComma ? 'comma-separated list' : 'multi-part shorthand'
    };
  }
  const one = parts[0];

  const hex = /^#([0-9a-fA-F]+)$/.exec(one);
  if (hex) {
    const digits = hex[1].length;
    if (digits === 4 || digits === 8) {
      throw new Error(
        `${name}: "${one}" carries an alpha channel. A translucent token renders as itself mixed with whatever is behind it, so no ring can be proven against it in isolation — composite it explicitly, or declare an opaque value.`
      );
    }
    if (digits !== 3 && digits !== 6) throw new Error(`${name}: "${one}" is not a valid hex color`);
    const full =
      digits === 3
        ? one
            .slice(1)
            .split('')
            .map((c) => c + c)
            .join('')
        : one.slice(1);
    return { kind: 'color', hex: '#' + full.toLowerCase() };
  }

  throw new Error(`${name}: unclassifiable value "${one}"`);
}

/** Every `--ds-*` token in the palette whose value is an opaque hex color. */
export function colorTokens(css: string): Record<string, string> {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: Record<string, string> = {};
  for (const m of stripped.matchAll(/(--ds-[\w-]+)\s*:\s*([^;]+);/g)) {
    const value = m[2].trim();
    if (/^#[0-9a-fA-F]{6}$/.test(value)) out[m[1]] = value;
  }
  return out;
}
