# PR-body Markdown rendering (open-trace disclosure) — design

**Date:** 2026-06-27
**Branch:** `feat/pr-body-markdown` (off `2c8b949`, the #151 merge)
**Scope:** frontend-only (display layer). Coordinator-only deploy (SPA baked in `driftscribe-agent`).

## Problem

The open-trace "What this change did (from the PR)" disclosure (shipped in #151)
renders the agent-authored PR body as `<pre>{body}</pre>`. Two operator-reported
papercuts:

1. **Raw Markdown markers** — `##`, `**bold**`, `- bullet`, `` `code` `` print
   literally because `<pre>{body}` escapes to literal text (the deliberate
   no-`{@html}`/no-md-library XSS stance).
2. **Missing 🤖 emoji** — *confirmed served* (live probe of
   `/trace/{id}/pr-body`: `contains U+1F916 == True`, `body_len 920`). It is the
   monospace `--ds-font-mono` `<pre>` (no color-emoji glyph / no emoji fallback)
   that drops it, NOT scrubbing.

Operator chose (AskUserQuestion): **render the Markdown** (vs strip to plain text).

## Decision

Render a **small, hand-rolled Markdown SUBSET** through Svelte's auto-escaping
native elements. **No `{@html}`. No markdown library.** XSS-safe by construction
(every text leaf is a `{value}` interpolation; the only attribute is a
scheme-allowlisted link `href`). Moving the body out of the monospace `<pre>`
into prose font fixes the emoji for free (sans → system emoji fallback).

### Supported subset (matches what agent PR-body templates emit)

Block: ATX headings `#`..`######`; paragraphs (blank-line separated; single
newline → `<br>`); unordered lists (`-`/`*`/`+`); ordered lists (`1.`); fenced
code blocks ```` ``` ````.
Inline: `**strong**`/`__strong__`, `*em*`/`_em_`, `` `code` `` (incl. multi-backtick
runs), `[text](href)`, backslash escapes (`\*` `` \` `` `\_` `\[`), hard line breaks.

### Safety rules

- **No `{@html}`** anywhere. Headings render as styled `<p class="md-heading">`
  (NOT real `<h*>`) to avoid polluting the page heading outline / a11y order.
- **Inline code parsed FIRST** so identifiers (`service_account`,
  `template.service_account`) are protected from `_`/`*` emphasis mangling.
- **Underscore emphasis is word-boundary-gated** (opener not preceded by an
  alphanumeric, closer not followed by one) so `service_account` in *prose* is
  never italicised. Asterisk emphasis uses normal matching.
- **Unclosed delimiters fall back to literal text** (robust to truncated bodies).
- **Link href allowlist** (`safeMarkdownLinkHref`): `http:`/`https:` ONLY (no
  `mailto:` — no template emits it, and it widens the surface; dropped per Codex);
  reject control chars / whitespace / backslash / angle brackets / userinfo; on
  reject, render the link *text* as plain inline (drop the anchor) so nothing
  unsafe reaches the DOM. Links may not nest (the label is parsed with link
  syntax disabled), so an unsafe outer link can't leak a safe inner anchor.
  External links get `target="_blank" rel="noopener noreferrer"`.
- **Parser never throws**; the component additionally wraps `parseMarkdown` in a
  try/catch that fails soft to a single plain-text paragraph.
- Recursion depth cap (inline emphasis/link nesting) to bound pathological input.

## Files

- NEW `frontend/src/lib/markdown.ts` — `parseMarkdown(src): BlockNode[]` (pure) +
  `safeMarkdownLinkHref(raw): string | null`. Exported AST types.
- NEW `frontend/tests/unit/markdown.test.ts` — exhaustive parser unit tests
  (headings, lists, emphasis, code spans + fences, links incl. `javascript:`
  rejection, intraword underscore protection, unclosed delimiters, escapes,
  emoji passthrough).
- MOD `frontend/src/components/PrBodyDisclosure.svelte` — render the AST via an
  `{#each}` block switch + a recursive inline `{#snippet}`; new prose styles;
  keep `data-testid="pr-body-disclosure"`, the null/empty guard, and the
  truncated note. Fail-soft try/catch around the parse.
- MOD `frontend/tests/unit/PrBodyDisclosure.test.ts` — update the two assertions
  that pinned the old `<pre>` textContent; add render tests (`<strong>`,
  `<code>`, safe `<a>`, escaping → no `<img>`/`<script>`, emoji present,
  no-`{@html}` evidence via attribute/text inspection).

## Non-goals / faithfulness note

- The **body content is unchanged** — internal codenames (C5f/C5g/C2) stay; the
  disclosure faithfully shows the real GitHub PR body (secret-scrubbed only). PR
  bodies remain Markdown on GitHub by deliberate team choice. De-jargoning future
  PR bodies is a separate optional agent-prompt tweak, out of scope here.
- No nested lists, tables, blockquotes, images, or raw-HTML passthrough in v1
  (agent templates don't emit them; unsupported syntax degrades to readable text).
- Backend untouched (`/trace/{id}/pr-body` already serves raw body + `truncated`).

## Verify

`npx vitest run` (full FE suite, was 735) + `svelte-check` (0) + `npm run build`.
Then coordinator-only deploy + live open-trace #32 shows headings/bold/bullets +
the 🤖. Codex plan + completed-work review; multi-lens adversarial Workflow
(XSS/correctness/a11y/regression/test-quality).
