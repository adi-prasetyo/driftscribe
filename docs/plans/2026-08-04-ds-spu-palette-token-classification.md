# Palette token classification (ds-spu) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A `--ds-*` palette entry written in any colour notation is either swept
by the focus-ring contrast pin or fails the test by name and notation. Never
silently skipped.

**Architecture:** Replace `colorTokens()`'s hex-only filter with a *total*
classifier over every `--ds-*` declaration: each one resolves to a colour, is
provably not a colour, or **throws**. One declaration parser feeds everything —
the sweep and alias resolution — and `resolveColor()`, the
file's dominant resolver, is routed through the same classifier so the palette
can actually adopt a non-hex notation.

**Tech Stack:** TypeScript, Vitest (`npm run test:unit`), no new dependencies.
Baseline in this worktree: 65 files, 1915 tests, green.

**Revision:** rewritten twice after Codex review. Round 1 found the central
mechanism still fail-open; round 2 found the `var()` semantics unsound and the
alias lookup bypassing the parser. See "What the reviews changed".

---

## Why this is worth doing, and why now

`tests/unit/contrast.ts:253` keeps only `/^#[0-9a-fA-F]{6}$/`:

```ts
export function colorTokens(css: string): Record<string, string> {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: Record<string, string> = {};
  for (const m of stripped.matchAll(/(--ds-[\w-]+)\s*:\s*([^;]+);/g)) {
    const value = m[2].trim();
    if (/^#[0-9a-fA-F]{6}$/.test(value)) out[m[1]] = value;
  }
  return out;
}
```

Its consumer is the sweep at `tests/unit/contrast.test.ts:116`, *"is carried by
some layer against EVERY color in the palette"* — the pin that makes the
two-tone ring an invariant rather than a one-time measurement.

**The hole is fail-open.** Today every colour token is a plain 6-digit hex, so
coverage is complete: 74 declarations, 32 of them direct 6-digit hex, and that
32 is the figure the `--ds-ring` design comment argues from. The first token
written as `rgb()`, `hsl()`, `oklch()`, `color-mix()`, or as `var(--other)`
leaves the sweep, the ring stops being proven against it, and the test stays
green.

Not hypothetical: **ds-1um** is queued to introduce
`--ds-scrim: color-mix(in srgb, var(--ds-fg) 42%, transparent)`, the first token
that would fall through. That bead is now blocked on this one.

## What the reviews changed

**Round 1 — the draft reproduced the bug it fixes.** It denylisted CSS *colour*
functions and returned `not-a-color` for every other function:

| value | draft 1 | why it is wrong |
|---|---|---|
| `var(--x, #fff)` | silently not-a-colour | fallback form misses the exact alias grammar, lands on the unknown-function path. **Already used here** — `ConversationThread.svelte:342-343`, `ApprovalDesk.svelte:1007`, all three colour-valued |
| `#fff !important` | silently not-a-colour | a declaration suffix, not part of the value; splits into two top-level parts |
| `env(--x, #fff)`, typed `attr()` | silently not-a-colour | both resolve to a colour |
| `contrast-color(...)` | silently not-a-colour | a colour function missing from the denylist |

Once CSS has general substitution and conditional functions (`var`, `env`,
`attr`, `if`, `@function`), **no denylist of colour functions can be complete**,
so "unknown function" must never mean "not a colour". The direction inverted: a
small allowlist of certainly-non-colour functions, colour functions convert or
throw by name, **everything else throws**.

**Round 2 — three more fail-open or incoherent paths.**

- **`var()` assumed a closed world it does not have.** Draft 2 followed the
  fallback whenever the referenced token was absent *from the string it was
  handed*. The browser may get that token from another stylesheet, an inline
  style, or script, in which case the fallback never renders and the sweep
  measured a colour that is not painted.
- **A declared non-colour with a colour fallback was called not-a-colour.** For
  `--ds-a: 2px; --ds-b: var(--ds-a, #fff)`, substitution *succeeds* with `2px`,
  the consuming declaration becomes invalid at computed-value time, and what
  paints is an inherited or initial value. So `--ds-b` is not provably a
  non-colour — it is unknowable from here, and must throw.
  **↳ Round 5 REVERSED this. It is wrong, and the change it prompted was backed
  out.** The arithmetic is right and the subject is wrong: that is a fact about a
  declaration *consuming* the token, not about the token. `--ds-b` computes to
  `2px`, `border-width: var(--ds-b)` is perfectly valid, and the identical
  argument would have condemned the no-fallback form, which every draft
  classified as not-a-colour without complaint. See Round 5 below for what
  replaced it.
- **Alias resolution bypassed the very parser Task 1 exists to fix.** Draft 2
  resolved aliases through line-anchored `readToken()`, which cannot see
  `:root { --ds-a: #fff; --ds-b: var(--ds-a); }` — while `tokenDeclarations()`
  sees both. Verified: `readToken` returns no match for that input. Two parsers
  in one file, disagreeing.

Round 2 also corrected two of my numbers and repaired five injections; see Task 9.

**Round 3 — two guards that still could not fail.** The test pinning alias
resolution asserted only the palette *lookup* and never classified the alias, so
the injection meant to catch a regression to `readToken()` stayed green. And I
had recorded the independent scanner's string-awareness as "unproven", arguing a
`/*`-in-a-string token cannot occur in a design-token file — rationalising, since
the three font tokens *are* quoted strings, and the fixture costs one assertion.
Both fixed. Round 3 also filled in three missing rows of the `var()` table.

**Round 4 — against the finished work, and it found the guard I had just
written.** Task 7's scope pin used `tokenDeclarations()`, whose regex requires a
declaration to end at `;` or `}`. That is true of a stylesheet rule and false of
both ways a stray token actually arrives:

- `<div style="--ds-x: #00ff00">` — valid CSS, paints, terminated by a **quote**
- `el.style.setProperty('--ds-x', v)` — no declaration syntax at all, and in a
  `.ts` file the walk never opened the file

Both verified to pass the guard. The premise the plan claims to pin ("no `--ds-*`
set from JS or an inline `style=`") is still true of this codebase, but the guard
did not enforce it — the #293 lesson exactly: *unmeasured is not fine.* The fix
scans source text for a token in declaration position across
`.svelte`/`.css`/`.ts`/`.js`/`.html` and flags any CSSOM custom-property write,
with **no parser it could get wrong**. It is deliberately blunt: prose that puts
a token name immediately before a colon trips it, which is the safe direction,
and one comment in `ChatForm.svelte` was reworded rather than adding a comment
stripper that would make the guard *less* sensitive.

Round 4 also reversed the `var()` fallback row above.

**Round 5 — against the round-4 fix, and the guard was STILL fail-open.** Three
further spellings were each verified in Chromium to declare a token and to leave
the round-4 guard green:

- `<div style:--ds-x={'#f00'}>` — a Svelte style **directive**. Compiles to
  `set_style(…, {'--ds-x': …})`; the source contains neither `--ds-x:` nor
  `.setProperty(`. This is the realistic one — it is idiomatic Svelte.
- `:root { --ds-x/**/: #f00 }` — CSS allows a comment between the property name
  and the colon, and the `\s*:` lookahead allowed only whitespace.
- `:root { --d\73-bg: #f00 }` — `\73` decodes to `s`, so this **is** `--ds-bg`.

The guard now carries one narrow rule per spelling. The escape rule is the
interesting one: it does not try to *decode* escapes, which is exactly what a
source scanner cannot do — it flags their **presence** in any `.css`/`.svelte`
file. There are zero today, so "none" is both cheaper and stronger than a decoder
that could be wrong, and it needs no `<style>`-block extraction (which would have
been a parser, failing open when it got a boundary wrong).

Round 5 also found the corrected `var()` prose *overstating* the browser rule.
The fallback is used whenever the primary is the **guaranteed-invalid** value,
and "not set at all" is only one way to be that: `--ds-a: initial`, a cycle, and
an unresolved `var()` are all declared yet guaranteed-invalid, and all three do
fall through to the fallback (verified in Chromium). The code was already right —
it throws on all three — but the stated boundary had to change from "declared" to
**"declared and successfully classified"**, and now has a test.

**Round 6 — a sixth spelling, and the point where enumerating stopped being the
answer.** Three more: `style['setProperty'](…)`, `style.setProperty?.(…)`, and a
Svelte style **object** `<div style={{ '--ds-x': '#f00' }}>` which compiles to
`set_style(div, {'--ds-x': …})` and looks nothing like the directive in source.

> ⚠️ **Round 8 correction to that third one.** I recorded it as a proven way to
> declare a token on the strength of the compiler output. It is not. The
> installed Svelte stringifies the object into the style attribute, and rendering
> it leaves `getPropertyValue('--ds-x') === ''`. I had verified the *compilation*
> and written it down as verified *behaviour* — the exact mistake this change is
> about, made while documenting the change. The guard still covers the shape, as
> an extraction control and as cover if a future Svelte honours style objects,
> but it is no longer claimed to declare anything.

Six spellings found across three rounds is not a list that was nearly complete —
it is evidence that **scanning source text for surface syntax cannot enforce this
premise**, because the number of ways to spell a style write is bounded only by
the language. So the Svelte side changed mechanism: compile, then **parse**.

The first attempt at that was still wrong, and round 7 caught it. It compiled
each `.svelte` and paren-matched the arguments of every `set_style(...)` call —
which fails two ways. `<div style={styles}>` with `const styles = {'--ds-bg': …}`
emits `set_style(div, styles)`, putting the literal elsewhere in the module. And
a paren matcher is not a JS tokenizer: in `{ t: /[)]/.source, '--ds-x': … }` it
reads the `)` inside a regex character class as structural and stops early.
Hand-rolling a JS scanner is the same mistake as hand-rolling a CSS one, made one
layer down.

What ships parses the emitted module with **acorn** and reads its string
literals. No pattern-matching over code, so indirection and regex literals are
both non-issues, and comments — which survive compilation, and which `CrewGlyph`
and `SealStamp` both use to name tokens in prose — are gone for free. The name
match is anchored so a BEM modifier (`btn--ds-primary`, ~120 in `src/`) cannot
read as a custom property. Positive controls cover each spelling plus the BEM
negative, because a pipeline that silently yielded nothing would report clean
while inspecting nothing.

The CSSOM rule was generalised the same way, from `\.setProperty\s*\(` to the
*name* `setProperty`/`cssText` plus any bracketed `.style[…]` access — one rule
covering dot, bracket, and optional-call spellings instead of three. The five
real `.style` uses in `src/` are all plain `.style.overflow` / `.style.height`
assignments, so requiring dot access costs nothing.

**What is still not enforced, stated plainly:** a custom-property name computed
at runtime (`` `--ds-${key}` `` fed to a bracketed call) is invisible to every
rule here. Only a runtime check — rendering the app and enumerating the custom
properties actually set — would close it, and that is filed rather than bodged.

**Round 8 — an eleventh route, and a claim of mine that was simply false.**
`el.setAttribute('style', '--ds-x: #ff0000')` declares the property while
touching neither `.style` nor `setProperty`, and passed both guards. Zero
`setAttribute` calls exist in `src/`, so the blanket name joins the CSSOM rule at
no cost. But the review's sharper point stands: one more rule is one more
spelling, and equivalent routes (a generic serializer, an imported helper) will
keep existing.

So the guard's **claim** was narrowed to match what it can actually prove. The
test is no longer called "is reading the WHOLE palette"; it is *"no `--ds-*` is
declared outside tokens.css by any **statically visible** route"*, and it states
in full what it does not cover — any write whose name or declaration is assembled
at runtime. That is ds-ley, and per the same review it should **instrument
writes** during the Playwright flows rather than enumerate final rendered
properties, which would miss unexercised branches and properties written then
removed.

Round 8 also caught three of my positive controls proving the wrong thing (see
the round-6 correction above): the object-based shapes are extraction controls,
not write controls, and are now labelled that way.

The pattern across all eight rounds is worth naming: **every round, the weakest
thing was a test that could not fail.** The classifier itself was wrong twice
(round 1's denylist, round 4's fallback row); the instruments were wrong
eighteen times — and most of those were a guard added *in this change*, found in
the five rounds *after* the reviews had already started finding exactly that.
Five times running, the fix for a fail-open guard was itself fail-open.

Two things ended it, and neither was a better pattern. **Match the instrument to
the substrate**: text rules where the substrate is text, a real parser where it
is code, a reported backslash where the scanner honestly cannot read what CSS
reads. And **narrow the claim to what the instrument proves** — the last round
found not only a missed route but a test whose name promised more than any static
check can deliver, and three controls I had labelled "verified" on the strength of
compiler output I never rendered.

## Design decisions, and what was rejected

**A colour is exactly one top-level CSS component value.** Every notation is a
single token (`#fff`, `white`) or a single function call; the commas inside
`rgb(...)` are nested. A top-level space or comma therefore means a list or a
shorthand, whatever colours it contains — this alone settles
`2px solid var(--ds-stream-ink)` and `0 1px 2px rgba(18, 21, 28, 0.04)`. It
mirrors `CANONICAL_LAYER` in the same file: anchor the whole value, reject
rather than normalise.

The claim is about *CSS component values*, not source characters, and the
scanner is not a CSS tokenizer: `r\67 b(18, 21, 28)` is one token to a browser
(the space terminates the hex escape) and two to a character scanner. So **any
value containing a backslash throws**. That also closes the escaped-quote gap
for free.

**Aliases are closed-world.** A `var()` must resolve inside the palette source
or throw — fallback or no fallback. The three live component fallbacks do not
argue against this: none is a `--ds-*` declaration this sweep classifies.

**Allowlist the non-colour functions; throw on unknown.** See round 1.

**Bare idents throw** unless in a tiny `NON_COLOR_KEYWORDS` set. `white` is a
colour and `none` is not, and nothing in the value says which. The CSS-wide
keywords are *not* safe: `inherit`, `unset`, `revert`, `revert-layer` can expose
an inherited custom-property value, so they throw; only `none` and `auto` stay.

**Rejected: converting `oklch()`/`lab()`.** Not because conversion is
approximate — it is standards-defined and exact for in-gamut colours. The cost
is matching *browser gamut mapping* for out-of-sRGB values, real untested
surface, and a verdict computed from a differently-mapped colour is confidently
wrong rather than absent. The bead's acceptance criterion explicitly allows a
named failure.

**Rejected: a name heuristic** (which the bead floats). Names lie in both
directions: `--ds-shadow` contains four colours and is not one.

**Out of scope:** the seven `color: #fff` literals in components are text
colours on filled controls, not palette tokens. **ds-1um's scrim will still fail
after this change** — loudly, which is what ds-spu promises — because a
translucent value has no contrast ratio of its own. It needs a substrate
specific proof like the ds-2fp premise test. Recorded on that bead.

## Scope premise this also has to pin

> **Read the instrument table below before adding a rule here.** Eleven routes
> were found across five review rounds, four of them in guards written *during*
> this change. If the answer to a new one looks like "add another regex", it is
> probably the wrong answer.


The sweep reads exactly one file. True today — 74 declarations in
`src/styles/tokens.css` and zero elsewhere in `src/`. Nothing enforced it. This
is the #293 lesson repeating: that review found the consumer scan reading only
`src/components/` while `App.svelte` and `base.css` already consumed the tokens
it checked.

**Enumerating the spellings was the wrong idea, and it took four attempts to
admit it.** Ten distinct ways to declare a token were verified here — an
ordinary rule, an inline `style=`, a Svelte `style:--x` directive, a Svelte
style *object*, that object reached through a `const`, a CSSOM write spelled
with a dot, with brackets, or with an optional call, a comment between name and
colon, and an escaped identifier. Each guard was written, believed complete,
and then shown to pass one nobody had thought of. The ways to spell a style
write are bounded only by the language, so a scanner over surface syntax cannot
enforce this premise at all.

What works is picking an instrument that matches the substrate:

| substrate | instrument | why |
|---|---|---|
| CSS text | anchored source rules | declarations really are textual there |
| CSS escapes | report the backslash, never decode it | "I cannot read this" is a claim a text scanner can make good on |
| Svelte templates | **compile, then parse the emitted JS** | every spelling funnels into one module; acorn reads its string literals |
| CSSOM from JS | one rule on the method NAME | dot, bracket and optional-call are the same call |

The residual after all that is a name computed at runtime, which no static
instrument can see. Filed as **ds-ley**, not papered over.

---

### Task 1: One declaration parser the guards can rely on

Everything below reads declarations through this function — the sweep, alias
resolution. A declaration it cannot see is invisible to both of
them at once, which is why it comes first.

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Baseline**

Run: `cd frontend && npm run test:unit -- --run`
Expected: 65 files / 1915 tests, PASS. The count must only grow.

**Step 2: Write the failing tests**

New top-level `describe` after the `focus ring` block:

```ts
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
        if (c === '\\') { i += 2; continue; }
        if (c === quote) quote = null;
        i++; continue;
      }
      if (c === '"' || c === "'") { quote = c; i++; continue; }
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
      hex: '#ffffff',
    });
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
      scan('--ds-font-a: "A/*", sans-serif;\n--ds-hidden: oklch(0.6 0.2 250);\n--ds-font-b: "*/", serif;\n')
    ).toBe(3);
  });
});
```

**Step 3: Run — verify FAIL** (`tokenDeclarations is not a function`).

**Step 4: Implement**

```ts
/**
 * Every `--ds-*` declaration in the source, comments stripped, in file order.
 *
 * Terminated by `;` OR by the closing `}`, because a final declaration may
 * legally omit its semicolon. That matters more than it looks: this one
 * function feeds both the ring sweep and alias resolution, so anything it
 * cannot see is invisible to both at once.
 *
 * It deliberately does NOT feed the scope guard. That guard once used this
 * parser and was fail-open because of it: a `;`/`}` terminator is a property of
 * a stylesheet rule, and a stray token arrives in ways that have none.
 *
 * Duplicates THROW. `readToken` returns the first match and the cascade uses
 * the last, so a duplicated name means an alias could be swept against a colour
 * the browser never paints.
 */
export function tokenDeclarations(css: string): { name: string; value: string }[] {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: { name: string; value: string }[] = [];
  const seen = new Set<string>();
  for (const m of stripped.matchAll(/(--ds-[\w-]+)\s*:\s*([^;{}]+)(?=[;}])/g)) {
    const name = m[1];
    if (seen.has(name)) {
      throw new Error(`${name} is declared more than once; the cascade uses the last and readToken() reads the first`);
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
```

**Step 5: Run, then commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): one declaration parser the guards can rely on (ds-spu)"
```

---

### Task 2: Total accounting, asserted by shape

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Branch order is load-bearing** — Tasks 2-5 each insert before the final
throw, and the final order must be exactly:

1. strip `!important`
2. throw on backslash
3. structural: top-level comma or >1 part → not-a-colour
4. hex
5. **`var()`** — before the general function branch, or `var(...)` is read as an
   unknown function and throws
6. functions: non-colour allowlist → colour functions → **unknown throws**
7. numeric
8. keyword allowlist
9. throw

**Step 1: Write the failing test**

```ts
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
        if (!/^#[0-9a-f]{6}$/.test(c.hex)) bad.push(`${name}: colour is not canonical #rrggbb: ${c.hex}`);
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
});
```

Extend the line-5 import with `classifyTokenValue`, `tokenDeclarations`,
`paletteOf`, and `type TokenClass`.

**Step 2: Run — FAIL** (`classifyTokenValue is not a function`).

**Step 3: Implement steps 1-4 and 9 of the branch order**

```ts
export type TokenClass =
  | { kind: 'color'; hex: string }
  | { kind: 'not-a-color'; why: string };
```

```ts
/**
 * A value's top-level components, splitting on whitespace and commas outside
 * `()` and outside quotes.
 *
 * The discrimination this rests on: **a colour is exactly ONE top-level CSS
 * component value.** Every notation is a single token or a single function
 * call, and the commas inside those functions are nested. So a top-level space
 * or comma means a list or a shorthand, whatever colours it may contain —
 * `2px solid var(--ds-stream-ink)` and `0 1px 2px rgba(18, 21, 28, 0.04)` are
 * both settled here, with no allowlist of "non-colour shapes" to maintain.
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
```

```ts
/**
 * What a `--ds-*` declaration is, for the ring sweep.
 *
 * TOTAL by construction: a colour, a reasoned not-a-colour, or a THROW naming
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
  const v = value.trim().replace(/\s+/g, ' ').replace(/\s*!\s*important$/i, '').trim();
  if (v.includes('\\')) {
    throw new Error(`${name}: "${v}" contains a backslash escape; this scanner is not a CSS tokenizer and will not guess at its token boundaries.`);
  }
  const { parts, hasTopLevelComma, unbalanced } = topLevelParts(v);
  if (unbalanced) throw new Error(`${name}: unbalanced parenthesis or quote in "${v}"`);
  if (!parts.length) throw new Error(`${name}: empty value`);
  if (hasTopLevelComma || parts.length > 1) {
    return { kind: 'not-a-color', why: hasTopLevelComma ? 'comma-separated list' : 'multi-part shorthand' };
  }
  const one = parts[0];

  const hex = /^#([0-9a-fA-F]+)$/.exec(one);
  if (hex) {
    const digits = hex[1].length;
    if (digits === 4 || digits === 8) {
      throw new Error(`${name}: "${one}" carries an alpha channel. A translucent token renders as itself mixed with whatever is behind it, so no ring can be proven against it in isolation — composite it explicitly, or declare an opaque value.`);
    }
    if (digits !== 3 && digits !== 6) throw new Error(`${name}: "${one}" is not a valid hex colour`);
    const full = digits === 3 ? one.slice(1).split('').map((c) => c + c).join('') : one.slice(1);
    return { kind: 'color', hex: '#' + full.toLowerCase() };
  }

  throw new Error(`${name}: unclassifiable value "${one}"`);
}
```

**Step 4: Run**

Expected: FAIL listing **exactly 34** single-component declarations — 31
numerics and three `cubic-bezier(...)`. (The other 8 non-hex declarations — 3
font stacks, 3 shadow lists, `--ds-ring`, `--ds-ring-inset-on-light` — are
already settled as structurally multipart.) **If the count is not 34, stop** and
find out why; that list is the specification for Tasks 3-5.

**Step 5: Commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): structural colour rule and total accounting (ds-spu)"
```

---

### Task 3: `var()` — closed-world, and honest about the fallback

**Policy, stated once because the browser's is not obvious:**

| case | verdict | why |
|---|---|---|
| `var(--x)`, `--x` declared as a colour | that colour | |
| `var(--x, …)`, `--x` declared as a colour | that colour, **fallback not evaluated** | the fallback cannot render, so it is not this sweep's business — even if it is a notation the sweep would reject on its own |
| `var(--x)`, `--x` declared as a non-colour | not-a-colour | the token simply *is* that non-colour |
| `var(--x, …)`, `--x` declared as a non-colour | not-a-colour, **fallback not evaluated** | same as the row above. **Corrected in round 4** — see below |
| `var(--x)`, `--x` not in the palette | **throw** | nothing to resolve, and no fallback to reason about either |
| `var(--x, …)`, `--x` not in the palette | **throw** | "absent from this string" is not "undefined in the browser" — another stylesheet, an inline style or script may define it, in which case the fallback never renders |
| `--x` declared, but classifying its value throws | **propagate**, fallback or not | the failure names the real declaration via the `name -> ref` chain |
| cycle | **throw**, naming the loop | |

So the fallback is never *followed*, and the two halves of that have different
reasons. When the primary is **declared and classifiable**, the browser itself
ignores the fallback. When the primary is **absent from this source**, the
browser might well use it, and that is exactly why this throws: following it
would require assuming a closed world this file does not have.

**Round 5 tightened "declared" to "declared and classifiable".** The fallback is
used whenever the primary is the guaranteed-invalid value, and *not set at all*
is only one way to be that. `--ds-a: initial`, a cycle, and an unresolved
`var()` are all declared yet guaranteed-invalid, and Chromium follows the
fallback for all three. None is a hole, because the classifier throws on each
rather than returning a verdict — but the rule above is only sound *because* it
does, so that is now the stated boundary and has its own test.

**Round 4 reversed row 4.** Drafts 2 and 3 threw there, arguing that `2px`
substitutes fine, the consuming declaration goes invalid at computed-value time,
and what paints is inherited or initial. The arithmetic is right and the subject
is wrong: that is a fact about a *declaration consuming the token*, not about
the token. `--ds-b` computes to `2px` either way, `border-width: var(--ds-b)` is
perfectly valid, and the identical argument would have condemned the
**no-fallback** form — which drafts 2 and 3 classified as not-a-colour without
complaint. An inconsistency that only survived because it failed in the safe
direction.

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the failing tests**

```ts
it('follows a var() alias to the colour it really is', () => {
  const p = P('--ds-a: #4285f4;');
  expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a)')).toEqual({ kind: 'color', hex: '#4285f4' });
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
    hex: '#4285f4',
  });
});

it('propagates a failure from the aliased declaration, naming the chain', () => {
  const p = P('--ds-a: oklch(0.6 0.2 250);');
  expect(() => classifyTokenValue(p, '--ds-b', 'var(--ds-a)')).toThrow(/--ds-b -> --ds-a[\s\S]*oklch/);
});

it('ignores the fallback when the primary is declared as a NON-colour too', () => {
  // ROUND 5: this replaced a throw. The token is whatever the primary is —
  // `--ds-a: 2px` substitutes fine, so --ds-b computes to `2px` and the fallback
  // never renders (verified in Chromium). The old throw confused the TOKEN with
  // a DECLARATION consuming it, and the same argument would have condemned the
  // no-fallback form, which is classified not-a-colour without complaint.
  const p = P('--ds-a: 2px;');
  expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a, #ffffff)')).toEqual(
    classifyTokenValue(p, '--ds-b', 'var(--ds-a)')
  );
  expect(classifyTokenValue(p, '--ds-b', 'var(--ds-a, #ffffff)').kind).toBe('not-a-color');
});

it('throws rather than ignoring a fallback the browser WOULD follow', () => {
  // The rule above is only sound because a verdict is returned solely for a
  // primary that could be classified. All three of these are DECLARED yet
  // guaranteed-invalid, and Chromium computes them to the fallback #ff0000.
  expect(() => classifyTokenValue(P('--ds-a: initial;'), '--ds-b', 'var(--ds-a, #ffffff)')).toThrow(/--ds-b/);
  expect(() => classifyTokenValue(P('--ds-a: var(--ds-nope);'), '--ds-b', 'var(--ds-a, #ffffff)')).toThrow(/--ds-nope/);
  expect(() => classifyTokenValue(P('--ds-a: var(--ds-c);\n--ds-c: var(--ds-a);'), '--ds-b', 'var(--ds-a, #ffffff)')).toThrow(/cycle/);
});

it('refuses a fallback whose primary is not in the palette at all', () => {
  // "absent from this string" is not "undefined in the browser": another
  // stylesheet, an inline style or script may define it, and then the fallback
  // never renders. The live component uses of this form are not palette tokens.
  expect(() => classifyTokenValue(P(), '--ds-b', 'var(--external-brand, #ffffff)')).toThrow(/--ds-b/);
  expect(() => classifyTokenValue(P(), '--ds-b', 'var(--ds-missing)')).toThrow(/--ds-b/);
});

it('names the cycle instead of recursing until the stack gives out', () => {
  const p = P('--ds-a: var(--ds-b);\n--ds-b: var(--ds-a);');
  // The DIAGNOSTIC, not merely "it threw" — a RangeError from an exhausted
  // stack would also throw, and would tell the next reader nothing.
  expect(() => classifyTokenValue(p, '--ds-a', 'var(--ds-b)')).toThrow(/cycle/i);
});
```

**Step 2: Run — FAIL** (`unclassifiable value "var(--ds-a)"`).

**Step 3: Implement — insert AFTER the hex branch, BEFORE the function branch**

```ts
  // `var()` is indirection, not a function to classify — and it must be handled
  // before the general function branch or it reads as an unknown function.
  //
  // Cycles are detected by NAME, not by a recursion counter: a counter only
  // bounds the damage and its limit is arbitrary, while the visited set states
  // the actual invariant and its message names the loop.
  const varCall = /^var\(\s*(--[\w-]+)\s*(?:,([\s\S]*))?\)$/.exec(one);
  if (varCall) {
    const [, ref, fallback] = varCall;
    if (seen.includes(ref)) {
      throw new Error(`${name}: var() alias cycle: ${[...seen, ref].join(' -> ')}`);
    }
    if (!palette.has(ref)) {
      throw new Error(
        `${name}: var(${ref}) is not declared in the palette source, so this sweep cannot know what it paints.` +
          (fallback === undefined
            ? ''
            : ` It has a fallback, but "absent here" is not "undefined in the browser" — another stylesheet, an inline style or script may define ${ref}, and then the fallback never renders.`)
      );
    }
    // ROUND 5: a `target.kind === 'not-a-color' && fallback !== undefined`
    // throw stood here and was BACKED OUT. `--ds-b` is simply whatever `--ds-a`
    // is; the browser does not follow the fallback for a primary that
    // substitutes successfully, and throwing here contradicted the no-fallback
    // form's verdict. The guaranteed-invalid cases that DO follow the fallback
    // (`initial`, a cycle, an unresolved var()) already throw above or via the
    // recursion, which is what makes the plain return below sound.
    return classifyTokenValue(palette, `${name} -> ${ref}`, palette.get(ref)!, [...seen, ref]);
  }
```

**Step 4: Run** — the five tests PASS; accounting still fails on numerics and
`cubic-bezier`.

**Step 5: Commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): closed-world var() resolution for the palette sweep (ds-spu)"
```

---

### Task 4: Functions — convert, reject by name, or throw. Never skip.

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the failing tests**

```ts
it('converts the rgb() form it can convert exactly', () => {
  expect(classifyTokenValue(P(), '--ds-x', 'rgb(18, 21, 28)')).toEqual({ kind: 'color', hex: '#12151c' });
  expect(classifyTokenValue(P(), '--ds-x', 'rgba(18, 21, 28, 1)').kind).toBe('color');
  // CSS clamps out-of-range channels rather than dropping the declaration, so
  // this matches the browser instead of inventing a failure.
  expect(classifyTokenValue(P(), '--ds-x', 'rgb(300, 0, 0)')).toEqual({ kind: 'color', hex: '#ff0000' });
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
    ['contrast-color(#4285f4)', 'contrast-color'],
  ] as const) {
    expect(() => classifyTokenValue(P(), '--ds-accent', value), value).toThrow(
      new RegExp(`--ds-accent[\\s\\S]*${notation}\\(\\) colour notation`)
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
  expect(classifyTokenValue(P(), '--ds-ease', 'cubic-bezier(0.2, 0.7, 0.2, 1)').kind).toBe('not-a-color');
});
```

**Step 2: Run — FAIL.**

**Step 3: Implement**

```ts
/**
 * Functions that certainly do not produce a colour.
 *
 * An ALLOWLIST, and the direction is the whole point. Round 1 of this change
 * denylisted the colour functions and let every other function through as
 * not-a-colour — silently dropping `var(--x, #fff)`, `env(…, #fff)`, typed
 * `attr()` and `contrast-color()`, reproducing the bug being fixed. Once CSS
 * has general substitution and conditional functions, no colour-function
 * denylist can be complete, so anything not listed here THROWS.
 */
const NON_COLOR_FUNCTIONS = new Set(['cubic-bezier', 'steps', 'linear', 'calc', 'clamp', 'min', 'max']);

/**
 * Colour functions, so the failure can name the notation and say what to do.
 *
 * DIAGNOSTIC, not a safety boundary: a colour function missing from here still
 * throws, via the unknown-function branch. The tests assert the specific
 * message so the distinction cannot rot.
 */
const COLOR_FUNCTIONS = new Set([
  'rgb', 'rgba', 'hsl', 'hsla', 'hwb', 'lab', 'lch', 'oklab', 'oklch',
  'color', 'color-mix', 'light-dark', 'device-cmyk', 'contrast-color', 'color-contrast',
]);
```

and, before the numeric branch:

```ts
  const fn = /^([a-z][-a-z0-9]*)\(([\s\S]*)\)$/i.exec(one);
  if (fn) {
    const f = fn[1].toLowerCase();
    if (NON_COLOR_FUNCTIONS.has(f)) return { kind: 'not-a-color', why: `${f}()` };

    if (f === 'rgb' || f === 'rgba') {
      // The SAME strict grammar the box-shadow layer parser uses, so a value
      // rejected there cannot be quietly re-read as valid here.
      const m = new RegExp(
        String.raw`^rgba?\(\s*(${CHANNEL})\s*,\s*(${CHANNEL})\s*,\s*(${CHANNEL})\s*(?:,\s*(${ALPHA})\s*)?\)$`
      ).exec(one);
      if (!m) throw new Error(`${name}: "${one}" is an rgb()/rgba() form this sweep does not parse — only the comma-separated 0-255 form. Declare it as #rrggbb.`);
      if (m[4] !== undefined && Number(m[4]) !== 1) {
        throw new Error(`${name}: "${one}" is translucent (alpha ${m[4]}). Its rendered colour depends on what is behind it, so no ring can be proven against it in isolation — composite it explicitly, or declare an opaque value.`);
      }
      // CSS clamps out-of-range channels; this matches the browser.
      const hex = [m[1], m[2], m[3]]
        .map((c) => Math.min(255, Number(c)).toString(16).padStart(2, '0'))
        .join('');
      return { kind: 'color', hex: '#' + hex };
    }

    if (COLOR_FUNCTIONS.has(f)) {
      throw new Error(`${name}: "${one}" uses the ${f}() colour notation, which this sweep does not convert. Conversion is standards-defined, but matching the browser's gamut mapping for out-of-sRGB values is untested surface here, and a verdict computed from a differently-mapped colour is confidently wrong rather than absent. Declare the token as #rrggbb, or add a TESTED conversion and extend this branch.`);
    }

    throw new Error(`${name}: "${one}" uses the unknown function ${f}(). It is not classified as a non-colour, because substitution and conditional functions can produce colours and a silent skip is exactly the defect this guard exists to prevent. Add ${f} to NON_COLOR_FUNCTIONS if it certainly is not a colour.`);
  }
```

**Step 4: Run** — the six tests PASS.

**Step 5: Commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): unknown functions throw; rgb() converts; notations named (ds-spu)"
```

---

### Task 5: Numerics, bare idents, and the parser's own edges

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the failing tests**

```ts
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
  expect(classifyTokenValue(P(), '--ds-x', '#4285f4 !important')).toEqual({ kind: 'color', hex: '#4285f4' });
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
```

**Step 2: Run** — only the **numeric list** and the **`none`** case are newly
failing. The `!important`, quoted-string, backslash and bare-colour-name cases
already pass, because Task 2 implemented those branches and its final throw
already carries the token name. That is expected, not a sign the tests are
inert: they are here to pin behaviour the later branches must not regress, and
injections 7, 8 and 11 are what prove they can fail.

**Step 3: Implement**

```ts
/** A number with an optional unit — never a colour. */
const NUMERIC = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:px|rem|em|ch|ex|vh|vw|vmin|vmax|%|ms|s|deg|fr|pt)?$/;

/**
 * Bare idents that are certainly not colours.
 *
 * Deliberately tiny. A bare ident is either a CSS named colour (`white`,
 * `transparent`, `currentColor`) or a keyword, and the value cannot say which,
 * so anything not listed FAILS rather than being assumed. The CSS-wide keywords
 * are absent on purpose: `inherit`/`unset`/`revert`/`revert-layer` can expose an
 * inherited custom-property value, which may be a colour.
 */
const NON_COLOR_KEYWORDS = new Set(['none', 'auto']);
```

replacing the final throw:

```ts
  if (NUMERIC.test(one)) return { kind: 'not-a-color', why: 'numeric' };
  if (NON_COLOR_KEYWORDS.has(one.toLowerCase())) return { kind: 'not-a-color', why: 'keyword' };

  throw new Error(`${name}: "${one}" is not a value this sweep recognises. A CSS named colour (white, transparent, currentColor) and a non-colour keyword are indistinguishable here, so it refuses to guess: declare a colour as #rrggbb, or add the keyword to NON_COLOR_KEYWORDS in contrast.ts.`);
```

**Step 4: Run the file** — expected PASS, accounting included: all 74 classified.

**Step 5: Commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): classify numerics, fail closed on bare keywords (ds-spu)"
```

---

### Task 6: Wire both resolvers onto the classifier

`colorTokens()` is the bead's subject, but `resolveColor()` is the file's
dominant resolver — **13 non-definition call sites** (12 in `contrast.test.ts`,
one in `layerColor()`) — and is hex-only. Leaving it behind means a palette
entry written as `rgb()` is swept correctly and then throws in the
instrument-band test at `contrast.test.ts:183`, because `--ds-navy` is a band
numeral colour. A fix that leaves the palette unable to adopt a non-hex notation
has not finished the job.

**Files:**
- Modify: `frontend/tests/unit/contrast.ts`
- Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the failing tests**

```ts
it('sweeps a colour token written in a non-hex notation', () => {
  const css = '--ds-base: #12151c;\n--ds-alias: var(--ds-base);\n--ds-rgb: rgb(66, 133, 244);\n';
  expect(colorTokens(css)).toEqual({
    '--ds-base': '#12151c',
    '--ds-alias': '#12151c',
    '--ds-rgb': '#4285f4',
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
  expect(() => resolveColor('', '2px solid #fff')).toThrow(/not a colour|shorthand/i);
  // Pre-existing hole: /^#[0-9a-fA-F]{3,8}$/ accepted invalid 5- and 7-digit hex.
  expect(() => resolveColor('', '#abcde')).toThrow();
});
```

**Step 2: Run — FAIL** on all four.

**Step 3: Implement**

```ts
export function colorTokens(css: string): Record<string, string> {
  const palette = paletteOf(css);
  const out: Record<string, string> = {};
  for (const [name, value] of palette) {
    const classified = classifyTokenValue(palette, name, value);
    if (classified.kind === 'color') out[name] = classified.hex;
  }
  return out;
}

export function resolveColor(css: string, value: string): string {
  const classified = classifyTokenValue(paletteOf(css), '(value)', value);
  if (classified.kind !== 'color') throw new Error(`not a colour: "${value}" (${classified.why})`);
  return classified.hex;
}
```

`resolveColor` keeps its contract — throws on anything non-opaque or
unresolvable — and gains every notation the classifier understands. Its `depth`
parameter goes away; no caller passes one (verified), and the internal recursive
call is replaced by the classifier's own.

**Step 4: Run the whole unit suite**

Run: `cd frontend && npm run test:unit -- --run`
Expected: PASS, count strictly above the Task 1 baseline of 1915. The ds-2fp
premise test at `contrast.test.ts:415-460` leans hardest on `resolveColor`
throwing — if anything there fails, that is a real finding; report it rather
than loosening the classifier to fit.

**Step 5: Commit**

```bash
git add frontend/tests/unit/contrast.ts frontend/tests/unit/contrast.test.ts
git commit -m "fix(ui): the palette sweep discovers colours in any notation, or fails (ds-spu)"
```

---

### Task 7: Pin the single-file scope premise

**Files:** Test: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the test**

**This is the round-8 version, and there are now TWO tests.** The first draft
used `tokenDeclarations()` over `.svelte`/`.css` only; rounds 4, 5 and 6 each
proved the then-current guard fail-open. The source test below keeps the rules
that suit a textual substrate; the compiled test that follows it replaces
source-scanning for Svelte templates entirely. Read the shipped file for the
authoritative body — the point of this section is the rule set and its proofs.

```ts
it('is reading the WHOLE palette — no --ds-* is declared outside tokens.css', () => {
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(svelte|css|ts|js|html)$/.test(entry.name)) files.push(full);
    }
  };
  walk(srcDir);
  // Premise: the walk reached the tree AND can see its own subject. A guard
  // that cannot reach what it checks reports clean and proves nothing (#293).
  // One assertion per file type that a rule below actually needs.
  expect(files.length).toBeGreaterThan(30);
  const tokensFile = files.find((f) => f.endsWith(join('styles', 'tokens.css')));
  expect(tokensFile, 'walk never reached tokens.css').toBeDefined();
  expect(files.filter((f) => f.endsWith('.ts')).length).toBeGreaterThan(0);
  expect(files.filter((f) => f.endsWith('.svelte')).length).toBeGreaterThan(0);

  const strays: string[] = [];
  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    const where = relative(srcDir, file);
    if (file !== tokensFile) {
      // Tolerates a comment between the name and the colon, which CSS allows.
      for (const m of source.matchAll(/--ds-[\w-]+(?=(?:\s|\/\*[\s\S]*?\*\/)*:)/g)) {
        strays.push(`${where} declares ${m[0]}`);
      }
    }
    for (const m of source.matchAll(/\bstyle:--[\w-]*/g)) {
      strays.push(`${where} sets ${m[0]} via a Svelte style directive`);
    }
    // The NAME, never a dotted call: `style.setProperty(…)`,
    // `style['setProperty'](…)` and `style.setProperty?.(…)` are one write.
    // `setAttribute` is here because `setAttribute('style','--ds-x: red')` also
    // declares the property without touching `.style` at all.
    for (const m of source.matchAll(
      /\bsetProperty\b|\bcssText\b|\bsetAttribute\b|\.style\s*\??\s*\[/g
    )) {
      strays.push(`${where} writes style via CSSOM (offset ${m.index})`);
    }
    // The scanner cannot DECODE escapes, so it flags their PRESENCE. Zero today.
    if (/\.(css|svelte)$/.test(file)) {
      const bs = [...source.matchAll(/\\/g)];
      if (bs.length > 0) strays.push(`${where} contains ${bs.length} backslash escape(s)`);
    }
  }
  expect(
    strays,
    `the ring sweep reads only tokens.css, so a --ds-* declared elsewhere is never measured:\n  ${strays.join('\n  ')}`
  ).toEqual([]);
});
```

`readdirSync` is already imported; add `join` and `relative` to the `node:path`
import.

**Step 2: Run, then prove it can fail — in all ELEVEN ways a stray arrives**

Green immediately (there are no strays), which proves nothing on its own. One
injection is nowhere near enough here: this guard was rewritten twice, and each
time the replacement still passed spellings the previous one had missed. Every
one of the five below was verified in a real browser to actually declare the
token before being used as an injection.

```bash
# A. inline style attribute — terminated by a quote, not ; or }
#    <div style="--ds-esc-inline: #00ff00"></div>            in any .svelte
# B. CSSOM write in a .ts file, which the walk must open at all
#    document.documentElement.style.setProperty('--ds-x', v) in src/main.ts
# C. Svelte style DIRECTIVE — no `--ds-x:` and no `.setProperty(` in the source
#    <div style:--ds-esc-directive={'#00ff00'}></div>        in any .svelte
# D. a comment between the property name and the colon
printf '\n:root { --ds-esc-comment/**/: #00ff00; }\n' >> src/styles/base.css
# E. an escaped identifier: \73 decodes to "s", so this IS --ds-...
printf '\n:root { --d\\73 -esc-escaped: #00ff00; }\n'  >> src/styles/base.css
# F. Svelte style OBJECT — nothing like C in source, identical after compiling
#    <div style={{ '--ds-x': '#00ff00' }}></div>          in any .svelte
# G. the same object reached through a const (STATIC INDIRECTION)
#    <script>const s={'--ds-x':'#0f0'}</script><div style={s}></div>
# H. a regex literal beside it, which broke the hand-rolled paren matcher
#    <div style={{ t: /[)]/.source, '--ds-x': '#0f0' }}></div>
# I. bracketed CSSOM       style['setProperty']('--ds-x', v)   in src/main.ts
# J. optional-call CSSOM   style.setProperty?.('--ds-x', v)    in src/main.ts
# K. setAttribute — declares the property without touching .style at all
#    el.setAttribute('style', '--ds-x: #ff0000')          in src/main.ts

npm run test:unit -- --run tests/unit/contrast.test.ts -t 'WHOLE palette'   # each must FAIL, naming the file
```

Restore with `cp` from a backup rather than `git checkout` if anything else in
the tree is uncommitted.

**Step 3: Commit**

```bash
git add frontend/tests/unit/contrast.test.ts
git commit -m "test(ui): pin that tokens.css is the whole palette (ds-spu)"
```

---

### Task 8: Update the design record in `tokens.css`

**Files:** Modify: `frontend/src/styles/tokens.css`

The `--ds-ring` comment says *"over all 32 opaque palette tokens — what the test
actually sweeps"*. That number is now a consequence of classification rather
than of a hex filter. Add one sentence: the sweep takes any notation it can
resolve and fails on the rest, so the next author does not read "32" as a
constraint on how a token may be written. No figures change.

Run `npm run build` after — `tokens.css` ships.

```bash
git add frontend/src/styles/tokens.css
git commit -m "docs(ui): the sweep counts colours, not hex literals (ds-spu)"
```

---

### Task 9: Injection sweep

Apply, run `npm run test:unit -- --run`, confirm the stated outcome, then
`git checkout -- <file>`.

Nine of the first draft's entries were dishonest — they would have "confirmed"
the guard while proving nothing, or failed for the wrong reason. Every review
round found more; the reason is kept in each row so the mistake is not repeated.
Rows 17-19 exist because rounds 4 and 5 each showed that *one* injection against
the scope guard had been passing while three real spellings walked through it.

| # | injection | expected | note |
|---|---|---|---|
| 1 | `COLOR_FUNCTIONS` loses `oklch` | notation test REDDENS | only because Task 4 asserts the specific `oklch() colour notation` message. Drafts 1-2 asserted merely "threw", and the unknown-function throw *also* names the token and function — so this stayed green and looked like proof |
| 2 | `COLOR_FUNCTIONS` loses `color-mix` | notation test REDDENS | same; ds-1um's exact future value |
| 3 | unknown-function branch returns `{kind:'not-a-color', why:'x'}` | unknown-function test REDDENS | **round 1's actual bug** |
| 4 | `var()` closed-world check deleted (follow the fallback when absent) | the not-in-palette test REDDENS | **round 2's finding** |
| 5 | the fallback IS followed when the primary is a declared non-colour | that test REDDENS | replaced in round 4: the throw this used to pin was itself the bug |
| 5b | the `initial` / cycle / unresolved-`var()` primaries return the target's verdict instead of throwing | the guaranteed-invalid test REDDENS | round 5. These are the cases where the browser really does follow the fallback, so "declared ⇒ ignore the fallback" is only sound while they throw |
| 6 | structural branch returns `{kind:'color', hex:'#000000'}` | shorthand-alias test REDDENS | must be a *valid* `TokenClass` — draft 2 wrote `{kind:'color'}`, which fails to compile and would have reddened for the wrong reason. Accounting's shape assertion does NOT catch this |
| 7 | `topLevelParts` stops tracking quotes | the `"a,b"` test REDDENS | draft 2 had no such test and pointed at the font stacks, which are multipart either way and prove nothing |
| 8 | 4/8-digit hex branch returns `{kind:'color', hex: first 6 digits}` | the `#12151c6b` test REDDENS | draft 2 merely *deleted* the throw, which fell through to "not a valid hex" — reddening for the diagnostic, not for the safety property |
| 9 | cycle detection replaced by a depth counter of 1000 | the cycle test REDDENS on the *diagnostic* | draft 1 raised the limit but kept the message, so `/deeper than 8/` still matched after 1001 recursions. Detecting by name removes the class; assert `/cycle/i`, not merely that it threw |
| 10 | `NON_COLOR_KEYWORDS` gains `white` | bare-keyword test REDDENS | |
| 11 | `!important` strip removed | that test REDDENS | |
| 12 | `colorTokens` wraps the classifier in `try {} catch {}` | throw-propagation test REDDENS | draft 1 expected *accounting* to catch this; it would not, since today's palette classifies cleanly |
| 13 | alias lookup switched back to `readToken()` | the inline-alias test REDDENS | round 2's third finding. Draft 3's version of that test only asserted `paletteOf(...).get('--ds-a')` and never resolved `--ds-b`, so this injection stayed green — it now classifies the alias |
| 14 | `scan()` reuses the regex comment-strip | the independence fixture REDDENS | draft 3 recorded this as "unproven" on the grounds that a `/*`-in-a-string token cannot occur here. That was rationalising: the font tokens *are* quoted strings, and pinning `scan()` on a fixture costs one assertion |
| 15 | **positive control:** `--ds-navy: rgb(14, 27, 95)` in `tokens.css` | **stays GREEN**, `--ds-navy` still swept | only works because Task 6 routed `resolveColor` too; without it the band test throws. This is the proof the fix achieves its purpose |
| 16 | **acceptance criterion:** `--ds-navy: oklch(0.3 0.15 270)` | FAILS naming `--ds-navy` and `oklch` | the bead's wording, verbatim |
| 17 | scope guard: `<div style:--ds-x={'#f00'}>` in a `.svelte` | scope test REDDENS naming the directive | round 5. A Svelte style **directive** — the source has neither `--ds-x:` nor `.setProperty(`, so both earlier guards passed it. The realistic one of the three |
| 18 | scope guard: `--ds-x/**/: #f00` in `base.css` | scope test REDDENS naming the file | round 5. CSS allows a comment between name and colon; the old `\s*:` lookahead did not |
| 19 | scope guard: `--d\73-bg: #f00` in `base.css` | scope test REDDENS on the **escape**, not the name | round 5. `\73` decodes to `s`. The guard cannot decode it and does not pretend to — it reports the backslash itself |
| 20 | scope guard: `<div style={{ '--ds-x': '#f00' }}>` in a `.svelte` | the **compiled-output** test REDDENS | round 6. A Svelte style *object* — nothing like the directive in source, identical after compilation. This is the row that justifies compiling instead of scanning |
| 21 | scope guard: `style['setProperty']('--ds-x', …)` in a `.ts` | CSSOM rule REDDENS | round 6. Bracketed access; the old `\.setProperty\s*\(` regex required a dot |
| 22 | scope guard: `style.setProperty?.('--ds-x', …)` in a `.ts` | CSSOM rule REDDENS | round 6. Optional call; same regex, same miss |
| 23 | the compiled-output pipeline yields nothing (parse returns an empty body) | the compiled-output test REDDENS on its **positive controls** | round 6/7. Without them, a compiler or parser change would leave the guard inspecting zero writes and reporting clean |
| 24 | scope guard: `<script>const s={'--ds-x':…}</script><div style={s}>` | compiled-output test REDDENS | round 7. **Static indirection** — not a computed name. The literal is in the module but not in the `set_style` call, which is why argument-scanning was replaced by a full parse |
| 25 | scope guard: `<div style={{ t: /[)]/.source, '--ds-x': … }}>` | compiled-output test REDDENS | round 7. The `)` in a regex character class ended the hand-rolled paren match early. Pinned by a positive control so the parser cannot regress to a matcher |
| 26 | the name match drops its `(?:^\|[^\w-])` anchor | the BEM **negative control** REDDENS | round 7. ~120 `btn--ds-*` class names would flag; an unusable guard gets deleted, not fixed |
| 27 | scope guard: `el.setAttribute('style', '--ds-x: #ff0000')` in a `.ts` | CSSOM rule REDDENS | round 8. Declares the property touching neither `.style` nor `setProperty`. Zero `setAttribute` in `src/`, so the blanket name is free |

Record each outcome in the PR body, including #14's honest "unproven".

---

### Task 10: Gates, then hand off

```bash
cd frontend
npm run test:unit -- --run
npm run check
npm run build
cd .. && uv run ruff check .
```

`test:smoke` is unaffected unless `src/` changed beyond the `tokens.css`
comment; run it if so.

Then: push `fix/ds-spu-palette-sweep-notations`, open the PR (body covers the
fail-open hole, the inverted allowlist direction and why draft 1 was wrong, the
closed-world `var()` policy, the `resolveColor` routing, the scope pin, and the
injection table), `mcp__codex__codex-reply` on the review thread, and
`bd close ds-spu` — at which point `ds-1um` unblocks.
