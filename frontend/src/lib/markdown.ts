// frontend/src/lib/markdown.ts
//
// A small, deliberately BORING Markdown SUBSET parser for rendering the
// agent-authored PR body in the open-trace "What this change did (from the PR)"
// disclosure. It is NOT a CommonMark implementation — it is a "template markdown
// renderer" for the headings / bold / italics / inline code / fenced code /
// lists / links / GFM-tables that the agent's PR-body prose actually uses.
// Unsupported syntax degrades to readable literal text.
//
// SECURITY: this module produces a pure AST of plain values. PrBodyDisclosure
// renders that AST through Svelte's auto-escaping native elements (NEVER
// {@html}), so the ONLY attribute carrying parsed content is a link `href`,
// which is strictly allowlisted by `safeMarkdownLinkHref`. We never decode HTML
// entities, so `jav&#x61;script:` / `&lt;script&gt;` stay literal text.
//
// The parser never throws; the renderer additionally fails soft to plain text.

export type InlineNode =
  | { type: 'text'; value: string }
  | { type: 'br' }
  | { type: 'strong'; children: InlineNode[] }
  | { type: 'em'; children: InlineNode[] }
  | { type: 'code'; value: string }
  | { type: 'link'; href: string; children: InlineNode[] };

export type BlockNode =
  | { type: 'heading'; level: number; children: InlineNode[] }
  | { type: 'paragraph'; children: InlineNode[] }
  | { type: 'list'; ordered: boolean; items: InlineNode[][] }
  | { type: 'codeblock'; value: string }
  | { type: 'table'; header: InlineNode[][]; rows: InlineNode[][][] };

// Inline emphasis/link nesting cap. Beyond this the parser literalizes the
// remaining text so the renderer stays a dumb walker (no runaway recursion).
const MAX_INLINE_DEPTH = 6;

/**
 * Allowlist guard for a Markdown link destination. Accepts ONLY `http:` /
 * `https:` (no `mailto:` — no template emits it, and it widens the surface).
 * Rejects non-strings, empties, anything with control chars / whitespace /
 * backslash / angle brackets, and any embedded userinfo (`good.com@evil.com`).
 * Returns the URL-normalized href on success, else null. We deliberately do
 * NOT decode HTML entities, so an entity-encoded scheme can never slip through.
 */
export function safeMarkdownLinkHref(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw === '') return null;
  // Reject up front so no URL-parser normalization trick slips a control char,
  // newline, tab, space, backslash, or angle bracket through.
  if (/[\u0000-\u001f\u007f\s\\<>]/.test(raw)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  if (u.username !== '' || u.password !== '') return null;
  return u.href;
}

// ----------------------------------------------------------------------------
// Block parsing
// ----------------------------------------------------------------------------

const HEADING_RE = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE_RE = /^\s*(`{3,}|~{3,})(.*)$/;
const LIST_ITEM_RE = /^\s*(?:[-*+]|\d+[.)])\s+(.*)$/;
const ORDERED_RE = /^\s*\d+[.)]\s+/;

function isListItem(line: string): boolean {
  return LIST_ITEM_RE.test(line);
}

function isTableRow(line: string): boolean {
  return line.includes('|') && line.trim() !== '';
}

function isTableDelimiter(line: string): boolean {
  const t = line.trim();
  if (!t.includes('-') || !t.includes('|')) return false;
  return /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/.test(t);
}

function startsBlock(lines: string[], i: number): boolean {
  const line = lines[i];
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    isListItem(line) ||
    (isTableRow(line) && i + 1 < lines.length && isTableDelimiter(lines[i + 1]))
  );
}

// Split a table row on UNESCAPED pipes, turning `\|` into a literal `|`. A
// hand scanner (not a lookbehind regex) so the module parses on every engine —
// a lookbehind SyntaxError would throw at import time, before safeParse() could
// fail soft.
function splitTableCells(line: string): string[] {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
  const cells: string[] = [];
  let buf = '';
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (ch === '\\' && t[i + 1] === '|') {
      buf += '|';
      i++;
      continue;
    }
    if (ch === '|') {
      cells.push(buf.trim());
      buf = '';
      continue;
    }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

export function parseMarkdown(src: string): BlockNode[] {
  if (typeof src !== 'string' || src === '') return [];
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const blocks: BlockNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line — block separator.
    if (line.trim() === '') {
      i++;
      continue;
    }

    // Fenced code block.
    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1][0]; // ` or ~
      const len = fence[1].length;
      const buf: string[] = [];
      i++;
      while (i < lines.length) {
        const close = lines[i].match(/^\s*(`{3,}|~{3,})\s*$/);
        if (close && close[1][0] === marker && close[1].length >= len) {
          i++;
          break;
        }
        buf.push(lines[i]);
        i++;
      }
      blocks.push({ type: 'codeblock', value: buf.join('\n') });
      continue;
    }

    // ATX heading (drop an empty "# " so it doesn't emit a blank heading).
    const h = line.match(HEADING_RE);
    if (h) {
      const children = parseInline(h[2]);
      if (children.length > 0) {
        blocks.push({ type: 'heading', level: h[1].length, children });
      }
      i++;
      continue;
    }

    // GFM table (header row immediately followed by a delimiter row).
    if (isTableRow(line) && i + 1 < lines.length && isTableDelimiter(lines[i + 1])) {
      const header = splitTableCells(line).map((c) => parseInline(c));
      i += 2;
      const rows: InlineNode[][][] = [];
      while (i < lines.length && lines[i].trim() !== '' && isTableRow(lines[i])) {
        rows.push(splitTableCells(lines[i]).map((c) => parseInline(c)));
        i++;
      }
      blocks.push({ type: 'table', header, rows });
      continue;
    }

    // List — a run of consecutive list items of the SAME kind. A marker-type
    // change (e.g. `-` then `1.`) starts a fresh list instead of latching.
    if (isListItem(line)) {
      const ordered = ORDERED_RE.test(line);
      const items: InlineNode[][] = [];
      while (i < lines.length && isListItem(lines[i]) && ORDERED_RE.test(lines[i]) === ordered) {
        const m = lines[i].match(LIST_ITEM_RE);
        items.push(parseInline(m ? m[1] : lines[i]));
        i++;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    // Paragraph — consume until a blank line or the start of another block.
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== '' && !startsBlock(lines, i)) {
      para.push(lines[i]);
      i++;
    }
    blocks.push({ type: 'paragraph', children: parseInlineMultiline(para) });
  }

  return blocks;
}

// ----------------------------------------------------------------------------
// Inline parsing
// ----------------------------------------------------------------------------

const ESCAPABLE = new Set(['\\', '`', '*', '_', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|', '~', '>']);

function isAlnum(ch: string | undefined): boolean {
  return ch !== undefined && /[A-Za-z0-9]/.test(ch);
}

// Treat end-of-string like whitespace for emphasis flanking checks.
function isSpaceOrEnd(ch: string | undefined): boolean {
  return ch === undefined || /\s/.test(ch);
}

function countRun(text: string, start: number, ch: string): number {
  let n = 0;
  while (start + n < text.length && text[start + n] === ch) n++;
  return n;
}

// Join multiple raw paragraph lines into one inline stream, inserting an
// explicit <br> between source lines (GFM treats a single newline as a break).
function parseInlineMultiline(srcLines: string[]): InlineNode[] {
  const out: InlineNode[] = [];
  srcLines.forEach((ln, idx) => {
    if (idx > 0) out.push({ type: 'br' });
    out.push(...parseInline(ln));
  });
  return out;
}

// `inLink` is true while parsing a link's LABEL: links may not nest (CommonMark),
// so the `[` branch is disabled there. This both keeps the HTML valid (no
// <a> inside <a>) and preserves the "unsafe outer link degrades to plain text"
// guarantee (a label can never smuggle a safe inner anchor out of an unsafe one).
export function parseInline(text: string, depth = 0, inLink = false): InlineNode[] {
  if (depth > MAX_INLINE_DEPTH) return text ? [{ type: 'text', value: text }] : [];
  const nodes: InlineNode[] = [];
  let buf = '';
  let i = 0;
  const flush = () => {
    if (buf) {
      nodes.push({ type: 'text', value: buf });
      buf = '';
    }
  };

  while (i < text.length) {
    const c = text[i];

    // Backslash escape of a markdown punctuation char.
    if (c === '\\' && i + 1 < text.length && ESCAPABLE.has(text[i + 1])) {
      buf += text[i + 1];
      i += 2;
      continue;
    }

    // Inline code span (a run of N backticks closed by another run of N).
    if (c === '`') {
      const run = countRun(text, i, '`');
      const close = findCodeClose(text, i + run, run);
      if (close !== -1) {
        flush();
        let code = text.slice(i + run, close);
        // CommonMark: one leading + trailing space is stripped iff both present
        // and the content is not all-spaces (lets `` ` `` render a literal tick).
        if (code.length >= 2 && code.startsWith(' ') && code.endsWith(' ') && code.trim() !== '') {
          code = code.slice(1, -1);
        }
        nodes.push({ type: 'code', value: code });
        i = close + run;
        continue;
      }
      buf += text.slice(i, i + run);
      i += run;
      continue;
    }

    // Link [text](dest) — never inside another link's label.
    if (c === '[' && !inLink) {
      const link = tryParseLink(text, i, depth);
      if (link) {
        flush();
        nodes.push(...link.nodes);
        i = link.next;
        continue;
      }
      // not a link — fall through, '[' becomes literal below
    }

    // Emphasis with * or _ — pair the opener run with the next same-char run.
    if (c === '*' || c === '_') {
      const run = countRun(text, i, c);
      // Underscore can NOT open emphasis intraword: if the char before the WHOLE
      // run is alphanumeric, the entire run is literal (protects service_account
      // AND double-underscore identifiers like MY__CONST__VALUE / a__b__c).
      if (c === '_' && isAlnum(text[i - 1])) {
        buf += c.repeat(run);
        i += run;
        continue;
      }
      // Left-flanking: an opener can't be followed by whitespace / end, so
      // whitespace-flanked delimiters (`2 * 3 * 4`, `* foo`) are NOT emphasis.
      if (isSpaceOrEnd(text[i + run])) {
        buf += c;
        i += 1;
        continue;
      }
      const closeStart = findDelimRun(text, i + run, c);
      if (closeStart > i + run) {
        const closeRun = countRun(text, closeStart, c);
        // Pair up to min(open, close) delimiters; render 1=em, 2=strong, 3=both.
        const levels = Math.min(run, closeRun, 3);
        const leadLiteral = run - levels; // leftover OPENER delimiters stay literal
        if (leadLiteral > 0) buf += c.repeat(leadLiteral);
        flush();
        const inner = parseInline(text.slice(i + run, closeStart), depth + 1, inLink);
        nodes.push(wrapEmphasis(levels, inner));
        i = closeStart + levels; // leftover CLOSER delimiters get reprocessed
        continue;
      }
      // No valid closer — consume one delimiter char literally and continue
      // (remaining run chars get reconsidered on the next iteration).
      buf += c;
      i += 1;
      continue;
    }

    buf += c;
    i++;
  }

  flush();
  return nodes;
}

// Find a closing backtick run of EXACTLY `run` backticks starting at/after `from`.
function findCodeClose(text: string, from: number, run: number): number {
  let i = from;
  while (i < text.length) {
    if (text[i] === '`') {
      const n = countRun(text, i, '`');
      if (n === run) return i;
      i += n;
    } else {
      i++;
    }
  }
  return -1;
}

// Find a closing emphasis delimiter (`want` copies of `char`) at/after `from`.
// For underscore, the closer must also be at a word boundary (the char AFTER
// the closing run must not be alphanumeric) so `a_b_c` stays literal.
// Find the start of the next run of `char` at/after `from` that can CLOSE
// emphasis. For underscore the closer can't be intraword on the right (the char
// after the run must not be alphanumeric), so `a_b_c` stays literal. Returns the
// run start, or -1.
function findDelimRun(text: string, from: number, char: string): number {
  let i = from;
  while (i < text.length) {
    const ch = text[i];
    // Skip an escaped char so `\*` can't be selected as a closer.
    if (ch === '\\') {
      i += 2;
      continue;
    }
    // Skip a code span so a `*` inside `` `*.tf` `` can't close emphasis.
    if (ch === '`') {
      const r = countRun(text, i, '`');
      const close = findCodeClose(text, i + r, r);
      i = close !== -1 ? close + r : i + r;
      continue;
    }
    // Skip a `[label](dest)` link span so a delimiter in the label can't close.
    if (ch === '[') {
      const bounds = linkSpanBounds(text, i);
      if (bounds) {
        i = bounds.destEnd + 1;
        continue;
      }
    }
    if (ch === char) {
      const r = countRun(text, i, char);
      // Right-flanking: a closer can't be preceded by whitespace.
      if (isSpaceOrEnd(text[i - 1])) {
        i += r;
        continue;
      }
      // Underscore closer can't be intraword on the right.
      if (char === '_' && isAlnum(text[i + r])) {
        i += r;
        continue;
      }
      return i;
    }
    i++;
  }
  return -1;
}

// Wrap inner nodes in `levels` of emphasis: 1 = em, 2 = strong, 3+ = em>strong
// (the CommonMark nesting for ***bold italic***).
function wrapEmphasis(levels: number, inner: InlineNode[]): InlineNode {
  if (levels >= 3) return { type: 'em', children: [{ type: 'strong', children: inner }] };
  if (levels === 2) return { type: 'strong', children: inner };
  return { type: 'em', children: inner };
}

// Strip a single matched <...> wrapper from a link destination (markdown allows
// `[t](<dest>)`); the angle brackets are then rejected by the href guard if any
// remain, so only a cleanly-wrapped dest survives.
function unwrapAngle(dest: string): string {
  if (dest.length >= 2 && dest.startsWith('<') && dest.endsWith('>')) {
    return dest.slice(1, -1);
  }
  return dest;
}

interface LinkParse {
  nodes: InlineNode[];
  next: number;
}

// Locate the `]` (textEnd) and `)` (destEnd) of a `[label](dest)` span starting
// at `start` (text[start] === '['), with balanced brackets/parens and backslash
// escapes skipped. Returns null if it isn't a well-formed link. Shared by
// tryParseLink (to parse it) and findDelimRun (to SKIP it during a closer scan,
// so a delimiter inside a link label can't close an outer emphasis span).
function linkSpanBounds(text: string, start: number): { textEnd: number; destEnd: number } | null {
  let bracket = 0;
  let textEnd = -1;
  for (let j = start; j < text.length; j++) {
    const ch = text[j];
    if (ch === '\\') {
      j++;
      continue;
    }
    if (ch === '[') bracket++;
    else if (ch === ']') {
      bracket--;
      if (bracket === 0) {
        textEnd = j;
        break;
      }
    }
  }
  if (textEnd === -1 || text[textEnd + 1] !== '(') return null;

  let paren = 0;
  for (let k = textEnd + 1; k < text.length; k++) {
    const ch = text[k];
    if (ch === '\\') {
      k++;
      continue;
    }
    if (ch === '(') paren++;
    else if (ch === ')') {
      paren--;
      if (paren === 0) return { textEnd, destEnd: k };
    }
  }
  return null;
}

// Parse `[text](dest)` at `start` (text[start] === '['). Returns the produced
// inline nodes + the index just past the link, or null if it isn't a link.
// On an UNSAFE/invalid dest, degrades to the link TEXT as plain inline (the
// anchor and the dangerous URL are dropped entirely).
function tryParseLink(text: string, start: number, depth: number): LinkParse | null {
  const bounds = linkSpanBounds(text, start);
  if (!bounds) return null;
  const { textEnd, destEnd } = bounds;

  const linkText = text.slice(start + 1, textEnd);
  const dest = unwrapAngle(text.slice(textEnd + 2, destEnd).trim());
  const href = safeMarkdownLinkHref(dest);
  if (!href) {
    // Unsafe / unparseable destination: keep the readable text, drop the link.
    return { nodes: parseInline(linkText, depth + 1, true), next: destEnd + 1 };
  }
  // Empty label ([](url)) would render an unreadable empty anchor — show the URL.
  const children =
    linkText.trim() === ''
      ? [{ type: 'text', value: href } as InlineNode]
      : parseInline(linkText, depth + 1, true);
  return { nodes: [{ type: 'link', href, children }], next: destEnd + 1 };
}
