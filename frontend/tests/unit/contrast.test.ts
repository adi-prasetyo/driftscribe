import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';
import { compile } from 'svelte/compiler';
import { parse as acornParse } from 'acorn';
import {
  classifyTokenValue,
  colorTokens,
  composite,
  contrastOver,
  contrastRatio,
  paletteOf,
  readToken,
  resolveColor,
  shadowLayers,
  tokenDeclarations,
  type TokenClass
} from './contrast';

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
/** The frontend project root: the build can import from anywhere under it. */
const projectDir = resolve(here, '../..');

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

describe('palette declaration parsing (ds-spu)', () => {
  // A second, INDEPENDENT declaration counter, used to cross-check the real
  // parser. Independence is the whole point, so it must NOT reuse the regex
  // comment-strip: with that shared, a pair of tokens whose values are the
  // quoted strings "/*" and "*/" erases every declaration between them from
  // both counters, and they agree while jointly blind. CSS does not treat
  // comment delimiters inside strings as comments; neither does this.
  //
  // Line comments deliberately, not JSDoc: the delimiters this is about cannot
  // be written inside a block comment.
  const scan = (source: string) => {
    let out = '';
    let i = 0;
    let quote: string | null = null;
    while (i < source.length) {
      const c = source[i];
      if (quote) {
        if (c === '\\') {
          i += 2;
          continue;
        }
        if (c === quote) quote = null;
        i++;
        continue;
      }
      if (c === '"' || c === "'") {
        quote = c;
        i++;
        continue;
      }
      if (c === '/' && source[i + 1] === '*') {
        const end = source.indexOf('*/', i + 2);
        i = end === -1 ? source.length : end + 2;
        continue;
      }
      out += c;
      i++;
    }
    return [...out.matchAll(/--ds-[\w-]+\s*:/g)].length;
  };

  it('sees a final declaration that omits its semicolon', () => {
    const css = ':root { --ds-a: #111111; --ds-b: #222222 }';
    expect(tokenDeclarations(css).map((d) => d.name)).toEqual(['--ds-a', '--ds-b']);
  });

  it('refuses a duplicated token name rather than picking one', () => {
    // readToken() returns the FIRST match; the cascade uses the LAST. An alias
    // would be swept against a colour the browser never paints.
    const css = ':root { --ds-a: #111111; --ds-a: #222222; }';
    expect(() => tokenDeclarations(css)).toThrow(/--ds-a/);
  });

  it('counts the same palette as an INDEPENDENTLY written scanner', () => {
    // The premise every other guard rests on. Not a hardcoded 74: that must be
    // edited on every legitimate palette addition and stops meaning anything
    // the first time it is.
    expect(scan(tokens)).toBeGreaterThan(60); // premise: the file was read
    expect(tokenDeclarations(tokens).length).toBe(scan(tokens));
  });

  it('the independent scanner is actually independent', () => {
    // Parity is only worth having if the second counter cannot go blind the
    // same way the first does. Quoted components are not hypothetical here —
    // the three font tokens are quoted strings — so this pins the property on a
    // fixture rather than leaving it to be demonstrated by an injection that
    // today's palette cannot exhibit.
    expect(
      scan(
        '--ds-font-a: "A/*", sans-serif;\n--ds-hidden: oklch(0.6 0.2 250);\n--ds-font-b: "*/", serif;\n'
      )
    ).toBe(3);
  });

  it('no --ds-* name appears outside tokens.css, by any route that names it', () => {
    // Deliberately NOT tokenDeclarations(). That parser requires a declaration
    // to end at `;` or `}` — true of a stylesheet rule, false of most of the
    // ways a stray token actually arrives. FIVE spellings were verified in a
    // real browser to declare a token; each one defeated an earlier version of
    // this guard, so each has its own rule below:
    //
    //   <div style="--ds-x: #00ff00">     terminated by a QUOTE, so the
    //                                     declaration parser never saw it
    //   el.style.setProperty('--ds-x', v) no declaration syntax at all, and in
    //                                     a .ts file the walk never opened it
    //   <div style:--ds-x={'#f00'}>       a Svelte style DIRECTIVE. Compiles to
    //                                     set_style(..., {'--ds-x': …}) — the
    //                                     source contains neither `--ds-x:` nor
    //                                     `.setProperty(`
    //   :root { --ds-x/**/: #f00 }        CSS allows a comment between the
    //                                     property name and the colon
    //   :root { --d\73-bg: #f00 }         `\73` decodes to `s`. A text scanner
    //                                     cannot see the name that CSS sees
    //
    // So this scans SOURCE TEXT with several narrow rules and does no parsing it
    // could get wrong. The escape rule is the interesting one: it does not try
    // to DECODE escapes — that is precisely what a source scanner cannot do (the
    // classifier refuses it too). It flags their PRESENCE. There are zero
    // backslashes in any .css or .svelte file today, so "none" is both cheaper
    // and stronger than a decoder that could be wrong.
    //
    // Blunt on purpose: it has no comment parser for the token rule, so prose
    // that puts a token name immediately before a colon trips it. That is the
    // safe direction — it fails loudly and the fix is to reword. A comment
    // stripper here would make the guard LESS sensitive, which hides bugs.
    //
    // WHAT THIS PROVES, AND WHAT IT DOES NOT — stated precisely, because five
    // rewrites of this guard each believed they were complete and each was
    // wrong, and twice the overclaim was in the test's own NAME.
    //
    // Proves: no file under src/ outside tokens.css and the components' own CSS
    // contains a `--ds-*` name at all, and no file calls a style-mutation API.
    // Because every route to declaring a custom property needs the NAME, that
    // covers every API — present or future — for any STATICALLY NAMED write.
    // The walk has no extension allowlist, so it holds for `.mts`, `.d.ts` or
    // anything else added later.
    //
    // Does not prove: a name assembled at runtime and never written literally,
    // e.g. `style[m]('-' + '-ds-' + key, v)` or a name arriving from the server.
    // The API rule catches the common shape of that, but is an enumeration and
    // is not claimed to be complete. Closing it properly needs a runtime check
    // that instruments writes during the Playwright flows: ds-ley, filed rather
    // than pretended away.
    // The walk root is the PROJECT, not src/. Vite's entry lives in src/ but
    // its imports need not: `import '../reviewProbe.css'` from main.ts is a
    // production build input a src/-rooted walk never opens, and the build
    // really does emit --ds-bg twice with the later one winning. The true
    // boundary is the build graph; this is a deliberate SUPERSET of it, which
    // is the safe direction — a superset can only produce a false positive,
    // never a miss, and a directory root cannot silently shrink the way the
    // extension allowlist did. Pruned: dependencies, build output, and the test
    // tree, which is not a build input and names tokens on every other line.
    const PRUNE = /^(node_modules|dist|coverage|test-results|playwright-report|tests|\.svelte-kit|\.git)$/;
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (PRUNE.test(entry.name)) continue;
        const full = join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        // EVERY file, no extension allowlist. An allowlist is an enumeration,
        // and this one missed `.mts` — a first-class Vite/TS build input.
        else files.push(full);
      }
    };
    walk(projectDir);
    // Premise: the walk reached the tree AND can see its own subject. A guard
    // that cannot reach what it checks reports clean and proves nothing (#293).
    expect(files.length).toBeGreaterThan(30);
    // Premise: it climbed ABOVE src/, or round 10's finding 2 is silently back.
    expect(
      files.some((f) => !f.startsWith(srcDir)),
      'walk never left src/, so a build input imported from outside it is invisible'
    ).toBe(true);
    const tokensFile = files.find((f) => f.endsWith(join('styles', 'tokens.css')));
    expect(tokensFile, 'walk never reached tokens.css').toBeDefined();
    expect(
      files.filter((f) => f.endsWith('.ts')).length,
      'walk never reached a .ts file, where a CSSOM mutation would live'
    ).toBeGreaterThan(0);
    // The directive and escape rules only ever bite in a .svelte file, so a walk
    // that reached none of them would report clean while checking nothing.
    expect(
      files.filter((f) => f.endsWith('.svelte')).length,
      'walk never reached a .svelte file, where a style: directive would live'
    ).toBeGreaterThan(0);

    const strays: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, 'utf8');
      const where = relative(srcDir, file);
      if (file !== tokensFile) {
        // Tolerates a comment between the name and the colon, which CSS allows.
        for (const m of source.matchAll(/--ds-[\w-]+(?=(?:\s|\/\*[\s\S]*?\*\/)*:)/g)) {
          strays.push(`${where} declares ${m[0]}`);
        }
        // `@property --ds-x { … }` REGISTERS the name and is bound by `{`, not
        // `:`, so the rule above cannot see it — and it is not a near-miss: a
        // registered property with `initial-value: #f00` supplies that colour
        // to every consumer with no ordinary declaration anywhere.
        //
        // Matched as the CONSTRUCT, not as "name followed by `{`". The looser
        // form flagged `--ds-crew-{verb}` written in a CrewGlyph doc comment,
        // and rewording good prose to satisfy a punctuation heuristic is the
        // wrong trade when the construct is this easy to name. CSS binds a
        // custom-property name with exactly two constructs — a declaration and
        // this one — and unlike the DOM API surface, that pair is closed
        // grammar rather than an open list.
        // The separator tolerates comments for the same reason the colon rule
        // does: CSS lets a comment sit between the at-keyword and its prelude,
        // and `@property/**/--ds-x { … }` normalises to a valid registration in
        // the emitted build. Requiring literal whitespace here was recognising
        // the construct with a character regex rather than by its grammar.
        for (const m of source.matchAll(
          /@property(?:\s|\/\*[\s\S]*?\*\/)+(--ds-[\w-]+)/g
        )) {
          strays.push(`${where} registers ${m[1]} with @property`);
        }
      }
      // A Svelte style directive on ANY custom property, not just a --ds-* one:
      // there are zero in src today (style:flex and style:width are ordinary
      // properties and do not match), so "none" needs no name analysis.
      for (const m of source.matchAll(/\bstyle:--[\w-]*/g)) {
        strays.push(`${where} sets ${m[0]} via a Svelte style directive`);
      }
      // THE RULE THAT CARRIES THE JS SIDE, and the one that stopped the
      // enumeration: outside CSS and Svelte, a file may not contain the string
      // `--ds-` at all. Every way to write a custom property — setProperty,
      // setAttribute, cssText, attributeStyleMap, CSS Typed OM, anything added
      // to the platform next year — needs the NAME. Ban the name and the API
      // list stops mattering. There are zero mentions across all 52 .ts files
      // today, so it costs nothing, and it holds for `.mts`, `.d.ts`, `.json`
      // or any extension nobody has thought of yet because the walk above has
      // no allowlist. Deliberately blunt: a .ts file that so much as names a
      // token in a comment trips it, which is the safe direction.
      if (!/\.(css|svelte)$/.test(file)) {
        for (const m of source.matchAll(/--ds-[\w-]*/g)) {
          strays.push(`${where} names ${m[0]} (offset ${m.index}); only CSS and components may`);
        }
      }
      // Style-mutation APIs by name. This IS an enumeration and is no longer
      // load-bearing: for a statically named write the rule above already has
      // it, whatever API is used. This adds the case that rule cannot see — a
      // name assembled at runtime — for the handful of APIs worth naming. Zero
      // in src, so a blanket match costs nothing. Not a completeness claim.
      for (const m of source.matchAll(
        /\bsetProperty\b|\bcssText\b|\bsetAttribute\b|\battributeStyleMap\b|\bstyleMap\b|\.style\s*\??\s*\[/g
      )) {
        strays.push(`${where} writes style via CSSOM (offset ${m.index}) — see the note in this test`);
      }
      // An escape can spell a --ds-* name that no text scan can recognise, so in
      // the two file types that carry CSS the escape ITSELF is the finding. This
      // covers tokens.css too: an escaped name there would be misparsed just as
      // badly. Not applied to .ts/.js, where backslashes are ordinary regex and
      // string syntax and CSS can only be reached through the CSSOM rule above.
      if (/\.(css|svelte)$/.test(file)) {
        const bs = [...source.matchAll(/\\/g)];
        if (bs.length > 0) {
          strays.push(
            `${where} contains ${bs.length} backslash escape(s) (first at offset ${bs[0].index}) — ` +
              `this scanner cannot decode escapes, so it cannot prove none of them spells a --ds-* name`
          );
        }
      }
    }
    // The prune list above is only sound if nothing the build ships can live
    // behind it. Pruning `tests/` was NOT a superset of Vite's graph: an
    // `import '../tests/probe.css'` from main.ts really does ship, and really
    // does win over tokens.css by declaration order. So rather than claim a
    // superset, PIN THE PREMISE — every CSS import in the project must resolve
    // to a file this walk actually visited. That also covers `node_modules`,
    // which can never be walked: a bare CSS specifier is reported rather than
    // waved through. There are exactly two CSS imports today, both in main.ts.
    const walked = new Set(files.map((f) => resolve(f)));
    for (const file of files) {
      const where = relative(projectDir, file);
      for (const m of readFileSync(file, 'utf8').matchAll(
        /(?:@import\s+(?:url\()?|from\s*|import\s*)['"]([^'"]+\.s?css)['"]/g
      )) {
        const spec = m[1];
        if (!spec.startsWith('.')) {
          strays.push(`${where} imports CSS from a package (${spec}); the walk cannot see it`);
        } else if (!walked.has(resolve(dirname(file), spec))) {
          strays.push(`${where} imports ${spec}, which this walk never visited`);
        }
      }
    }
    // Premise: the import scan found the imports we know exist. If the pattern
    // silently matched nothing, the check above would pass while proving zero.
    expect(
      files.filter((f) => /\.s?css['"]/.test(readFileSync(f, 'utf8'))).length,
      'import scan found no CSS imports at all; main.ts has two'
    ).toBeGreaterThan(0);

    expect(
      strays,
      `the ring sweep reads only tokens.css, so a --ds-* that lives anywhere else is never measured:\n  ${strays.join('\n  ')}`
    ).toEqual([]);
  });

  it('no Svelte component names a --ds-* anywhere in its compiled module', () => {
    // The rules above scan SOURCE, and source scanning is spelling-by-spelling:
    // three separate reviews each produced a new way to write a declaration that
    // the previous scan missed. `style:--ds-x={…}` and `style={{'--ds-x': …}}`
    // look nothing alike in source and compile to the same call. (They do NOT
    // behave the same: the installed Svelte stringifies the object and only the
    // directive actually writes the property. That is exactly the distinction an
    // earlier revision of this file got wrong — see the controls below.)
    //
    // So this checks the COMPILER'S OUTPUT instead — one mechanism rather than a
    // list of spellings. Every Svelte style write, whatever its surface syntax,
    // ends up in the emitted module: a `set_style(...)` call for the dynamic
    // forms, a `from_html` template for the static attribute.
    //
    // It PARSES the emitted module with acorn and reads its string literals,
    // rather than pattern-matching the text. An earlier version paren-matched
    // the arguments of `set_style` and missed both of these. Neither turned out
    // to write the property in the installed Svelte, so neither was a live
    // fail-open — but a matcher that loses a literal it was pointed at is broken
    // regardless of whether that literal happened to matter:
    //
    //   <div style={styles}>          with `const styles = {'--ds-bg': …}` emits
    //                                 `set_style(div, styles)`. The literal is
    //                                 elsewhere in the module, so scanning the
    //                                 call's arguments never saw it
    //   { t: /[)]/.source, '--ds-x': … }  the `)` inside a regex character class
    //                                 read as structural, ending extraction early
    //
    // Hand-rolling a JS scanner is the same mistake as hand-rolling a CSS one,
    // made one layer down. A real parse also removes comments for free, which
    // matters because doc comments in <script> survive compilation and both
    // CrewGlyph and SealStamp name tokens in prose.
    const svelteFiles: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith('.svelte')) svelteFiles.push(full);
      }
    };
    walk(srcDir);
    expect(svelteFiles.length).toBeGreaterThan(30);

    /** Every string the compiled module actually contains, via a real JS parse. */
    const stringLiterals = (node: unknown, out: string[] = []): string[] => {
      if (!node || typeof node !== 'object') return out;
      if (Array.isArray(node)) {
        for (const child of node) stringLiterals(child, out);
        return out;
      }
      const n = node as Record<string, unknown> & { type?: string };
      if (n.type === 'Literal' && typeof n.value === 'string') out.push(n.value);
      if (n.type === 'TemplateLiteral') {
        for (const q of n.quasis as { value: { cooked?: string; raw: string } }[]) {
          out.push(q.value.cooked ?? q.value.raw);
        }
      }
      for (const key of Object.keys(n)) {
        if (key !== 'loc' && key !== 'range') stringLiterals(n[key], out);
      }
      return out;
    };
    // Anchored so a BEM modifier cannot read as a custom property: in
    // `btn--ds-primary` the `--ds-` is preceded by a word character. There are
    // ~120 such class names in src, and matching them would have made this
    // guard unusable — which is how a guard ends up deleted rather than fixed.
    const NAME = /(?:^|[^\w-])(--ds-[\w-]+)/g;
    const namesIn = (source: string): string[] =>
      stringLiterals(
        acornParse(compile(source, { generate: 'client' }).js.code, {
          ecmaVersion: 'latest',
          sourceType: 'module'
        })
      ).flatMap((s) => [...s.matchAll(NAME)].map((m) => m[1]));

    // Premise, and the one that matters most here: if this pipeline ever yielded
    // nothing — a compiler change, a parse that silently returns an empty body —
    // every file below reports clean while inspecting nothing. So pin it on
    // fixtures whose answers are known, one per spelling, plus the negative.
    //
    // These two DO write the property. Verified by rendering, not by reading
    // compiler output — see the note below about why that distinction bit.
    for (const [spelling, src] of [
      ['directive', `<div style:--ds-probe={'#f00'}></div>`],
      ['static attribute', `<div style="--ds-probe: #f00"></div>`]
    ] as const) {
      expect(namesIn(src), `positive control: a ${spelling} style write is no longer seen`).toContain(
        '--ds-probe'
      );
    }
    // These three do NOT write the property in the installed Svelte: `style={obj}`
    // stringifies the object into the style attribute, and rendering each leaves
    // getPropertyValue('--ds-probe') === ''. They are kept as EXTRACTION controls
    // — they pin that the parser still finds a literal through indirection and
    // past a regex literal, which is what the previous paren-matcher failed — and
    // as cheap cover should a future Svelte start honouring style objects.
    // Labelled honestly, because an earlier revision of this file called them
    // proven ways to declare a token on the strength of compiler output alone.
    for (const [shape, src] of [
      ['object literal', `<div style={{ '--ds-probe': '#f00' }}></div>`],
      ['static indirection', `<script>const s={'--ds-probe':'#f00'}</script><div style={s}></div>`],
      ['regex-literal neighbour', `<div style={{ t: /[)]/.source, '--ds-probe': '#f00' }}></div>`]
    ] as const) {
      expect(namesIn(src), `extraction control: the parser no longer sees a ${shape}`).toContain(
        '--ds-probe'
      );
    }
    expect(
      namesIn(`<div class="btn btn--ds-primary">x</div>`),
      'negative control: a BEM modifier must not read as a custom property'
    ).toEqual([]);

    const strays: string[] = [];
    for (const file of svelteFiles) {
      for (const name of namesIn(readFileSync(file, 'utf8'))) {
        strays.push(`${relative(srcDir, file)} names ${name} in compiled output`);
      }
    }

    expect(
      strays,
      `a custom property named in a component is never in tokens.css, so the sweep cannot see it:\n  ${strays.join('\n  ')}`
    ).toEqual([]);
  });
});

describe('palette token classification (ds-spu)', () => {
  /** A palette for the direct cases; most need no aliases. */
  const P = (css = '') => paletteOf(css);

  it('classifies EVERY --ds-* declaration, and returns a usable answer for each', () => {
    const palette = paletteOf(tokens);
    const decls = tokenDeclarations(tokens);
    expect(decls.length).toBeGreaterThan(60); // premise: parsed the palette

    const bad: string[] = [];
    for (const { name, value } of decls) {
      let c: TokenClass;
      try {
        c = classifyTokenValue(palette, name, value);
      } catch (e) {
        bad.push(`${name}: ${value} -> ${(e as Error).message}`);
        continue;
      }
      // "Did not throw" is not a result. An implementation returning undefined
      // or {} would pass a truthiness check while classifying nothing.
      if (c?.kind === 'color') {
        if (!/^#[0-9a-f]{6}$/.test(c.hex))
          bad.push(`${name}: colour is not canonical #rrggbb: ${c.hex}`);
      } else if (c?.kind === 'not-a-color') {
        if (!c.why?.trim()) bad.push(`${name}: classified not-a-colour with no reason`);
      } else {
        bad.push(`${name}: unrecognised classification ${JSON.stringify(c)}`);
      }
    }
    expect(
      bad,
      `every palette declaration must resolve to a colour or be provably not one:\n  ${bad.join('\n  ')}`
    ).toEqual([]);
  });

  it('resolves an INLINE alias, which readToken() cannot see', () => {
    // The two-parsers-disagree bug: readToken() is line-anchored, so it finds
    // nothing here while tokenDeclarations() finds both. Alias resolution used
    // the former and accounting the latter.
    //
    // This must CLASSIFY --ds-b, not merely look up --ds-a. Asserting the
    // lookup alone would stay green with alias resolution switched back to
    // readToken(), which is exactly the injection it is supposed to pin.
    const p = paletteOf(':root { --ds-a: #ffffff; --ds-b: var(--ds-a); }');
    expect(classifyTokenValue(p, '--ds-b', p.get('--ds-b')!)).toEqual({
      kind: 'color',
      hex: '#ffffff'
    });
  });

  it('follows a var() alias to the colour it really is', () => {
    const p = P('--ds-a: #4285f4;');
    expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a)')).toEqual({
      kind: 'color',
      hex: '#4285f4'
    });
  });

  it('does not mistake an alias to a shorthand for a colour', () => {
    const p = P('--ds-a: 2px solid #4285f4;');
    expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a)').kind).toBe('not-a-color');
  });

  it('ignores the fallback entirely when the primary is a colour', () => {
    // Even an unsupported notation in the fallback is irrelevant: it cannot
    // render, so rejecting on it would fail a token that is perfectly fine.
    const p = P('--ds-a: #4285f4;');
    expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a, oklch(0.6 0.2 250))')).toEqual({
      kind: 'color',
      hex: '#4285f4'
    });
  });

  it('propagates a failure from the aliased declaration, naming the chain', () => {
    const p = P('--ds-a: oklch(0.6 0.2 250);');
    expect(() => classifyTokenValue(p, '--ds-b', 'var(--ds-a)')).toThrow(
      /--ds-b -> --ds-a[\s\S]*oklch/
    );
  });

  it('ignores the fallback when the primary is declared as a NON-colour too', () => {
    // The token is whatever the primary is. `--ds-a: 2px` substitutes fine, so
    // --ds-b computes to `2px` and the fallback never renders (verified in
    // Chromium). An earlier draft threw here, on the
    // theory that the consuming declaration goes invalid at computed-value time
    // and paints something unknowable. That confuses the TOKEN with a
    // DECLARATION that consumes it: `border-width: var(--ds-b)` is perfectly
    // valid, and the same argument would have condemned the no-fallback form,
    // which this classifies as not-a-colour without complaint.
    const p = P('--ds-a: 2px;');
    expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a, #ffffff)')).toEqual(
      classifyTokenValue(p, '--ds-b', 'var(--ds-a)')
    );
    expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a, #ffffff)').kind).toBe('not-a-color');
  });

  it('throws rather than ignoring a fallback the browser WOULD follow', () => {
    // The rule above ("declared -> ignore the fallback") is only sound because
    // the classifier returns a verdict solely for a primary it could classify.
    // A DECLARED primary can still be the guaranteed-invalid value, and then the
    // browser does follow the fallback. All three forms below were verified in
    // Chromium to compute to the fallback #ff0000. So the boundary that has to
    // hold is "declared AND classifiable" — each of these must throw, never
    // return the primary's verdict and never quietly return the fallback's.
    expect(() => classifyTokenValue(P('--ds-a: initial;'), '--ds-b', 'var(--ds-a, #ffffff)')).toThrow(
      /--ds-b/
    );
    expect(() =>
      classifyTokenValue(P('--ds-a: var(--ds-nope);'), '--ds-b', 'var(--ds-a, #ffffff)')
    ).toThrow(/--ds-nope/);
    expect(() =>
      classifyTokenValue(P('--ds-a: var(--ds-c);\n--ds-c: var(--ds-a);'), '--ds-b', 'var(--ds-a, #ffffff)')
    ).toThrow(/cycle/);
  });

  it('refuses a fallback whose primary is not in the palette at all', () => {
    // "absent from this string" is not "undefined in the browser": another
    // stylesheet, an inline style or script may define it, and then the fallback
    // never renders. The live component uses of this form are not palette tokens.
    expect(() => classifyTokenValue(P(), '--ds-b', 'var(--external-brand, #ffffff)')).toThrow(
      /--ds-b/
    );
    expect(() => classifyTokenValue(P(), '--ds-b', 'var(--ds-missing)')).toThrow(/--ds-b/);
  });

  it('names the cycle instead of recursing until the stack gives out', () => {
    const p = P('--ds-a: var(--ds-b);\n--ds-b: var(--ds-a);');
    // The DIAGNOSTIC, not merely "it threw" — a RangeError from an exhausted
    // stack would also throw, and would tell the next reader nothing.
    expect(() => classifyTokenValue(p, '--ds-a', 'var(--ds-b)')).toThrow(/cycle/i);
  });

  it('converts the rgb() form it can convert exactly', () => {
    expect(classifyTokenValue(P(), '--ds-x', 'rgb(18, 21, 28)')).toEqual({
      kind: 'color',
      hex: '#12151c'
    });
    expect(classifyTokenValue(P(), '--ds-x', 'rgba(18, 21, 28, 1)').kind).toBe('color');
    // CSS clamps out-of-range channels rather than dropping the declaration, so
    // this matches the browser instead of inventing a failure.
    expect(classifyTokenValue(P(), '--ds-x', 'rgb(300, 0, 0)')).toEqual({
      kind: 'color',
      hex: '#ff0000'
    });
  });

  it('names the token AND the notation for a colour it will not convert', () => {
    // Asserts the SPECIFIC diagnostic, not just "it threw". Otherwise dropping a
    // name from COLOR_FUNCTIONS still lands on the unknown-function throw, whose
    // message also contains the token and the function — and the test would not
    // notice that the notation-specific guidance had disappeared.
    for (const [value, notation] of [
      ['oklch(0.6 0.2 250)', 'oklch'],
      ['color-mix(in srgb, #4285f4 42%, transparent)', 'color-mix'],
      ['hsl(210, 90%, 60%)', 'hsl'],
      ['lab(50% 40 59.5)', 'lab'],
      ['contrast-color(#4285f4)', 'contrast-color']
    ] as const) {
      expect(() => classifyTokenValue(P(), '--ds-accent', value), value).toThrow(
        new RegExp(`--ds-accent[\\s\\S]*${notation}\\(\\) color notation`)
      );
    }
  });

  it('throws on an UNKNOWN function rather than calling it not-a-colour', () => {
    // Round 1's actual bug. Substitution and conditional functions can produce
    // colours, so no denylist of colour functions is complete and "unknown" must
    // never resolve to "safe to skip".
    for (const v of ['env(--brand, #fff)', 'attr(data-c type(<color>), red)', 'whatever(1)']) {
      expect(() => classifyTokenValue(P(), '--ds-x', v), v).toThrow(/unknown function/);
    }
  });

  it('rejects a translucent colour rather than measuring the wrong thing', () => {
    expect(() => classifyTokenValue(P(), '--ds-x', 'rgba(18, 21, 28, 0.42)')).toThrow(/translucent/);
    expect(() => classifyTokenValue(P(), '--ds-x', '#12151c6b')).toThrow(/alpha channel/);
  });

  it('rejects an rgb() form it does not parse instead of guessing', () => {
    expect(() => classifyTokenValue(P(), '--ds-x', 'rgb(18 21 28)')).toThrow(/--ds-x/);
  });

  it('lets a known non-colour function through', () => {
    expect(classifyTokenValue(P(), '--ds-ease', 'cubic-bezier(0.2, 0.7, 0.2, 1)').kind).toBe(
      'not-a-color'
    );
  });

  it('lets a plain number or length through as not-a-colour', () => {
    for (const v of ['400', '1.55', '0.8125rem', '999px', '68ch', '120ms', '0.06em', '56rem']) {
      expect(classifyTokenValue(P(), '--ds-x', v).kind, v).toBe('not-a-color');
    }
  });

  it('refuses to guess whether a bare keyword is a colour', () => {
    for (const v of ['white', 'transparent', 'currentColor', 'rebeccapurple']) {
      expect(() => classifyTokenValue(P(), '--ds-x', v), v).toThrow(/--ds-x/);
    }
    // CSS-wide keywords are NOT safe: each can expose an inherited or cascaded
    // custom-property value, which may well be a colour.
    for (const v of ['inherit', 'initial', 'unset', 'revert', 'revert-layer']) {
      expect(() => classifyTokenValue(P(), '--ds-x', v), v).toThrow(/--ds-x/);
    }
    expect(classifyTokenValue(P(), '--ds-x', 'none').kind).toBe('not-a-color');
  });

  it('strips !important, which is a declaration suffix and not part of the colour', () => {
    expect(classifyTokenValue(P(), '--ds-x', '#4285f4 !important')).toEqual({
      kind: 'color',
      hex: '#4285f4'
    });
  });

  it('does not let a quoted string split into a false multi-part value', () => {
    // Distinguishes quote tracking on its own. The real font stacks have
    // top-level commas either way and so prove nothing about it; a single quoted
    // string containing a comma is one component, and must reach the bare-value
    // throw rather than be waved through as a comma-separated list.
    expect(() => classifyTokenValue(P(), '--ds-x', '"a,b"')).toThrow(/--ds-x/);
  });

  it('refuses a value containing a backslash escape', () => {
    // `r\67 b(...)` is one token to a browser and two to this scanner. Also
    // closes the escaped-quote gap, which the scanner does not model.
    expect(() => classifyTokenValue(P(), '--ds-x', 'r\\67 b(18, 21, 28)')).toThrow(/backslash/);
  });

  it('sweeps a colour token written in a non-hex notation', () => {
    const css = '--ds-base: #12151c;\n--ds-alias: var(--ds-base);\n--ds-rgb: rgb(66, 133, 244);\n';
    expect(colorTokens(css)).toEqual({
      '--ds-base': '#12151c',
      '--ds-alias': '#12151c',
      '--ds-rgb': '#4285f4'
    });
  });

  it('propagates the failure instead of skipping the token', () => {
    // Directly, not via today's palette — every current declaration classifies
    // cleanly, so a `try {} catch {}` inside colorTokens would otherwise leave
    // the whole suite green.
    expect(() => colorTokens('--ds-x: oklch(0.6 0.2 250);')).toThrow(/--ds-x[\s\S]*oklch/);
  });

  it('still sweeps every hex token it swept before, with the same value', () => {
    // The change must move no cheese on today's palette. A property rather than a
    // count: a count would need editing on every legitimate palette addition, and
    // would stop meaning anything the first time it was.
    const swept = colorTokens(tokens);
    const hexes = tokenDeclarations(tokens).filter((d) => /^#[0-9a-fA-F]{6}$/.test(d.value));
    expect(hexes.length).toBeGreaterThan(25); // premise: found the palette
    for (const { name, value } of hexes) {
      expect(swept[name], `${name} left the sweep`).toBe(value.toLowerCase());
    }
  });

  it('resolveColor accepts any notation the sweep accepts', () => {
    // Otherwise the palette cannot actually adopt one: --ds-navy is swept here
    // AND resolved by the instrument-band test.
    expect(resolveColor('--ds-a: rgb(14, 27, 95);', 'var(--ds-a)')).toBe('#0e1b5f');
    expect(() => resolveColor('', 'rgba(0, 0, 0, 0.5)')).toThrow(/translucent/);
    expect(() => resolveColor('', '2px solid #fff')).toThrow(/not a colou?r|shorthand/i);
    // Pre-existing hole: /^#[0-9a-fA-F]{3,8}$/ accepted invalid 5- and 7-digit hex.
    expect(() => resolveColor('', '#abcde')).toThrow();
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

  /**
   * Subjects whose translucent fills are resolved by a dedicated test rather
   * than by the generic sweep — today only the autonomy dial, whose
   * `transparent` active state and `color-mix` armed state are composited
   * against the pill and popover in the sweep above. Adding a prefix here is a
   * promise that such a test exists.
   */
  const RESOLVED_BY_A_COMPONENT_TEST = ['.autonomy-segment'];

  it('is used ONLY on controls whose own fill is light, wherever it is consumed', () => {
    // The state sweep above proves the premise for the autonomy dial. It says
    // nothing about the NEXT consumer, and the token grew one: CapabilityCard's
    // workload summary. A precondition proven for one caller and assumed for the
    // rest is exactly the shape of bug this whole file exists to stop, so find
    // every consumer from the source and prove each one.
    const ink = resolveColor(tokens, 'var(--ds-stream-ink)');
    const consumers: { where: string; hex: string; ratio: number }[] = [];

    // Walk ALL of src/, not just components/. `src/App.svelte` and
    // `src/styles/base.css` both already consume ring tokens, so a directory-
    // scoped scan would let the next consumer escape the premise entirely —
    // silently, since nothing would report a file it never opened.
    const styleSources = (dir: string): { file: string; css: string }[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
        const full = resolve(dir, e.name);
        if (e.isDirectory()) return styleSources(full);
        if (e.name.endsWith('.svelte')) {
          const src = readFileSync(full, 'utf8');
          return [{ file: full.slice(srcDir.length + 1), css: /<style[^>]*>([\s\S]*)<\/style>/.exec(src)?.[1] ?? '' }];
        }
        if (e.name.endsWith('.css')) return [{ file: full.slice(srcDir.length + 1), css: readFileSync(full, 'utf8') }];
        return [];
      });

    for (const { file, css: block } of styleSources(srcDir)) {
      // Skip the declaration itself; only USES matter here.
      if (!new RegExp(`var\\(\\s*--ds-ring-inset-on-light`).test(block)) continue;
      const rs = rules(block);

      for (const rule of rs.filter((r) => /--ds-ring-inset-on-light/.test(r.body))) {
        for (const subj of subjects(rule.selector)) {
          // The control the ring is drawn ON, minus the focus pseudo-class.
          const base = subj.replace(/:focus-visible/g, '');
          // EVERY fill this control can take, not just its resting one. The ring
          // is painted while focused, so a rule like
          //   .foo:focus-visible { background: var(--ds-navy); outline: <ring> }
          // puts a dark fill under it at exactly the moment it is drawn — and
          // measuring only the rule whose subject is exactly `.foo` walks right
          // past it. Match any subject that STARTS with the base, which is what
          // picks up `:focus-visible`, `:hover`, `--modifier`, `[attr]`, etc.
          const painting = rs.filter(
            (r) => subjects(r.selector).some((x) => x === base || x.startsWith(`${base}:`) || x.startsWith(`${base}--`) || x.startsWith(`${base}[`)) && fill(r.body, r.selector),
          );
          expect(
            painting.length,
            `${file}: ${base} takes the inset ring but declares no background, so what the ring is drawn on cannot be checked`,
          ).toBeGreaterThan(0);
          // ALL of them must clear the floor — the ring cannot know which state
          // it will be drawn in.
          for (const rule of painting) {
            const declared = fill(rule.body, rule.selector)!;
            // A fully see-through fill has no substrate this test can name: what
            // the ring lands on depends on what is painted BEHIND the control,
            // which no per-rule reading can supply. Rather than skip it (silent)
            // or guess (wrong), require a component-specific proof and name it.
            if (/^(transparent|color-mix\()/.test(declared)) {
              expect(
                RESOLVED_BY_A_COMPONENT_TEST.some((prefix) => base.startsWith(prefix)),
                `${file}: ${rule.selector} takes the inset ring over "${declared}", whose substrate depends on what is behind it. Add a component-specific test that resolves it (see the autonomy dial sweep above) and list its subject in RESOLVED_BY_A_COMPONENT_TEST.`,
              ).toBe(true);
              continue;
            }
            const hex = resolveColor(tokens, declared);
            consumers.push({ where: `${file} ${rule.selector}`, hex, ratio: contrastRatio(ink, hex) });
          }
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
